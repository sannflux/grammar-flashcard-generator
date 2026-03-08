import streamlit as st
import pandas as pd
import tempfile
import os
import time
import json
import sqlite3
import hashlib
import re  # Added for regex cleaning
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List

# ====================== 1. CONFIGURATION & STATE ======================
st.set_page_config(
    page_title="Flashcard Library Pro", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize Session State
if "flashcards" not in st.session_state:
    st.session_state["flashcards"] = [] # List of dicts
if "card_index" not in st.session_state:
    st.session_state["card_index"] = 0
if "show_answer" not in st.session_state:
    st.session_state["show_answer"] = False
if "current_deck_name" not in st.session_state:
    st.session_state["current_deck_name"] = ""

# ====================== 2. DATABASE ENGINE (SQLite) ======================
DB_NAME = "flashcards.db"

def init_db():
    """Initialize the local SQLite database for deck storage."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Create Decks Table
    c.execute('''CREATE TABLE IF NOT EXISTS decks
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  name TEXT UNIQUE, 
                  created_at TEXT)''')
    # Create Cards Table
    c.execute('''CREATE TABLE IF NOT EXISTS cards
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  deck_id INTEGER, 
                  front TEXT, 
                  back TEXT, 
                  tag TEXT,
                  FOREIGN KEY(deck_id) REFERENCES decks(id))''')
    conn.commit()
    conn.close()

def save_deck_to_db(deck_name, cards):
    """Saves the current session cards to the database."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        # Insert Deck
        c.execute("INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)", 
                  (deck_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        
        # Get Deck ID
        c.execute("SELECT id FROM decks WHERE name=?", (deck_name,))
        deck_id = c.fetchone()[0]
        
        # Insert Cards (Clear old ones for this deck first to avoid dupes on re-save)
        c.execute("DELETE FROM cards WHERE deck_id=?", (deck_id,))
        for card in cards:
            c.execute("INSERT INTO cards (deck_id, front, back, tag) VALUES (?, ?, ?, ?)",
                      (deck_id, card['front'], card['back'], card['tag']))
        conn.commit()
        return True
    except Exception as e:
        st.error(f"Database Error: {e}")
        return False
    finally:
        conn.close()

def load_deck_from_db(deck_name):
    """Loads a specific deck into session state."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id FROM decks WHERE name=?", (deck_name,))
    result = c.fetchone()
    if result:
        deck_id = result[0]
        c.execute("SELECT front, back, tag FROM cards WHERE deck_id=?", (deck_id,))
        rows = c.fetchall()
        # Convert back to list of dicts
        loaded_cards = [{"front": r[0], "back": r[1], "tag": r[2]} for r in rows]
        conn.close()
        return loaded_cards
    conn.close()
    return []

def get_all_deck_names():
    """Fetches all saved deck names for the sidebar."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT name FROM decks ORDER BY created_at DESC")
    names = [row[0] for row in c.fetchall()]
    conn.close()
    return names

# Initialize DB on script run
init_db()

# ====================== 3. PYDANTIC SCHEMAS ======================
class Flashcard(BaseModel):
    front: str = Field(description="The question. No markdown.")
    back: str = Field(description="The answer. Use HTML <b> and <i> tags. No markdown.")
    tag: str = Field(description="A short category tag.")

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

# ====================== 4. HELPER: CLEAN MARKDOWN ======================
def clean_markdown(text):
    """
    Sanitizes LLM output to ensure no Markdown leaks into Anki cards.
    Converts **bold** to <b>bold</b> and *italic* to <i>italic</i>.
    Removes other markdown symbols.
    """
    if not text: return ""
    
    # 1. Convert Bold (**text**) -> <b>text</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # 2. Convert Italic (*text*) -> <i>text</i>
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    
    # 3. Remove remaining markdown artifacts (backticks, hashes)
    text = text.replace('`', '').replace('#', '')
    
    return text.strip()

# ====================== 5. CACHED AI FUNCTIONS ======================

@st.cache_data(show_spinner=False)
def generate_flashcards_cached(api_key, file_content, file_type, text_input, difficulty, card_count):
    """
    Core generation logic wrapped in cache. 
    Note: We pass file CONTENT (bytes), not the file object, to make it hashable.
    """
    client = genai.Client(api_key=api_key)
    contents = []

    # Handle File (Re-uploading logic needed here since cache stores result, not the remote file ref)
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

    # Prompt construction
    count_instruction = f"Generate {card_count} cards." if isinstance(card_count, int) else "Generate a comprehensive set."
    
    # --- REINFORCED SYSTEM PROMPT ---
    system_prompt = f"""
    You are an expert tutor for {difficulty} students.
    Task: Create active-recall flashcards based on the provided content.
    
    STRICT FORMATTING RULES:
    1. **NO MARKDOWN:** Do not use markdown syntax like **, *, `, or # in the output.
    2. **HTML ONLY:** For emphasis in the 'back' (Answer) field, use ONLY <b> for bold and <i> for italic.
    3. **FRONT FIELD:** The Question must be plain text only. No formatting.
    4. **BACK FIELD:** The Answer must be concise (max 3 sentences).
    
    Output must be a valid JSON object matching the schema.
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
    
    # Cleanup remote file if created
    if file_content and gemini_file:
        try:
            client.files.delete(name=gemini_file.name)
        except:
            pass

    return response.text

def simplify_card_eli5(api_key, front, back):
    """Non-cached function to simplify a specific card on demand."""
    client = genai.Client(api_key=api_key)
    prompt = f"""
    Rewrite this flashcard to be simpler (Explain Like I'm 5).
    Keep the meaning but use simpler words.
    ENSURE OUTPUT HAS NO MARKDOWN. Use <b> tags only if needed.
    
    Original Question: {front}
    Original Answer: {back}
    
    Return JSON: {{"front": "New Simple Question", "back": "New Simple Answer"}}
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

# ====================== 7. SIDEBAR: LIBRARY & SETTINGS ======================
with st.sidebar:
    st.header("📚 Library & Settings")
    
    with st.expander("🔑 API Key", expanded=not os.environ.get("GEMINI_API_KEY")):
        api_key = st.text_input("Gemini API Key", type="password", value=os.environ.get("GEMINI_API_KEY", ""))

    st.subheader("📁 Saved Decks")
    saved_decks = get_all_deck_names()
    selected_deck = st.selectbox("Load a Deck", ["-- Select --"] + saved_decks)
    
    if selected_deck != "-- Select --":
        if st.button("📂 Load Selected Deck"):
            st.session_state["flashcards"] = load_deck_from_db(selected_deck)
            st.session_state["current_deck_name"] = selected_deck
            st.rerun()

    st.divider()
    
    st.subheader("⚙️ Generator Settings")
    difficulty = st.select_slider("Level", ["High School", "Undergrad", "PhD"], value="Undergrad")
    card_count = st.selectbox("Quantity", ["Auto", 5, 10, 20])
    mode = st.radio("Mode", ["Append", "Replace"], index=1)

# ====================== 8. MAIN UI ======================
st.title("🧠 Flashcard Library Pro")

# --- Tabs for Creation ---
t1, t2 = st.tabs(["🆕 Create New", "💾 Save Current Deck"])

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
                # Prepare args for cached function
                file_bytes = uploaded_file.getvalue() if uploaded_file else None
                file_type = uploaded_file.type.split('/')[-1] if uploaded_file else None
                count_val = card_count if isinstance(card_count, int) else "Auto"
                
                # Call Cached Function
                json_str = generate_flashcards_cached(
                    api_key, file_bytes, file_type, pasted_text, difficulty, count_val
                )
                
                new_data = json.loads(json_str).get("cards", [])
                
                # --- POST-PROCESS CLEANING ---
                # We scrub the data here just in case the LLM ignored instructions
                cleaned_data = []
                for card in new_data:
                    cleaned_data.append({
                        "front": clean_markdown(card.get("front", "")),
                        "back": clean_markdown(card.get("back", "")),
                        "tag": card.get("tag", "")
                    })
                
                if mode == "Replace":
                    st.session_state["flashcards"] = cleaned_data
                    st.session_state["current_deck_name"] = "" # Reset name on new gen
                else:
                    st.session_state["flashcards"].extend(cleaned_data)
                
                st.success(f"Generated {len(cleaned_data)} clean cards!")
                time.sleep(1) # Small delay for UX
                st.rerun()
                
            except Exception as e:
                st.error(f"Error: {str(e)}")

with t2:
    if st.session_state["flashcards"]:
        save_name = st.text_input("Deck Name", value=st.session_state["current_deck_name"])
        if st.button("💾 Save to Library"):
            if save_name:
                if save_deck_to_db(save_name, st.session_state["flashcards"]):
                    st.success(f"Deck '{save_name}' saved to database!")
                    time.sleep(1)
                    st.rerun()
            else:
                st.warning("Please enter a name.")
    else:
        st.info("Generate cards first to save them.")

# ====================== 9. CARD PREVIEW & EDITOR ======================
if st.session_state["flashcards"]:
    st.divider()
    
    # Setup Dataframe
    df = pd.DataFrame(st.session_state["flashcards"])
    
    tab_view, tab_edit = st.tabs(["👀 Focus Mode", "✏️ Editor"])

    # --- FOCUS MODE (With ELI5) ---
    with tab_view:
        if not df.empty:
            # Controls
            c1, c2, c3 = st.columns([1, 4, 1])
            idx = st.session_state.card_index
            
            # Bounds check
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
            
            # Card Display
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
                
                # Action Buttons
                b1, b2 = st.columns(2)
                with b1:
                    if st.button("👁️ Reveal", use_container_width=True):
                        st.session_state.show_answer = True
                        st.rerun()
                with b2:
                    # ELI5 FEATURE
                    if st.button("✨ Simplify (ELI5)", use_container_width=True, help="Rewrite this card to be simpler."):
                        with st.spinner("Simplifying..."):
                            try:
                                simplified = simplify_card_eli5(api_key, current['front'], current['back'])
                                # Update State directly & clean output
                                st.session_state["flashcards"][idx]['front'] = clean_markdown(simplified['front'])
                                st.session_state["flashcards"][idx]['back'] = clean_markdown(simplified['back'])
                                st.session_state["flashcards"][idx]['tag'] = current['tag'] + " (Simple)"
                                st.rerun()
                            except Exception as e:
                                st.error("Could not simplify.")

    # --- EDITOR ---
    with tab_edit:
        st.info("Double-click cells to edit. Edits are saved to Export automatically.")
        new_df = st.data_editor(df, num_rows="dynamic", use_container_width=True)
        st.session_state["flashcards"] = new_df.to_dict('records')
        
        # CSV Export
        csv = new_df.to_csv(index=False, header=False, sep="|")
        st.download_button("📥 Download CSV (Anki)", csv, "deck.csv", "text/csv")
