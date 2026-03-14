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
st.set_page_config(page_title="Flashcard Pro v7.5", layout="wide")

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, deck_id INTEGER, front TEXT, back TEXT, 
            explanation TEXT, tag TEXT, next_review TEXT)''')
        conn.commit()

init_db()

# ====================== COOKIE CONVERTER ======================

def prepare_netscape_cookies():
    """Converts JSON cookies into the strict Netscape format for 2026 bypass."""
    json_path = "youtube_cookies.json"
    txt_path = "youtube_cookies.txt"
    if not os.path.exists(json_path):
        return None
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
    except:
        return None

# ====================== THE "BROWSER IMPERSONATOR" ======================

def get_transcript_v7_5(video_id):
    """Fetches transcript by impersonating a real browser session."""
    cookie_file = prepare_netscape_cookies()
    
    import yt_dlp
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'cookiefile': cookie_file,
        # IMPERSONATION: This makes YouTube think you are on a real PC
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.google.com/',
        },
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en', 'id'],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        
        # Priority 1: Manual Subtitles
        sub_url = None
        if 'subtitles' in info and info['subtitles']:
            for lang in ['en', 'id']:
                if lang in info['subtitles']:
                    sub_url = info['subtitles'][lang][0]['url']
                    break
        
        # Priority 2: Auto-Generated
        if not sub_url and 'automatic_captions' in info:
            for lang in ['en', 'id']:
                if lang in info['automatic_captions']:
                    # Prefer JSON3 format for cleaner AI processing
                    for fmt in info['automatic_captions'][lang]:
                        if fmt.get('ext') == 'json3' or 'fmt=json3' in fmt.get('url', ''):
                            sub_url = fmt['url']
                            break
                    if not sub_url: sub_url = info['automatic_captions'][lang][0]['url']
                    break

        if sub_url:
            resp = requests.get(sub_url)
            if 'json3' in sub_url or '"events"' in resp.text:
                data = resp.json()
                # Extract text and filter out metadata
                lines = []
                for event in data.get('events', []):
                    if 'segs' in event:
                        text = "".join([s['utf8'] for s in event['segs'] if 'utf8' in s])
                        if text.strip(): lines.append(text)
                return " ".join(lines)
            else:
                # Clean VTT tags
                text = re.sub(r'<[^>]+>', '', resp.text)
                text = re.sub(r'\d{2}:\d{2}:\d{2}.\d{3} --> \d{2}:\d{2}:\d{2}.\d{3}', '', text)
                return text
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
        contents=[f"Text: {text}"],
        config=types.GenerateContentConfig(
            system_instruction=f"Create {qty} flashcards in JSON. Use <b>bold</b> for key terms.",
            response_mime_type="application/json",
            response_schema=FlashcardSet
        )
    )
    return json.loads(response.text).get("cards", [])

def main():
    st.title("🧠 One-Click Flashcard Factory")
    
    with st.sidebar:
        api_key = st.text_input("Gemini API Key", type="password")
        if os.path.exists("youtube_cookies.json"):
            st.success("✅ Cookies JSON detected")
        else:
            st.error("❌ Missing youtube_cookies.json")

    url = st.text_input("YouTube URL")
    deck_name = st.text_input("Deck Name", "My New Deck")

    if st.button("🚀 Build My Deck", type="primary"):
        if not api_key:
            st.error("Enter API Key first.")
            return

        vid_match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
        if vid_match:
            with st.spinner("Impersonating browser & grabbing content..."):
                try:
                    text = get_transcript_v7_5(vid_match.group(1))
                    if text:
                        cards = generate_cards(api_key, text, 10)
                        
                        # Save to DB
                        with sqlite3.connect(DB_NAME) as conn:
                            c = conn.cursor()
                            c.execute("INSERT OR IGNORE INTO decks (name) VALUES (?)", (deck_name,))
                            deck_id = c.execute("SELECT id FROM decks WHERE name=?", (deck_name,)).fetchone()[0]
                            for card in cards:
                                c.execute("INSERT INTO cards (deck_id, front, back, explanation, tag, next_review) VALUES (?,?,?,?,?,?)",
                                          (deck_id, card['front'], card['back'], card['explanation'], card['tag'], datetime.now().date()))
                            conn.commit()
                        
                        st.balloons()
                        for card in cards:
                            with st.expander(f"🎴 {card['front']}"):
                                st.write(card['back'])
                                st.caption(card['explanation'])
                    else:
                        st.error("Transcript unavailable for this video.")
                except Exception as e:
                    st.error(f"Error: {e}")
        else:
            st.error("Invalid YouTube Link")

if __name__ == "__main__":
    main()
