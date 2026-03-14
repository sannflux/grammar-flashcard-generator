import streamlit as st
import pandas as pd
import sqlite3
import re
import json
import os
import subprocess
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import List

# ====================== DATABASE & CONFIG ======================
DB_NAME = "flashcards_v7.db"
st.set_page_config(page_title="Flashcard Pro v7.0", layout="wide")

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, deck_id INTEGER, front TEXT, back TEXT, 
            explanation TEXT, tag TEXT, next_review TEXT)''')
        conn.commit()

init_db()

# ====================== THE BYPASS ENGINE ======================

def get_transcript_v7(video_id):
    """
    Uses yt-dlp directly via subprocess. 
    This is the most 'brute force' way to get transcripts in 2026.
    """
    json_cookie_path = "youtube_cookies.json"
    output_file = f"{video_id}_subs"
    
    # Check if cookies exist
    cookie_arg = f"--cookies {json_cookie_path}" if os.path.exists(json_cookie_path) else ""
    
    # Command to yt-dlp: Get subtitles, don't download video, output to terminal
    # We use --get-subs and --skip-download
    cmd = f'yt-dlp --get-subs --skip-download --sub-langs "en.*,id.*" --write-auto-subs {cookie_arg} https://www.youtube.com/watch?v={video_id}'
    
    try:
        # We attempt to use the python library first as it's cleaner
        import yt_dlp
        ydl_opts = {
            'skip_download': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en', 'id'],
            'quiet': True,
            'cookiefile': json_cookie_path if os.path.exists(json_cookie_path) else None
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            
            # Locate the subtitle URL
            sub_url = None
            if 'subtitles' in info and len(info['subtitles']) > 0:
                # Prefer manual subtitles
                lang = list(info['subtitles'].keys())[0]
                sub_url = info['subtitles'][lang][0]['url']
            elif 'automatic_captions' in info and len(info['automatic_captions']) > 0:
                # Fallback to auto-generated
                lang = 'en' if 'en' in info['automatic_captions'] else list(info['automatic_captions'].keys())[0]
                # Filter for 'json3' format which is easiest to parse
                for formats in info['automatic_captions'][lang]:
                    if formats.get('ext') == 'json3' or 'fmt=json3' in formats.get('url', ''):
                        sub_url = formats['url']
                        break
                if not sub_url: sub_url = info['automatic_captions'][lang][0]['url']

            if sub_url:
                resp = subprocess.check_output(['curl', '-L', sub_url], stderr=subprocess.STDOUT)
                # If it's JSON3 format, we parse it
                try:
                    data = json.loads(resp)
                    full_text = " ".join([event['segs'][0]['utf8'] for event in data['events'] if 'segs' in event])
                    return full_text
                except:
                    # If it's VTT format, we just strip the tags
                    raw_text = resp.decode('utf-8')
                    clean_text = re.sub(r'<[^>]+>', '', raw_text) # Remove HTML tags
                    clean_text = re.sub(r'\d{2}:\d{2}:\d{2}.\d{3}.*', '', clean_text) # Remove timestamps
                    return clean_text
    except Exception as e:
        raise ValueError(f"Bypass failed. YouTube's bot wall is too strong for your current IP. Error: {str(e)}")

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
        contents=[f"Create {qty} flashcards from this: {text}"],
        config=types.GenerateContentConfig(
            system_instruction="Output JSON matching the schema.",
            response_mime_type="application/json",
            response_schema=FlashcardSet
        )
    )
    return json.loads(response.text).get("cards", [])

def main():
    st.title("🚀 Flashcard Pro v7.0 (The Fix)")
    
    api_key = st.sidebar.text_input("Gemini API Key", type="password")
    url = st.text_input("YouTube URL")
    
    if st.button("Magic Generate (Like Before)"):
        if not api_key:
            st.error("Enter API Key")
            return
            
        vid_match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
        if vid_match:
            vid = vid_match.group(1)
            with st.spinner("Executing Stage 3 Bypass..."):
                try:
                    text = get_transcript_v7(vid)
                    if text:
                        cards = generate_cards(api_key, text, 10)
                        for card in cards:
                            st.write(f"**Front:** {card['front']}")
                            st.write(f"**Back:** {card['back']}")
                            st.divider()
                        st.success("Done!")
                except Exception as e:
                    st.error(f"YouTube Blocked the Automatic Fetch: {e}")
                    st.info("Since the auto-fetch failed, use the 'Show Transcript' button on YouTube and paste the text below.")
                    manual_text = st.text_area("Paste Transcript manually:")
                    if st.button("Generate from Paste"):
                         cards = generate_cards(api_key, manual_text, 10)
                         st.json(cards)

if __name__ == "__main__":
    main()
