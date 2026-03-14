def get_robust_youtube_transcript(video_id):
    """Multi-stage, highly robust YouTube Extractor with empty-state handling."""
    raw_text = ""
    
    # --- STAGE 1: Official Library (Best Approach) ---
    if YOUTUBE_AVAILABLE:
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            # Try to fetch ANY available transcript (prefers manual, fallback to auto-generated)
            transcript = transcript_list.find_transcript(['en', 'en-US', 'en-GB']) 
            raw_text = " ".join([t['text'] for t in transcript.fetch()]).strip()
            if len(raw_text) > 15: return raw_text
        except Exception:
            try:
                # If no English, grab the very first available transcript (any language)
                for transcript in transcript_list:
                    raw_text = " ".join([t['text'] for t in transcript.fetch()]).strip()
                    if len(raw_text) > 15: return raw_text
            except Exception:
                pass # Proceed to fallback

    # --- STAGE 2: Proxy API Fallback ---
    try:
        proxy_resp = requests.get(f"https://youtubetranscript.com/?server_vid2={video_id}", timeout=10)
        if '<transcript>' in proxy_resp.text or '<?xml' in proxy_resp.text:
            clean_text = re.sub(r'<[^>]+>', ' ', proxy_resp.text)
            clean_text = html.unescape(clean_text)
            raw_text = re.sub(r'\s+', ' ', clean_text).strip()
            
            # Ensure it's not a generic proxy error masquerading as a success
            if len(raw_text) > 15 and "server error" not in raw_text.lower():
                return raw_text
    except Exception:
        pass
        
    raise ValueError("Failed to extract transcript. The video may not have closed captions enabled, is a music track without dialogue, or is region-locked.")
