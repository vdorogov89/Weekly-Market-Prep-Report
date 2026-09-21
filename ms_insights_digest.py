"""
Morgan Stanley Insights digest -> Telegram bot.

This is a SEPARATE script/workflow from the others in this repo — sends
its own message(s), on its own schedule, to the same Telegram bot.

What this does:
1. Fetches https://www.morganstanley.com/insights?filter=market-trends
2. Finds article links and "Thoughts on the Market" podcast episode links
   on that page.
3. Compares them against a list of previously-seen URLs (state file,
   committed back to the repo after each run) to find what's NEW since
   the last run.
4. For each new ARTICLE: sends the title + a Russian translation of the
   one-sentence teaser that's shown on the listing page (that's all the
   text available without visiting the article itself, and article pages
   weren't verified to have a consistent scrapable structure).
5. For each new PODCAST EPISODE: the listing page happens to include the
   full episode transcript inline (visible even without JavaScript), so
   this extracts it, produces a genuine extractive summary locally (no
   AI, no paid API — a simple word-frequency sentence-scoring algorithm),
   and translates that summary to Russian.
6. On the very FIRST run (no state file yet), nothing is sent — the
   current set of articles/episodes is just recorded as the baseline, so
   the very next run only reports what's genuinely new after that point.

Requirements (installed automatically by the GitHub Actions workflow):
    pip install requests beautifulsoup4

Environment variables required:
    TELEGRAM_BOT_TOKEN   - same one already used by the other scripts
    TELEGRAM_CHAT_ID     - same one already used by the other scripts

NOTE ON RELIABILITY (important — read if this stops working):
- This page was reachable via a normal HTTP GET with a browser-like
  User-Agent at the time this was written, and did NOT require
  JavaScript to render article/podcast links or podcast transcripts.
  However, large corporate sites sometimes have bot-detection (Akamai,
  PerimeterX, etc.) that blocks automated traffic from cloud/CI IP
  ranges specifically — if that happens here, fetch_page() will raise
  and the workflow log will show the HTTP status/response body to
  confirm it.
- The HTML-parsing logic in this file was written without being able to
  inspect Morgan Stanley's live raw HTML from this environment (network
  restrictions here), so it was built to be structure-tolerant (matching
  by URL patterns and heading/paragraph relationships rather than exact
  CSS classes) — but the FIRST live run should be treated as a
  calibration run. If it finds 0 articles or 0 podcasts, check the log's
  diagnostic counts and the raw page length; the parsing functions below
  (find_articles / find_podcast_episodes) are where to adjust.
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

PAGE_URL = "https://www.morganstanley.com/insights?filter=market-trends"
STATE_FILE = "seen_insights.json"
MAX_STATE_ENTRIES = 500  # keep the state file from growing forever

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
            f"Unexpected status {resp.status_code} fetching insights page. "
            f"Response snippet: {resp.text[:300]}"
        )
    return resp.text


# ---------------------------------------------------------------------
# Parsing: articles
# ---------------------------------------------------------------------

ARTICLE_HREF_RE = re.compile(r"/insights/articles/[a-z0-9\-]+/?$", re.IGNORECASE)


def find_articles(soup: BeautifulSoup) -> list:
    """
    Returns a list of {"url", "title", "teaser"} for each distinct
    article link found on the page. "teaser" is the best-effort
    one-sentence description shown near the title, or "" if none found.
    """
    by_href = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not ARTICLE_HREF_RE.search(href):
            continue
        full_url = href if href.startswith("http") else f"https://www.morganstanley.com{href}"
        text = a.get_text(strip=True)
        entry = by_href.setdefault(full_url, {"url": full_url, "title": "", "teaser": ""})
        # Several <a> tags can point at the same article (an image link
        # with no text, and a heading link with the real title) — keep
        # the longest text seen as the title.
        if len(text) > len(entry["title"]):
            entry["title"] = text

    # Best-effort teaser: for each article, look for a <p> immediately
    # following the heading that contains its title link.
    for entry in by_href.values():
        if not entry["title"]:
            continue
        heading = None
        for tag_name in ("h1", "h2", "h3", "h4"):
            heading = soup.find(tag_name, string=lambda s: s and entry["title"] in s)
            if heading is None:
                # title text might be split across inline tags; search by link instead
                link = soup.find("a", href=re.compile(re.escape(entry["url"].replace("https://www.morganstanley.com", ""))))
                if link:
                    heading = link.find_parent(["h1", "h2", "h3", "h4"])
            if heading:
                break
        if heading:
            sibling_p = heading.find_next_sibling("p")
            if sibling_p:
                entry["teaser"] = sibling_p.get_text(strip=True)

    return [e for e in by_href.values() if e["title"]]


# ---------------------------------------------------------------------
# Parsing: podcast episodes + transcripts
# ---------------------------------------------------------------------

PODCAST_EPISODE_HREF_RE = re.compile(
    r"/insights/podcasts/thoughts-on-the-market/[a-z0-9\-]+/?$", re.IGNORECASE
)


def find_podcast_episodes(soup: BeautifulSoup) -> list:
    """
    Returns a list of {"url", "title", "date", "transcript"} for each
    podcast episode found. Walks the page in document order so a
    "Transcript" heading and the paragraphs that follow it are correctly
    associated with the most recently seen episode title/link.
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
# Local extractive summarization (no AI, no external API)
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
# Translation (free, no key — MyMemory API)
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
# State (seen URLs) persistence
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

    articles = find_articles(soup)
    podcasts = find_podcast_episodes(soup)

    print(f"Diagnostic: page length {len(html)} chars; found {len(articles)} articles, {len(podcasts)} podcast episodes.")

    all_urls = {a["url"] for a in articles} | {p["url"] for p in podcasts}
    seen = load_seen_urls()

    if seen is None:
        print("No state file found — this is the first run. Recording current items as baseline, sending nothing.")
        save_seen_urls(all_urls)
        return

    new_articles = [a for a in articles if a["url"] not in seen]
    new_podcasts = [p for p in podcasts if p["url"] not in seen]

    if not new_articles and not new_podcasts:
        print("No new items since last run.")
        save_seen_urls(seen | all_urls)
        return

    for article in new_articles:
        teaser_ru = translate_to_russian(article["teaser"]) if article["teaser"] else ""
        message = f"\U0001F4F0 Новая статья — Morgan Stanley Insights\n\n*{article['title']}*\n"
        if teaser_ru:
            message += f"\n{teaser_ru}\n"
        message += f"\n{article['url']}"
        try:
            send_telegram_message(message)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: failed to send Telegram message for article {article['url']}: {exc}", file=sys.stderr)

    for episode in new_podcasts:
        if episode["transcript"]:
            summary_en = summarize(episode["transcript"])
            summary_ru = translate_to_russian(summary_en)
        else:
            summary_ru = "(транскрипт не найден на странице — см. эпизод по ссылке)"
        message = (
            f"\U0001F3A7 Новый подкаст — Thoughts on the Market\n\n"
            f"*{episode['title']}*\n\n"
            f"{summary_ru}\n\n{episode['url']}"
        )
        try:
            send_telegram_message(message)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: failed to send Telegram message for episode {episode['url']}: {exc}", file=sys.stderr)

    save_seen_urls(seen | all_urls)
    print(f"Sent {len(new_articles)} new article(s) and {len(new_podcasts)} new podcast episode(s).")


if __name__ == "__main__":
    main()
