import streamlit as st
import pandas as pd
import sqlite3
import re
import json
import os
import requests
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import List

# ====================== DATABASE & CONFIG ======================
DB_NAME = "flashcards_v7.db"
st.set_page_config(page_title="Flashcard Pro v7.7", layout="wide")

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, deck_id INTEGER, front TEXT, back TEXT, 
            explanation TEXT, tag TEXT, next_review TEXT)''')
        conn.commit()

init_db()

# ====================== THE EXTRACTOR ======================

def get_transcript_v7_7(video_id):
    """Attempt auto-fetch, but fail gracefully with explanation."""
    import yt_dlp
    
    # Check for cookies
    json_path = "youtube_cookies.json"
    txt_path = "youtube_cookies.txt"
    cookie_file = None
    
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r') as f:
                cookies = json.load(f)
            with open(txt_path, 'w') as f:
                f.write("# Netscape HTTP Cookie File\n")
                for c in cookies:
                    domain = c.get('domain', '')
                    f.write(f"{domain}\tTRUE\t/\tTRUE\t{int(c.get('expirationDate', 0))}\t{c.get('name', '')}\t{c.get('value', '')}\n")
            cookie_file = txt_path
        except: pass

    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'cookiefile': cookie_file,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en.*', 'id.*'],
        'check_formats': False, 
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            # Search for subtitle URLs
            sub_url = None
            if 'subtitles' in info and info['subtitles']:
                sub_url = next(iter(info['subtitles'].values()))[0]['url']
            elif 'automatic_captions' in info and info['automatic_captions']:
                sub_url = next(iter(info['automatic_captions'].values()))[0]['url']
            
            if sub_url:
                r = requests.get(sub_url)
                if 'json3' in sub_url:
                    data = r.json()
                    return " ".join([s['utf8'] for e in data.get('events', []) for s in e.get('segs', []) if 'utf8' in s])
                return re.sub(r'<[^>]+>', '', r.text)
    except Exception as e:
        return f"BLOCK_ERROR: {str(e)}"
    return None

# ====================== AI LOGIC ======================

class Flashcard(BaseModel):
    front: str
    back: str
    explanation: str
    tag: str

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

def generate_cards(api_key, text, qty):
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[f"Create {qty} flashcards from: {text}"],
        config=types.GenerateContentConfig(
            system_instruction="Output JSON matching schema. Use <b>bold</b>.",
            response_mime_type="application/json",
            response_schema=FlashcardSet
        )
    )
    return json.loads(response.text).get("cards", [])

# ====================== UI ======================

def main():
    st.title("🧠 Flashcard Factory v7.7")
    
    api_key = st.sidebar.text_input("Gemini API Key", type="password")
    
    if "final_text" not in st.session_state:
        st.session_state.final_text = ""

    url = st.text_input("1. YouTube Link")
    
    col_auto, col_manual = st.columns(2)
    
    with col_auto:
        if st.button("🪄 Auto-Fetch Content"):
            vid = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
            if vid:
                with st.spinner("Battling YouTube..."):
                    res = get_transcript_v7_7(vid.group(1))
                    if res and "BLOCK_ERROR" not in res:
                        st.session_state.final_text = res
                        st.success("Success! Text loaded.")
                    else:
                        st.error("YouTube Blocked the Auto-Fetch.")
                        st.info("The server IP is flagged. Please use the 'Manual' box to the right.")

    with col_manual:
        st.session_state.final_text = st.text_area("2. Or Paste Transcript Manually:", 
                                                  value=st.session_state.final_text, 
                                                  placeholder="Paste from 'Show Transcript' here...",
                                                  height=150)

    deck_name = st.text_input("3. Deck Name", "New Study Set")
    
    if st.button("🚀 Build Flashcards", type="primary"):
        if not api_key:
            st.error("Enter API Key")
        elif len(st.session_state.final_text) < 100:
            st.error("Content too short. Paste or fetch more text.")
        else:
            with st.spinner("AI is thinking..."):
                try:
                    cards = generate_cards(api_key, st.session_state.final_text, 10)
                    # Database save
                    with sqlite3.connect(DB_NAME) as conn:
                        c = conn.cursor()
                        c.execute("INSERT OR IGNORE INTO decks (name) VALUES (?)", (deck_name,))
                        d_id = c.execute("SELECT id FROM decks WHERE name=?", (deck_name,)).fetchone()[0]
                        for card in cards:
                            c.execute("INSERT INTO cards (deck_id, front, back, explanation, tag, next_review) VALUES (?,?,?,?,?,?)",
                                      (d_id, card['front'], card['back'], card['explanation'], card['tag'], datetime.now().date()))
                    st.balloons()
                    for card in cards:
                        with st.expander(card['front']):
                            st.write(card['back'])
                except Exception as e:
                    st.error(f"AI Error: {e}")

if __name__ == "__main__":
    main()
