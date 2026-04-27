import os
import logging
import base64
import asyncio
import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

from prompt import SYSTEM_PROMPT

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NVIDIA_API_KEY = os.getenv('NVIDIA_API_KEY')
NVIDIA_MODEL = os.getenv('NVIDIA_MODEL', 'mistralai/mistral-large-3-675b-instruct-2512')

if not TELEGRAM_BOT_TOKEN or not NVIDIA_API_KEY:
    raise ValueError("Missing required environment variables: TELEGRAM_BOT_TOKEN and NVIDIA_API_KEY")


# Global dicts for media group batching
# Format: {media_group_id: [file_id, file_id, ...]}
pending_groups = {}
# Format: set of media_group_ids already scheduled for processing (one task per group)
scheduled_groups = set()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message explaining how to use the bot."""
    welcome_message = """🎮 **Welcome to Galaxy Accounts Bot**

I analyze BGMI (Battlegrounds Mobile India) account screenshots and create formatted listings automatically.

**How to use:**
1️⃣ Send me a screenshot of a BGMI account
2️⃣ I'll extract the stats using AI
3️⃣ You'll get a formatted listing ready to share

**Supported stats:**
• UID, Level, Tier & Rank Points
• K/D Ratio, Matches, Win Rate
• Inventory items (skins, outfits, vehicles)

Just send a screenshot and I'll do the rest! 📸"""
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown')
    logger.info(f"User {update.effective_user.id} started the bot")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages - batch multiple photos or process single photo."""
    media_group_id = update.message.media_group_id
    chat_id = update.effective_chat.id
    file_id = update.message.photo[-1].file_id
    
    if media_group_id:
        # This photo is part of a batch (multiple photos sent together)
        logger.info(f"Received photo with media_group_id: {media_group_id}")
        
        # Always add file_id to pending group
        if media_group_id not in pending_groups:
            pending_groups[media_group_id] = []
        pending_groups[media_group_id].append(file_id)
        
        # Only schedule ONE task per media_group_id (use scheduled_groups flag)
        if media_group_id not in scheduled_groups:
            scheduled_groups.add(media_group_id)
            logger.info(f"Scheduled processing for batch {media_group_id}")
            asyncio.create_task(process_group_after_delay(media_group_id, chat_id, context))
        else:
            logger.info(f"Batch {media_group_id} already scheduled, added photo #{len(pending_groups[media_group_id])}")
    else:
        # Single photo (no media group) - process immediately
        await process_single_photo(update, context)


async def process_group_after_delay(media_group_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process all photos in a media group batch after waiting for collection."""
    # Wait 2 seconds for all photos in the group to arrive
    await asyncio.sleep(2)
    
    # Mark as no longer scheduled
    scheduled_groups.discard(media_group_id)
    
    # Get all file_ids for this group
    file_ids = pending_groups.pop(media_group_id, [])
    
    if not file_ids:
        logger.warning(f"Media group {media_group_id} has no file_ids")
        return
    
    total = len(file_ids)
    logger.info(f"Processing batch {media_group_id} with {total} photos")
    
    try:
        # Send status message
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ {total} screenshot{'s' if total > 1 else ''} analyze ho rahe hain... wait karo"
        )
        
        # Download all photos and convert to base64 (send all together in one API call)
        base64_images = []
        for i, file_id in enumerate(file_ids, 1):
            try:
                # Download photo
                file_info = await context.bot.get_file(file_id)
                photo_bytes = await file_info.download_as_bytearray()
                base64_image = base64.standard_b64encode(bytes(photo_bytes)).decode('utf-8')
                base64_images.append(base64_image)
                logger.info(f"Downloaded photo {i}/{total} from batch {media_group_id}")
                
            except Exception as e:
                logger.error(f"Error downloading photo {i} in batch {media_group_id}: {str(e)}")
        
        if not base64_images:
            logger.error(f"Failed to download any photos from batch {media_group_id}")
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ **Error downloading screenshots**\nPlease try again"
            )
            return
        
        # Analyze ALL photos together in ONE API call
        try:
            listing = await analyze_image_with_nvidia(base64_images)
            logger.info(f"Analyzed batch {media_group_id} with {len(base64_images)} photos in one call")
        except httpx.HTTPStatusError as e:
            logger.error(f"NVIDIA NIM API error for batch {media_group_id}: {e.response.status_code}")
            listing = f"❌ **NVIDIA NIM API Error**\nPlease try again later."
        except Exception as e:
            logger.error(f"Error analyzing batch {media_group_id}: {str(e)}")
            listing = f"❌ **Error analyzing screenshots**\n{str(e)[:80]}"
        
        # Delete the status message
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except TelegramError:
            pass
        
        # Send single listing (no "Account X of N" headers)
        await context.bot.send_message(
            chat_id=chat_id,
            text=listing,
            parse_mode='HTML'
        )
        logger.info(f"Sent combined reply for batch {media_group_id} with {total} photos")
        
    except Exception as e:
        logger.error(f"Unexpected error processing batch {media_group_id}: {str(e)}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ **Unexpected Error**\n{str(e)[:100]}"
            )
        except TelegramError:
            logger.error(f"Failed to send error message for batch {media_group_id}")


async def send_combined_listings(context: ContextTypes.DEFAULT_TYPE, chat_id: int, listing: str) -> None:
    """Send listing to user (split if >4096 chars)."""
    # Telegram message limit is 4096 chars
    max_message_length = 4096
    
    if len(listing) <= max_message_length:
        # Single message fits
        await context.bot.send_message(
            chat_id=chat_id,
            text=listing,
            parse_mode='HTML'
        )
    else:
        # Split into multiple messages (break on double newlines to preserve structure)
        sections = listing.split('\n\n')
        messages = []
        current_message = ""
        
        for section in sections:
            if len(current_message) + len(section) + 2 <= max_message_length:
                current_message += section + '\n\n'
            else:
                if current_message:
                    messages.append(current_message.strip())
                current_message = section + '\n\n'
        
        if current_message:
            messages.append(current_message.strip())
        
        # Send all split messages
        for msg in messages:
            if msg:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode='HTML'
                )


async def process_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process a single photo (not part of a batch)."""
    try:
        # Send processing message
        processing_msg = await update.message.reply_text("🔄 Analyzing screenshot...")
        
        # Download and analyze photo
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        base64_image = base64.standard_b64encode(bytes(photo_bytes)).decode('utf-8')
        
        logger.info(f"Downloaded single photo from user {update.effective_user.id}, size: {len(photo_bytes)} bytes")
        
        # Get listing from NVIDIA NIM API (pass as list with single image)
        listing = await analyze_image_with_nvidia([base64_image])
        
        # Delete processing message
        try:
            await processing_msg.delete()
        except TelegramError:
            pass
        
        # Send the listing
        await update.message.reply_text(listing, parse_mode='HTML')
        logger.info(f"Sent listing to user {update.effective_user.id}")
        
    except httpx.HTTPStatusError as e:
        logger.error(f"NVIDIA NIM API error for user {update.effective_user.id}: {e.response.status_code}")
        try:
            await update.message.reply_text("❌ **NVIDIA NIM API Error**\nPlease try again later.")
        except TelegramError:
            pass
            
    except Exception as e:
        logger.error(f"Error processing single photo for user {update.effective_user.id}: {str(e)}")
        try:
            await update.message.reply_text(f"❌ **Error Processing Screenshot**\n{str(e)[:80]}")
        except TelegramError:
            pass



async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages - ask user to send a screenshot instead."""
    response = "📸 Please send a BGMI account screenshot instead of text. I'll analyze it and create a formatted listing for you!"
    await update.message.reply_text(response)
    logger.info(f"User {update.effective_user.id} sent text message")


async def analyze_image_with_nvidia(base64_images: list) -> str:
    """Send images to NVIDIA NIM API and get formatted listing.
    
    Args:
        base64_images: List of base64-encoded image strings to analyze together
    
    Returns:
        Formatted listing generated by analyzing all images as one account
    """
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # Build content array with system prompt and all images
    content = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT
        }
    ]
    
    # Add each image as separate image_url block
    for base64_image in base64_images:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{base64_image}"
            }
        })
    
    payload = {
        "model": NVIDIA_MODEL,
        "messages": [
            {
                "role": "user",
                "content": content
            }
        ],
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
        logger.info(f"Received response from NVIDIA NIM API: {len(listing)} characters for {len(base64_images)} images")
        return listing


def main() -> None:
    """Start the bot."""
    logger.info(f"Starting BGMI Account Analyzer bot with NVIDIA NIM API")
    logger.info(f"Using NVIDIA model: {NVIDIA_MODEL}")
    
    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Start the bot
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
