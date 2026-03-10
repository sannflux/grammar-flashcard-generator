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
import base64
import html
from datetime import datetime, timedelta
import altair as alt

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional
from PIL import Image

from gtts import gTTS
from tenacity import retry, stop_after_attempt, wait_exponential

# ====================== 1. SAFE IMPORTS & CONFIGURATION ======================
st.set_page_config(
    page_title="Flashcard Library Pro v6.5", 
    page_icon="🧠", 
    layout="wide",
    initial_sidebar_state="expanded"
)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

DEFAULT_STATE = {
    "current_deck_id": None,
    "study_queue": [],      
    "study_index": 0,       
    "show_answer": False,
    "session_stats": {"reviewed": 0, "correct": 0, "start_time": None},
    "cram_mode": False,
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ====================== 2. DATABASE ENGINE ======================
DB_NAME = "flashcards_v5.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute('''CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, description TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, deck_id INTEGER, front TEXT, back TEXT, 
            explanation TEXT, tag TEXT, ease_factor REAL DEFAULT 2.5, interval INTEGER DEFAULT 0,
            repetitions INTEGER DEFAULT 0, next_review TEXT DEFAULT CURRENT_DATE, last_reviewed TEXT,
            FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE)''')
        
        try: c.execute("SELECT explanation FROM cards LIMIT 1")
        except sqlite3.OperationalError: c.execute("ALTER TABLE cards ADD COLUMN explanation TEXT DEFAULT ''")
        try: c.execute("SELECT last_reviewed FROM cards LIMIT 1")
        except sqlite3.OperationalError: c.execute("ALTER TABLE cards ADD COLUMN last_reviewed TEXT")
        conn.commit()

init_db()

# ====================== 3. CORE LOGIC ======================
def update_card_sm2(card_id, quality):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT ease_factor, interval, repetitions FROM cards WHERE id=?", (card_id,))
        row = c.fetchone()
        if row:
            ease, interval, reps = row['ease_factor'], row['interval'], row['repetitions']
            if quality < 3:
                reps, interval = 0, 1
            else:
                interval = 1 if reps == 0 else (6 if reps == 1 else math.ceil(interval * ease))
                reps += 1
                ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))

            next_review = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")
            last_reviewed = datetime.now().strftime("%Y-%m-%d")

            c.execute('''UPDATE cards SET ease_factor=?, interval=?, repetitions=?, next_review=?, last_reviewed=? WHERE id=?''', 
                      (ease, interval, reps, next_review, last_reviewed, card_id))
            conn.commit()

def delete_deck(deck_name):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM decks WHERE name=?", (deck_name,))
        conn.commit()

def rename_deck(old_name, new_name):
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE decks SET name=? WHERE name=?", (new_name, old_name))
            conn.commit()
        return True
    except sqlite3.IntegrityError: return False

def get_due_cards_count():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM cards WHERE next_review <= ?", (today,)).fetchone()[0]

# ====================== 4. AI CONTENT ENGINE & EXTRACTORS ======================
class Flashcard(BaseModel):
    front: str = Field(description="The question/concept. Plain text.")
    back: str = Field(description="The answer. Use HTML <b> for key terms.")
    explanation: str = Field(description="A short context or mnemonic explaining WHY the answer is correct.")
    tag: str = Field(description="A short category tag.")

class FlashcardSet(BaseModel):
    cards: List[Flashcard]

def clean_text(text):
    if not text: return ""
    return re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text).strip()

def sanitize_json(text):
    text = re.sub(r'^```json', '', text, flags=re.MULTILINE)
    return re.sub(r'^```', '', text, flags=re.MULTILINE).strip()

def extract_pdf_text(uploaded_file):
    try:
        pdf_bytes = io.BytesIO(uploaded_file.getvalue())
        reader = PdfReader(pdf_bytes)
        text = "".join((page.extract_text() or "") + "\n" for page in reader.pages)
        return text[:25000], None 
    except Exception as e: 
        return None, f"Error reading PDF: {str(e)}"

def extract_youtube_id(url):
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.hostname == 'youtu.be':
        return parsed_url.path[1:]
    if parsed_url.hostname in ('www.youtube.com', 'youtube.com'):
        if parsed_url.path == '/watch':
            return urllib.parse.parse_qs(parsed_url.query).get('v', [None])[0]
        if parsed_url.path.startswith(('/shorts/', '/embed/', '/v/')):
            return parsed_url.path.split('/')[2]
    match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return match.group(1) if match else None

def get_internal_youtube_transcript(video_id):
    """
    Zero-Dependency, Bot-Wall Immune Extractor.
    Communicates directly with Google's mobile JSON API endpoint.
    """
    url = "https://www.youtube.com/youtubei/v1/player"
    headers = {"Content-Type": "application/json"}
    
    # Masquerade as an Android Client to bypass Web Bot Walls
    payload = {
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "17.31.35",
                "androidSdkVersion": 30,
                "hl": "en"
            }
        },
        "videoId": video_id
    }
    
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        data = resp.json()
        captions = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
        
        # Fallback to WEB client if Android fails
        if not captions:
            payload["context"]["client"]["clientName"] = "WEB"
            payload["context"]["client"]["clientVersion"] = "2.20240105.01.00"
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            data = resp.json()
            captions = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])

        if not captions:
            raise ValueError("No captions or auto-captions exist for this video.")

        # Prioritize English, fallback to the first available language
        track = next((c for c in captions if c.get("languageCode") == "en"), captions[0])
        xml_url = track.get("baseUrl")
        
        if not xml_url:
            raise ValueError("Caption URL could not be retrieved.")
            
        xml_resp = requests.get(xml_url, timeout=10)
        clean_text = re.sub(r'<[^>]+>', ' ', xml_resp.text)
        clean_text = html.unescape(clean_text)
        final_text = re.sub(r'\s+', ' ', clean_text).strip()
        
        if len(final_text) < 20:
            raise ValueError("Extracted text was blank or too short.")
            
        return final_text
        
    except Exception as e:
        raise ValueError(f"{str(e)}")

def clean_web_markdown(text):
    text = re.sub(r'<HTML.*?>.*?</HTML>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    lines = text.split('\n')
    clean_lines = [line for line in lines if "Access Denied" not in line and "edgesuite.net" not in line and "Reference #" not in line]
    text = '\n'.join(clean_lines)
    return re.sub(r'\n\s*\n', '\n\n', text).strip()

def fetch_web_content(url):
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", timeout=15)
        resp.raise_for_status()
        text = clean_web_markdown(resp.text)
        if not text or (text.count("Access Denied") > 5 and len(text) < 1000): raise ValueError("WAF")
        return text[:25000]
    except Exception:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        for s in soup(["script", "style", "nav", "footer", "header"]): s.decompose()
        return " ".join(soup.stripped_strings)[:25000]

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def generate_flashcards(api_key, text_content, image_content, difficulty, count_val):
    client = genai.Client(api_key=api_key)
    system_prompt = f"Act as a professor for {difficulty} level students. Create {count_val} flashcards strictly based on the core educational content provided. IGNORE website navigation menus, sidebars, 'Log in' prompts, and comment sections. Focus ONLY on the actual definitions, rules, or educational topics. Output JSON only. 'back' field MUST use <b>bold</b> tags for keywords."
    contents = []
    if text_content: contents.append(text_content)
    if image_content: contents.append(image_content)
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite", 
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt, response_mime_type="application/json", response_schema=FlashcardSet, temperature=0.3)
    )
    return json.loads(sanitize_json(response.text)).get("cards", [])

def text_to_speech_html(text):
    try:
        clean_text = re.sub(r'<[^>]+>', '', text)
        tts = gTTS(text=clean_text, lang='en')
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        b64 = base64.b64encode(fp.read()).decode()
        return f'<audio controls style="height: 30px; width: 100%; margin-top: 10px;"><source src="data:audio/mp3;base64,{b64}" type="audio/mp3"></audio>'
    except Exception: return ""

# ====================== 5. UI COMPONENTS & CSS ======================
def inject_custom_css():
    st.markdown("""
    <style>
        .flashcard { background-color: var(--secondary-background-color); border: 1px solid var(--text-color); border-radius: 15px; padding: 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 300px; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; margin-bottom: 20px; }
        .card-front { font-size: 24px; font-weight: 700; margin-bottom: 20px; color: var(--text-color); }
        .card-back { font-size: 18px; margin-bottom: 15px; color: var(--primary-color); line-height: 1.5; }
        .card-explanation { font-size: 14px; color: var(--text-color); opacity: 0.8; font-style: italic; border-top: 1px solid var(--text-color); padding-top: 10px; width: 100%; }
        .card-tag { background: var(--primary-color); color: #ffffff; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; margin-bottom: 15px; }
    </style>
    """, unsafe_allow_html=True)

# ====================== 6. APPLICATION SECTIONS ======================
def section_generator(api_key):
    st.header("🏭 Flashcard Factory")
    
    col_input, col_sets = st.columns([2, 1])
    content_text, image_content = "", None
    
    with col_input:
        source_type = st.radio("Input Source", ["Text/Paste", "Upload PDF", "Image Analysis", "YouTube URL", "Web Article"], horizontal=True)
        
        if source_type == "Text/Paste":
            content_text = st.text_area("Paste Notes Here", height=200)
        
        elif source_type == "Upload PDF":
            if PDF_AVAILABLE:
                pdf_file = st.file_uploader("Upload PDF Document", type=["pdf"])
                if pdf_file:
                    with st.spinner("Extracting..."):
                        raw_text, error_msg = extract_pdf_text(pdf_file)
                        if not error_msg:
                            st.success(f"PDF Extracted! ({len(raw_text)} chars)")
                            with st.expander("Preview & Edit", expanded=True):
                                content_text = st.text_area("Edit text:", raw_text, height=200)
                        else: st.error(error_msg)
            else: st.warning("Please install 'pypdf'")
            
        elif source_type == "Image Analysis":
            img_file = st.file_uploader("Upload Diagram", type=["png", "jpg", "jpeg"])
            if img_file:
                image_content = Image.open(img_file)
                st.image(image_content, width=300)
                content_text = "Generate flashcards based on this image."

        elif source_type == "YouTube URL":
            url = st.text_input("Video URL")
            if url:
                with st.spinner("Bypassing Bot Walls & Transcribing..."):
                    try:
                        video_id = extract_youtube_id(url)
                        if not video_id:
                            raise ValueError("Invalid YouTube URL.")
                        
                        # Use the Internal Google API to guarantee clean extraction
                        raw_text = get_internal_youtube_transcript(video_id)
                        
                        st.success("Transcript Extracted Successfully!")
                        with st.expander("Preview & Edit Transcript", expanded=True):
                            content_text = st.text_area("Edit text before generating:", raw_text, height=200)
                            
                    except Exception as e: 
                        st.error(f"Error extracting transcript: {str(e)}")

        elif source_type == "Web Article":
            url = st.text_input("Article URL")
            if url:
                with st.spinner("Fetching Webpage..."):
                    try:
                        raw_text = fetch_web_content(url)
                        st.success("Web Content Extracted!")
                        with st.expander("Preview & Edit", expanded=True):
                            content_text = st.text_area("Edit text:", raw_text, height=200)
                    except Exception as e: st.error(str(e))

    with col_sets:
        st.subheader("Config")
        deck_name = st.text_input("Deck Name", placeholder="e.g., Biology 101")
        difficulty = st.select_slider("Level", ["Beginner", "Intermediate", "Expert"], value="Intermediate")
        qty = st.number_input("Count", 1, 30, 10)
        
        if st.button("🚀 Generate via AI", type="primary", use_container_width=True):
            if not api_key: st.error("API Key Missing"); return
            if not (content_text or image_content): st.warning("No valid content"); return
            
            with st.spinner("Gemini is thinking..."):
                try:
                    cards = generate_flashcards(api_key, content_text, image_content, difficulty, qty)
                    if cards and deck_name:
                        with get_db_connection() as conn:
                            c = conn.cursor()
                            c.execute("INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)", (deck_name, datetime.now().strftime("%Y-%m-%d")))
                            deck_id = c.execute("SELECT id FROM decks WHERE name=?", (deck_name,)).fetchone()[0]
                            data = [(deck_id, clean_text(c['front']), clean_text(c['back']), c.get('explanation', ''), c['tag']) for c in cards]
                            c.executemany("INSERT INTO cards (deck_id, front, back, explanation, tag) VALUES (?, ?, ?, ?, ?)", data)
                            conn.commit()
                        st.success(f"Created {len(cards)} cards!")
                except Exception as e: st.error(f"Failed: {e}")

    with st.expander("✍️ Create Flashcard Manually"):
        with st.form("manual_card", clear_on_submit=True):
            with get_db_connection() as conn:
                existing_decks = [d['name'] for d in conn.execute("SELECT name FROM decks").fetchall()]
            m_deck = st.selectbox("Select Deck", existing_decks) if existing_decks else st.text_input("New Deck Name")
            m_front = st.text_area("Front (Question)")
            m_back = st.text_area("Back (Answer)")
            m_exp = st.text_input("Explanation (Optional)")
            m_tag = st.text_input("Tag", "Manual")
            
            if st.form_submit_button("Save Card"):
                if m_deck and m_front and m_back:
                    with get_db_connection() as conn:
                        c = conn.cursor()
                        c.execute("INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)", (m_deck, datetime.now().strftime("%Y-%m-%d")))
                        d_id = c.execute("SELECT id FROM decks WHERE name=?", (m_deck,)).fetchone()[0]
                        c.execute("INSERT INTO cards (deck_id, front, back, explanation, tag) VALUES (?, ?, ?, ?, ?)", (d_id, m_front, m_back, m_exp, m_tag))
                        conn.commit()
                    st.success("Card added!")
                else: st.error("Missing fields")

def cb_show_answer(): st.session_state["show_answer"] = True
def cb_submit_review(score, card_id):
    if not st.session_state["cram_mode"]: update_card_sm2(card_id, score)
    st.session_state["session_stats"]["reviewed"] += 1
    if score >= 3: st.session_state["session_stats"]["correct"] += 1
    st.session_state["study_index"] += 1
    st.session_state["show_answer"] = False

def section_study():
    st.header("🧘 Zen Study Mode")
    with get_db_connection() as conn:
        decks = conn.execute("SELECT id, name FROM decks").fetchall()
    if not decks: st.info("No decks."); return

    col_deck, col_mode = st.columns([3, 1])
    with col_deck:
        deck_opts = {d['name']: d['id'] for d in decks}
        selected_deck = st.selectbox("Select Deck", list(deck_opts.keys()))
        deck_id = deck_opts[selected_deck]
    
    with col_mode:
        st.write("") 
        cram = st.toggle("🔥 Cram Mode", value=st.session_state["cram_mode"])
        if cram != st.session_state["cram_mode"]:
            st.session_state["cram_mode"] = cram
            st.session_state["current_deck_id"] = None 
            st.rerun()

    if st.session_state["current_deck_id"] != deck_id:
        st.session_state["current_deck_id"] = deck_id
        with get_db_connection() as conn:
            if st.session_state["cram_mode"]:
                cards = conn.execute("SELECT * FROM cards WHERE deck_id = ? ORDER BY RANDOM() LIMIT 50", (deck_id,)).fetchall()
            else:
                cards = conn.execute("SELECT * FROM cards WHERE deck_id = ? AND next_review <= ? ORDER BY next_review ASC LIMIT 50", (deck_id, datetime.now().strftime("%Y-%m-%d"))).fetchall()
            
            st.session_state["study_queue"] = [dict(c) for c in cards]
            st.session_state["study_index"], st.session_state["show_answer"] = 0, False
            st.session_state["session_stats"] = {"reviewed": 0, "correct": 0, "start_time": time.time()}

    queue, idx = st.session_state["study_queue"], st.session_state["study_index"]
    if not queue: st.success("All caught up!"); return

    if idx < len(queue):
        card = queue[idx]
        st.progress((idx + 1) / len(queue), text=f"Card {idx+1}/{len(queue)}")
        
        audio_html, back_content, explanation_html = "", "<span style='opacity:0.6;'>(Think...)</span>", ""
        if st.session_state["show_answer"]:
            back_content = card['back']
            audio_html = text_to_speech_html(card['front'] + " ... " + card['back'])
            if card.get("explanation"): explanation_html = f'<div class="card-explanation">💡 {card["explanation"]}</div>'

        st.markdown(f"""
        <div class="flashcard">
            <div class="card-tag">{card['tag']}</div>
            <div class="card-front">{card['front']}</div>
            <div class="card-back">{back_content}</div>
            {explanation_html}{audio_html}
        </div>""", unsafe_allow_html=True)
        st.write("") 
        
        if not st.session_state["show_answer"]: st.button("👁️ Show Answer", type="primary", use_container_width=True, on_click=cb_show_answer)
        else:
            cols, labels, scores = st.columns(4), ["Again", "Hard", "Good", "Easy"], [0, 3, 4, 5]
            for i, col in enumerate(cols): col.button(labels[i], use_container_width=True, on_click=cb_submit_review, args=(scores[i], card['id']))
    else:
        st.balloons()
        st.subheader("🏁 Complete!")
        stats = st.session_state["session_stats"]
        acc = int((stats["correct"]/stats["reviewed"]*100)) if stats["reviewed"] else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("Cards", stats['reviewed'])
        c2.metric("Accuracy", f"{acc}%")
        c3.metric("Time", f"{round((time.time() - stats['start_time']) / 60, 1) if stats['start_time'] else 0} min")
        if st.button("Start Over"):
            st.session_state["current_deck_id"] = None
            st.rerun()

def section_library():
    st.header("📚 Library")
    with get_db_connection() as conn:
        df_cards = pd.read_sql("SELECT * FROM cards", conn)
        decks = pd.read_sql("SELECT * FROM decks", conn)
        
    if decks.empty: st.info("No decks."); return

    t1, t2, t3, t4 = st.tabs(["📊 Stats", "✏️ Edit Cards", "📤 Export", "🗑️ Manage"])

    with t1:
        if not df_cards.empty:
            df_merged = pd.merge(df_cards, decks, left_on="deck_id", right_on="id", suffixes=('_card', '_deck'))
            stats_df = df_merged.groupby("name").agg({"repetitions": "mean", "ease_factor": "mean", "id_card": "count"}).rename(columns={"id_card": "Total Cards", "repetitions": "Avg Reps", "ease_factor": "Avg Ease"})
            st.dataframe(stats_df, use_container_width=True)
            if df_cards['last_reviewed'].notna().any():
                df_cards['last_reviewed'] = pd.to_datetime(df_cards['last_reviewed']).dt.date
                activity = df_cards['last_reviewed'].value_counts().reset_index()
                activity.columns = ['date', 'count']
                st.altair_chart(alt.Chart(activity).mark_rect().encode(x='date:O', y='count:Q', color='count'), use_container_width=True)

    with t2:
        if not df_cards.empty:
            edited = st.data_editor(df_cards[['id', 'front', 'back', 'explanation', 'tag']], hide_index=True, use_container_width=True, disabled=["id"])
            if st.button("💾 Save to DB", type="primary"):
                with get_db_connection() as conn:
                    for _, r in edited.iterrows(): conn.execute("UPDATE cards SET front=?, back=?, explanation=?, tag=? WHERE id=?", (r['front'], r['back'], r['explanation'], r['tag'], r['id']))
                    conn.commit()
                st.success("Updated!")

    with t3:
        export_deck = st.selectbox("Export Deck", decks['name'].tolist())
        deck_id_raw = decks[decks['name'] == export_deck].iloc[0]['id']
        with get_db_connection() as conn: cards_df = pd.read_sql("SELECT front, back, tag FROM cards WHERE deck_id=?", conn, params=(int(deck_id_raw),))
        st.download_button("Download CSV", data=cards_df.to_csv(index=False, header=False).encode('utf-8') if not cards_df.empty else b"", file_name=f"{export_deck}.csv", disabled=cards_df.empty)

    with t4:
        c1, c2 = st.columns(2)
        with c1:
            ren = st.selectbox("Rename", decks['name'].tolist(), key="r_sel")
            new_n = st.text_input("New Name")
            if st.button("Rename") and new_n:
                if rename_deck(ren, new_n): st.success("Done!"); time.sleep(1); st.rerun()
                else: st.error("Name exists.")
        with c2:
            d_del = st.selectbox("Delete", decks['name'].tolist(), key="d_sel")
            if st.button(f"🗑️ Delete {d_del}"): delete_deck(d_del); st.rerun()

def main():
    inject_custom_css()
    with st.sidebar:
        st.title("🧠 Flashcard Pro")
        api_key = st.text_input("Gemini API Key", type="password")
        st.metric("Cards Due Today", get_due_cards_count())
        st.divider()
        page = st.radio("Navigation", ["Study Mode", "Generator", "Library & Stats"], label_visibility="collapsed")
    
    if page == "Generator": section_generator(api_key)
    elif page == "Study Mode": section_study()
    elif page == "Library & Stats": section_library()

if __name__ == "__main__": main()
