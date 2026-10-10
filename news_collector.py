#!/usr/bin/env python3
"""Marinad News Collector v2.7: stricter filtering and category diversity."""
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
USER_AGENT = "MarinadNewsBot/2.4 (+RSS reader)"
MAX_DESCRIPTION_CHARS = 900
MAX_SEEN_IDS = 5000

# Rule-based signals. These prioritize topics but do not verify facts.
EVENT_RULES = {
    "происшествия": {
        "critical": ["погибли", "погиб", "теракт", "массовая стрельба",
            "ракетный удар", "обрушение", "крупная авария", "массовая эвакуация",
            "много пострадавших", "режим чрезвычайной ситуации",
            "угроза атаки бпла", "опасность атаки бпла",
            "атака беспилотников", "объявили опасность атаки", "массовое отравление"],
        "strong": ["пожар", "взрыв", "нападение", "авария", "беспилотник",
            "бпла", "эвакуация", "пострадали", "экстренные службы",
            "чрезвычайное происшествие"]},
    "экономика": {
        "critical": ["экстренное повышение ставки", "обвал рынка",
            "рекордное падение рубля", "резкое падение рубля",
            "резкое повышение цен", "банк лишился лицензии", "дефолт",
            "биржа приостановила торги"],
        "strong": ["ключевая ставка", "индекс мосбиржи", "курс рубля",
            "центробанк", "инфляция", "цены на бензин", "санкции",
            "рост цен", "падение акций", "безработица", "зарплата",
            "пенсия", "налоги", "экономический прогноз"]},
    "политика": {
        "critical": ["объявил об отставке", "подал в отставку",
            "результаты выборов", "объявлена мобилизация",
            "досрочные выборы", "правительство распущено"],
        "strong": ["выборы", "госдума", "государственная дума", "законопроект",
            "решение суда", "переговоры", "правительство", "президент россии",
            "президент сша", "министр", "дипломат", "посол", "санкции"]},
    "международные события": {
        "critical": ["началась война", "объявление войны",
            "перемирие вступило в силу", "разрыв дипломатических отношений"],
        "strong": ["международный конфликт", "саммит", "нато", "евросоюз",
            "оон", "посол", "граница", "военная операция", "удар по", "переговоры"]},
    "здоровье": {
        "critical": ["введен карантин", "вспышка опасной инфекции",
            "чрезвычайная ситуация в здравоохранении"],
        "strong": ["грипп", "эпидемия", "инфекция", "вакцина", "минздрав",
            "заболеваемость", "больница", "лекарство", "медицинское исследование"]},
    "технологии": {
        "critical": ["массовая утечка данных", "массовый сбой",
            "утечка персональных данных", "крупная кибератака"],
        "strong": ["искусственный интеллект", "кибербезопасность", "кибератака",
            "сбой сервиса", "смартфон", "научное открытие", "технология", "робот"]},
    "общество": {
        "critical": ["массовая эвакуация", "режим чрезвычайной ситуации",
            "крупное отключение электричества"],
        "strong": ["общественный транспорт", "отключение электричества", "школа",
            "университет", "жилье", "коммунальные услуги", "погода", "наводнение", "шторм"]},
    "спорт": {
        "critical": ["выиграла чемпионат мира", "установил мировой рекорд",
            "завоевал олимпийское золото"],
        "strong": ["сборная россии", "финал турнира", "чемпионат", "олимпиада",
            "футбол", "хоккей"]},
    "развлечения": {
        "critical": [],
        "strong": ["фестиваль", "премьера", "афиша", "билеты на концерт",
            "модный образ", "выбрал костюм", "гороскоп", "рецепт дня",
            "откроется выставка"]},
}
GENERIC_LOW_SIGNAL = {"впервые", "президент", "россия", "заявил",
    "сообщает", "названа", "рассказал", "стало известно", "по словам"}
LOW_PRIORITY_DEFAULT = ["гороскоп", "рецепт дня", "афиша", "фестиваль",
    "билеты на концерт", "премьера спектакля", "откроется выставка",
    "модный образ", "выбрал костюм", "в чем вышел", "народные приметы",
    "приметы погоды", "что расскажет погода", "лунный календарь"]

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
        except (TypeError, ValueError, OverflowError):
            return None

def fetch_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with urllib.request.urlopen(req, timeout=20) as response:
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
                    rel, candidate = child.attrib.get("rel", "alternate"), child.attrib.get("href", "")
                    if candidate and rel in ("alternate", ""):
                        link = candidate
                        break
        description = clean_text(child_text(node, ("description", "summary", "content", "encoded")))
        published = parse_date(child_text(node, ("pubdate", "published", "updated", "date", "issued")))
        if title and link.startswith(("https://", "http://")):
            identifier = child_text(node, ("guid", "id")) or link
            results.append({"id": f"{source_name}:{identifier}", "source": source_name,
                "title": title, "link": link, "description": description[:MAX_DESCRIPTION_CHARS],
                "published": published.isoformat() if published else "", "_published_dt": published})
    return results

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default

def load_seen_state():
    raw = load_json(SEEN_PATH, {"seen": []})
    ids = raw if isinstance(raw, list) else raw.get("seen", []) if isinstance(raw, dict) else []
    if not isinstance(ids, list):
        ids = []
    return {"seen": list(dict.fromkeys(v for v in ids if isinstance(v, str) and v))[-MAX_SEEN_IDS:]}

def save_seen_state(state):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = SEEN_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(SEEN_PATH)

def normalize_title(title):
    title = title.lower().replace("ё", "е")
    title = re.sub(r"https?://\S+", " ", title)
    title = re.sub(r"[^a-zа-я0-9]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()

def normalize_keyword(keyword):
    return re.sub(r"\s+", " ", str(keyword).lower().replace("ё", "е")).strip()

def keyword_matches(text, keyword):
    keyword = normalize_keyword(keyword)
    if not keyword:
        return False
    return re.search(r"(?<![a-zа-я0-9])" + re.escape(keyword) + r"(?![a-zа-я0-9])", text) is not None

def matched_keywords(text, keywords):
    return [str(k) for k in keywords if keyword_matches(text, k)]

def classify_news(title, description=""):
    title, description = normalize_keyword(title), normalize_keyword(description)
    scores, evidence = {}, {}
    for category, groups in EVENT_RULES.items():
        critical = matched_keywords(title, groups["critical"])
        strong_title = matched_keywords(title, groups["strong"])
        strong_body = matched_keywords(description, groups["strong"])
        value = (28 if critical else 0) + (min(18, 10 + 3 * (len(strong_title)-1)) if strong_title else 0)
        if not strong_title and strong_body:
            value += 4
        if critical or strong_title or strong_body:
            scores[category] = value
            evidence[category] = {"critical": critical, "title": strong_title, "description": strong_body}
    if not scores:
        return "прочее", {}, {}
    category = max(scores, key=scores.get)
    return category, scores, evidence

def score_news(item, rules):
    title = normalize_keyword(item.get("title", ""))
    description = normalize_keyword(item.get("description", ""))
    category, category_scores, evidence = classify_news(title, description)
    score, reasons = 20, []
    cat_score = category_scores.get(category, 0)
    score += cat_score
    if cat_score:
        reasons.append(f"категория: {category}")
        for field, label in (("critical", "сильный сигнал"), ("title", "сигнал в заголовке"),
                             ("description", "сигнал в описании")):
            matches = evidence.get(category, {}).get(field, [])
            if matches:
                reasons.append(f"{label}: {', '.join(matches[:2])}")
    high = rules.get("high_priority_keywords", [])
    medium = rules.get("medium_priority_keywords", [])
    low = rules.get("low_priority_keywords", LOW_PRIORITY_DEFAULT)
    high_title, high_body = matched_keywords(title, high), matched_keywords(description, high)
    medium_title, medium_body = matched_keywords(title, medium), matched_keywords(description, medium)
    low_title = matched_keywords(title, list(dict.fromkeys(list(low) + LOW_PRIORITY_DEFAULT)))
    if high_title:
        score += min(20, 14 + 2 * (len(high_title)-1))
        reasons.append("важная тема: " + ", ".join(high_title[:3]))
    elif high_body:
        score += 5
        reasons.append("важная тема в описании: " + ", ".join(high_body[:2]))
    useful_medium = [w for w in medium_title if normalize_keyword(w) not in GENERIC_LOW_SIGNAL]
    if useful_medium:
        score += min(8, 4 + 2 * (len(useful_medium)-1))
        reasons.append("дополнительная тема: " + ", ".join(useful_medium[:3]))
    elif medium_body:
        score += 1
        reasons.append("тематическое упоминание в описании")
    if low_title:
        score -= min(22, 12 + 4 * (len(low_title)-1))
        reasons.append("низкий редакционный приоритет: " + ", ".join(low_title[:2]))
    published = item.get("_published_dt")
    if published:
        age = (datetime.now(timezone.utc) - published).total_seconds() / 3600
        if 0 <= age <= 2:
            score += 5
            reasons.append("свежесть: до 2 часов")
        elif 2 < age <= 6:
            score += 3
            reasons.append("свежесть: до 6 часов")
        elif 6 < age <= 12:
            score += 1
            reasons.append("свежесть: до 12 часов")
        elif 12 < age <= 24:
            # Small bonus keeps eligible older stories in consideration without
            # letting age outweigh relevance or reliability signals.
            score += 1
            reasons.append("свежесть: до 24 часов")
    return max(0, min(100, score)), reasons or ["общих значимых сигналов не найдено"]

def similarity(a, b):
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    wa, wb = set(na.split()), set(nb.split())
    jaccard = len(wa & wb) / max(1, len(wa | wb))
    return max(seq, jaccard)


INCIDENT_TYPES = {
    "дтп": ["дтп", "столкновен", "авария", "сбил", "наезд", "погиб под колесами"],
    "беспилотники": ["бпла", "беспилотник", "дрон", "атака беспилот"],
    "пожар": ["пожар", "загорел", "возгорание"],
    "взрыв": ["взрыв", "взорвал"],
    "погода": ["шторм", "ураган", "снегопад", "наводнение", "паводок"],
    "уголовное": ["задержан", "арестован", "обвиняется", "подозреваемый"],
}
LOCATION_RE = re.compile(
    r"\b([а-яё-]+\s+(?:крае|области|республике|округе|районе))\b", re.I
)
CITY_RE = re.compile(
    r"\b(?:в|во|на)\s+(?:городском округе\s+|городе\s+|поселке\s+|посёлке\s+)?([а-яё-]{4,}(?:\s+[а-яё-]{4,})?)",
    re.I,
)
GENERIC_LOCATIONS = {
    "россии", "россия", "стране", "регионе", "регионов", "мире", "сша",
    "европе", "украине", "страны", "области", "крае", "городе", "результате",
    "случае", "связи", "рамках", "ходе", "время", "субботу", "понедельник",
    "вторник", "среду", "четверг", "пятницу", "сегодня", "вчера", "октябре"
}

def event_signature(item):
    """Conservative event fingerprint: incident type + specific location phrase."""
    text = normalize_keyword(f"{item.get('title', '')} {item.get('description', '')}")
    kinds = [kind for kind, terms in INCIDENT_TYPES.items()
             if any(normalize_keyword(term) in text for term in terms)]
    locations = []
    for match in LOCATION_RE.finditer(text):
        phrase = normalize_keyword(match.group(1))
        words = [w for w in phrase.split() if w not in GENERIC_LOCATIONS]
        if words and words[0] not in GENERIC_LOCATIONS and len(words[0]) > 3:
            locations.append(" ".join(_stem_location_word(w) for w in words))
    # City names often appear in different grammatical forms across publishers:
    # e.g. «в Мытищах» and «в городском округе Мытищи».
    for match in CITY_RE.finditer(text):
        words = [w for w in normalize_keyword(match.group(1)).split()
                 if w not in GENERIC_LOCATIONS and w not in {"городском", "округе", "город", "поселке", "посёлке"}]
        if words and len(words[0]) > 4:
            locations.append(" ".join(_stem_location_word(w) for w in words))
    return set(kinds), set(locations)

def _stem_location_word(word):
    """Very conservative ending normalization for Russian place-name cases."""
    word = normalize_keyword(word)
    for suffix in ("ами", "ями", "ого", "ему", "ому", "ыми", "ими", "ах", "ях",
                   "ов", "ев", "ей", "ам", "ям", "ом", "ем", "ой", "ей", "ью",
                   "ия", "ии", "ья", "ье", "и", "ы", "а", "я", "е", "у"):
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word

def same_event(a, b):
    """Only merge cross-source items with a shared event type and location."""
    kinds_a, loc_a = event_signature(a)
    kinds_b, loc_b = event_signature(b)
    if not (kinds_a & kinds_b and loc_a & loc_b):
        return False
    da, db = a.get("_published_dt"), b.get("_published_dt")
    if da and db and abs((da - db).total_seconds()) > 8 * 3600:
        return False
    return True

def virality_score(item):
    """Heuristic share/discussion potential, not a reach forecast or truth score."""
    raw_title = item.get("title", "")
    title = normalize_keyword(raw_title)
    body = normalize_keyword(item.get("description", ""))
    text = f"{title} {body}"
    score = 18

    # Strong event or conflict signals; match stems to cover inflected Russian forms.
    high_impact = ["погиб", "погибл", "теракт", "массовая стрельба", "взрыв",
                   "скандал", "разоблач", "запретил", "отменил", "обрушил",
                   "рекорд", "миллион", "миллиард", "утечка данных", "слив данных",
                   "арестовал", "задержал", "признался", "неожиданно", "шок"]
    medium_impact = ["штраф", "подорожал", "подешевел", "закрылся", "уволил",
                     "исчез", "спас", "победил", "провал", "новый закон", "запустил"]
    if any(term in title for term in high_impact):
        score += 24
    elif any(term in body for term in high_impact):
        score += 10
    if any(term in title for term in medium_impact):
        score += 12
    elif any(term in body for term in medium_impact):
        score += 5

    social_signals = ["видео", "на видео", "очевидцы", "пользователи сети",
                      "соцсет", "обсуждают", "мем", "вирусн", "разошлось по сети"]
    if any(term in text for term in social_signals):
        score += 10
    if "?" in raw_title or "!" in raw_title:
        score += 4
    if re.search(r"\b\d{2,}\b", title):
        score += 4
    # Routine service/lifestyle items should not look viral merely because they are fresh.
    low_interest = ["народные приметы", "гороскоп", "рецепт дня", "афиша",
                    "прогноз погоды", "что расскажет погода", "курс валют"]
    if any(term in title for term in low_interest):
        score -= 14
    return max(0, min(100, score))

def source_type(source):
    """Publisher class only; it does not verify a particular claim."""
    name = normalize_keyword(source)
    official = ["министерство", "правительство", "мчс", "полиция", "росстат", "мэрия"]
    established = ["риа новости", "тасс", "интерфакс", "российская газета",
                   "коммерсант", "известия", "лента.ру", "москва 24", "мк"]
    if any(term in name for term in official):
        return "официальный источник"
    if any(term in name for term in established):
        return "редакционное СМИ"
    return "тип источника не классифицирован"


def deduplicate(items, threshold):
    kept = []
    for item in items:
        duplicate = False
        for prev in kept:
            if similarity(item["title"], prev["title"]) >= threshold or same_event(item, prev):
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return kept

def ranking_score(item):
    """Editorial-first ranking that also considers share/discussion potential.

    The editorial score remains dominant (75%); virality is a secondary signal
    (25%), never a measure of truth or a reason to skip fact-checking.
    """
    editorial = max(0, min(100, int(item.get("score", 20))))
    viral = max(0, min(100, int(item.get("virality_score", virality_score(item)))))
    return round(editorial * 0.75 + viral * 0.25)


def diverse_select(items, max_items, max_per_category=3):
    """Select high-ranked stories while preventing one category from dominating."""
    selected, counts, remaining = [], {}, list(items)
    while remaining and len(selected) < max_items:
        eligible = [
            (i, item) for i, item in enumerate(remaining)
            if counts.get(item.get("category", "прочее"), 0) < max_per_category
        ]
        if not eligible:
            break
        idx, item = max(
            eligible,
            key=lambda pair: (
                ranking_score(pair[1])
                - counts.get(pair[1].get("category", "прочее"), 0) * 12,
                int(pair[1].get("score", 0)),
                pair[1].get("_sort") or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        remaining.pop(idx)
        selected.append(item)
        cat = item.get("category", "прочее")
        counts[cat] = counts.get(cat, 0) + 1
    return selected

def telegram_send(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message,
        "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram API rejected the message.")
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
            dt = datetime.fromisoformat(item["published"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            time_line = f"🕒 {dt.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}\n"
        except (TypeError, ValueError):
            pass
    reasons = "; ".join(item.get("score_reasons", []))
    viral = item.get("virality_score", virality_score(item))
    reliability = source_type(item.get("source", ""))
    return ("⚡ ЧЕРНОВИК МАРИНАД\n\n" f"{item['title']}\n\n"
        f"Категория: {item.get('category', 'прочее')}\n"
        f"Редакционный приоритет: {item.get('score', 20)}/100\n"
        f"Вирусный потенциал: {viral}/100 (эвристическая оценка)\n"
        f"Источник: {reliability}; фактчек новости не выполнен\n"
        f"Причина: {reasons}\n\n" f"{description}"
        f"Источник: {item['source']}\n" f"{time_line}"
        f"Оригинал: {item['link']}\n\n"
        "Проверь факты и формулировку перед публикацией.")

def main():
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("ADMIN_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("ERROR: Telegram secrets are missing.")
        return 2
    config, rules = load_json(CONFIG_PATH, {}), load_json(FILTER_PATH, None)
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
    for key in ("high_priority_keywords", "medium_priority_keywords", "low_priority_keywords"):
        if key in rules and not isinstance(rules[key], list):
            print(f"ERROR: {key} must be a JSON array.")
            return 2
    try:
        max_age = max(1, int(config.get("max_age_hours", 12)))
        max_items = max(1, min(20, int(config.get("max_items_per_run", 5))))
        min_score = max(0, min(100, int(rules.get("min_score", 20))))
        similarity_limit = max(0.5, min(1.0, float(rules.get("max_similar_title_ratio", 0.88))))
    except (TypeError, ValueError):
        print("ERROR: invalid numeric configuration.")
        return 2
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)
    state, collected = load_seen_state(), []
    seen, skipped_score = set(), 0
    seen = set(state["seen"])
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
                item["score"], item["score_reasons"] = score_news(item, rules)
                item["virality_score"] = virality_score(item)
                item["category"] = classify_news(item["title"], item["description"])[0]
                if item["score"] < min_score:
                    skipped_score += 1
                    print(f"SKIP low score ({item['score']}): {item['title']}")
                    continue
                item["_sort"] = published
                collected.append(item)
        except Exception as exc:
            print(f"WARN {name}: {type(exc).__name__}: {exc}")
    collected.sort(key=lambda x: (ranking_score(x), x["score"], x["_sort"]), reverse=True)
    unique = deduplicate(collected, similarity_limit)
    for item in unique:
        print(f"CANDIDATE rank={ranking_score(item)} score={item['score']} "
              f"viral={item.get('virality_score', 0)} category={item['category']} "
              f"reasons={'; '.join(item['score_reasons'])} — {item['title']}")
    selected = diverse_select(unique, max_items)
    print(f"FILTER SUMMARY: candidates={len(collected)}, low_score={skipped_score}, "
          f"duplicates={len(collected)-len(unique)}, selected={len(selected)}")
    if not selected:
        print("No new items passed the filters.")
        return 0
    sent = 0
    for item in selected:
        item.pop("_sort", None)
        item.pop("_published_dt", None)
        try:
            telegram_send(token, chat_id, format_draft(item))
        except Exception as exc:
            print(f"ERROR sending item {item.get('id')}: {type(exc).__name__}: {exc}")
            print(f"Stopped after sending {sent} draft(s).")
            return 1
        state["seen"].append(item["id"])
        state["seen"] = list(dict.fromkeys(state["seen"]))[-MAX_SEEN_IDS:]
        try:
            save_seen_state(state)
        except OSError as exc:
            print(f"CRITICAL: could not save seen.json: {exc}")
            return 1
        sent += 1
        print(f"SENT score={item['score']} category={item['category']} "
              f"{item['source']} — {item['title']}")
        time.sleep(0.4)
    print(f"Finished. Sent {sent} draft(s).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
