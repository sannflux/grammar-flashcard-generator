import streamlit as st
import pandas as pd
import sqlite3
import re
import requests
import urllib.parse
import math
import json
import time
import io
import base64
import os
import html
import tempfile
from datetime import datetime, timedelta
import altair as alt

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional
from PIL import Image

from gtts import gTTS
from tenacity import retry, stop_after_attempt, wait_exponential

# ====================== 1. SAFE IMPORTS & CONFIGURATION ======================
st.set_page_config(
    page_title="Flashcard Library Pro v7.0",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded"
)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    YOUTUBE_AVAILABLE = True
except ImportError:
    YOUTUBE_AVAILABLE = False

try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    import genanki
    GENANKI_AVAILABLE = True
except ImportError:
    GENANKI_AVAILABLE = False

# ====================== 2. SESSION STATE ======================
DEFAULT_STATE = {
    "current_deck_id": None,
    "study_queue": [],
    "study_index": 0,
    "show_answer": False,
    "session_stats": {"reviewed": 0, "correct": 0, "start_time": None},
    "cram_mode": False,
    # --- NEW: API Rate Limiting (#1, #5) ---
    "api_calls_today": 0,
    "api_call_date": None,
    "rpm_timestamps": [],
    # --- NEW: API Key Persistence (#19) ---
    "api_key": "",
    # --- NEW: Card Preview Buffer (#14) ---
    "pending_cards": [],
    "pending_deck_name": "",
    # --- NEW: Generation History (#22) ---
    "gen_history": [],
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ====================== 3. RATE LIMITING ENGINE (#1, #2, #4, #5) ======================
MAX_RPM = 5
MAX_RPD = 20

def _refresh_daily_counter():
    """Reset RPD counter on a new calendar day (#5)."""
    today = datetime.now().date()
    if st.session_state["api_call_date"] != today:
        st.session_state["api_call_date"] = today
        st.session_state["api_calls_today"] = 0

def _clean_rpm_window():
    """Evict timestamps older than 60 seconds from the RPM window."""
    cutoff = datetime.now() - timedelta(seconds=60)
    st.session_state["rpm_timestamps"] = [
        t for t in st.session_state["rpm_timestamps"] if t > cutoff
    ]

def check_rate_limits():
    """
    Hard gate on API calls.
    Returns (ok: bool, message: str, wait_secs: int).
    """
    _refresh_daily_counter()
    _clean_rpm_window()
    rpd = st.session_state["api_calls_today"]
    rpm = len(st.session_state["rpm_timestamps"])

    if rpd >= MAX_RPD:
        return False, f"🚫 Daily limit reached ({MAX_RPD}/day). Resets at midnight.", 0
    if rpm >= MAX_RPM:
        oldest = st.session_state["rpm_timestamps"][0]
        wait = max(1, 61 - int((datetime.now() - oldest).total_seconds()))
        return False, f"⏳ RPM limit hit ({MAX_RPM}/min). Wait ~{wait}s.", wait
    return True, "", 0

def record_api_call():
    """Log a completed API call against both RPM and RPD counters."""
    st.session_state["rpm_timestamps"].append(datetime.now())
    st.session_state["api_calls_today"] += 1

def get_quota_status():
    """Return (rpm_used, rpd_used) for display."""
    _refresh_daily_counter()
    _clean_rpm_window()
    return len(st.session_state["rpm_timestamps"]), st.session_state["api_calls_today"]

# ====================== 4. DATABASE ENGINE ======================
DB_NAME = "flashcards_v5.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute('''CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
            description TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, deck_id INTEGER,
            front TEXT, back TEXT, explanation TEXT, tag TEXT,
            ease_factor REAL DEFAULT 2.5, interval INTEGER DEFAULT 0,
            repetitions INTEGER DEFAULT 0, next_review TEXT DEFAULT CURRENT_DATE,
            last_reviewed TEXT,
            FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE)''')
        try:
            c.execute("SELECT explanation FROM cards LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE cards ADD COLUMN explanation TEXT DEFAULT ''")
        try:
            c.execute("SELECT last_reviewed FROM cards LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE cards ADD COLUMN last_reviewed TEXT")
        conn.commit()

init_db()

# ====================== 5. CORE LOGIC ======================
def update_card_sm2(card_id, quality):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT ease_factor, interval, repetitions FROM cards WHERE id=?", (card_id,))
        row = c.fetchone()
        if row:
            ease, interval, reps = row['ease_factor'], row['interval'], row['repetitions']
            if quality < 3:
                reps, interval = 0, 1
            else:
                interval = 1 if reps == 0 else (6 if reps == 1 else math.ceil(interval * ease))
                reps += 1
                ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
            next_review = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")
            last_reviewed = datetime.now().strftime("%Y-%m-%d")
            c.execute(
                '''UPDATE cards SET ease_factor=?, interval=?, repetitions=?,
                   next_review=?, last_reviewed=? WHERE id=?''',
                (ease, interval, reps, next_review, last_reviewed, card_id)
            )
            conn.commit()

def delete_deck(deck_name):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM decks WHERE name=?", (deck_name,))
        conn.commit()

def rename_deck(old_name, new_name):
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE decks SET name=? WHERE name=?", (new_name, old_name))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def get_due_cards_count():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM cards WHERE next_review <= ?", (today,)
        ).fetchone()[0]

# ====================== 6. ANKI EXPORT ENGINE (#6, #7, #8) ======================
ANKI_MODEL_ID = 1607392319  # STABLE — never change

def sanitize_for_anki(text):
    """
    Strip all HTML tags except the safe allowlist <b><i><u><br> (#8).
    Protects Anki fields from broken markup without destroying formatting.
    """
    if not text:
        return ""
    return re.sub(
        r'<(?!/?(?:b|i|u|br)\b)[^>]*>',
        '',
        str(text),
        flags=re.IGNORECASE
    )

def export_to_apkg(deck_name: str, cards_df: pd.DataFrame) -> bytes:
    """
    Build a fully styled .apkg file from a DataFrame (#6).
    Columns required: front, back, explanation, tag.
    Model ID is permanently fixed to ANKI_MODEL_ID.
    """
    if not GENANKI_AVAILABLE:
        raise ImportError("genanki is not installed. Run: pip install genanki")

    # Catppuccin-dark themed Anki card template (#7)
    anki_model = genanki.Model(
        ANKI_MODEL_ID,
        'Flashcard Pro Model',
        fields=[
            {'name': 'Front'},
            {'name': 'Back'},
            {'name': 'Explanation'},
            {'name': 'Tag'},
        ],
        templates=[{
            'name': 'Card 1',
            'qfmt': (
                '<div class="card-tag">{{Tag}}</div>'
                '<div class="card-front">{{Front}}</div>'
            ),
            'afmt': (
                '{{FrontSide}}'
                '<hr id="answer">'
                '<div class="card-back">{{Back}}</div>'
                '{{#Explanation}}'
                '<div class="card-explanation">💡 {{Explanation}}</div>'
                '{{/Explanation}}'
            ),
        }],
        css='''
.card {
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 18px;
    text-align: center;
    background: #1e1e2e;
    color: #cdd6f4;
    padding: 24px 32px;
    border-radius: 14px;
    min-height: 200px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
}
.card-front {
    font-size: 22px;
    font-weight: 700;
    margin: 14px 0 8px;
    color: #cdd6f4;
    line-height: 1.4;
}
.card-back {
    font-size: 17px;
    color: #89b4fa;
    line-height: 1.7;
    margin-top: 10px;
}
.card-tag {
    background: #89b4fa;
    color: #1e1e2e;
    padding: 3px 14px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    display: inline-block;
    letter-spacing: 0.05em;
}
.card-explanation {
    font-size: 13px;
    color: #a6adc8;
    font-style: italic;
    border-top: 1px solid #45475a;
    padding-top: 12px;
    margin-top: 14px;
    width: 100%;
    text-align: center;
}
hr#answer {
    border: none;
    border-top: 1px solid #45475a;
    margin: 16px 0;
    width: 100%;
}
b { color: #f38ba8; }
i { color: #a6e3a1; }
'''
    )

    deck_id = abs(hash(deck_name)) % (10 ** 10)
    anki_deck = genanki.Deck(deck_id, deck_name)

    for _, row in cards_df.iterrows():
        note = genanki.Note(
            model=anki_model,
            fields=[
                sanitize_for_anki(row.get('front', '')),
                sanitize_for_anki(row.get('back', '')),
                sanitize_for_anki(row.get('explanation', '') or ''),
                str(row.get('tag', '')),
            ],
            tags=[re.sub(r'\s+', '_', str(row.get('tag', '')))],
        )
        anki_deck.add_note(note)

    package = genanki.Package(anki_deck)
    with tempfile.NamedTemporaryFile(suffix='.apkg', delete=False) as f:
        tmp_path = f.name
    try:
        package.write_to_file(tmp_path)
        with open(tmp_path, 'rb') as f:
            return f.read()
    finally:
        os.unlink(tmp_path)

# ====================== 7. AI CONTENT ENGINE & EXTRACTORS ======================
class Flashcard(BaseModel):
    front: str = Field(description="The question/concept. Plain text only.")
    back: str = Field(description="The answer. Use HTML <b> tags for ALL key terms.")
    explanation: str = Field(
        description="A unique analogy or mnemonic explaining WHY. Must NOT restate the answer."
    )
    tag: str = Field(description="A short category tag.")

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

def clean_text(text):
    if not text:
        return ""
    return re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text).strip()

def sanitize_json(text):
    text = re.sub(r'^```json', '', text, flags=re.MULTILINE)
    return re.sub(r'^```', '', text, flags=re.MULTILINE).strip()

def extract_pdf_text(uploaded_file):
    try:
        pdf_bytes = io.BytesIO(uploaded_file.getvalue())
        reader = PdfReader(pdf_bytes)
        text = "".join((page.extract_text() or "") + "\n" for page in reader.pages)
        return text[:25000], None
    except Exception as e:
        return None, f"Error reading PDF: {str(e)}"

def extract_youtube_id(url):
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.hostname == 'youtu.be':
        return parsed_url.path[1:]
    if parsed_url.hostname in ('www.youtube.com', 'youtube.com'):
        if parsed_url.path == '/watch':
            return urllib.parse.parse_qs(parsed_url.query).get('v', [None])[0]
        if parsed_url.path.startswith(('/shorts/', '/embed/', '/v/')):
            return parsed_url.path.split('/')[2]
    match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return match.group(1) if match else None

def get_native_youtube_transcript(video_id):
    """Multi-stage YouTube extractor: Native HTML → Proxy API."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookies = {"CONSENT": "YES+cb.20210328-17-p0.en+FX+478"}

    # Stage 1: Native HTML parse
    try:
        html_content = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers=headers, cookies=cookies, timeout=10
        ).text
        captions_match = re.search(r'"captionTracks"\s*:\s*(\[.*?\])', html_content)
        if captions_match:
            captions_json = json.loads(captions_match.group(1))
            xml_url = captions_json[0]['baseUrl']
            xml_resp = requests.get(xml_url, headers=headers, timeout=10)
            clean = re.sub(r'<[^>]+>', ' ', xml_resp.text)
            clean = html.unescape(clean)
            return re.sub(r'\s+', ' ', clean).strip()
    except Exception:
        pass

    # Stage 2: Proxy API fallback
    try:
        proxy_resp = requests.get(
            f"https://youtubetranscript.com/?server_vid2={video_id}", timeout=10
        )
        if '<transcript>' in proxy_resp.text or '<?xml' in proxy_resp.text:
            clean = re.sub(r'<[^>]+>', ' ', proxy_resp.text)
            clean = html.unescape(clean)
            return re.sub(r'\s+', ' ', clean).strip()
    except Exception:
        pass

    raise ValueError("All extraction methods failed. The video may not have closed captions.")

def clean_web_markdown(text):
    text = re.sub(r'<HTML.*?>.*?</HTML>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    lines = text.split('\n')
    clean_lines = [
        ln for ln in lines
        if "Access Denied" not in ln
        and "edgesuite.net" not in ln
        and "Reference #" not in ln
    ]
    return re.sub(r'\n\s*\n', '\n\n', '\n'.join(clean_lines)).strip()

def fetch_web_content(url):
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", timeout=15)
        resp.raise_for_status()
        text = clean_web_markdown(resp.text)
        if not text or (text.count("Access Denied") > 5 and len(text) < 1000):
            raise ValueError("WAF block detected")
        return text[:25000]
    except Exception:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        return " ".join(soup.stripped_strings)[:25000]

# --- SOURCE-AWARE PROMPTING (#11) ---
SOURCE_FRAMES = {
    "YouTube URL": (
        "spoken lecture transcript. "
        "Ignore filler words, timestamps, speaker annotations, and repetitive phrases."
    ),
    "Upload PDF": (
        "academic or professional text document. "
        "Focus on definitions, theorems, key arguments, and technical terms."
    ),
    "Web Article": (
        "web article. "
        "IGNORE: navigation menus, header/footer links, ads, cookie banners, sidebars, "
        "login prompts, subscription calls-to-action, and author bios. "
        "Focus EXCLUSIVELY on the article body content."
    ),
    "Image Analysis": (
        "visual diagram or image. "
        "Generate cards based on the concepts, labels, and relationships shown visually."
    ),
    "Text/Paste": "notes or text passage.",
}

# Token budget per API call (#12)
TOKEN_BUDGET = 6000

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def generate_flashcards(
    api_key, text_content, image_content, difficulty, count_val,
    source_type="Text/Paste", tag_taxonomy=None
):
    """
    Core generation function — model locked to gemini-2.5-flash-lite.
    Enriched with source-aware framing (#11), anti-noise injection (#10),
    tag taxonomy enforcement (#9), and quality explanation mandate (#13).
    """
    client = genai.Client(api_key=api_key)

    source_frame = SOURCE_FRAMES.get(source_type, "text passage.")

    # Tag taxonomy constraint (#9)
    tag_instruction = ""
    if tag_taxonomy and tag_taxonomy.strip():
        tag_list = ", ".join(
            f'"{t.strip()}"' for t in tag_taxonomy.split(",") if t.strip()
        )
        tag_instruction = (
            f" The 'tag' field MUST be exactly one of these values: {tag_list}. "
            f"Do not invent, abbreviate, or combine tags."
        )

    system_prompt = (
        f"Act as a professor creating flashcards for {difficulty} level students. "
        f"The source material is a {source_frame} "  # (#11)
        f"Create exactly {count_val} flashcards strictly from the core educational content. "
        # Anti-noise injection (#10)
        f"DO NOT generate cards about: navigation menus, login/signup prompts, "
        f"cookie consent notices, advertisements, comment sections, author bios, "
        f"subscription pop-ups, page footers, or any non-educational boilerplate. "
        # Formatting mandate
        f"The 'back' field MUST use <b>bold</b> HTML tags around ALL key terms and definitions. "
        # Explanation quality boost (#13)
        f"The 'explanation' field MUST provide a unique analogy, mnemonic, or memorable "
        f"comparison that helps the student remember the answer. "
        f"It MUST NOT simply restate or paraphrase the answer. "
        f"{tag_instruction} "
        f"Output valid JSON only — no preamble, no markdown fences."
    )

    contents = []
    if text_content:
        contents.append(text_content)
    if image_content:
        contents.append(image_content)

    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",  # LOCKED — do not change
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=FlashcardSet,
            temperature=0.3
        )
    )
    return json.loads(sanitize_json(response.text)).get("cards", [])


def generate_with_rate_limit(
    api_key, text_content, image_content, difficulty, count_val,
    source_type, tag_taxonomy, status_obj=None
):
    """
    Orchestration layer: enforces token budget (#12), smart batching (#3),
    inter-batch delays, and rate-limit auto-wait (#4).
    Records every successful call (#1).
    """
    # Token budget guard (#12)
    if text_content and len(text_content) > TOKEN_BUDGET:
        text_content = text_content[:TOKEN_BUDGET]

    # Build batch plan (#3)
    chunks, remaining = [], count_val
    while remaining > 0:
        chunks.append(min(10, remaining))
        remaining -= 10

    all_cards = []

    for i, chunk_size in enumerate(chunks):
        # 13s inter-batch delay (#3)
        if i > 0:
            if status_obj:
                status_obj.update(
                    label=f"⏳ Pacing API — waiting 13s before batch {i+1}/{len(chunks)}..."
                )
            time.sleep(13)

        # Rate-limit gate with auto-wait (#4)
        ok, msg, wait_secs = check_rate_limits()
        if not ok and wait_secs > 0:
            if status_obj:
                status_obj.update(label=f"⏳ {msg}")
            time.sleep(wait_secs + 1)
            ok, msg, _ = check_rate_limits()
        if not ok:
            raise Exception(msg)

        if status_obj:
            status_obj.update(
                label=f"🤖 Batch {i+1}/{len(chunks)}: requesting {chunk_size} cards from Gemini..."
            )

        cards = generate_flashcards(
            api_key, text_content, image_content,
            difficulty, chunk_size, source_type, tag_taxonomy
        )
        record_api_call()  # (#1)
        all_cards.extend(cards)

    return all_cards


def add_to_gen_history(deck_name: str, count: int):
    """Prepend an entry to the generation log, capped at 5 (#22)."""
    hist = st.session_state.get("gen_history", [])
    hist.insert(0, {
        "deck": deck_name,
        "count": count,
        "time": datetime.now().strftime("%H:%M:%S")
    })
    st.session_state["gen_history"] = hist[:5]


def text_to_speech_html(text):
    try:
        clean = re.sub(r'<[^>]+>', '', text)
        tts = gTTS(text=clean, lang='en')
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        b64 = base64.b64encode(fp.read()).decode()
        return (
            f'<audio controls style="height:30px;width:100%;margin-top:10px;">'
            f'<source src="data:audio/mp3;base64,{b64}" type="audio/mp3"></audio>'
        )
    except Exception:
        return ""

# ====================== 8. UI COMPONENTS & CSS ======================
def inject_custom_css():
    st.markdown("""
    <style>
        .flashcard {
            background-color: var(--secondary-background-color);
            border: 1px solid var(--text-color);
            border-radius: 15px;
            padding: 30px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            min-height: 300px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
            margin-bottom: 20px;
        }
        .card-front  { font-size: 24px; font-weight: 700; margin-bottom: 20px; color: var(--text-color); }
        .card-back   { font-size: 18px; margin-bottom: 15px; color: var(--primary-color); line-height: 1.5; }
        .card-explanation {
            font-size: 14px; color: var(--text-color); opacity: 0.8; font-style: italic;
            border-top: 1px solid var(--text-color); padding-top: 10px; width: 100%;
        }
        .card-tag {
            background: var(--primary-color); color: #ffffff;
            padding: 4px 10px; border-radius: 20px;
            font-size: 12px; font-weight: 600; text-transform: uppercase; margin-bottom: 15px;
        }
        /* Quota badge styles (#2) */
        .quota-bar  { padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; display: inline-block; }
        .quota-ok   { background: #1a4731; color: #4ade80; }
        .quota-warn { background: #422006; color: #fb923c; }
        .quota-full { background: #450a0a; color: #f87171; }
    </style>
    """, unsafe_allow_html=True)


def render_quota_badge(rpm: int, rpd: int):
    """Render a colour-coded RPM/RPD status pill (#2)."""
    rpm_pct = rpm / MAX_RPM
    rpd_pct = rpd / MAX_RPD
    if rpm_pct >= 1 or rpd_pct >= 1:
        cls = "quota-full"
    elif rpm_pct >= 0.6 or rpd_pct >= 0.6:
        cls = "quota-warn"
    else:
        cls = "quota-ok"
    st.markdown(
        f'<div class="quota-bar {cls}">⚡ RPM {rpm}/{MAX_RPM} &nbsp;|&nbsp; 📅 RPD {rpd}/{MAX_RPD}</div>',
        unsafe_allow_html=True
    )

# ====================== 9. APPLICATION SECTIONS ======================

def section_generator(api_key):
    st.header("🏭 Flashcard Factory")

    # Tag taxonomy input lives in sidebar so it's always accessible (#9)
    tag_taxonomy = st.sidebar.text_input(
        "🏷️ Tag Taxonomy",
        placeholder="e.g., Biology, Chemistry, History",
        help="AI will ONLY use these tags. Leave blank for free tagging."
    )

    col_input, col_sets = st.columns([2, 1])
    content_text, image_content = "", None

    with col_input:
        source_type = st.radio(
            "Input Source",
            ["Text/Paste", "Upload PDF", "Image Analysis", "YouTube URL", "Web Article"],
            horizontal=True
        )

        if source_type == "Text/Paste":
            content_text = st.text_area("Paste Notes Here", height=200)

        elif source_type == "Upload PDF":
            if PDF_AVAILABLE:
                pdf_file = st.file_uploader("Upload PDF Document", type=["pdf"])
                if pdf_file:
                    with st.spinner("Extracting..."):
                        raw_text, error_msg = extract_pdf_text(pdf_file)
                        if not error_msg:
                            st.success(f"PDF Extracted! ({len(raw_text)} chars)")
                            with st.expander("Preview & Edit", expanded=True):
                                content_text = st.text_area("Edit text:", raw_text, height=200)
                        else:
                            st.error(error_msg)
            else:
                st.warning("Please install 'pypdf': pip install pypdf")

        elif source_type == "Image Analysis":
            img_file = st.file_uploader("Upload Diagram", type=["png", "jpg", "jpeg"])
            if img_file:
                image_content = Image.open(img_file)
                st.image(image_content, width=300)
                content_text = "Generate flashcards based on this image."

        elif source_type == "YouTube URL":
            url = st.text_input("Video URL")
            if url:
                with st.spinner("Transcribing..."):
                    try:
                        video_id = extract_youtube_id(url)
                        if not video_id:
                            raise ValueError("Invalid YouTube URL.")
                        raw_text = ""
                        if YOUTUBE_AVAILABLE:
                            try:
                                raw_text = " ".join(
                                    [t['text'] for t in YouTubeTranscriptApi.get_transcript(video_id)]
                                )
                            except Exception:
                                pass
                        if not raw_text:
                            raw_text = get_native_youtube_transcript(video_id)
                        st.success("Transcript Extracted Successfully!")
                        with st.expander("Preview & Edit Transcript", expanded=True):
                            content_text = st.text_area(
                                "Edit text before generating:", raw_text, height=200
                            )
                    except Exception as e:
                        st.error(f"Error extracting transcript: {str(e)}")

        elif source_type == "Web Article":
            url = st.text_input("Article URL")
            if url:
                with st.spinner("Fetching Webpage..."):
                    try:
                        raw_text = fetch_web_content(url)
                        st.success("Web Content Extracted!")
                        with st.expander("Preview & Edit", expanded=True):
                            content_text = st.text_area("Edit text:", raw_text, height=200)
                    except Exception as e:
                        st.error(str(e))

        # Token budget warning (#12)
        if content_text and len(content_text) > TOKEN_BUDGET:
            st.caption(
                f"⚠️ Content will be truncated to {TOKEN_BUDGET:,} chars per request "
                f"(current: {len(content_text):,}). "
                f"Consider editing the preview above for best results."
            )

    with col_sets:
        st.subheader("Config")
        deck_name = st.text_input("Deck Name", placeholder="e.g., Biology 101")
        difficulty = st.select_slider(
            "Level", ["Beginner", "Intermediate", "Expert"], value="Intermediate"
        )
        qty = st.number_input("Count", 1, 30, 10)

        # Live quota badge above button (#2)
        rpm_used, rpd_used = get_quota_status()
        render_quota_badge(rpm_used, rpd_used)

        # Batch info hint (#3)
        chunks_needed = math.ceil(qty / 10)
        if chunks_needed > 1:
            st.caption(
                f"ℹ️ {qty} cards → {chunks_needed} batches "
                f"(~{13 * (chunks_needed - 1)}s inter-batch delay)"
            )

        generate_disabled = rpd_used >= MAX_RPD
        if generate_disabled:
            st.error(f"Daily limit reached ({MAX_RPD} RPD). Resets tomorrow.")

        if st.button(
            "🚀 Generate via AI",
            type="primary",
            use_container_width=True,
            disabled=generate_disabled
        ):
            if not api_key:
                st.error("API Key missing — enter it in the sidebar.")
                return
            if not (content_text or image_content):
                st.warning("No content to generate from. Add text or upload a file.")
                return
            if not deck_name:
                st.warning("Please enter a Deck Name.")
                return

            # Stage-labeled generation spinner (#20)
            with st.status("⚙️ Starting generation...", expanded=True) as status:
                try:
                    status.update(label="📤 Preparing content and checking rate limits...")
                    cards = generate_with_rate_limit(
                        api_key, content_text, image_content,
                        difficulty, qty, source_type, tag_taxonomy,
                        status_obj=status
                    )
                    status.update(
                        label=f"✅ Received {len(cards)} cards — ready for your review.",
                        state="complete"
                    )
                    # Stage into preview buffer (#14) — no DB write yet
                    st.session_state["pending_cards"] = cards
                    st.session_state["pending_deck_name"] = deck_name
                    add_to_gen_history(deck_name, len(cards))

                except Exception as e:
                    status.update(label=f"❌ Generation failed: {e}", state="error")
                    st.error(f"Error: {e}")

    # ─── CARD PREVIEW & CONFIRMATION (#14) ───────────────────────────────────
    if st.session_state.get("pending_cards"):
        pending = st.session_state["pending_cards"]
        p_deck = st.session_state["pending_deck_name"]
        st.divider()
        st.subheader(f"🔍 Review Before Saving — {len(pending)} cards for '{p_deck}'")
        st.caption(
            "Inspect the generated cards below. "
            "Click **Confirm & Save** to write them to the database, or **Discard** to cancel."
        )

        preview_df = pd.DataFrame(pending)[['front', 'back', 'explanation', 'tag']]
        st.dataframe(preview_df, use_container_width=True, hide_index=True)

        col_confirm, col_discard = st.columns(2)
        with col_confirm:
            if st.button("✅ Confirm & Save to Deck", type="primary", use_container_width=True):
                with st.spinner("💾 Writing to database..."):
                    with get_db_connection() as conn:
                        c = conn.cursor()
                        c.execute(
                            "INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)",
                            (p_deck, datetime.now().strftime("%Y-%m-%d"))
                        )
                        deck_id = c.execute(
                            "SELECT id FROM decks WHERE name=?", (p_deck,)
                        ).fetchone()[0]
                        data = [
                            (
                                deck_id,
                                clean_text(card['front']),
                                clean_text(card['back']),
                                card.get('explanation', ''),
                                card['tag']
                            )
                            for card in pending
                        ]
                        c.executemany(
                            "INSERT INTO cards (deck_id, front, back, explanation, tag) "
                            "VALUES (?, ?, ?, ?, ?)",
                            data
                        )
                        conn.commit()
                    saved_count = len(pending)
                    st.session_state["pending_cards"] = []
                    st.session_state["pending_deck_name"] = ""
                st.toast(f"🎉 {saved_count} cards saved to '{p_deck}'!", icon="✅")
                st.rerun()

        with col_discard:
            if st.button("🗑️ Discard", use_container_width=True):
                st.session_state["pending_cards"] = []
                st.session_state["pending_deck_name"] = ""
                st.toast("Cards discarded.", icon="🗑️")
                st.rerun()

    # ─── MANUAL CARD FORM ────────────────────────────────────────────────────
    with st.expander("✍️ Create Flashcard Manually"):
        with st.form("manual_card", clear_on_submit=True):
            with get_db_connection() as conn:
                existing_decks = [
                    d['name'] for d in conn.execute("SELECT name FROM decks").fetchall()
                ]
            m_deck = (
                st.selectbox("Select Deck", existing_decks)
                if existing_decks
                else st.text_input("New Deck Name")
            )
            m_front = st.text_area("Front (Question)")
            m_back  = st.text_area("Back (Answer)")
            m_exp   = st.text_input("Explanation (Optional)")
            m_tag   = st.text_input("Tag", "Manual")

            if st.form_submit_button("Save Card"):
                if m_deck and m_front and m_back:
                    with get_db_connection() as conn:
                        c = conn.cursor()
                        c.execute(
                            "INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)",
                            (m_deck, datetime.now().strftime("%Y-%m-%d"))
                        )
                        d_id = c.execute(
                            "SELECT id FROM decks WHERE name=?", (m_deck,)
                        ).fetchone()[0]
                        c.execute(
                            "INSERT INTO cards (deck_id, front, back, explanation, tag) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (d_id, m_front, m_back, m_exp, m_tag)
                        )
                        conn.commit()
                    st.toast("Card added!", icon="✅")
                else:
                    st.error("Front, Back, and Deck Name are required.")


# ─── STUDY MODE CALLBACKS ────────────────────────────────────────────────────
def cb_show_answer():
    st.session_state["show_answer"] = True

def cb_submit_review(score, card_id):
    if not st.session_state["cram_mode"]:
        update_card_sm2(card_id, score)
    st.session_state["session_stats"]["reviewed"] += 1
    if score >= 3:
        st.session_state["session_stats"]["correct"] += 1
    st.session_state["study_index"] += 1
    st.session_state["show_answer"] = False


def section_study():
    st.header("🧘 Zen Study Mode")
    st.caption("💡 Rate honestly — SM-2 adapts intervals to your actual memory.")

    with get_db_connection() as conn:
        decks = conn.execute("SELECT id, name FROM decks").fetchall()
    if not decks:
        st.info("No decks yet. Head to the Generator to create some!")
        return

    col_deck, col_mode = st.columns([3, 1])
    with col_deck:
        deck_opts = {d['name']: d['id'] for d in decks}
        selected_deck = st.selectbox("Select Deck", list(deck_opts.keys()))
        deck_id = deck_opts[selected_deck]
    with col_mode:
        st.write("")
        cram = st.toggle("🔥 Cram Mode", value=st.session_state["cram_mode"])
        if cram != st.session_state["cram_mode"]:
            st.session_state["cram_mode"] = cram
            st.session_state["current_deck_id"] = None
            st.rerun()

    if st.session_state["current_deck_id"] != deck_id:
        st.session_state["current_deck_id"] = deck_id
        with get_db_connection() as conn:
            if st.session_state["cram_mode"]:
                cards = conn.execute(
                    "SELECT * FROM cards WHERE deck_id = ? ORDER BY RANDOM() LIMIT 50",
                    (deck_id,)
                ).fetchall()
            else:
                cards = conn.execute(
                    "SELECT * FROM cards WHERE deck_id = ? AND next_review <= ? "
                    "ORDER BY next_review ASC LIMIT 50",
                    (deck_id, datetime.now().strftime("%Y-%m-%d"))
                ).fetchall()
            st.session_state["study_queue"]  = [dict(c) for c in cards]
            st.session_state["study_index"]  = 0
            st.session_state["show_answer"]  = False
            st.session_state["session_stats"] = {
                "reviewed": 0, "correct": 0, "start_time": time.time()
            }

    queue = st.session_state["study_queue"]
    idx   = st.session_state["study_index"]

    if not queue:
        st.success("🎉 All caught up! No cards due right now.")
        return

    if idx < len(queue):
        card = queue[idx]
        st.progress((idx + 1) / len(queue), text=f"Card {idx+1}/{len(queue)}")

        audio_html, back_content, explanation_html = "", "<span style='opacity:0.6;'>(Think...)</span>", ""
        if st.session_state["show_answer"]:
            back_content = card['back']
            audio_html   = text_to_speech_html(card['front'] + " ... " + card['back'])
            if card.get("explanation"):
                explanation_html = f'<div class="card-explanation">💡 {card["explanation"]}</div>'

        st.markdown(f"""
        <div class="flashcard">
            <div class="card-tag">{card['tag']}</div>
            <div class="card-front">{card['front']}</div>
            <div class="card-back">{back_content}</div>
            {explanation_html}{audio_html}
        </div>""", unsafe_allow_html=True)
        st.write("")

        if not st.session_state["show_answer"]:
            st.button(
                "👁️ Show Answer", type="primary",
                use_container_width=True, on_click=cb_show_answer
            )
        else:
            cols = st.columns(4)
            labels = ["Again", "Hard", "Good", "Easy"]
            scores = [0, 3, 4, 5]
            for i, col in enumerate(cols):
                col.button(
                    labels[i], use_container_width=True,
                    on_click=cb_submit_review, args=(scores[i], card['id'])
                )
    else:
        st.balloons()
        st.subheader("🏁 Session Complete!")
        stats = st.session_state["session_stats"]
        acc   = int((stats["correct"] / stats["reviewed"] * 100)) if stats["reviewed"] else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("Cards Reviewed", stats["reviewed"])
        c2.metric("Accuracy",       f"{acc}%")
        c3.metric("Time", f"{round((time.time() - stats['start_time']) / 60, 1) if stats['start_time'] else 0} min")
        if st.button("🔄 Start Over"):
            st.session_state["current_deck_id"] = None
            st.rerun()


def section_library():
    st.header("📚 Library")
    with get_db_connection() as conn:
        df_cards = pd.read_sql("SELECT * FROM cards", conn)
        decks    = pd.read_sql("SELECT * FROM decks",  conn)

    if decks.empty:
        st.info("No decks yet. Generate some cards first!")
        return

    t1, t2, t3, t4 = st.tabs(["📊 Stats", "✏️ Edit Cards", "📤 Export", "🗑️ Manage"])

    with t1:
        if not df_cards.empty:
            df_merged = pd.merge(
                df_cards, decks,
                left_on="deck_id", right_on="id",
                suffixes=('_card', '_deck')
            )
            stats_df = df_merged.groupby("name").agg(
                {"repetitions": "mean", "ease_factor": "mean", "id_card": "count"}
            ).rename(columns={
                "id_card": "Total Cards",
                "repetitions": "Avg Reps",
                "ease_factor": "Avg Ease"
            })
            st.dataframe(stats_df, use_container_width=True)

            if df_cards['last_reviewed'].notna().any():
                df_cards['last_reviewed'] = pd.to_datetime(
                    df_cards['last_reviewed']
                ).dt.date
                activity = df_cards['last_reviewed'].value_counts().reset_index()
                activity.columns = ['date', 'count']
                st.altair_chart(
                    alt.Chart(activity).mark_rect().encode(
                        x='date:O', y='count:Q', color='count'
                    ),
                    use_container_width=True
                )
        else:
            st.info("No cards reviewed yet.")

    with t2:
        if not df_cards.empty:
            # Library search bar (#17)
            search_q = st.text_input(
                "🔍 Search cards",
                placeholder="Filter by front, back, or tag...",
                key="lib_search"
            )
            display_df = df_cards[['id', 'front', 'back', 'explanation', 'tag']].copy()
            if search_q:
                q = search_q.lower()
                mask = display_df.apply(
                    lambda r: (
                        q in str(r['front']).lower()
                        or q in str(r['back']).lower()
                        or q in str(r['tag']).lower()
                    ),
                    axis=1
                )
                display_df = display_df[mask]
            st.caption(f"Showing {len(display_df):,} of {len(df_cards):,} card(s)")

            edited = st.data_editor(
                display_df, hide_index=True,
                use_container_width=True, disabled=["id"]
            )

            col_save, col_del = st.columns(2)
            with col_save:
                if st.button("💾 Save Changes", type="primary", use_container_width=True):
                    with get_db_connection() as conn:
                        for _, r in edited.iterrows():
                            conn.execute(
                                "UPDATE cards SET front=?, back=?, explanation=?, tag=? WHERE id=?",
                                (r['front'], r['back'], r['explanation'], r['tag'], r['id'])
                            )
                        conn.commit()
                    st.toast("Changes saved!", icon="💾")

            with col_del:
                ids_in_view = edited['id'].tolist()
                if st.button(
                    f"🗑️ Delete {len(ids_in_view)} Shown Card(s)",
                    use_container_width=True
                ):
                    with get_db_connection() as conn:
                        conn.executemany(
                            "DELETE FROM cards WHERE id=?", [(i,) for i in ids_in_view]
                        )
                        conn.commit()
                    st.toast(f"Deleted {len(ids_in_view)} card(s).", icon="🗑️")
                    st.rerun()
        else:
            st.info("No cards to edit yet.")

    with t3:
        export_deck  = st.selectbox("Export Deck", decks['name'].tolist())
        deck_id_raw  = decks[decks['name'] == export_deck].iloc[0]['id']
        with get_db_connection() as conn:
            cards_df = pd.read_sql(
                "SELECT front, back, explanation, tag FROM cards WHERE deck_id=?",
                conn, params=(int(deck_id_raw),)
            )

        col_csv, col_apkg = st.columns(2)

        with col_csv:
            csv_data = (
                cards_df.to_csv(index=False, header=False).encode('utf-8')
                if not cards_df.empty else b""
            )
            st.download_button(
                "📥 Download CSV",
                data=csv_data,
                file_name=f"{export_deck}.csv",
                disabled=cards_df.empty,
                use_container_width=True
            )

        with col_apkg:
            if GENANKI_AVAILABLE:
                if not cards_df.empty:
                    try:
                        apkg_bytes = export_to_apkg(export_deck, cards_df)
                        st.download_button(
                            "🃏 Download Anki (.apkg)",
                            data=apkg_bytes,
                            file_name=f"{export_deck}.apkg",
                            mime="application/octet-stream",
                            use_container_width=True
                        )
                        st.caption(
                            f"Ready to import into Anki Desktop. "
                            f"{len(cards_df)} notes with custom CSS styling."
                        )
                    except Exception as e:
                        st.error(f"Anki export error: {e}")
                else:
                    st.button("🃏 Download Anki (.apkg)", disabled=True, use_container_width=True)
            else:
                st.warning("Install genanki for Anki export: `pip install genanki`")

    with t4:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Rename Deck")
            ren   = st.selectbox("Select deck", decks['name'].tolist(), key="r_sel")
            new_n = st.text_input("New name")
            if st.button("✏️ Rename") and new_n:
                if rename_deck(ren, new_n):
                    st.toast(f"Renamed to '{new_n}'", icon="✏️")
                    time.sleep(0.4)
                    st.rerun()
                else:
                    st.error("That name already exists.")

        with c2:
            st.subheader("Delete Deck")
            d_del = st.selectbox("Select deck", decks['name'].tolist(), key="d_sel")
            st.warning(f"⚠️ Permanently deletes ALL cards in '{d_del}'.")
            if st.button(f"🗑️ Confirm Delete '{d_del}'", type="primary"):
                delete_deck(d_del)
                st.toast(f"Deck '{d_del}' deleted.", icon="🗑️")
                st.rerun()


# ====================== 10. MAIN ======================
def main():
    inject_custom_css()

    with st.sidebar:
        st.title("🧠 Flashcard Pro v7.0")

        # API Key Persistence (#19) — survives reruns and section switches
        api_key_input = st.text_input(
            "Gemini API Key",
            type="password",
            value=st.session_state.get("api_key", ""),
            placeholder="AIza..."
        )
        if api_key_input != st.session_state.get("api_key"):
            st.session_state["api_key"] = api_key_input
        api_key = st.session_state.get("api_key", "")

        if api_key:
            st.caption("🔑 API key loaded.")

        # Live quota badge (#2)
        rpm_used, rpd_used = get_quota_status()
        render_quota_badge(rpm_used, rpd_used)

        st.metric("📅 Cards Due Today", get_due_cards_count())
        st.divider()

        page = st.radio(
            "Navigation",
            ["Study Mode", "Generator", "Library & Stats"],
            label_visibility="collapsed"
        )

        # Generation history log (#22)
        if st.session_state.get("gen_history"):
            with st.expander("🕓 Recent Generations"):
                for entry in st.session_state["gen_history"]:
                    st.caption(
                        f"**{entry['deck']}** — {entry['count']} cards @ {entry['time']}"
                    )

    if page == "Generator":
        section_generator(api_key)
    elif page == "Study Mode":
        section_study()
    elif page == "Library & Stats":
        section_library()


if __name__ == "__main__":
    main()
