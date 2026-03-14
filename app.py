import streamlit as st
import pandas as pd
import sqlite3
import re
import requests
import urllib.parse
import math
import json
import time
import io
import os
import base64
import html
from datetime import datetime, timedelta

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional
from PIL import Image
from gtts import gTTS

# ====================== 1. UPDATED DEPENDENCIES ======================
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    YOUTUBE_AVAILABLE = True
except ImportError:
    YOUTUBE_AVAILABLE = False

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False

# ====================== 2. CONFIG & DB ======================
st.set_page_config(page_title="Flashcard Pro v6.6", page_icon="🧠", layout="wide")
DB_NAME = "flashcards_v5.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ====================== 3. THE "UNBLOCKABLE" EXTRACTOR ======================

def get_robust_youtube_transcript(video_id):
    """
    4-Stage Extraction Logic:
    Stage 1: Official API (Fastest)
    Stage 2: YT-DLP Bypass (Most resilient)
    Stage 3: Proxy Fallback (External service)
    Stage 4: User Manual Intervention
    """
    # STAGE 1: Standard API
    if YOUTUBE_AVAILABLE:
        try:
            # If you have a 'cookies.txt' in the app folder, use it!
            cookie_path = "cookies.txt" if os.path.exists("cookies.txt") else None
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id, cookies=cookie_path)
            transcript = transcript_list.find_transcript(['en', 'en-US'])
            text = " ".join([t['text'] for t in transcript.fetch()])
            if len(text) > 100: return text
        except Exception as e:
            print(f"Stage 1 failed: {e}")

    # STAGE 2: YT-DLP (The Heavy Hitter)
    if YTDLP_AVAILABLE:
        try:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['en.*'],
                'quiet': True,
                'no_warnings': True,
                # Injection of cookies.txt helps bypass bot detection
                'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                # Check if subtitles were found in the info dict
                if 'subtitles' in info and 'en' in info['subtitles']:
                    sub_url = info['subtitles']['en'][0]['url']
                    resp = requests.get(sub_url)
                    if resp.ok: return resp.text # Note: Might need simple regex cleaning
        except Exception as e:
            print(f"Stage 2 failed: {e}")

    # STAGE 3: Proxy Fallback (External Web Service)
    try:
        # Using a newer secondary proxy endpoint
        proxy_resp = requests.get(f"https://youtubetranscript.com/?server_vid2={video_id}", timeout=10)
        if '<transcript>' in proxy_resp.text:
            clean_text = re.sub(r'<[^>]+>', ' ', proxy_resp.text)
            clean_text = html.unescape(clean_text)
            return re.sub(r'\s+', ' ', clean_text).strip()
    except:
        pass
        
    raise ValueError("YouTube is currently blocking automated requests. Please paste the transcript manually below.")

# ====================== 4. GENERATOR UI (v6.6) ======================

def section_generator(api_key):
    st.header("🏭 Flashcard Factory v6.6")
    
    col_input, col_sets = st.columns([2, 1])
    content_text = ""
    
    with col_input:
        source_type = st.radio("Input Source", ["YouTube URL", "Text/Paste", "Web Article", "PDF"], horizontal=True)
        
        if source_type == "YouTube URL":
            url = st.text_input("Video URL")
            if url:
                video_id = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
                if video_id:
                    vid = video_id.group(1)
                    if st.button("🔍 Fetch Transcript"):
                        with st.spinner("Bypassing YouTube blocks..."):
                            try:
                                content_text = get_robust_youtube_transcript(vid)
                                st.success("Success! Transcript loaded.")
                            except Exception as e:
                                st.error(f"⚠️ {str(e)}")
                                # Manual Fallback UI
                                content_text = st.text_area("YouTube blocked us. Please copy/paste the transcript from the video manually here:", height=200)
                else:
                    st.error("Invalid URL")
        
        elif source_type == "Text/Paste":
            content_text = st.text_area("Paste Content", height=300)

    with col_sets:
        st.subheader("Config")
        deck_name = st.text_input("Deck Name", value="New Deck")
        qty = st.slider("Card Count", 5, 20, 10)
        
        if st.button("🚀 Generate via AI", type="primary"):
            if not content_text or len(content_text) < 50:
                st.warning("Content too short to generate quality cards.")
                return
            
            # (Rest of the generation logic from v6.5 goes here)
            st.info("Generating... (This uses the content_text gathered above)")

# ====================== 5. THE "REQUIREMENTS" UPDATE ======================
# Add these to your requirements.txt:
# yt-dlp
# youtube-transcript-api
# requests

def main():
    api_key = st.sidebar.text_input("Gemini API Key", type="password")
    page = st.sidebar.selectbox("Go to", ["Generator", "Study Mode", "Library"])
    
    if page == "Generator": section_generator(api_key)
    else: st.write("Remaining sections (Study/Library) same as v6.5")

if __name__ == "__main__":
    main()
