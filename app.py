import streamlit as st
import pandas as pd
from io import StringIO
import tempfile
import os
from datetime import datetime
from google import genai
import requests
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi

# ====================== PAGE CONFIG ======================
st.set_page_config(page_title="Grammar Flashcards Pro", page_icon="🎓", layout="wide")
st.title("🎓 Grammar Flashcard Generator Pro")
st.markdown("**Upload PDF, Paste Text, Scrape Web, or Summarize YouTube** → Get Anki-ready cards.")

# ====================== SESSION STATE INITIALIZATION ======================
if "flashcards" not in st.session_state:
    st.session_state["flashcards"] = pd.DataFrame()
if "source_text" not in st.session_state:
    st.session_state["source_text"] = ""
if "quiz_index" not in st.session_state:
    st.session_state["quiz_index"] = 0
if "show_answer" not in st.session_state:
    st.session_state["show_answer"] = False

# ====================== SIDEBAR ======================
with st.sidebar:
    st.header("🔑 API Settings")
    api_key = st.text_input("Gemini API Key", type="password", help="Get it free at https://aistudio.google.com")
    model_name = "gemini-2.5-flash-lite"
    
    if api_key:
        os.environ["GEMINI_API_KEY"] = api_key
        st.success("✅ API key set")

    st.divider()
    st.header("🎨 Card Style (Feature #8)")
    card_style = st.selectbox(
        "Choose Prompt Personality",
        ["Standard (Active Recall)", "True/False Mode", "Fill-in-the-Blank Mode"],
        help="Changes how the AI phrases the questions."
    )
    
    # Define style instructions
    style_prompts = {
        "Standard (Active Recall)": "Keep the standard Active Recall format (Explain, Define, Why).",
        "True/False Mode": "PHRASING RULE: Every Question must be a statement followed by 'True or False?'. The Answer must start with 'True' or 'False', followed by a 1-sentence explanation.",
        "Fill-in-the-Blank Mode": "PHRASING RULE: Every Question must be a sentence with a key word replaced by '______'. The Answer must be the missing word(s) only."
    }

# ====================== INPUT TABS ======================
tab1, tab2, tab3, tab4 = st.tabs(["📸 Image/PDF", "📝 Paste Text", "📺 YouTube URL", "🌐 Website URL"])

uploaded_file = None
pasted_text = ""
youtube_url = ""
web_url = ""

with tab1:
    uploaded_file = st.file_uploader("Upload PNG, JPG, or PDF", type=["png", "jpg", "jpeg", "pdf"])
with tab2:
    pasted_text = st.text_area("Paste notes here", height=150)
with tab3:
    youtube_url = st.text_input("Paste YouTube Link (Feature #5)")
with tab4:
    web_url = st.text_input("Paste Website Link (Feature #6)")

# ====================== HELPER FUNCTIONS ======================
def get_youtube_transcript(url):
    try:
        if "v=" in url:
            video_id = url.split("v=")[1].split("&")[0]
        elif "youtu.be/" in url:
            video_id = url.split("youtu.be/")[1].split("?")[0]
        else:
            return None, "Invalid YouTube URL format."
        
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        full_text = " ".join([entry['text'] for entry in transcript])
        return full_text, None
    except Exception as e:
        return None, str(e)

# ====================== UPDATED HELPER FUNCTIONS ======================


# ====================== GENERATE BUTTON ======================
if st.button("🚀 Generate Flashcards", type="primary", use_container_width=True):
    if not api_key:
        st.error("Please enter your Gemini API Key in the sidebar")
        st.stop()
    
    # Determine Source
    final_content = None
    source_type = "text" # or 'file'
    display_source_text = ""

    if uploaded_file:
        source_type = "file"
        display_source_text = f"File uploaded: {uploaded_file.name}"
    elif pasted_text.strip():
        final_content = pasted_text
        display_source_text = pasted_text
    elif youtube_url.strip():
        with st.status("Fetching YouTube transcript..."):
            text, err = get_youtube_transcript(youtube_url)
            if err:
                st.error(f"YouTube Error: {err}")
                st.stop()
            final_content = text
            display_source_text = f"YouTube Transcript from {youtube_url}:\n\n{text[:500]}..."
    elif web_url.strip():
        with st.status("Scraping website..."):
            text, err = get_website_text(web_url)
            if err:
                st.error(f"Web Scraping Error: {err}")
                st.stop()
            final_content = text
            display_source_text = f"Web content from {web_url}:\n\n{text[:500]}..."
    else:
        st.error("Please provide an input source.")
        st.stop()

    st.session_state["source_text"] = display_source_text

    with st.status("Processing with Gemini...", expanded=True) as status:
        try:
            client = genai.Client()
            contents = []

            if source_type == "file":
                status.update(label=f"Uploading {uploaded_file.name}...")
                with tempfidef get_website_text(url):
    try:
        # 1. Pretend to be a real browser to avoid getting blocked
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        # 2. Add a Timeout! (Stops it from hanging forever)
        response = requests.get(url, headers=headers, timeout=5) 
        response.raise_for_status()
        
        # 3. Use 'lxml' if installed (faster), else 'html.parser'
        try:
            soup = BeautifulSoup(response.content, 'lxml')
        except:
            soup = BeautifulSoup(response.content, 'html.parser')
        
        # 4. Remove junk elements
        for script in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "form", "button"]):
            script.decompose()

        # 5. improved Text Extraction strategy
        # Try to find the specific content block first
        content = soup.find('article') or soup.find('main') or soup.find('div', class_='content') or soup.body
        
        if not content:
            return None, "Could not find main content on this page."

        # Get text and clean it up
        text_content = content.get_text(separator=' ', strip=True)
        
        # Collapse multiple spaces into one
        import re
        clean_text = re.sub(r'\s+', ' ', text_content)

        # 6. Final validation
        if len(clean_text) < 300:
            return None, "Not enough text found. Are you using a Homepage URL? Please try a specific article link."
            
        return clean_text[:15000], None 
        
    except requests.exceptions.Timeout:
        return None, "The website took too long to respond. Try a different link."
    except Exception as e:
        return None, f"Scraping Error: {str(e)}"
le.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp:
                    tmp.write(uploaded_file.getbuffer())
                    tmp_path = tmp.name
                gemini_file_ref = client.files.upload(file=tmp_path)
                contents.append(gemini_file_ref)
                os.unlink(tmp_path)
            else:
                contents.append(final_content)

            # === SYSTEM INSTRUCTION (Original) ===
            base_system_instruction = """
You are an expert academic assistant specializing in creating high-quality, dense, and engaging flashcards.
Your output MUST follow every rule below:
FORMAT RULES:
1. Output MUST be STRICT pipe-separated flashcards.
2. The FIRST LINE must be EXACTLY: Question|Answer
3. Every following line must contain exactly ONE card.
4. NO markdown, NO code fences, NO explanations outside the flashcards.
HTML RULES:
- The Answer field MUST use ONLY <b> and <i> tags.
- Use <b> frequently to highlight key concepts.
QUESTION CLEANLINESS RULE:
- The Question field must NOT contain ANY markdown symbols.
DUPLICATE PREVENTION:
- No "Question" or "Answer" placeholders.
QUESTION VARIETY RULE:
- Mix of Explain, Why, How, Compare, etc.
"""

            # === TASK PROMPT (Modified for Feature #8) ===
            # We append the user selected style requirement to the prompt
            selected_style_instruction = style_prompts[card_style]
            
            text_prompt = f"""
Analyze the content thoroughly and generate flashcards.

STRICT GENERATION RULES:
1. You must generate between 8 and 20 flashcards.
2. Answers must be formatted ONLY with <b> or <i> tags.
3. Pipe-separated format only.

IMPORTANT STYLE OVERRIDE:
{selected_style_instruction}
"""
            contents.append(text_prompt)

            status.update(label="Generating...")
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=genai.types.GenerateContentConfig(system_instruction=base_system_instruction)
            )

            # === CLEANING LOGIC ===
            df_flashcards = pd.read_csv(StringIO(response.text), sep='|', engine='python', on_bad_lines='skip')
            df_flashcards.columns = ['Question', 'Answer']
            df_flashcards = df_flashcards.apply(lambda col: col.str.strip() if col.dtype == "object" else col)
            df_flashcards["Answer"] = df_flashcards["Answer"].str.replace(r"\*\*(.*?)\*\*", r"<b>\1</b>", regex=True).str.replace(r"\*(.*?)\*", r"<i>\1</i>", regex=True)
            df_flashcards = df_flashcards[~((df_flashcards['Question'].str.lower() == 'question') & (df_flashcards['Answer'].str.lower() == 'answer'))]
            
            st.session_state["flashcards"] = df_flashcards
            st.session_state["quiz_index"] = 0 # Reset quiz
            status.update(label="✅ Done!", state="complete")

        except Exception as e:
            st.error(f"Error: {str(e)}")
            st.stop()

# ====================== OUTPUT AREA ======================
if not st.session_state["flashcards"].empty:
    df = st.session_state["flashcards"]
    
    # === TABS FOR VIEWING ===
    view_tab1, view_tab2, view_tab3 = st.tabs(["📝 Editor & Download", "⚖️ Validation View (Feature #4)", "🧠 Quiz Mode (Feature #3)"])
    
    # --- TAB 1: EDITOR ---
    with view_tab1:
        st.subheader("Edit Flashcards")
        edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, height=400)
        
        csv = edited_df.to_csv(index=False, header=False)
        st.download_button(
            label="📥 Download CSV (Anki Ready)",
            data=csv,
            file_name=f"Flashcards_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv",
            mime="text/csv",
            type="primary"
        )

    # --- TAB 2: VALIDATION VIEW (Feature #4) ---
    with view_tab2:
        st.subheader("Source vs. Output Comparison")
        col_src, col_res = st.columns(2)
        with col_src:
            st.info("📜 Original Source Content")
            st.text_area("Source Text", value=st.session_state["source_text"], height=600, disabled=True)
        with col_res:
            st.success("🃏 Generated Cards")
            for i, row in df.iterrows():
                with st.expander(f"{i+1}. {row['Question']}", expanded=True):
                    st.markdown(row['Answer'], unsafe_allow_html=True)

    # --- TAB 3: QUIZ MODE (Feature #3) ---
    with view_tab3:
        st.subheader("🧠 Interactive Quiz")
        
        if len(df) > 0:
            current_idx = st.session_state["quiz_index"]
            current_card = df.iloc[current_idx]
            
            # Progress bar
            progress = (current_idx + 1) / len(df)
            st.progress(progress, text=f"Card {current_idx + 1} of {len(df)}")
            
            # Card Container
            with st.container(border=True):
                st.markdown(f"### Q: {current_card['Question']}")
                
                if st.session_state["show_answer"]:
                    st.divider()
                    st.markdown("### A:")
                    st.markdown(current_card['Answer'], unsafe_allow_html=True)
            
            # Controls
            c1, c2, c3 = st.columns([1, 1, 1])
            
            with c1:
                if st.button("⬅️ Previous"):
                    if current_idx > 0:
                        st.session_state["quiz_index"] -= 1
                        st.session_state["show_answer"] = False
                        st.rerun()

            with c2:
                if st.button("👀 Show/Hide Answer", type="primary"):
                    st.session_state["show_answer"] = not st.session_state["show_answer"]
                    st.rerun()

            with c3:
                if st.button("Next ➡️"):
                    if current_idx < len(df) - 1:
                        st.session_state["quiz_index"] += 1
                        st.session_state["show_answer"] = False
                        st.rerun()
        else:
            st.warning("No flashcards generated yet.")

