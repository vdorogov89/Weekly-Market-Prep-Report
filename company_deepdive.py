"""
Daily company deep-dive report -> Telegram bot.

This is a SEPARATE script/workflow from monthly_top_gainers.py, but it
depends on that script's output: monthly_top_gainers.py saves the
month's top-10 list to top10_companies.json. Starting the day after
that report, this script sends ONE deep-dive report per day, covering
the companies in rank order — day 1 after the report -> rank #1, day 2
-> rank #2, ... day 10 -> rank #10. After that, it sends nothing until
next month's top-10 report resets the cycle.

What this sends, per company: what the business actually does, why its
stock rose this much over the period, key financial metrics, and an
investment outlook — in Russian.

This uses Claude with the web_search tool (not just the model's own
training-time knowledge) specifically because "why did this grow" and
"what does the latest outlook look like" are inherently about CURRENT,
often very recent events — a model's static knowledge would likely be
outdated or simply wrong on ticker-specific recent news.

Requirements (installed automatically by the GitHub Actions workflow):
    pip install requests

Environment variables required:
    TELEGRAM_BOT_TOKEN    - same one already used by the other scripts
    TELEGRAM_CHAT_ID      - same one already used by the other scripts
    ANTHROPIC_API_KEY     - same one already used by ms_insights_digest.py
                            (paid, pay-per-use; this script's calls use
                            web search too, so each one costs more than
                            a plain summarization call — see README for
                            a cost estimate)

NOTE: unlike most scripts here, this one does nothing most days of the
month (only the 10 days following the monthly top-10 report) — "no
company due today" is the expected, normal outcome outside that window,
not an error.
"""

import json
import os
import sys
from datetime import date, datetime, timezone

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-5"

TOP10_FILE = "top10_companies.json"
STATE_FILE = "deepdive_sent_state.json"

NUM_COMPANIES = 10  # matches TOP_N in monthly_top_gainers.py


# ---------------------------------------------------------------------
# Loading the month's top-10 list + state
# ---------------------------------------------------------------------

def load_top10() -> dict:
    with open(TOP10_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"report_date": None, "sent_ranks": []}
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not read state file, starting fresh: {exc}", file=sys.stderr)
        return {"report_date": None, "sent_ranks": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------
# Claude API with web search
# ---------------------------------------------------------------------

def generate_deepdive(company: dict, window_start: str, window_end: str, today_str: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    prompt = (
        f"Сегодняшняя дата: {today_str}. Используй актуальный веб-поиск, "
        "чтобы ответ был основан на свежих данных, а не только на твоих "
        "внутренних знаниях.\n\n"
        f"Компания {company['name']} (тикер {company['symbol']}) заняла "
        f"{company['rank']}-е место в топ-10 акций S&P 500 по росту цены "
        f"за период {window_start} — {window_end}, с ростом "
        f"{company['pct_change']:+.1f}%.\n\n"
        "Сделай структурированный разбор на русском языке для личного "
        "инвестиционного дайджеста, по разделам:\n"
        "1. Суть бизнеса — чем компания занимается, в двух-трёх предложениях\n"
        "2. Почему именно такой рост за этот период — конкретные события, "
        "новости, отчётности, катализаторы (используй поиск, чтобы найти "
        "реальные причины, а не догадки)\n"
        "3. Основные метрики — выручка, маржинальность, P/E или другая "
        "релевантная оценка, рыночная капитализация (последние доступные "
        "данные)\n"
        "4. Инвестиционный прогноз — что говорят аналитики, основные риски "
        "и на что обратить внимание дальше\n\n"
        "Пиши по существу, без вступлений вроде \"вот разбор\" — сразу "
        "переходи к сути. Не более 300-350 слов."
    )

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    resp = requests.post(CLAUDE_API_URL, headers=headers, json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    result = "\n".join(text_blocks).strip()
    if not result:
        raise RuntimeError(f"Claude API returned no text content: {str(data)[:300]}")
    return result


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set. "
            "Set them as GitHub repo secrets (see README)."
        )
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
    try:
        top10 = load_top10()
    except FileNotFoundError:
        print(f"No {TOP10_FILE} found yet — monthly_top_gainers.py hasn't run successfully. Nothing to do.")
        return

    today = datetime.now(timezone.utc).date()

    # Manual test mode: set via the workflow_dispatch "test_rank" input.
    # Bypasses the date/offset logic entirely and does NOT touch
    # deepdive_sent_state.json, so it has no effect on the normal daily
    # cycle — safe to run any time, as many times as useful.
    test_rank_raw = os.environ.get("TEST_RANK", "").strip()
    is_test_mode = bool(test_rank_raw)

    if is_test_mode:
        try:
            rank = int(test_rank_raw)
        except ValueError:
            print(f"Warning: TEST_RANK={test_rank_raw!r} is not a valid integer.", file=sys.stderr)
            return
        print(f"Diagnostic: TEST MODE — forcing rank {rank}, ignoring date/offset and not touching state file.")
    else:
        report_date = date.fromisoformat(top10["report_date"])
        offset = (today - report_date).days
        print(f"Diagnostic: report_date={report_date}, today={today}, offset={offset} days.")

        if not (1 <= offset <= NUM_COMPANIES):
            print(f"Offset {offset} is outside the 1-{NUM_COMPANIES} day window — nothing due today.")
            return

        state = load_state()
        if state.get("report_date") != top10["report_date"]:
            # A new month's top-10 list has appeared since we last tracked
            # state — start tracking fresh for this cycle.
            state = {"report_date": top10["report_date"], "sent_ranks": []}

        rank = offset
        if rank in state["sent_ranks"]:
            print(f"Rank {rank} was already sent for this cycle — nothing to do.")
            return

    company = next((c for c in top10["companies"] if c["rank"] == rank), None)
    if company is None:
        print(f"Warning: no company found at rank {rank} in {TOP10_FILE}.", file=sys.stderr)
        return

    today_str = today.strftime("%d.%m.%Y")
    try:
        deepdive_text = generate_deepdive(company, top10["window_start"], top10["window_end"], today_str)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: failed to generate deep-dive for rank {rank} ({company['symbol']}): {exc}", file=sys.stderr)
        return  # not marked as sent, so it will be retried on the next run

    test_prefix = "\U0001F9EA ТЕСТОВЫЙ ЗАПУСК\n\n" if is_test_mode else ""
    message = (
        f"{test_prefix}\U0001F4C4 Разбор компании #{rank} из топ-10 (рост {company['pct_change']:+.1f}%)\n\n"
        f"*{company['name']} ({company['symbol']})*\n\n"
        f"{deepdive_text}"
    )

    try:
        send_telegram_message(message)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: failed to send Telegram message for rank {rank}: {exc}", file=sys.stderr)
        return  # not marked as sent (in non-test mode), so it will be retried on the next run

    if is_test_mode:
        print(f"Sent TEST deep-dive for rank {rank} ({company['symbol']}). State file not touched.")
    else:
        state["sent_ranks"].append(rank)
        save_state(state)
        print(f"Sent deep-dive for rank {rank} ({company['symbol']}).")


if __name__ == "__main__":
    main()
