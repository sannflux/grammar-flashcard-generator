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
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional

# ====================== 1. SAFE IMPORTS & CONFIGURATION ======================
st.set_page_config(
    page_title="Flashcard Library Pro v2.1", 
    page_icon="🧠", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# Optional Dependencies (Graceful Degradation)
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

# Initialize Session State
DEFAULT_STATE = {
    "current_deck_id": None,
    "study_queue": [],      # List of cards due for review
    "study_index": 0,       # Current position in queue
    "show_answer": False,
    "session_stats": {"reviewed": 0, "correct": 0},
    "processing": False,    # For UI locking during generation
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ====================== 2. DATABASE ENGINE (Transactional & SM-2 Ready) ======================
DB_NAME = "flashcards_v2.db"

def get_db_connection():
    """Returns a connection with row_factory set to sqlite3.Row"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the DB with Spaced Repetition fields."""
    with get_db_connection() as conn:
        c = conn.cursor()
        
        # Decks Table
        c.execute('''CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            name TEXT UNIQUE, 
            description TEXT,
            created_at TEXT
        )''')
        
        # Cards Table with SM-2 Algorithm Fields
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

# Initialize on load
init_db()

# ====================== 3. CORE LOGIC: SPACED REPETITION (SM-2) ======================
def update_card_sm2(card_id, quality):
    """
    Updates a card's schedule based on the SuperMemo-2 Algorithm.
    Quality: 0 (Again/Fail), 3 (Hard), 4 (Good), 5 (Easy)
    """
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT ease_factor, interval, repetitions FROM cards WHERE id=?", (card_id,))
        row = c.fetchone()
        
        if row:
            ease, interval, reps = row['ease_factor'], row['interval'], row['repetitions']
            
            if quality < 3:
                # If user failed, reset repetitions and interval
                reps = 0
                interval = 1
            else:
                # Success path
                if reps == 0:
                    interval = 1
                elif reps == 1:
                    interval = 6
                else:
                    interval = math.ceil(interval * ease)
                
                reps += 1
                # Adjust Ease Factor (standard SM-2 formula)
                ease = ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
                if ease < 1.3: ease = 1.3 # Minimum ease cap

            # Calculate new review date
            next_review_date = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")

            c.execute('''UPDATE cards 
                         SET ease_factor=?, interval=?, repetitions=?, next_review=? 
                         WHERE id=?''', 
                      (ease, interval, reps, next_review_date, card_id))
            conn.commit()

# ====================== 4. CONTENT INGESTION ENGINE ======================
class Flashcard(BaseModel):
    front: str = Field(description="The question/concept. Plain text.")
    back: str = Field(description="The answer. Use HTML <b> for key terms. No Markdown.")
    tag: str = Field(description="A short category tag (e.g., 'History', 'Formula').")

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

def clean_text(text):
    """Sanitize and standardize text."""
    if not text: return ""
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text) # MD Bold -> HTML
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)     # MD Italic -> HTML
    return text.strip()

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_youtube_transcript(url):
    if not YOUTUBE_AVAILABLE:
        return "Error: library 'youtube-transcript-api' not installed."
    try:
        video_id = url.split("v=")[1].split("&")[0]
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        full_text = " ".join([t['text'] for t in transcript])
        return full_text
    except Exception as e:
        return f"Error: Could not retrieve transcript. {str(e)}"

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_web_content(url):
    if not BS4_AVAILABLE:
        return "Error: library 'beautifulsoup4' not installed."
    
    # 1. Impersonate a real browser (Chrome on Windows 10)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/"
    }
    
    try:
        # 2. Add a Timeout (10 seconds max)
        response = requests.get(url, headers=headers, timeout=10)
        
        # 3. Check for errors (403 Forbidden, 404 Not Found, etc.)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 4. Remove junk elements
        for script in soup(["script", "style", "nav", "footer", "header", "aside"]):
            script.decompose()
            
        text = " ".join(soup.stripped_strings)
        
        # 5. Limit text length (to prevent Gemini context errors)
        return text[:20000] 

    except requests.exceptions.Timeout:
        return "Error: The website took too long to respond. It might be blocking scrapers."
    except requests.exceptions.HTTPError as e:
        return f"Error: The website refused the connection ({e}). Try a different site."
    except Exception as e:
        return f"Error: Could not scrape website. {str(e)}"

def generate_flashcards(api_key, content_text, difficulty, count_val):
    client = genai.Client(api_key=api_key)
    
    system_prompt = f"""
    Act as a professor for {difficulty} level students.
    Create {count_val} flashcards based strictly on the user text.
    
    RULES:
    1. Output JSON only.
    2. 'back' field MUST use <b>bold</b> tags for keywords.
    3. Keep questions concise and answers definitive.
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=[content_text],
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

# ====================== 5. UI COMPONENTS & CSS ======================
def inject_custom_css():
    st.markdown("""
    <style>
        /* Card Container */
        .flashcard {
            background-color: #ffffff;
            border: 1px solid #e0e0e0;
            border-radius: 15px;
            padding: 40px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.05);
            min-height: 300px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
            transition: transform 0.2s;
        }
        .stApp[data-theme='dark'] .flashcard {
            background-color: #262730;
            border-color: #444;
        }
        
        /* Typography */
        .card-front { font-size: 24px; font-weight: 700; color: #1f1f1f; margin-bottom: 20px; }
        .stApp[data-theme='dark'] .card-front { color: #ffffff; }
        
        .card-back { font-size: 20px; line-height: 1.6; color: #424242; }
        .stApp[data-theme='dark'] .card-back { color: #e0e0e0; }
        
        .card-tag { 
            background: #f0f2f6; color: #555; padding: 4px 10px; 
            border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase;
            margin-bottom: 15px;
        }
        .stApp[data-theme='dark'] .card-tag { background: #333; color: #aaa; }
        
        /* Buttons */
        .stButton>button { border-radius: 8px; font-weight: 600; }
        
        /* Hide Streamlit Branding */
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
    </style>
    """, unsafe_allow_html=True)

# ====================== 6. APPLICATION SECTIONS ======================

def section_generator(api_key):
    st.header("🏭 Flashcard Factory")
    
    # 1. Source Selection
    source_type = st.radio("Input Source", ["Text/Paste", "Upload PDF", "YouTube URL", "Web Article"], horizontal=True)
    
    content_text = ""
    
    if source_type == "Text/Paste":
        content_text = st.text_area("Paste Notes Here", height=200)
    
    elif source_type == "Upload PDF":
        uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])
        if uploaded_file:
            st.info("💡 Pro Tip: For direct PDF analysis, ensure your API key supports Gemini 1.5 Pro.")
            # Simplified text extraction placeholder
            content_text = "Extract relevant concepts from this document."
            
    elif source_type == "YouTube URL":
        if not YOUTUBE_AVAILABLE:
            st.warning("⚠️ 'youtube-transcript-api' is missing. Please install it to use this feature.")
        else:
            url = st.text_input("Video URL")
            if url:
                with st.spinner("Transcribing Video..."):
                    content_text = fetch_youtube_transcript(url)
                    if "Error" not in content_text:
                        st.success(f"Transcript loaded ({len(content_text)} chars)")
                    else:
                        st.error(content_text)
                    
    elif source_type == "Web Article":
        if not BS4_AVAILABLE:
            st.warning("⚠️ 'beautifulsoup4' is missing. Please install it to use this feature.")
        else:
            url = st.text_input("Article URL")
            if url:
                with st.spinner("Scraping Article..."):
                    content_text = fetch_web_content(url)
                    if "Error" not in content_text:
                        st.success(f"Content loaded ({len(content_text)} chars)")
                    else:
                        st.error(content_text)

    # 2. Settings
    col1, col2, col3 = st.columns(3)
    with col1:
        deck_name = st.text_input("Target Deck Name")
    with col2:
        difficulty = st.select_slider("Difficulty", ["Beginner", "Intermediate", "Expert"], value="Intermediate")
    with col3:
        qty = st.number_input("Card Count", min_value=1, max_value=50, value=10)

    # 3. Generate
    if st.button("🚀 Generate Cards", type="primary", use_container_width=True):
        if not api_key:
            st.error("Please provide an API Key in the sidebar.")
            return

        if not content_text or "Error" in content_text:
            st.warning("Please provide valid content.")
            return

        with st.spinner("AI is crafting your study materials..."):
            cards = generate_flashcards(api_key, content_text, difficulty, qty)
            
            if cards and deck_name:
                # Save to DB
                with get_db_connection() as conn:
                    c = conn.cursor()
                    # Ensure Deck Exists
                    c.execute("INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)", 
                              (deck_name, datetime.now().strftime("%Y-%m-%d")))
                    c.execute("SELECT id FROM decks WHERE name=?", (deck_name,))
                    deck_id = c.fetchone()[0]
                    
                    # Insert Cards
                    data = [(deck_id, clean_text(c['front']), clean_text(c['back']), c['tag']) for c in cards]
                    c.executemany("INSERT INTO cards (deck_id, front, back, tag) VALUES (?, ?, ?, ?)", data)
                    conn.commit()
                
                st.toast(f"🎉 Successfully created {len(cards)} cards in '{deck_name}'!", icon="✅")
                time.sleep(1)
                st.rerun()

def section_study_mode():
    st.header("🧘 Zen Study Mode")
    
    # 1. Deck Selector
    with get_db_connection() as conn:
        decks = conn.execute("SELECT id, name FROM decks").fetchall()
    
    if not decks:
        st.info("Your library is empty. Go to the Generator to create a deck.")
        return

    deck_opts = {d['name']: d['id'] for d in decks}
    selected_deck_name = st.selectbox("Select Deck", list(deck_opts.keys()))

    deck_id = deck_opts[selected_deck_name]
    
    # 2. Load Queue (Only load if deck changed or queue empty)
    if st.session_state["current_deck_id"] != deck_id:
        st.session_state["current_deck_id"] = deck_id
        # Logic: Fetch cards due today OR new cards
        today = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            # Prioritize: Overdue > Due Today > New
            q = """
            SELECT * FROM cards 
            WHERE deck_id = ? AND next_review <= ?
            ORDER BY next_review ASC, id ASC
            LIMIT 50
            """
            cards = conn.execute(q, (deck_id, today)).fetchall()
            st.session_state["study_queue"] = [dict(c) for c in cards] # Convert rows to dicts
            st.session_state["study_index"] = 0
            st.session_state["show_answer"] = False
            st.session_state["session_stats"] = {"reviewed": 0, "correct": 0}

    queue = st.session_state["study_queue"]
    idx = st.session_state["study_index"]

    # 3. Empty State
    if not queue:
        st.success("🎉 You are all caught up on this deck for today!")
        st.balloons()
        if st.button("Study Ahead (Review All Cards)", type="secondary"):
             with get_db_connection() as conn:
                cards = conn.execute("SELECT * FROM cards WHERE deck_id = ?", (deck_id,)).fetchall()
                st.session_state["study_queue"] = [dict(c) for c in cards]
                st.rerun()
        return

    # 4. Display Card
    if idx < len(queue):
        card = queue[idx]
        
        # Progress Bar
        progress = (idx) / len(queue)
        st.progress(progress, text=f"Card {idx+1} of {len(queue)}")

        # Card Container
        with st.container():
            st.markdown(f"""
            <div class="flashcard">
                <div class="card-tag">{card['tag']}</div>
                <div class="card-front">{card['front']}</div>
                {"<hr style='opacity:0.2; width:80%'>" if st.session_state["show_answer"] else ""}
                <div class="card-back">
                    {card['back'] if st.session_state["show_answer"] else "<span style='color:#ccc; font-style:italic'>(Think about the answer...)</span>"}
                </div>
            </div>
            """, unsafe_allow_html=True)

        st.write("") # Spacer

        # Interaction Controls
        if not st.session_state["show_answer"]:
            col_rev, col_skip = st.columns([4, 1])
            with col_rev:
                if st.button("👁️ Show Answer (Spacebar)", type="primary", use_container_width=True):
                    st.session_state["show_answer"] = True
                    st.rerun()
        else:
            st.write("### How well did you know this?")
            c1, c2, c3, c4 = st.columns(4)
            
            def handle_rating(quality):
                update_card_sm2(card['id'], quality)
                st.session_state["session_stats"]["reviewed"] += 1
                if quality >= 3:
                    st.session_state["session_stats"]["correct"] += 1
                
                # Advance Queue
                st.session_state["study_index"] += 1
                st.session_state["show_answer"] = False
                st.rerun()

            with c1: 
                if st.button("🔴 Again", use_container_width=True): handle_rating(0)
            with c2: 
                if st.button("🟠 Hard", use_container_width=True): handle_rating(3)
            with c3: 
                if st.button("🟢 Good", use_container_width=True): handle_rating(4)
            with c4: 
                if st.button("🔵 Easy", use_container_width=True): handle_rating(5)
    else:
        # End of session
        st.success("Session Complete!")
        stats = st.session_state["session_stats"]
        acc = int((stats["correct"] / stats["reviewed"] * 100)) if stats["reviewed"] > 0 else 0
        st.metric("Retention Rate", f"{acc}%", f"{stats['reviewed']} Cards Reviewed")
        if st.button("Start Over"):
            st.session_state["study_index"] = 0
            st.rerun()

def section_library():
    st.header("📚 Library Management")
    
    with get_db_connection() as conn:
        decks = conn.execute("SELECT * FROM decks ORDER BY created_at DESC").fetchall()
    
    if not decks:
        st.info("No decks found.")
        return

    # Deck Statistics
    deck_data = []
    for d in decks:
        with get_db_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM cards WHERE deck_id=?", (d['id'],)).fetchone()[0]
            due = conn.execute("SELECT COUNT(*) FROM cards WHERE deck_id=? AND next_review <= ?", 
                               (d['id'], datetime.now().strftime("%Y-%m-%d"))).fetchone()[0]
            deck_data.append({
                "ID": d['id'],
                "Deck Name": d['name'], 
                "Total Cards": count, 
                "Due Today": due,
                "Created": d['created_at']
            })
    
    df = pd.DataFrame(deck_data)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Deck Operations
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("🗑️ Delete Deck")
        del_deck = st.selectbox("Choose deck to delete", [d['name'] for d in decks])
        if st.button("Delete Permanently"):
            with get_db_connection() as conn:
                conn.execute("DELETE FROM decks WHERE name=?", (del_deck,))
                conn.commit()
            st.toast(f"Deleted {del_deck}")
            time.sleep(1)
            st.rerun()
            
    with c2:
        st.subheader("📥 Export to CSV")
        exp_deck = st.selectbox("Choose deck to export", [d['name'] for d in decks])
        if st.button("Generate CSV"):
             with get_db_connection() as conn:
                deck_id = conn.execute("SELECT id FROM decks WHERE name=?", (exp_deck,)).fetchone()[0]
                cards = conn.execute("SELECT front, back, tag FROM cards WHERE deck_id=?", (deck_id,)).fetchall()
                
                # Format for Anki Import
                # Anki expects HTML in fields, so our 'back' field is already compatible
                csv_data = pd.DataFrame([dict(c) for c in cards]).to_csv(index=False, header=False, sep="\t")
                st.download_button("Download for Anki (.txt)", csv_data, f"{exp_deck}_anki.txt", "text/csv")
                st.info("Import this into Anki using 'Tab-separated' settings. Allow HTML in fields.")

# ====================== 7. MAIN APP LAYOUT ======================
inject_custom_css()

with st.sidebar:
    st.title("🧠 Flashcard Pro")
    
    # Navigation
    nav = st.radio("Navigation", ["Study Mode", "Generator", "Library"], index=0)
    
    st.divider()
    
    # API Key Management
    api_key = st.text_input("Gemini API Key", type="password", value=os.environ.get("GEMINI_API_KEY", ""))
    if not api_key:
        st.warning("API Key required for generation.")
        
    st.divider()
    st.caption(f"v2.1.0 | DB: {DB_NAME}")

# Routing
if nav == "Study Mode":
    section_study_mode()
elif nav == "Generator":
    section_generator(api_key)
elif nav == "Library":
    section_library()
