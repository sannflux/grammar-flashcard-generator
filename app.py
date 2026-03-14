import streamlit as st
import pandas as pd
import sqlite3
import re
import requests
import json
import os
import html
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import List

# --- DEPENDENCIES ---
try:
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError:
    st.error("Missing: youtube-transcript-api")

try:
    import yt_dlp
except ImportError:
    st.error("Missing: yt-dlp")

# ====================== CONFIG & DATABASE ======================
st.set_page_config(page_title="Flashcard Pro v6.8", page_icon="🧠", layout="wide")
DB_NAME = "flashcards_v8.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, deck_id INTEGER, front TEXT, back TEXT, 
            explanation TEXT, tag TEXT, next_review TEXT, interval INTEGER, ease REAL)''')
        conn.commit()

init_db()

# ====================== COOKIE TRANSLATOR ======================

def prepare_cookies():
    """Converts your JSON cookies into the Netscape format required by YouTube libraries."""
    json_path = "youtube_cookies.json"
    txt_path = "youtube_cookies.txt"
    
    if not os.path.exists(json_path):
        return None

    try:
        with open(json_path, 'r') as f:
            cookies_json = json.load(f)
        
        with open(txt_path, 'w') as f:
            f.write("# Netscape HTTP Cookie File\n")
            for c in cookies_json:
                domain = c.get('domain', '')
                flag = "TRUE" if domain.startswith('.') else "FALSE"
                path = c.get('path', '/')
                secure = "TRUE" if c.get('secure') else "FALSE"
                expiry = int(c.get('expirationDate', 0))
                name = c.get('name', '')
                value = c.get('value', '')
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
        return txt_path
    except Exception as e:
        st.error(f"Cookie Processing Error: {e}")
        return None

# ====================== TRANSCRIPT ENGINE ======================

def get_transcript_safe(video_id):
    """Uses cookies to bypass YouTube's 'Blocking' error."""
    cookie_file = prepare_cookies()
    
    # 1. Attempt with official Transcript API + Cookies
    try:
        loader = YouTubeTranscriptApi.list_transcripts(video_id, cookies=cookie_file)
        transcript = loader.find_transcript(['en', 'id']) # Added 'id' for Indonesian videos
        text = " ".join([t['text'] for t in transcript.fetch()])
        if len(text.strip()) > 100: return text
    except Exception as e:
        print(f"API Fetch failed: {e}")

    # 2. Attempt with yt-dlp (Stronger bypass)
    try:
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en', 'id'],
            'quiet': True,
            'cookiefile': cookie_file
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            # If Stage 1 failed but Stage 2 found info, the block is likely TLS-based
            pass 
    except Exception:
        pass
    
    # 3. Last Resort: Proxy
    try:
        resp = requests.get(f"https://youtubetranscript.com/?server_vid2={video_id}", timeout=10)
        final = html.unescape(re.sub(r'<[^>]+>', ' ', resp.text))
        if len(final.strip()) > 100: return final
    except:
        pass

    raise ValueError("YouTube is still blocking access. Please refresh your cookies in Kiwi and try again.")

# ====================== UI & GENERATION ======================

class Flashcard(BaseModel):
    front: str
    back: str
    explanation: str
    tag: str

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

def generate_cards(api_key, text, qty):
    client = genai.Client(api_key=api_key)
    prompt = f"Create {qty} flashcards from this text. Use <b>bold</b> for keywords. Output JSON."
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
    st.title("🧠 AI Flashcard Factory")
    
    if "transcript_content" not in st.session_state:
        st.session_state.transcript_content = ""

    with st.sidebar:
        api_key = st.text_input("Gemini API Key", type="password")
        if os.path.exists("youtube_cookies.json"):
            st.success("Cookies Loaded!")
        else:
            st.warning("Please add youtube_cookies.json")

    url = st.text_input("YouTube URL")
    
    if st.button("Fetch & Process"):
        video_id = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
        if video_id:
            with st.spinner("Bypassing YouTube blocks..."):
                try:
                    text = get_transcript_safe(video_id.group(1))
                    st.session_state.transcript_content = text
                    st.success("Transcript Extracted!")
                except Exception as e:
                    st.error(str(e))
        else:
            st.error("Invalid URL")

    st.session_state.transcript_content = st.text_area("Content:", st.session_state.transcript_content, height=300)

    if st.button("Generate Cards", type="primary"):
        if not api_key:
            st.error("Need API Key")
        elif len(st.session_state.transcript_content) > 100:
            with st.spinner("AI is working..."):
                cards = generate_cards(api_key, st.session_state.transcript_content, 10)
                # (Database saving logic included in full app)
                st.balloons()
                st.json(cards)

if __name__ == "__main__":
    main()
