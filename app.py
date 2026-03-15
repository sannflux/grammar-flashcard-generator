import streamlit as st
import pandas as pd
from github import Github, GithubException
import io
import random
from datetime import date, datetime, timezone, timedelta
import google.generativeai as genai
import json
import re
import time
import os
import tempfile
import hashlib
import concurrent.futures
import math

try:
    from gtts import gTTS
    import genanki
except ImportError:
    st.error("⚠️ Missing libraries! Please add `gTTS` and `genanki` to your requirements.txt")
    st.stop()

# ========================== SETUP ==========================
st.set_page_config(page_title="Vocab App", layout="centered", page_icon="📚")
st.title("📚 My Cloud Vocab")

THEME_COLOR = "#00ff41"
THEME_GLOW  = "rgba(0, 255, 65, 0.4)"
BG_COLOR    = "#111111"
BG_STRIPE   = "#181818"
TEXT_COLOR  = "#aaffaa"

# ========================== MOBILE KEYBOARD FIX ==========================
st.components.v1.html("""
<script>
const doc = window.parent.document;
doc.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && e.target.tagName === 'INPUT') {
        setTimeout(() => { e.target.blur(); }, 50);
    }
}, true);
</script>
""", height=0)

# ========================== SECRETS ==========================
try:
    token              = st.secrets["GITHUB_TOKEN"]
    repo_name          = st.secrets["REPO_NAME"]
    DEFAULT_GEMINI_KEY = st.secrets["GEMINI_API_KEY"]
except KeyError as e:
    st.error(f"❌ Missing Secret: {e}. Check your .streamlit/secrets.toml")
    st.stop()

# ========================== B8: MODULE-LEVEL PRE-COMPILED REGEX ==========================
_RE_SPACES       = re.compile(r"\s+")
_RE_SENT_SPLIT   = re.compile(r'(?<=[.!?])\s+')
_RE_JSON_FENCE_S = re.compile(r"^```(?:json)?\s*")
_RE_JSON_FENCE_E = re.compile(r"\s*```$")
_RE_JSON_ARRAY   = re.compile(r'\[.*\]', re.DOTALL)
_RE_CLEAN_TEXT   = re.compile(r'[^\w\s\-\']')
_RE_CLEAN_FNAME  = re.compile(r'[^a-zA-Z0-9]')
_RE_AUDIO_CLEAN  = re.compile(r'[^a-zA-Z0-9\s\-\']')
_RE_DECK_ILLEGAL = re.compile(r'[<>"/\\|?*]')
_RE_TAG_CLEAN    = re.compile(r'[^\w\-]')
_RE_STRIP_HTML   = re.compile(r'<[^>]+>')   # FIX-8: strip HTML before word count

_GRAMMAR_RULES = [
    (re.compile(r"\bto doing\b",      re.IGNORECASE), "to do"),
    (re.compile(r"\bfor helps\b",     re.IGNORECASE), "to help"),
    (re.compile(r"\bis use to\b",     re.IGNORECASE), "is used to"),
    (re.compile(r"\bhelp for to\b",   re.IGNORECASE), "help to"),
    (re.compile(r"\bfor to\b",        re.IGNORECASE), "to"),
    (re.compile(r"\bcan able to\b",   re.IGNORECASE), "can"),
    (re.compile(r"\bI am agree\b",    re.IGNORECASE), "I agree"),
    (re.compile(r"\bdiscuss about\b", re.IGNORECASE), "discuss"),
    (re.compile(r"\breturn back\b",   re.IGNORECASE), "return"),
]

# ========================== T3-C: SUBJECT PERSONAS ==========================
PERSONAS: dict[str, str] = {
    "General":           "",
    "Medical":           "You are a medical lexicographer. Use precise clinical terminology. ",
    "Legal":             "You are a legal lexicographer. Use precise juridical definitions. ",
    "Coding / Tech":     "You are a software-engineering lexicographer. Use technical CS context. ",
    "Language Learning": "You are an EFL/ESL teacher. Prioritize learner-friendly definitions and natural example sentences. ",
}

# ========================== T3-D: DIFFICULTY MODIFIERS ==========================
DIFFICULTY_SUFFIX: dict[str, str] = {
    "Beginner":     "Use simple vocabulary (A1-A2 CEFR). Short example sentences. Avoid jargon.",
    "Intermediate": "",
    "Advanced":     "Use sophisticated vocabulary (C1-C2 CEFR). Complex, nuanced examples with idioms.",
}

# ========================== X3-C: REGISTER LABELS ==========================
# Five values the AI assigns; mapped to inline badge colors for Anki CSS compat.
REGISTER_VALUES = ["Formal", "Informal", "Slang", "Technical", "Neutral"]
REGISTER_BADGE_CSS: dict[str, str] = {
    "Formal":    "color:#00ffff;border:1px solid #00ffff",
    "Informal":  "color:#ffff66;border:1px solid #ffff66",
    "Slang":     "color:#ff6b6b;border:1px solid #ff6b6b",
    "Technical": "color:#c084fc;border:1px solid #c084fc",
    "Neutral":   "color:#aaffaa;border:1px solid #aaffaa",
}

# ========================== T1-D: TPM PRE-FLIGHT THRESHOLDS ==========================
TPM_WARN_THRESHOLD  = 700_000
TPM_BLOCK_THRESHOLD = 850_000

# ========================== N2-D: CARD QUALITY SCORER ==========================
# FIX-8: _RE_STRIP_HTML strips HTML tags before word count — prevents <ul><li>
#         markup from inflating definition length scores.
QUALITY_WARN_THRESHOLD = 60   # 🔴 below this; 🟡 60-79; 🟢 80+

def score_card(note_data: dict) -> int:
    """N2-D: Returns 0-100 quality score. FIX-8: strips HTML before word count."""
    score = 0
    defn  = _RE_STRIP_HTML.sub(' ', note_data.get("Definition", ""))   # FIX-8
    if defn and len(defn.split()) >= 10:
        score += 30
    if note_data.get("Examples", ""):
        score += 30
    if note_data.get("Synonyms", ""):
        score += 20
    if "/" in note_data.get("Pronunciation", ""):   # IPA uses slashes
        score += 20
    return score

def quality_badge(score: int) -> str:
    if score >= 80: return "🟢"
    if score >= 60: return "🟡"
    return "🔴"

# ========================== A4: CYBERPUNK CSS ==========================
CYBERPUNK_CSS = f"""
.card {{
    font-family: 'Roboto Mono', 'Consolas', monospace;
    font-size: 18px; line-height: 1.5;
    color: {THEME_COLOR}; background-color: {BG_COLOR};
    background-image: repeating-linear-gradient(
        0deg, {BG_STRIPE}, {BG_STRIPE} 1px, {BG_COLOR} 1px, {BG_COLOR} 20px
    );
    padding: 30px 20px; text-align: left;
}}
.vellum-focus-container {{
    background: #0d0d0d; padding: 30px 20px; margin: 0 auto 40px;
    border: 2px solid {THEME_COLOR};
    box-shadow: 0 0 5px {THEME_COLOR}, 0 0 15px {THEME_GLOW};
    text-align: center;
}}
.prompt-text {{
    font-family: 'Electrolize', sans-serif; font-size: 1.8em; font-weight: 900;
    color: #ffffff;
    text-shadow: 1px 1px 0 #ff00ff, -1px -1px 0 #00ffff;
}}
.cloze {{ color: {BG_COLOR}; background-color: {THEME_COLOR}; padding: 2px 4px; }}
.solved-text .cloze {{
    color: #ff00ff; background: none;
    border-bottom: 3px double #00ffff; text-shadow: 0 0 5px #ff00ff;
}}
.vellum-section {{ margin: 15px 0; padding: 10px 0; border-bottom: 1px dashed {THEME_COLOR}; }}
.section-header {{
    font-weight: 600; color: #00ffff;
    border-left: 3px solid {THEME_COLOR}; padding-left: 10px;
}}
.content {{ color: {TEXT_COLOR}; padding-left: 13px; }}
.register-badge {{
    display: inline-block; font-size: 0.75em; font-weight: 700;
    padding: 1px 7px; border-radius: 3px; letter-spacing: 0.08em;
    text-transform: uppercase; margin-left: 6px;
}}
@media (max-width: 480px) {{
    .card {{ font-size: 16px; padding: 15px; }}
    .vellum-focus-container {{ padding: 15px; }}
}}
"""

# ========================== C12: BACKGROUND GITHUB EXECUTOR ==========================
@st.cache_resource
def _get_gh_executor():
    return concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="gh_bg"
    )

# ========================== GITHUB CONNECT ==========================
@st.cache_resource
def get_repo():
    try:
        g = Github(token)
        return g.get_repo(repo_name)
    except GithubException as e:
        st.error(f"❌ GitHub connection failed: {e}")
        st.stop()

repo = get_repo()

# ========================== T1-A + T4-D: COMBINED USAGE FILE ==========================
_COMBINED_USAGE_FILE = "usage_combined.json"

def _legacy_load_rpd() -> int:
    try:
        file = repo.get_contents("usage.json")
        data = json.loads(file.decoded_content.decode('utf-8'))
        return data.get("rpd_count", 0) if data.get("date") == str(date.today()) else 0
    except:
        return 0

def _legacy_load_rpm() -> list:
    try:
        file = repo.get_contents("usage_minute.json")
        data = json.loads(file.decoded_content.decode('utf-8'))
        return [datetime.fromisoformat(ts) for ts in data.get("timestamps", [])]
    except:
        return []

def _bg_save_combined(rpd_count: int, timestamps: list):
    """T1-A + FIX-3: Background writer; retries on 409 SHA conflict."""
    data = json.dumps({
        "date":       str(date.today()),
        "rpd_count":  rpd_count,
        "timestamps": [ts.isoformat() for ts in timestamps],
    })
    for attempt in range(3):
        try:
            try:
                file = repo.get_contents(_COMBINED_USAGE_FILE)
                repo.update_file(file.path, "Update combined usage", data, file.sha)
                return
            except GithubException as e:
                if e.status == 404:
                    repo.create_file(_COMBINED_USAGE_FILE, "Init combined usage", data)
                    return
                elif e.status == 409:
                    time.sleep(1 + attempt)
                    continue
                raise
        except Exception as e:
            if attempt == 2:
                print(f"Combined usage save error after 3 attempts: {e}")
            time.sleep(1)

def save_combined_usage(rpd_count: int, timestamps: list):
    _get_gh_executor().submit(_bg_save_combined, rpd_count, list(timestamps))

def load_combined_usage() -> tuple:
    try:
        file = repo.get_contents(_COMBINED_USAGE_FILE)
        data = json.loads(file.decoded_content.decode('utf-8'))
        rpd  = data.get("rpd_count", 0) if data.get("date") == str(date.today()) else 0
        tss  = [datetime.fromisoformat(ts) for ts in data.get("timestamps", [])]
        return rpd, tss
    except GithubException as e:
        if e.status == 404:
            rpd = _legacy_load_rpd()
            tss = _legacy_load_rpm()
            save_combined_usage(rpd, tss)
            return rpd, tss
        return 0, []
    except:
        return 0, []

# ========================== T1-C: SAFETY BLOCK LOGGER ==========================
def _bg_log_safety_block(vocab_words: list, prompt_hash: str):
    entry = {"timestamp": datetime.now().isoformat(), "vocab": vocab_words, "prompt_hash": prompt_hash}
    try:
        existing, file_sha = [], None
        try:
            file     = repo.get_contents("safety_log.json")
            file_sha = file.sha
            existing = json.loads(file.decoded_content.decode('utf-8'))
            if not isinstance(existing, list): existing = []
        except GithubException as e:
            if e.status != 404: return
        existing.append(entry)
        existing = existing[-100:]
        data = json.dumps(existing, ensure_ascii=False, indent=2)
        if file_sha: repo.update_file("safety_log.json", "Safety block log", data, file_sha)
        else:        repo.create_file("safety_log.json", "Init safety log", data)
    except Exception as e:
        print(f"Safety log write error: {e}")

def log_safety_block(vocab_words: list, prompt: str):
    _get_gh_executor().submit(_bg_log_safety_block, list(vocab_words),
                              hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:16])

# ========================== T4-C: EXPORT HISTORY LOGGER ==========================
def _bg_save_export_history(deck_name: str, card_count: int, vocab_list: list):
    entry = {"timestamp": datetime.now().isoformat(), "deck_name": deck_name,
             "card_count": card_count, "vocabs": vocab_list[:50]}
    try:
        existing, file_sha = [], None
        try:
            file     = repo.get_contents("export_history.json")
            file_sha = file.sha
            existing = json.loads(file.decoded_content.decode('utf-8'))
            if not isinstance(existing, list): existing = []
        except GithubException as e:
            if e.status != 404: return
        existing.append(entry)
        existing = existing[-200:]
        data = json.dumps(existing, ensure_ascii=False, indent=2)
        if file_sha: repo.update_file("export_history.json", "Export history", data, file_sha)
        else:        repo.create_file("export_history.json", "Init export history", data)
    except Exception as e:
        print(f"Export history write error: {e}")

def save_export_history(deck_name: str, card_count: int, vocab_list: list):
    _get_gh_executor().submit(_bg_save_export_history, deck_name, card_count, list(vocab_list))

# ========================== N4-E: EXPORT HISTORY LOADER ==========================
@st.cache_data(ttl=120)
def load_export_history() -> list:
    try:
        file = repo.get_contents("export_history.json")
        data = json.loads(file.decoded_content.decode('utf-8'))
        return data if isinstance(data, list) else []
    except:
        return []

# ========================== A5: SMART RPM ENFORCEMENT ==========================
def enforce_rpm() -> float:
    t0  = time.perf_counter()
    now = datetime.now()
    st.session_state.rpm_timestamps = [
        ts for ts in st.session_state.rpm_timestamps
        if (now - ts).total_seconds() < 60
    ]
    save_combined_usage(st.session_state.rpd_count, st.session_state.rpm_timestamps)
    if len(st.session_state.rpm_timestamps) >= 5:
        _slot = st.empty()
        for remaining in range(12, 0, -1):
            _slot.warning(f"⏳ RPM limit (5/min). Resuming in **{remaining}s**...")
            time.sleep(1)
        _slot.empty()
    st.session_state.rpm_timestamps.append(now)
    save_combined_usage(st.session_state.rpd_count, st.session_state.rpm_timestamps)
    return time.perf_counter() - t0

# ========================== GEMINI (🔒 model stasis) ==========================
@st.cache_resource
def get_gemini_model(api_key: str, model_name: str):
    try:
        genai.configure(api_key=api_key)
        return genai.GenerativeModel(
            model_name,
            generation_config={"response_mime_type": "application/json", "temperature": 0.1}
        )
    except Exception as e:
        st.error(f"❌ Gemini key error: {e}")
        return None

# ========================== B8: CLEANING FUNCTIONS ==========================
def cap_first(s: str) -> str:
    s = str(s).strip()
    return s[0].upper() + s[1:] if s else s

def ensure_trailing_dot(s: str) -> str:
    s = str(s).strip()
    return s if s and s[-1] in ".!?" else (s + "." if s else "")

def normalize_spaces(text: str) -> str:
    return _RE_SPACES.sub(" ", str(text)).strip() if text else ""

def clean_grammar(text: str) -> str:
    if not isinstance(text, str): return text
    for pat, repl in _GRAMMAR_RULES:
        text = pat.sub(repl, text)
    return text

def cap_each_sentence(text: str) -> str:
    if not isinstance(text, str): return text
    return " ".join(cap_first(s) for s in _RE_SENT_SPLIT.split(text) if s.strip())

def highlight_vocab(text: str, vocab: str) -> str:
    if not text or not vocab: return text
    return re.compile(r'\b' + re.escape(vocab) + r'\b', re.IGNORECASE).sub(
        f'<b><u>{vocab}</u></b>', text)

def fix_vocab_casing(phrase: str, vocab: str) -> str:
    if not phrase or not vocab: return phrase
    return re.compile(r'\b' + re.escape(vocab.lower()) + r'\b', re.IGNORECASE).sub(vocab, phrase)

def robust_json_parse(text: str):
    text = _RE_JSON_FENCE_S.sub("", text.strip())
    text = _RE_JSON_FENCE_E.sub("", text)
    try: return json.loads(text)
    except: pass
    m = _RE_JSON_ARRAY.search(text)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    return None

def speak_word(text: str, lang: str = "en-US"):
    if not text: return
    safe = text.replace('"', '\\"').replace("'", "\\'")
    st.components.v1.html(
        f"""<script>if('speechSynthesis'in window){{var u=new SpeechSynthesisUtterance("{safe}");
        u.lang="{lang}";u.rate=0.95;window.speechSynthesis.speak(u);}}</script>""", height=0)

def normalize_phrase(p: str) -> str:
    p = p.strip()
    if not p or p == "1" or p.startswith("*"): return p
    if p.endswith(","): p = p[:-1] + "."
    elif not p.endswith((".", "!", "?")): p += "."
    return cap_first(p)

def _clean_field(text: str) -> str:
    return ensure_trailing_dot(cap_each_sentence(clean_grammar(normalize_spaces(text))))

def sanitize_tags(raw_tags: str) -> list:
    if not raw_tags or not raw_tags.strip(): return []
    tags = []
    for t in re.split(r'[,\s]+', raw_tags.strip()):
        t = t.strip()
        if not t: continue
        clean = _RE_TAG_CLEAN.sub('_', t)
        clean = re.sub(r'_+', '_', clean).strip('_')
        if clean: tags.append(clean)
    return tags[:10]

# ========================== T4-G + T1-D: TPM TRACKING ==========================
def log_tpm_chars(char_count: int):
    st.session_state.tpm_log.append({"ts": datetime.now(), "chars": char_count})

def get_rolling_tpm() -> int:
    now = datetime.now()
    st.session_state.tpm_log = [
        e for e in st.session_state.tpm_log if (now - e["ts"]).total_seconds() < 60
    ]
    return sum(e["chars"] for e in st.session_state.tpm_log) // 4

def check_tpm_preflight(prompt: str) -> tuple:
    projected = get_rolling_tpm() + len(prompt) // 4
    return projected < TPM_BLOCK_THRESHOLD, projected

# ========================== N1-C: QUOTA RESET COUNTDOWN ==========================
def quota_reset_countdown() -> str:
    utc_now  = datetime.now(timezone.utc)
    midnight = (utc_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    delta    = midnight - utc_now
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    return f"{hours}h {rem // 60}m"

# ========================== C13 + C14: BATCH GENERATOR ==========================
def generate_anki_card_data_batched(
    vocab_phrase_list: list,
    batch_size: int = 6,
    dry_run: bool   = False
) -> list:
    TARGET_LANG    = st.session_state.get("target_lang", "Indonesian")
    model_name     = st.session_state.get("gemini_model_name", "gemini-2.5-flash-lite")
    model          = get_gemini_model(st.session_state.gemini_key, model_name)
    if not model: return []

    persona_prefix = PERSONAS.get(st.session_state.get("persona", "General"), "")
    diff_str       = DIFFICULTY_SUFFIX.get(st.session_state.get("difficulty", "Intermediate"), "")
    # X3-D: mnemonic rule injected as Rule 11 when opt-in is active
    use_mnemonic   = st.session_state.get("use_mnemonic", False)
    mnemonic_rule  = (
        "\n11. 'mnemonic': ONE memorable image or word-story hook to aid recall (max 20 words). "
        "Example for SERENDIPITY: 'Imagine a SERENe DIP into a lucky pool.'"
        if use_mnemonic else ""
    )
    # X3-C: register is Rule 9; difficulty is Rule 10 (conditional)
    difficulty_rule = f"\n10. DIFFICULTY LEVEL: {diff_str} Tailor all outputs accordingly." \
        if diff_str else ""

    word_cache     = st.session_state.get("word_cache", {})
    cached_results = [
        word_cache[vp[0].strip().lower()]
        for vp in vocab_phrase_list if vp[0].strip().lower() in word_cache
    ]
    deduped_list = [
        vp for vp in vocab_phrase_list if vp[0].strip().lower() not in word_cache
    ]

    if cached_results:
        st.info(f"♻️ {len(cached_results)} word(s) served from cache — **zero quota used**.")
    if not deduped_list:
        return cached_results

    all_new_data = []
    batches      = [deduped_list[i:i + batch_size] for i in range(0, len(deduped_list), batch_size)]
    timings      = []

    with st.status("🤖 Processing AI Batches (RPM Throttled)...", expanded=True) as status_log:
        progress_bar = st.progress(0)

        for idx, batch in enumerate(batches):
            if st.session_state.rpd_count >= 20:
                st.warning("🛑 Daily AI Limit (20 requests) reached. Try again tomorrow.")
                for vp in batch: st.session_state.failed_words.append(vp[0])
                break

            t_rpm       = enforce_rpm()
            batch_dicts = [{"vocab": v[0], "phrase": v[1]} for v in batch]
            vocab_words = [v[0] for v in batch]

            # Rule 8 = collocations, Rule 9 = register, Rule 10 = difficulty (optional),
            # Rule 11 = mnemonic (optional, X3-D)
            prompt = f"""{persona_prefix}You are an expert educational lexicographer. Think step-by-step:
1. Identify primary sense from phrase or context.
2. Generate accurate fields.
3. Ensure JSON validity.

Output EXACTLY {len(batch_dicts)} items as a JSON array. No extra text or commentary.

SAFETY OVERRIDE: Do not block slang, idioms, or medical terms. Provide purely educational linguistic definitions.
RULES:
1. Copy ALL fields exactly.
2. IF 'phrase' starts with '*': Treat it as a CONTEXT HINT.
3. IF 'phrase' is normal text: Use it ONLY to understand which meaning/nuance of the 'vocab' to use.
4. IF 'phrase' is empty: Generate ONE simple sentence (max 12 words) for the 'vocab'.
5. EXACT 'vocab' must remain unchanged.
6. MANDATORY: 'translation' must contain ONLY the {TARGET_LANG} translation of the 'vocab' word/phrase. NEVER translate the full example sentence.
7. 'part_of_speech' MUST be one of: Noun, Verb, Adjective, Adverb, Pronoun, Preposition, Conjunction, Interjection, Phrase.
8. 'collocations': provide exactly 2-3 of the most natural and common word combinations for the vocab as a JSON array of strings.
9. 'register': MUST be exactly one of: Formal, Informal, Slang, Technical, Neutral.{difficulty_rule}{mnemonic_rule}

EXAMPLES:
[
  {{"vocab":"serendipity","phrase":"We found the perfect cafe by pure serendipity.","translation":"kebetulan","part_of_speech":"Noun","pronunciation_ipa":"/ˌsɛrənˈdɪpɪti/","definition_english":"The occurrence and development of events by chance in a happy or beneficial way.","example_sentences":["It was pure serendipity that we met."],"synonyms_antonyms":{{"synonyms":["chance","luck"],"antonyms":["misfortune"]}},"etymology":"Coined by Horace Walpole in 1754.","collocations":["by pure serendipity","happy serendipity","moment of serendipity"],"register":"Neutral","mnemonic":"Imagine a SERENe DIP into a lucky pool of fate."}},
  {{"vocab":"run","phrase":"*He decided to run for office","translation":"mencalonkan diri","part_of_speech":"Verb","pronunciation_ipa":"/rʌn/","definition_english":"To compete in an election.","example_sentences":["She will run for president."],"synonyms_antonyms":{{"synonyms":["campaign"],"antonyms":["withdraw"]}},"etymology":"Old English rinnan.","collocations":["run for office","run a campaign","run against"],"register":"Formal","mnemonic":"A person RUNning in a suit toward a ballot box."}}
]

BATCH INPUT: {json.dumps(batch_dicts, ensure_ascii=False)}"""

            log_tpm_chars(len(prompt))

            if not dry_run:
                _is_tpm_safe, _projected = check_tpm_preflight(prompt)
                if not _is_tpm_safe:
                    st.error(
                        f"🛑 TPM Pre-flight blocked `{', '.join(vocab_words)}`: "
                        f"~{_projected:,} projected tokens exceeds {TPM_BLOCK_THRESHOLD:,} limit. "
                        f"Wait ~60s for the rolling window to reset."
                    )
                    st.session_state.failed_words.extend(vocab_words)
                    timings.append({"batch": idx + 1, "words": ", ".join(vocab_words),
                                    "rpm_wait_s": round(t_rpm, 3), "gemini_s": 0.0,
                                    "cached": False, "note": "TPM_BLOCKED"})
                    progress_bar.progress((idx + 1) / len(batches))
                    continue
                elif _projected > TPM_WARN_THRESHOLD:
                    st.warning(f"⚠️ TPM approaching limit: ~{_projected:,} / 1,000,000 projected tokens.")

            success     = False
            t_api_start = time.perf_counter()

            if dry_run:
                st.info(f"🔬 Dry-run: `{', '.join(vocab_words)}`")
                mock = [
                    {
                        "vocab": v[0], "phrase": v[1],
                        "translation": "mock-" + v[0], "part_of_speech": "Noun",
                        "pronunciation_ipa": "/mock/",
                        "definition_english": "Simulated definition for testing purposes.",
                        "example_sentences": ["Mock example sentence for dry run."],
                        "synonyms_antonyms": {"synonyms": ["mock", "simulated"], "antonyms": []},
                        "etymology": "Simulated.",
                        "collocations": ["mock collocation one", "mock collocation two"],
                        "register": "Neutral",
                        "mnemonic": "Mock mnemonic hook for dry run.",
                    }
                    for v in batch
                ]
                all_new_data.extend(mock)
                for card in mock: word_cache[card['vocab'].strip().lower()] = card
                success = True
            else:
                for attempt in range(3):
                    try:
                        response = model.generate_content(prompt)
                        st.session_state.rpd_count += 1
                        save_combined_usage(st.session_state.rpd_count, st.session_state.rpm_timestamps)

                        if hasattr(response, 'candidates') and response.candidates:
                            finish = str(response.candidates[0].finish_reason)
                            if finish in ("3", "SAFETY", "FinishReason.SAFETY"):
                                st.warning(f"🛡️ Safety filter blocked `{', '.join(vocab_words)}`. Logged and queued for retry.")
                                log_safety_block(vocab_words, prompt)
                                st.session_state.failed_words.extend(vocab_words)
                                break

                        parsed = robust_json_parse(response.text)
                        if isinstance(parsed, list) and len(parsed) > 0:
                            all_new_data.extend(parsed)
                            for card in parsed: word_cache[card['vocab'].strip().lower()] = card
                            recovered = [c.get('vocab', '') for c in parsed]
                            missed    = [v for v in vocab_words if v not in recovered]
                            if missed:
                                st.session_state.failed_words.extend(missed)
                                st.warning(f"⚠️ Partial batch {idx+1}: {len(parsed)}/{len(batch_dicts)} recovered. Missed: `{', '.join(missed)}`")
                            else:
                                st.markdown(f"✅ **Batch {idx+1}**: `{', '.join(vocab_words)}`")
                            success = True
                            break

                    except Exception as e:
                        if "429" in str(e):
                            backoff = 20 + (2 ** attempt) + random.uniform(0, 1)
                            _slot   = st.empty()
                            for r in range(int(backoff), 0, -1):
                                _slot.warning(f"⚠️ 429 Rate Limit. Retrying in **{r}s**... (attempt {attempt+1}/3)")
                                time.sleep(1)
                            _slot.empty()
                        else:
                            time.sleep(2)

            t_api_elapsed = time.perf_counter() - t_api_start
            timings.append({"batch": idx+1, "words": ", ".join(vocab_words),
                            "rpm_wait_s": round(t_rpm, 3), "gemini_s": round(t_api_elapsed, 3),
                            "cached": False, "note": ""})
            if not success and not dry_run:
                st.error(f"❌ **Failed**: `{', '.join(vocab_words)}` — queued for retry")
                st.session_state.failed_words.extend(vocab_words)
            progress_bar.progress((idx + 1) / len(batches))

        total = len(all_new_data) + len(cached_results)
        status_log.update(label=f"✅ AI Complete! ({total} items | {len(cached_results)} cached)",
                          state="complete", expanded=False)

    st.session_state.word_cache = word_cache
    if timings:
        with st.expander("⏱️ Batch Performance Timings", expanded=False):
            st.dataframe(pd.DataFrame(timings), hide_index=True)
    return cached_results + all_new_data

# ========================== B9 + B10: PROCESS ANKI DATA ==========================
def process_anki_data(
    df_subset: pd.DataFrame,
    batch_size: int = 6,
    dry_run: bool   = False
) -> list:
    t0        = time.perf_counter()
    cache_key = str(pd.util.hash_pandas_object(df_subset).sum())
    cached    = st.session_state.get("processed_cache", {})
    if (cached.get("key") == cache_key
            and (datetime.now() - cached.get("time", datetime.min)).total_seconds() < 300):
        st.info("♻️ Using cached processed notes — no re-generation needed.")
        return cached["notes"]

    df_clean = df_subset[df_subset['vocab'].astype(str).str.strip().str.len() > 0].copy()
    vocab_phrase_list = (
        df_clean.reindex(columns=['vocab', 'phrase'], fill_value='')[['vocab', 'phrase']].values.tolist()
    )

    # FIX-4: Vectorized tags lookup
    tags_lookup: dict[str, list] = {}
    if 'tags' in df_clean.columns:
        tags_series = (
            df_clean.assign(_vk=df_clean['vocab'].astype(str).str.strip().str.lower())
            .set_index('_vk')['tags'].fillna('')
        )
        tags_lookup = {str(k): sanitize_tags(str(v)) for k, v in tags_series.items() if str(v).strip()}

    all_card_data = generate_anki_card_data_batched(vocab_phrase_list, batch_size=batch_size, dry_run=dry_run)

    processed_notes = []
    for card_data in all_card_data:
        required = ["vocab", "translation", "part_of_speech"]
        if not all(k in card_data and card_data[k] for k in required):
            st.error(f"⚠️ Missing required fields for `{card_data.get('vocab','?')}` — skipping")
            continue

        vocab_raw = str(card_data.get("vocab", "")).strip().lower()
        if not vocab_raw: continue
        vocab_cap = cap_first(vocab_raw)

        phrase      = fix_vocab_casing(_clean_field(card_data.get("phrase", "")), vocab_raw)
        formatted   = highlight_vocab(phrase, vocab_raw) if phrase else ""
        translation = _clean_field(card_data.get("translation", "?"))
        pos         = str(card_data.get("part_of_speech", "")).title()
        ipa         = card_data.get("pronunciation_ipa", "")
        eng_def     = _clean_field(card_data.get("definition_english", ""))
        examples    = [_clean_field(e) for e in (card_data.get("example_sentences") or [])[:3]]
        ex_field    = "<ul>" + "".join(f"<li><i>{e}</i></li>" for e in examples) + "</ul>" if examples else ""
        syn_ant     = card_data.get("synonyms_antonyms") or {}
        synonyms    = ensure_trailing_dot(", ".join(cap_first(s) for s in (syn_ant.get("synonyms") or [])[:5]))
        antonyms    = ensure_trailing_dot(", ".join(cap_first(a) for a in (syn_ant.get("antonyms") or [])[:5]))
        etymology   = normalize_spaces(card_data.get("etymology", ""))

        # N3-C: Collocations — handle list or string
        collocations_raw = card_data.get("collocations") or []
        if isinstance(collocations_raw, list):
            collocations = "; ".join(cap_first(c) for c in collocations_raw[:3] if c)
        elif isinstance(collocations_raw, str):
            collocations = cap_first(collocations_raw.strip())
        else:
            collocations = ""

        # X3-C: Register — validate, default Neutral, build inline-styled badge
        register_raw  = str(card_data.get("register", "") or "").strip().title()
        register      = register_raw if register_raw in REGISTER_VALUES else "Neutral"
        reg_css       = REGISTER_BADGE_CSS.get(register, REGISTER_BADGE_CSS["Neutral"])
        register_html = f'<span class="register-badge" style="{reg_css}">{register}</span>'

        # X3-D: Mnemonic
        mnemonic_raw = str(card_data.get("mnemonic", "") or "").strip()
        mnemonic     = cap_first(mnemonic_raw) if mnemonic_raw else ""

        text_field = (
            f"{formatted}<br><br>{vocab_cap}: <b>{{{{c1::{translation}}}}}</b>"
            if formatted else f"{vocab_cap}: <b>{{{{c1::{translation}}}}}</b>"
        )
        pron_field = f"<b>[{pos}]</b> {ipa}" if ipa else f"<b>[{pos}]</b>"

        note = {
            "VocabRaw":      vocab_raw,
            "Text":          text_field,
            "Pronunciation": pron_field,
            "Definition":    eng_def,
            "Examples":      ex_field,
            "Synonyms":      synonyms,
            "Antonyms":      antonyms,
            "Etymology":     etymology,
            "Collocations":  collocations,   # N3-C
            "Register":      register_html,  # X3-C: colored badge HTML for Anki
            "RegisterLabel": register,       # X3-C: plain text for editor display
            "Mnemonic":      mnemonic,       # X3-D
            "Tags":          tags_lookup.get(vocab_raw, []),
        }
        note["_quality_score"] = score_card(note)   # N2-D
        processed_notes.append(note)

    st.session_state.processed_cache = {"key": cache_key, "notes": processed_notes, "time": datetime.now()}
    st.caption(f"⏱️ `process_anki_data`: {time.perf_counter() - t0:.3f}s — {len(processed_notes)} notes")
    return processed_notes

# ========================== D17: AUDIO HELPER ==========================
def generate_audio_file(args: tuple):
    vocab, temp_dir = args
    try:
        clean_vocab = _RE_AUDIO_CLEAN.sub('', vocab).strip()
        clean_fname = _RE_CLEAN_FNAME.sub('', clean_vocab) + ".mp3"
        file_path   = os.path.join(temp_dir, clean_fname)
        if clean_vocab:
            gTTS(text=clean_vocab, lang='en', slow=False).save(file_path)
            return vocab, clean_fname, file_path
    except Exception as e:
        print(f"Audio error for {vocab}: {e}")
    return vocab, None, None

# ========================== GENANKI LOGIC ==========================
def create_anki_package(
    notes_data:       list,
    deck_name:        str,
    generate_audio:   bool = True,
    deck_id:          int  = 2059400110,
    include_antonyms: bool = True
) -> io.BytesIO:
    t0 = time.perf_counter()

    front_html = """<div class="vellum-focus-container front">
<div class="prompt-text">{{cloze:Text}}</div></div>"""

    back_html = """<div class="vellum-focus-container back">
<div class="prompt-text solved-text">{{cloze:Text}}</div></div>
<div class="vellum-detail-container">
{{#Definition}}<div class="vellum-section">
<div class="section-header">📜 DEFINITION</div>
<div class="content">{{Definition}}</div></div>{{/Definition}}
{{#Pronunciation}}<div class="vellum-section">
<div class="section-header">🗣️ PRONUNCIATION</div>
<div class="content">{{Pronunciation}}</div></div>{{/Pronunciation}}
{{#Register}}<div class="vellum-section">
<div class="section-header">🏷 REGISTER</div>
<div class="content">{{Register}}</div></div>{{/Register}}
{{#Examples}}<div class="vellum-section">
<div class="section-header">🖋️ EXAMPLES</div>
<div class="content">{{Examples}}</div></div>{{/Examples}}
{{#Collocations}}<div class="vellum-section">
<div class="section-header">🔗 COLLOCATIONS</div>
<div class="content">{{Collocations}}</div></div>{{/Collocations}}
{{#Synonyms}}<div class="vellum-section">
<div class="section-header">➕ SYNONYMS</div>
<div class="content">{{Synonyms}}</div></div>{{/Synonyms}}"""

    if include_antonyms:
        back_html += """
{{#Antonyms}}<div class="vellum-section">
<div class="section-header">➖ ANTONYMS</div>
<div class="content">{{Antonyms}}</div></div>{{/Antonyms}}"""

    back_html += """
{{#Etymology}}<div class="vellum-section">
<div class="section-header">🏛️ ETYMOLOGY</div>
<div class="content">{{Etymology}}</div></div>{{/Etymology}}
{{#Mnemonic}}<div class="vellum-section">
<div class="section-header">💡 MEMORY HOOK</div>
<div class="content">{{Mnemonic}}</div></div>{{/Mnemonic}}
<div style='display:none'>{{Audio}}</div>
</div>"""

    model_id = st.session_state.get("model_id", 1607392319)
    my_model = genanki.Model(
        model_id, 'Cyberpunk Vocab Model',
        fields=[
            {'name': 'Text'},          {'name': 'Pronunciation'},
            {'name': 'Definition'},    {'name': 'Examples'},
            {'name': 'Collocations'},  # N3-C
            {'name': 'Register'},      # X3-C
            {'name': 'Synonyms'},      {'name': 'Antonyms'},
            {'name': 'Etymology'},
            {'name': 'Mnemonic'},      # X3-D
            {'name': 'Audio'},
        ],
        templates=[{'name': 'Card 1', 'qfmt': front_html, 'afmt': back_html}],
        css=CYBERPUNK_CSS,
        model_type=genanki.Model.CLOZE
    )
    my_deck     = genanki.Deck(deck_id, deck_name)
    media_files = []

    with tempfile.TemporaryDirectory() as temp_dir:
        audio_map = {}
        if generate_audio:
            t_audio       = time.perf_counter()
            unique_vocabs = {n['VocabRaw'] for n in notes_data if n['VocabRaw']}
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as exc:
                for vk, fn, fp in exc.map(generate_audio_file, [(v, temp_dir) for v in unique_vocabs]):
                    if fn:
                        media_files.append(fp)
                        audio_map[vk] = f"[sound:{fn}]"
            st.caption(f"⏱️ Audio: {time.perf_counter() - t_audio:.2f}s for {len(unique_vocabs)} words")

        exported_hashes = st.session_state.get("exported_hashes", set())
        for note_data in notes_data:
            guid_input = note_data['VocabRaw'] + deck_name
            vocab_hash = str(int(hashlib.sha256(guid_input.encode('utf-8')).hexdigest(), 16) % (10 ** 10))
            exported_hashes.add(hashlib.sha256(note_data['VocabRaw'].encode('utf-8')).hexdigest()[:16])
            my_deck.add_note(genanki.Note(
                model=my_model,
                fields=[
                    note_data['Text'],             note_data['Pronunciation'],
                    note_data['Definition'],       note_data['Examples'],
                    note_data.get('Collocations', ''),
                    note_data.get('Register', ''),
                    note_data['Synonyms'],         note_data['Antonyms'],
                    note_data['Etymology'],
                    note_data.get('Mnemonic', ''),
                    audio_map.get(note_data['VocabRaw'], ""),
                ],
                tags=note_data['Tags'],
                guid=vocab_hash
            ))

        st.session_state.exported_hashes = exported_hashes
        my_package             = genanki.Package(my_deck)
        my_package.media_files = media_files
        output_path            = os.path.join(temp_dir, 'output.apkg')
        my_package.write_to_file(output_path)
        buffer = io.BytesIO()
        with open(output_path, "rb") as f: buffer.write(f.read())
        buffer.seek(0)

    save_export_history(deck_name=deck_name, card_count=len(notes_data),
                        vocab_list=[n['VocabRaw'] for n in notes_data])
    st.caption(f"⏱️ `create_anki_package` total: {time.perf_counter() - t0:.2f}s")
    return buffer

# ========================== LOAD / SAVE ==========================
@st.cache_data(ttl=600)
def load_data() -> pd.DataFrame:
    try:
        file_content = repo.get_contents("vocabulary.csv")
        df = pd.read_csv(io.StringIO(file_content.decoded_content.decode('utf-8')), dtype=str)
        df['phrase'] = df['phrase'].fillna("")
        df['status'] = df['status'].fillna('New') if 'status' in df.columns else 'New'
        df['tags']   = df['tags'].fillna('')      if 'tags'   in df.columns else ''
        return df.sort_values(by="vocab", ignore_index=True)
    except GithubException as e:
        if e.status == 404: return pd.DataFrame(columns=['vocab', 'phrase', 'status', 'tags'])
        st.stop()
    except:
        st.stop()

def save_to_github(dataframe: pd.DataFrame) -> bool:
    """N4-B: Snapshots vocab_df to undo_df before every write."""
    st.session_state.undo_df = st.session_state.vocab_df.copy()
    t0        = time.perf_counter()
    mask      = dataframe['vocab'].astype(str).str.strip().str.len() > 0
    dataframe = dataframe[mask].drop_duplicates(subset=['vocab'], keep='last')
    drop_cols = [c for c in ['Export', '⚠️ Prev. Exported', '_quality_score',
                              'RegisterLabel'] if c in dataframe.columns]
    if drop_cols: dataframe = dataframe.drop(columns=drop_cols)
    csv_data = dataframe.to_csv(index=False)
    try:
        file = repo.get_contents("vocabulary.csv")
        repo.update_file(file.path, "Updated vocab", csv_data, file.sha)
    except GithubException as e:
        if e.status == 404: repo.create_file("vocabulary.csv", "Initial commit", csv_data)
    load_data.clear()
    st.caption(f"⏱️ GitHub save: {time.perf_counter() - t0:.2f}s")
    return True

# ========================== SESSION STATE INIT ==========================
# FIX-2: load_combined_usage gated to cold boot only
if "rpd_count" not in st.session_state or "rpm_timestamps" not in st.session_state:
    _init_rpd, _init_rpm = load_combined_usage()
else:
    _init_rpd = st.session_state.rpd_count
    _init_rpm = st.session_state.rpm_timestamps

st.session_state.setdefault("gemini_key",        DEFAULT_GEMINI_KEY)
st.session_state.setdefault("vocab_df",          load_data().copy())
st.session_state.setdefault("rpd_count",         _init_rpd)
st.session_state.setdefault("rpm_timestamps",    _init_rpm)
st.session_state.setdefault("deck_id",           2059400110)
st.session_state.setdefault("bulk_preview_df",   None)
st.session_state.setdefault("apkg_buffer",       None)
st.session_state.setdefault("processed_vocabs",  [])
st.session_state.setdefault("model_id",          1607392319)
st.session_state.setdefault("include_antonyms",  True)
st.session_state.setdefault("dry_run",           False)
st.session_state.setdefault("processed_cache",   {})
st.session_state.setdefault("word_cache",        {})
st.session_state.setdefault("input_phrase",      "")
st.session_state.setdefault("input_vocab",       "")
st.session_state.setdefault("_quota_cache_key",  None)
st.session_state.setdefault("_quota_cache",      (20, 0))
st.session_state.setdefault("target_lang",       "Indonesian")
st.session_state.setdefault("gemini_model_name", "gemini-2.5-flash-lite")
st.session_state.setdefault("persona",           "General")
st.session_state.setdefault("difficulty",        "Intermediate")
st.session_state.setdefault("tpm_log",           [])
st.session_state.setdefault("failed_words",      [])
st.session_state.setdefault("exported_hashes",   set())
st.session_state.setdefault("preview_notes",     [])
st.session_state.setdefault("last_deck_name",    "-English Learning::Vocabulary")
st.session_state.setdefault("last_batch_size",   6)
st.session_state.setdefault("model_id_confirm",  False)
st.session_state.setdefault("undo_df",           None)
st.session_state.setdefault("use_mnemonic",      False)   # X3-D
st.session_state.setdefault("editing_notes",     None)    # X2-A: card editor buffer
st.session_state.setdefault("editing_deck_name", "")      # X2-A: preserved for pack step
st.session_state.setdefault("editing_audio",     True)    # X2-A: preserved for pack step

# ========================== CALLBACKS ==========================
def mark_as_done_callback():
    if st.session_state.processed_vocabs:
        st.session_state.vocab_df.loc[
            st.session_state.vocab_df['vocab'].isin(st.session_state.processed_vocabs), 'status'
        ] = 'Done'
        save_to_github(st.session_state.vocab_df)
    st.session_state.apkg_buffer      = None
    st.session_state.processed_vocabs = []
    st.session_state.preview_notes    = []
    st.session_state.editing_notes    = None   # X2-A

def save_single_word_callback():
    v = st.session_state.input_vocab.lower().strip()
    if v:
        p    = normalize_phrase(st.session_state.input_phrase)
        mask = st.session_state.vocab_df['vocab'] == v
        if not st.session_state.vocab_df.empty and mask.any():
            st.session_state.vocab_df.loc[mask, ['phrase', 'status']] = [p, 'New']
        else:
            new_row = pd.DataFrame([{"vocab": v, "phrase": p, "status": "New", "tags": ""}])
            st.session_state.vocab_df = pd.concat(
                [st.session_state.vocab_df, new_row], ignore_index=True)
        save_to_github(st.session_state.vocab_df)
        st.session_state.input_phrase = ""
        st.session_state.input_vocab  = ""
        st.toast(f"✅ Saved '{v}'!", icon="🚀")
    else:
        st.error("⚠️ Enter a vocabulary word.")

# ========================== SIDEBAR ==========================
with st.sidebar:
    st.header("⚙️ Settings")

    total_words = len(st.session_state.vocab_df)
    new_words   = len(st.session_state.vocab_df[st.session_state.vocab_df['status'] == 'New'])
    col1, col2  = st.columns(2)
    col1.metric("📖 Total", total_words)
    col2.metric("✨ New",   new_words)
    st.metric("🤖 Daily AI Usage", f"{st.session_state.rpd_count}/20 Requests")

    rpm_live = len([ts for ts in st.session_state.rpm_timestamps
                    if (datetime.now() - ts).total_seconds() < 60])
    st.progress(rpm_live / 5,                    text=f"RPM Live: {rpm_live}/5 (last 60s)")
    st.progress(st.session_state.rpd_count / 20, text=f"RPD: {st.session_state.rpd_count}/20")

    tpm_estimate = get_rolling_tpm()
    tpm_frac     = min(tpm_estimate / 1_000_000, 1.0)
    tpm_icon     = "🟢" if tpm_frac < 0.60 else ("🟡" if tpm_frac < 0.85 else "🔴")
    st.progress(tpm_frac, text=f"TPM est: {tpm_icon} {tpm_estimate:,} / 1,000,000 (last 60s)")
    st.caption(f"⏰ Quota resets in **{quota_reset_countdown()}** (UTC midnight)")

    st.divider()

    st.selectbox("🎯 Definition Language",
                 ["Indonesian", "Spanish", "French", "German", "Japanese", "English (Simple)"],
                 index=0, key="target_lang")
    st.selectbox("🤖 AI Model",
                 ["gemini-2.5-flash-lite", "gemini-2.0-flash-exp"],
                 index=0, key="gemini_model_name")
    st.selectbox("🧠 Subject Persona", list(PERSONAS.keys()), index=0, key="persona",
                 help="Shapes the AI's definition style and example sentence domain.")
    st.radio("📊 Difficulty Level", list(DIFFICULTY_SUFFIX.keys()),
             index=1, horizontal=True, key="difficulty",
             help="Beginner/Advanced appends a CEFR-level instruction as Rule 10.")
    if st.session_state.difficulty != "Intermediate":
        st.caption(f"📌 Rule 10: _{DIFFICULTY_SUFFIX[st.session_state.difficulty]}_")

    # X3-D: Mnemonic generator opt-in
    st.checkbox("💡 Generate Memory Hooks", key="use_mnemonic",
                help="Adds a memorable story/image hook per card (Rule 11, ~25 extra tokens/word).")
    if st.session_state.use_mnemonic:
        st.caption("📌 Rule 11 active: mnemonic hook generated for each word.")

    st.divider()

    has_exported = (len(st.session_state.processed_vocabs) > 0
                    or len(st.session_state.exported_hashes) > 0)
    if st.button("🔄 Regenerate Note Type Model ID"):
        if has_exported and not st.session_state.model_id_confirm:
            st.session_state.model_id_confirm = True
        else:
            st.session_state.model_id         = random.randrange(1 << 30, 1 << 31)
            st.session_state.model_id_confirm = False
            st.success(f"New Model ID: {st.session_state.model_id}")
    if st.session_state.model_id_confirm:
        st.warning("⚠️ You have exported cards this session. Changing the Model ID creates a new "
                   "Anki note type and may orphan existing cards. **Click again to confirm.**")
    st.caption(f"Current Model ID: {st.session_state.model_id}")

    if st.button("🗑️ Clear Word Cache"):
        st.session_state.word_cache      = {}
        st.session_state.processed_cache = {}
        st.toast("🗑️ Word cache cleared.")

    if not st.session_state.vocab_df.empty:
        st.download_button("💾 Backup Database (CSV)",
                           st.session_state.vocab_df.to_csv(index=False).encode('utf-8'),
                           f"vocab_backup_{date.today()}.csv", "text/csv")

# ========================== TABS ==========================
tab1, tab2, tab3 = st.tabs(["➕ Add", "✏️ Edit / Review", "📇 Generate Anki"])

# ──────────────────────────── TAB 1 ────────────────────────────
with tab1:
    st.subheader("Add new word")
    add_mode = st.radio("Mode", ["Single", "Bulk"], horizontal=True, label_visibility="collapsed")

    if add_mode == "Single":
        p_raw      = st.text_input("🔤 Phrase", placeholder="Paste your sentence here...", key="input_phrase")
        v_selected = ""
        if p_raw and p_raw not in ["1", "*"]:
            clean_text   = _RE_CLEAN_TEXT.sub('', p_raw)
            unique_words = list(dict.fromkeys([w.lower() for w in clean_text.split() if w.strip()]))
            if unique_words:
                st.caption("Click words below to extract them as vocabulary:")
                try:
                    selected_pills = st.pills("Select Vocab", unique_words,
                                              selection_mode="multi", label_visibility="collapsed")
                    v_selected = " ".join(selected_pills) if selected_pills else ""
                except:
                    selected_words = []
                    cols = st.columns(6)
                    for i, w in enumerate(unique_words):
                        if cols[i % 6].checkbox(w, key=f"chk_{w}"): selected_words.append(w)
                    v_selected = " ".join(selected_words)

        if v_selected and v_selected != st.session_state.input_vocab:
            st.session_state.input_vocab = v_selected

        st.text_input("📝 Vocab", placeholder="e.g. serendipity", key="input_vocab")
        v_check = st.session_state.input_vocab.lower().strip()
        if (v_check and not st.session_state.vocab_df.empty
                and (st.session_state.vocab_df['vocab'] == v_check).any()):
            st.warning(f"⚠️ '{v_check}' already exists. Saving will overwrite its phrase and reset to 'New'.")
        st.button("💾 Save to Cloud", type="primary", use_container_width=True,
                  on_click=save_single_word_callback)
    else:
        bulk_text = st.text_area("Paste List (word, phrase)", height=150, key="bulk_input")
        if st.button("Preview Bulk Import"):
            lines    = [l.strip() for l in bulk_text.split('\n') if l.strip()]
            new_rows = []
            for line in lines:
                parts = line.split(',', 1)
                bv    = parts[0].strip().lower()
                bp    = normalize_phrase(parts[1].strip() if len(parts) > 1 else "")
                if bv: new_rows.append({"vocab": bv, "phrase": bp, "status": "New", "tags": ""})
            if new_rows: st.session_state.bulk_preview_df = pd.DataFrame(new_rows)

        if st.session_state.bulk_preview_df is not None:
            st.write("### Preview:")
            st.dataframe(st.session_state.bulk_preview_df, hide_index=True)
            if st.button("💾 Confirm & Process Bulk", type="primary"):
                st.session_state.vocab_df = pd.concat(
                    [st.session_state.vocab_df, st.session_state.bulk_preview_df]
                ).drop_duplicates(subset=['vocab'], keep='last')
                save_to_github(st.session_state.vocab_df)
                st.success(f"✅ Added {len(st.session_state.bulk_preview_df)} words!")
                st.session_state.bulk_preview_df = None
                st.rerun()

# ──────────────────────────── TAB 2 ────────────────────────────
with tab2:
    @st.fragment
    def render_tab2():
        if st.session_state.vocab_df.empty:
            st.info("Add words first!")
            return

        st.subheader(f"✏️ Edit List ({len(st.session_state.vocab_df)} words)")

        # N4-B: Undo last save
        if st.session_state.undo_df is not None:
            if st.button("↩️ Undo Last Save", use_container_width=True):
                st.session_state.vocab_df = st.session_state.undo_df.copy()
                st.session_state.undo_df  = None
                save_to_github(st.session_state.vocab_df)
                st.toast("↩️ Undo applied — previous state restored.", icon="↩️")
                st.rerun(scope="app")

        search     = st.text_input("🔎 Search...", "").lower().strip()
        display_df = st.session_state.vocab_df.copy()
        if search:
            display_df = display_df[display_df['vocab'].str.contains(search, case=False)]

        page_size = 50
        page      = st.number_input("Page", min_value=1, value=1, step=1)
        start     = (page - 1) * page_size
        paginated = display_df.iloc[start:start + page_size]

        # FIX-6: Edit buffer — prevents fragment reruns from resetting data_editor
        _buf_key = f"_edit_buf_{page}_{search}"
        if st.session_state.get("_edit_buf_key") != _buf_key:
            st.session_state["_edit_buf_key"] = _buf_key
            st.session_state["_edit_buffer"]  = paginated.copy()

        edited = st.data_editor(
            st.session_state["_edit_buffer"],
            num_rows="dynamic", use_container_width=True, hide_index=True,
            column_config={"status": st.column_config.SelectboxColumn(
                "Status", options=["New", "Done"], required=True)}
        )
        st.session_state["_edit_buffer"] = edited

        col_save, col_quality = st.columns(2)

        if col_save.button("💾 Save Changes", type="primary", use_container_width=True):
            full_df      = st.session_state.vocab_df.copy()
            existing_idx = [i for i in edited.index if i in full_df.index]
            if existing_idx:
                full_df.loc[existing_idx, edited.columns.tolist()] = edited.loc[existing_idx].values
            new_mask = ~edited.index.isin(full_df.index)
            if new_mask.any():
                full_df = pd.concat([full_df, edited[new_mask]], ignore_index=True)
            # FIX-5: deletions
            deleted_idx = [i for i in paginated.index if i not in edited.index]
            if deleted_idx:
                full_df = full_df.drop(index=deleted_idx).reset_index(drop=True)

            st.session_state.vocab_df = full_df
            # FIX-7: invalidate edit buffer so next visit reloads from updated vocab_df
            st.session_state.pop("_edit_buf_key", None)
            st.session_state.pop("_edit_buffer", None)
            save_to_github(st.session_state.vocab_df)
            st.toast("✅ Cloud updated!")
            st.rerun(scope="app")

        # N4-A: CSV Data Quality Report
        if col_quality.button("📊 Data Quality", use_container_width=True):
            df    = st.session_state.vocab_df
            total = len(df)
            if total == 0:
                st.info("No data to analyse.")
            else:
                with_phrase = (df['phrase'].astype(str).str.strip() != '').sum()
                with_tags   = (df['tags'].astype(str).str.strip() != '').sum() if 'tags' in df.columns else 0
                dups        = df['vocab'].duplicated().sum()
                short_vocab = (df['vocab'].astype(str).str.strip().str.len() <= 2).sum()
                done_count  = (df['status'] == 'Done').sum()
                new_count   = (df['status'] == 'New').sum()
                pct = lambda n: f"{n / total * 100:.0f}%"
                report = pd.DataFrame({
                    "Metric": ["Total words","With phrases","With tags",
                               "Duplicate vocab","Short vocab (≤2 chars)","Status: New","Status: Done"],
                    "Count":  [total, with_phrase, with_tags, dups, short_vocab, new_count, done_count],
                    "%":      ["100%", pct(with_phrase), pct(with_tags), pct(dups),
                               pct(short_vocab), pct(new_count), pct(done_count)],
                })
                st.dataframe(report, hide_index=True, use_container_width=True)
                if dups > 0:        st.warning(f"⚠️ {dups} duplicate vocab entries detected.")
                if short_vocab > 0: st.warning(f"⚠️ {short_vocab} entries with vocab ≤2 characters.")
                if total > 0 and with_phrase / total < 0.5:
                    st.info(f"💡 Only {pct(with_phrase)} of words have phrases. Adding phrases improves card quality.")

    render_tab2()

# ──────────────────────────── TAB 3 ────────────────────────────
with tab3:
    @st.fragment
    def render_tab3():

        # ══════════════════════════════════════════════════════════════
        # X2-A PHASE 2: Card editor — raw_notes generated, user edits
        # before packing into .apkg
        # ══════════════════════════════════════════════════════════════
        if st.session_state.editing_notes is not None:
            st.subheader("✏️ Edit Generated Cards")
            st.caption(
                "Review and fix AI output before downloading. "
                "Edit any field directly — changes are reflected in the final .apkg."
            )

            # Build an editable DataFrame from the notes list
            # Private fields (_quality_score, Tags list) are excluded from the editor
            EDITABLE_COLS = ["VocabRaw", "Definition", "Collocations",
                             "RegisterLabel", "Synonyms", "Antonyms", "Mnemonic"]
            notes_df = pd.DataFrame([
                {col: n.get(col, "") for col in EDITABLE_COLS}
                for n in st.session_state.editing_notes
            ])

            edited_notes_df = st.data_editor(
                notes_df,
                num_rows="fixed",
                use_container_width=True,
                hide_index=True,
                column_config={
                    "VocabRaw":      st.column_config.TextColumn("Vocab",        disabled=True),
                    "Definition":    st.column_config.TextColumn("Definition",   width="large"),
                    "Collocations":  st.column_config.TextColumn("Collocations", width="medium"),
                    "RegisterLabel": st.column_config.SelectboxColumn(
                        "Register", options=REGISTER_VALUES, required=True),
                    "Synonyms":      st.column_config.TextColumn("Synonyms",     width="medium"),
                    "Antonyms":      st.column_config.TextColumn("Antonyms",     width="medium"),
                    "Mnemonic":      st.column_config.TextColumn("Memory Hook",  width="large"),
                }
            )

            # N2-D quality overview for the full set
            scores  = [n.get("_quality_score", 0) for n in st.session_state.editing_notes]
            avg_q   = int(sum(scores) / len(scores)) if scores else 0
            low_q   = sum(1 for s in scores if s < QUALITY_WARN_THRESHOLD)
            st.info(
                f"📊 **{len(scores)} cards** · Avg quality: {quality_badge(avg_q)} **{avg_q}/100**"
                + (f" · ⚠️ {low_q} card(s) below {QUALITY_WARN_THRESHOLD}" if low_q else "")
            )

            col_pack, col_cancel = st.columns(2)

            if col_pack.button("📦 Pack & Download .apkg", type="primary", use_container_width=True):
                # Merge edits back into the notes list
                updated_notes = []
                for i, note in enumerate(st.session_state.editing_notes):
                    if i < len(edited_notes_df):
                        row = edited_notes_df.iloc[i]
                        note = dict(note)   # shallow copy
                        note["Definition"]    = str(row.get("Definition", note["Definition"]))
                        note["Collocations"]  = str(row.get("Collocations", note["Collocations"]))
                        note["Synonyms"]      = str(row.get("Synonyms", note["Synonyms"]))
                        note["Antonyms"]      = str(row.get("Antonyms", note["Antonyms"]))
                        note["Mnemonic"]      = str(row.get("Mnemonic", note.get("Mnemonic", "")))
                        # X3-C: re-build register badge HTML from edited plain label
                        new_reg               = str(row.get("RegisterLabel", note.get("RegisterLabel", "Neutral")))
                        new_reg               = new_reg if new_reg in REGISTER_VALUES else "Neutral"
                        reg_css               = REGISTER_BADGE_CSS.get(new_reg, REGISTER_BADGE_CSS["Neutral"])
                        note["RegisterLabel"] = new_reg
                        note["Register"]      = f'<span class="register-badge" style="{reg_css}">{new_reg}</span>'
                        note["_quality_score"] = score_card(note)
                    updated_notes.append(note)

                with st.spinner("🎵 Generating audio & packing .apkg..."):
                    apkg = create_anki_package(
                        updated_notes,
                        st.session_state.editing_deck_name,
                        generate_audio=st.session_state.editing_audio,
                        deck_id=st.session_state.deck_id,
                        include_antonyms=st.session_state.include_antonyms
                    )
                st.session_state.apkg_buffer      = apkg.getvalue()
                st.session_state.processed_vocabs = [n['VocabRaw'] for n in updated_notes]
                st.session_state.preview_notes    = updated_notes[:3]
                st.session_state.editing_notes    = None
                st.rerun(scope="app")

            if col_cancel.button("❌ Discard & Start Over", use_container_width=True):
                st.session_state.editing_notes = None
                st.rerun(scope="app")
            return

        # ══════════════════════════════════════════════════════════════
        # X2-A PHASE 3: Download state (after pack)
        # ══════════════════════════════════════════════════════════════
        if st.session_state.apkg_buffer is not None:
            st.success("✅ Deck packed! Download below.")

            if st.session_state.get("preview_notes"):
                with st.expander("👁️ Card Preview (first 3 cards)", expanded=True):
                    for i, note in enumerate(st.session_state.preview_notes, 1):
                        q_score = note.get("_quality_score", 0)
                        q_badge = quality_badge(q_score)
                        st.markdown(f"**Card {i} — FRONT** &nbsp; {q_badge} Quality: **{q_score}/100**")
                        if q_score < QUALITY_WARN_THRESHOLD:
                            st.caption(f"⚠️ Low quality score ({q_score}/100).")
                        front_preview = re.sub(r'\{\{c\d+::(.*?)\}\}', r'[___]', note['Text'])
                        st.markdown(
                            f"<div style='background:#1a1a1a; border:1px solid #00ff41; "
                            f"padding:10px 14px; border-radius:4px; font-family:monospace; "
                            f"color:#aaffaa; line-height:1.6'>{front_preview}</div>",
                            unsafe_allow_html=True
                        )
                        st.markdown(f"**Card {i} — BACK**")
                        back_items = []
                        if note.get("Pronunciation"):   back_items.append(f"🗣️ {note['Pronunciation']}")
                        if note.get("Definition"):      back_items.append(f"📜 {note['Definition']}")
                        if note.get("Examples"):
                            plain_ex = re.sub(r'<[^>]+>', ' ', note['Examples']).strip()
                            back_items.append(f"🖋️ {plain_ex}")
                        if note.get("Collocations"):    back_items.append(f"🔗 {note['Collocations']}")
                        # X3-C: show plain register label (not HTML badge) in preview
                        if note.get("RegisterLabel"):   back_items.append(f"🏷 {note['RegisterLabel']}")
                        if note.get("Synonyms"):        back_items.append(f"➕ {note['Synonyms']}")
                        if note.get("Mnemonic"):        back_items.append(f"💡 {note['Mnemonic']}")
                        if note.get("Tags"):            back_items.append(f"🔖 {', '.join(note['Tags'])}")
                        st.markdown(
                            "<div style='background:#1a1a1a; border:1px solid #00ffff; "
                            "padding:10px 14px; border-radius:4px; font-family:monospace; "
                            f"color:#aaffaa; line-height:1.8'>{'<br>'.join(back_items)}</div>",
                            unsafe_allow_html=True
                        )
                        if i < len(st.session_state.preview_notes): st.divider()

            st.download_button(
                "📥 Download .apkg",
                data=st.session_state.apkg_buffer,
                file_name=f"AnkiDeck_{datetime.now().strftime('%Y%m%d_%H%M')}.apkg",
                mime="application/octet-stream",
                use_container_width=True,
                on_click=mark_as_done_callback
            )
            if st.button("❌ Cancel / Clear"):
                st.session_state.apkg_buffer      = None
                st.session_state.processed_vocabs = []
                st.session_state.preview_notes    = []
                st.rerun(scope="app")
            return

        # ══════════════════════════════════════════════════════════════
        # PHASE 1: Normal generation UI
        # ══════════════════════════════════════════════════════════════
        if st.session_state.vocab_df.empty:
            st.info("Add words first!")
            return

        subset = st.session_state.vocab_df[st.session_state.vocab_df['status'] == 'New'].copy()
        if subset.empty:
            st.warning("⚠️ No 'New' words to export! All words are marked 'Done'.")
            return

        # T1-B: Failed words retry panel
        if st.session_state.failed_words:
            with st.expander(
                f"⚠️ {len(st.session_state.failed_words)} word(s) failed — click to retry",
                expanded=True
            ):
                st.dataframe(pd.DataFrame({"Queued for Retry": st.session_state.failed_words}),
                             hide_index=True)
                col_retry, col_dismiss = st.columns(2)
                if col_retry.button("🔁 Retry Failed Words", type="primary"):
                    retry_df = pd.DataFrame({
                        "vocab":  st.session_state.failed_words,
                        "phrase": [""] * len(st.session_state.failed_words),
                        "status": ["New"] * len(st.session_state.failed_words),
                        "tags":   [""] * len(st.session_state.failed_words),
                    })
                    st.session_state.failed_words = []
                    retry_notes = process_anki_data(retry_df,
                                                    batch_size=st.session_state.last_batch_size,
                                                    dry_run=st.session_state.dry_run)
                    if retry_notes:
                        # X2-A: send retried notes to the editor phase too
                        st.session_state.editing_notes     = retry_notes
                        st.session_state.editing_deck_name = st.session_state.last_deck_name
                        st.session_state.editing_audio     = True
                        st.rerun(scope="app")
                    else:
                        st.error("❌ Retry failed. Check your API quota.")
                if col_dismiss.button("🗑️ Dismiss"):
                    st.session_state.failed_words = []
                    st.rerun(scope="app")

        st.subheader("📇 Generate Cyberpunk Anki Deck")

        # T2-B: Deck hierarchy UI
        deck_col1, deck_col2 = st.columns([3, 1])
        deck_name_raw   = deck_col1.text_input("📦 Deck Name (use :: for sub-decks)",
                                               value=st.session_state.last_deck_name)
        deck_parts_raw  = [p.strip() for p in deck_name_raw.split("::") if p.strip()]
        deck_parts      = [_RE_DECK_ILLEGAL.sub("", p) for p in deck_parts_raw]
        deck_name_input = "::".join(deck_parts) if deck_parts else "Vocabulary"
        if deck_name_raw: st.session_state.last_deck_name = deck_name_input
        if _RE_DECK_ILLEGAL.search(deck_name_raw.replace("::", "")):
            st.warning("⚠️ Illegal characters removed from deck name.")
        if len(deck_parts) > 1:
            st.caption("📂 Hierarchy: " + " → ".join(deck_parts))
        if deck_col2.button("🎲 New Deck ID"):
            st.session_state.deck_id = random.randrange(1 << 30, 1 << 31)
        deck_col2.caption(f"ID: {st.session_state.deck_id}")

        requests_left = max(0, 20 - st.session_state.rpd_count)
        raw_batch     = st.slider("⚡ Batch Size (Words per Request)", 1, 15, 10)
        max_safe      = (max(1, math.ceil(len(subset) / max(1, requests_left)))
                         if requests_left > 0 else 1)
        batch_size    = min(raw_batch, max_safe)
        st.session_state.last_batch_size = batch_size
        st.caption(f"✅ Effective batch size: **{batch_size}** (quota-adjusted from {raw_batch})")

        include_audio                     = st.checkbox("🔊 Generate Audio Files",             value=True)
        st.session_state.include_antonyms = st.checkbox("➖ Include Antonyms in Card Back",     value=st.session_state.include_antonyms)
        st.session_state.dry_run          = st.checkbox("🔬 Dry Run Mode (simulate, no quota)", value=st.session_state.dry_run)

        # T2-C: Duplicate detection column
        def _is_dup(vocab_raw: str) -> bool:
            return hashlib.sha256(str(vocab_raw).lower().encode('utf-8')).hexdigest()[:16] \
                in st.session_state.exported_hashes

        subset_display = subset.copy()
        subset_display['Export']             = True
        subset_display['⚠️ Prev. Exported'] = subset_display['vocab'].apply(_is_dup)

        st.write("**Select words to export:**")
        edited_export = st.data_editor(
            subset_display,
            column_config={
                "Export": st.column_config.CheckboxColumn("Export?", required=True),
                "⚠️ Prev. Exported": st.column_config.CheckboxColumn(
                    "Prev. Exported?", disabled=True,
                    help="This word was already exported to Anki this session."),
            },
            hide_index=True,
            disabled=["vocab", "phrase", "status", "tags", "⚠️ Prev. Exported"]
        )

        final_export = edited_export[edited_export['Export'].astype(bool)]
        dup_count    = int(final_export['⚠️ Prev. Exported'].astype(bool).sum()) \
            if '⚠️ Prev. Exported' in final_export.columns else 0
        if dup_count > 0:
            st.warning(f"⚠️ **{dup_count}** selected word(s) were previously exported this session.")

        if not final_export.empty:
            st.write("### Export Preview")
            st.dataframe(final_export[['vocab', 'phrase']], hide_index=True)
            card_count  = len(final_export)
            per_card_kb = 35.0 if include_audio else 2.5
            est_size_kb = card_count * per_card_kb
            size_label  = f"{est_size_kb / 1024:.2f} MB" if est_size_kb > 1024 else f"{est_size_kb:.1f} KB"
            st.info(f"📊 **{card_count} cards** • Est. .apkg size: **{size_label}** "
                    f"({'with audio ~35 KB/card' if include_audio else 'no audio ~2.5 KB/card'})")

        quota_key = (st.session_state.rpd_count, len(final_export), batch_size)
        if st.session_state._quota_cache_key != quota_key:
            r_left = max(0, 20 - st.session_state.rpd_count)
            r_req  = math.ceil(len(final_export) / batch_size) if not final_export.empty else 0
            st.session_state._quota_cache     = (r_left, r_req)
            st.session_state._quota_cache_key = quota_key
        requests_left, required_requests = st.session_state._quota_cache

        st.info(f"💡 You have **{requests_left}** API requests left today. "
                f"This batch requires **{required_requests}** request(s).")

        if final_export.empty:
            st.warning("Select at least one word to export.")
        elif required_requests > requests_left and not st.session_state.dry_run:
            st.error("🛑 Exceeds Daily Limit! Reduce your selection or increase batch size.")
        else:
            if st.button("🚀 Generate Deck", type="primary", use_container_width=True):
                st.session_state.failed_words = []
                raw_notes = []
                try:
                    raw_notes = process_anki_data(final_export, batch_size=batch_size,
                                                  dry_run=st.session_state.dry_run)
                    if raw_notes:
                        # X2-A: route to card editor instead of packing immediately
                        st.session_state.editing_notes     = raw_notes
                        st.session_state.editing_deck_name = deck_name_input
                        st.session_state.editing_audio     = include_audio
                        st.rerun(scope="app")
                except Exception as e:
                    st.error(f"❌ Generation error: {e} — Status rolled back to 'New'.")
                    if raw_notes:
                        failed = [n.get('VocabRaw', '') for n in raw_notes]
                        st.session_state.vocab_df.loc[
                            st.session_state.vocab_df['vocab'].isin(failed), 'status'] = 'New'
                        save_to_github(st.session_state.vocab_df)

        # N4-E: Export History Viewer
        st.divider()
        with st.expander("📜 Export History", expanded=False):
            if st.button("🔄 Load History", use_container_width=True):
                load_export_history.clear()
            history = load_export_history()
            if not history:
                st.info("No export history found. Generate your first deck to start logging.")
            else:
                rows = []
                for entry in reversed(history):
                    ts = entry.get("timestamp", "")
                    try: ts = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
                    except: pass
                    rows.append({
                        "Timestamp": ts, "Deck": entry.get("deck_name", ""),
                        "Cards":     entry.get("card_count", 0),
                        "Vocab":     ", ".join(entry.get("vocabs", [])[:10])
                                     + ("…" if len(entry.get("vocabs", [])) > 10 else ""),
                    })
                hist_df  = pd.DataFrame(rows)
                h_page   = st.number_input("History page", min_value=1,
                                           max_value=max(1, math.ceil(len(hist_df) / 10)),
                                           value=1, step=1, key="hist_page")
                h_start  = (h_page - 1) * 10
                st.dataframe(hist_df.iloc[h_start:h_start + 10],
                             hide_index=True, use_container_width=True)
                st.caption(f"Showing {min(h_start + 10, len(hist_df))} of {len(hist_df)} records.")

    render_tab3()
