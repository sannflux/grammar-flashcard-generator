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
st.set_page_config(page_title="Flashcard Pro v7.6", layout="wide")

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, deck_id INTEGER, front TEXT, back TEXT, 
            explanation TEXT, tag TEXT, next_review TEXT)''')
        conn.commit()

init_db()

# ====================== THE "SECRET SAUCE" BYPASS ======================

def prepare_cookies():
    """Converts JSON to Netscape. Mandatory for v7.6."""
    json_path = "youtube_cookies.json"
    txt_path = "youtube_cookies.txt"
    if not os.path.exists(json_path): return None
    try:
        with open(json_path, 'r') as f:
            cookies = json.load(f)
        with open(txt_path, 'w') as f:
            f.write("# Netscape HTTP Cookie File\n")
            for c in cookies:
                domain = c.get('domain', '')
                flag = "TRUE" if domain.startswith('.') else "FALSE"
                path = c.get('path', '/')
                secure = "TRUE" if c.get('secure') else "FALSE"
                expiry = int(c.get('expirationDate', 0))
                name = c.get('name', '')
                value = c.get('value', '')
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
        return txt_path
    except: return None

def get_transcript_v7_6(video_id):
    """Bypasses 'Format not available' by using the 'extract_flat' method."""
    cookie_file = prepare_cookies()
    
    import yt_dlp
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'cookiefile': cookie_file,
        # CRITICAL FIX: Tell yt-dlp NOT to check for video formats
        'check_formats': False, 
        'ignore_no_formats_error': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en.*', 'id.*'], # Catch any English or Indonesian variant
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # We use download=False and let it fail gracefully on formats
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        
        sub_url = None
        # Logic to grab subtitles even if video formats are hidden
        for lang_key in ['en', 'en-US', 'id']:
            if 'subtitles' in info and lang_key in info['subtitles']:
                sub_url = info['subtitles'][lang_key][0]['url']
                break
            if not sub_url and 'automatic_captions' in info and lang_key in info['automatic_captions']:
                # Find JSON3 if possible
                for f in info['automatic_captions'][lang_key]:
                    if 'json3' in f.get('url', ''):
                        sub_url = f['url']
                        break
                if not sub_url: sub_url = info['automatic_captions'][lang_key][0]['url']
                break

        if sub_url:
            r = requests.get(sub_url)
            if 'json3' in sub_url or '"events"' in r.text:
                data = r.json()
                return " ".join([s['utf8'] for e in data.get('events', []) for s in e.get('segs', []) if 'utf8' in s])
            return re.sub(r'<[^>]+>', '', r.text)
    return None

# ====================== AI & UI ======================

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
        contents=[f"Content: {text}"],
        config=types.GenerateContentConfig(
            system_instruction=f"Generate {qty} flashcards in JSON.",
            response_mime_type="application/json",
            response_schema=FlashcardSet
        )
    )
    return json.loads(response.text).get("cards", [])

def main():
    st.title("🧠 Flashcard Pro v7.6 (The Final Fix)")
    
    api_key = st.sidebar.text_input("Gemini API Key", type="password")
    url = st.text_input("YouTube URL")

    if st.button("Magic Build"):
        if not api_key:
            st.error("Missing API Key")
            return

        vid = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
        if vid:
            with st.spinner("Extracting content (Force-Bypassing Blocks)..."):
                try:
                    text = get_transcript_v7_6(vid.group(1))
                    if text:
                        cards = generate_cards(api_key, text, 10)
                        # DB Saving logic
                        with sqlite3.connect(DB_NAME) as conn:
                            c = conn.cursor()
                            c.execute("INSERT OR IGNORE INTO decks (name) VALUES (?)", ("Auto Deck",))
                            d_id = c.execute("SELECT id FROM decks WHERE name='Auto Deck'").fetchone()[0]
                            for card in cards:
                                c.execute("INSERT INTO cards (deck_id, front, back, explanation, tag, next_review) VALUES (?,?,?,?,?,?)",
                                          (d_id, card['front'], card['back'], card['explanation'], card['tag'], datetime.now().date()))
                        st.balloons()
                        st.success(f"Created {len(cards)} cards!")
                    else:
                        st.error("No transcript found. YouTube is successfully hiding the content.")
                except Exception as e:
                    st.error(f"Fatal Block: {e}")
        else:
            st.error("Invalid URL")

if __name__ == "__main__":
    main()
