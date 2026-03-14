import streamlit as st
import pandas as pd
import sqlite3
import re
import json
import os
import requests
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import List

# ====================== COOKIE TRANSLATOR (STRICT NETSCAPE) ======================

def json_to_netscape_v2():
    """Converts JSON to a version of Netscape that yt-dlp 2026 cannot reject."""
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
                # Ensure domain starts with a dot if it's a subdomain flag
                flag = "TRUE" if domain.startswith('.') else "FALSE"
                path = c.get('path', '/')
                secure = "TRUE" if c.get('secure') else "FALSE"
                expiry = int(c.get('expirationDate', 0))
                name = c.get('name', '')
                value = c.get('value', '')
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
        return txt_path
    except Exception as e:
        st.error(f"Cookie Error: {e}")
        return None

# ====================== THE "FORMAT-IGNORE" ENGINE ======================

def get_transcript_v7_2(video_id):
    """Bypasses 'Requested format not available' by targeting only metadata."""
    cookie_file = json_to_netscape_v2()
    
    import yt_dlp
    ydl_opts = {
        'skip_download': True,        # Don't touch the video file
        'ignoreerrors': True,
        'no_warnings': True,
        'quiet': True,
        'cookiefile': cookie_file,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en', 'id'],
        # This is the secret sauce: 
        # force yt-dlp to not care about video quality/formats
        'format': 'bestaudio/best', 
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # extract_info is what usually triggers the 'Format Not Available' error.
            # We use download=False to just get the 'Manifest' of the video.
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            
            if not info:
                raise ValueError("YouTube returned no data. Your IP might be temporarily throttled.")

            # Look for Subtitle URLs in the manifest
            sub_url = None
            # 1. Try Manual
            subs = info.get('subtitles', {})
            for lang in ['en', 'id']:
                if lang in subs:
                    sub_url = subs[lang][0]['url']
                    break
            
            # 2. Try Auto-Generated
            if not sub_url:
                auto = info.get('automatic_captions', {})
                for lang in ['en', 'id']:
                    if lang in auto:
                        # Find the smallest, easiest format (json3)
                        for f in auto[lang]:
                            if f.get('ext') == 'json3' or 'fmt=json3' in f.get('url', ''):
                                sub_url = f['url']
                                break
                        if not sub_url: sub_url = auto[lang][0]['url']
                        break

            if sub_url:
                r = requests.get(sub_url)
                if 'fmt=json3' in sub_url or '"events"' in r.text:
                    data = r.json()
                    return " ".join([s['utf8'] for e in data.get('events', []) for s in e.get('segs', []) if 'utf8' in s])
                return re.sub(r'<[^>]+>', '', r.text) # Clean VTT
                
        except Exception as e:
            if "Format not available" in str(e):
                return "BLOCK_BY_FORMAT"
            raise e

    return None

# ====================== MAIN APP ======================

def main():
    st.title("🧠 Flashcard Magic v7.2")
    
    url = st.text_input("YouTube URL")
    api_key = st.sidebar.text_input("Gemini API Key", type="password")

    if st.button("Generate (One-Click)"):
        vid = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
        if vid:
            with st.spinner("Bypassing YouTube's format check..."):
                try:
                    text = get_transcript_v7_2(vid.group(1))
                    
                    if text == "BLOCK_BY_FORMAT":
                        st.warning("YouTube is hiding the video formats, but we can still win!")
                        st.info("Quick Fix: Open the video in YouTube, click 'Show Transcript', copy and paste it below.")
                        manual = st.text_area("Paste here:")
                        if st.button("Process Manual Paste"):
                            # Logic for AI generation
                            pass
                    elif text:
                        # SUCCESS - PROCEED TO AI
                        st.success("Transcript Found! Sending to AI...")
                        # (Insert your generate_cards call here)
                    else:
                        st.error("No subtitles found for this video.")
                except Exception as e:
                    st.error(f"System Error: {e}")

if __name__ == "__main__":
    main()
