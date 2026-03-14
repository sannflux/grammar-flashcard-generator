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
st.set_page_config(page_title="Flashcard Pro v7.1", layout="wide")

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY, deck_id INTEGER, front TEXT, back TEXT, 
            explanation TEXT, tag TEXT, next_review TEXT)''')
        conn.commit()

init_db()

# ====================== THE COOKIE TRANSLATOR ======================

def json_to_netscape():
    """Converts your Kiwi JSON cookies to the Netscape .txt format yt-dlp requires."""
    json_path = "youtube_cookies.json"
    txt_path = "youtube_cookies.txt"
    
    if not os.path.exists(json_path):
        return None

    try:
        with open(json_path, 'r') as f:
            cookies = json.load(f)
        
        with open(txt_path, 'w') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# This file is generated from JSON\n")
            for c in cookies:
                # Handle domain dots
                domain = c.get('domain', '')
                flag = "TRUE" if domain.startswith('.') else "FALSE"
                path = c.get('path', '/')
                secure = "TRUE" if c.get('secure') else "FALSE"
                # Expiry must be an integer
                expiry = int(c.get('expirationDate', 0))
                name = c.get('name', '')
                value = c.get('value', '')
                
                # Format: domain - flag - path - secure - expiry - name - value
                line = f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n"
                f.write(line)
        return txt_path
    except Exception as e:
        st.error(f"Cookie Conversion Failed: {e}")
        return None

# ====================== THE UPDATED BYPASS ENGINE ======================

def get_transcript_v7_1(video_id):
    """Uses converted Netscape cookies to get the transcript."""
    # Step 1: Convert the JSON you provided into the format YouTube wants
    cookie_file = json_to_netscape()
    
    if not cookie_file:
        raise ValueError("No youtube_cookies.json found in the folder!")

    import yt_dlp
    ydl_opts = {
        'skip_download': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en', 'id'],
        'quiet': True,
        'cookiefile': cookie_file, # Now using the .txt file!
        'no_warnings': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        
        sub_url = None
        # Check for manual subs first
        if 'subtitles' in info and info['subtitles']:
            for lang in ['en', 'id']:
                if lang in info['subtitles']:
                    sub_url = info['subtitles'][lang][0]['url']
                    break
        
        # Fallback to auto-captions
        if not sub_url and 'automatic_captions' in info:
            for lang in ['en', 'id']:
                if lang in info['automatic_captions']:
                    # Look for json3 format first
                    for fmt in info['automatic_captions'][lang]:
                        if fmt.get('ext') == 'json3' or 'fmt=json3' in fmt.get('url', ''):
                            sub_url = fmt['url']
                            break
                    if not sub_url:
                        sub_url = info['automatic_captions'][lang][0]['url']
                    break

        if sub_url:
            # Download the actual subtitle content
            resp = requests.get(sub_url)
            if resp.ok:
                if 'fmt=json3' in sub_url or '"events"' in resp.text:
                    data = resp.json()
                    return " ".join([s['utf8'] for e in data.get('events', []) for s in e.get('segs', []) if 'utf8' in s])
                else:
                    # Strip VTT tags
                    clean = re.sub(r'<[^>]+>', '', resp.text)
                    clean = re.sub(r'\d{2}:\d{2}.*', '', clean)
                    return clean

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
        contents=[f"Create {qty} flashcards from this text: {text}"],
        config=types.GenerateContentConfig(
            system_instruction="Provide educational flashcards in JSON format.",
            response_mime_type="application/json",
            response_schema=FlashcardSet
        )
    )
    return json.loads(response.text).get("cards", [])

def main():
    st.title("🚀 Flashcard Magic v7.1")
    
    api_key = st.sidebar.text_input("Gemini API Key", type="password")
    url = st.text_input("Paste YouTube Link")
    
    if st.button("Generate Automatically"):
        if not api_key:
            st.error("API Key is missing!")
            return
            
        vid_match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
        if vid_match:
            vid = vid_match.group(1)
            with st.spinner("Bypassing YouTube Wall..."):
                try:
                    text = get_transcript_v7_1(vid)
                    if text:
                        cards = generate_cards(api_key, text, 10)
                        for card in cards:
                            with st.container():
                                st.markdown(f"**Q:** {card['front']}")
                                st.markdown(f"**A:** {card['back']}")
                                st.caption(card['explanation'])
                                st.divider()
                        st.success("Success!")
                    else:
                        st.error("Could not find subtitles for this video.")
                except Exception as e:
                    st.error(f"Error: {e}")
                    st.info("YouTube is still blocking the automated request. Try running the app locally or paste the text manually.")
        else:
            st.error("Invalid URL")

if __name__ == "__main__":
    main()
