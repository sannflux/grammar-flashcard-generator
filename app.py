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
import functools
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

# ── YouTube: v1.x API with formatters & exception classes ────────────────────
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
_RE_WORD            = re.compile(r'\b\w+\b')

# ====================== API RATE-LIMIT CONSTANTS ======================
_API_MIN_INTERVAL = 12.0   # seconds — enforces 5 RPM (Free Tier)

# ====================== COOKIE FILE PATH ======================
_COOKIE_PATH = "/tmp/yt_cookies.txt"

# ====================== SESSION STATE DEFAULTS ======================
DEFAULT_STATE = {
    "current_deck_id":  None,
    "study_queue":      [],
    "study_index":      0,
    "show_answer":      False,
    "session_stats":    {"reviewed": 0, "correct": 0, "start_time": None},
    "cram_mode":        False,
    "last_api_call_ts": 0.0,
    "cookie_path":      None,
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
    return conn

def init_db():
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

        try:    c.execute("SELECT explanation FROM cards LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE cards ADD COLUMN explanation TEXT DEFAULT ''")
        try:    c.execute("SELECT last_reviewed FROM cards LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE cards ADD COLUMN last_reviewed TEXT")

        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_deck_review ON cards(deck_id, next_review)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_deck_id    ON cards(deck_id)")
        conn.commit()

init_db()

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

# ====================== 4. AI CONTENT ENGINE & EXTRACTORS ======================

# ── FIX 1: source_quote field anchors every card to real source text ──────────
class Flashcard(BaseModel):
    front:        str = Field(
        description=(
            "The question or concept to test. Plain text only. "
            "MUST be derived directly from the provided source material — "
            "do NOT introduce facts or definitions not present in the source."
        )
    )
    back:         str = Field(
        description=(
            "The answer. Use HTML <b> tags for key terms. "
            "Every fact stated here MUST appear verbatim or as a close paraphrase "
            "in the source material. Do NOT add outside knowledge."
        )
    )
    explanation:  str = Field(
        description=(
            "A brief mnemonic or context note that explains WHY the answer is correct, "
            "grounded exclusively in the provided content."
        )
    )
    tag:          str = Field(description="A short category tag.")
    source_quote: str = Field(
        description=(
            "A short verbatim excerpt (10–25 words) copied directly from the source "
            "material that contains the evidence for this card's answer. "
            "If no exact supporting passage exists in the source, set this to an empty string "
            "and omit the card entirely."
        )
    )

class FlashcardSet(BaseModel):
    cards: List[Flashcard]


def clean_text(text):
    if not text: return ""
    return _RE_BOLD.sub(r'<b>\1</b>', text).strip()

def sanitize_json(text):
    text = _RE_CODE_FENCE_JSON.sub('', text)
    return _RE_CODE_FENCE.sub('', text).strip()

def extract_pdf_text(uploaded_file):
    try:
        reader = PdfReader(io.BytesIO(uploaded_file.getvalue()))
        text   = "".join((page.extract_text() or "") + "\n" for page in reader.pages)
        return text[:25000], None
    except Exception as e:
        return None, f"Error reading PDF: {str(e)}"


# ── FIX 2: Post-generation grounding validator ────────────────────────────────
def _validate_cards_against_source(
    cards: list,
    source_text: str,
    min_overlap: float = 0.55,
) -> tuple:
    """
    Returns (valid_cards, rejected_cards).

    For each card the model produced a `source_quote`.  We tokenise both the
    quote and the full source and compute what fraction of the quote's words
    appear in the source.  Cards whose quote has < min_overlap word-overlap
    with the source are rejected as likely hallucinations.

    Image-only requests (no source_text) skip validation and pass everything.
    """
    if not source_text or not source_text.strip():
        return cards, []

    # Stop-words that carry no semantic signal — excluded from overlap scoring
    _STOP = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
        "it", "its", "that", "this", "these", "those", "and", "or", "but",
        "not", "no", "so", "if", "then", "than", "also", "can", "will",
        "may", "has", "have", "had", "do", "does", "did",
    }

    source_words = {
        w for w in _RE_WORD.findall(source_text.lower()) if w not in _STOP
    }

    valid, rejected = [], []
    for card in cards:
        quote = (card.get("source_quote") or "").strip()

        # Model signalled no supporting passage → reject immediately
        if not quote:
            rejected.append(card)
            continue

        quote_tokens = [
            w for w in _RE_WORD.findall(quote.lower()) if w not in _STOP
        ]
        if not quote_tokens:
            # Quote is all stop-words — can't score, give benefit of the doubt
            valid.append(card)
            continue

        overlap = sum(1 for w in quote_tokens if w in source_words) / len(quote_tokens)
        if overlap >= min_overlap:
            valid.append(card)
        else:
            rejected.append(card)

    return valid, rejected


# ── YouTube helpers ───────────────────────────────────────────────────────────

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
def get_youtube_api(cookie_path: str = None):
    if not YOUTUBE_AVAILABLE:
        return None
    if cookie_path and os.path.exists(cookie_path):
        try:
            return YouTubeTranscriptApi(cookies=cookie_path)
        except TypeError:
            pass
    return YouTubeTranscriptApi()

def _scrape_youtube_transcript(video_id: str) -> str:
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookies = {"CONSENT": "YES+cb.20210328-17-p0.en+FX+478"}

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

    raise ValueError("All extraction methods failed. The video may not have closed captions.")

@st.cache_data(ttl=3600, show_spinner=False)
def get_youtube_transcript(video_id: str, cookie_path: str = None) -> str:
    if YOUTUBE_AVAILABLE:
        try:
            ytt             = get_youtube_api(cookie_path)
            transcript_list = ytt.list(video_id)
            try:
                transcript = transcript_list.find_transcript(['en'])
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

# ── FIX 3: Tightened system prompt + temperature=0.1 ─────────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def generate_flashcards(api_key, text_content, image_content, difficulty, count_val):
    client = genai.Client(api_key=api_key)

    # Build a content-bounded context block so the model can reference it
    source_block = ""
    if text_content:
        source_block = (
            f"\n\n<SOURCE_MATERIAL>\n{text_content}\n</SOURCE_MATERIAL>\n\n"
        )

    system_prompt = (
        f"You are a professor creating flashcards for {difficulty}-level students.\n\n"
        "## STRICT GROUNDING RULES — READ CAREFULLY\n"
        "1. Every flashcard MUST be derived EXCLUSIVELY from the text inside "
        "<SOURCE_MATERIAL> tags (or the provided image). "
        "Do NOT use your general training knowledge to add, infer, or embellish facts.\n"
        "2. The `back` field must state only facts that are explicitly present "
        "in the source. If a fact is not there, do not include it.\n"
        "3. The `source_quote` field MUST contain a short verbatim excerpt "
        "(10–25 words) copied directly from the source that contains the evidence "
        "for this card. If no supporting passage exists, omit the card entirely — "
        "do NOT fabricate a quote.\n"
        "4. IGNORE navigation menus, login prompts, ads, and comment sections.\n"
        "5. Output JSON only. Use <b>bold</b> HTML tags in the `back` field for key terms.\n"
        f"6. Create exactly {count_val} flashcards — fewer if the source does not "
        "contain enough distinct facts."
    )

    contents = []
    if source_block:
        contents.append(source_block)
    elif text_content:
        contents.append(text_content)
    if image_content:
        contents.append(image_content)

    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=FlashcardSet,
            temperature=0.1,          # FIX 3: lower = less creative confabulation
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

# ====================== 5. UI COMPONENTS & CSS ======================
def inject_custom_css():
    st.markdown("""
    <style>
        .flashcard { background-color: var(--secondary-background-color); border: 1px solid var(--text-color); border-radius: 15px; padding: 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 300px; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; margin-bottom: 20px; }
        .card-front { font-size: 24px; font-weight: 700; margin-bottom: 20px; color: var(--text-color); }
        .card-back { font-size: 18px; margin-bottom: 15px; color: var(--primary-color); line-height: 1.5; }
        .card-explanation { font-size: 14px; color: var(--text-color); opacity: 0.8; font-style: italic; border-top: 1px solid var(--text-color); padding-top: 10px; width: 100%; }
        .card-tag { background: var(--primary-color); color: #ffffff; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; margin-bottom: 15px; }
        .cookie-box { background: #1a1a2e; border: 1px solid #4a4a8a; border-radius: 10px; padding: 12px; margin-top: 8px; font-size: 13px; }
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
                    with st.spinner("Extracting..."):
                        raw_text, error_msg = extract_pdf_text(pdf_file)
                        if not error_msg:
                            st.success(f"PDF Extracted! ({len(raw_text)} chars)")
                            with st.expander("Preview & Edit", expanded=True):
                                content_text = st.text_area("Edit text:", raw_text, height=200)
                        else:
                            st.error(error_msg)
            else:
                st.warning("Please install 'pypdf'")

        elif source_type == "Image Analysis":
            img_file = st.file_uploader("Upload Diagram", type=["png", "jpg", "jpeg"])
            if img_file:
                image_content = Image.open(img_file)
                st.image(image_content, width=300)
                content_text = ""   # image-only; validator will skip grounding check

        elif source_type == "YouTube URL":
            url = st.text_input("Video URL")
            if url:
                with st.spinner("Transcribing…"):
                    try:
                        video_id = extract_youtube_id(url)
                        if not video_id:
                            raise ValueError("Invalid YouTube URL.")
                        cookie_path = st.session_state.get("cookie_path")
                        raw_text    = get_youtube_transcript(video_id, cookie_path)
                        st.success("✅ Transcript Extracted Successfully!")
                        with st.expander("Preview & Edit Transcript", expanded=True):
                            content_text = st.text_area(
                                "Edit text before generating:", raw_text, height=200
                            )
                    except Exception as e:
                        st.error(f"Transcript blocked by YouTube: {str(e)}")
                        st.info(
                            "💡 **Fix:** Export your YouTube cookies as `cookies.txt` "
                            "and upload them in the sidebar under **YouTube Cookies**."
                        )

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

    with col_sets:
        st.subheader("Config")
        deck_name  = st.text_input("Deck Name", placeholder="e.g., Biology 101")
        difficulty = st.select_slider("Level", ["Beginner", "Intermediate", "Expert"], value="Intermediate")
        qty        = st.number_input("Count", 1, 30, 10)

        if st.button("🚀 Generate via AI", type="primary", use_container_width=True):
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
                    raw_cards = generate_flashcards(
                        api_key, content_text, image_content, difficulty, qty
                    )
                    api_ms = (time.perf_counter() - t_api) * 1000
                    st.write(f"✅ Gemini responded in {api_ms:.0f} ms")

                    # ── FIX 2: Grounding validation ───────────────────────
                    st.write("🔍 Validating cards against source…")
                    cards, rejected = _validate_cards_against_source(
                        raw_cards, content_text
                    )

                    if rejected:
                        st.warning(
                            f"⚠️ {len(rejected)} card(s) removed: their claimed quotes "
                            "could not be matched back to your source material "
                            "(likely hallucinations)."
                        )
                        with st.expander("🗑️ Rejected cards (hallucination log)", expanded=False):
                            for rc in rejected:
                                st.markdown(
                                    f"**Q:** {rc.get('front','—')}  \n"
                                    f"**A:** {_RE_HTML_TAGS.sub('', rc.get('back','—'))}  \n"
                                    f"**Unverified quote:** _{rc.get('source_quote','(none)')}_"
                                )

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
                                 clean_text(card['front']),
                                 clean_text(card['back']),
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
                            label=(
                                f"✅ {len(cards)} verified cards created"
                                + (f" ({len(rejected)} hallucinations removed)" if rejected else "")
                                + f"  (API: {api_ms:.0f} ms | DB: {db_ms:.0f} ms)"
                            ),
                            state="complete"
                        )
                    else:
                        status.update(
                            label="⚠️ No cards passed grounding validation. "
                                  "Try providing more detailed source material.",
                            state="error"
                        )

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
            {
                "id":          c["id"],
                "front":       c["front"],
                "back":        c["back"],
                "explanation": c["explanation"],
                "tag":         c["tag"],
            }
            for c in cards
        ]
        st.session_state["study_index"]   = 0
        st.session_state["show_answer"]   = False
        st.session_state["session_stats"] = {
            "reviewed": 0, "correct": 0, "start_time": time.time()
        }

    queue, idx = st.session_state["study_queue"], st.session_state["study_index"]
    if not queue:
        st.success("All caught up! 🎉"); return

    if idx < len(queue):
        card = queue[idx]
        st.progress((idx + 1) / len(queue), text=f"Card {idx+1}/{len(queue)}")

        audio_html, back_content, explanation_html = "", "<span style='opacity:0.6;'>(Think…)</span>", ""
        if st.session_state["show_answer"]:
            back_content  = card['back']
            audio_html    = text_to_speech_html(card['front'] + " ... " + card['back'])
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
        df_cards = pd.read_sql("SELECT * FROM cards", conn)
        decks    = pd.read_sql("SELECT * FROM decks", conn)

    if decks.empty:
        st.info("No decks."); return

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
            ).rename(columns={"id_card": "Total Cards", "repetitions": "Avg Reps", "ease_factor": "Avg Ease"})
            st.dataframe(stats_df, use_container_width=True)
            if df_cards['last_reviewed'].notna().any():
                df_cards['last_reviewed'] = pd.to_datetime(df_cards['last_reviewed']).dt.date
                activity = df_cards['last_reviewed'].value_counts().reset_index()
                activity.columns = ['date', 'count']
                st.altair_chart(
                    alt.Chart(activity).mark_rect().encode(
                        x='date:O', y='count:Q', color='count'
                    ),
                    use_container_width=True
                )

    with t2:
        if not df_cards.empty:
            edited = st.data_editor(
                df_cards[['id', 'front', 'back', 'explanation', 'tag']],
                hide_index=True, use_container_width=True, disabled=["id"]
            )
            if st.button("💾 Save to DB", type="primary"):
                data = [
                    (r['front'], r['back'], r['explanation'], r['tag'], r['id'])
                    for _, r in edited.iterrows()
                ]
                with get_db_connection() as conn:
                    conn.executemany(
                        "UPDATE cards SET front=?, back=?, explanation=?, tag=? WHERE id=?",
                        data
                    )
                    conn.commit()
                st.success("Updated!")

    with t3:
        export_deck = st.selectbox("Export Deck", decks['name'].tolist())
        deck_id_raw = decks[decks['name'] == export_deck].iloc[0]['id']
        with get_db_connection() as conn:
            cards_df = pd.read_sql(
                "SELECT front, back, tag FROM cards WHERE deck_id=?",
                conn, params=(int(deck_id_raw),)
            )
        st.download_button(
            "Download CSV",
            data=(
                cards_df.to_csv(index=False, header=False).encode('utf-8')
                if not cards_df.empty else b""
            ),
            file_name=f"{export_deck}.csv",
            disabled=cards_df.empty
        )

    with t4:
        c1, c2 = st.columns(2)
        with c1:
            ren   = st.selectbox("Rename", decks['name'].tolist(), key="r_sel")
            new_n = st.text_input("New Name")
            if st.button("Rename") and new_n:
                if rename_deck(ren, new_n):
                    st.toast("✅ Deck renamed successfully!")
                    st.rerun()
                else:
                    st.error("Name already exists.")
        with c2:
            d_del = st.selectbox("Delete", decks['name'].tolist(), key="d_sel")
            if st.button(f"🗑️ Delete {d_del}"):
                delete_deck(d_del)
                st.rerun()

# ====================== 7. MAIN ======================

def main():
    inject_custom_css()
    with st.sidebar:
        st.title("🧠 Flashcard Pro")
        api_key = st.text_input("Gemini API Key", type="password")
        st.metric("Cards Due Today", get_due_cards_count())
        st.divider()

        st.subheader("🍪 YouTube Cookies")
        st.caption(
            "Upload a `cookies.txt` (Netscape format) exported from your browser "
            "to bypass YouTube's bot-blocking on transcripts."
        )
        cookie_file = st.file_uploader(
            "cookies.txt", type=["txt"], key="yt_cookie_upload",
            label_visibility="collapsed"
        )
        if cookie_file is not None:
            cookie_bytes = cookie_file.getvalue()
            with open(_COOKIE_PATH, "wb") as f:
                f.write(cookie_bytes)
            st.session_state["cookie_path"] = _COOKIE_PATH
            st.success("✅ Cookies loaded!")
        elif st.session_state.get("cookie_path") and os.path.exists(_COOKIE_PATH):
            st.success("✅ Cookies active (session)")
            if st.button("🗑️ Remove Cookies"):
                os.remove(_COOKIE_PATH)
                st.session_state["cookie_path"] = None
                get_youtube_transcript.clear()
                get_youtube_api.clear()
                st.rerun()
        else:
            st.info("No cookies loaded — some videos may be blocked.")

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
