import streamlit as st
import pandas as pd
from io import StringIO
import tempfile
import os
from datetime import datetime
from google import genai

# ====================== PAGE CONFIG ======================
st.set_page_config(page_title="Grammar Flashcards", page_icon="📚", layout="wide")
st.title("📚 Grammar Flashcard Generator")
st.markdown("**Upload image/PDF or paste text** → Get perfect active-recall flashcards (Anki-ready)")

# ====================== SIDEBAR ======================
with st.sidebar:
    st.header("🔑 API Settings")
    api_key = st.text_input("Gemini API Key", type="password", help="Get it free at https://aistudio.google.com")
    model_name = "gemini-2.5-flash-lite"
    
    if api_key:
        os.environ["GEMINI_API_KEY"] = api_key
        st.success("✅ API key set")

# ====================== INPUT TABS ======================
tab1, tab2 = st.tabs(["📸 Upload Image or PDF", "📝 Paste Text Notes"])

with tab1:
    uploaded_file = st.file_uploader("Choose PNG, JPG, or PDF", type=["png", "jpg", "jpeg", "pdf"])

with tab2:
    pasted_text = st.text_area("Paste your grammar notes here", height=300, 
                               placeholder="Type or paste the full notes...")

# ====================== GENERATE BUTTON ======================
if st.button("🚀 Generate Flashcards", type="primary", use_container_width=True):
    if not api_key:
        st.error("Please enter your Gemini API Key in the sidebar")
        st.stop()
    
    if not uploaded_file and not pasted_text.strip():
        st.error("Please upload a file OR paste text notes")
        st.stop()

    with st.status("Uploading notes → Generating flashcards...", expanded=True) as status:
        try:
            client = genai.Client()

            # === Prepare contents (exact same as your Colab) ===
            if uploaded_file:
                status.update(label=f"Uploading {uploaded_file.name} to Gemini...")
                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp:
                    tmp.write(uploaded_file.getbuffer())
                    tmp_path = tmp.name
                
                gemini_file_ref = client.files.upload(file=tmp_path)
                contents = [gemini_file_ref]
                os.unlink(tmp_path)  # clean temp file
            else:
                contents = [pasted_text]

            # === YOUR ORIGINAL SYSTEM INSTRUCTION (100% unchanged) ===
            system_instruction = """
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

            # === YOUR ORIGINAL TASK PROMPT (100% unchanged) ===
            text_prompt = """
Analyze the content of the uploaded study notes thoroughly and generate a large, comprehensive set of flashcards.

STRICT GENERATION RULES:
1. You must generate between 8 and 20 flashcards.
2. Every question must require active recall (Explain, Why, How, Compare, etc.)
3. Answers must be short, synthesized, and formatted ONLY with <b> or <i> tags.
4. NO markdown symbols of any kind. NO asterisks.
5. Pipe-separated format only.

IMPORTANT: DO NOT produce a card where the Question is literally "Question" or the Answer is literally "Answer".
"""

            contents.append(text_prompt)

            status.update(label="Calling Gemini...")
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=genai.types.GenerateContentConfig(system_instruction=system_instruction)
            )

            # === YOUR ORIGINAL CLEANING LOGIC (unchanged) ===
            df_flashcards = pd.read_csv(
                StringIO(response.text),
                sep='|',
                engine='python',
                on_bad_lines='skip'
            )
            df_flashcards.columns = ['Question', 'Answer']

            df_flashcards = df_flashcards.apply(lambda col: col.str.strip() if col.dtype == "object" else col)

            # Markdown → HTML fix
            df_flashcards["Answer"] = (
                df_flashcards["Answer"]
                .str.replace(r"\*\*(.*?)\*\*", r"<b>\1</b>", regex=True)
                .str.replace(r"\*(.*?)\*", r"<i>\1</i>", regex=True)
            )

            # Remove bad rows
            df_flashcards = df_flashcards[
                ~((df_flashcards['Question'].str.lower() == 'question') & 
                  (df_flashcards['Answer'].str.lower() == 'answer'))
            ]
            df_flashcards = df_flashcards[df_flashcards['Question'].str.strip() != '']
            df_flashcards = df_flashcards[df_flashcards['Answer'].str.strip() != '']

            st.session_state["flashcards"] = df_flashcards
            status.update(label="✅ Done!", state="complete")

        except Exception as e:
            st.error(f"Error: {str(e)}")
            st.stop()

# ====================== RESULTS ======================
if "flashcards" in st.session_state:
    df = st.session_state["flashcards"]
    
    st.success(f"🎉 Generated {len(df)} high-quality flashcards!")
    
    st.subheader("Preview")
    for idx, row in df.iterrows():
        with st.expander(f"Q: {row['Question'][:80]}..."):
            st.markdown(f"**Question:** {row['Question']}")
            st.markdown(f"**Answer:** {row['Answer']}", unsafe_allow_html=True)
    
    # Download
    csv = df.to_csv(index=False, header=False)
    st.download_button(
        label="📥 Download Grammar_Cards.csv (ready for Anki/Quizlet)",
        data=csv,
        file_name=f"Grammar_Cards_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv",
        mime="text/csv"
    )

st.caption("Converted from your Colab notebook • Dual input (image + text) • Exact same card generation rules")
