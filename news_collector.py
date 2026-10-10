
#!/usr/bin/env python3
"""Collect, rank and send recent RSS news as Telegram drafts."""

import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "news_sources.json"
FILTER_PATH = ROOT / "news_filters.json"
SEEN_PATH = ROOT / "data" / "seen.json"

USER_AGENT = "MarinadNewsBot/1.4 (+RSS reader)"
MAX_DESCRIPTION_CHARS = 650
MAX_SEEN_IDS = 5000


def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def child_text(element, names):
    for child in list(element):
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names and child.text:
            return child.text.strip()
    return ""


def parse_date(raw):
    if not raw:
        return None

    try:
        dt = parsedate_to_datetime(raw.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            dt = datetime.fromisoformat(
                raw.strip().replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None


def fetch_feed(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml, text/xml"
            ),
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(4_000_000)


def parse_feed(xml_bytes, source_name):
    root = ET.fromstring(xml_bytes)
    results = []

    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue

        title = clean_text(child_text(node, ("title",)))
        link = child_text(node, ("link",))

        if not link:
            for child in list(node):
                child_tag = child.tag.rsplit("}", 1)[-1].lower()
                if child_tag == "link":
                    rel = child.attrib.get("rel", "alternate")
                    candidate = child.attrib.get("href", "")
                    if candidate and rel in ("alternate", ""):
                        link = candidate
                        break

        description = clean_text(
            child_text(
                node,
                ("description", "summary", "content", "encoded"),
            )
        )

        raw_date = child_text(
            node,
            ("pubdate", "published", "updated", "date", "issued"),
        )
        published = parse_date(raw_date)

        if title and link.startswith(("https://", "http://")):
            identifier = child_text(node, ("guid", "id")) or link
            results.append(
                {
                    "id": f"{source_name}:{identifier}",
                    "source": source_name,
                    "title": title,
                    "link": link,
                    "description": description[:MAX_DESCRIPTION_CHARS],
                    "published": published.isoformat() if published else "",
                    "_published_dt": published,
                }
            )

    return results


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_seen_state():
    raw = load_json(SEEN_PATH, {"seen": []})

    if isinstance(raw, list):
        ids = raw
    elif isinstance(raw, dict):
        ids = raw.get("seen", [])
    else:
        ids = []

    if not isinstance(ids, list):
        ids = []

    clean_ids = list(
        dict.fromkeys(
            value for value in ids
            if isinstance(value, str) and value
        )
    )
    return {"seen": clean_ids[-MAX_SEEN_IDS:]}


def save_seen_state(state):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SEEN_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(SEEN_PATH)


def normalize_title(title):
    title = title.lower().replace("ё", "е")
    title = re.sub(r"https?://\S+", " ", title)
    title = re.sub(r"[^a-zа-я0-9]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def normalize_keyword(keyword):
    keyword = str(keyword).lower().replace("ё", "е")
    return re.sub(r"\s+", " ", keyword).strip()


def keyword_matches(text, keyword):
    """Match a phrase without matching it inside a longer word."""
    keyword = normalize_keyword(keyword)
    if not keyword:
        return False

    pattern = (
        r"(?<![a-zа-я0-9])"
        + re.escape(keyword)
        + r"(?![a-zа-я0-9])"
    )
    return re.search(pattern, text) is not None


def matched_keywords(text, keywords):
    matches = []
    for keyword in keywords:
        if keyword_matches(text, keyword):
            matches.append(str(keyword))
    return matches


def score_news(item, rules):
    title = normalize_keyword(item.get("title", ""))
    description = normalize_keyword(item.get("description", ""))

    high_keywords = rules.get("high_priority_keywords", [])
    medium_keywords = rules.get("medium_priority_keywords", [])
    low_keywords = rules.get("low_priority_keywords", [])

    high_title = matched_keywords(title, high_keywords)
    high_body = matched_keywords(description, high_keywords)
    medium_title = matched_keywords(title, medium_keywords)
    medium_body = matched_keywords(description, medium_keywords)
    low_title = matched_keywords(title, low_keywords)

    score = 20
    reasons = []

    # Event categories reward concrete signals in the headline.
    event_groups = {
        "происшествия": [
            "погибли",
            "погиб",
            "массовая стрельба",
            "теракт",
            "ракетный удар",
            "обрушение",
            "крупная авария",
            "массовая эвакуация",
            "режим чрезвычайной ситуации",
            "много пострадавших",
        ],
        "экономика": [
            "ключевая ставка",
            "обвал рынка",
            "рекордное падение",
            "резкое падение рубля",
            "индекс мосбиржи",
            "курс рубля",
            "центробанк",
            "инфляция",
        ],
        "политика": [
            "результаты выборов",
            "объявил об отставке",
            "экстренное заседание",
            "законопроект",
            "решение суда",
            "объявлена мобилизация",
        ],
        "технологии": [
            "массовый сбой",
            "утечка персональных данных",
            "кибератака",
            "искусственный интеллект",
            "кибербезопасность",
            "научное открытие",
        ],
    }

    for group, keywords in event_groups.items():
        title_matches = matched_keywords(title, keywords)
        body_matches = matched_keywords(description, keywords)

        if title_matches:
            score += 22
            reasons.append(
                f"событие: {group} — "
                + ", ".join(title_matches[:2])
            )
        elif body_matches:
            score += 6
            reasons.append(
                f"событие по теме «{group}» упомянуто в описании"
            )

    # High-priority terms are stronger when they appear in the headline.
    if high_title:
        score += min(25, 18 + 3 * (len(high_title) - 1))
        reasons.append(
            "важный сигнал в заголовке: "
            + ", ".join(high_title[:3])
        )
    elif high_body:
        score += 8
        reasons.append(
            "важный сигнал в описании: "
            + ", ".join(high_body[:2])
        )

    # A name or broad topic alone should not dominate the ranking.
    generic_terms = {
        "президент россии",
        "президент сша",
        "правительство",
        "переговоры",
        "госдума",
        "государственная дума",
        "санкции",
        "индекс мосбиржи",
        "мосбиржа",
        "курс рубля",
        "центробанк",
        "инфляция",
        "ключевая ставка",
        "атака",
        "нападение",
        "пожар",
        "взрыв",
        "кибератака",
        "искусственный интеллект",
        "кибербезопасность",
        "сборная россии",
        "финал турнира",
    }

    meaningful_medium = [
        keyword for keyword in medium_title
        if normalize_keyword(keyword) not in generic_terms
    ]

    if meaningful_medium:
        score += min(10, 6 + 2 * (len(meaningful_medium) - 1))
        reasons.append(
            "дополнительная тема в заголовке: "
            + ", ".join(meaningful_medium[:3])
        )
    elif medium_title:
        reasons.append("общее тематическое упоминание в заголовке")
    elif medium_body:
        score += 2
        reasons.append("тематическое упоминание в описании")

    # Freshness helps, but cannot outweigh the topic by itself.
    published = item.get("_published_dt")
    if published:
        age_hours = (
            datetime.now(timezone.utc) - published
        ).total_seconds() / 3600

        if 0 <= age_hours <= 2:
            score += 5
            reasons.append("опубликовано не более 2 часов назад")
        elif 2 < age_hours <= 6:
            score += 3
            reasons.append("опубликовано не более 6 часов назад")

    # Deprioritize lifestyle and entertainment headlines when configured.
    if low_title:
        penalty = min(25, 12 + 4 * (len(low_title) - 1))
        score -= penalty
        reasons.append(
            "понижен приоритет: " + ", ".join(low_title[:3])
        )

    score = max(0, min(100, score))
    return score, reasons or ["нет совпадений с ключевыми темами"]


def telegram_send(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode()

    request = urllib.request.Request(url, data=payload, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))

        if not result.get("ok"):
            raise RuntimeError("Telegram API rejected the message.")

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"Telegram HTTP {exc.code}: {detail}"
        ) from exc


def format_draft(item):
    description = item.get("description", "")
    if description:
        description = description.rstrip() + "\n\n"

    time_line = ""
    if item.get("published"):
        try:
            dt = datetime.fromisoformat(item["published"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            time_line = f"🕒 {dt.strftime('%d.%m.%Y %H:%M UTC')}\n"
        except (TypeError, ValueError):
            pass

    reasons = ", ".join(item.get("score_reasons", []))

    return (
        "⚡ ЧЕРНОВИК МАРИНАД\n\n"
        f"{item['title']}\n\n"
        f"Приоритет: {item.get('score', 20)}/100\n"
        f"Причина: {reasons}\n\n"
        f"{description}"
        f"Источник: {item['source']}\n"
        f"{time_line}"
        f"Оригинал: {item['link']}\n\n"
        "Проверь факты и формулировку перед публикацией."
    )


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("ADMIN_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("ERROR: Telegram secrets are missing.")
        return 2

    config = load_json(CONFIG_PATH, {})
    rules = load_json(FILTER_PATH, None)

    if not isinstance(config, dict):
        print("ERROR: news_sources.json must be a JSON object.")
        return 2

    if not isinstance(rules, dict):
        print("ERROR: news_filters.json is missing or invalid.")
        return 2

    sources = config.get("sources", [])
    if not isinstance(sources, list) or not sources:
        print("ERROR: no RSS sources configured.")
        return 2

    for key in (
        "high_priority_keywords",
        "medium_priority_keywords",
        "low_priority_keywords",
    ):
        if not isinstance(rules.get(key, []), list):
            print(f"ERROR: {key} must be a JSON array.")
            return 2

    try:
        max_age_hours = max(
            1, int(config.get("max_age_hours", 12))
        )
        max_items = max(
            1, min(20, int(config.get("max_items_per_run", 5)))
        )
        min_score = max(
            0, min(100, int(rules.get("min_score", 20)))
        )
        similarity_limit = float(
            rules.get("max_similar_title_ratio", 0.88)
        )
        similarity_limit = max(0.5, min(1.0, similarity_limit))
    except (TypeError, ValueError):
        print("ERROR: invalid numeric configuration.")
        return 2

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=max_age_hours
    )
    state = load_seen_state()
    seen = set(state["seen"])
    collected = []
    skipped_score = 0
    skipped_similar = 0

    for source in sources:
        if not isinstance(source, dict):
            print("WARN: skipping invalid source entry.")
            continue

        name = str(source.get("name", "Источник")).strip() or "Источник"
        url = str(source.get("url", "")).strip()

        if not url.startswith("https://"):
            print(f"SKIP {name}: only HTTPS RSS URLs are allowed.")
            continue

        try:
            entries = parse_feed(fetch_feed(url), name)
            print(f"OK {name}: fetched {len(entries)} entries")

            for item in entries:
                if item["id"] in seen:
                    continue

                published = item.pop("_published_dt", None)
                if published is None:
                    print(f"SKIP undated item: {item['title']}")
                    continue

                if published < cutoff:
                    continue

                item["_published_dt"] = published
                item["score"], item["score_reasons"] = score_news(
                    item, rules
                )

                if item["score"] < min_score:
                    skipped_score += 1
                    print(
                        f"SKIP low score ({item['score']}): "
                        f"{item['title']}"
                    )
                    continue

                item["_sort"] = published
                collected.append(item)

        except Exception as exc:
            print(f"WARN {name}: {type(exc).__name__}: {exc}")

    collected.sort(
        key=lambda item: (item["score"], item["_sort"]),
        reverse=True,
    )

    unique_items = []
    normalized_titles = []

    for item in collected:
        normalized = normalize_title(item["title"])
        if not normalized:
            continue

        duplicate = any(
            difflib.SequenceMatcher(
                None, normalized, previous
            ).ratio() >= similarity_limit
            for previous in normalized_titles
        )

        if duplicate:
            skipped_similar += 1
            print(f"SKIP similar title: {item['title']}")
            continue

        normalized_titles.append(normalized)
        unique_items.append(item)

    selected = unique_items[:max_items]

    print(
        f"FILTER SUMMARY: accepted={len(unique_items)}, "
        f"low_score={skipped_score}, similar={skipped_similar}"
    )

    if not selected:
        print("No new items passed the filters.")
        return 0

    sent_count = 0

    for item in selected:
        item.pop("_sort", None)
        item.pop("_published_dt", None)

        try:
            telegram_send(token, chat_id, format_draft(item))
        except Exception as exc:
            print(
                f"ERROR sending item {item.get('id')}: "
                f"{type(exc).__name__}: {exc}"
            )
            print(f"Stopped after sending {sent_count} draft(s).")
            return 1

        state["seen"].append(item["id"])
        state["seen"] = list(
            dict.fromkeys(state["seen"])
        )[-MAX_SEEN_IDS:]

        try:
            save_seen_state(state)
        except OSError as exc:
            print(f"CRITICAL: could not save seen.json: {exc}")
            return 1

        sent_count += 1
        print(
            f"SENT score={item['score']} "
            f"{item['source']} — {item['title']}"
        )
        time.sleep(0.4)

    print(f"Finished. Sent {sent_count} draft(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
