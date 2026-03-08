import streamlit as st
import pandas as pd
import tempfile
import os
import time
import json
import sqlite3
import hashlib
import re
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List

# ====================== 1. CONFIGURATION & STATE ======================
st.set_page_config(
    page_title="Flashcard Library Pro", 
    page_icon="🧠", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize Session State
if "flashcards" not in st.session_state:
    st.session_state["flashcards"] = [] 
if "card_index" not in st.session_state:
    st.session_state["card_index"] = 0
if "show_answer" not in st.session_state:
    st.session_state["show_answer"] = False
if "current_deck_name" not in st.session_state:
    st.session_state["current_deck_name"] = ""

# ====================== 2. DATABASE ENGINE (Transactional SQLite) ======================
DB_NAME = "flashcards.db"

def init_db():
    """Initialize the local SQLite database."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS decks
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  name TEXT UNIQUE, 
                  created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cards
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  deck_id INTEGER, 
                  front TEXT, 
                  back TEXT, 
                  tag TEXT,
                  FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE)''')
    conn.commit()
    conn.close()

def save_deck_atomic(deck_name, cards):
    """
    Saves cards using an Atomic Transaction.
    This ensures that we never end up with a half-saved deck.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        # Start Transaction
        c.execute("BEGIN TRANSACTION")
        
        # 1. Upsert Deck Name
        c.execute("INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)", 
                  (deck_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        
        # 2. Get Deck ID
        c.execute("SELECT id FROM decks WHERE name=?", (deck_name,))
        deck_id = c.fetchone()[0]
        
        # 3. Smart Update: Delete old cards for this deck -> Bulk Insert new ones
        # (This is faster and safer than row-by-row updates for local SQLite)
        c.execute("DELETE FROM cards WHERE deck_id=?", (deck_id,))
        
        data_to_insert = [(deck_id, card['front'], card['back'], card['tag']) for card in cards]
        c.executemany("INSERT INTO cards (deck_id, front, back, tag) VALUES (?, ?, ?, ?)", data_to_insert)
        
        # Commit Transaction
        conn.commit()
        return True
    except Exception as e:
        conn.rollback() # Undo changes if error occurs
        st.error(f"Database Save Error: {e}")
        return False
    finally:
        conn.close()

def delete_deck_from_db(deck_name):
    """Permanently removes a deck and its cards."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("PRAGMA foreign_keys = ON") # Ensure cascade delete works
        c.execute("DELETE FROM decks WHERE name=?", (deck_name,))
        conn.commit()
        return True
    except Exception as e:
        st.error(f"Delete Error: {e}")
        return False
    finally:
        conn.close()

def load_deck_from_db(deck_name):
    """Loads a deck into memory."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id FROM decks WHERE name=?", (deck_name,))
    result = c.fetchone()
    if result:
        deck_id = result[0]
        c.execute("SELECT front, back, tag FROM cards WHERE deck_id=?", (deck_id,))
        rows = c.fetchall()
        loaded_cards = [{"front": r[0], "back": r[1], "tag": r[2]} for r in rows]
        conn.close()
        return loaded_cards
    conn.close()
    return []

def get_all_deck_names():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT name FROM decks ORDER BY created_at DESC")
    names = [row[0] for row in c.fetchall()]
    conn.close()
    return names

# Initialize DB
init_db()

# ====================== 3. PYDANTIC SCHEMAS ======================
class Flashcard(BaseModel):
    front: str = Field(description="The question. No markdown.")
    back: str = Field(description="The answer. Use HTML <b> and <i> tags. No markdown.")
    tag: str = Field(description="A short category tag.")

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

# ====================== 4. TECHNICAL HELPERS ======================
def clean_markdown(text):
    """
    Sanitizes LLM output but PRESERVES the Anki bolding system.
    **bold** -> <b>bold</b>
    """
    if not text: return ""
    
    # 1. Convert Markdown Bold to HTML Bold (Anki Style)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # 2. Convert Markdown Italic to HTML Italic
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    
    # 3. Scrub unwanted artifacts
    text = text.replace('`', '').replace('#', '')
    
    return text.strip()

# ====================== 5. CACHED AI ENGINE ======================
@st.cache_data(show_spinner=False)
def generate_flashcards_cached(api_key, file_content, file_type, text_input, difficulty, card_count):
    client = genai.Client(api_key=api_key)
    contents = []

    if file_content:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_type}") as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name
        
        gemini_file = client.files.upload(file=tmp_path)
        contents.append(gemini_file)
        os.unlink(tmp_path)
        if file_type == "pdf": time.sleep(2)
    
    if text_input:
        contents.append(text_input)

    count_instruction = f"Generate {card_count} cards." if isinstance(card_count, int) else "Generate a comprehensive set."
    
    # Reinforced System Prompt for HTML Bolding
    system_prompt = f"""
    You are an expert tutor for {difficulty} students.
    Task: Create active-recall flashcards.
    
    CRITICAL FORMATTING RULES:
    1. **NO MARKDOWN:** Do not use markdown syntax output.
    2. **USE HTML TAGS:** For the 'back' (Answer) field, you MUST use <b>text</b> for bold keywords and <i>text</i> for emphasis.
    3. **FRONT FIELD:** Plain text only.
    
    Output must be a valid JSON object.
    """
    
    contents.append(f"{count_instruction} Focus on key concepts.")

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
    
    if file_content and gemini_file:
        try: client.files.delete(name=gemini_file.name)
        except: pass

    return response.text

def simplify_card_eli5(api_key, front, back):
    client = genai.Client(api_key=api_key)
    prompt = f"""
    Rewrite this flashcard to be simpler (ELI5).
    Original Question: {front}
    Original Answer: {back}
    USE <b> tags for key terms. NO Markdown.
    Return JSON: {{"front": "...", "back": "..."}}
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(response.text)

# ====================== 6. CSS STYLING ======================
st.markdown("""
<style>
    .flashcard-container {
        background-color: #f9f9f9;
        border: 1px solid #ddd;
        border-radius: 10px;
        padding: 30px;
        min-height: 250px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        text-align: center;
    }
    .dark-mode .flashcard-container { background-color: #262730; border-color: #444; }
    .card-front { font-size: 22px; font-weight: 600; margin-bottom: 10px; }
    .card-back { font-size: 20px; color: #555; }
    .card-tag { 
        background-color: #e0e0e0; color: #333; 
        padding: 4px 8px; border-radius: 12px; font-size: 12px; margin-bottom: 15px; 
    }
</style>
""", unsafe_allow_html=True)

# ====================== 7. SIDEBAR: DECK MANAGEMENT ======================
with st.sidebar:
    st.header("🗂️ Deck Management")
    
    with st.expander("🔑 API Key", expanded=not os.environ.get("GEMINI_API_KEY")):
        api_key = st.text_input("Gemini API Key", type="password", value=os.environ.get("GEMINI_API_KEY", ""))

    st.subheader("Load & Merge")
    saved_decks = get_all_deck_names()
    
    # Deck Loading Logic
    selected_deck = st.selectbox("Active Deck", ["-- New Session --"] + saved_decks)
    
    if selected_deck != "-- New Session --":
        col_load, col_del = st.columns([3, 1])
        with col_load:
            if st.button("📂 Load", use_container_width=True):
                st.session_state["flashcards"] = load_deck_from_db(selected_deck)
                st.session_state["current_deck_name"] = selected_deck
                st.rerun()
        with col_del:
            if st.button("🗑️", help="Delete Deck"):
                if delete_deck_from_db(selected_deck):
                    st.success("Deleted!")
                    time.sleep(1)
                    st.rerun()

    # Deck Merging Logic (Technical Upgrade)
    if st.session_state["flashcards"] and len(saved_decks) > 0:
        with st.expander("🔗 Merge Decks"):
            merge_target = st.selectbox("Merge current cards into:", ["-- Select --"] + saved_decks)
            if merge_target != "-- Select --":
                if st.button("Merge Now"):
                    # Load target deck
                    target_cards = load_deck_from_db(merge_target)
                    # Append current session cards
                    combined = target_cards + st.session_state["flashcards"]
                    # Save atomically
                    if save_deck_atomic(merge_target, combined):
                        st.success(f"Merged successfully into '{merge_target}'!")
                        st.session_state["flashcards"] = combined
                        st.session_state["current_deck_name"] = merge_target
                        time.sleep(1)
                        st.rerun()

    st.divider()
    
    st.subheader("⚙️ Generator Settings")
    difficulty = st.select_slider("Level", ["High School", "Undergrad", "PhD"], value="Undergrad")
    card_count = st.selectbox("Quantity", ["Auto", 5, 10, 20])
    mode = st.radio("Mode", ["Append", "Replace"], index=1)

# ====================== 8. MAIN UI ======================
st.title("🧠 Flashcard Library Pro")

# --- Tabs ---
t1, t2 = st.tabs(["🆕 Create / Add", "💾 Database Operations"])

with t1:
    col_up, col_txt = st.columns(2)
    with col_up:
        uploaded_file = st.file_uploader("Upload PDF/Image", type=["pdf", "png", "jpg"])
    with col_txt:
        pasted_text = st.text_area("Or Paste Text", height=100)
    
    if st.button("🚀 Generate Cards", type="primary", use_container_width=True):
        if not api_key:
            st.error("API Key required.")
            st.stop()
            
        with st.spinner("AI is thinking (Cached)..."):
            try:
                # Prepare args
                file_bytes = uploaded_file.getvalue() if uploaded_file else None
                file_type = uploaded_file.type.split('/')[-1] if uploaded_file else None
                count_val = card_count if isinstance(card_count, int) else "Auto"
                
                # Execute Cached Gen
                json_str = generate_flashcards_cached(
                    api_key, file_bytes, file_type, pasted_text, difficulty, count_val
                )
                
                new_data = json.loads(json_str).get("cards", [])
                
                # Post-Process Cleaning (Preserving Bolding)
                cleaned_data = []
                for card in new_data:
                    cleaned_data.append({
                        "front": clean_markdown(card.get("front", "")),
                        "back": clean_markdown(card.get("back", "")),
                        "tag": card.get("tag", "")
                    })
                
                if mode == "Replace":
                    st.session_state["flashcards"] = cleaned_data
                    st.session_state["current_deck_name"] = "" 
                else:
                    st.session_state["flashcards"].extend(cleaned_data)
                
                st.success(f"Generated {len(cleaned_data)} cards!")
                time.sleep(0.5)
                st.rerun()
                
            except Exception as e:
                st.error(f"Error: {str(e)}")

with t2:
    if st.session_state["flashcards"]:
        col_name, col_save = st.columns([3, 1])
        with col_name:
            save_name = st.text_input("Deck Name", value=st.session_state["current_deck_name"])
        with col_save:
            st.write("") # Spacer
            st.write("")
            if st.button("💾 Save Deck", use_container_width=True):
                if save_name:
                    if save_deck_atomic(save_name, st.session_state["flashcards"]):
                        st.success(f"Deck '{save_name}' saved securely!")
                        st.session_state["current_deck_name"] = save_name
                        time.sleep(1)
                        st.rerun()
                else:
                    st.warning("Enter a name.")
    else:
        st.info("No cards in session to save.")

# ====================== 9. CARD PREVIEW & EDITOR ======================
if st.session_state["flashcards"]:
    st.divider()
    
    df = pd.DataFrame(st.session_state["flashcards"])
    
    tab_view, tab_edit = st.tabs(["👀 Focus Mode", "✏️ Editor"])

    # --- FOCUS MODE ---
    with tab_view:
        if not df.empty:
            c1, c2, c3 = st.columns([1, 4, 1])
            idx = st.session_state.card_index
            
            if idx >= len(df): idx = 0; st.session_state.card_index = 0
            
            with c1:
                if st.button("⬅️"):
                    st.session_state.card_index = max(0, idx - 1)
                    st.session_state.show_answer = False
                    st.rerun()
            with c3:
                if st.button("➡️"):
                    st.session_state.card_index = min(len(df) - 1, idx + 1)
                    st.session_state.show_answer = False
                    st.rerun()
            
            current = df.iloc[idx]
            
            with c2:
                st.progress((idx + 1) / len(df))
                
                with st.container(border=True):
                    st.markdown(f"<div style='text-align:center'><span class='card-tag'>{current['tag']}</span></div>", unsafe_allow_html=True)
                    st.markdown(f"<div class='card-front'>{current['front']}</div>", unsafe_allow_html=True)
                    st.markdown("---")
                    
                    if st.session_state.show_answer:
                        st.markdown(f"<div class='card-back'>{current['back']}</div>", unsafe_allow_html=True)
                    else:
                        st.markdown("<div style='text-align:center; color:#888; padding:20px'><i>Answer Hidden</i></div>", unsafe_allow_html=True)
                
                b1, b2 = st.columns(2)
                with b1:
                    if st.button("👁️ Reveal", use_container_width=True):
                        st.session_state.show_answer = True
                        st.rerun()
                with b2:
                    if st.button("✨ Simplify (ELI5)", use_container_width=True):
                        with st.spinner("Simplifying..."):
                            try:
                                simplified = simplify_card_eli5(api_key, current['front'], current['back'])
                                st.session_state["flashcards"][idx]['front'] = clean_markdown(simplified['front'])
                                st.session_state["flashcards"][idx]['back'] = clean_markdown(simplified['back'])
                                st.session_state["flashcards"][idx]['tag'] = current['tag'] + " (Simple)"
                                st.rerun()
                            except Exception as e:
                                st.error("Could not simplify.")

    # --- EDITOR ---
    with tab_edit:
        st.info("Double-click cells to edit.")
        new_df = st.data_editor(df, num_rows="dynamic", use_container_width=True)
        st.session_state["flashcards"] = new_df.to_dict('records')
        
        csv = new_df.to_csv(index=False, header=False, sep="|")
        st.download_button("📥 Download CSV (Anki)", csv, "deck.csv", "text/csv")
