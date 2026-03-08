import streamlit as st
import pandas as pd
import sqlite3
import re
import requests
import math
import json
import time
import altair as alt
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional
from PIL import Image
import io

# ====================== 1. SAFE IMPORTS & CONFIGURATION ======================
st.set_page_config(
    page_title="Flashcard Library Pro v3.3", 
    page_icon="🧠", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# Optional Dependencies
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
    "study_queue": [],      
    "study_index": 0,       
    "show_answer": False,
    "session_stats": {"reviewed": 0, "correct": 0},
    "cram_mode": False,
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ====================== 2. DATABASE ENGINE (WAL Mode) ======================
DB_NAME = "flashcards_v3.db"

def get_db_connection():
    """Returns a connection with row_factory set to sqlite3.Row"""
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize DB with WAL mode."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        
        c.execute('''CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            name TEXT UNIQUE, 
            description TEXT,
            created_at TEXT
        )''')
        
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
            FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE
        )''')
        
        # Migrations for existing DBs
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

# ====================== 3. CORE LOGIC: SPACED REPETITION (SM-2) ======================
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

            next_review = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")
            last_reviewed = datetime.now().strftime("%Y-%m-%d")

            c.execute('''UPDATE cards 
                         SET ease_factor=?, interval=?, repetitions=?, next_review=?, last_reviewed=? 
                         WHERE id=?''', 
                      (ease, interval, reps, next_review, last_reviewed, card_id))
            conn.commit()

# ====================== 4. AI CONTENT ENGINE ======================
class Flashcard(BaseModel):
    front: str = Field(description="The question/concept. Plain text.")
    back: str = Field(description="The answer. Use HTML <b> for key terms.")
    explanation: str = Field(description="A short context or mnemonic explaining WHY the answer is correct.")
    tag: str = Field(description="A short category tag (e.g., 'History', 'Formula').")

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

def clean_text(text):
    if not text: return ""
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text) 
    return text.strip()

def sanitize_json(text):
    text = re.sub(r'^```json', '', text, flags=re.MULTILINE)
    text = re.sub(r'^```', '', text, flags=re.MULTILINE)
    return text.strip()

def generate_flashcards(api_key, text_content, image_content, difficulty, count_val):
    client = genai.Client(api_key=api_key)
    
    system_prompt = f"""
    Act as a professor for {difficulty} level students.
    Create {count_val} flashcards based strictly on the user input.
    
    RULES:
    1. Output JSON only.
    2. 'back' field MUST use <b>bold</b> tags for keywords.
    3. 'explanation' field should provide context or a mnemonic.
    """
    
    contents = []
    if text_content:
        contents.append(text_content)
    if image_content:
        contents.append(image_content)
        
    try:
        # STRICT MODEL CONSTRAINT: gemini-2.5-flash-lite
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
        clean_json = sanitize_json(response.text)
        return json.loads(clean_json).get("cards", [])
    except Exception as e:
        st.error(f"AI Generation Failed: {e}")
        return []

# ====================== 5. UI COMPONENTS & CSS ======================
def inject_custom_css():
    st.markdown("""
    <style>
        .flashcard {
            background-color: #ffffff;
            border: 1px solid #e0e0e0;
            border-radius: 15px;
            padding: 40px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.05);
            min-height: 350px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
        }
        .stApp[data-theme='dark'] .flashcard {
            background-color: #262730;
            border-color: #444;
        }
        .card-front { font-size: 26px; font-weight: 700; margin-bottom: 20px; }
        .card-back { font-size: 22px; margin-bottom: 15px; color: #009688; }
        .card-explanation { font-size: 16px; color: #666; font-style: italic; border-top: 1px solid #eee; padding-top: 10px; width: 100%;}
        .stApp[data-theme='dark'] .card-explanation { color: #aaa; border-top: 1px solid #444;}
        .card-tag { 
            background: #f0f2f6; color: #555; padding: 4px 10px; 
            border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase;
            margin-bottom: 15px;
        }
        .stApp[data-theme='dark'] .card-tag { background: #333; color: #aaa; }
    </style>
    """, unsafe_allow_html=True)

# ====================== 6. APPLICATION SECTIONS ======================

def section_generator(api_key):
    st.header("🏭 Flashcard Factory")
    
    col_input, col_sets = st.columns([2, 1])
    
    with col_input:
        source_type = st.radio("Input Source", ["Text/Paste", "Upload PDF", "Image Analysis", "YouTube URL", "Web Article"], horizontal=True)
        
        content_text = ""
        image_content = None
        
        if source_type == "Text/Paste":
            content_text = st.text_area("Paste Notes Here", height=200)
            
        elif source_type == "Image Analysis":
            img_file = st.file_uploader("Upload Diagram/Chart", type=["png", "jpg", "jpeg"])
            if img_file:
                image_content = Image.open(img_file)
                st.image(image_content, caption="Image for Analysis", width=300)
                content_text = "Generate flashcards based on the visual information in this image."

        elif source_type == "YouTube URL":
            if YOUTUBE_AVAILABLE:
                url = st.text_input("Video URL")
                if url:
                    with st.spinner("Transcribing..."):
                        try:
                            video_id = url.split("v=")[1].split("&")[0]
                            transcript = YouTubeTranscriptApi.get_transcript(video_id)
                            content_text = " ".join([t['text'] for t in transcript])
                            st.success(f"Transcript Loaded ({len(content_text)} chars)")
                        except Exception as e:
                            st.error(f"Error: {e}")
            else:
                st.warning("Install youtube-transcript-api to use this.")

        elif source_type == "Web Article":
            if BS4_AVAILABLE:
                url = st.text_input("Article URL")
                if url:
                    try:
                        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                        soup = BeautifulSoup(resp.content, 'html.parser')
                        for s in soup(["script", "style"]): s.decompose()
                        content_text = " ".join(soup.stripped_strings)[:20000]
                        st.success("Web Content Loaded")
                    except Exception as e:
                        st.error(f"Error: {e}")
            else:
                st.warning("Install beautifulsoup4 to use this.")

    with col_sets:
        st.subheader("Config")
        deck_name = st.text_input("Deck Name", placeholder="e.g., Biology 101")
        difficulty = st.select_slider("Level", ["Beginner", "Intermediate", "Expert"], value="Intermediate")
        qty = st.number_input("Count", 1, 30, 10)
        
        if st.button("🚀 Generate", type="primary", use_container_width=True):
            if not api_key:
                st.error("API Key Missing")
                return
            if not (content_text or image_content):
                st.warning("No content provided")
                return
            
            with st.spinner("Gemini is thinking..."):
                cards = generate_flashcards(api_key, content_text, image_content, difficulty, qty)
                
                if cards and deck_name:
                    with get_db_connection() as conn:
                        c = conn.cursor()
                        c.execute("INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)", 
                                  (deck_name, datetime.now().strftime("%Y-%m-%d")))
                        c.execute("SELECT id FROM decks WHERE name=?", (deck_name,))
                        deck_id = c.fetchone()[0]
                        
                        data = [(deck_id, clean_text(c['front']), clean_text(c['back']), c.get('explanation', ''), c['tag']) for c in cards]
                        c.executemany("INSERT INTO cards (deck_id, front, back, explanation, tag) VALUES (?, ?, ?, ?, ?)", data)
                        conn.commit()
                    st.toast(f"Created {len(cards)} cards!", icon="✅")

def section_study():
    st.header("🧘 Zen Study Mode")
    
    with get_db_connection() as conn:
        decks = conn.execute("SELECT id, name FROM decks").fetchall()
    
    if not decks:
        st.info("No decks found.")
        return

    col_deck, col_mode = st.columns([3, 1])
    with col_deck:
        deck_opts = {d['name']: d['id'] for d in decks}
        selected_deck = st.selectbox("Select Deck", list(deck_opts.keys()))
        deck_id = deck_opts[selected_deck]
    
    with col_mode:
        st.write("") # Spacer
        cram = st.toggle("🔥 Cram Mode", value=st.session_state["cram_mode"], help="Ignore schedule, shuffle all cards.")
        if cram != st.session_state["cram_mode"]:
            st.session_state["cram_mode"] = cram
            st.session_state["current_deck_id"] = None # Force reload
            st.rerun()

    # Load Queue
    if st.session_state["current_deck_id"] != deck_id:
        st.session_state["current_deck_id"] = deck_id
        
        with get_db_connection() as conn:
            if st.session_state["cram_mode"]:
                q = "SELECT * FROM cards WHERE deck_id = ? ORDER BY RANDOM() LIMIT 50"
                cards = conn.execute(q, (deck_id,)).fetchall()
            else:
                today = datetime.now().strftime("%Y-%m-%d")
                q = "SELECT * FROM cards WHERE deck_id = ? AND next_review <= ? ORDER BY next_review ASC LIMIT 50"
                cards = conn.execute(q, (deck_id, today)).fetchall()
            
            st.session_state["study_queue"] = [dict(c) for c in cards]
            st.session_state["study_index"] = 0
            st.session_state["show_answer"] = False
            st.session_state["session_stats"] = {"reviewed": 0, "correct": 0}

    queue = st.session_state["study_queue"]
    idx = st.session_state["study_index"]

    if not queue:
        st.success("🎉 All caught up!" if not st.session_state["cram_mode"] else "Cram session complete!")
        return

    if idx < len(queue):
        card = queue[idx]
        st.progress((idx + 1) / len(queue), text=f"Card {idx+1}/{len(queue)}")
        
        # HTML Content Preparation
        back_content = card['back'] if st.session_state["show_answer"] else "<span style='color:#ccc; font-style:italic'>(Think...)</span>"
        explanation_html = ""
        if st.session_state["show_answer"] and card.get("explanation"):
            explanation_html = f'<div class="card-explanation">💡 {card["explanation"]}</div>'

        with st.container():
            # FIXED: HTML Rendering Structure
            html_code = f"""
            <div class="flashcard">
                <div class="card-tag">{card['tag']}</div>
                <div class="card-front">{card['front']}</div>
                <div class="card-back">{back_content}</div>
                {explanation_html}
            </div>
            """
            st.markdown(html_code, unsafe_allow_html=True)

        st.write("") 
        
        if not st.session_state["show_answer"]:
            if st.button("👁️ Show Answer", type="primary", use_container_width=True):
                st.session_state["show_answer"] = True
                st.rerun()
        else:
            cols = st.columns(4)
            labels = ["Again (Fail)", "Hard", "Good", "Easy"]
            scores = [0, 3, 4, 5]
            
            def submit_review(score):
                if not st.session_state["cram_mode"]:
                    update_card_sm2(card['id'], score)
                
                st.session_state["session_stats"]["reviewed"] += 1
                if score >= 3: st.session_state["session_stats"]["correct"] += 1
                st.session_state["study_index"] += 1
                st.session_state["show_answer"] = False
                st.rerun()

            for i, col in enumerate(cols):
                if col.button(labels[i], use_container_width=True):
                    submit_review(scores[i])

    else:
        stats = st.session_state["session_stats"]
        acc = int((stats["correct"]/stats["reviewed"]*100)) if stats["reviewed"] else 0
        st.metric("Session Accuracy", f"{acc}%", f"{stats['reviewed']} Cards")
        if st.button("Start Over"):
            st.session_state["current_deck_id"] = None
            st.rerun()

def section_library():
    st.header("📚 Library & Exports")
    
    with get_db_connection() as conn:
        df_cards = pd.read_sql("SELECT id, deck_id, ease_factor, repetitions, last_reviewed FROM cards", conn)
        decks = pd.read_sql("SELECT id, name, created_at FROM decks", conn)
    
    if decks.empty:
        st.info("No decks found.")
        return

    # 1. Deck Stats
    if not df_cards.empty:
        st.subheader("Deck Health")
        
        # CRITICAL FIX: Explicit suffixes to prevent KeyErrors
        df_merged = pd.merge(df_cards, decks, left_on="deck_id", right_on="id", suffixes=('_card', '_deck'))
        
        deck_stats = df_merged.groupby("name").agg({
            "repetitions": "mean",
            "ease_factor": "mean",
            "id_card": "count" # Use specific suffix column
        }).rename(columns={"id_card": "Total Cards", "repetitions": "Avg Reps", "ease_factor": "Avg Ease"})
        
        st.dataframe(deck_stats, use_container_width=True)

    # 2. Anki Export Section
    st.divider()
    st.subheader("📤 Export to Anki")
    col_exp, col_info = st.columns([1, 2])
    
    with col_exp:
        export_deck_name = st.selectbox("Select Deck to Export", decks['name'].tolist())
        
        if export_deck_name:
            # Fetch Cards for export
            deck_id = decks[decks['name'] == export_deck_name].iloc[0]['id']
            with get_db_connection() as conn:
                cards_df = pd.read_sql("SELECT front, back, tag FROM cards WHERE deck_id=?", conn, params=(deck_id,))
            
            # Convert to CSV
            csv = cards_df.to_csv(index=False, header=False).encode('utf-8')
            
            st.download_button(
                label="Download Anki CSV",
                data=csv,
                file_name=f"{export_deck_name}_anki.csv",
                mime="text/csv",
                help="Import this file into Anki. It contains Front, Back, and Tags."
            )

    with col_info:
        st.info("""
        **How to Import into Anki:**
        1. Download the CSV.
        2. Open Anki -> File -> Import.
        3. Select the file.
        4. Ensure 'Field Separator' is Comma.
        5. Map fields: Field 1 -> Front, Field 2 -> Back, Field 3 -> Tags.
        """)

    # 3. Heatmap
    if not df_cards.empty and df_cards['last_reviewed'].notna().any():
        st.divider()
        st.subheader("Study Activity")
        df_cards['last_reviewed'] = pd.to_datetime(df_cards['last_reviewed']).dt.date
        activity = df_cards['last_reviewed'].value_counts().reset_index()
        activity.columns = ['date', 'count']
        
        chart = alt.Chart(activity).mark_rect().encode(
            x=alt.X('date:O', title="Date"),
            y=alt.Y('count:Q', title="Cards Reviewed"),
            color=alt.Color('count', scale=alt.Scale(scheme='greens'))
        ).properties(height=200)
        st.altair_chart(chart, use_container_width=True)

# ====================== 7. MAIN NAVIGATION ======================
def main():
    inject_custom_css()
    
    with st.sidebar:
        st.title("🧠 Flashcard Pro")
        api_key = st.text_input("Gemini API Key", type="password")
        if not api_key:
            st.warning("Enter API Key to generate.")
            
        st.divider()
        page = st.radio("Navigation", ["Study Mode", "Generator", "Library & Exports"], label_visibility="collapsed")
        
        st.caption("v3.3 - Stable")

    if page == "Generator":
        section_generator(api_key)
    elif page == "Study Mode":
        section_study()
    elif page == "Library & Exports":
        section_library()

if __name__ == "__main__":
    main()
                            
