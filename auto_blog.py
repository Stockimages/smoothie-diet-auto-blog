"""
Fully automatic publisher for The Smoothie Diet Daily (affiliate blog).

Every run (triggered by cron-job.org, 6:30 AM and 6:30 PM PKT — same job,
no AM/PM mode split):
  1. Reads history.json so Gemini doesn't repeat itself
  2. Picks article_type: ~60% recipe (soft CTA) / ~40% review (strong CTA)
  3. Asks Gemini for the full article (JSON), a Pinterest hook, a hero image
     query, per-section image queries, and a short voiceover script
  4. Fetches a vertical hero photo + section photos from Pexels
  5. Uploads images to Cloudflare R2 (so Blogger/Pinterest/Tumblr can all
     reference public URLs without bloating this git repo)
  6. Publishes the article to Blogger (with affiliate disclosure + CTA link)
  7. Builds a short narrated vertical video (Ken Burns + AI voiceover) from
     the same images and posts it as a Pinterest VIDEO pin (every run)
  8. Posts the hero image + caption to Tumblr (every run)
  9. Saves history, sends a phone notification

This reuses the same tested patterns as the DecorVibe repo (R2 storage,
Blogger/Pinterest OAuth refresh-token flows, ffmpeg video builder, retry
logic) adapted for this niche's content rules and publishing schedule.
"""

import os
import re
import json
import base64
import subprocess
import textwrap
import random
import time
import asyncio
import smtplib
from email.mime.text import MIMEText
from io import BytesIO
from datetime import datetime, timezone

import requests
from requests_oauthlib import OAuth1Session
import edge_tts
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Required / optional environment variables (set these as GitHub Secrets)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

BLOGGER_BLOG_ID = os.environ["BLOGGER_BLOG_ID"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
SITE_URL = os.environ.get("SITE_URL", "https://thesmoothiedietdaily.blogspot.com")

PINTEREST_APP_ID = os.environ["PINTEREST_APP_ID"]
PINTEREST_APP_SECRET = os.environ["PINTEREST_APP_SECRET"]
PINTEREST_REFRESH_TOKEN = os.environ["PINTEREST_REFRESH_TOKEN"]
PINTEREST_BOARD_ID = os.environ["PINTEREST_BOARD_ID"]

TUMBLR_CONSUMER_KEY = os.environ["TUMBLR_CONSUMER_KEY"]
TUMBLR_CONSUMER_SECRET = os.environ["TUMBLR_CONSUMER_SECRET"]
TUMBLR_ACCESS_TOKEN = os.environ["TUMBLR_OAUTH_TOKEN"]
TUMBLR_ACCESS_TOKEN_SECRET = os.environ["TUMBLR_OAUTH_TOKEN_SECRET"]
TUMBLR_BLOG_NAME = os.environ["TUMBLR_BLOG_IDENTIFIER"]

AFFILIATE_LINK = os.environ["AFFILIATE_LINK"]

# Fine-grained PAT scoped to this repo only ("Secrets: read and write") —
# lets the script auto-update PINTEREST_REFRESH_TOKEN if Pinterest ever
# rotates it. If not set, a new token is just printed in the logs instead.
GH_SECRETS_PAT = os.environ.get("GH_SECRETS_PAT")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "your-username/your-repo")

# Cloudflare R2 (image/video hosting) — same pattern as DecorVibe.
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

# Optional phone notification via Gmail SMTP.
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", GMAIL_ADDRESS)

CONFIG_FILE = "config.json"
HISTORY_FILE = "history.json"
DEFAULT_WAIT_SECONDS = [5, 15, 30]

GEMINI_MODELS = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
    "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
]

AFFILIATE_DISCLOSURE = (
    "This post contains affiliate links. We may earn a commission at no "
    "additional cost to you."
)

# Words/phrases that make weight-loss content sound like generic AI filler
# OR cross into risky guarantee/medical-claim territory — banned outright.
BANNED_PHRASES = [
    "unlock", "delve", "in today's fast-paced world", "game-changer",
    "elevate", "unleash", "harness the power", "furthermore", "in conclusion",
    "guaranteed", "cure", "treat", "melts fat", "lose weight fast",
    "in just days", "miracle",
]

CONTENT_MIX = {"recipe": 0.6, "review": 0.4}


# ---------------------------------------------------------------------------
# Cloudflare R2 helpers (verbatim pattern from DecorVibe)
# ---------------------------------------------------------------------------

_r2_client = None


def get_r2_client():
    global _r2_client
    if _r2_client is None:
        import boto3
        from botocore.config import Config as BotoConfig
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=BotoConfig(signature_version="s3v4"),
            region_name="auto",
        )
    return _r2_client


_R2_CONTENT_TYPES = {
    ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".mp4": "video/mp4",
}


def upload_to_r2(local_path):
    key = local_path.replace(os.sep, "/")
    ext = os.path.splitext(local_path)[1].lower()
    content_type = _R2_CONTENT_TYPES.get(ext, "application/octet-stream")
    client = get_r2_client()
    with open(local_path, "rb") as f:
        client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=f, ContentType=content_type)
    return f"{R2_PUBLIC_URL}/{key}"


def delete_from_r2(local_path):
    if not local_path:
        return
    try:
        key = local_path.replace(os.sep, "/")
        get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)
        print(f"Cleaned up from R2: {key}")
    except Exception as e:  # noqa: BLE001
        print(f"R2 cleanup failed for {local_path} (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Networking / retry helper (verbatim pattern from DecorVibe)
# ---------------------------------------------------------------------------

def robust_request(method, url, max_attempts=4, wait_seconds=None,
                    retry_statuses=(429, 500, 502, 503, 504), **kwargs):
    wait_seconds = wait_seconds or DEFAULT_WAIT_SECONDS
    last_response = None
    for attempt in range(1, max_attempts + 1):
        is_last_attempt = attempt == max_attempts
        try:
            res = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            if is_last_attempt:
                raise RuntimeError(f"Request to {url} failed after {max_attempts} attempts: {e}")
            delay = wait_seconds[min(attempt - 1, len(wait_seconds) - 1)]
            print(f"Network error calling {url} ({e}), retrying in {delay}s...")
            time.sleep(delay)
            continue
        if res.ok or res.status_code not in retry_statuses or is_last_attempt:
            return res
        last_response = res
        delay = wait_seconds[min(attempt - 1, len(wait_seconds) - 1)]
        print(f"{url} returned {res.status_code}, retrying in {delay}s...")
        time.sleep(delay)
    return last_response


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def git_commit_and_push(paths, message, max_attempts=3):
    subprocess.run(["git", "config", "user.email", "auto-blog-bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "config", "user.name", "auto-blog-bot"], check=True)
    subprocess.run(["git", "add", *paths], check=True)
    result = subprocess.run(["git", "commit", "-m", message])
    if result.returncode != 0:
        return
    for attempt in range(1, max_attempts + 1):
        push_result = subprocess.run(["git", "push"])
        if push_result.returncode == 0:
            return
        if attempt == max_attempts:
            raise RuntimeError("git push failed after retries.")
        print(f"git push failed (attempt {attempt}/{max_attempts}), retrying...")
        subprocess.run(["git", "fetch", "--unshallow"], check=False)
        subprocess.run(["git", "pull", "--rebase"], check=True)


# ---------------------------------------------------------------------------
# Gemini content generation (6-model fallback, tried once each per cycle)
# ---------------------------------------------------------------------------

def pick_article_type():
    choices = list(CONTENT_MIX.keys())
    weights = list(CONTENT_MIX.values())
    return random.choices(choices, weights=weights, k=1)[0]


def generate_draft(history, article_type):
    recent_titles = [h["title"] for h in history[-300:]]
    banned_list = ", ".join(f'"{w}"' for w in BANNED_PHRASES)

    if article_type == "recipe":
        topic_instruction = (
            "Write a smoothie recipe article for any wellness goal (energy, digestion, "
            "glowing skin, post-workout recovery, gut health, etc). This is a FREE, "
            "genuinely useful recipe post — the affiliate mention should be a single "
            "soft, low-pressure line, not the focus of the article."
        )
        extra_schema_fields = """
  "ingredients": [{"item": "string", "amount": "string"}],
  "calories": "approx per serving, e.g. '180 kcal'",
  "prep_time": "e.g. '5 min'","""
    else:
        topic_instruction = (
            "Write a review, comparison, or 'does it actually work' style article about "
            "smoothie-based weight-management diets and 21-day smoothie meal plans in "
            "general. This article should build genuine trust (real pros AND real cons) "
            "before mentioning the affiliate program with a soft, low-pressure call to "
            "action — same gentle tone as the recipe articles, just naturally more direct "
            "since the topic itself is about the program."
        )
        extra_schema_fields = """
  "pros": ["string", "string", "string"],
  "cons": ["string", "string"],"""

    prompt = f"""You are a real person who writes for a smoothie/wellness blog that also
promotes an affiliate 21-day smoothie weight-management program. Every reader
could be someone with a real, sometimes difficult relationship with their body
and food — write with warmth, respect, and zero judgment.

{topic_instruction}

STRICT CONTENT RULES (never break these):
1. NEVER use specific numeric weight-loss claims (no "lose X lbs", no "in X
   days" guarantees, no before/after framing). Use soft phrasing like "may
   support your weight-management goals" or "many people find".
2. NEVER use guarantee/cure/treat language.
3. NEVER reference or imply a specific body type, "ideal" body, or before/after
   transformation photos.
4. Every article must include this exact affiliate disclosure sentence
   somewhere natural near the top: "{AFFILIATE_DISCLOSURE}"
5. NEVER use these overused/risky words or phrases, in any form: {banned_list}.
6. Write like a real person talking to a friend — vary sentence length, use
   contractions, be specific and concrete. No generic filler.

Topics already covered (do NOT repeat these or anything too similar):
{json.dumps(recent_titles, ensure_ascii=False)}

LENGTH: 700-1000 words.

CRITICAL JSON-SAFETY RULE: inside the "html" string, use SINGLE quotes for
every HTML attribute value. Never use a double-quote character inside the
html string.

STRUCTURE (HTML using ONLY p, h2, h3, ul, ol, li, strong tags):
1. Opening hook paragraph (this is what Pinterest/Google show as preview).
2. A few h2/h3 sections.
3. IMAGE PLACEHOLDERS: after the intro and after 1-2 major sections, insert
   on its own line: [[IMG_1]], then [[IMG_2]] — 1-2 total, sequential.

Also write:
- "pin_hook": punchy Pinterest pin text, 5-8 words, no ending punctuation.
- "image_prompt": 3-5 keyword search terms for a REAL, vertical food/lifestyle
  photo (smoothies, fresh fruit, healthy kitchen scenes) — no people's faces,
  no text, no brand names.
- "section_images": list matching [[IMG_n]] placeholders, each with "token"
  and "query" (3-5 keywords, same style as image_prompt).
- "cta_text": one soft, low-pressure sentence pointing toward the affiliate
  program — same gentle tone for both recipe and review articles, still
  following all the content rules above.
- "key_benefit": one-line FDA-safe benefit claim.
- "reel_script": 45-65 word spoken-word voiceover script for a ~20 second
  vertical video. Punchy hook first sentence. NO call-to-action/link/bio
  line in it (that's added separately as a closing slide).

Return ONLY valid JSON matching exactly this schema (no markdown fences):
{{
  "article_type": "{article_type}",
  "title": "string",
  "sections": [{{"heading": "string", "html": "string with [[IMG_n]] placeholders"}}],{extra_schema_fields}
  "image_prompt": "string",
  "section_images": [{{"token": "IMG_1", "query": "string"}}],
  "pin_hook": "string",
  "cta_text": "string",
  "key_benefit": "string",
  "reel_script": "string",
  "hashtag_tags": ["string", "string"]
}}"""

    last_error = None
    for model in GEMINI_MODELS:
        try:
            res = robust_request(
                "POST",
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=120,
                max_attempts=1,  # each model tried once per cycle -- the 6-model list IS the retry strategy
            )
            if not res or not res.ok:
                last_error = f"{model}: {getattr(res, 'status_code', 'no response')}"
                print(f"[gemini] {model} failed: {last_error}")
                continue
            text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(text)
            print(f"[gemini] success with model {model}")
            return parsed
        except Exception as e:  # noqa: BLE001
            last_error = f"{model}: {e}"
            print(f"[gemini] {model} failed: {e}")
            continue
    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


def normalize_draft(draft, article_type):
    if not draft.get("title"):
        raise RuntimeError("Gemini response is missing required field 'title'.")
    if not draft.get("sections"):
        raise RuntimeError("Gemini response is missing required field 'sections'.")

    draft.setdefault("article_type", article_type)
    draft.setdefault("pin_hook", draft["title"])
    draft.setdefault("image_prompt", "fresh smoothie glass healthy kitchen")
    draft.setdefault("cta_text", "Curious about the full 21-day plan? Take a look here.")
    draft.setdefault("key_benefit", "May support your overall wellness routine.")
    draft.setdefault(
        "reel_script",
        f"{draft.get('pin_hook', draft['title'])}. Here's a simple way to start your day right.",
    )
    if not isinstance(draft.get("section_images"), list):
        draft["section_images"] = []
    else:
        draft["section_images"] = [
            s for s in draft["section_images"] if isinstance(s, dict) and s.get("query")
        ]
    if not isinstance(draft.get("hashtag_tags"), list):
        draft["hashtag_tags"] = ["smoothie", "wellness"]

    if article_type == "recipe":
        if not isinstance(draft.get("ingredients"), list):
            draft["ingredients"] = []
        draft.setdefault("calories", "See recipe for details")
        draft.setdefault("prep_time", "5-10 min")
    else:
        if not isinstance(draft.get("pros"), list):
            draft["pros"] = []
        if not isinstance(draft.get("cons"), list):
            draft["cons"] = []

    return draft


# ---------------------------------------------------------------------------
# Pexels images (verbatim pattern from DecorVibe)
# ---------------------------------------------------------------------------

def search_pexels_image(query, orientation="portrait", used_photo_ids=None, target_ratio=None):
    used_photo_ids = used_photo_ids or set()
    res = robust_request(
        "GET", "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": orientation, "per_page": 30},
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Pexels search failed ({res.status_code}): {res.text}")
    photos = res.json().get("photos", [])
    if not photos:
        res = robust_request(
            "GET", "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": "healthy smoothie", "orientation": orientation, "per_page": 30},
            timeout=30,
        )
        photos = res.json().get("photos", []) if res.ok else []
        if not photos:
            raise RuntimeError(f"No Pexels photos found for query: {query}")

    unused = [p for p in photos if p["id"] not in used_photo_ids]
    candidates = unused or photos

    if target_ratio:
        close_enough = [
            p for p in candidates
            if p.get("width") and p.get("height")
            and abs((p["width"] / p["height"]) - target_ratio) / target_ratio < 0.35
        ]
        photo = random.choice(close_enough) if close_enough else random.choice(candidates)
    else:
        photo = random.choice(candidates)

    image_url = photo["src"]["large2x"]
    image_res = robust_request("GET", image_url, timeout=30)
    if not image_res.ok:
        raise RuntimeError(f"Pexels image download failed ({image_res.status_code})")
    return image_res.content, photo["id"]


def search_pexels_video(query, used_video_ids=None, target_width=1080):
    used_video_ids = used_video_ids or set()
    res = robust_request(
        "GET", "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": "portrait", "per_page": 15},
        timeout=30,
    )
    if not res.ok:
        return None
    videos = res.json().get("videos", [])
    if not videos:
        return None
    unused = [v for v in videos if v["id"] not in used_video_ids] or videos
    video = random.choice(unused)

    portrait_files = [f for f in video["video_files"] if f.get("width") and f.get("height") and f["height"] > f["width"]]
    candidates = portrait_files or video["video_files"]
    best_file = min(candidates, key=lambda f: abs((f.get("width") or 9999) - target_width))

    file_res = robust_request("GET", best_file["link"], timeout=60)
    if not file_res.ok:
        return None
    return {"bytes": file_res.content, "id": video["id"], "duration": video.get("duration", 4)}


# Broad, near-always-available fallback queries, tried in order, so every
# reel slide gets REAL footage -- never a static photo standing in for video.
GENERIC_VIDEO_FALLBACKS = [
    "smoothie making", "blending fruit smoothie", "pouring smoothie glass",
    "healthy smoothie drink", "fresh fruit blender",
]


def get_video_clip_with_fallback(primary_query, used_video_ids):
    for query in [primary_query, *GENERIC_VIDEO_FALLBACKS]:
        try:
            clip = search_pexels_video(query, used_video_ids=used_video_ids)
        except Exception as e:  # noqa: BLE001
            print(f"Pexels video search failed for '{query}' (trying next fallback): {e}")
            continue
        if clip:
            return clip
    return None


def compress_image(image_bytes, max_width=1200, quality=78):
    img = Image.open(BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="WEBP", quality=quality)
    return out.getvalue()


FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
BALOO_FONT_PATH = "assets/fonts/Baloo2-Bold.ttf"
CAVEAT_FONT_PATH = "assets/fonts/Caveat-Bold.ttf"


def _load_bold_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _load_display_font(size, weight=b"ExtraBold"):
    """Rounded, punchy display font (Baloo 2) for pin headlines. Falls back
    to the plain bold font if the asset isn't present."""
    if os.path.exists(BALOO_FONT_PATH):
        font = ImageFont.truetype(BALOO_FONT_PATH, size)
        try:
            names = font.get_variation_names()
            font.set_variation_by_name(weight if weight in names else names[-1])
        except Exception:  # noqa: BLE001
            pass
        return font
    return _load_bold_font(size)


def _load_script_font(size):
    """Handwritten-style accent font (Caveat) for small taglines."""
    if os.path.exists(CAVEAT_FONT_PATH):
        font = ImageFont.truetype(CAVEAT_FONT_PATH, size)
        try:
            names = font.get_variation_names()
            if b"Bold" in names:
                font.set_variation_by_name(b"Bold")
        except Exception:  # noqa: BLE001
            pass
        return font
    return _load_bold_font(size)


def crop_to_ratio(img, target_ratio=9 / 16):
    w, h = img.size
    current_ratio = w / h
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    return img


# ---------------------------------------------------------------------------
# Video builder (Ken Burns + AI voiceover -- verbatim pattern from DecorVibe)
# ---------------------------------------------------------------------------

REEL_VOICES = ["en-US-AvaMultilingualNeural", "en-US-EmmaMultilingualNeural", "en-US-JennyNeural"]
MUSIC_DIR = "music"


def pick_background_music():
    if not os.path.isdir(MUSIC_DIR):
        return None
    tracks = [os.path.join(MUSIC_DIR, f) for f in os.listdir(MUSIC_DIR) if f.lower().endswith(".mp3")]
    return random.choice(tracks) if tracks else None


def synthesize_voiceover(script_text, out_path, voice=None):
    voice = voice or random.choice(REEL_VOICES)
    pitch_offset = random.randint(-15, 5)

    async def _run():
        communicate = edge_tts.Communicate(script_text, voice, rate="-4%", pitch=f"{pitch_offset:+d}Hz")
        await communicate.save(out_path)

    asyncio.run(_run())


def get_audio_duration_seconds(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(res.stdout.strip())


def split_script_into_captions(script_text, n_parts):
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", script_text.strip()) if s.strip()]
    if len(sentences) >= n_parts:
        groups = [[] for _ in range(n_parts)]
        for i, sentence in enumerate(sentences):
            idx = min(i * n_parts // len(sentences), n_parts - 1)
            groups[idx].append(sentence)
        return [" ".join(g).strip() or sentences[min(i, len(sentences) - 1)] for i, g in enumerate(groups)]
    words = script_text.split()
    per = max(1, len(words) // n_parts)
    chunks = [" ".join(words[i:i + per]) for i in range(0, len(words), per)]
    while len(chunks) < n_parts:
        chunks.append(chunks[-1] if chunks else script_text)
    return chunks[:n_parts]


def build_caption_overlay_png(text, width=1080, is_cta=False):
    font_size = 58 if is_cta else 50
    font = _load_bold_font(font_size)
    text_color = (20, 20, 20, 255) if is_cta else (255, 255, 255, 255)
    bg_color = (255, 205, 60, 235) if is_cta else (0, 0, 0, 150)

    dummy_img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy_img)
    wrapped = textwrap.fill(text, width=24)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=10, align="center")
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_v = 30 if is_cta else 26
    bar_h = text_h + pad_v * 2

    img = Image.new("RGBA", (width, bar_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (width, bar_h)], fill=bg_color)
    x = (width - text_w) / 2 - bbox[0]
    y = pad_v - bbox[1]
    draw.multiline_text((x, y), wrapped, font=font, fill=text_color, align="center", spacing=10)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def build_watermark_overlay_png(brand_text="The Smoothie Diet Daily", canvas_size=(1080, 1920)):
    font = _load_bold_font(26)
    img = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text_w = draw.textlength(brand_text, font=font)
    margin = 36
    x = canvas_size[0] - text_w - margin
    y = margin
    draw.text((x + 2, y + 2), brand_text, font=font, fill=(0, 0, 0, 110))
    draw.text((x, y), brand_text, font=font, fill=(255, 255, 255, 170))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# Brand palette (matches the site's pink/green smoothie-glass logo).
COVER_PALETTES = {
    "recipe": {"accent": (27, 94, 32), "highlight": (255, 205, 60), "banner": (27, 94, 32), "badge": (232, 90, 138)},
    "review": {"accent": (232, 90, 138), "highlight": (255, 205, 60), "banner": (232, 90, 138), "badge": (76, 175, 80)},
}


def _draw_text_with_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0, 110), offset=3, **kwargs):
    x, y = xy
    draw.text((x + offset, y + offset), text, font=font, fill=shadow, **kwargs)
    draw.text((x, y), text, font=font, fill=fill, **kwargs)


def build_infographic_cover(photo_bytes, headline_text, tagline_text, bullets, cta_text, article_type,
                             width=1080, height=1350):
    """Text-overlay cover image matching the high-performing competitor pin
    style (script accent + bold highlighted headline + checklist + bottom CTA
    banner) — every string is code-drawn (never AI-generated) so spelling and
    disclosure-adjacent copy are always exactly correct."""
    palette = COVER_PALETTES.get(article_type, COVER_PALETTES["recipe"])

    photo = Image.open(BytesIO(photo_bytes)).convert("RGB")
    photo = crop_to_ratio(photo, target_ratio=width / height).resize((width, height), Image.LANCZOS)
    canvas = photo.convert("RGBA")

    # Soft gradient scrim at top (headline legibility) and bottom (banner legibility)
    scrim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    top_h, bottom_h = int(height * 0.34), int(height * 0.22)
    for i in range(top_h):
        alpha = int(190 * (1 - i / top_h))
        scrim_draw.line([(0, i), (width, i)], fill=(255, 255, 255, alpha))
    for i in range(bottom_h):
        alpha = int(215 * (i / bottom_h))
        scrim_draw.line([(0, height - bottom_h + i), (width, height - bottom_h + i)], fill=(*palette["banner"], alpha))
    canvas = Image.alpha_composite(canvas, scrim)
    draw = ImageDraw.Draw(canvas, "RGBA")

    margin = 56
    cursor_y = 44

    # 1. Small script tagline
    script_font = _load_script_font(52)
    _draw_text_with_shadow(draw, (margin, cursor_y), tagline_text, script_font, fill=(*palette["accent"], 255),
                            shadow=(255, 255, 255, 160), offset=2)
    cursor_y += 66

    # 2. Big headline, word-wrapped, alternating plain / highlighted-marker lines
    headline_font = _load_display_font(78)
    words = headline_text.upper().split()
    lines, current = [], []
    for word in words:
        test = " ".join(current + [word])
        if draw.textlength(test, font=headline_font) > width - 2 * margin and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    lines = lines[:3]

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        line_w = bbox[2] - bbox[0]
        line_top, line_bottom = bbox[1], bbox[3]
        x = margin
        if i == min(1, len(lines) - 1):  # highlight the 2nd line (or last, if only 1-2 lines)
            pad_x, pad_y = 14, 10
            draw.rounded_rectangle(
                [(x - pad_x, cursor_y + line_top - pad_y), (x + line_w + pad_x, cursor_y + line_bottom + pad_y)],
                radius=10, fill=(*palette["highlight"], 235),
            )
            _draw_text_with_shadow(draw, (x, cursor_y), line, headline_font, fill=(20, 20, 20, 255),
                                    shadow=(0, 0, 0, 0))
        else:
            _draw_text_with_shadow(draw, (x, cursor_y), line, headline_font, fill=(255, 255, 255, 255))
        cursor_y += line_bottom + 28

    # 3. Checklist bullets (calories/prep-time for recipes, key pros for reviews)
    cursor_y += 18
    bullet_font = _load_bold_font(38)
    for bullet in bullets[:3]:
        badge_r = 16
        cy = cursor_y + badge_r
        draw.ellipse([(margin, cy - badge_r), (margin + badge_r * 2, cy + badge_r)], fill=(*palette["badge"], 235))
        draw.text((margin + badge_r - 8, cy - badge_r + 2), "\u2713", font=_load_bold_font(24), fill=(255, 255, 255, 255))
        _draw_text_with_shadow(draw, (margin + badge_r * 2 + 16, cursor_y), bullet, bullet_font,
                                fill=(255, 255, 255, 255))
        cursor_y += 52

    # 4. Bottom CTA banner
    banner_font = _load_display_font(46, weight=b"Bold")
    banner_text = cta_text.upper()
    bbox = draw.textbbox((0, 0), banner_text, font=banner_font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if text_w > width - 2 * margin:
        banner_font = _load_display_font(34, weight=b"Bold")
        bbox = draw.textbbox((0, 0), banner_text, font=banner_font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    banner_y = height - int(height * 0.11)
    x = (width - text_w) / 2 - bbox[0]
    _draw_text_with_shadow(draw, (x, banner_y), banner_text, banner_font, fill=(255, 255, 255, 255))
    # simple arrow accent
    arrow_font = _load_bold_font(46)
    draw.text((width - margin - 40, banner_y - 4), "\u2192", font=arrow_font, fill=(*palette["highlight"], 255))

    return canvas.convert("RGB")


def build_infographic_cover_png(*args, **kwargs):
    out = BytesIO()
    build_infographic_cover(*args, **kwargs).save(out, format="PNG")
    return out.getvalue()


def build_reel_video(image_specs, audio_path, work_dir, width=1080, height=1920):
    os.makedirs(work_dir, exist_ok=True)
    fps = 30
    segment_paths = []
    scale_w, scale_h = width * 3, height * 3

    watermark_path = os.path.join(work_dir, "watermark.png")
    with open(watermark_path, "wb") as f:
        f.write(build_watermark_overlay_png(canvas_size=(width, height)))

    for i, spec in enumerate(image_specs):
        duration = max(0.8, spec["duration"])
        fade_dur = min(0.3, duration / 4)
        is_cta = bool(spec.get("is_cta"))
        is_first_slide = (i == 0)
        is_video_clip = spec.get("kind") == "video"

        caption_png_path = None
        if spec.get("caption"):
            caption_png_path = os.path.join(work_dir, f"caption_{i}.png")
            with open(caption_png_path, "wb") as f:
                f.write(build_caption_overlay_png(spec["caption"], is_cta=is_cta))
        caption_y = "H*0.42" if is_cta else "H*0.12"

        seg_path = os.path.join(work_dir, f"seg_{i}.mp4")

        if is_video_clip:
            src_path = os.path.join(work_dir, f"clip_{i}.mp4")
            with open(src_path, "wb") as f:
                f.write(spec["bytes"])
            base_vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                + ("" if is_first_slide else f"fade=t=in:st=0:d={fade_dur},")
                + f"fade=t=out:st={max(0, duration - fade_dur)}:d={fade_dur}"
            )
            inputs = ["-i", src_path]
        else:
            src_path = os.path.join(work_dir, f"img_{i}.png")
            with open(src_path, "wb") as f:
                f.write(spec["bytes"])
            target_zoom = 1.15
            frames = max(1, int(round(duration * fps)))
            zoom_rate = (target_zoom - 1.0) / frames
            base_vf = (
                f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=increase,"
                f"crop={scale_w}:{scale_h},"
                f"zoompan=z='min(zoom+{zoom_rate:.8f},{target_zoom})':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},"
                + ("" if is_first_slide else f"fade=t=in:st=0:d={fade_dur},")
                + f"fade=t=out:st={max(0, duration - fade_dur)}:d={fade_dur}"
            )
            inputs = ["-loop", "1", "-i", src_path]

        if caption_png_path:
            cmd = [
                "ffmpeg", "-y", *inputs, "-i", caption_png_path, "-i", watermark_path, "-t", str(duration),
                "-filter_complex",
                f"[0:v]{base_vf}[bg];[bg][1:v]overlay=0:{caption_y}[bg2];[bg2][2:v]overlay=0:0[out]",
                "-map", "[out]", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "23", seg_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", *inputs, "-i", watermark_path, "-t", str(duration),
                "-filter_complex", f"[0:v]{base_vf}[bg];[bg][1:v]overlay=0:0[out]",
                "-map", "[out]", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "23", seg_path,
            ]
        subprocess.run(cmd, check=True, capture_output=True)
        segment_paths.append(seg_path)

    concat_list_path = os.path.join(work_dir, "concat.txt")
    with open(concat_list_path, "w") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    silent_video_path = os.path.join(work_dir, "silent.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
         "-c:v", "libx264", "-preset", "fast", "-crf", "23", silent_video_path],
        check=True, capture_output=True,
    )

    final_path = os.path.join(work_dir, "final.mp4")
    music_path = pick_background_music()
    if music_path:
        subprocess.run(
            ["ffmpeg", "-y", "-i", silent_video_path, "-i", audio_path,
             "-stream_loop", "-1", "-i", music_path,
             "-filter_complex",
             "[2:a]volume=0.12[music];[1:a][music]amix=inputs=2:duration=first:dropout_transition=0[mixed]",
             "-map", "0:v", "-map", "[mixed]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
             "-shortest", "-movflags", "+faststart", final_path],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-i", silent_video_path, "-i", audio_path,
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
             "-shortest", "-movflags", "+faststart", final_path],
            check=True, capture_output=True,
        )

    with open(final_path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Blogger (verbatim pattern from DecorVibe)
# ---------------------------------------------------------------------------

def get_access_token():
    res = robust_request(
        "POST", "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN, "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Could not refresh Google access token: {res.text}")
    return res.json()["access_token"]


def publish_post(access_token, title, html, labels, search_description=None):
    payload = {"title": title, "content": html, "labels": labels}
    if search_description:
        payload["searchDescription"] = search_description[:150]
    res = robust_request(
        "POST", f"https://www.googleapis.com/blogger/v3/blogs/{BLOGGER_BLOG_ID}/posts/",
        headers={"Authorization": f"Bearer {access_token}"}, json=payload, timeout=60,
    )
    if not res.ok:
        raise RuntimeError(f"Blogger publish failed ({res.status_code}): {res.text}")
    return res.json()


def build_post_html(draft, image_urls):
    parts = [f'<p style="font-size:0.9em;color:#666"><em>{AFFILIATE_DISCLOSURE}</em></p>']

    for section in draft["sections"]:
        html = section["html"]
        for img in draft["section_images"]:
            token, url = img["token"], image_urls.get(img["token"])
            if url:
                html = html.replace(f"[[{token}]]", f'<img src="{url}" alt="{section["heading"]}" style="max-width:100%;"/>')
        parts.append(f'<h2>{section["heading"]}</h2>{html}')

    if draft["article_type"] == "recipe" and draft.get("ingredients"):
        ing_list = "".join(f'<li>{i.get("amount", "")} {i.get("item", "")}</li>' for i in draft["ingredients"])
        parts.append(f"<h3>Ingredients</h3><ul>{ing_list}</ul>")
        facts = f'<p><strong>Calories:</strong> {draft.get("calories", "")} &nbsp; <strong>Prep time:</strong> {draft.get("prep_time", "")}</p>'
        parts.insert(1, facts)

    if draft["article_type"] == "review":
        pros = "".join(f"<li>{p}</li>" for p in draft.get("pros", []))
        cons = "".join(f"<li>{c}</li>" for c in draft.get("cons", []))
        parts.append(f"<h3>Pros</h3><ul>{pros}</ul><h3>Cons</h3><ul>{cons}</ul>")

    cta_html = f'<p><a href="{AFFILIATE_LINK}" target="_blank" rel="nofollow noopener"><strong>{draft["cta_text"]}</strong></a></p>'
    parts.append(cta_html)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pinterest (verbatim pattern from DecorVibe)
# ---------------------------------------------------------------------------

def update_github_secret(secret_name, secret_value):
    if not GH_SECRETS_PAT:
        print(f"GH_SECRETS_PAT not set -- could not auto-update {secret_name}. New value (update manually):")
        print(secret_value)
        return
    try:
        from nacl import encoding, public
        headers = {"Authorization": f"Bearer {GH_SECRETS_PAT}", "Accept": "application/vnd.github+json"}
        key_res = robust_request(
            "GET", f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/public-key",
            headers=headers, timeout=30,
        )
        key_res.raise_for_status()
        key_data = key_res.json()
        public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
        sealed_box = public.SealedBox(public_key)
        encrypted_b64 = base64.b64encode(sealed_box.encrypt(secret_value.encode("utf-8"))).decode("utf-8")
        put_res = robust_request(
            "PUT", f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/{secret_name}",
            headers=headers, json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]}, timeout=30,
        )
        print(f"Auto-updated GitHub secret: {secret_name}" if put_res.status_code in (201, 204)
              else f"Failed to auto-update {secret_name} ({put_res.status_code}): {put_res.text}")
    except Exception as e:  # noqa: BLE001
        print(f"Could not auto-update {secret_name} (new value below, update manually): {e}")
        print(secret_value)


def get_pinterest_access_token():
    basic_auth = base64.b64encode(f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}".encode()).decode()
    res = robust_request(
        "POST", "https://api.pinterest.com/v5/oauth/token",
        headers={"Authorization": f"Basic {basic_auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": PINTEREST_REFRESH_TOKEN},
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Could not refresh Pinterest access token: {res.text}")
    data = res.json()
    new_refresh_token = data.get("refresh_token")
    if new_refresh_token and new_refresh_token != PINTEREST_REFRESH_TOKEN:
        print("Pinterest issued a new refresh_token -- updating GitHub secret...")
        update_github_secret("PINTEREST_REFRESH_TOKEN", new_refresh_token)
    return data["access_token"]


def create_pinterest_video_pin(access_token, board_id, title, description, link, video_bytes, cover_image_url):
    register_res = robust_request(
        "POST", "https://api.pinterest.com/v5/media",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"media_type": "video"}, timeout=30,
    )
    if not register_res.ok:
        raise RuntimeError(f"Pinterest media registration failed ({register_res.status_code}): {register_res.text}")
    media_info = register_res.json()
    media_id, upload_url, upload_fields = media_info["media_id"], media_info["upload_url"], media_info["upload_parameters"]

    upload_res = requests.post(upload_url, data=upload_fields, files={"file": ("video.mp4", video_bytes)}, timeout=120)
    if not upload_res.ok:
        raise RuntimeError(f"Pinterest video upload failed ({upload_res.status_code}): {upload_res.text}")

    for attempt in range(15):
        time.sleep(8)
        status_res = robust_request("GET", f"https://api.pinterest.com/v5/media/{media_id}",
                                     headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        status = status_res.json().get("status") if status_res.ok else None
        print(f"Pinterest video processing status (attempt {attempt + 1}/15): {status}")
        if status == "succeeded":
            break
        if status == "failed":
            raise RuntimeError("Pinterest media processing failed.")
    else:
        raise RuntimeError("Pinterest media never finished processing in time.")

    pin_res = robust_request(
        "POST", "https://api.pinterest.com/v5/pins",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "board_id": board_id, "title": title[:100], "description": description[:500], "link": link,
            "media_source": {"source_type": "video_id", "cover_image_url": cover_image_url, "media_id": media_id},
        },
        timeout=60,
    )
    if not pin_res.ok:
        raise RuntimeError(f"Pinterest video pin creation failed ({pin_res.status_code}): {pin_res.text}")
    return pin_res.json()


def build_pin_hashtags(labels, max_tags=5):
    tags = []
    for label in labels[:max_tags]:
        tag = re.sub(r"[^a-zA-Z0-9 ]", "", label).title().replace(" ", "")
        if tag and f"#{tag}" not in tags:
            tags.append(f"#{tag}")
    return " ".join(tags)


# ---------------------------------------------------------------------------
# Tumblr (adapted from DecorVibe: image is referenced by its R2 URL, no
# multipart upload needed, since the hero image already lives in R2)
# ---------------------------------------------------------------------------

def post_to_tumblr(title, key_benefit, image_url, link, hashtags):
    try:
        oauth = OAuth1Session(
            TUMBLR_CONSUMER_KEY, client_secret=TUMBLR_CONSUMER_SECRET,
            resource_owner_key=TUMBLR_ACCESS_TOKEN, resource_owner_secret=TUMBLR_ACCESS_TOKEN_SECRET,
        )
        content = [
            {"type": "image", "media": [{"url": image_url}]},
            {"type": "text", "text": title, "subtype": "heading1"},
            {"type": "text", "text": key_benefit},
            {"type": "text", "text": AFFILIATE_DISCLOSURE},
            {"type": "link", "url": link, "display_url": link, "title": "Read the Full Post"},
            {"type": "text", "text": hashtags},
        ]
        res = oauth.post(f"https://api.tumblr.com/v2/blog/{TUMBLR_BLOG_NAME}/posts", json={"content": content}, timeout=30)
        if res.ok:
            print("Posted to Tumblr:", res.json().get("response", {}).get("id"))
            return True
        print(f"Tumblr post failed ({res.status_code}): {res.text}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"Tumblr post failed (blog post is still published fine): {e}")
        return False


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_phone_notification(subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and NOTIFY_EMAIL):
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = NOTIFY_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
    except Exception as e:  # noqa: BLE001
        print(f"Notification email failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    history = load_history()
    article_type = pick_article_type()
    print(f"[main] article_type = {article_type}")

    draft = generate_draft(history, article_type)
    draft = normalize_draft(draft, article_type)

    used_photo_ids = set()
    for h in history[-100:]:
        used_photo_ids.update(h.get("photo_ids", []))

    local_paths, image_urls, this_run_photo_ids = [], {}, []

    hero_bytes, hero_id = search_pexels_image(draft["image_prompt"], used_photo_ids=used_photo_ids, target_ratio=9 / 16)
    this_run_photo_ids.append(hero_id)
    hero_local = f"images/hero_{hero_id}.webp"
    os.makedirs("images", exist_ok=True)
    with open(hero_local, "wb") as f:
        f.write(compress_image(hero_bytes))
    hero_url = upload_to_r2(hero_local)

    for img in draft["section_images"]:
        try:
            photo_bytes, photo_id = search_pexels_image(img["query"], used_photo_ids=used_photo_ids)
            this_run_photo_ids.append(photo_id)
            local_path = f"images/{img['token']}_{photo_id}.webp"
            with open(local_path, "wb") as f:
                f.write(compress_image(photo_bytes))
            image_urls[img["token"]] = upload_to_r2(local_path)
            local_paths.append(local_path)
        except Exception as e:  # noqa: BLE001
            print(f"Section image fetch failed for {img.get('query')} (non-fatal): {e}")

    # --- 1. Blogger ---
    print("Publishing to Blogger...")
    access_token = get_access_token()
    full_html = build_post_html(draft, image_urls)
    result = publish_post(access_token, draft["title"], full_html, draft.get("hashtag_tags", []),
                           search_description=draft.get("key_benefit"))
    post_url = result.get("url")
    print("Published:", post_url)

    # --- 2. Video (real Pexels footage only, no Ken Burns fallback) + Pinterest ---
    pin_hashtags = build_pin_hashtags(draft.get("hashtag_tags", []), max_tags=5)
    pinterest_ok = False
    this_run_video_ids = []
    try:
        used_video_ids = set()
        for h in history[-100:]:
            used_video_ids.update(h.get("video_ids", []))

        hero_clip = get_video_clip_with_fallback(draft["image_prompt"], used_video_ids)
        if not hero_clip:
            raise RuntimeError("No Pexels video clip available even after all fallback queries.")
        this_run_video_ids.append(hero_clip["id"])
        reel_image_specs = [{"bytes": hero_clip["bytes"], "duration": 3.0, "kind": "video"}]

        for img in draft["section_images"]:
            clip = get_video_clip_with_fallback(f'{img["query"]} smoothie', used_video_ids)
            if clip:
                this_run_video_ids.append(clip["id"])
                reel_image_specs.append({"bytes": clip["bytes"], "duration": 3.0, "kind": "video"})

        print("Building video for Pinterest...")
        work_dir = "work_video"
        os.makedirs(work_dir, exist_ok=True)
        audio_path = os.path.join(work_dir, "voice.mp3")
        synthesize_voiceover(draft["reel_script"], audio_path)
        voice_duration = get_audio_duration_seconds(audio_path)

        n_caption_slides = max(1, len(reel_image_specs))
        captions = split_script_into_captions(draft["reel_script"], n_caption_slides)
        per_slide_duration = max(2.0, voice_duration / n_caption_slides)
        for i, spec in enumerate(reel_image_specs):
            spec["duration"] = per_slide_duration
            spec["caption"] = captions[i] if i < len(captions) else None
        # closing CTA slide: reuse the hero clip's footage with a bold CTA caption
        reel_image_specs.append({
            "bytes": hero_clip["bytes"], "duration": 2.5, "caption": draft["cta_text"],
            "is_cta": True, "kind": "video",
        })

        video_bytes = build_reel_video(reel_image_specs, audio_path, work_dir, width=1080, height=1350)  # 2:3

        if article_type == "recipe":
            tagline = "Tasty \u2022 Healthy \u2022 Simple!"
            bullets = [draft.get("calories", ""), f"Ready in {draft.get('prep_time', '')}", "Simple, real ingredients"]
            banner_text = "Get the Full Recipe"
        else:
            tagline = "Real Talk, Real Results"
            bullets = draft.get("pros", [])[:3] or ["An honest, no-hype look"]
            banner_text = "Read the Full Review"
        bullets = [b for b in bullets if b]

        infographic_bytes = build_infographic_cover_png(
            hero_bytes, draft["pin_hook"], tagline, bullets, banner_text, article_type,
        )
        infographic_local = f"images/cover_{hero_id}.png"
        with open(infographic_local, "wb") as f:
            f.write(infographic_bytes)
        infographic_cover_url = upload_to_r2(infographic_local)
        local_paths.append(infographic_local)

        pinterest_token = get_pinterest_access_token()
        pin_result = create_pinterest_video_pin(
            pinterest_token, board_id=PINTEREST_BOARD_ID, title=draft["title"],
            description=f'{draft["key_benefit"]} {AFFILIATE_DISCLOSURE} {pin_hashtags}',
            link=post_url, video_bytes=video_bytes, cover_image_url=infographic_cover_url,
        )
        print("Pinned:", pin_result.get("id"))
        pinterest_ok = True
    except Exception as e:  # noqa: BLE001
        print(f"Pinterest post failed (blog post is still published fine): {e}")

    # --- 3. Tumblr (every run) ---
    print("Posting to Tumblr...")
    social_hashtags = build_pin_hashtags(draft.get("hashtag_tags", []), max_tags=10)
    tumblr_ok = post_to_tumblr(
        title=draft["title"], key_benefit=draft["key_benefit"],
        image_url=hero_url, link=post_url, hashtags=social_hashtags,
    )

    # --- History + cleanup ---
    history.append({
        "title": draft["title"], "article_type": article_type,
        "date": datetime.now(timezone.utc).isoformat(),
        "url": post_url, "photo_ids": this_run_photo_ids, "video_ids": this_run_video_ids,
    })
    save_history(history)
    git_commit_and_push([HISTORY_FILE], f"Auto post history: {draft['title']}")

    for path in local_paths:
        delete_from_r2(path)  # hero_url intentionally kept -- Blogger embeds it permanently

    def tick(ok):
        return "OK" if ok else "FAILED"

    send_phone_notification(
        f"Smoothie Diet Daily posted: {draft['title'][:60]}",
        f"{draft['title']}\n{post_url}\n\nType: {article_type}\nBlogger: OK\nPinterest: {tick(pinterest_ok)}\nTumblr: {tick(tumblr_ok)}",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[FATAL] {e}")
        send_phone_notification("Smoothie Diet Daily run FAILED", f"Error: {e}")
        raise
