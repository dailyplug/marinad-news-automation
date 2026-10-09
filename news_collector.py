#!/usr/bin/env python3
"""Collect recent RSS items and send source-linked drafts to a private Telegram chat."""
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
SEEN_PATH = ROOT / "data" / "seen.json"
USER_AGENT = "MarinadNewsBot/1.0 (+RSS reader)"
MAX_DESCRIPTION_CHARS = 650


def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def child_text(element, names):
    for child in list(element):
        if child.tag.rsplit("}", 1)[-1].lower() in names and child.text:
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
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None


def fetch_feed(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(4_000_000)


def parse_feed(xml_bytes, source_name):
    root = ET.fromstring(xml_bytes)
    results = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        title = clean_text(child_text(node, ("title",)))
        link = child_text(node, ("link",))
        if not link:
            for child in list(node):
                if child.tag.rsplit("}", 1)[-1].lower() == "link":
                    link = child.attrib.get("href", "")
                    if link:
                        break
        description = clean_text(child_text(node, ("description", "summary", "content", "encoded")))
        raw_date = child_text(node, ("pubdate", "published", "updated", "date", "issued"))
        published = parse_date(raw_date)
        if title and link.startswith(("https://", "http://")):
            identifier = child_text(node, ("guid", "id")) or link
            results.append({
                "id": f"{source_name}:{identifier}",
                "source": source_name,
                "title": title,
                "link": link,
                "description": description[:MAX_DESCRIPTION_CHARS],
                "published": published.isoformat() if published else "",
                "_published_dt": published,
            })
    return results


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def telegram_send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true"
    }).encode()
    request = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API returned an error: {result}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc


def format_draft(item):
    description = item.get("description", "")
    if description:
        description = description.rstrip() + "\n\n"
    time_line = ""
    if item.get("published"):
        try:
            dt = datetime.fromisoformat(item["published"]).astimezone(timezone.utc)
            time_line = f"🕒 {dt.strftime('%d.%m.%Y %H:%M UTC')}\n"
        except ValueError:
            pass
    return (
        "⚡ ЧЕРНОВИК МАРИНАД\n\n"
        f"{item['title']}\n\n{description}"
        f"Источник: {item['source']}\n{time_line}"
        f"Оригинал: {item['link']}\n\n"
        "Проверь факты и формулировку перед публикацией."
    )


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("ADMIN_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("ERROR: set TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID in GitHub Actions secrets.")
        return 2

    config = load_json(CONFIG_PATH, {})
    sources = config.get("sources", [])
    if not sources:
        print("ERROR: no sources configured in news_sources.json.")
        return 2
    max_age_hours = max(1, int(config.get("max_age_hours", 12)))
    max_items = max(1, min(20, int(config.get("max_items_per_run", 10))))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    state = load_json(SEEN_PATH, {"seen": []})
    seen = set(state.get("seen", []))
    collected = []

    for source in sources:
        name, url = str(source.get("name", "Источник")), str(source.get("url", ""))
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
                if published and published < cutoff:
                    continue
                item["_sort"] = published or datetime.min.replace(tzinfo=timezone.utc)
                collected.append(item)
        except Exception as exc:
            print(f"WARN {name}: {type(exc).__name__}: {exc}")

    collected.sort(key=lambda item: item["_sort"], reverse=True)
    selected = collected[:max_items]
    if not selected:
        print("No new recent items to send.")
        return 0

    sent_ids = []
    for item in selected:
        item.pop("_sort", None)
        try:
            telegram_send(token, chat_id, format_draft(item))
            sent_ids.append(item["id"])
            print(f"SENT: {item['source']} — {item['title']}")
            time.sleep(0.4)
        except Exception as exc:
            print(f"ERROR sending item {item.get('id')}: {exc}")
            return 1

    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    updated = list(dict.fromkeys(state.get("seen", []) + sent_ids))[-5000:]
    SEEN_PATH.write_text(json.dumps({"seen": updated}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Finished. Sent {len(sent_ids)} draft(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
