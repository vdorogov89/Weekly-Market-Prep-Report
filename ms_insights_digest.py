"""
Morgan Stanley "Thoughts on the Market" podcast digest -> Telegram bot.

This is a SEPARATE script/workflow from the others in this repo — sends
its own message(s), on its own schedule, to the same Telegram bot.

What this does:
1. Fetches https://www.morganstanley.com/insights/podcasts/thoughts-on-the-market
   (the show's own page, which lists many recent episodes at once, each
   with its date, title, teaser, and full transcript inline).
2. Compares the episodes found against a list of previously-seen episode
   URLs (state file, committed back to the repo after each run) to find
   what's NEW since the last run.
3. For each new episode: sends the full transcript to Claude (Anthropic's
   API) with a single request that both summarizes AND translates it to
   Russian in one step. This costs a small amount per call (see README)
   — a local, free, non-AI extractive summary is used as an automatic
   fallback if ANTHROPIC_API_KEY isn't set or the API call fails for any
   reason, so the script still works (at lower summary quality) without
   it.
4. On the very FIRST run (no state file yet), nothing is sent — the
   current set of episodes is just recorded as the baseline, so the very
   next run only reports what's genuinely new after that point.

Requirements (installed automatically by the GitHub Actions workflow):
    pip install requests beautifulsoup4

Environment variables required:
    TELEGRAM_BOT_TOKEN   - same one already used by the other scripts
    TELEGRAM_CHAT_ID     - same one already used by the other scripts

Environment variable optional (enables real AI summaries):
    ANTHROPIC_API_KEY    - from console.anthropic.com (paid, pay-per-use;
                            see README for cost estimate). Without it,
                            summaries fall back to the free local
                            extractive-summary + MyMemory-translation
                            pipeline (lower quality, but free).

NOTE ON RELIABILITY (important — read if this stops working):
- This page was reachable via a normal HTTP GET with a browser-like
  User-Agent at the time this was written, and did NOT require
  JavaScript to render episode links or transcripts. However, large
  corporate sites sometimes have bot-detection (Akamai, PerimeterX,
  etc.) that blocks automated traffic from cloud/CI IP ranges
  specifically — if that happens here, fetch_page() will raise and the
  workflow log will show the HTTP status/response body to confirm it.
- The HTML-parsing logic in find_podcast_episodes() was validated
  against a real fetch of the general /insights page (which uses the
  same episode-card structure) and correctly separated episodes and
  transcripts there. It has NOT been separately re-verified against
  this specific podcast-hub page's raw HTML, since that wasn't
  reachable from this environment either — so still treat the first
  live run here as a calibration run. If it finds 0 episodes, check the
  log's diagnostic page length, and if it finds episodes but empty
  transcripts, that's the next thing to check.
- Translation uses the free MyMemory API (no key), which has a modest
  daily character quota for anonymous use — comfortably enough for a
  handful of short summaries per day, which is all this needs.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-5"

PAGE_URL = "https://www.morganstanley.com/insights/podcasts/thoughts-on-the-market"
STATE_FILE = "seen_podcasts.json"
MAX_STATE_ENTRIES = 200  # keep the state file from growing forever

TRANSLATE_URL = "https://api.mymemory.translated.net/get"
TRANSLATE_CHUNK_CHARS = 450  # stay safely under MyMemory's ~500 char/request limit

SUMMARY_SENTENCE_COUNT = 6

STOPWORDS = set("""
a an the this that these those is are was were be been being have has had
do does did will would shall should may might must can could of in on at
to for with as by from into onto over under about above below between
and or but if then than so not no nor it its it's their his her they he
she we you your yours our ours i my me him them us also which who whom
whose what when where why how there here all any both each few more most
other some such only own same too very just s t can will don should now
""".split())


# ---------------------------------------------------------------------
# Fetching the page
# ---------------------------------------------------------------------

def fetch_page() -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(PAGE_URL, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Unexpected status {resp.status_code} fetching podcast page. "
            f"Response snippet: {resp.text[:300]}"
        )
    return resp.text


# ---------------------------------------------------------------------
# Parsing: podcast episodes + transcripts
# ---------------------------------------------------------------------

PODCAST_EPISODE_HREF_RE = re.compile(
    r"/insights/podcasts/thoughts-on-the-market/[a-z0-9\-]+/?$", re.IGNORECASE
)


def find_podcast_episodes(soup: BeautifulSoup) -> list:
    """
    Returns a list of {"url", "title", "transcript"} for each podcast
    episode found. Walks the page in document order so a "Transcript"
    heading and the paragraphs that follow it are correctly associated
    with the most recently seen episode title/link.
    """
    episodes = []
    current = None
    collecting_transcript = False

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "a"]):
        if tag.name == "a" and tag.get("href") and PODCAST_EPISODE_HREF_RE.search(tag["href"]):
            title_text = tag.get_text(strip=True)
            if not title_text:
                continue
            href = tag["href"]
            full_url = href if href.startswith("http") else f"https://www.morganstanley.com{href}"
            # A new episode link (title) starts a new episode context,
            # unless it's a repeat of the one we're already on (e.g. an
            # "Explore Episode" link at the end using the same title).
            # Either way, hitting any episode link means we've exited
            # whatever transcript paragraph block came before it.
            collecting_transcript = False
            if current is None or current["url"] != full_url:
                current = {"url": full_url, "title": title_text, "transcript_parts": []}
                episodes.append(current)
            continue

        if tag.name == "h1" or tag.name == "h2" or tag.name == "h3" or tag.name == "h4":
            heading_text = tag.get_text(strip=True).lower()
            if heading_text == "transcript":
                collecting_transcript = (current is not None)
            else:
                collecting_transcript = False
            continue

        if tag.name == "a":
            # Any other link (e.g. share icons, unrelated navigation)
            # also signals we're no longer inside a transcript block.
            collecting_transcript = False
            continue

        if tag.name == "p" and collecting_transcript and current is not None:
            text = tag.get_text(strip=True)
            if text:
                current["transcript_parts"].append(text)

    results = []
    for ep in episodes:
        transcript = " ".join(ep["transcript_parts"]).strip()
        results.append({
            "url": ep["url"],
            "title": ep["title"],
            "transcript": transcript,
        })
    return results


# ---------------------------------------------------------------------
# Local extractive summarization (no AI, no external API — fallback only)
# ---------------------------------------------------------------------

def split_sentences(text: str) -> list:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [p.strip() for p in parts if p.strip()]


def summarize(text: str, sentence_count: int = SUMMARY_SENTENCE_COUNT) -> str:
    sentences = split_sentences(text)
    if len(sentences) <= sentence_count:
        return " ".join(sentences)

    word_freq = {}
    for sentence in sentences:
        for word in re.findall(r"[a-zA-Z']+", sentence.lower()):
            if word in STOPWORDS or len(word) < 3:
                continue
            word_freq[word] = word_freq.get(word, 0) + 1

    if not word_freq:
        return " ".join(sentences[:sentence_count])

    max_freq = max(word_freq.values())
    for word in word_freq:
        word_freq[word] /= max_freq

    scored = []
    for i, sentence in enumerate(sentences):
        words = re.findall(r"[a-zA-Z']+", sentence.lower())
        if not words:
            continue
        score = sum(word_freq.get(w, 0) for w in words) / len(words)
        # Small boost for earlier sentences (lede bias), tapering off.
        position_boost = max(0, 1 - i / max(1, len(sentences))) * 0.15
        # Sentences with numbers/percentages tend to carry the concrete,
        # citable facts in financial commentary (figures, forecasts) —
        # give them a modest boost over purely descriptive sentences.
        number_boost = 0.2 if re.search(r"\d", sentence) else 0
        scored.append((score + position_boost + number_boost, i, sentence))

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:sentence_count]
    top_in_order = [s for _, i, s in sorted(top, key=lambda x: x[1])]
    return " ".join(top_in_order)


# ---------------------------------------------------------------------
# AI summary via Claude API (optional — used when ANTHROPIC_API_KEY is set)
# ---------------------------------------------------------------------

def summarize_with_claude(title: str, transcript: str) -> str:
    """
    Sends the full transcript to Claude and asks for a structured,
    ready-to-read Russian-language summary in one step (summary +
    translation combined, rather than summarizing in English and
    translating separately). Raises on any failure so the caller can
    fall back to the free local pipeline.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    prompt = (
        "Ниже — полная текстовая расшифровка эпизода финансового подкаста "
        "Morgan Stanley \"Thoughts on the Market\". Сделай краткое содержание "
        "на русском языке для личного дайджеста инвестора: по возможности "
        "укажи имя и должность спикера (или спикеров), раздели по 2-4 "
        "ключевым темам (с конкретными цифрами, датами и фактами из "
        "транскрипта), и в конце добавь короткий вывод для инвестора. Пиши "
        "по существу, без вступлений вроде \"вот краткое содержание\" — "
        "сразу переходи к сути. Не более 200-250 слов.\n\n"
        f"Название эпизода: {title}\n\n"
        f"Транскрипт:\n{transcript}"
    )

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(CLAUDE_API_URL, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    result = "\n".join(text_blocks).strip()
    if not result:
        raise RuntimeError(f"Claude API returned no text content: {str(data)[:300]}")
    return result


def summarize_podcast_to_russian(title: str, transcript: str) -> str:
    """
    Tries the Claude API first (better quality); falls back to the free
    local extractive-summary + MyMemory translation pipeline if the key
    isn't set or the API call fails for any reason.
    """
    try:
        return summarize_with_claude(title, transcript)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: Claude summary failed, falling back to local summary: {exc}", file=sys.stderr)
        summary_en = summarize(transcript)
        return translate_to_russian(summary_en)


# ---------------------------------------------------------------------
# Translation (free, no key — MyMemory API; used only as a fallback)
# ---------------------------------------------------------------------

def _translate_chunk(text: str) -> str:
    params = {"q": text, "langpair": "en|ru"}
    resp = requests.get(TRANSLATE_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    translated = data.get("responseData", {}).get("translatedText")
    if not translated:
        raise RuntimeError(f"Unexpected translation response: {str(data)[:200]}")
    return translated


def translate_to_russian(text: str) -> str:
    if not text.strip():
        return ""
    sentences = split_sentences(text)
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) + 1 > TRANSLATE_CHUNK_CHARS:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence
        else:
            current_chunk = f"{current_chunk} {sentence}".strip()
    if current_chunk:
        chunks.append(current_chunk.strip())

    translated_chunks = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(1)
        try:
            translated_chunks.append(_translate_chunk(chunk))
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: translation chunk failed, using original text: {exc}", file=sys.stderr)
            translated_chunks.append(chunk)
    return " ".join(translated_chunks)


# ---------------------------------------------------------------------
# State (seen episode URLs) persistence
# ---------------------------------------------------------------------

def load_seen_urls() -> set:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen", []))
    except FileNotFoundError:
        return None  # signals "first run"
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not read state file, treating as first run: {exc}", file=sys.stderr)
        return None


def save_seen_urls(urls: set) -> None:
    trimmed = list(urls)[-MAX_STATE_ENTRIES:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": trimmed, "updated": datetime.now(timezone.utc).isoformat()}, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set. "
            "Set them as GitHub repo secrets (see README)."
        )
    # Telegram rejects any message over 4096 characters outright — an
    # AI summary occasionally runs long enough to cross that with the
    # title/URL added. Truncate defensively rather than let the whole
    # send fail (which, upstream, must never silently mark the episode
    # as "seen" — see main()).
    max_len = 4096
    if len(text) > max_len:
        text = text[: max_len - 20].rstrip() + "\n…(обрезано)"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    resp = requests.post(url, data=payload, timeout=20)
    resp.raise_for_status()


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    html = fetch_page()
    soup = BeautifulSoup(html, "html.parser")

    episodes = find_podcast_episodes(soup)
    print(f"Diagnostic: page length {len(html)} chars; found {len(episodes)} podcast episodes.")

    all_urls = {e["url"] for e in episodes}
    seen = load_seen_urls()

    if seen is None:
        print("No state file found — this is the first run. Recording current episodes as baseline, sending nothing.")
        save_seen_urls(all_urls)
        return

    new_episodes = [e for e in episodes if e["url"] not in seen]

    if not new_episodes:
        print("No new episodes since last run.")
        save_seen_urls(seen | all_urls)
        return

    successfully_sent = set()
    for episode in new_episodes:
        if episode["transcript"]:
            summary_ru = summarize_podcast_to_russian(episode["title"], episode["transcript"])
        else:
            summary_ru = "(транскрипт не найден на странице — см. эпизод по ссылке)"
        message = (
            f"\U0001F3A7 Новый подкаст — Thoughts on the Market\n\n"
            f"*{episode['title']}*\n\n"
            f"{summary_ru}\n\n{episode['url']}"
        )
        try:
            send_telegram_message(message)
            successfully_sent.add(episode["url"])
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: failed to send Telegram message for episode {episode['url']}: {exc}", file=sys.stderr)
            print(f"  -> {episode['url']} will NOT be marked as seen, so it will be retried on the next run.", file=sys.stderr)

    # Only mark episodes as "seen" if we actually managed to send them —
    # anything that failed to send stays out of the seen set so it's
    # retried on the next run instead of silently disappearing forever.
    failed_urls = {e["url"] for e in new_episodes} - successfully_sent
    save_seen_urls((seen | all_urls) - failed_urls)
    print(f"Sent {len(successfully_sent)}/{len(new_episodes)} new podcast episode(s).")
    if failed_urls:
        print(f"{len(failed_urls)} episode(s) failed to send and will be retried next run: {failed_urls}", file=sys.stderr)


if __name__ == "__main__":
    main()
