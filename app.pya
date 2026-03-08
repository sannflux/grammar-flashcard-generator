import streamlit as st
import pandas as pd
import tempfile
import os
import time
import json
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List

# ====================== 1. CONFIGURATION & STATE ======================
st.set_page_config(
    page_title="Flashcard Architect Pro", 
    page_icon="🧠", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize Session State
if "flashcards" not in st.session_state:
    st.session_state["flashcards"] = [] # List of dicts
if "card_index" not in st.session_state:
    st.session_state["card_index"] = 0
if "show_answer" not in st.session_state:
    st.session_state["show_answer"] = False

# ====================== 2. PYDANTIC SCHEMAS (STRUCTURED OUTPUT) ======================
class Flashcard(BaseModel):
    front: str = Field(description="The question or prompt for the flashcard. No markdown.")
    back: str = Field(description="The answer. Use HTML <b> and <i> tags for emphasis. No markdown.")
    tag: str = Field(description="A short, one-word category tag (e.g., #Syntax, #History).")

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

# ====================== 3. PROMPT ENGINEERING ======================
def build_system_instruction(difficulty, focus_area):
    return f"""
    You are an expert academic tutor designed to optimize student memory via active recall.
    
    TARGET AUDIENCE: {difficulty} level students.
    FOCUS AREA: {focus_area}
    
    Your task is to analyze the source text and generate specific, high-value flashcards.
    
    RULES:
    1. **Front (Question):** specific, unambiguous, and requires active thinking. Avoid "What is..." if possible. Use "Compare...", "Why...", "How...".
    2. **Back (Answer):** concise (max 3 sentences). Use HTML <b> for key terms and <i> for nuance. NO Markdown.
    3. **Tags:** Generate one relevant hashtag for filtering.
    4. **Output:** Return ONLY a valid JSON object matching the requested schema.
    """

# ====================== 4. CSS STYLING ======================
st.markdown("""
<style>
    /* Card Container Styling */
    .flashcard-container {
        background-color: #f9f9f9;
        border: 1px solid #ddd;
        border-radius: 10px;
        padding: 30px;
        box-shadow: 2px 2px 10px rgba(0,0,0,0.05);
        text-align: center;
        min-height: 250px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
    }
    .dark-mode .flashcard-container {
        background-color: #262730;
        border: 1px solid #444;
    }
    .card-front { font-size: 22px; font-weight: 600; color: #333; }
    .card-back { font-size: 20px; color: #555; margin-top: 15px; }
    .card-tag { 
        font-size: 12px; 
        color: #888; 
        background-color: #eee; 
        padding: 4px 8px; 
        border-radius: 12px;
        margin-bottom: 10px;
        display: inline-block;
    }
    /* Dark mode adjustments */
    @media (prefers-color-scheme: dark) {
        .card-front { color: #f0f2f6; }
        .card-back { color: #dce0e6; }
        .card-tag { background-color: #333; color: #aaa; }
    }
</style>
""", unsafe_allow_html=True)

# ====================== 5. SIDEBAR CONTROLS ======================
with st.sidebar:
    st.header("⚙️ Architect Controls")
    
    with st.expander("🔑 API Key", expanded=True):
        api_key = st.text_input(
            "Gemini API Key", 
            type="password",
            value=os.environ.get("GEMINI_API_KEY", ""),
            help="Required to access Google's Generative AI."
        )

    st.subheader("Generation Settings")
    difficulty = st.select_slider(
        "Target Level",
        options=["High School", "Undergraduate", "Masters/PhD"],
        value="Undergraduate"
    )
    
    card_count = st.selectbox(
        "Target Quantity",
        options=[5, 10, 20, 30, "Auto-Detect"],
        index=1,
        help="The AI will try to generate this many cards. 'Auto-Detect' depends on text length."
    )
    
    generation_mode = st.radio(
        "Workflow Mode",
        ["Append to Deck", "Replace Deck"],
        index=0,
        help="Append allows you to upload multiple files and build one large deck."
    )

    st.divider()
    st.info(f"**Current Deck Size:** {len(st.session_state['flashcards'])} cards")
    if st.button("🗑️ Clear Deck", type="secondary"):
        st.session_state["flashcards"] = []
        st.rerun()

# ====================== 6. MAIN UI ======================
st.title("🧠 Flashcard Architect Pro")
st.markdown("Transform documents into **structured, active-recall** datasets.")

# --- Input Tabs ---
tab_upload, tab_paste = st.tabs(["📂 Upload Documents", "✍️ Paste Text"])

with tab_upload:
    uploaded_file = st.file_uploader("Supports PDF, PNG, JPG", type=["pdf", "png", "jpg", "jpeg"])

with tab_paste:
    pasted_text = st.text_area("Paste raw notes", height=150)

# --- Action Zone ---
col_gen, col_status = st.columns([1, 2])
with col_gen:
    generate_btn = st.button("🚀 Generate Flashcards", type="primary", use_container_width=True)

# ====================== 7. LOGIC CORE ======================
if generate_btn:
    if not api_key:
        st.error("⚠️ API Key missing in sidebar.")
        st.stop()
    
    if not uploaded_file and not pasted_text.strip():
        st.warning("⚠️ No content provided.")
        st.stop()

    status_box = col_status.status("Initializing AI Architect...", expanded=True)
    
    try:
        client = genai.Client(api_key=api_key)
        
        # 1. Handle Inputs
        contents = []
        if uploaded_file:
            status_box.write(f"📤 Processing {uploaded_file.name}...")
            suffix = os.path.splitext(uploaded_file.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uploaded_file.getbuffer())
                tmp_path = tmp.name
            
            gemini_file = client.files.upload(file=tmp_path)
            contents.append(gemini_file)
            os.unlink(tmp_path) # Cleanup local
            if "pdf" in uploaded_file.type: time.sleep(2) # PDF processing buffer
        
        if pasted_text:
            contents.append(pasted_text)

        # 2. Build Dynamic Prompt
        count_instruction = f"Generate approximately {card_count} cards." if card_count != "Auto-Detect" else "Generate a comprehensive set covering all key concepts."
        
        full_prompt = f"""
        {count_instruction}
        Ensure the 'front' field is a clear question and 'back' is the answer.
        Focus on the following content.
        """
        contents.append(full_prompt)

        # 3. Call API with JSON Schema (The "Magic" Step)
        status_box.write("🤖 Architecting structure (JSON)...")
        
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=build_system_instruction(difficulty, "General Study"),
                response_mime_type="application/json",
                response_schema=FlashcardSet, # Pydantic enforcement
                temperature=0.3
            )
        )

        # 4. Parse & Update State
        try:
            # The SDK parses it into the Pydantic object automatically or returns a Dict structure
            # We access parsed objects carefully
            raw_json = json.loads(response.text)
            new_cards = raw_json.get("cards", [])
            
            if not new_cards:
                raise ValueError("AI returned empty JSON.")

            # Append or Replace Logic
            if generation_mode == "Replace Deck":
                st.session_state["flashcards"] = new_cards
            else:
                st.session_state["flashcards"].extend(new_cards)
            
            status_box.update(label="✅ Success!", state="complete", expanded=False)
            st.toast(f"Added {len(new_cards)} cards to deck!", icon="🎉")
            
        except json.JSONDecodeError:
            st.error("Failed to decode AI response. The model may have hallucinated.")
            status_box.update(label="❌ Formatting Error", state="error")

        # Cleanup Remote File
        if uploaded_file and gemini_file:
            client.files.delete(name=gemini_file.name)

    except Exception as e:
        st.error(f"System Error: {str(e)}")
        status_box.update(label="❌ System Error", state="error")

# ====================== 8. RESULTS DASHBOARD ======================
if st.session_state["flashcards"]:
    st.divider()
    
    # Convert list of dicts to DataFrame for easy editing
    df = pd.DataFrame(st.session_state["flashcards"])
    
    # --- Metric Header ---
    m1, m2, m3 = st.columns(3)
    m1.metric("Total Cards", len(df))
    m2.metric("Difficulty", difficulty)
    m3.metric("Last Update", datetime.now().strftime("%H:%M"))

    tab_preview, tab_edit, tab_export = st.tabs(["👀 Focus Mode (Preview)", "✏️ Editor", "📥 Export"])

    # --- TAB 1: FOCUS PREVIEW ---
    with tab_preview:
        if not df.empty:
            # Navigation
            col_back, col_prog, col_fwd = st.columns([1, 4, 1])
            
            # Ensure index is within bounds (safety check after deletions)
            if st.session_state.card_index >= len(df):
                st.session_state.card_index = 0

            with col_back:
                if st.button("⬅️ Prev", use_container_width=True):
                    st.session_state.card_index = max(0, st.session_state.card_index - 1)
                    st.session_state.show_answer = False # Reset flip
            
            with col_fwd:
                if st.button("Next ➡️", use_container_width=True):
                    st.session_state.card_index = min(len(df) - 1, st.session_state.card_index + 1)
                    st.session_state.show_answer = False # Reset flip

            # Display Card
            curr_card = df.iloc[st.session_state.card_index]
            
            with col_prog:
                st.progress((st.session_state.card_index + 1) / len(df))
                
            # The "Flip" Container
            with st.container(border=True):
                # Tag pill
                st.markdown(f"<span class='card-tag'>{curr_card['tag']}</span>", unsafe_allow_html=True)
                
                # Question (Always visible)
                st.markdown(f"<div class='card-front'>{curr_card['front']}</div>", unsafe_allow_html=True)
                
                st.markdown("---")
                
                # Answer Interaction
                if st.session_state.show_answer:
                    st.markdown(f"<div class='card-back'>{curr_card['back']}</div>", unsafe_allow_html=True)
                else:
                    if st.button("👁️ Reveal Answer", type="secondary", use_container_width=True):
                        st.session_state.show_answer = True
                        st.rerun()

    # --- TAB 2: EDITOR ---
    with tab_edit:
        st.caption("Edit text or delete rows. Changes auto-save to the 'Export' tab.")
        edited_df = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "front": st.column_config.TextColumn("Question", width="medium"),
                "back": st.column_config.TextColumn("Answer (HTML)", width="large"),
                "tag": st.column_config.TextColumn("Tag", width="small")
            }
        )
        # Sync edits back to session state list format
        st.session_state["flashcards"] = edited_df.to_dict('records')

    # --- TAB 3: EXPORT ---
    with tab_export:
        st.subheader("Download Options")
        
        # Anki Friendly CSV (Front, Back, Tag)
        # We need to ensure columns are in specific order for Anki default import
        export_df = edited_df[['front', 'back', 'tag']] if not edited_df.empty else pd.DataFrame()
        
        deck_name = st.text_input("Deck Filename", "Study_Deck")
        
        if not export_df.empty:
            csv = export_df.to_csv(index=False, header=False, sep="|")
            st.download_button(
                label="📥 Download for Anki (.csv)",
                data=csv,
                file_name=f"{deck_name}_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                type="primary"
            )
            st.info("ℹ️ **Anki Import Note:** Use `|` (Pipe) as the separator. Field mapping: Field 1 -> Front, Field 2 -> Back, Field 3 -> Tags.")
