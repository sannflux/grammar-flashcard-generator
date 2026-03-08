import streamlit as st
import google.generativeai as genai
import pandas as pd
import json
import datetime
import random
import re
import requests

# --- ROBUST IMPORTS & ERROR HANDLING ---
# We wrap optional dependencies in try/except blocks to prevent immediate crashes
# and provide clear UI warnings instead.

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    HAS_YOUTUBE = True
except ImportError:
    HAS_YOUTUBE = False

try:
    from PIL import Image
    HAS_IMAGE = True
except ImportError:
    HAS_IMAGE = False

# --- CONFIGURATION ---
st.set_page_config(page_title="AI Flashcards Ultimate", page_icon="🧠", layout="wide")

# --- SESSION STATE SETUP ---
if "flashcards" not in st.session_state:
    st.session_state.flashcards = []
# Ensure a default 'next_review' exists for old cards without it
for card in st.session_state.flashcards:
    if "next_review" not in card:
        card["next_review"] = datetime.date.today().isoformat()
    if "box" not in card:
        card["box"] = 1

if "current_card_index" not in st.session_state:
    st.session_state.current_card_index = 0
if "flipped" not in st.session_state:
    st.session_state.flipped = False
if "api_key_configured" not in st.session_state:
    st.session_state.api_key_configured = False

# --- HELPER FUNCTIONS ---

def get_next_review_date(box_level):
    """Calculates the next review date based on the Leitner box level."""
    intervals = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
    days = intervals.get(box_level, 30)
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

def extract_json_from_text(text):
    """ extracts JSON from markdown code blocks if present """
    try:
        # Try finding JSON inside ```json ... ``` or just [...]
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return json.loads(text)
    except json.JSONDecodeError:
        return []

def fetch_youtube_transcript(url):
    """Extracts transcript from a YouTube video."""
    if not HAS_YOUTUBE:
        return "Error: `youtube-transcript-api` is not installed."
    
    try:
        video_id = url.split("v=")[-1].split("&")[0]
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        text = " ".join([t['text'] for t in transcript_list])
        return text
    except Exception as e:
        return f"Error fetching YouTube transcript: {str(e)}"

def fetch_web_content(url):
    """Scrapes text from a general website."""
    if not HAS_BS4:
        return "Error: `beautifulsoup4` is not installed."
    
    try:
        # Added User-Agent to mimic a real browser and avoid 403 errors (e.g., Wikipedia)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Kill all script and style elements
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.decompose()
            
        text = soup.get_text(separator=' ')
        
        # Break into lines and remove leading and trailing space on each
        lines = (line.strip() for line in text.splitlines())
        # Break multi-headlines into a line each
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        # Drop blank lines
        text = '\n'.join(chunk for chunk in chunks if chunk)
        
        return text[:15000] # Limit to 15k chars to save tokens
    except Exception as e:
        return f"Error scraping website: {str(e)}"

def generate_flashcards(source_text, content_type="text"):
    """Calls Gemini API to generate flashcards."""
    if not st.session_state.api_key_configured:
        st.error("Please configure your Gemini API Key first!")
        return []

    prompt = f"""
    You are an expert tutor. Create a JSON list of flashcards based on the {content_type} provided below.
    
    The JSON structure must be:
    [
        {{"front": "Question or Concept", "back": "Answer or Explanation"}}
    ]

    - Focus on key concepts, definitions, and relationships.
    - Keep answers concise (under 2 sentences).
    - Create 5-10 cards.
    - Return ONLY the raw JSON array. No markdown formatting.

    Here is the {content_type}:
    {source_text}
    """

    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Handling image vs text inputs
        if content_type == "image":
             # source_text is actually the PIL Image object here
            response = model.generate_content([prompt, source_text])
        else:
            response = model.generate_content(prompt)
            
        return extract_json_from_text(response.text)
    except Exception as e:
        st.error(f"Gemini API Error: {str(e)}")
        return []

# --- SIDEBAR: SETTINGS & INPUTS ---
with st.sidebar:
    st.header("⚙️ Settings")
    
    api_key = st.text_input("Gemini API Key", type="password")
    if api_key:
        genai.configure(api_key=api_key)
        st.session_state.api_key_configured = True
        st.success("API Key Active ✅")
    else:
        st.warning("Enter API Key to start.")

    st.markdown("---")
    st.header("📥 Create Cards")
    
    input_method = st.radio("Source:", ["Text / Notes", "Image / Screenshot", "YouTube URL", "Web Article URL"])
    
    source_content = None
    process_button = False

    if input_method == "Text / Notes":
        source_content = st.text_area("Paste your notes here:", height=150)
        process_button = st.button("Generate from Text")

    elif input_method == "Image / Screenshot":
        if HAS_IMAGE:
            uploaded_file = st.file_uploader("Upload an image of your notes", type=["jpg", "png", "jpeg"])
            if uploaded_file:
                image = Image.open(uploaded_file)
                st.image(image, caption="Uploaded Notes", use_container_width=True)
                source_content = image # We pass the object directly
                process_button = st.button("Generate from Image")
        else:
            st.error("⚠️ Pillow (PIL) library missing. Please add `Pillow` to requirements.txt")

    elif input_method == "YouTube URL":
        if HAS_YOUTUBE:
            url_input = st.text_input("Paste YouTube Link:")
            if url_input:
                process_button = st.button("Generate from Video")
                if process_button:
                    with st.spinner("Transcribing video..."):
                        source_content = fetch_youtube_transcript(url_input)
                        if "Error" in source_content:
                            st.error(source_content)
                            source_content = None
        else:
            st.error("⚠️ `youtube-transcript-api` missing. Add it to requirements.txt")

    elif input_method == "Web Article URL":
        if HAS_BS4:
            url_input = st.text_input("Paste Article Link:")
            if url_input:
                process_button = st.button("Generate from Article")
                if process_button:
                    with st.spinner("Scraping website..."):
                        source_content = fetch_web_content(url_input)
                        if "Error" in source_content:
                            st.error(source_content)
                            source_content = None
        else:
            st.error("⚠️ `beautifulsoup4` or `requests` missing. Add them to requirements.txt")

    if process_button and source_content:
        with st.spinner("AI is thinking..."):
            # Determine content type string for the prompt
            ctype = "image" if input_method == "Image / Screenshot" else "text"
            
            new_cards = generate_flashcards(source_content, content_type=ctype)
            
            if new_cards:
                # Add default Leitner fields
                for card in new_cards:
                    card['box'] = 1
                    card['next_review'] = datetime.date.today().isoformat()
                    
                st.session_state.flashcards.extend(new_cards)
                st.success(f"Generated {len(new_cards)} new cards!")
                st.rerun()

    st.markdown("---")
    st.metric("Total Cards", len(st.session_state.flashcards))
    if st.button("Clear All Cards"):
        st.session_state.flashcards = []
        st.rerun()

# --- MAIN AREA: REVIEW SYSTEM ---
st.title("🧠 AI Spaced Repetition Flashcards")

# Filter cards for today
today_str = datetime.date.today().isoformat()
due_cards = [
    (i, card) for i, card in enumerate(st.session_state.flashcards) 
    if card['next_review'] <= today_str
]

if not due_cards:
    st.info("🎉 You're all caught up! No cards due for review today.")
    
    if len(st.session_state.flashcards) > 0:
        with st.expander("View All Cards (Cheat Sheet)"):
            df = pd.DataFrame(st.session_state.flashcards)
            st.dataframe(df[['front', 'back', 'box', 'next_review']])
else:
    # Get current card data
    # We use a session index to track which *due card* we are looking at
    # Ensure index is within bounds of due_cards list
    if st.session_state.current_card_index >= len(due_cards):
        st.session_state.current_card_index = 0
        
    original_index, card_data = due_cards[st.session_state.current_card_index]

    st.markdown(f"**Reviewing Card {st.session_state.current_card_index + 1} of {len(due_cards)}**")
    
    # Progress Bar
    progress = (st.session_state.current_card_index) / len(due_cards)
    st.progress(progress)

    # Card Display UI
    card_container = st.container(border=True)
    
    with card_container:
        card_height = 300
        # CSS to center text vertically and horizontally
        st.markdown(
            f"""
            <div style="
                height: {card_height}px; 
                display: flex; 
                justify-content: center; 
                align-items: center; 
                text-align: center; 
                font-size: 24px; 
                font-weight: bold;
                background-color: #f0f2f6;
                border-radius: 10px;
                color: #31333F;
                padding: 20px;
            ">
                {card_data['back'] if st.session_state.flipped else card_data['front']}
            </div>
            """, 
            unsafe_allow_html=True
        )

    # Interaction Buttons
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        if not st.session_state.flipped:
            if st.button("Show Answer", use_container_width=True):
                st.session_state.flipped = True
                st.rerun()
        else:
            # Rating Buttons
            st.markdown("### How was it?")
            b1, b2, b3 = st.columns(3)
            
            with b1:
                if st.button("❌ Hard / Forgot", use_container_width=True):
                    # Reset to Box 1
                    st.session_state.flashcards[original_index]['box'] = 1
                    st.session_state.flashcards[original_index]['next_review'] = get_next_review_date(1)
                    
                    st.session_state.flipped = False
                    # Move to next due card (or loop)
                    if st.session_state.current_card_index < len(due_cards) - 1:
                        st.session_state.current_card_index += 1
                    else:
                        st.session_state.current_card_index = 0
                    st.rerun()
            
            with b2:
                if st.button("🆗 Good", use_container_width=True):
                    # Remain in same box (or slight bump? Standard Leitner usually bumps up)
                    # Let's bump box +1
                    current_box = st.session_state.flashcards[original_index]['box']
                    new_box = min(current_box + 1, 5)
                    st.session_state.flashcards[original_index]['box'] = new_box
                    st.session_state.flashcards[original_index]['next_review'] = get_next_review_date(new_box)

                    st.session_state.flipped = False
                    if st.session_state.current_card_index < len(due_cards) - 1:
                        st.session_state.current_card_index += 1
                    else:
                        st.session_state.current_card_index = 0
                    st.rerun()

            with b3:
                if st.button("✅ Easy", use_container_width=True):
                    # Jump 2 boxes (Bonus)
                    current_box = st.session_state.flashcards[original_index]['box']
                    new_box = min(current_box + 2, 5)
                    st.session_state.flashcards[original_index]['box'] = new_box
                    st.session_state.flashcards[original_index]['next_review'] = get_next_review_date(new_box)
                    
                    st.session_state.flipped = False
                    if st.session_state.current_card_index < len(due_cards) - 1:
                        st.session_state.current_card_index += 1
                    else:
                        st.session_state.current_card_index = 0
                    st.rerun()

