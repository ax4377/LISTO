# BGMI Account Analyzer Bot

A Telegram bot that analyzes BGMI (Battlegrounds Mobile India) account screenshots using NVIDIA NIM API and generates formatted account listings automatically.

## Features

- ✅ Download BGMI account screenshots from Telegram
- ✅ Extract account stats using NVIDIA NIM Vision API
- ✅ Auto-format stats into professional listings
- ✅ Display inventory items (gun skins, outfits, vehicles)
- ✅ Async/polling-based (no webhook required)
- ✅ Fully configurable via environment variables
- ✅ Comprehensive error handling
- ✅ Production-ready logging

## Project Structure

```
bgmi-bot/
├── bot.py           # Main bot application
├── prompt.py        # AI system prompt for listing format
├── requirements.txt # Python dependencies
├── Procfile        # Railway deployment config
└── README.md       # This file
```

## File Descriptions

### bot.py
- Uses `python-telegram-bot==21.6` (async version with Application API)
- Handles `/start` command with welcome message
- Processes photo messages: downloads, converts to base64, queries NVIDIA NIM API
- Handles text messages with instructions
- Async HTTP requests via `httpx`
- Comprehensive logging and error handling

### prompt.py
- Contains `SYSTEM_PROMPT` variable
- Instructs AI to analyze BGMI screenshots and extract stats
- Specifies exact listing format with emojis and structure
- Handles N/A values and missing inventory

### requirements.txt
- `python-telegram-bot==21.6` - Async Telegram bot library
- `httpx==0.27.2` - Async HTTP client

### Procfile
- `worker: python bot.py` - Railway worker process (no web server)

## Getting API Keys

### 1. Telegram Bot Token
1. Open Telegram and search for **@BotFather**
2. Send command `/newbot`
3. Follow prompts to name your bot and choose username
4. BotFather will provide your `TELEGRAM_BOT_TOKEN`
5. Save this token (keep it secret!)

### 2. NVIDIA NIM API Key
1. Visit [NVIDIA NIM](https://build.nvidia.com/)
2. Sign up or log in with your NVIDIA account
3. Navigate to API keys section
4. Create a new API key for the Mistral Large model
5. Copy your `NVIDIA_API_KEY` (keep it secret!)
6. Note: NVIDIA NIM offers free tier with generous rate limits

## Environment Variables

Configure the bot by setting these environment variables on Railway:

| Variable          | Required | Default Value                                      | Description                    |
|-------------------|----------|----------------------------------------------------|--------------------------------|
| TELEGRAM_BOT_TOKEN| YES      | —                                                  | BotFather से token             |
| NVIDIA_API_KEY    | YES      | —                                                  | build.nvidia.com से API key    |
| NVIDIA_MODEL      | NO       | mistralai/mistral-large-3-675b-instruct-2512       | NVIDIA NIM model name          |

## Deployment on Railway

### Step 1: Prepare Your Repository

1. Create a new GitHub repository (or use existing)
2. Clone it locally:
   ```bash
   git clone https://github.com/yourusername/bgmi-bot.git
   cd bgmi-bot
   ```

3. Copy all 5 files (`bot.py`, `prompt.py`, `requirements.txt`, `Procfile`, `README.md`) into the repository

4. Create a `.gitignore` file:
   ```
   __pycache__/
   *.pyc
   .env
   venv/
   ```

5. Commit and push:
   ```bash
   git add .
   git commit -m "Initial BGMI bot setup"
   git push origin main
   ```

### Step 2: Deploy on Railway

1. Go to [railway.app](https://railway.app)
2. Sign up with GitHub
3. Click **New Project** → **Deploy from GitHub repo**
4. Select your `bgmi-bot` repository
5. Railway will auto-detect the service

### Step 3: Configure Environment Variables

1. In Railway project dashboard, go to **Variables**
2. Add these two environment variables:
   - `TELEGRAM_BOT_TOKEN` = your BotFather token
   - `NVIDIA_API_KEY` = your NVIDIA NIM API key

3. Click **Deploy** to trigger auto-deployment

### Step 4: Verify Deployment

1. In Railway dashboard, check the **Logs** tab
2. Should see: `"Starting BGMI Account Analyzer bot with NVIDIA NIM API"`
3. Open Telegram and search for your bot (by username from BotFather)
4. Send `/start` - bot should reply with welcome message
5. Send a BGMI screenshot - bot should analyze and reply with formatted listing

## Troubleshooting

### Bot not responding?
- Check Railway logs for errors
- Verify `TELEGRAM_BOT_TOKEN` is correct (no spaces)
- Ensure bot username matches BotFather username

### "NVIDIA NIM API error"?
- Check `NVIDIA_API_KEY` is valid (from NVIDIA NIM dashboard)
- Verify your NVIDIA account has access to Mistral Large model
- Check API response in Railway logs
- Ensure you're using the correct endpoint

### Photo not downloading?
- Ensure bot has permissions to download files
- Try a different BGMI screenshot file
- Check internet connection on Railway

### Listing format looks wrong?
- Edit `SYSTEM_PROMPT` in `prompt.py`
- Commit and push changes
- Railway will auto-redeploy

## Vision Model

The bot uses **Mistral Large 3** (mistralai/mistral-large-3-675b-instruct-2512) via NVIDIA NIM API by default.

### Model Features:
- **Provider**: NVIDIA NIM
- **Model**: Mistral Large 3 (675B parameters)
- **Capabilities**: Excellent image understanding, text extraction, object recognition
- **Pricing**: Free tier available with generous rate limits
- **Accuracy**: High accuracy for BGMI screenshot analysis

## 🤖 NVIDIA NIM — Recommended Vision Models

These models support image input and work well for BGMI screenshots:

| Model Name | Speed | Quality | Notes |
|---|---|---|---|
| mistralai/mistral-large-3-675b-instruct-2512 | Medium | High | **Default** — balanced quality & speed |
| microsoft/phi-3.5-vision-instruct | Very Fast | Medium | Lightweight, great for mobile |
| meta/llama-3.2-11b-vision-instruct | Fast | Medium | Good for screenshots, efficient |
| meta/llama-3.2-90b-vision-instruct | Slow | Very High | Best quality (slower) |
| nvidia/llama-3.1-nemotron-nano-vl-8b-v1 | Very Fast | Medium | Ultra lightweight, super fast |

### How to Change Model

To switch to a different vision model without editing code:

1. Open Railway project dashboard
2. Go to **Variables** tab
3. Add or update: `NVIDIA_MODEL=<model_name_from_table_above>`
4. Example: `NVIDIA_MODEL=meta/llama-3.2-11b-vision-instruct`
5. Railway will auto-redeploy — no code changes needed ✨
6. Check logs to verify: `Using NVIDIA model: meta/llama-3.2-11b-vision-instruct`

## Code Features Explained

### Async Architecture
- Uses `python-telegram-bot` Application API (fully async)
- `httpx.AsyncClient` for non-blocking API calls
- `run_polling(drop_pending_updates=True)` for clean restarts

### Error Handling
- Catches `httpx.HTTPStatusError` separately for API errors
- Catches general exceptions with user-friendly messages
- All errors logged with user ID and context

### Logging
- Python `logging` module configured
- Logs include timestamps, module name, and log level
- All actions logged: start, photo download, API calls, errors

### Security
- Never hardcodes API keys (uses environment variables)
- Validates required env vars on startup
- No user whitelist (open to all, can be restricted later)

## Customization

### Change Listing Format
Edit the format in `prompt.py` `SYSTEM_PROMPT` variable. The AI will follow the new format in its output.

### Add More Commands
Add handlers in `bot.py`:
```python
application.add_handler(CommandHandler("yourcommand", your_function))
```

### Add Database
Currently stateless. To add persistent storage (PostgreSQL, MongoDB), add connection in Railway Variables and update `bot.py` imports.

## Python Version

Requires Python 3.11 or higher. Check your Railway Python version in project settings (usually 3.12 by default).

## License

MIT License - Feel free to fork and modify!

## Support

For issues with:
- **Telegram Bot API**: [python-telegram-bot docs](https://python-telegram-bot.readthedocs.io/)
- **NVIDIA NIM API**: [NVIDIA NIM docs](https://build.nvidia.com/mistralai/mistral-large-3)
- **Railway Deployment**: [railway.app docs](https://docs.railway.app/)
