import streamlit as st
import google.generativeai as genai
import pandas as pd
import json
import requests
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from PIL import Image
import pypdf
import io

# --- Page Config ---
st.set_page_config(page_title="Flashcard Generator Pro", layout="wide", page_icon="🗂️")

# --- Session State Management ---
if 'generated_flashcards' not in st.session_state:
    st.session_state.generated_flashcards = []
if 'raw_text' not in st.session_state:
    st.session_state.raw_text = ""

# --- Helper Functions ---

def extract_text_from_url(url):
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.content, 'html.parser')
        # Extract text from paragraphs to avoid navigation/menus
        paragraphs = soup.find_all('p')
        return " ".join([p.get_text() for p in paragraphs])
    except Exception as e:
        return f"Error extracting URL: {e}"

def extract_text_from_youtube(video_url):
    try:
        video_id = video_url.split("v=")[-1].split("&")[0]
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([i['text'] for i in transcript])
    except Exception as e:
        return f"Error fetching YouTube transcript: {e}"

def extract_text_from_pdf(uploaded_file):
    try:
        reader = pypdf.PdfReader(uploaded_file)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        return f"Error reading PDF: {e}"

def generate_flashcards(api_key, text, count, tone, difficulty, card_type, source_lang=None, target_lang=None):
    if not api_key:
        st.error("Please enter your Google Gemini API Key.")
        return []

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')

        # Refined Prompt Logic based on Card Type
        if card_type == "Language Learning":
            prompt_context = f"Create {count} vocabulary flashcards for learning {target_lang} from {source_lang}. Include the word, pronunciation guide, and example sentence."
        elif card_type == "Code Syntax":
            prompt_context = f"Create {count} flashcards for coding concepts found in the text. Front: Concept/Question. Back: Code snippet or explanation. Format code in backticks."
        else: # Standard
            prompt_context = f"Create {count} high-quality flashcards based on the text provided below. Tone: {tone}. Difficulty: {difficulty}."

        prompt = f"""
        {prompt_context}

        Return the result strictly as a JSON list of objects. 
        Each object must have exactly two keys: "front" and "back".
        Do not wrap the JSON in markdown code blocks (like ```json). Just return the raw JSON list.

        Source Text:
        {text}
        """

        response = model.generate_content(prompt)
        cleaned_text = response.text.strip()
        # Remove markdown wrapping if the AI still adds it
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:]
        if cleaned_text.endswith("```"):
            cleaned_text = cleaned_text[:-3]
        
        return json.loads(cleaned_text)

    except Exception as e:
        st.error(f"Generation Error: {e}")
        return []

def get_flashcard_style():
    # CSS to force black text on the card, preventing Dark Mode issues
    return """
    <style>
    .flashcard {
        background-color: #fdf6e3; /* Solarized Light Beige */
        color: #000000 !important; /* FORCE BLACK TEXT */
        border-radius: 15px;
        padding: 20px;
        margin: 10px 0;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        border-left: 5px solid #d33682; /* Pink Accent */
        font-family: 'Segoe UI', sans-serif;
    }
    .flashcard h4 {
        color: #d33682 !important;
        margin-bottom: 5px;
        font-size: 0.9em;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    .flashcard p {
        font-size: 1.1em;
        line-height: 1.5;
        margin-bottom: 10px;
        color: #333333 !important;
    }
    .flashcard-divider {
        border-top: 1px dashed #ccc;
        margin: 10px 0;
    }
    </style>
    """

# --- Sidebar Configuration ---
with st.sidebar:
    st.header("⚙️ Configuration")
    
    # 1. API Key (Moved to top)
    api_key = st.text_input("Gemini API Key", type="password", placeholder="Paste key here...", help="Get from Google AI Studio")
    
    st.markdown("---")
    
    # 2. Input Method
    input_method = st.radio("Input Source", ["Text Paste", "Upload PDF", "Web Article URL", "YouTube Video"], index=0)

    st.markdown("---")
    
    # 3. Generation Settings
    num_cards = st.slider("Number of Cards", 3, 20, 5)
    card_type = st.selectbox("Card Focus", ["Standard (Q&A)", "Language Learning", "Code Syntax"])
    
    if card_type == "Language Learning":
        col1, col2 = st.columns(2)
        with col1:
            source_lang = st.text_input("From Lang", "English")
        with col2:
            target_lang = st.text_input("To Lang", "Spanish")
    else:
        source_lang, target_lang = None, None

    difficulty = st.select_slider("Difficulty", options=["Beginner", "Intermediate", "Advanced"], value="Intermediate")
    tone = st.selectbox("Tone", ["Academic", "Humorous", "Concise", "ELI5"])

# --- Main Content ---
st.title("⚡ AI Flashcard Generator")
st.markdown("Turn any content into study-ready flashcards in seconds.")

# --- Input Handling ---
input_data = ""

if input_method == "Text Paste":
    input_data = st.text_area("Paste your notes here:", height=200, placeholder="Paste text...")
elif input_method == "Web Article URL":
    url = st.text_input("Enter Article URL:")
    if url:
        if st.button("Fetch Article Text"):
            with st.spinner("Scraping website..."):
                input_data = extract_text_from_url(url)
                st.session_state.raw_text = input_data
                st.success("Text extracted successfully!")
    input_data = st.session_state.raw_text
elif input_method == "YouTube Video":
    yt_url = st.text_input("Enter YouTube URL:")
    if yt_url:
        if st.button("Fetch Transcript"):
            with st.spinner("Downloading transcript..."):
                input_data = extract_text_from_youtube(yt_url)
                st.session_state.raw_text = input_data
                st.success("Transcript extracted!")
    input_data = st.session_state.raw_text
elif input_method == "Upload PDF":
    uploaded_pdf = st.file_uploader("Upload PDF Document", type=["pdf"])
    if uploaded_pdf:
        with st.spinner("Reading PDF..."):
            input_data = extract_text_from_pdf(uploaded_pdf)
            st.success("PDF Loaded!")

# --- Generation Trigger ---
if st.button("Generate Flashcards", type="primary"):
    if not input_data:
        st.warning("Please provide some source text first.")
    else:
        with st.spinner("🤖 AI is crafting your flashcards..."):
            cards = generate_flashcards(api_key, input_data, num_cards, tone, difficulty, card_type, source_lang, target_lang)
            if cards:
                st.session_state.generated_flashcards = cards
                st.toast(f"Success! Generated {len(cards)} cards.", icon="✅")

# --- Results Display ---
if st.session_state.generated_flashcards:
    st.divider()
    st.subheader("📝 Review & Export")

    # Inject CSS Style
    st.markdown(get_flashcard_style(), unsafe_allow_html=True)

    # Display Cards
    for i, card in enumerate(st.session_state.generated_flashcards):
        # We construct the HTML string here
        card_html = f"""
        <div class="flashcard">
            <h4>Card {i+1} - Front</h4>
            <p>{card['front']}</p>
            <div class="flashcard-divider"></div>
            <h4>Back</h4>
            <p>{card['back']}</p>
        </div>
        """
        # CRITICAL FIX: Use st.markdown with unsafe_allow_html=True to render the HTML visually
        st.markdown(card_html, unsafe_allow_html=True)

    # --- CSV Export ---
    csv_data = pd.DataFrame(st.session_state.generated_flashcards).to_csv(index=False).encode('utf-8')
    
    col1, col2 = st.columns([1, 4])
    with col1:
        st.download_button(
            label="📥 Download CSV",
            data=csv_data,
            file_name="flashcards.csv",
            mime="text/csv"
        )
    with col2:
        st.info("Import this CSV directly into Anki, Quizlet, or Brainscape.")
