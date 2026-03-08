import streamlit as st
import pandas as pd
import tempfile
import os
import time
import json
import sqlite3
import re
import requests
import math
from datetime import datetime, timedelta
from typing import List, Optional
from PIL import Image
import io

# ====================== 1. SAFE IMPORTS & CONFIGURATION ======================
st.set_page_config(
    page_title="Flashcard Library Pro v3.0", 
    page_icon="🧠", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Dependency Checks ---
try:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel, Field
    GENAI_AVAILABLE = True
except ImportError:
    st.error("CRITICAL: 'google-genai' library missing. pip install google-genai")
    GENAI_AVAILABLE = False

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
    import pypdf
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

# ====================== 2. PERSISTENCE & SETTINGS ======================
SETTINGS_FILE = "flashcard_settings.json"

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_settings(key, value):
    current = load_settings()
    current[key] = value
    with open(SETTINGS_FILE, "w") as f:
        json.dump(current, f)

# Initialize Session State
DEFAULT_STATE = {
    "current_deck_id": None,
    "study_queue": [],
    "study_index": 0,
    "show_answer": False,
    "session_stats": {"reviewed": 0, "correct": 0},
    "processing": False,
    "api_key": load_settings().get("api_key", "")
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ====================== 3. DATABASE ENGINE ======================
DB_NAME = "flashcards_v3.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            name TEXT UNIQUE, 
            created_at TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            deck_id INTEGER, 
            front TEXT, 
            back TEXT, 
            tag TEXT,
            ease_factor REAL DEFAULT 2.5,
            interval INTEGER DEFAULT 0,
            repetitions INTEGER DEFAULT 0,
            next_review TEXT DEFAULT CURRENT_DATE,
            FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE
        )''')
        conn.commit()

init_db()

# ====================== 4. CORE LOGIC: SM-2 ALGORITHM ======================
def update_card_sm2(card_id, quality):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT ease_factor, interval, repetitions FROM cards WHERE id=?", (card_id,))
        row = c.fetchone()
        
        if row:
            ease, interval, reps = row['ease_factor'], row['interval'], row['repetitions']
            
            if quality < 3:
                reps = 0
                interval = 1
            else:
                if reps == 0:
                    interval = 1
                elif reps == 1:
                    interval = 6
                else:
                    interval = math.ceil(interval * ease)
                
                reps += 1
                ease = ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
                if ease < 1.3: ease = 1.3 

            next_review_date = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")

            c.execute('''UPDATE cards 
                         SET ease_factor=?, interval=?, repetitions=?, next_review=? 
                         WHERE id=?''', 
                      (ease, interval, reps, next_review_date, card_id))
            conn.commit()

# ====================== 5. CONTENT ENGINE ======================
if GENAI_AVAILABLE:
    class Flashcard(BaseModel):
        front: str = Field(description="The question. Plain text.")
        back: str = Field(description="The answer. Use HTML <b> for key terms.")
        tag: str = Field(description="A short category tag.")

    class FlashcardSet(BaseModel):
        cards: List[Flashcard]

def clean_text(text):
    if not text: return ""
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    return text.strip()

@st.cache_data(show_spinner=False)
def extract_pdf_text(file_bytes):
    if not PDF_AVAILABLE: return "Error: 'pypdf' not installed."
    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text[:30000] # Limit for context window
    except Exception as e:
        return f"Error reading PDF: {e}"

@st.cache_data(show_spinner=False)
def fetch_youtube_transcript(url):
    if not YOUTUBE_AVAILABLE: return "Error: 'youtube-transcript-api' missing."
    try:
        video_id = url.split("v=")[1].split("&")[0]
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([t['text'] for t in transcript])
    except Exception as e:
        return f"Error: {str(e)}"

@st.cache_data(show_spinner=False)
def fetch_web_content(url):
    if not BS4_AVAILABLE: return "Error: 'beautifulsoup4' missing."
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        for script in soup(["script", "style", "nav", "footer"]): script.decompose()
        return " ".join(soup.stripped_strings)[:20000]
    except Exception as e:
        return f"Error: {str(e)}"

def generate_flashcards(api_key, content_input, difficulty, count_val, is_image=False):
    if not GENAI_AVAILABLE: return []
    
    try:
        client = genai.Client(api_key=api_key)
        
        system_prompt = f"""
        Act as a professor for {difficulty} level students.
        Create {count_val} flashcards based strictly on the provided content.
        RULES:
        1. Output JSON only.
        2. 'back' field MUST use <b>bold</b> tags for keywords.
        3. Keep questions concise.
        """
        
        contents = [content_input] if is_image else [content_input]

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite", # Using latest fast model (multimodal)
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=FlashcardSet,
                temperature=0.3
            )
        )
        return json.loads(response.text).get("cards", [])
    except Exception as e:
        st.error(f"AI Generation Failed: {e}")
        return []

# ====================== 6. UI SECTIONS ======================

def section_generator():
    st.header("🏭 Flashcard Factory")
    
    # Input Source
    source_type = st.radio("Source", ["Text/Paste", "Upload PDF", "Upload Image (Notes)", "YouTube URL", "Web Article"], horizontal=True)
    
    content_payload = None
    is_image_mode = False
    
    if source_type == "Text/Paste":
        content_payload = st.text_area("Paste Notes", height=200)
    
    elif source_type == "Upload PDF":
        uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])
        if uploaded_file:
            with st.spinner("Reading PDF..."):
                content_payload = extract_pdf_text(uploaded_file.getvalue())
                if "Error" in content_payload: st.error(content_payload)
                else: st.success(f"Loaded {len(content_payload)} characters")

    elif source_type == "Upload Image (Notes)":
        uploaded_file = st.file_uploader("Upload Image", type=["png", "jpg", "jpeg", "webp"])
        if uploaded_file:
            try:
                image = Image.open(uploaded_file)
                st.image(image, caption="Uploaded Notes", use_column_width=True)
                content_payload = image
                is_image_mode = True
            except Exception as e:
                st.error(f"Error processing image: {e}")

    elif source_type == "YouTube URL":
        url = st.text_input("Video URL")
        if url:
            with st.spinner("Fetching Transcript..."):
                content_payload = fetch_youtube_transcript(url)
                if "Error" in content_payload: st.error(content_payload)
                else: st.success("Transcript loaded")

    elif source_type == "Web Article":
        url = st.text_input("Article URL")
        if url:
            with st.spinner("Scraping..."):
                content_payload = fetch_web_content(url)
                if "Error" in content_payload: st.error(content_payload)
                else: st.success("Article loaded")

    # Settings
    c1, c2, c3 = st.columns(3)
    deck_name = c1.text_input("Deck Name")
    difficulty = c2.select_slider("Difficulty", ["Beginner", "Intermediate", "Expert"], value="Intermediate")
    qty = c3.number_input("Count", 1, 50, 10)

    # Action
    if st.button("🚀 Generate", type="primary"):
        if not st.session_state.get("api_key"):
            st.error("⚠️ API Key missing. Go to Settings.")
            return
        if not deck_name:
            st.warning("⚠️ Please name your deck.")
            return
        if not content_payload:
            st.warning("⚠️ No content provided.")
            return

        with st.spinner("🤖 AI is analyzing and generating cards..."):
            cards = generate_flashcards(st.session_state["api_key"], content_payload, difficulty, qty, is_image=is_image_mode)
            
            if cards:
                with get_db_connection() as conn:
                    c = conn.cursor()
                    c.execute("INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)", 
                              (deck_name, datetime.now().strftime("%Y-%m-%d")))
                    c.execute("SELECT id FROM decks WHERE name=?", (deck_name,))
                    deck_id = c.fetchone()[0]
                    
                    data = [(deck_id, clean_text(c['front']), clean_text(c['back']), c['tag']) for c in cards]
                    c.executemany("INSERT INTO cards (deck_id, front, back, tag) VALUES (?, ?, ?, ?)", data)
                    conn.commit()
                
                st.toast(f"✅ Created {len(cards)} cards!", icon="🎉")
                time.sleep(1.5)
                st.switch_page("app.py") # Optional reset or just rerun
                st.rerun()

def section_study_mode():
    st.header("🧘 Zen Study Mode")
    
    with get_db_connection() as conn:
        decks = conn.execute("SELECT id, name FROM decks").fetchall()
    
    if not decks:
        st.info("Library empty. Create a deck first.")
        return

    deck_opts = {d['name']: d['id'] for d in decks}
    selected_deck = st.selectbox("Select Deck", list(deck_opts.keys()))
    deck_id = deck_opts[selected_deck]
    
    # Load Queue Logic
    if st.session_state["current_deck_id"] != deck_id:
        st.session_state["current_deck_id"] = deck_id
        today = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            q = "SELECT * FROM cards WHERE deck_id = ? AND next_review <= ? ORDER BY next_review ASC LIMIT 50"
            cards = conn.execute(q, (deck_id, today)).fetchall()
            st.session_state["study_queue"] = [dict(c) for c in cards]
            st.session_state["study_index"] = 0
            st.session_state["show_answer"] = False
            st.session_state["session_stats"] = {"reviewed": 0, "correct": 0}

    queue = st.session_state["study_queue"]
    idx = st.session_state["study_index"]

    if not queue:
        st.success("🎉 No cards due for this deck!")
        if st.button("Review All Anyway"):
             with get_db_connection() as conn:
                cards = conn.execute("SELECT * FROM cards WHERE deck_id = ?", (deck_id,)).fetchall()
                st.session_state["study_queue"] = [dict(c) for c in cards]
                st.rerun()
        return

    if idx < len(queue):
        card = queue[idx]
        st.progress((idx)/len(queue), text=f"Card {idx+1}/{len(queue)}")

        # Card UI
        with st.container():
            st.markdown(f"""
            <div style="
                background-color: {'#262730' if st.session_state.get('theme')=='dark' else '#ffffff'};
                border: 1px solid #444; border-radius: 15px; padding: 40px; text-align: center;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 20px;">
                <div style="font-size:12px; color:#888; text-transform:uppercase; margin-bottom:10px;">{card['tag']}</div>
                <div style="font-size:24px; font-weight:bold; margin-bottom:20px;">{card['front']}</div>
                {"<hr style='opacity:0.2'>" if st.session_state["show_answer"] else ""}
                <div style="font-size:20px; color:#aaa;">
                    {card['back'] if st.session_state["show_answer"] else "..."}
                </div>
            </div>
            """, unsafe_allow_html=True)

        if not st.session_state["show_answer"]:
            if st.button("👁️ Show Answer", type="primary", use_container_width=True):
                st.session_state["show_answer"] = True
                st.rerun()
        else:
            c1, c2, c3, c4 = st.columns(4)
            def rate(q):
                update_card_sm2(card['id'], q)
                st.session_state["session_stats"]["reviewed"] += 1
                if q >= 3: st.session_state["session_stats"]["correct"] += 1
                st.session_state["study_index"] += 1
                st.session_state["show_answer"] = False
                st.rerun()

            with c1: st.button("🔴 Again", on_click=rate, args=(0,), use_container_width=True)
            with c2: st.button("🟠 Hard", on_click=rate, args=(3,), use_container_width=True)
            with c3: st.button("🟢 Good", on_click=rate, args=(4,), use_container_width=True)
            with c4: st.button("🔵 Easy", on_click=rate, args=(5,), use_container_width=True)

    else:
        st.success("Session Complete!")
        if st.button("Back to Menu"):
            st.session_state["study_index"] = 0
            st.rerun()

def section_library():
    st.header("📚 Library & Export")
    
    with get_db_connection() as conn:
        decks = conn.execute("SELECT * FROM decks").fetchall()
    
    if not decks:
        st.info("No decks found.")
        return

    # Deck Stats
    data = []
    for d in decks:
        with get_db_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM cards WHERE deck_id=?", (d['id'],)).fetchone()[0]
            data.append({"Deck": d['name'], "Cards": cnt, "Created": d['created_at']})
    
    st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)

    st.divider()
    c1, c2 = st.columns(2)
    
    with c1:
        st.subheader("🗑️ Delete")
        to_del = st.selectbox("Select Deck to Delete", [d['name'] for d in decks])
        if st.button("Delete Permanently", type="primary"):
            with get_db_connection() as conn:
                conn.execute("DELETE FROM decks WHERE name=?", (to_del,))
                conn.commit()
            st.rerun()

    with c2:
        st.subheader("📥 Export (Anki Compatible)")
        to_exp = st.selectbox("Select Deck to Export", [d['name'] for d in decks])
        if st.button("Generate CSV"):
            with get_db_connection() as conn:
                deck_id = conn.execute("SELECT id FROM decks WHERE name=?", (to_exp,)).fetchone()[0]
                cards = conn.execute("SELECT front, back, tag FROM cards WHERE deck_id=?", (deck_id,)).fetchall()
            
            # Create CSV string
            csv_buffer = io.StringIO()
            # Anki format: Front, Back, Tag (no header usually preferred, or specific header)
            # We will use standard CSV with header for broad compatibility
            writer = pd.DataFrame(cards, columns=["Front", "Back", "Tag"])
            writer.to_csv(csv_buffer, index=False)
            
            st.download_button(
                label="Download CSV",
                data=csv_buffer.getvalue(),
                file_name=f"{to_exp}_flashcards.csv",
                mime="text/csv"
            )

def section_settings():
    st.header("⚙️ Settings")
    st.info("API Key is saved locally to 'flashcard_settings.json'")
    
    new_key = st.text_input("Google Gemini API Key", value=st.session_state.get("api_key", ""), type="password")
    
    if st.button("Save API Key"):
        save_settings("api_key", new_key)
        st.session_state["api_key"] = new_key
        st.success("API Key Saved!")

# ====================== 7. MAIN APP LOOP ======================
def main():
    # --- Sidebar Configuration ---
    st.sidebar.title("🧠 Flashcard Pro")
    
    # 1. API Key Logic (Always visible if missing)
    api_key = st.session_state.get("api_key", "")
    
    if not api_key:
        st.sidebar.warning("⚠️ API Key Missing")
        entered_key = st.sidebar.text_input("Enter Gemini API Key", type="password", key="sidebar_api_input")
        if st.sidebar.button("Save Key"):
            save_settings("api_key", entered_key)
            st.session_state["api_key"] = entered_key
            st.sidebar.success("Saved! Reloading...")
            time.sleep(1)
            st.rerun()
    else:
        st.sidebar.success("🔑 API Key Active")
        if st.sidebar.button("Clear Key", type="secondary"):
            save_settings("api_key", "")
            st.session_state["api_key"] = ""
            st.rerun()

    st.sidebar.divider()

    # 2. Navigation
    # Default to Generator if no cards exist, otherwise Study Mode
    if "menu_selection" not in st.session_state:
        st.session_state["menu_selection"] = "Generator"

    menu = st.sidebar.radio(
        "Navigation", 
        ["Study Mode", "Generator", "Library", "Settings"],
        index=1 # Defaults to Generator for first run
    )
    
    # 3. Page Routing
    if menu == "Study Mode":
        section_study_mode()
    elif menu == "Generator":
        section_generator()
    elif menu == "Library":
        section_library()
    elif menu == "Settings":
        section_settings()

if __name__ == "__main__":
    main()
