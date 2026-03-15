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
import hashlib
import functools
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
    page_title="Flashcard Library Pro v6.3",
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
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        TranscriptsDisabled,
        NoTranscriptFound,
    )
    from youtube_transcript_api.formatters import TextFormatter as YTTextFormatter
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

# ====================== PRE-COMPILED REGEX PATTERNS ======================
_RE_BOLD            = re.compile(r'\*\*(.*?)\*\*')
_RE_CODE_FENCE_JSON = re.compile(r'^```json', re.MULTILINE)
_RE_CODE_FENCE      = re.compile(r'^```',     re.MULTILINE)
_RE_HTML_TAGS       = re.compile(r'<[^>]+>')
_RE_WHITESPACE      = re.compile(r'\s+')
_RE_HTML_BLOCK      = re.compile(r'<HTML.*?>.*?</HTML>', re.IGNORECASE | re.DOTALL)
_RE_IMG_TAG         = re.compile(r'!\[.*?\]\(.*?\)')
_RE_LINK            = re.compile(r'\[([^\]]+)\]\([^\)]+\)')
_RE_MULTILINE       = re.compile(r'\n\s*\n')
_RE_CAPTIONS        = re.compile(r'"captionTracks"\s*:\s*(\[.*?\])')
_RE_ACCESS_DENIED   = re.compile(r'Access Denied|edgesuite\.net|Reference #')

# ====================== API RATE-LIMIT & BUDGET CONSTANTS ======================
_API_MIN_INTERVAL = 12.0
_API_DAILY_LIMIT  = 20
_API_WARN_AT      = 18

# ====================== SESSION STATE DEFAULTS ======================
DEFAULT_STATE = {
    "current_deck_id":  None,
    "study_queue":      [],
    "study_index":      0,
    "show_answer":      False,
    "session_stats":    {"reviewed": 0, "correct": 0, "start_time": None},
    "cram_mode":        False,
    "last_api_call_ts": 0.0,
    "api_budget":       {"date": datetime.now().strftime("%Y-%m-%d"), "count": 0},
    "_db_initialized":  False,
    # YouTube cookie state
    "yt_cookie_path":   None,   # path to converted Netscape temp file on disk
    "yt_cookie_hash":   "",     # MD5 of uploaded JSON — detects re-uploads
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ====================== 2. DATABASE ENGINE ======================
DB_NAME = "flashcards_v5.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA cache_size=-8000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA mmap_size=268435456;")
    return conn

def init_db() -> bool:
    migrated = False
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            description TEXT,
            created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deck_id INTEGER,
            front TEXT,
            back TEXT,
            explanation TEXT,
            tag TEXT,
            ease_factor REAL DEFAULT 2.5,
            interval INTEGER DEFAULT 0,
            repetitions INTEGER DEFAULT 0,
            next_review TEXT DEFAULT CURRENT_DATE,
            last_reviewed TEXT,
            FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE)''')
        try:
            c.execute("SELECT explanation FROM cards LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE cards ADD COLUMN explanation TEXT DEFAULT ''")
            migrated = True
        try:
            c.execute("SELECT last_reviewed FROM cards LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE cards ADD COLUMN last_reviewed TEXT")
            migrated = True
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_deck_review ON cards(deck_id, next_review)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_deck_id    ON cards(deck_id)")
        conn.commit()
    return migrated

# ====================== 3. CORE LOGIC ======================
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
            next_review   = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")
            last_reviewed = datetime.now().strftime("%Y-%m-%d")
            c.execute(
                'UPDATE cards SET ease_factor=?, interval=?, repetitions=?, next_review=?, last_reviewed=? WHERE id=?',
                (ease, interval, reps, next_review, last_reviewed, card_id)
            )
            conn.commit()

def delete_deck(deck_name):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM decks WHERE name=?", (deck_name,))
        conn.commit()

def delete_cards_by_ids(card_ids: list):
    if not card_ids:
        return
    placeholders = ",".join("?" * len(card_ids))
    with get_db_connection() as conn:
        conn.execute(f"DELETE FROM cards WHERE id IN ({placeholders})", card_ids)
        conn.commit()

def rename_deck(old_name, new_name):
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE decks SET name=? WHERE name=?", (new_name, old_name))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

@st.cache_data(ttl=30, show_spinner=False)
def get_due_cards_count():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM cards WHERE next_review <= ?", (today,)
        ).fetchone()[0]

def _increment_api_budget():
    today  = datetime.now().strftime("%Y-%m-%d")
    budget = st.session_state["api_budget"]
    if budget["date"] != today:
        st.session_state["api_budget"] = {"date": today, "count": 0}
    st.session_state["api_budget"]["count"] += 1

def _get_api_budget_count() -> int:
    today  = datetime.now().strftime("%Y-%m-%d")
    budget = st.session_state["api_budget"]
    if budget["date"] != today:
        st.session_state["api_budget"] = {"date": today, "count": 0}
        return 0
    return budget["count"]

# ====================== 4. AI CONTENT ENGINE & EXTRACTORS ======================
class Flashcard(BaseModel):
    front:       str = Field(description="The question/concept. Plain text.")
    back:        str = Field(description="The answer. Use HTML <b> for key terms.")
    explanation: str = Field(description="A short context or mnemonic explaining WHY the answer is correct.")
    tag:         str = Field(description="A short category tag.")

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

def clean_text(text):
    if not text: return ""
    return _RE_BOLD.sub(r'<b>\1</b>', text).strip()

def sanitize_json(text):
    text = _RE_CODE_FENCE_JSON.sub('', text)
    return _RE_CODE_FENCE.sub('', text).strip()

# ── PDF extraction ────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def extract_pdf_text(file_hash: str, file_bytes: bytes):
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        chunks, total = [], 0
        for page in reader.pages:
            page_text = (page.extract_text() or "") + "\n"
            chunks.append(page_text)
            total += len(page_text)
            if total >= 25_000:
                break
        return "".join(chunks)[:25_000], None
    except Exception as e:
        return None, f"Error reading PDF: {str(e)}"

def hash_uploaded_file(uploaded_file) -> str:
    return hashlib.md5(uploaded_file.getvalue()).hexdigest()

# ── Image compression ─────────────────────────────────────────────────────────

def compress_image(img: Image.Image, max_px: int = 1024, quality: int = 85) -> Image.Image:
    ratio = min(max_px / max(img.width, img.height), 1.0)
    if ratio < 1.0:
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    return img

# ── YouTube cookie converter ──────────────────────────────────────────────────

def convert_cookies_json_to_netscape(json_bytes: bytes) -> tuple[str, int]:
    """
    Converts a Cookie-Editor JSON export to Netscape HTTP Cookie File format.

    Handles the Cookie-Editor schema exactly:
      domain, expirationDate, hostOnly, httpOnly, name, path,
      sameSite, secure, session, storeId, value

    Deduplication strategy: when (name, domain) collide — which happens
    frequently in Cookie-Editor exports — keep the entry with the LATEST
    expirationDate so stale tokens never shadow fresh ones.

    Returns (netscape_string, cookie_count).
    """
    raw = json.loads(json_bytes.decode("utf-8"))

    # ── Step 1: deduplicate by (name, domain), keep latest expiration ────
    deduped: dict = {}
    for c in raw:
        key      = (c["name"], c["domain"])
        new_exp  = float(c.get("expirationDate") or 0)
        prev_exp = float((deduped[key].get("expirationDate") or 0)) if key in deduped else -1
        if new_exp > prev_exp:
            deduped[key] = c

    # ── Step 2: emit Netscape lines ───────────────────────────────────────
    lines = [
        "# Netscape HTTP Cookie File",
        "# Converted from Cookie-Editor JSON by Flashcard Pro",
    ]
    for c in deduped.values():
        domain      = c["domain"]
        # Netscape "include_subdomains" = TRUE when domain starts with "."
        # (matches hostOnly=false in Cookie-Editor)
        subdomain   = "TRUE" if domain.startswith(".") else "FALSE"
        path        = c.get("path", "/")
        secure      = "TRUE" if c.get("secure", False) else "FALSE"
        # session=true means no persistent expiry → use 0
        expiry      = int(float(c["expirationDate"])) if c.get("expirationDate") else 0
        name        = c["name"]
        value       = c["value"]
        lines.append(
            f"{domain}\t{subdomain}\t{path}\t{secure}\t{expiry}\t{name}\t{value}"
        )

    return "\n".join(lines), len(deduped)


def _save_netscape_cookie_file(json_bytes: bytes) -> tuple[str, int, str]:
    """
    Convert JSON bytes → Netscape string → write to temp file.
    Returns (temp_file_path, cookie_count, error_message).
    """
    try:
        netscape_str, count = convert_cookies_json_to_netscape(json_bytes)
        # Write to a named temp file that persists until we delete it
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write(netscape_str)
        tmp.flush()
        tmp.close()
        return tmp.name, count, ""
    except Exception as e:
        return "", 0, str(e)


def _clear_cookie_file():
    """Delete the on-disk temp file and wipe session state."""
    path = st.session_state.get("yt_cookie_path")
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    st.session_state["yt_cookie_path"] = None
    st.session_state["yt_cookie_hash"] = ""

# ── YouTube transcript engine ─────────────────────────────────────────────────

@functools.lru_cache(maxsize=64)
def extract_youtube_id(url: str):
    patterns = [
        r'(?:v=)([\w-]{11})',
        r'(?:youtu\.be/)([\w-]{11})',
        r'(?:embed/)([\w-]{11})',
        r'(?:shorts/)([\w-]{11})',
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    if re.match(r'^[\w-]{11}$', url.strip()):
        return url.strip()
    return None

@st.cache_resource
def get_youtube_api(cookie_path: str = ""):
    """
    Returns a YouTubeTranscriptApi instance.
    Keyed on cookie_path so a new authenticated client is created automatically
    whenever the user uploads a new cookie file.

    cookie_path = ""          → unauthenticated (fallback)
    cookie_path = "/tmp/..."  → authenticated with user's session cookies
    """
    if not YOUTUBE_AVAILABLE:
        return None
    if cookie_path and os.path.exists(cookie_path):
        return YouTubeTranscriptApi(cookies=cookie_path)
    return YouTubeTranscriptApi()

def _scrape_youtube_transcript(video_id: str) -> str:
    """HTML scrape fallback — Stage 1 (direct XML) then Stage 2 (proxy)."""
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookies = {"CONSENT": "YES+cb.20210328-17-p0.en+FX+478"}

    # Stage 1
    try:
        page_html = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers=headers, cookies=cookies, timeout=10
        ).text
        m = _RE_CAPTIONS.search(page_html)
        if m:
            xml_url    = json.loads(m.group(1))[0]['baseUrl']
            xml_resp   = requests.get(xml_url, headers=headers, timeout=10)
            transcript = _RE_HTML_TAGS.sub(' ', xml_resp.text)
            transcript = html.unescape(transcript)
            return _RE_WHITESPACE.sub(' ', transcript).strip()
    except Exception:
        pass

    # Stage 2
    try:
        proxy = requests.get(
            f"https://youtubetranscript.com/?server_vid2={video_id}", timeout=10
        )
        if '<transcript>' in proxy.text or '<?xml' in proxy.text:
            transcript = _RE_HTML_TAGS.sub(' ', proxy.text)
            transcript = html.unescape(transcript)
            return _RE_WHITESPACE.sub(' ', transcript).strip()
    except Exception:
        pass

    raise ValueError(
        "YouTube is blocking transcript access from this server's IP. "
        "Upload your YouTube cookies.json in the sidebar to authenticate."
    )

@st.cache_data(ttl=3600, show_spinner=False)
def get_youtube_transcript(video_id: str, cookie_path: str = "") -> str:
    """
    Full 3-plan transcript engine. cookie_path is included in the cache key
    so switching from unauthenticated → authenticated invalidates the cache.

    Plan A — youtube-transcript-api v1.x (authenticated if cookie_path set)
    Plan B — HTML parse of captionTracks XML
    Plan C — youtubetranscript.com proxy
    """
    if YOUTUBE_AVAILABLE:
        try:
            ytt             = get_youtube_api(cookie_path)
            transcript_list = ytt.list(video_id)
            try:
                transcript = transcript_list.find_transcript(["en"])
            except Exception:
                transcript = next(iter(transcript_list))
            fetched   = transcript.fetch()
            formatter = YTTextFormatter()
            return formatter.format_transcript(fetched)
        except Exception:
            pass

    return _scrape_youtube_transcript(video_id)

# ── Web content ───────────────────────────────────────────────────────────────

def clean_web_markdown(text):
    text        = _RE_HTML_BLOCK.sub('', text)
    text        = _RE_IMG_TAG.sub('', text)
    text        = _RE_LINK.sub(r'\1', text)
    clean_lines = [ln for ln in text.split('\n') if not _RE_ACCESS_DENIED.search(ln)]
    return _RE_MULTILINE.sub('\n\n', '\n'.join(clean_lines)).strip()

@st.cache_data(ttl=300, show_spinner=False)
def fetch_web_content(url: str) -> str:
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", timeout=15)
        resp.raise_for_status()
        text = clean_web_markdown(resp.text)
        if not text or (text.count("Access Denied") > 5 and len(text) < 1000):
            raise ValueError("WAF")
        return text[:25000]
    except Exception:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp    = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        return " ".join(soup.stripped_strings)[:25000]

# ── Gemini ────────────────────────────────────────────────────────────────────

@st.cache_resource
def get_genai_client(api_key: str):
    return genai.Client(api_key=api_key)

@st.cache_data(ttl=300, show_spinner=False)
def validate_api_key(api_key: str) -> bool:
    if not api_key or len(api_key) < 10:
        return False
    try:
        client = get_genai_client(api_key)
        next(iter(client.models.list()))
        return True
    except Exception:
        return False

@st.cache_data(
    show_spinner=False,
    hash_funcs={Image.Image: lambda img: hashlib.md5(img.tobytes()).hexdigest()}
)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def generate_flashcards(api_key, text_content, image_content, difficulty, count_val):
    client        = get_genai_client(api_key)
    system_prompt = (
        f"Act as a professor for {difficulty} level students. "
        f"Create {count_val} flashcards strictly based on the core educational content provided. "
        "IGNORE website navigation menus, sidebars, 'Log in' prompts, and comment sections. "
        "Focus ONLY on actual definitions, rules, or educational topics. "
        "Output JSON only. 'back' field MUST use <b>bold</b> tags for keywords."
    )
    contents = []
    if text_content:  contents.append(text_content)
    if image_content: contents.append(image_content)
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=FlashcardSet,
            temperature=0.3
        )
    )
    return json.loads(sanitize_json(response.text)).get("cards", [])

# ── TTS ───────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def text_to_speech_html(text: str) -> str:
    try:
        clean = _RE_HTML_TAGS.sub('', text)
        tts   = gTTS(text=clean, lang='en')
        fp    = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        b64 = base64.b64encode(fp.read()).decode()
        return (
            '<audio controls style="height:30px;width:100%;margin-top:10px;">'
            f'<source src="data:audio/mp3;base64,{b64}" type="audio/mp3"></audio>'
        )
    except Exception:
        return ""

# ── Anki .apkg export ─────────────────────────────────────────────────────────

def export_deck_apkg(deck_name: str, cards_df: pd.DataFrame) -> bytes:
    model_id  = abs(hash(f"fpro_model_{deck_name}")) % (10 ** 10)
    deck_id   = abs(hash(f"fpro_deck_{deck_name}"))  % (10 ** 10)
    model = genanki.Model(
        model_id, "Flashcard Pro",
        fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Explanation"}],
        templates=[{
            "name": "Card",
            "qfmt": "<div style='font-size:20px;font-weight:700;'>{{Front}}</div>",
            "afmt": (
                "{{FrontSide}}<hr id=answer>"
                "<div style='font-size:16px;'>{{Back}}</div>"
                "<div style='font-size:12px;font-style:italic;margin-top:8px;'>{{Explanation}}</div>"
            ),
        }]
    )
    anki_deck = genanki.Deck(deck_id, deck_name)
    for _, row in cards_df.iterrows():
        anki_deck.add_note(genanki.Note(
            model=model,
            fields=[str(row.get("front","")), str(row.get("back","")), str(row.get("explanation",""))]
        ))
    with tempfile.NamedTemporaryFile(suffix=".apkg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        genanki.Package(anki_deck).write_to_file(tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_path)

# ====================== 5. UI COMPONENTS & CSS ======================
def inject_custom_css():
    st.markdown("""
    <style>
        .flashcard { background-color: var(--secondary-background-color); border: 1px solid var(--text-color); border-radius: 15px; padding: 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 300px; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; margin-bottom: 20px; }
        .card-front { font-size: 24px; font-weight: 700; margin-bottom: 20px; color: var(--text-color); }
        .card-back { font-size: 18px; margin-bottom: 15px; color: var(--primary-color); line-height: 1.5; }
        .card-explanation { font-size: 14px; color: var(--text-color); opacity: 0.8; font-style: italic; border-top: 1px solid var(--text-color); padding-top: 10px; width: 100%; }
        .card-tag { background: var(--primary-color); color: #ffffff; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; margin-bottom: 15px; }
    </style>
    """, unsafe_allow_html=True)

# ====================== 6. APPLICATION SECTIONS ======================

def section_generator(api_key):
    st.header("🏭 Flashcard Factory")

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
                    with st.spinner("Extracting…"):
                        f_hash        = hash_uploaded_file(pdf_file)
                        raw_text, err = extract_pdf_text(f_hash, pdf_file.getvalue())
                        if not err:
                            st.success(f"PDF Extracted! ({len(raw_text):,} chars)")
                            with st.expander("Preview & Edit", expanded=True):
                                content_text = st.text_area("Edit text:", raw_text, height=200)
                        else:
                            st.error(err)
            else:
                st.warning("Please install 'pypdf'")

        elif source_type == "Image Analysis":
            img_file = st.file_uploader("Upload Diagram", type=["png", "jpg", "jpeg"])
            if img_file:
                raw_img       = Image.open(img_file)
                image_content = compress_image(raw_img)
                st.image(image_content, width=300)
                orig_px = raw_img.width  * raw_img.height
                comp_px = image_content.width * image_content.height
                if orig_px > comp_px:
                    st.caption(
                        f"🗜️ Compressed to {image_content.width}×{image_content.height} "
                        f"({comp_px/orig_px:.0%} of original pixels)"
                    )
                content_text = "Generate flashcards based on this image."

        elif source_type == "YouTube URL":
            url = st.text_input("Video URL")
            if url:
                cookie_path = st.session_state.get("yt_cookie_path") or ""
                if not cookie_path:
                    st.info(
                        "💡 No YouTube cookies loaded. If extraction fails, upload your "
                        "`cookies.json` in the sidebar **🍪 YouTube Cookies** panel.",
                        icon="ℹ️"
                    )
                with st.spinner("Transcribing…"):
                    try:
                        video_id = extract_youtube_id(url)
                        if not video_id:
                            raise ValueError("Invalid YouTube URL.")
                        raw_text = get_youtube_transcript(video_id, cookie_path)
                        auth_label = "🔐 authenticated" if cookie_path else "🌐 unauthenticated"
                        st.success(f"✅ Transcript extracted ({auth_label})")
                        with st.expander("Preview & Edit Transcript", expanded=True):
                            content_text = st.text_area(
                                "Edit text before generating:", raw_text, height=200
                            )
                    except Exception as e:
                        st.error(f"Transcript error: {str(e)}")

        elif source_type == "Web Article":
            url = st.text_input("Article URL")
            if url:
                with st.spinner("Fetching Webpage…"):
                    try:
                        raw_text = fetch_web_content(url)
                        st.success("Web Content Extracted!")
                        with st.expander("Preview & Edit", expanded=True):
                            content_text = st.text_area("Edit text:", raw_text, height=200)
                    except Exception as e:
                        st.error(str(e))

    with col_sets:
        st.subheader("Config")
        deck_name  = st.text_input("Deck Name", placeholder="e.g., Biology 101")
        difficulty = st.select_slider("Level", ["Beginner", "Intermediate", "Expert"], value="Intermediate")
        qty        = st.number_input("Count", 1, 30, 10)

        calls_today = _get_api_budget_count()
        st.progress(min(calls_today / _API_DAILY_LIMIT, 1.0))
        if calls_today >= _API_DAILY_LIMIT:
            st.error(f"🚫 Daily limit reached ({calls_today}/{_API_DAILY_LIMIT})")
        elif calls_today >= _API_WARN_AT:
            st.warning(f"⚠️ API calls today: {calls_today}/{_API_DAILY_LIMIT}")
        else:
            st.caption(f"✅ API calls today: {calls_today}/{_API_DAILY_LIMIT}")
        st.write("")

        budget_exhausted = calls_today >= _API_DAILY_LIMIT

        if st.button("🚀 Generate via AI", type="primary", use_container_width=True,
                     disabled=budget_exhausted):
            if not api_key:                         st.error("API Key Missing");          return
            if not (content_text or image_content): st.warning("No valid content");       return
            if not deck_name:                       st.error("Please enter a Deck Name"); return

            elapsed     = time.perf_counter() - st.session_state["last_api_call_ts"]
            wait_needed = max(0.0, _API_MIN_INTERVAL - elapsed)

            with st.status("🤖 Generating Flashcards…", expanded=True) as status:
                if wait_needed > 0.1:
                    st.write(f"⏳ Rate-limit buffer: {wait_needed:.1f}s remaining…")
                    time.sleep(wait_needed)

                st.write("📡 Sending to Gemini…")
                t_api = time.perf_counter()

                try:
                    st.session_state["last_api_call_ts"] = time.perf_counter()
                    cards  = generate_flashcards(api_key, content_text, image_content, difficulty, qty)
                    api_ms = (time.perf_counter() - t_api) * 1000
                    _increment_api_budget()

                    st.write(f"✅ Gemini responded in {api_ms:.0f} ms")
                    st.write("💾 Saving to database…")

                    if cards:
                        t_db = time.perf_counter()
                        with get_db_connection() as conn:
                            c = conn.cursor()
                            c.execute(
                                "INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)",
                                (deck_name, datetime.now().strftime("%Y-%m-%d"))
                            )
                            deck_id = c.execute(
                                "SELECT id FROM decks WHERE name=?", (deck_name,)
                            ).fetchone()[0]
                            data = [
                                (deck_id,
                                 _RE_BOLD.sub(r'<b>\1</b>', card['front']).strip(),
                                 _RE_BOLD.sub(r'<b>\1</b>', card['back']).strip(),
                                 card.get('explanation', ''),
                                 card['tag'])
                                for card in cards
                            ]
                            c.executemany(
                                "INSERT INTO cards (deck_id, front, back, explanation, tag) VALUES (?,?,?,?,?)",
                                data
                            )
                            conn.commit()
                        db_ms = (time.perf_counter() - t_db) * 1000
                        status.update(
                            label=f"✅ {len(cards)} cards created!  (API: {api_ms:.0f} ms | DB: {db_ms:.0f} ms)",
                            state="complete"
                        )
                    else:
                        status.update(label="⚠️ No cards returned by Gemini.", state="error")

                except Exception as e:
                    status.update(label="❌ Generation failed", state="error")
                    st.error(f"Failed: {e}")

    with st.expander("✍️ Create Flashcard Manually"):
        with st.form("manual_card", clear_on_submit=True):
            with get_db_connection() as conn:
                existing_decks = [d['name'] for d in conn.execute("SELECT name FROM decks").fetchall()]
            m_deck  = st.selectbox("Select Deck", existing_decks) if existing_decks else st.text_input("New Deck Name")
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
                            "INSERT INTO cards (deck_id, front, back, explanation, tag) VALUES (?,?,?,?,?)",
                            (d_id, m_front, m_back, m_exp, m_tag)
                        )
                        conn.commit()
                    st.success("Card added!")
                else:
                    st.error("Missing fields")

# ── Study callbacks ───────────────────────────────────────────────────────────

def cb_show_answer():
    st.session_state["show_answer"] = True

def cb_submit_review(score, card_id):
    if not st.session_state["cram_mode"]:
        update_card_sm2(card_id, score)
    st.session_state["session_stats"]["reviewed"] += 1
    if score >= 3:
        st.session_state["session_stats"]["correct"] += 1
    st.session_state["study_index"] += 1
    st.session_state["show_answer"]  = False

def section_study():
    st.header("🧘 Zen Study Mode")
    with get_db_connection() as conn:
        decks = conn.execute("SELECT id, name FROM decks").fetchall()
    if not decks:
        st.info("No decks."); return

    col_deck, col_mode = st.columns([3, 1])
    with col_deck:
        deck_opts     = {d['name']: d['id'] for d in decks}
        selected_deck = st.selectbox("Select Deck", list(deck_opts.keys()))
        deck_id       = deck_opts[selected_deck]

    with col_mode:
        st.write("")
        cram = st.toggle("🔥 Cram Mode", value=st.session_state["cram_mode"])
        if cram != st.session_state["cram_mode"]:
            st.session_state["cram_mode"]       = cram
            st.session_state["current_deck_id"] = None
            st.rerun()

    if st.session_state["current_deck_id"] != deck_id:
        st.session_state["current_deck_id"] = deck_id
        with get_db_connection() as conn:
            if st.session_state["cram_mode"]:
                cards = conn.execute(
                    "SELECT id, front, back, explanation, tag FROM cards WHERE deck_id=? ORDER BY RANDOM() LIMIT 50",
                    (deck_id,)
                ).fetchall()
            else:
                cards = conn.execute(
                    "SELECT id, front, back, explanation, tag FROM cards "
                    "WHERE deck_id=? AND next_review<=? ORDER BY next_review ASC LIMIT 50",
                    (deck_id, datetime.now().strftime("%Y-%m-%d"))
                ).fetchall()
        st.session_state["study_queue"] = [
            {"id": c["id"], "front": c["front"], "back": c["back"],
             "explanation": c["explanation"], "tag": c["tag"]}
            for c in cards
        ]
        st.session_state["study_index"]   = 0
        st.session_state["show_answer"]   = False
        st.session_state["session_stats"] = {"reviewed": 0, "correct": 0, "start_time": time.time()}

    queue, idx = st.session_state["study_queue"], st.session_state["study_index"]
    if not queue:
        st.success("All caught up! 🎉"); return

    if idx < len(queue):
        card = queue[idx]
        st.progress((idx + 1) / len(queue), text=f"Card {idx+1}/{len(queue)}")

        audio_html, back_content, explanation_html = "", "<span style='opacity:0.6;'>(Think…)</span>", ""
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
            st.button("👁️ Show Answer", type="primary", use_container_width=True, on_click=cb_show_answer)
        else:
            cols   = st.columns(4)
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
        c1.metric("Cards",    stats['reviewed'])
        c2.metric("Accuracy", f"{acc}%")
        c3.metric("Time",     f"{round((time.time() - stats['start_time']) / 60, 1) if stats['start_time'] else 0} min")
        if st.button("Start Over"):
            st.session_state["current_deck_id"] = None
            st.rerun()

def section_library():
    st.header("📚 Library")
    with get_db_connection() as conn:
        decks = pd.read_sql("SELECT id, name, created_at FROM decks", conn)
    if decks.empty:
        st.info("No decks."); return

    t1, t2, t3, t4 = st.tabs(["📊 Stats", "✏️ Edit Cards", "📤 Export", "🗑️ Manage"])

    with t1:
        with get_db_connection() as conn:
            df_stats = pd.read_sql(
                "SELECT id, deck_id, repetitions, ease_factor, last_reviewed FROM cards", conn
            )
        if not df_stats.empty:
            df_merged = pd.merge(df_stats, decks, left_on="deck_id", right_on="id", suffixes=('_card','_deck'))
            summary   = df_merged.groupby("name").agg(
                {"repetitions": "mean", "ease_factor": "mean", "id_card": "count"}
            ).rename(columns={"id_card": "Total Cards", "repetitions": "Avg Reps", "ease_factor": "Avg Ease"})
            st.dataframe(summary, use_container_width=True)
            if df_stats['last_reviewed'].notna().any():
                df_stats['last_reviewed'] = pd.to_datetime(df_stats['last_reviewed']).dt.date
                activity                  = df_stats['last_reviewed'].value_counts().reset_index()
                activity.columns          = ['date', 'count']
                st.altair_chart(
                    alt.Chart(activity).mark_rect().encode(x='date:O', y='count:Q', color='count'),
                    use_container_width=True
                )

    with t2:
        selected_edit_deck = st.selectbox("Select deck to edit", decks['name'].tolist(), key="edit_deck_sel")
        sel_deck_id        = int(decks[decks['name'] == selected_edit_deck].iloc[0]['id'])
        with get_db_connection() as conn:
            df_edit = pd.read_sql(
                "SELECT id, front, back, explanation, tag FROM cards WHERE deck_id=?",
                conn, params=(sel_deck_id,)
            )
        if df_edit.empty:
            st.info("No cards in this deck yet.")
        else:
            df_edit.insert(0, "delete", False)
            edited = st.data_editor(
                df_edit, hide_index=True, use_container_width=True, disabled=["id"],
                column_config={
                    "delete": st.column_config.CheckboxColumn(
                        "🗑️", help="Check rows to mark for deletion", default=False, width="small"
                    )
                }
            )
            col_save, col_del = st.columns(2)
            with col_save:
                if st.button("💾 Save Changes", type="primary", use_container_width=True):
                    rows_to_save = edited[edited["delete"] == False]
                    data = [(r['front'], r['back'], r['explanation'], r['tag'], r['id'])
                            for _, r in rows_to_save.iterrows()]
                    with get_db_connection() as conn:
                        conn.executemany(
                            "UPDATE cards SET front=?, back=?, explanation=?, tag=? WHERE id=?", data
                        )
                        conn.commit()
                    st.success(f"Saved {len(data)} cards!")
            with col_del:
                ids_to_delete = edited[edited["delete"] == True]['id'].tolist()
                if ids_to_delete:
                    if st.button(f"🗑️ Delete {len(ids_to_delete)} card(s)", type="secondary", use_container_width=True):
                        delete_cards_by_ids(ids_to_delete)
                        st.toast(f"🗑️ Deleted {len(ids_to_delete)} card(s).")
                        st.rerun()
                else:
                    st.button("🗑️ Delete Selected", disabled=True, use_container_width=True)

    with t3:
        export_deck = st.selectbox("Export Deck", decks['name'].tolist(), key="exp_sel")
        exp_deck_id = int(decks[decks['name'] == export_deck].iloc[0]['id'])
        with get_db_connection() as conn:
            cards_df = pd.read_sql(
                "SELECT front, back, explanation, tag FROM cards WHERE deck_id=?",
                conn, params=(exp_deck_id,)
            )
        col_csv, col_apkg = st.columns(2)
        with col_csv:
            st.download_button(
                "📄 Download CSV",
                data=(cards_df[['front','back','tag']].to_csv(index=False, header=False).encode('utf-8')
                      if not cards_df.empty else b""),
                file_name=f"{export_deck}.csv",
                disabled=cards_df.empty, use_container_width=True
            )
        with col_apkg:
            if GENANKI_AVAILABLE:
                if not cards_df.empty:
                    st.download_button(
                        "📦 Download .apkg (Anki)",
                        data=export_deck_apkg(export_deck, cards_df),
                        file_name=f"{export_deck}.apkg",
                        mime="application/octet-stream", use_container_width=True
                    )
                else:
                    st.button("📦 Download .apkg (Anki)", disabled=True, use_container_width=True)
            else:
                st.caption("Install `genanki` to enable Anki export.")

    with t4:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Rename Deck")
            ren   = st.selectbox("Select deck", decks['name'].tolist(), key="r_sel")
            new_n = st.text_input("New name")
            if st.button("✏️ Rename", use_container_width=True) and new_n:
                if rename_deck(ren, new_n):
                    st.toast(f'✅ Renamed to "{new_n}"')
                    st.rerun()
                else:
                    st.error("That name already exists.")
        with c2:
            st.subheader("Delete Deck")
            d_del      = st.selectbox("Select deck to delete", decks['name'].tolist(), key="d_sel")
            with get_db_connection() as conn:
                row        = conn.execute(
                    "SELECT COUNT(*) FROM cards WHERE deck_id=(SELECT id FROM decks WHERE name=?)", (d_del,)
                ).fetchone()
                card_count = row[0] if row else 0
            st.warning(f'Deleting **"{d_del}"** will permanently remove the deck and all **{card_count} card(s)**.')
            confirmed = st.checkbox(f'Yes, permanently delete "{d_del}"', key=f"del_confirm_{d_del}")
            if st.button("🗑️ Delete Deck", disabled=not confirmed,
                         type="primary" if confirmed else "secondary",
                         use_container_width=True) and confirmed:
                delete_deck(d_del)
                st.toast(f'🗑️ "{d_del}" deleted.')
                st.rerun()

# ====================== 7. MAIN ======================

def main():
    inject_custom_css()

    if not st.session_state["_db_initialized"]:
        migrated = init_db()
        st.session_state["_db_initialized"] = True
        if migrated:
            st.toast("🔧 Database schema updated.")

    with st.sidebar:
        st.title("🧠 Flashcard Pro")

        # ── Gemini API key ────────────────────────────────────────────────
        api_key = st.text_input("Gemini API Key", type="password")
        if api_key:
            st.caption("✅ API key valid" if validate_api_key(api_key) else "❌ Invalid API key")

        st.metric("Cards Due Today", get_due_cards_count())
        st.divider()

        # ── YouTube Cookies panel ─────────────────────────────────────────
        cookie_path   = st.session_state.get("yt_cookie_path") or ""
        cookie_active = bool(cookie_path and os.path.exists(cookie_path))

        with st.expander(
            "🔐 YouTube Cookies  ✅" if cookie_active else "🍪 YouTube Cookies",
            expanded=not cookie_active
        ):
            if cookie_active:
                st.success("Cookies active — transcripts are authenticated.")
                if st.button("🗑️ Clear Cookies", use_container_width=True, key="clear_yt_cookies"):
                    _clear_cookie_file()
                    st.toast("🍪 Cookies cleared.")
                    st.rerun()
            else:
                st.caption(
                    "If YouTube blocks transcript fetching, upload your `cookies.json` "
                    "exported from **Cookie-Editor** (Chrome/Firefox extension)."
                )
                cookie_upload = st.file_uploader(
                    "Upload cookies.json", type=["json"], key="yt_cookie_upload",
                    label_visibility="collapsed"
                )
                if cookie_upload:
                    file_hash = hashlib.md5(cookie_upload.getvalue()).hexdigest()
                    # Only re-process if this is a new file
                    if file_hash != st.session_state.get("yt_cookie_hash", ""):
                        with st.spinner("Converting cookies…"):
                            path, count, err = _save_netscape_cookie_file(cookie_upload.getvalue())
                        if err:
                            st.error(f"❌ Parse error: {err}")
                        else:
                            # Clean up previous temp file before storing new one
                            old_path = st.session_state.get("yt_cookie_path")
                            if old_path and os.path.exists(old_path):
                                try: os.unlink(old_path)
                                except OSError: pass
                            st.session_state["yt_cookie_path"] = path
                            st.session_state["yt_cookie_hash"] = file_hash
                            st.success(f"✅ {count} cookies loaded.")
                            st.rerun()

        st.divider()
        page = st.radio(
            "Navigation",
            ["Study Mode", "Generator", "Library & Stats"],
            label_visibility="collapsed"
        )

    if   page == "Generator":       section_generator(api_key)
    elif page == "Study Mode":      section_study()
    elif page == "Library & Stats": section_library()

if __name__ == "__main__":
    main()
