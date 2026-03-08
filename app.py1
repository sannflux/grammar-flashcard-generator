import streamlit as st
import pandas as pd
from io import StringIO
import tempfile
import os
import time
from datetime import datetime
from google import genai
from google.genai import types

# ====================== CONSTANTS & PROMPTS ======================
MODEL_NAME = "gemini-2.5-flash-lite"

SYSTEM_INSTRUCTION = """
You are an expert academic assistant specializing in creating high-quality, dense, and engaging flashcards.

Your output MUST follow every rule below:

FORMAT RULES:
1. Output MUST be STRICT pipe-separated flashcards.
2. The FIRST LINE must be EXACTLY: Question|Answer
3. Every following line must contain exactly ONE card.
4. NO markdown, NO code fences, NO explanations outside the flashcards.

HTML RULES:
- The Answer field MUST use ONLY <b> and <i> tags.
- Use <b> frequently to highlight key concepts, processes, or definitions.
- NEVER use *, **, _, #, or any markdown formatting.

QUESTION CLEANLINESS RULE:
- The Question field must NOT contain ANY markdown symbols, including *, **, _, #, or backticks.

DUPLICATE PREVENTION:
- You must NOT generate any card where the Question field is literally "Question" OR the Answer field is literally "Answer". If it would appear, rewrite it immediately.
- Every flashcard MUST be unique. No rephrased or repeated cards.

QUESTION VARIETY RULE:
- You MUST use a balanced mix of question types: Explain, Why, How, Compare, Identify, Describe, Define, What-if.

ANSWER QUALITY RULE:
- Answers MUST be dense, high-value, and concept-focused.
- Avoid vague or generic responses.
- Maximum of 3 short sentences OR 3 bullet points.
- Examples in the Answer MUST strictly match any constraints stated in the Question.

CONCEPT LINKING RULE:
- When appropriate, highlight relationships between concepts using <i>italic</i> phrasing.

FOCUS RULE:
- Each card must focus on ONE meaningful concept only.
- No multi-topic questions.
"""

TASK_PROMPT = """
Analyze the content of the uploaded study notes thoroughly and generate a large, comprehensive set of flashcards.

STRICT GENERATION RULES:
1. You must generate between 8 and 20 flashcards.
2. Every question must require active recall (Explain, Why, How, Compare, etc.)
3. Answers must be short, synthesized, and formatted ONLY with <b> or <i> tags.
4. NO markdown symbols of any kind. NO asterisks.
5. Pipe-separated format only.

IMPORTANT: DO NOT produce a card where the Question is literally "Question" or the Answer is literally "Answer".
"""

# ====================== PAGE CONFIG ======================
st.set_page_config(
    page_title="Grammar Flashcards Pro", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# ====================== CSS STYLING ======================
st.markdown("""
<style>
    .stTextArea textarea {font-size: 16px;}
    div[data-testid="stMetricValue"] {font-size: 24px;}
</style>
""", unsafe_allow_html=True)

# ====================== HELPER FUNCTIONS ======================
def clean_flashcard_data(csv_text):
    """
    Cleans the raw LLM response to ensure robust CSV parsing.
    Removes chatty intros/outros and enforces pipe separation.
    """
    lines = csv_text.strip().split('\n')
    cleaned_lines = []
    start_found = False
    
    for line in lines:
        # Detect the header or the start of data
        if "Question|Answer" in line:
            start_found = True
            cleaned_lines.append(line)
            continue
        if start_found and "|" in line:
            cleaned_lines.append(line)
            
    return "\n".join(cleaned_lines) if cleaned_lines else csv_text

# ====================== SIDEBAR ======================
with st.sidebar:
    st.header("⚙️ Configuration")
    
    with st.expander("🔑 API Settings", expanded=True):
        api_key = st.text_input(
            "Gemini API Key", 
            type="password", 
            help="Get it free at https://aistudio.google.com",
            value=os.environ.get("GEMINI_API_KEY", "") # Allow env var fallback
        )
        if api_key:
            st.caption("✅ API Key detected")
    
    with st.expander("🛠️ Export Settings", expanded=False):
        deck_name = st.text_input("Deck Name", value="Grammar_Cards")
        separator = st.selectbox("CSV Separator", options=["| (Pipe)", ", (Comma)", "\\t (Tab)"], index=0)
        
        # Map selection to actual character
        sep_char = "|" if "Pipe" in separator else "," if "Comma" in separator else "\t"

    st.divider()
    st.markdown("### ℹ️ About")
    st.info(
        "**Grammar Flashcards Pro**\n\n"
        "Upload images/PDFs or paste text to generate active-recall flashcards.\n\n"
        "Features:\n"
        "- Auto-cleanup of markdown\n"
        "- Editable results table\n"
        "- Instant preview mode"
    )

# ====================== MAIN UI ======================
st.title("📚 Grammar Flashcard Generator")
st.markdown("Turn your **notes, images, or PDFs** into Anki-ready flashcards in seconds.")

# --- Input Section ---
input_tab1, input_tab2 = st.tabs(["📸 Upload File", "📝 Paste Text"])

with input_tab1:
    uploaded_file = st.file_uploader(
        "Choose PNG, JPG, or PDF", 
        type=["png", "jpg", "jpeg", "pdf"],
        help="Upload clear images or text-based PDFs for best results."
    )
    if uploaded_file:
        file_details = {"FileName": uploaded_file.name, "FileType": uploaded_file.type, "FileSize": f"{uploaded_file.size / 1024:.2f} KB"}
        st.write(file_details)

with input_tab2:
    pasted_text = st.text_area(
        "Paste your notes here", 
        height=200, 
        placeholder="Paste grammar rules, vocabulary lists, or lecture notes..."
    )

# --- Action Button ---
col1, col2 = st.columns([1, 4])
with col1:
    generate_btn = st.button("🚀 Generate Cards", type="primary", use_container_width=True)

# ====================== GENERATION LOGIC ======================
if generate_btn:
    if not api_key:
        st.error("Please enter your Gemini API Key in the sidebar.")
        st.stop()
    
    if not uploaded_file and not pasted_text.strip():
        st.warning("Please upload a file OR paste text notes first.")
        st.stop()

    # Reset state for new generation
    if "flashcards" in st.session_state:
        del st.session_state["flashcards"]
        
    status_container = st.status("Initializing...", expanded=True)
    
    gemini_file_ref = None
    client = None

    try:
        # 1. Initialize Client (Securely)
        client = genai.Client(api_key=api_key)
        
        # 2. Prepare Content
        contents = []
        
        if uploaded_file:
            status_container.write(f"📤 Uploading {uploaded_file.name}...")
            
            # Create a safer temp file using suffix
            suffix = os.path.splitext(uploaded_file.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uploaded_file.getbuffer())
                tmp_path = tmp.name
            
            # Upload to Gemini
            gemini_file_ref = client.files.upload(file=tmp_path)
            contents.append(gemini_file_ref)
            
            # Clean local temp file immediately
            os.unlink(tmp_path)
            status_container.write("✅ File uploaded to Gemini.")
            
            # Wait for processing if it's a PDF (good practice)
            if "pdf" in uploaded_file.type:
                time.sleep(2) 
        else:
            contents.append(pasted_text)

        # 3. Append Prompts
        contents.append(TASK_PROMPT)

        # 4. Generate Content
        status_container.write("🤖 AI is analyzing and generating cards...")
        
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.2 # Lower temp for more consistent formatting
            )
        )
        
        # 5. Parse Response
        status_container.write("🧹 Cleaning and formatting data...")
        
        cleaned_csv = clean_flashcard_data(response.text)
        
        df_flashcards = pd.read_csv(
            StringIO(cleaned_csv),
            sep='|',
            engine='python',
            on_bad_lines='skip'
        )
        
        # Ensure columns exist (basic validation)
        if len(df_flashcards.columns) < 2:
             # Fallback: try comma if pipe failed, though prompt enforces pipe
             df_flashcards = pd.read_csv(StringIO(cleaned_csv), sep=',', on_bad_lines='skip')

        df_flashcards.columns = ['Question', 'Answer'] # Force headers

        # 6. Post-Processing (HTML & Cleanup)
        df_flashcards = df_flashcards.apply(lambda col: col.str.strip() if col.dtype == "object" else col)
        
        # Markdown to HTML conversion
        df_flashcards["Answer"] = (
            df_flashcards["Answer"]
            .astype(str)
            .str.replace(r"\*\*(.*?)\*\*", r"<b>\1</b>", regex=True)
            .str.replace(r"\*(.*?)\*", r"<i>\1</i>", regex=True)
        )

        # Remove "Question/Answer" header rows if they snuck in
        df_flashcards = df_flashcards[
            ~((df_flashcards['Question'].str.lower() == 'question') & 
              (df_flashcards['Answer'].str.lower() == 'answer'))
        ]
        
        # Remove empty rows
        df_flashcards = df_flashcards.dropna(subset=['Question', 'Answer'])
        
        # Save to Session State
        st.session_state["flashcards"] = df_flashcards
        
        status_container.update(label="✅ Success! Flashcards generated.", state="complete", expanded=False)
        st.toast(f"Generated {len(df_flashcards)} cards!", icon="🎉")

    except Exception as e:
        status_container.update(label="❌ Error occurred", state="error")
        st.error(f"An error occurred: {str(e)}")
        
    finally:
        # --- CRITICAL: CLEANUP ---
        # Delete the file from Google's servers to save storage and privacy
        if gemini_file_ref and client:
            try:
                client.files.delete(name=gemini_file_ref.name)
                print(f"Deleted remote file: {gemini_file_ref.name}")
            except Exception as cleanup_error:
                print(f"Warning: Could not delete remote file: {cleanup_error}")

# ====================== RESULTS DISPLAY ======================
if "flashcards" in st.session_state:
    df = st.session_state["flashcards"]
    
    st.divider()
    
    # --- Metrics ---
    m1, m2, m3 = st.columns(3)
    m1.metric("Cards Generated", len(df))
    m2.metric("Format", "Anki-Ready (HTML)")
    m3.metric("Source", "Uploaded File" if uploaded_file else "Text Input")

    # --- Output Tabs ---
    out_tab1, out_tab2, out_tab3 = st.tabs(["✏️ Edit Data", "👀 Card Preview", "📥 Download"])

    with out_tab1:
        st.caption("Double-click any cell to edit content before downloading.")
        edited_df = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "Question": st.column_config.TextColumn("Front (Question)", width="medium"),
                "Answer": st.column_config.TextColumn("Back (Answer HTML)", width="large"),
            }
        )
        # Update session state with edits
        st.session_state["flashcards"] = edited_df

    with out_tab2:
        st.caption("Navigate through your generated cards.")
        
        if not edited_df.empty:
            # Use a session state index for the carousel
            if "card_index" not in st.session_state:
                st.session_state.card_index = 0
                
            col_prev, col_card, col_next = st.columns([1, 6, 1])
            
            with col_prev:
                if st.button("⬅️", use_container_width=True):
                    st.session_state.card_index = max(0, st.session_state.card_index - 1)
            
            with col_next:
                if st.button("➡️", use_container_width=True):
                    st.session_state.card_index = min(len(edited_df) - 1, st.session_state.card_index + 1)
            
            # Display current card
            current_card = edited_df.iloc[st.session_state.card_index]
            
            with col_card:
                with st.container(border=True):
                    st.markdown(f"**Card {st.session_state.card_index + 1} / {len(edited_df)}**")
                    st.markdown("---")
                    st.markdown(f"### {current_card['Question']}")
                    st.markdown("---")
                    st.markdown(current_card['Answer'], unsafe_allow_html=True)
        else:
            st.info("No cards to display.")

    with out_tab3:
        st.subheader("Ready to Export?")
        st.write(f"Filename: `{deck_name}_{datetime.now().strftime('%Y%m%d')}.csv`")
        
        # Convert DF to CSV with selected separator
        csv = edited_df.to_csv(index=False, header=False, sep=sep_char)
        
        st.download_button(
            label="📥 Download Flashcards (.csv)",
            data=csv,
            file_name=f"{deck_name}_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv",
            mime="text/csv",
            type="primary"
        )
        
        st.info("💡 **Tip:** When importing into Anki, ensure you select the correct separator in the import dialog.")
