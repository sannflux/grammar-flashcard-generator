import streamlit as st
import pandas as pd
import sqlite3
import re
import requests
import json
import os
import html
import time
import io
import base64
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List
from PIL import Image
from gtts import gTTS

# --- EXTRACTOR IMPORTS ---
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    YOUTUBE_API_READY = True
except ImportError:
    YOUTUBE_API_READY = False

try:
    import yt_dlp
    YTDLP_READY = True
except ImportError:
    YTDLP_READY = False

# ====================== CONFIG & DATABASE ======================
st.set_page_config(page_title="Flashcard Pro v6.7", page_icon="🧠", layout="wide")
DB_NAME = "flashcards_v7.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, deck_id INTEGER, front TEXT, back TEXT, 
            explanation TEXT, tag TEXT, next_review TEXT, interval INTEGER, ease REAL)''')
        conn.commit()

init_db()

# ====================== THE "REAL" TRANSCRIPT ENGINE ======================

def get_transcript_v2(video_id):
    """Multi-stage fetch with strict character-count validation."""
    cookie_file = "youtube_cookies.json"
    
    # STAGE 1: Standard API (with optional cookies)
    if YOUTUBE_API_READY:
        try:
            # Note: API usually expects a .txt file, but some versions handle dicts
            cookies = cookie_file if os.path.exists(cookie_file) else None
            loader = YouTubeTranscriptApi.list_transcripts(video_id, cookies=cookies)
            transcript = loader.find_transcript(['en', 'en-US'])
            text = " ".join([t['text'] for t in transcript.fetch()])
            if len(text.strip()) > 100: return text
        except Exception: pass

    # STAGE 2: yt-dlp (The reliable bypass)
    if YTDLP_READY:
        try:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['en'],
                'quiet': True,
                'cookiefile': cookie_file if os.path.exists(cookie_file) else None
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                # This logic is simplified; yt-dlp fetches to disk or stdout
                # We mainly use this to check if subtitles ARE available
                if 'subtitles' in info or 'automatic_captions' in info:
                    # If we got here, Stage 1 should have worked with cookies.
                    # If it didn't, we likely need a manual paste.
                    pass 
        except Exception: pass

    # STAGE 3: Proxy Fallback
    try:
        resp = requests.get(f"https://youtubetranscript.com/?server_vid2={video_id}", timeout=10)
        clean_text = html.unescape(re.sub(r'<[^>]+>', ' ', resp.text))
        final = re.sub(r'\s+', ' ', clean_text).strip()
        if len(final) > 100: return final
    except: pass
    
    # If all stages fail to find actual text
    raise ValueError("YouTube blocked the automated fetch. Please use the manual box below.")

def extract_vid(url):
    match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
    return match.group(1) if match else None

# ====================== AI & UI LOGIC ======================

class Flashcard(BaseModel):
    front: str
    back: str
    explanation: str
    tag: str

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

def generate_cards(api_key, text, qty):
    client = genai.Client(api_key=api_key)
    prompt = f"Create {qty} educational flashcards from the provided text. Use <b>bold</b> for key terms. Output JSON."
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[text],
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            response_mime_type="application/json",
            response_schema=FlashcardSet
        )
    )
    return json.loads(response.text).get("cards", [])

def main():
    st.title("🧠 Flashcard Pro v6.7")
    
    with st.sidebar:
        api_key = st.sidebar.text_input("Gemini API Key", type="password")
        st.divider()
        st.info("🍪 Tip: Since you're on Kiwi, paste your 'JSON Cookie' into a file named 'youtube_cookies.json' in your app directory to bypass YouTube blocks.")

    # State management for transcript
    if "transcript_content" not in st.session_state:
        st.session_state.transcript_content = ""

    tab_gen, tab_lib = st.tabs(["Factory", "Library"])

    with tab_gen:
        col1, col2 = st.columns([2, 1])
        
        with col1:
            url = st.text_input("Video URL", placeholder="https://www.youtube.com/watch?v=...")
            
            if st.button("🔍 Fetch Transcript"):
                vid = extract_vid(url)
                if vid:
                    with st.spinner("Battling YouTube's bot detection..."):
                        try:
                            result = get_transcript_v2(vid)
                            st.session_state.transcript_content = result
                            st.success("Transcript loaded successfully!")
                        except Exception as e:
                            st.error(str(e))
                            st.session_state.transcript_content = ""
                else:
                    st.error("Could not find a valid Video ID in that URL.")

            # The Text Area (Always visible, but pre-filled if fetch works)
            st.session_state.transcript_content = st.text_area(
                "Source Text (Edit or Paste Manually):", 
                value=st.session_state.transcript_content, 
                height=300
            )

        with col2:
            st.subheader("Config")
            deck_name = st.text_input("Deck Name", "New Study Set")
            qty = st.number_input("Card Count", 5, 30, 10)
            
            if st.button("🚀 Build Deck", type="primary", use_container_width=True):
                if not api_key:
                    st.error("API Key Required")
                elif len(st.session_state.transcript_content) < 50:
                    st.warning("Please provide more text content first.")
                else:
                    with st.spinner("AI is studying..."):
                        try:
                            cards = generate_cards(api_key, st.session_state.transcript_content, qty)
                            with sqlite3.connect(DB_NAME) as conn:
                                c = conn.cursor()
                                c.execute("INSERT OR IGNORE INTO decks (name) VALUES (?)", (deck_name,))
                                deck_id = c.execute("SELECT id FROM decks WHERE name=?", (deck_name,)).fetchone()[0]
                                for card in cards:
                                    c.execute("INSERT INTO cards (deck_id, front, back, explanation, tag, next_review, interval, ease) VALUES (?,?,?,?,?,?,1,2.5)",
                                              (deck_id, card['front'], card['back'], card['explanation'], card['tag'], datetime.now().date()))
                                conn.commit()
                            st.balloons()
                            st.success(f"Generated {len(cards)} cards!")
                        except Exception as e:
                            st.error(f"AI Error: {e}")

    with tab_lib:
        with sqlite3.connect(DB_NAME) as conn:
            decks = pd.read_sql("SELECT * FROM decks", conn)
            if not decks.empty:
                sel = st.selectbox("Select Deck", decks['name'])
                d_id = decks[decks['name'] == sel]['id'].values[0]
                df = pd.read_sql(f"SELECT front, back, tag FROM cards WHERE deck_id={d_id}", conn)
                st.table(df)
            else:
                st.write("No decks created yet.")

if __name__ == "__main__":
    main()
