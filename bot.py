import os
import logging
import base64
import asyncio
import httpx

from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from supabase import create_client, Client

from prompt import SYSTEM_PROMPT

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Environment variables ─────────────────────────────────────

TELEGRAM_BOT_TOKEN  = os.getenv('TELEGRAM_BOT_TOKEN')
FREEMODEL_API_KEY   = os.getenv('FREEMODEL_API_KEY')
FREEMODEL_MODEL     = os.getenv('FREEMODEL_MODEL', 'google/gemma-4-31b-it:free')
BOT_MODE            = os.getenv('BOT_MODE', 'start').strip().lower()
SUPABASE_URL        = os.getenv('SUPABASE_URL')
SUPABASE_KEY        = os.getenv('SUPABASE_KEY')
BOT_USERNAME        = os.getenv('BOT_USERNAME', 'ListoAIbot')  # e.g. ListoBot (without @)

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing: TELEGRAM_BOT_TOKEN")
if not FREEMODEL_API_KEY:
    raise ValueError("Missing: FREEMODEL_API_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing: SUPABASE_URL or SUPABASE_KEY")

# ── Constants ─────────────────────────────────────────────────

DAILY_FREE_LIMIT  = 3    # Base free uses per day
REFERRAL_BONUS    = 5    # Bonus uses referrer ko milega
ADMIN_IDS         = {1787566342}  # Unlimited uses — no restrictions
IST               = timezone(timedelta(hours=5, minutes=30))

# ── Supabase client ───────────────────────────────────────────

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Media group batching ──────────────────────────────────────

pending_groups   = {}
scheduled_groups = set()

# ── Messages ──────────────────────────────────────────────────

MAINTENANCE_MESSAGE = (
    "🔧 LISTO Bot — Maintenance Mode\n\n"
    "The bot is temporarily offline.\n"
    "We're working on improvements and bug fixes.\n\n"
    "We'll be back shortly!\n"
    "Sorry for the inconvenience 🙏"
)

ACTIVE_MESSAGE = (
    "✅ LISTO Bot — Back Online!\n\n"
    "The bot is back up and running.\n"
    "Send your screenshots and get your listing ready!\n\n"
    "🎮 Send a screenshot to get started"
)



# ── IST helpers ───────────────────────────────────────────────

def get_ist_today() -> str:
    """Return today's IST date string (YYYY-MM-DD)."""
    return datetime.now(IST).strftime('%Y-%m-%d')


def seconds_until_midnight_ist() -> int:
    """Return seconds until IST midnight."""
    now = datetime.now(IST)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds())


def format_countdown(seconds: int) -> str:
    """Convert seconds to HH:MM:SS format."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── Supabase helpers ──────────────────────────────────────────

def save_user(chat_id: int, referred_by: int = None) -> bool:
    """Save user to Supabase. Returns True if new user, False if already exists."""
    try:
        # Check if user already exists
        result = supabase.table('listo_users').select('chat_id').eq('chat_id', chat_id).execute()
        if result.data:
            return False  # Already exist karta hai

        # Insert new user
        data = {
            'chat_id': chat_id,
            'daily_count': 0,
            'last_used_date': None,
            'bonus_uses': 0,
            'referred_by': referred_by,
            'referral_count': 0,
        }
        supabase.table('listo_users').insert(data).execute()
        logger.info(f"New user saved: {chat_id} | referred_by: {referred_by}")
        return True
    except Exception as e:
        logger.error(f"Supabase save_user error: {e}")
        return False


def load_all_users() -> list[int]:
    """Load all chat_ids from Supabase."""
    try:
        result = supabase.table('listo_users').select('chat_id').execute()
        return [row['chat_id'] for row in result.data]
    except Exception as e:
        logger.error(f"Supabase load_all_users error: {e}")
        return []


def get_user(chat_id: int) -> dict | None:
    """Fetch full user record."""
    try:
        result = supabase.table('listo_users').select('*').eq('chat_id', chat_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Supabase get_user error: {e}")
        return None


def get_users_with_exhausted_limit() -> list[dict]:
    """Fetch users whose daily limit was exhausted today (for reset notification)."""
    try:
        today = get_ist_today()
        result = supabase.table('listo_users').select('chat_id, daily_count, bonus_uses').eq('last_used_date', today).execute()
        exhausted = []
        for row in result.data:
            total_limit = DAILY_FREE_LIMIT + row.get('bonus_uses', 0)
            if row.get('daily_count', 0) >= total_limit:
                exhausted.append(row)
        return exhausted
    except Exception as e:
        logger.error(f"Supabase get_users_with_exhausted_limit error: {e}")
        return []


def check_and_use_limit(chat_id: int) -> dict:
    """
    User ka limit check karo aur use karo.
    Returns:
      {'allowed': True, 'remaining': N}  — use allowed
      {'allowed': False, 'reset_in': seconds, 'countdown': 'HH:MM:SS'}  — limit khatam
    """
    # Admin — unlimited, no restrictions
    if chat_id in ADMIN_IDS:
        return {'allowed': True, 'remaining': 999}

    try:
        today = get_ist_today()
        user  = get_user(chat_id)

        if not user:
            save_user(chat_id)
            user = get_user(chat_id)

        daily_count  = user.get('daily_count', 0)
        last_date    = user.get('last_used_date')
        bonus_uses   = user.get('bonus_uses', 0)
        total_limit  = DAILY_FREE_LIMIT + bonus_uses

        # New day — reset count
        if last_date != today:
            daily_count = 0
            bonus_uses  = 0
            total_limit = DAILY_FREE_LIMIT
            supabase.table('listo_users').update({
                'daily_count': 0,
                'bonus_uses': 0,
                'last_used_date': today,
            }).eq('chat_id', chat_id).execute()

        if daily_count >= total_limit:
            secs = seconds_until_midnight_ist()
            return {
                'allowed': False,
                'reset_in': secs,
                'countdown': format_countdown(secs),
            }

        # Increment use count
        new_count = daily_count + 1
        supabase.table('listo_users').update({
            'daily_count': new_count,
            'last_used_date': today,
        }).eq('chat_id', chat_id).execute()

        return {
            'allowed': True,
            'remaining': total_limit - new_count,
        }

    except Exception as e:
        logger.error(f"check_and_use_limit error: {e}")
        # On error, fail open (allow usage)
        return {'allowed': True, 'remaining': 0}


def apply_referral_bonus(referrer_chat_id: int) -> None:
    """Give referrer +5 bonus uses for today."""
    try:
        today = get_ist_today()
        user  = get_user(referrer_chat_id)
        if not user:
            return

        # If not used today, reset first
        last_date = user.get('last_used_date')
        if last_date != today:
            supabase.table('listo_users').update({
                'daily_count': 0,
                'bonus_uses': REFERRAL_BONUS,
                'last_used_date': today,
            }).eq('chat_id', referrer_chat_id).execute()
        else:
            current_bonus = user.get('bonus_uses', 0)
            supabase.table('listo_users').update({
                'bonus_uses': current_bonus + REFERRAL_BONUS,
            }).eq('chat_id', referrer_chat_id).execute()

        # Increment referral count
        ref_count = user.get('referral_count', 0)
        supabase.table('listo_users').update({
            'referral_count': ref_count + 1,
        }).eq('chat_id', referrer_chat_id).execute()

        logger.info(f"Referral bonus applied to {referrer_chat_id} (+{REFERRAL_BONUS} uses)")
    except Exception as e:
        logger.error(f"apply_referral_bonus error: {e}")


def is_maintenance() -> bool:
    return BOT_MODE == 'stop'

def is_broadcast_enabled() -> bool:
    """Sirf BOT_MODE=start pe broadcast hoga. pause/stop pe nahi."""
    return BOT_MODE == 'start'


# ── Limit exceeded message ────────────────────────────────────

def limit_exceeded_message(chat_id: int, countdown: str) -> str:
    return (
        f"⛔ You've reached today's limit!\n\n"
        f"🔄 Reset hoga: <b>{countdown}</b> mein (raat 12 baje IST)\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💡 Want more uses? Refer your friends!\n"
        f"Get <b>+{REFERRAL_BONUS} bonus uses</b> for each referral today.\n\n"
        f"🔗 Your referral link:\n"
        f"<code>https://t.me/{BOT_USERNAME}?start=ref_{chat_id}</code>"
    )


# ── Broadcast ─────────────────────────────────────────────────

async def broadcast_active(app: Application) -> None:
    """Send 'bot active' message to all saved users."""
    users = load_all_users()
    if not users:
        logger.info("No users in Supabase — broadcast skip")
        return

    logger.info(f"Broadcasting to {len(users)} users...")
    success, failed = 0, 0

    for chat_id in users:
        try:
            await app.bot.send_message(chat_id=chat_id, text=ACTIVE_MESSAGE)
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Broadcast failed for {chat_id}: {e}")
            failed += 1

    logger.info(f"Broadcast done — success: {success} | failed: {failed}")


async def broadcast_reset_notification(app: Application) -> None:
    """Send midnight reset notification to users whose limit was exhausted."""
    exhausted_users = get_users_with_exhausted_limit()
    if not exhausted_users:
        logger.info("No exhausted users — reset broadcast skip")
        return

    logger.info(f"Reset broadcast to {len(exhausted_users)} users...")
    success, failed = 0, 0

    for user in exhausted_users:
        chat_id = user['chat_id']
        msg = (
            f"🌅 New day, fresh start!\n\n"
            f"✅ Your daily limit has been reset.\n"
            f"You now have <b>{DAILY_FREE_LIMIT} free uses</b> available.\n\n"
            f"🎮 Send a screenshot and create your listing!\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 Want more uses? Refer your friends!\n"
            f"Get <b>+{REFERRAL_BONUS} bonus uses</b> per referral.\n\n"
            f"🔗 Your referral link:\n"
            f"<code>https://t.me/{BOT_USERNAME}?start=ref_{chat_id}</code>"
        )
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Reset broadcast failed for {chat_id}: {e}")
            failed += 1

    logger.info(f"Reset broadcast done — success: {success} | failed: {failed}")


async def schedule_midnight_reset(app: Application) -> None:
    """IST midnight ka wait karo, phir reset notification bhejo, loop karo."""
    while True:
        secs = seconds_until_midnight_ist()
        logger.info(f"Next reset broadcast in {format_countdown(secs)}")
        await asyncio.sleep(secs + 2)  # 2 sec buffer
        await broadcast_reset_notification(app)


# ── Handlers ──────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args    = context.args  # /start ref_XXXXXXX

    referred_by = None
    if args and args[0].startswith('ref_'):
        try:
            referrer_id = int(args[0].split('_')[1])
            if referrer_id != chat_id:
                referred_by = referrer_id
        except (ValueError, IndexError):
            pass

    is_new = save_user(chat_id, referred_by=referred_by)

    # Referrer ko bonus do agar naya user hai aur referral valid hai
    if is_new and referred_by:
        apply_referral_bonus(referred_by)
        logger.info(f"User {chat_id} joined via referral from {referred_by}")
        try:
            await app_ref.bot.send_message(
                chat_id=referred_by,
                text=(
                    f"🎉 Someone joined using your referral link!\n\n"
                    f"✅ You've received <b>+{REFERRAL_BONUS} bonus uses</b> for today.\n\n"
                    f"Keep referring, keep earning! 🚀"
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Referral notification failed: {e}")

    if is_maintenance():
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return

    welcome_message = (
        "🎮 Welcome to LISTO Bot\n\n"
        "Send your BGMI account screenshots — AI will analyze them and create a ready-to-post listing.\n\n"
        "How to use:\n"
        "1. Take a screenshot of your BGMI account\n"
        "2. AI will extract your stats\n"
        "3. Get a formatted listing instantly\n\n"
        "You can send multiple screenshots at once — they'll all be combined into one listing.\n\n"
        f"📊 You get <b>{DAILY_FREE_LIMIT} free uses</b> every day.\n"
        f"💡 Refer friends and get <b>+{REFERRAL_BONUS} bonus uses</b> — use /refer command.\n\n"
        "Send a screenshot to get started!"
    )
    await update.message.reply_text(welcome_message, parse_mode='HTML')
    logger.info(f"User {chat_id} started LISTO bot")


async def refer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's referral link."""
    chat_id = update.effective_chat.id
    save_user(chat_id)

    user = get_user(chat_id)
    ref_count = user.get('referral_count', 0) if user else 0

    msg = (
        f"🔗 Your Referral Link:\n\n"
        f"<code>https://t.me/{BOT_USERNAME}?start=ref_{chat_id}</code>\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 How it works:\n"
        f"• Share your link with friends\n"
        f"• When someone joins using your link\n"
        f"• You get <b>+{REFERRAL_BONUS} bonus uses</b> for that day\n\n"
        f"👥 Total referrals so far: <b>{ref_count}</b>\n\n"
        f"More referrals = more uses! 🚀"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show today's remaining uses."""
    chat_id = update.effective_chat.id
    save_user(chat_id)

    if is_maintenance():
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return

    today = get_ist_today()
    user  = get_user(chat_id)

    if not user:
        await update.message.reply_text("❌ An error occurred. Please try again.")
        return

    daily_count = user.get('daily_count', 0)
    last_date   = user.get('last_used_date')
    bonus_uses  = user.get('bonus_uses', 0)

    if last_date != today:
        daily_count = 0
        bonus_uses  = 0

    total_limit = DAILY_FREE_LIMIT + bonus_uses
    used        = daily_count
    remaining   = max(0, total_limit - used)
    secs        = seconds_until_midnight_ist()

    filled = min(used, total_limit)
    empty  = total_limit - filled
    bar    = "🟩" * filled + "⬜" * empty

    if remaining > 0:
        status = f"✅ <b>{remaining}</b> use(s) remaining today"
    else:
        status = f"⛔ Limit reached! Resets in: <b>{format_countdown(secs)}</b>"

    msg = (
        "📊 Today's Usage\n\n"
        + bar + "\n"
        + f"Used: <b>{used}/{total_limit}</b>\n\n"
        + status + "\n\n"
        + "━━━━━━━━━━━━━━━\n"
        + "🔄 Daily reset: midnight IST\n"
        + f"💡 Want more uses? /refer → +{REFERRAL_BONUS} bonus"
    )
    await update.message.reply_text(msg, parse_mode='HTML')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot help guide."""
    chat_id = update.effective_chat.id
    save_user(chat_id)

    msg = (
        "📖 LISTO Bot — Help\n\n"
        "🎮 <b>What does it do?</b>\n"
        "Creates AI-powered listings from your BGMI account screenshots — ready to post!\n\n"
        "━━━━━━━━━━━━━━━\n"
        "⚡ <b>Commands</b>\n\n"
        "/start — Bot shuru karo\n"
        "/limit — Aaj kitne uses bache hain dekho\n"
        "/refer — Referral link lo, dosto ko share karo\n"
        "/help — Ye guide\n\n"
        "━━━━━━━━━━━━━━━\n"
        "📸 <b>How to use?</b>\n\n"
        "1. Take a screenshot of your BGMI account\n"
        "2. Send it to the bot (one or multiple)\n"
        "3. AI will analyze it\n"
        "4. Get your ready-to-post listing!\n\n"
        "━━━━━━━━━━━━━━━\n"
        "📊 <b>Daily Limit</b>\n\n"
        f"• <b>{DAILY_FREE_LIMIT} free uses</b> every day\n"
        "• Resets at midnight IST\n"
        f"• Refer friends → <b>+{REFERRAL_BONUS} bonus uses</b> that day\n\n"
        "━━━━━━━━━━━━━━━\n"
        "❓ Having issues? Try sending the screenshot again or come back later."
    )
    await update.message.reply_text(msg, parse_mode='HTML')



async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    save_user(chat_id)

    if is_maintenance():
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return

    # Limit check
    limit_result = check_and_use_limit(chat_id)
    if not limit_result['allowed']:
        await update.message.reply_text(
            limit_exceeded_message(chat_id, limit_result['countdown']),
            parse_mode='HTML'
        )
        return

    media_group_id = update.message.media_group_id
    file_id = update.message.photo[-1].file_id

    if media_group_id:
        logger.info(f"Batch photo received: {media_group_id}")
        if media_group_id not in pending_groups:
            pending_groups[media_group_id] = []
        pending_groups[media_group_id].append(file_id)

        if media_group_id not in scheduled_groups:
            scheduled_groups.add(media_group_id)
            asyncio.create_task(process_group_after_delay(media_group_id, chat_id, context, limit_result['remaining']))
    else:
        await process_single_photo(update, context, limit_result['remaining'])


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    save_user(chat_id)

    if is_maintenance():
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return

    await update.message.reply_text(
        "📸 Send your BGMI account screenshot — I'll create a listing for you!"
    )
    logger.info(f"User {chat_id} sent text")


# ── Photo processing ───────────────────────────────────────────

async def process_group_after_delay(
    media_group_id: str,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    remaining: int
) -> None:
    await asyncio.sleep(2)
    scheduled_groups.discard(media_group_id)
    file_ids = pending_groups.pop(media_group_id, [])

    if not file_ids:
        return

    total = len(file_ids)
    logger.info(f"Processing batch {media_group_id} — {total} photos")

    try:
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Analyzing {total} screenshot{'s' if total > 1 else ''}... please wait"
        )

        base64_images = []
        for i, file_id in enumerate(file_ids, 1):
            try:
                file_info   = await context.bot.get_file(file_id)
                photo_bytes = await file_info.download_as_bytearray()
                base64_images.append(base64.standard_b64encode(bytes(photo_bytes)).decode('utf-8'))
                logger.info(f"Downloaded {i}/{total}")
            except Exception as e:
                logger.error(f"Download error photo {i}: {e}")

        if not base64_images:
            await context.bot.send_message(chat_id=chat_id, text="❌ Could not download screenshots. Please try again.")
            return

        try:
            listing = await analyze_image_with_freemodel(base64_images)
        except httpx.HTTPStatusError as e:
            logger.error(f"FreeModel API error: {e.response.status_code} — {e.response.text}")
            listing = "❌ AI API error. Please try again later."
        except Exception as e:
            logger.error(f"Analysis error: {e}")
            listing = "❌ Could not analyze screenshot. Please try again."

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except TelegramError:
            pass

        await send_listing(context, chat_id, listing, remaining)

    except Exception as e:
        logger.error(f"Batch unexpected error: {e}")
        try:
            await context.bot.send_message(chat_id=chat_id, text="❌ An error occurred. Please try again.")
        except TelegramError:
            pass


async def process_single_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    remaining: int
) -> None:
    try:
        status_msg   = await update.message.reply_text("⏳ Analyzing screenshot... please wait")
        photo_file   = await update.message.photo[-1].get_file()
        photo_bytes  = await photo_file.download_as_bytearray()
        base64_image = base64.standard_b64encode(bytes(photo_bytes)).decode('utf-8')

        logger.info(f"Single photo: {len(photo_bytes)} bytes")

        listing = await analyze_image_with_freemodel([base64_image])

        try:
            await status_msg.delete()
        except TelegramError:
            pass

        # Listing + remaining uses footer
        footer = ""
        if remaining == 0:
            secs = seconds_until_midnight_ist()
            footer = (
                f"\n\n━━━━━━━━━━━━━━━\n"
                f"⚠️ That was your last use for today!\n"
                f"🔄 Resets in: <b>{format_countdown(secs)}</b>\n"
                f"💡 Refer friends → +{REFERRAL_BONUS} uses: /refer"
            )
        elif remaining <= 1:
            footer = f"\n\n⚠️ Sirf <b>{remaining}</b> use bacha aaj ke liye."

        await update.message.reply_text(listing + footer, parse_mode='HTML')

    except httpx.HTTPStatusError as e:
        logger.error(f"FreeModel API error: {e.response.status_code} — {e.response.text}")
        await update.message.reply_text("❌ AI API error. Please try again later.")
    except Exception as e:
        logger.error(f"Single photo error: {e}")
        await update.message.reply_text("❌ Could not analyze screenshot. Please try again.")


async def send_listing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    listing: str,
    remaining: int = 99
) -> None:
    """Listing bhejo — 4096 char se badi ho toh split karo."""
    footer = ""
    if remaining == 0:
        secs = seconds_until_midnight_ist()
        footer = (
            f"\n\n━━━━━━━━━━━━━━━\n"
            f"⚠️ That was your last use for today!\n"
            f"🔄 Resets in: <b>{format_countdown(secs)}</b>\n"
            f"💡 Refer friends → +{REFERRAL_BONUS} uses: /refer"
        )
    elif remaining <= 1:
        footer = f"\n\n⚠️ Sirf <b>{remaining}</b> use bacha aaj ke liye."

    full_listing = listing + footer
    max_len = 4096

    if len(full_listing) <= max_len:
        await context.bot.send_message(chat_id=chat_id, text=full_listing, parse_mode='HTML')
        return

    # Split karo
    sections, current = listing.split('\n\n'), ""
    parts = []
    for section in sections:
        if len(current) + len(section) + 2 <= max_len:
            current += section + '\n\n'
        else:
            if current:
                parts.append(current.strip())
            current = section + '\n\n'
    if current:
        parts.append(current.strip())

    for i, part in enumerate(parts):
        text = part + (footer if i == len(parts) - 1 else "")
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')


# ── FreeModel API ────────────────────────────────────────────

async def analyze_image_with_freemodel(base64_images: list) -> str:
    """Analyze images using FreeModel vision model."""
    url = "https://api.freemodel.dev/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {FREEMODEL_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/listo-bot",
        "X-Title": "LISTO Bot",
    }

    content = [{"type": "text", "text": SYSTEM_PROMPT}]
    for img in base64_images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img}"}
        })

    payload = {
        "model": FREEMODEL_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 4096,
        "temperature": 0.15,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data    = response.json()
        listing = data['choices'][0]['message']['content'].strip()
        logger.info(f"FreeModel response: {len(listing)} chars | model: {FREEMODEL_MODEL} | images: {len(base64_images)}")
        return listing


# ── Main ──────────────────────────────────────────────────────

app_ref = None  # Global app reference (referral notification ke liye)

def main() -> None:
    global app_ref

    logger.info("Starting LISTO bot")
    logger.info(f"Model  : {FREEMODEL_MODEL}")
    logger.info(f"Mode   : {BOT_MODE.upper()}")

    if is_maintenance():
        logger.info("MAINTENANCE MODE — bot offline")
    elif is_broadcast_enabled():
        logger.info("START MODE — broadcasting on startup")
    else:
        logger.info("PAUSE MODE — bot active, no broadcast")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app_ref = application

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("refer", refer_command))
    application.add_handler(CommandHandler("limit", limit_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def post_init(app: Application) -> None:
        # Sirf BOT_MODE=start pe hi broadcast hoga
        if is_broadcast_enabled():
            await broadcast_active(app)
        # Midnight reset scheduler — maintenance mode mein nahi chalega
        if not is_maintenance():
            asyncio.create_task(schedule_midnight_reset(app))

    application.post_init = post_init
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
