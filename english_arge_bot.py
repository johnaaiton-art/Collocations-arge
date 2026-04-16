import os
import uuid
import hashlib
import logging
import requests
import asyncio
import tempfile
import base64
import re
import json
import zipfile
from io import BytesIO
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import OpenAI
from google.cloud import texttospeech
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2 import service_account
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ------------------ CONFIG ------------------
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
YANDEX_API_KEY = os.getenv('YANDEX_API_KEY')
YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')

# Google credentials file
GOOGLE_CREDS_FILE = os.path.join(os.path.dirname(__file__), 'google-creds.json')

# ------------------ PER-STUDENT CONFIG ------------------
# Map Telegram user_id (int) → student info
# spreadsheet_url: their own Google Sheet where collocations are saved
# name: display name used in messages
#
# To add a new student:
#   1. Create a new Google Sheet for them and share it with your service account
#   2. Add an entry here with their Telegram user_id
#
STUDENT_CONFIG: Dict[int, Dict] = {
    435346955: {
        "name": "Tania",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1G65r-HU41GYOj6p1BF3DqGwzhUk5Mu6tS65cTWALiaI/edit",
        "sheet_name": "Sheet1",
    },
    # Add more students here...
}
# Fallback sheet for unknown users (keeps old behaviour)
DEFAULT_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1Ifaczs-IELEvyb94QI_gfeD5aM3z-K5liapaISwrGnw/edit?gid=0#gid=0"
DEFAULT_SHEET_NAME = "English"

# Chirp3-HD voices for /anki TTS (rotating)
CHIRP_VOICES = [
    "en-US-Chirp3-HD-Aoede",
    "en-US-Chirp3-HD-Leda",
    "en-US-Chirp3-HD-Puck",
    "en-US-Chirp3-HD-Fenrir",
]

if not TELEGRAM_BOT_TOKEN or not DEEPSEEK_API_KEY:
    raise EnvironmentError("Missing TELEGRAM_BOT_TOKEN or DEEPSEEK_API_KEY in environment variables.")

if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
    raise EnvironmentError("Missing YANDEX_API_KEY or YANDEX_FOLDER_ID in environment variables.")

TEMP_DIR = tempfile.mkdtemp()

# Cache for collocations: chat_id → list of (english, russian)
COLLOCATION_CACHE: Dict[int, List[Tuple[str, str]]] = {}

# Initialize DeepSeek client
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# Initialize Google Sheets client
def get_google_sheets_client():
    """Initialize Google Sheets API client"""
    try:
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        logging.error(f"Failed to initialize Google Sheets client: {e}")
        return None

# ------------------ INPUT PARSING ------------------
def parse_input(text: str) -> Tuple[str, str]:
    """
    Parse input to determine mode and extract the word/phrase.
    Returns: (mode, word/phrase)
    Modes: 'def', 'pic', 'etym', or None
    """
    text = text.strip()
    
    # Check for "word def", "word pic", "word etym"
    if text.lower().endswith(' def'):
        return ('def', text[:-4].strip())
    elif text.lower().endswith(' pic'):
        return ('pic', text[:-4].strip())
    elif text.lower().endswith(' etym'):
        return ('etym', text[:-5].strip())
    
    return (None, text)

# ------------------ DEFINITION MODE ------------------
async def generate_definition(word: str) -> Tuple[str, List[str]]:
    """
    Generate a learner-friendly definition and similar words.
    Returns: (definition, [similar_words])
    """
    system_prompt = """You are an English language teacher for upper-intermediate (B2) students.

When given a word or phrase, provide:
1. A clear, simple definition using only common words (B2 level or below)
2. 2 similar words or expressions (also B2 level or below)

Format your response EXACTLY like this:
DEFINITION: [simple definition here]
SIMILAR: word1, word2

Rules:
- Keep the definition concise (1-2 sentences max)
- Use only common, everyday English words in the definition
- Similar words must be B2 level or lower
- Do not use advanced vocabulary (C1/C2 words)
- Think like Oxford Learner's Dictionary

Example for "setback":
DEFINITION: A problem that delays or prevents progress, or makes a situation worse.
SIMILAR: obstacle, difficulty"""

    user_prompt = f"Word/phrase: {word}"

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3
        )
        
        result_text = response.choices[0].message.content.strip()
        logging.info(f"DeepSeek definition response: {result_text}")
        
        # Parse the response
        definition = ""
        similar_words = []
        
        for line in result_text.split('\n'):
            line = line.strip()
            if line.startswith('DEFINITION:'):
                definition = line.replace('DEFINITION:', '').strip()
            elif line.startswith('SIMILAR:'):
                similar_text = line.replace('SIMILAR:', '').strip()
                similar_words = [w.strip() for w in similar_text.split(',')]
        
        if not definition:
            definition = "No definition available."
        
        return (definition, similar_words)
        
    except Exception as e:
        logging.error(f"DeepSeek definition error: {e}")
        return (f"Definition for '{word}'", [])

async def generate_collocations(word: str) -> List[Tuple[str, str]]:
    """
    Generate 5 common collocations with Russian translations.
    Returns list of (english_collocation, russian_translation) tuples.
    """
    system_prompt = """You are an English collocation expert for Russian-speaking learners.

CRITICAL FORMAT REQUIREMENT:
Every line MUST use this EXACT format: English collocation|Russian translation
The pipe symbol | is MANDATORY between English and Russian.

RULES:
1. Each collocation must be 2-5 words including the target word
2. Provide natural, common collocations (the ones native speakers actually use)
3. Give EXACTLY 5 collocations
4. Russian translations must be natural and accurate
5. Output ONLY the list, no numbering, no explanations

CORRECT EXAMPLE for "setback":
suffer a setback|потерпеть неудачу
major setback|серьезная неудача
overcome a setback|преодолеть неудачу
temporary setback|временная неудача
experience a setback|испытать неудачу

WRONG (missing pipe or translation):
suffer a setback ❌
a big setback ❌"""

    user_prompt = f"Generate 5 common collocations for: {word}"

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2
        )
        
        result_text = response.choices[0].message.content.strip()
        logging.info(f"DeepSeek collocation response: {result_text}")
        
        # Parse the response
        collocations = []
        lines = result_text.split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Remove numbering
            line = re.sub(r'^\d+[\.\)]\s*', '', line)
            
            # MUST have pipe
            if '|' not in line:
                logging.warning(f"Skipping line without pipe: {line}")
                continue
            
            # Split by pipe
            parts = line.split('|', 1)
            
            if len(parts) == 2:
                english = parts[0].strip()
                russian = parts[1].strip()
                
                if english and russian:
                    collocations.append((english, russian))
        
        if collocations:
            return collocations[:5]
        else:
            logging.error(f"No valid collocations parsed from: {result_text}")
            return [(f"{word} usage", "использование")]
            
    except Exception as e:
        logging.error(f"DeepSeek collocation error: {e}")
        return [(f"{word} usage", "использование")]

# ------------------ ETYMOLOGY MODE ------------------
async def generate_etymology(word: str) -> Tuple[str, str]:
    """
    Generate etymology (root meanings) and Spanish translation.
    Returns: (etymology, spanish_translation)
    """
    system_prompt = """You are an etymology expert.

When given a word, provide:
1. A CONCISE etymology showing the root meanings of its parts
2. Spanish translation of the word

Format EXACTLY like this:
ETYMOLOGY: [root meanings only, e.g., "Latin: con- (with, together) + sentire (to feel)"]
SPANISH: [translation]

Rules:
- Focus ONLY on root meanings (Latin, Greek, Old English origins)
- DO NOT include historical usage, dates, or when it entered English
- Keep it very brief - just show the meaningful parts
- If no clear etymology, say "Modern English formation" or similar

Example for "setback":
ETYMOLOGY: set (to place) + back (backward) - literally "to place backward"
SPANISH: contratiempo

Example for "consensus":
ETYMOLOGY: Latin: con- (with, together) + sentire (to feel)
SPANISH: consenso"""

    user_prompt = f"Word: {word}"

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3
        )
        
        result_text = response.choices[0].message.content.strip()
        logging.info(f"DeepSeek etymology response: {result_text}")
        
        # Parse the response
        etymology = ""
        spanish = ""
        
        for line in result_text.split('\n'):
            line = line.strip()
            if line.startswith('ETYMOLOGY:'):
                etymology = line.replace('ETYMOLOGY:', '').strip()
            elif line.startswith('SPANISH:'):
                spanish = line.replace('SPANISH:', '').strip()
        
        if not etymology:
            etymology = f"Etymology for '{word}'"
        if not spanish:
            spanish = word
        
        return (etymology, spanish)
        
    except Exception as e:
        logging.error(f"DeepSeek etymology error: {e}")
        return (f"Etymology for '{word}'", word)

# ------------------ STUDENT HELPERS ------------------
def _telegram_label(user) -> str:
    """
    Build a clean sheet-tab label from a Telegram User object.
    Priority: @username > First name > user_id
    Strips leading '@' so the sheet title is just the bare name.
    """
    if user is None:
        return "unknown"
    if user.username:
        return user.username          # e.g. "ivan_petrov"
    if user.first_name:
        # Replace spaces/special chars that Google Sheets dislikes in tab names
        return re.sub(r'[^\w\-]', '_', user.first_name.strip())
    return str(user.id)


def get_or_create_worksheet(client, spreadsheet_url: str, tab_name: str):
    """
    Open the spreadsheet and return the worksheet named `tab_name`.
    If no such tab exists yet, create it with the standard header row.
    """
    spreadsheet = client.open_by_url(spreadsheet_url)
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=4)
        ws.append_row(["English", "Russian", "Timestamp"], value_input_option="USER_ENTERED")
        logging.info(f"[Sheets] Created new tab '{tab_name}' in {spreadsheet_url}")
        return ws


def get_student_info(user_id: int, telegram_user=None) -> Dict:
    """
    Return student config dict.

    • Known students (in STUDENT_CONFIG) → their own dedicated spreadsheet.
    • Unknown users → DEFAULT_SPREADSHEET_URL, but with a per-user tab named
      after their Telegram username / first name / user_id.  The tab is created
      automatically on first save, so no manual setup is needed.
    """
    if user_id in STUDENT_CONFIG:
        return STUDENT_CONFIG[user_id]

    # Auto-assign a tab on the default sheet
    tab_name = _telegram_label(telegram_user) if telegram_user else str(user_id)
    return {
        "name": tab_name,
        "spreadsheet_url": DEFAULT_SPREADSHEET_URL,
        "sheet_name": tab_name,
    }

# ------------------ GOOGLE SHEETS OPERATIONS ------------------
def save_collocation_to_sheet(english: str, russian: str, user_id: int, telegram_user=None) -> bool:
    """Save a collocation to the student's own Google Sheet tab with timestamp.

    For known students (STUDENT_CONFIG) this uses their dedicated spreadsheet.
    For unknown users this auto-creates a tab named after their Telegram handle
    on the default spreadsheet.
    """
    try:
        student = get_student_info(user_id, telegram_user)
        client = get_google_sheets_client()
        if not client:
            logging.error("Google Sheets client not initialized")
            return False

        worksheet = get_or_create_worksheet(client, student["spreadsheet_url"], student["sheet_name"])

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [english, russian, timestamp]
        worksheet.append_row(row, value_input_option="USER_ENTERED")
        logging.info(f"[{student['name']}] Saved to sheet tab '{student['sheet_name']}': {english} | {russian}")
        return True

    except Exception as e:
        logging.error(f"Failed to save to sheet: {e}")
        return False

# ------------------ ANKI TTS HELPERS ------------------
def get_tts_client():
    """Return a Google Cloud TTS client using service account file."""
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDS_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return texttospeech.TextToSpeechClient(credentials=credentials)

def generate_tts_chirp3_sync(text: str, voice_name: str) -> bytes:
    """Generate MP3 audio for a short phrase using Chirp3-HD."""
    client = get_tts_client()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name=voice_name
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    return response.audio_content

async def generate_tts_chirp3_async(text: str, voice_name: str) -> bytes:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, generate_tts_chirp3_sync, text, voice_name)

EXPORT_STATE_FILE = os.path.join(os.path.dirname(__file__), 'anki_export_state.json')

def load_export_state() -> Dict:
    """Load the per-student last-export timestamps from disk."""
    if os.path.exists(EXPORT_STATE_FILE):
        try:
            with open(EXPORT_STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"[ExportState] Could not read state file: {e}")
    return {}

def save_export_state(state: Dict):
    """Persist the per-student last-export timestamps to disk."""
    try:
        with open(EXPORT_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logging.error(f"[ExportState] Could not save state file: {e}")

def get_last_export(user_id: int) -> Optional[datetime]:
    """Return the datetime of the last successful /anki export for this student, or None."""
    state = load_export_state()
    ts = state.get(str(user_id))
    if ts:
        try:
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None

def mark_export_done(user_id: int):
    """Record now as the last successful export time for this student."""
    state = load_export_state()
    state[str(user_id)] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_export_state(state)

def fetch_student_collocations(user_id: int, telegram_user=None) -> Tuple[List[Tuple[str, str]], Optional[datetime]]:
    """
    Read rows from the student's sheet that are NEW since their last /anki export.
    Returns (list of (russian, english) tuples, last_export_datetime or None).
    Rows must have a timestamp in column 3 (added by save_collocation_to_sheet).
    """
    student = get_student_info(user_id, telegram_user)
    client = get_google_sheets_client()
    if not client:
        return [], None
    worksheet = get_or_create_worksheet(client, student["spreadsheet_url"], student["sheet_name"])
    rows = worksheet.get_all_values()

    last_export = get_last_export(user_id)
    result = []

    for row in rows:
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            continue
        english = row[0].strip()
        russian = row[1].strip()

        # If we have a last export time, filter by the row's timestamp (col 3)
        if last_export and len(row) >= 3 and row[2].strip():
            try:
                row_ts = datetime.strptime(row[2].strip(), "%Y-%m-%d %H:%M:%S")
                if row_ts <= last_export:
                    continue  # already exported
            except ValueError:
                pass  # no parseable timestamp → include it to be safe

        result.append((russian, english))

    return result, last_export

async def build_anki_package(user_id: int, telegram_user=None) -> Optional[Tuple[str, BytesIO, int, Optional[datetime]]]:
    """
    Fetch new collocations since last export, generate Chirp3-HD TTS,
    and return (zip_filename, zip_buffer, item_count, last_export_datetime).

    Tab file format:  Russian \\t English \\t [sound:filename.mp3]
    """
    items, last_export = fetch_student_collocations(user_id, telegram_user)
    if not items:
        return None

    student = get_student_info(user_id, telegram_user)

    # Rotate through Chirp voices
    voice_cycle = CHIRP_VOICES

    tts_tasks = []
    for idx, (russian, english) in enumerate(items):
        voice = voice_cycle[idx % len(voice_cycle)]
        tts_tasks.append(generate_tts_chirp3_async(english, voice))

    logging.info(f"[Anki] Generating TTS for {len(items)} items (student: {student['name']})")
    audio_results = await asyncio.gather(*tts_tasks, return_exceptions=True)

    tab_lines = []
    audio_files: Dict[str, bytes] = {}

    for (russian, english), audio_data in zip(items, audio_results):
        md5 = hashlib.md5(english.encode()).hexdigest()
        audio_filename = f"tts_{md5}.mp3"
        if isinstance(audio_data, Exception) or not audio_data:
            logging.warning(f"[Anki] TTS failed for '{english}': {audio_data}")
            tab_lines.append(f"{russian}\t{english}")
        else:
            audio_files[audio_filename] = audio_data
            tab_lines.append(f"{russian}\t{english}\t[sound:{audio_filename}]")

    tab_content = "\n".join(tab_lines)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r'[^\w]', '_', student['name'])
    txt_filename = f"{safe_name}_{timestamp}_anki.txt"

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(txt_filename, tab_content.encode('utf-8'))
        for fname, data in audio_files.items():
            zf.writestr(fname, data)
    zip_buffer.seek(0)

    zip_filename = f"{safe_name}_{timestamp}_anki.zip"
    logging.info(f"[Anki] Package ready: {zip_filename} ({len(items)} cards, {len(audio_files)} audio files)")

    # Mark export done so next /anki only fetches new words
    mark_export_done(user_id)

    return zip_filename, zip_buffer, len(items), last_export

# ------------------ YANDEX IMAGE GENERATION ------------------
async def generate_image_with_yandex(prompt: str, update: Update) -> Optional[str]:
    """Generate image using Yandex Art API"""
    try:
        url = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
        headers = {
            "Authorization": f"Api-Key {YANDEX_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "modelUri": f"art://{YANDEX_FOLDER_ID}/yandex-art/latest",
            "generationOptions": {
                "seed": 42,
                "aspectRatio": {
                    "widthRatio": 1,
                    "heightRatio": 1
                }
            },
            "messages": [
                {"text": prompt}
            ]
        }
        
        logging.info(f"Sending Yandex image request for prompt: {prompt}")
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        
        if resp.status_code != 200:
            logging.error(f"Yandex API error: {resp.status_code} - {resp.text}")
            return None
        
        data = resp.json()
        operation_id = data["id"]
        logging.info(f"Yandex operation ID: {operation_id}")
        
        # Poll for result
        result_url = f"https://llm.api.cloud.yandex.net:443/operations/{operation_id}"
        
        max_wait = 180
        poll_interval = 5
        elapsed = 0
        notification_sent = False
        
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            
            try:
                result_resp = requests.get(result_url, headers=headers, timeout=30)
            except Exception as e:
                logging.error(f"Yandex status check exception: {e}")
                continue
            
            if result_resp.status_code != 200:
                logging.error(f"Yandex status check error: {result_resp.status_code}")
                continue
            
            result_data = result_resp.json()
            
            if elapsed >= 30 and not notification_sent and not result_data.get("done"):
                try:
                    await update.message.reply_text("⏳ Image generation in progress, please wait...")
                    notification_sent = True
                except:
                    pass
            
            if result_data.get("done"):
                if "error" in result_data:
                    error_msg = result_data["error"].get("message", "Unknown error")
                    logging.error(f"Yandex generation failed: {error_msg}")
                    return None
                
                if "response" in result_data and "image" in result_data["response"]:
                    image_b64 = result_data["response"]["image"]
                    logging.info("Yandex image generation successful")
                    
                    img_name = f"{uuid.uuid4().hex[:8]}.png"
                    img_path = os.path.join(TEMP_DIR, img_name)
                    
                    image_bytes = base64.b64decode(image_b64)
                    with open(img_path, 'wb') as f:
                        f.write(image_bytes)
                    
                    return img_path
                else:
                    logging.error("Yandex response missing image data")
                    return None
        
        logging.error(f"Yandex generation timed out after {max_wait} seconds")
        await update.message.reply_text("⏱️ Generation timed out. Server might be busy, please try again.")
        return None
        
    except Exception as e:
        logging.error(f"Yandex image generation exception: {e}")
        return None

# ------------------ TELEGRAM HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🇬🇧 **English Arge Bot**\n\n'
        '**Three modes:**\n\n'
        '1️⃣ **Definition mode**\n'
        '   Format: `word def`\n'
        '   Example: `setback def`\n'
        '   Returns: Definition + similar words + collocation buttons\n\n'
        '2️⃣ **Picture mode**\n'
        '   Format: `word/phrase pic`\n'
        '   Example: `setback pic` or `a man in a race pic`\n'
        '   Returns: Generated image\n\n'
        '3️⃣ **Etymology mode**\n'
        '   Format: `word etym`\n'
        '   Example: `setback etym`\n'
        '   Returns: Root meanings + Spanish translation\n\n'
        '⚠️ Image generation takes 1-3 minutes',
        parse_mode='Markdown'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages"""
    user_input = update.message.text.strip()
    
    if not user_input or user_input.startswith('/'):
        return
    
    # Parse input
    mode, content = parse_input(user_input)
    
    if mode == 'def':
        # MODE 1: Definition + Similar Words + Collocations
        await update.message.reply_text(f'📖 Looking up "{content}"...')
        
        # Generate definition and similar words
        definition, similar_words = await generate_definition(content)
        
        # Format response
        response = f"**{content}**\n\n"
        response += f"📝 **Definition:**\n{definition}\n\n"
        if similar_words:
            response += f"🔄 **Similar words:**\n{', '.join(similar_words)}\n\n"
        response += "👇 Click a collocation to save it:"
        
        await update.message.reply_text(response, parse_mode='Markdown')
        
        # Generate collocations
        collocations = await generate_collocations(content)
        
        if not collocations:
            await update.message.reply_text("❌ Could not generate collocations.")
            return
        
        # Store in cache
        chat_id = update.message.chat_id
        COLLOCATION_CACHE[chat_id] = collocations
        
        # Create buttons
        keyboard = []
        for idx, (english, russian) in enumerate(collocations):
            button_text = f"{english} | {russian}"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."
            callback_data = f"save:{idx}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ {len(collocations)} collocations found:",
            reply_markup=reply_markup
        )
        
    elif mode == 'pic':
        # MODE 2: Picture Generation
        await update.message.reply_text(f'🎨 Generating image for: "{content}"...')
        
        img_path = await generate_image_with_yandex(content, update)
        if not img_path:
            await update.message.reply_text("❌ Image generation failed. Please try again.")
            return
        
        try:
            with open(img_path, 'rb') as photo:
                await update.message.reply_photo(photo=photo)
            os.remove(img_path)
            logging.info(f"Sent image for: {content}")
        except Exception as e:
            logging.error(f"Send image exception: {e}")
            await update.message.reply_text(f"⚠️ Failed to send image: {str(e)}")
            if os.path.exists(img_path):
                os.remove(img_path)
    
    elif mode == 'etym':
        # MODE 3: Etymology + Spanish Translation
        await update.message.reply_text(f'📚 Looking up etymology for "{content}"...')
        
        etymology, spanish = await generate_etymology(content)
        
        response = f"**{content}**\n\n"
        response += f"🌱 **Etymology:**\n{etymology}\n\n"
        response += f"🇪🇸 **Spanish:**\n{spanish}"
        
        await update.message.reply_text(response, parse_mode='Markdown')
    
    else:
        # No mode specified
        await update.message.reply_text(
            "ℹ️ Please specify a mode:\n"
            "• `word def` - definition\n"
            "• `word pic` - picture\n"
            "• `word etym` - etymology",
            parse_mode='Markdown'
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks for collocation saving"""
    query = update.callback_query
    await query.answer()
    
    if not query.data.startswith("save:"):
        await query.edit_message_text("❌ Invalid button data")
        return
    
    try:
        idx = int(query.data.split(":")[1])
        
        chat_id = query.message.chat_id
        cached = COLLOCATION_CACHE.get(chat_id)
        
        if not cached or idx >= len(cached):
            await query.edit_message_text("❌ Data expired, please request collocations again")
            return
        
        english, russian = cached[idx]
        
    except (ValueError, IndexError, TypeError) as e:
        logging.error(f"Button callback error: {e}")
        await query.edit_message_text("❌ Data format error")
        return
    
    # Save to Google Sheets — pass user_id + full user object for auto-tab naming
    telegram_user = query.from_user
    user_id = telegram_user.id
    success = save_collocation_to_sheet(english, russian, user_id, telegram_user)
    
    if success:
        await query.edit_message_text(
            f"✅ Saved!\n\n**English:** {english}\n**Russian:** {russian}\n\n"
            f"Added to spreadsheet!",
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            f"❌ Save failed. Check Google Sheets configuration.\n\n{english} | {russian}"
        )

async def anki_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/anki — build an Anki zip of new collocations since last export."""
    telegram_user = update.effective_user
    user_id = telegram_user.id
    student = get_student_info(user_id, telegram_user)

    last_export = get_last_export(user_id)
    since_msg = (
        f"since your last export on {last_export.strftime('%d %b %Y %H:%M')}"
        if last_export else "from your full sheet (first export)"
    )

    await update.message.reply_text(
        f"⏳ Building Anki package for {student['name']} — fetching new words {since_msg}..."
    )

    try:
        result = await build_anki_package(user_id, telegram_user)
    except Exception as e:
        logging.error(f"[/anki] Error for user {user_id}: {e}")
        await update.message.reply_text(f"❌ Something went wrong: {e}")
        return

    if result is None:
        no_words_msg = (
            f"No new collocations since {last_export.strftime('%d %b %Y %H:%M')}."
            if last_export else "No collocations found in your sheet yet."
        )
        await update.message.reply_text(f"📭 {no_words_msg}")
        return

    zip_filename, zip_buffer, count, _ = result

    await update.message.reply_document(
        document=zip_buffer,
        filename=zip_filename,
        caption=(
            f"✅ Anki package ready for {student['name']}!\n\n"
            f"📦 {count} new card{'s' if count != 1 else ''} {since_msg}\n"
            f"🎵 Chirp3-HD audio included\n\n"
            f"Import the .txt file into Anki and drop the .mp3 files into your media folder."
        )
    )

# ------------------ MAIN ------------------
def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    if not os.path.exists(GOOGLE_CREDS_FILE):
        logging.warning(f"Google credentials file not found: {GOOGLE_CREDS_FILE}")
        logging.warning("Collocation saving will not work until you add google-creds.json")
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("anki", anki_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("✅ English Arge Bot is running!")
    print("   • word def → definition + collocations")
    print("   • word pic → image generation")
    print("   • word etym → etymology + Spanish")
    print("   • /anki    → Anki zip of new words since last export")
    print("⚠️ Note: Image generation may take 1-3 minutes")
    app.run_polling()

if __name__ == '__main__':
    main()
