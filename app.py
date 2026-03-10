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
import html
from datetime import datetime, timedelta
import altair as alt

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional, Tuple
from PIL import Image

from gtts import gTTS
from tenacity import retry, stop_after_attempt, wait_exponential

# ====================== 1. SAFE IMPORTS & CONFIGURATION ======================
st.set_page_config(
    page_title="Flashcard Library Pro v6.5",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded"
)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

YOUTUBE_AVAILABLE = False
YouTubeTranscriptApi = None
try:
    from youtube_transcript_api import YouTubeTranscriptApi as _YTA
    YouTubeTranscriptApi = _YTA
    YOUTUBE_AVAILABLE = True
except Exception:
    YOUTUBE_AVAILABLE = False

def _get_pkg_version(pkg: str) -> str:
    try:
        import importlib.metadata as md
        return md.version(pkg)
    except Exception:
        try:
            import pkg_resources
            return pkg_resources.get_distribution(pkg).version
        except Exception:
            return "unknown"

try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

DEFAULT_STATE = {
    "current_deck_id": None,
    "study_queue": [],
    "study_index": 0,
    "show_answer": False,
    "session_stats": {"reviewed": 0, "correct": 0, "start_time": None},
    "cram_mode": False,
    "yt_assist_text": "",
    "yt_last_error": "",
    "yt_last_video_id": "",
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ====================== 2. DATABASE ENGINE ======================
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, description TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, deck_id INTEGER, front TEXT, back TEXT,
            explanation TEXT, tag TEXT, ease_factor REAL DEFAULT 2.5, interval INTEGER DEFAULT 0,
            repetitions INTEGER DEFAULT 0, next_review TEXT DEFAULT CURRENT_DATE, last_reviewed TEXT,
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

            next_review = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")
            last_reviewed = datetime.now().strftime("%Y-%m-%d")

            c.execute('''UPDATE cards SET ease_factor=?, interval=?, repetitions=?, next_review=?, last_reviewed=? WHERE id=?''',
                      (ease, interval, reps, next_review, last_reviewed, card_id))
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
        return conn.execute("SELECT COUNT(*) FROM cards WHERE next_review <= ?", (today,)).fetchone()[0]

# ====================== 4. AI CONTENT ENGINE & EXTRACTORS ======================
class Flashcard(BaseModel):
    front: str = Field(description="The question/concept. Plain text.")
    back: str = Field(description="The answer. Use HTML <b> for key terms.")
    explanation: str = Field(description="A short context or mnemonic explaining WHY the answer is correct.")
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
    if parsed_url.hostname in ('www.youtube.com', 'youtube.com', 'm.youtube.com'):
        if parsed_url.path == '/watch':
            return urllib.parse.parse_qs(parsed_url.query).get('v', [None])[0]
        if parsed_url.path.startswith(('/shorts/', '/embed/', '/v/')):
            parts = parsed_url.path.split('/')
            return parts[2] if len(parts) > 2 else None
    match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return match.group(1) if match else None

def _normalize_transcript_text(chunks: List[str]) -> str:
    text = " ".join([c.strip() for c in chunks if c and c.strip()])
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def _parse_transcript_xml(xml_text: str) -> str:
    bodies = re.findall(r'<text[^>]*>(.*?)</text>', xml_text, flags=re.DOTALL | re.IGNORECASE)
    bodies = [html.unescape(b) for b in bodies]
    bodies = [re.sub(r'<[^>]+>', ' ', b) for b in bodies]
    return _normalize_transcript_text(bodies)

def _parse_transcript_json3(json_text: str) -> str:
    try:
        obj = json.loads(json_text)
    except Exception:
        return ""
    chunks = []
    for ev in obj.get("events", []) or []:
        for seg in ev.get("segs", []) or []:
            t = seg.get("utf8")
            if t:
                chunks.append(t)
    return _normalize_transcript_text(chunks)

def _parse_transcript_vtt(vtt_text: str) -> str:
    lines = vtt_text.splitlines()
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.upper().startswith("WEBVTT"):
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}\.\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\.\d{3}', s):
            continue
        if re.match(r'^\d{2}:\d{2}\.\d{3}\s+-->\s+\d{2}:\d{2}\.\d{3}', s):
            continue
        s = re.sub(r'<[^>]+>', '', s)
        out.append(s)
    return _normalize_transcript_text(out)

def _parse_transcript_srt(srt_text: str) -> str:
    lines = srt_text.splitlines()
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if re.match(r'^\d+$', s):
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}', s):
            continue
        s = re.sub(r'<[^>]+>', '', s)
        out.append(s)
    return _normalize_transcript_text(out)

def _parse_transcript_auto(text: str) -> str:
    if not text:
        return ""
    t = text.lstrip()

    if t.startswith("{") or t.startswith("["):
        parsed = _parse_transcript_json3(text)
        if parsed:
            return parsed

    if "WEBVTT" in t[:80].upper():
        parsed = _parse_transcript_vtt(text)
        if parsed:
            return parsed

    if re.search(r'\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}', text):
        parsed = _parse_transcript_srt(text)
        if parsed:
            return parsed

    if "<text" in text and "</text>" in text:
        parsed = _parse_transcript_xml(text)
        if parsed:
            return parsed

    if len(text.strip()) > 20 and "<html" not in text.lower():
        return _normalize_transcript_text([text])

    return ""

def parse_uploaded_transcript(uploaded_file) -> Tuple[str, Optional[str]]:
    try:
        raw_bytes = uploaded_file.getvalue()
        raw = raw_bytes.decode("utf-8", errors="ignore")
        parsed = _parse_transcript_auto(raw)
        if not parsed or len(parsed.strip()) < 20:
            return "", "Could not parse transcript file (supported: .txt .vtt .srt .json .xml)."
        return parsed[:25000], None
    except Exception as e:
        return "", f"Error reading transcript file: {e}"

def get_youtube_transcript_via_library(video_id: str, preferred_langs: Optional[List[str]] = None) -> str:
    if not YOUTUBE_AVAILABLE:
        raise RuntimeError("youtube-transcript-api not installed.")

    preferred_langs = preferred_langs or ["en", "en-US", "en-GB"]

    # Most compatible call across versions:
    data = YouTubeTranscriptApi.get_transcript(video_id, languages=preferred_langs)
    return _normalize_transcript_text([x.get("text", "") for x in data])

def get_native_youtube_transcript(video_id: str) -> Tuple[str, List[str]]:
    stage_log = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookies = {"CONSENT": "YES+cb.20210328-17-p0.en+FX+478"}

    try:
        params = {"v": video_id, "type": "list"}
        r = requests.get("https://www.youtube.com/api/timedtext", params=params, headers=headers, cookies=cookies, timeout=15)
        stage_log.append(f"Timedtext list status={r.status_code} len={len(r.text)}")
        if r.ok and "<transcript_list" in r.text:
            tracks = []
            for m in re.finditer(r'<track\b([^/>]*)\/?>', r.text, flags=re.IGNORECASE):
                attrs = m.group(1)
                lang_code = re.search(r'lang_code="([^"]+)"', attrs)
                kind = re.search(r'kind="([^"]+)"', attrs)
                if lang_code:
                    tracks.append({"lang_code": lang_code.group(1), "kind": kind.group(1) if kind else None})
            stage_log.append(f"Timedtext list tracks={len(tracks)}")
            if tracks:
                chosen = None
                for t in tracks:
                    if t["lang_code"].startswith("en") and t.get("kind") is None:
                        chosen = t
                        break
                if chosen is None:
                    for t in tracks:
                        if t["lang_code"].startswith("en"):
                            chosen = t
                            break
                if chosen is None:
                    chosen = tracks[0]

                params2 = {"v": video_id, "lang": chosen["lang_code"], "fmt": "json3"}
                if chosen.get("kind"):
                    params2["kind"] = chosen["kind"]
                r2 = requests.get("https://www.youtube.com/api/timedtext", params=params2, headers=headers, cookies=cookies, timeout=15)
                stage_log.append(f"Timedtext json3 status={r2.status_code} len={len(r2.text)} head='{r2.text[:80].replace(chr(10),' ')}'")
                if r2.ok:
                    parsed = _parse_transcript_auto(r2.text)
                    stage_log.append(f"Timedtext parsed_len={len(parsed)}")
                    if parsed and len(parsed) > 20:
                        return parsed, stage_log
    except Exception as e:
        stage_log.append(f"Timedtext failed: {type(e).__name__}: {str(e)[:120]}")

    raise ValueError("Native YouTube extraction blocked or unavailable in this environment.")

def _external_provider_transcript(video_id: str, stage_log: List[str]) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        u = f"https://youtubetranscript.com/?server_vid2={video_id}"
        r = requests.get(u, headers=headers, timeout=15)
        stage_log.append(f"External1 status={r.status_code} len={len(r.text)}")
        if r.ok:
            parsed = _parse_transcript_auto(r.text)
            if parsed and len(parsed) > 20:
                return parsed
    except Exception as e:
        stage_log.append(f"External1 failed: {type(e).__name__}: {str(e)[:80]}")

    return ""

@st.cache_data(show_spinner=False, ttl=60 * 60)
def cached_transcript_attempt(video_id: str, allow_external: bool) -> Tuple[str, List[str]]:
    logs: List[str] = []
    logs.append(f"youtube-transcript-api version: {_get_pkg_version('youtube-transcript-api')}")

    if YOUTUBE_AVAILABLE:
        try:
            t = get_youtube_transcript_via_library(video_id)
            if t and len(t) > 20:
                logs.append("Plan A succeeded ✅")
                return t, logs
            logs.append("Plan A returned empty ❌")
        except Exception as e:
            logs.append(f"Plan A failed ❌ ({type(e).__name__}: {str(e)})")
    else:
        logs.append("Plan A not installed ❌")

    try:
        t, l = get_native_youtube_transcript(video_id)
        logs.append("Plan B succeeded ✅")
        logs.extend([f"Native: {x}" for x in l])
        return t, logs
    except Exception as e:
        logs.append(f"Plan B failed ❌ ({type(e).__name__}: {str(e)})")

    if allow_external:
        t = _external_provider_transcript(video_id, logs)
        if t and len(t) > 20:
            logs.append("Plan C succeeded ✅")
            return t, logs
        logs.append("Plan C failed/empty ❌")

    return "", logs

def clean_web_markdown(text):
    text = re.sub(r'<HTML.*?>.*?</HTML>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    lines = text.split('\n')
    clean_lines = [line for line in lines if "Access Denied" not in line and "edgesuite.net" not in line and "Reference #" not in line]
    text = '\n'.join(clean_lines)
    return re.sub(r'\n\s*\n', '\n\n', text).strip()

def fetch_web_content(url):
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", timeout=15)
        resp.raise_for_status()
        text = clean_web_markdown(resp.text)
        if not text or (text.count("Access Denied") > 5 and len(text) < 1000):
            raise ValueError("WAF")
        return text[:25000]
    except Exception:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        return " ".join(soup.stripped_strings)[:25000]

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def generate_flashcards(api_key, text_content, image_content, difficulty, count_val):
    client = genai.Client(api_key=api_key)
    system_prompt = f"Act as a professor for {difficulty} level students. Create {count_val} flashcards strictly based on the core educational content provided. IGNORE website navigation menus, sidebars, 'Log in' prompts, and comment sections. Focus ONLY on the actual definitions, rules, or educational topics. Output JSON only. 'back' field MUST use <b>bold</b> tags for keywords."
    contents = []
    if text_content:
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
            temperature=0.3
        )
    )
    return json.loads(sanitize_json(response.text)).get("cards", [])

def text_to_speech_html(text):
    try:
        clean_t = re.sub(r'<[^>]+>', '', text)
        tts = gTTS(text=clean_t, lang='en')
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        b64 = base64.b64encode(fp.read()).decode()
        return f'<audio controls style="height: 30px; width: 100%; margin-top: 10px;"><source src="data:audio/mp3;base64,{b64}" type="audio/mp3"></audio>'
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
        .smallhelp { font-size: 13px; opacity: 0.8; }
        .warnbox { padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(255,165,0,0.4); background: rgba(255,165,0,0.08); }
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
                content_text = "Generate flashcards based on this image."

        elif source_type == "YouTube URL":
            url = st.text_input("Video URL")
            allow_external = st.toggle("Allow external transcript fallback (recommended on Streamlit Cloud)", value=True)

            video_id = extract_youtube_id(url) if url else None
            if url and not video_id:
                st.error("Invalid YouTube URL (couldn't detect video ID).")

            if video_id:
                st.caption(f"Video ID: {video_id}")

                if st.session_state.get("yt_last_video_id") != video_id:
                    st.session_state["yt_last_video_id"] = video_id
                    st.session_state["yt_last_error"] = ""
                    with st.spinner("Auto-fetching transcript..."):
                        t, logs = cached_transcript_attempt(video_id, allow_external=allow_external)
                        if t and len(t.strip()) > 20:
                            st.session_state["yt_assist_text"] = t
                        else:
                            st.session_state["yt_last_error"] = "Auto transcript fetch failed on this host/IP."

                    with st.expander("Debug details", expanded=False):
                        st.write(logs)

                if st.session_state.get("yt_last_error"):
                    st.markdown(
                        f"<div class='warnbox'><b>Auto-fetch status</b><br/>{html.escape(st.session_state['yt_last_error'])}</div>",
                        unsafe_allow_html=True
                    )

                with st.expander("Transcript (auto-filled when possible)", expanded=True):
                    st.session_state["yt_assist_text"] = st.text_area(
                        "Transcript:",
                        value=st.session_state.get("yt_assist_text", ""),
                        height=240
                    )
                    content_text = st.session_state["yt_assist_text"]

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
        deck_name = st.text_input("Deck Name", placeholder="e.g., Biology 101")
        difficulty = st.select_slider("Level", ["Beginner", "Intermediate", "Expert"], value="Intermediate")
        qty = st.number_input("Count", 1, 30, 10)

        if st.button("🚀 Generate via AI", type="primary", use_container_width=True):
            if not api_key:
                st.error("API Key Missing")
                return
            if not (content_text or image_content):
                st.warning("No valid content")
                return
            if source_type == "YouTube URL" and (not content_text or len(content_text.strip()) < 50):
                st.warning("Transcript is empty/too short. Auto-fetch may be blocked; upload/paste transcript.")
                return

            with st.spinner("Gemini is thinking..."):
                try:
                    cards = generate_flashcards(api_key, content_text, image_content, difficulty, qty)
                    if cards and deck_name:
                        with get_db_connection() as conn:
                            c = conn.cursor()
                            c.execute(
                                "INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)",
                                (deck_name, datetime.now().strftime("%Y-%m-%d"))
                            )
                            deck_id = c.execute("SELECT id FROM decks WHERE name=?", (deck_name,)).fetchone()[0]
                            data = [
                                (deck_id, clean_text(card['front']), clean_text(card['back']),
                                 card.get('explanation', ''), card['tag'])
                                for card in cards
                            ]
                            c.executemany(
                                "INSERT INTO cards (deck_id, front, back, explanation, tag) VALUES (?, ?, ?, ?, ?)",
                                data
                            )
                            conn.commit()
                        st.success(f"Created {len(cards)} cards!")
                    else:
                        st.warning("No cards returned, or Deck Name is missing.")
                except Exception as e:
                    st.error(f"Failed: {e}")

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
    with get_db_connection() as conn:
        decks = conn.execute("SELECT id, name FROM decks").fetchall()
    if not decks:
        st.info("No decks.")
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
                    "SELECT * FROM cards WHERE deck_id = ? AND next_review <= ? ORDER BY next_review ASC LIMIT 50",
                    (deck_id, datetime.now().strftime("%Y-%m-%d"))
                ).fetchall()

            st.session_state["study_queue"] = [dict(c) for c in cards]
            st.session_state["study_index"], st.session_state["show_answer"] = 0, False
            st.session_state["session_stats"] = {"reviewed": 0, "correct": 0, "start_time": time.time()}

    queue, idx = st.session_state["study_queue"], st.session_state["study_index"]
    if not queue:
        st.success("All caught up!")
        return

    if idx < len(queue):
        card = queue[idx]
        st.progress((idx + 1) / len(queue), text=f"Card {idx+1}/{len(queue)}")

        audio_html, back_content, explanation_html = "", "<span style='opacity:0.6;'>(Think...)</span>", ""
        if st.session_state["show_answer"]:
            back_content = card['back']
            audio_html = text_to_speech_html(card['front'] + " ... " + card['back'])
            if card.get("explanation"):
                explanation_html = f'<div class="card-explanation">💡 {card["explanation"]}</div>'

        st.markdown(f"""
        <div class="flashcard">
            <div class="card-tag">{card['tag']}</div>
            <div class="card-front">{card['front']}</div>
            <div class="card-back">{back_content}</div>
            {explanation_html}{audio_html}
        </div>""", unsafe_allow_html=True)

        if not st.session_state["show_answer"]:
            st.button("👁️ Show Answer", type="primary", use_container_width=True, on_click=cb_show_answer)
        else:
            cols, labels, scores = st.columns(4), ["Again", "Hard", "Good", "Easy"], [0, 3, 4, 5]
            for i, col in enumerate(cols):
                col.button(labels[i], use_container_width=True, on_click=cb_submit_review, args=(scores[i], card['id']))
    else:
        st.balloons()
        st.subheader("🏁 Complete!")
        stats = st.session_state["session_stats"]
        acc = int((stats["correct"] / stats["reviewed"] * 100)) if stats["reviewed"] else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("Cards", stats['reviewed'])
        c2.metric("Accuracy", f"{acc}%")
        c3.metric("Time", f"{round((time.time() - stats['start_time']) / 60, 1) if stats['start_time'] else 0} min")
        if st.button("Start Over"):
            st.session_state["current_deck_id"] = None
            st.rerun()

def section_library():
    st.header("📚 Library")
    with get_db_connection() as conn:
        df_cards = pd.read_sql("SELECT * FROM cards", conn)
        decks = pd.read_sql("SELECT * FROM decks", conn)

    if decks.empty:
        st.info("No decks.")
        return

    t1, t2, t3, t4 = st.tabs(["📊 Stats", "✏️ Edit Cards", "📤 Export", "🗑️ Manage"])

    with t1:
        if not df_cards.empty:
            df_merged = pd.merge(df_cards, decks, left_on="deck_id", right_on="id", suffixes=('_card', '_deck'))
            stats_df = (
                df_merged.groupby("name")
                .agg({"repetitions": "mean", "ease_factor": "mean", "id_card": "count"})
                .rename(columns={"id_card": "Total Cards", "repetitions": "Avg Reps", "ease_factor": "Avg Ease"})
            )
            st.dataframe(stats_df, use_container_width=True)
            if df_cards['last_reviewed'].notna().any():
                df_cards['last_reviewed'] = pd.to_datetime(df_cards['last_reviewed']).dt.date
                activity = df_cards['last_reviewed'].value_counts().reset_index()
                activity.columns = ['date', 'count']
                st.altair_chart(
                    alt.Chart(activity).mark_rect().encode(x='date:O', y='count:Q', color='count'),
                    use_container_width=True
                )

    with t2:
        if not df_cards.empty:
            edited = st.data_editor(
                df_cards[['id', 'front', 'back', 'explanation', 'tag']],
                hide_index=True,
                use_container_width=True,
                disabled=["id"]
            )
            if st.button("💾 Save to DB", type="primary"):
                with get_db_connection() as conn:
                    for _, r in edited.iterrows():
                        conn.execute(
                            "UPDATE cards SET front=?, back=?, explanation=?, tag=? WHERE id=?",
                            (r['front'], r['back'], r['explanation'], r['tag'], r['id'])
                        )
                    conn.commit()
                st.success("Updated!")

    with t3:
        export_deck = st.selectbox("Export Deck", decks['name'].tolist())
        deck_id_raw = decks[decks['name'] == export_deck].iloc[0]['id']
        with get_db_connection() as conn:
            cards_df = pd.read_sql(
                "SELECT front, back, tag FROM cards WHERE deck_id=?",
                conn,
                params=(int(deck_id_raw),)
            )
        st.download_button(
            "Download CSV",
            data=cards_df.to_csv(index=False, header=False).encode('utf-8') if not cards_df.empty else b"",
            file_name=f"{export_deck}.csv",
            disabled=cards_df.empty
        )

    with t4:
        c1, c2 = st.columns(2)
        with c1:
            ren = st.selectbox("Rename", decks['name'].tolist(), key="r_sel")
            new_n = st.text_input("New Name")
            if st.button("Rename") and new_n:
                if rename_deck(ren, new_n):
                    st.success("Done!")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error("Name exists.")
        with c2:
            d_del = st.selectbox("Delete", decks['name'].tolist(), key="d_sel")
            if st.button(f"🗑️ Delete {d_del}"):
                delete_deck(d_del)
                st.rerun()

def main():
    inject_custom_css()
    with st.sidebar:
        st.title("🧠 Flashcard Pro")
        api_key = st.text_input("Gemini API Key", type="password")
        st.metric("Cards Due Today", get_due_cards_count())
        st.divider()
        page = st.radio("Navigation", ["Study Mode", "Generator", "Library & Stats"], label_visibility="collapsed")

    if page == "Generator":
        section_generator(api_key)
    elif page == "Study Mode":
        section_study()
    elif page == "Library & Stats":
        section_library()

if __name__ == "__main__":
    main()
