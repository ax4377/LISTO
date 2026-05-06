import os
import json
import logging
import base64
import asyncio
import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

from prompt import SYSTEM_PROMPT

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NVIDIA_API_KEY = os.getenv('NVIDIA_API_KEY')
NVIDIA_MODEL = os.getenv('NVIDIA_MODEL', 'mistralai/mistral-large-3-675b-instruct-2512')
BOT_MODE = os.getenv('BOT_MODE', 'start').strip().lower()

if not TELEGRAM_BOT_TOKEN or not NVIDIA_API_KEY:
    raise ValueError("Missing required environment variables: TELEGRAM_BOT_TOKEN and NVIDIA_API_KEY")

# Media group batching
pending_groups = {}
scheduled_groups = set()

# Users file — Railway mein /tmp use karo (writable)
USERS_FILE = '/tmp/listo_users.json'

MAINTENANCE_MESSAGE = (
    "🔧 LISTO Bot — Maintenance Mode\n\n"
    "Abhi bot temporarily offline hai.\n"
    "Hum kuch improvements aur bug fixes kar rahe hain.\n\n"
    "Thodi der mein wapas aa jayenge!\n"
    "Inconvenience ke liye sorry 🙏"
)

ACTIVE_MESSAGE = (
    "✅ LISTO Bot — Ab Active Hai!\n\n"
    "Bot wapas aa gaya hai.\n"
    "Ab screenshots bhejo aur listing ready ho jayegi!\n\n"
    "🎮 Screenshot bhejo aur shuru karo"
)


# ── User storage ──────────────────────────────────────────────

def load_users() -> set:
    """Saved users ka set load karo."""
    try:
        with open(USERS_FILE, 'r') as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_user(chat_id: int) -> None:
    """Naye user ko file mein save karo."""
    users = load_users()
    if chat_id not in users:
        users.add(chat_id)
        try:
            with open(USERS_FILE, 'w') as f:
                json.dump(list(users), f)
            logger.info(f"New user saved: {chat_id} | Total users: {len(users)}")
        except Exception as e:
            logger.error(f"User save failed: {e}")


def is_maintenance() -> bool:
    return BOT_MODE == 'stop'


# ── Broadcast ─────────────────────────────────────────────────

async def broadcast_active(app: Application) -> None:
    """Sabhi saved users ko 'bot active' message bhejo."""
    users = load_users()
    if not users:
        logger.info("No saved users — broadcast skip")
        return

    logger.info(f"Broadcasting to {len(users)} users...")
    success, failed = 0, 0

    for chat_id in users:
        try:
            await app.bot.send_message(chat_id=chat_id, text=ACTIVE_MESSAGE)
            success += 1
            await asyncio.sleep(0.05)  # rate limit se bachne ke liye
        except Exception as e:
            logger.error(f"Broadcast failed for {chat_id}: {e}")
            failed += 1

    logger.info(f"Broadcast done — success: {success}, failed: {failed}")


# ── Handlers ──────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    save_user(chat_id)

    if is_maintenance():
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return

    welcome_message = (
        "🎮 Welcome to LISTO Bot\n\n"
        "BGMI account screenshots bhejo — main AI se analyze karke ek ready-to-post listing bana dunga.\n\n"
        "Kaise use kare:\n"
        "1. BGMI account ka screenshot bhejo\n"
        "2. AI stats extract karega\n"
        "3. Formatted listing turant mil jayegi\n\n"
        "Multiple screenshots ek saath bhej sakte ho — sab ek listing mein combine ho jayenge.\n\n"
        "Screenshot bhejo aur shuru karo!"
    )
    await update.message.reply_text(welcome_message)
    logger.info(f"User {chat_id} started LISTO bot")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    save_user(chat_id)

    if is_maintenance():
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return

    media_group_id = update.message.media_group_id
    file_id = update.message.photo[-1].file_id

    if media_group_id:
        logger.info(f"Received photo with media_group_id: {media_group_id}")
        if media_group_id not in pending_groups:
            pending_groups[media_group_id] = []
        pending_groups[media_group_id].append(file_id)

        if media_group_id not in scheduled_groups:
            scheduled_groups.add(media_group_id)
            asyncio.create_task(process_group_after_delay(media_group_id, chat_id, context))
    else:
        await process_single_photo(update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    save_user(chat_id)

    if is_maintenance():
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return

    await update.message.reply_text(
        "📸 BGMI account ka screenshot bhejo — main listing bana dunga!"
    )
    logger.info(f"User {chat_id} sent text message")


# ── Photo processing ───────────────────────────────────────────

async def process_group_after_delay(media_group_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.sleep(2)
    scheduled_groups.discard(media_group_id)
    file_ids = pending_groups.pop(media_group_id, [])

    if not file_ids:
        return

    total = len(file_ids)
    logger.info(f"Processing batch {media_group_id} with {total} photos")

    try:
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ {total} screenshot{'s' if total > 1 else ''} analyze ho raha hai... thoda wait karo"
        )

        base64_images = []
        for i, file_id in enumerate(file_ids, 1):
            try:
                file_info = await context.bot.get_file(file_id)
                photo_bytes = await file_info.download_as_bytearray()
                base64_images.append(base64.standard_b64encode(bytes(photo_bytes)).decode('utf-8'))
                logger.info(f"Downloaded photo {i}/{total}")
            except Exception as e:
                logger.error(f"Error downloading photo {i}: {e}")

        if not base64_images:
            await context.bot.send_message(chat_id=chat_id, text="❌ Screenshots download nahi ho sake. Dobara try karo.")
            return

        try:
            listing = await analyze_image_with_nvidia(base64_images)
        except httpx.HTTPStatusError as e:
            logger.error(f"NVIDIA API error: {e.response.status_code}")
            listing = "❌ AI API error. Thodi der baad dobara try karo."
        except Exception as e:
            logger.error(f"Analysis error: {e}")
            listing = "❌ Screenshot analyze nahi ho saka. Dobara try karo."

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except TelegramError:
            pass

        await send_listing(context, chat_id, listing)

    except Exception as e:
        logger.error(f"Unexpected batch error: {e}")
        try:
            await context.bot.send_message(chat_id=chat_id, text="❌ Kuch error aaya. Dobara try karo.")
        except TelegramError:
            pass


async def process_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        processing_msg = await update.message.reply_text("⏳ Screenshot analyze ho raha hai... thoda wait karo")

        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        base64_image = base64.standard_b64encode(bytes(photo_bytes)).decode('utf-8')

        logger.info(f"Single photo downloaded: {len(photo_bytes)} bytes")

        listing = await analyze_image_with_nvidia([base64_image])

        try:
            await processing_msg.delete()
        except TelegramError:
            pass

        await update.message.reply_text(listing, parse_mode='HTML')

    except httpx.HTTPStatusError as e:
        logger.error(f"NVIDIA API error: {e.response.status_code}")
        await update.message.reply_text("❌ AI API error. Thodi der baad dobara try karo.")
    except Exception as e:
        logger.error(f"Single photo error: {e}")
        await update.message.reply_text("❌ Screenshot analyze nahi ho saka. Dobara try karo.")


async def send_listing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, listing: str) -> None:
    max_len = 4096
    if len(listing) <= max_len:
        await context.bot.send_message(chat_id=chat_id, text=listing, parse_mode='HTML')
        return

    sections = listing.split('\n\n')
    current = ""
    for section in sections:
        if len(current) + len(section) + 2 <= max_len:
            current += section + '\n\n'
        else:
            if current:
                await context.bot.send_message(chat_id=chat_id, text=current.strip(), parse_mode='HTML')
            current = section + '\n\n'
    if current:
        await context.bot.send_message(chat_id=chat_id, text=current.strip(), parse_mode='HTML')


# ── NVIDIA API ────────────────────────────────────────────────

async def analyze_image_with_nvidia(base64_images: list) -> str:
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    content = [{"type": "text", "text": SYSTEM_PROMPT}]
    for img in base64_images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}})

    payload = {
        "model": NVIDIA_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 4096,
        "temperature": 0.15,
        "top_p": 1.00,
        "frequency_penalty": 0.00,
        "presence_penalty": 0.00,
        "stream": False
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        listing = data['choices'][0]['message']['content'].strip()
        logger.info(f"NVIDIA response: {len(listing)} chars for {len(base64_images)} images")
        return listing


# ── Main ──────────────────────────────────────────────────────

def main() -> None:
    logger.info("Starting LISTO bot")
    logger.info(f"NVIDIA model: {NVIDIA_MODEL}")
    logger.info(f"BOT_MODE: {BOT_MODE.upper()}")

    if is_maintenance():
        logger.info("MAINTENANCE MODE — users will see maintenance message")
    else:
        logger.info("ACTIVE MODE — normal operation")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # BOT_MODE=start pe broadcast — sabhi purane users ko active message
    if not is_maintenance():
        async def post_init(app: Application) -> None:
            await broadcast_active(app)
        application.post_init = post_init

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
