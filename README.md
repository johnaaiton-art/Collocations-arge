# English Arge Bot

A Telegram bot for English language learners (B2 level) with three main features:
1. **Definition mode** - Get learner-friendly definitions, similar words, and save collocations
2. **Picture mode** - Generate AI images from any description
3. **Etymology mode** - Learn word origins and Spanish translations

## Features

### 1️⃣ Definition Mode (`word def`)

**Example:** `setback def`

Returns:
- Clear B2-level definition (Oxford Learner's Dictionary style)
- 2 similar words (also B2 level)
- 5 collocation buttons with Russian translations
- Click any button to save to Google Sheets

### 2️⃣ Picture Mode (`word/phrase pic`)

**Examples:** 
- `setback pic`
- `a man suffering a setback in a race pic`

Returns:
- AI-generated image based on your description
- Works with any English text

### 3️⃣ Etymology Mode (`word etym`)

**Example:** `setback etym`

Returns:
- Concise root meanings (Latin, Greek, Old English)
- Spanish translation of the word

## Setup

### Prerequisites
- Python 3.8+
- Telegram Bot Token
- Yandex Cloud API credentials
- DeepSeek API Key  
- Google Cloud Service Account (for Sheets)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/johnaaiton-art/Collocations-arge.git
cd Collocations-arge
```

2. Create virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up environment variables (create `.env` file):
```bash
TELEGRAM_BOT_TOKEN=your-bot-token
DEEPSEEK_API_KEY=your-deepseek-key
YANDEX_API_KEY=your-yandex-key
YANDEX_FOLDER_ID=your-folder-id
```

5. Add Google credentials:
- Place `google-creds.json` in the project directory
- Make sure the service account has edit access to your spreadsheet

6. Run the bot:
```bash
python english_arge_bot.py
```

## Usage Examples

```
setback def
→ Definition + similar words + 5 collocation buttons

setback pic
→ Image of a setback

a tired runner setback pic
→ Image of a tired runner experiencing a setback

setback etym
→ Etymology: set (to place) + back (backward)
   Spanish: contratiempo
```

## Google Sheets Integration

Collocations are saved to the "Collocations arge" spreadsheet in the "English" sheet with:
- English collocation
- Russian translation
- Timestamp

## Deployment

### Running as a systemd service (Linux)

1. Create service file at `/etc/systemd/system/english-arge-bot.service`

2. Enable and start:
```bash
sudo systemctl enable english-arge-bot.service
sudo systemctl start english-arge-bot.service
```

## Troubleshooting

### Image generation timeout
Images take 1-3 minutes. The bot will notify if it's taking longer.

### Google Sheets not saving
- Check `google-creds.json` is present
- Verify service account has edit permissions
- Ensure spreadsheet URL is correct in code

### Bot not responding
Check logs:
```bash
sudo journalctl -u english-arge-bot.service -f
```

## License

MIT License

## Acknowledgments

- Yandex Cloud for image generation
- DeepSeek for language model
- Google Sheets for data storage
