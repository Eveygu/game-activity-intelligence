#!/usr/bin/env python3
"""Discover and inspect public game announcement pages; failures are never treated as zero activity."""
import json, re, hashlib, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
DISCOVERY_FILE = ROOT / "data/discoveries.json"
STATUS_FILE = ROOT / "data/source_status.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GameActivityIntelBot/0.2; +https://github.com/Eveygu/game-activity-intelligence)"}
TIMEOUT = 20
KEYWORDS = re.compile(r"活动|公告|更新|版本|赛季|联动|上新|礼包|通行证|充值|签到|福利|维护|赛事|展会|キャンペーン|イベント|アップデート|コラボ|シーズン|お知らせ|開催", re.I)
DATE_PATTERNS = [
    re.compile(r"(20\d{2})[年./-]\s?(\d{1,2})[月./-]\s?(\d{1,2})日?"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
]
SKIP_EXT = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|svg|pdf|zip|mp4)(?:\?|$)", re.I)

def now():
    return datetime.now(timezone.utc)

def iso(dt=None):
    return (dt or now()).isoformat(timespec="seconds")

def clean_url(url):
    return urldefrag(url)[0].rstrip("/")

def parse_date(value):
    if not value:
        return None
    value = str(value).strip()
    for pattern in DATE_PATTERNS:
        m = pattern.search(value)
        if m:
            try:
                y, mo, d = map(int, m.groups())
                return datetime(y, mo, d, tzinfo=timezone.utc).date().isoformat()
            except ValueError:
                pass
    return None

def extract_dates(soup, text):
    published = None
    for selector, attr in [
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="pubdate"]', "content"),
        ('meta[name="publishdate"]', "content"),
        ('meta[name="date"]', "content"),
        ("time[datetime]", "datetime"),
    ]:
        node = soup.select_one(selector)
        if node:
            published = node.get(attr)
            if published:
                break
    published = published or None
    start = None
    end = None
    range_match = re.search(r"((?:20\d{2}[年./-])?\s?\d{1,2}[月./-]\s?\d{1,2}日?)\s*(?:至|到|~|～|—|-|〜)\s*((?:20\d{2}[年./-])?\s?\d{1,2}[月./-]\s?\d{1,2}日?)", text)
    if range_match:
        start = parse_date(range_match.group(1))
        end = parse_date(range_match.group(2))
    return published, start, end

def classify(text):
    rules = [
        ("联动合作", r"联动|合作|コラボ"),
        ("赛事展会", r"赛事|比赛|展会|线下|大会|eスポーツ"),
        ("商业化上新", r"礼包|通行证|皮肤|外观|上新|商城|充值|販売|パス"),
        ("版本更新", r"更新|版本|维护|アップデート|メンテナンス"),
        ("运营活动", r"活动|福利|签到|キャンペーン|イベント|開催|抽奖|评论赢"),
    ]
    for label, pattern in rules:
        if re.search(pattern, text, re.I):
            return label
    return "官方资讯"

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def candidate_links(soup, base_url):
    found = []
    for a in soup.select("a[href]"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = clean_url(urljoin(base_url, a.get("href", "")))
        if not title or len(title) < 5 or len(title) > 180:
            continue
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or parsed.netloc != urlparse(base_url).netloc:
            continue
        if SKIP_EXT.search(href) or href == clean_url(base_url):
            continue
        context = a.parent.get_text(" ", strip=True) if a.parent else title
        if KEYWORDS.search(title + " " + context) or re.search(r"/(news|notice|activity|event|topic|moment|article|detail|announcement)/", parsed.path, re.I):
            found.append((title, href, " ".join(context.split())[:700]))
    return found

def fetch_page(session, url):
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        raise ValueError("页面不是可解析的 HTML 文本")
    return response, BeautifulSoup(response.text, "html.parser")

def main():
    data = load_json(DISCOVERY_FILE, {"updated_at": None, "items": []})
    items = data.get("items", [])
    known = {clean_url(x.get("url", "")) for x in items if x.get("url")}
    statuses = []
    additions = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for game in CONFIG["games"]:
        for source in game.get("sources", []):
            url = source["url"]
            status = {
                "game_id": game["id"], "game_name": game["name"],
                "source_type": source["type"], "source_label": source["label"],
                "url": url, "checked_at": iso(), "status": "unknown",
                "http_status": None, "candidate_links": 0, "details_read": 0,
                "new_items": 0, "error": None
            }
            try:
                response, soup = fetch_page(session, url)
                status["http_status"] = response.status_code
                links = candidate_links(soup, response.url)
                status["candidate_links"] = len(links)
                for title, href, context in links[:20]:
                    normalized = clean_url(href)
                    if normalized in known:
                        continue
                    detail_title, detail_text = title, context
                    detail_soup = None
                    detail_url = href
                    try:
                        detail_response, detail_soup = fetch_page(session, href)
                        status["details_read"] += 1
                        main = detail_soup.select_one("article, main, .article, .content, .detail, .post-content") or detail_soup
                        detail_text = " ".join(main.get_text(" ", strip=True).split())[:3500]
                        h = detail_soup.select_one("h1") or detail_soup.select_one("title")
                        if h and h.get_text(" ", strip=True):
                            detail_title = h.get_text(" ", strip=True)[:180]
                    except Exception as detail_error:
                        detail_text = context + " [详情页读取失败: " + str(detail_error)[:160] + "]"
                    combined = detail_title + " " + detail_text
                    published, start_date, end_date = extract_dates(detail_soup or soup, combined)
                    age_days = None
                    if published:
                        try:
                            age_days = (now().date() - datetime.fromisoformat(published).date()).days
                        except ValueError:
                            pass
                    item = {
                        "id": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
                        "game_id": game["id"], "game_name": game["name"],
                        "title": detail_title, "summary": detail_text[:900],
                        "published_at": published, "start_date": start_date, "end_date": end_date,
                        "age_days": age_days,
                        "historical_candidate": bool(age_days is not None and age_days > 180),
                        "category": classify(combined),
                        "commercial_mechanism": None, "price": None,
                        "source_type": source["type"], "source_label": source["label"],
                        "url": detail_url, "evidence": detail_text[:1200],
                        "confidence": 0.72 if detail_soup else 0.45,
                        "review_status": "疑似历史信息，待核验" if age_days is not None and age_days > 180 else "待人工核验",
                        "discovered_at": iso()
                    }
                    additions.append(item)
                    known.add(normalized)
                    status["new_items"] += 1
                    time.sleep(0.5)
                status["status"] = "success"
            except Exception as exc:
                status["status"] = "failed"
                status["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            statuses.append(status)
            time.sleep(1)

    data["updated_at"] = iso()
    data["items"] = additions + items
    DISCOVERY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATUS_FILE.write_text(json.dumps({"updated_at": iso(), "sources": statuses}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Sources checked: {}; successful: {}; failed: {}; detail pages read: {}; new candidates: {}".format(
        len(statuses), sum(s["status"] == "success" for s in statuses),
        sum(s["status"] == "failed" for s in statuses),
        sum(s["details_read"] for s in statuses), len(additions)))
    for s in statuses:
        print("{} | {} | {} | candidates={} | details={} | new={} | {}".format(
            s["game_id"], s["source_label"], s["status"], s["candidate_links"],
            s["details_read"], s["new_items"], s["error"] or ""))

if __name__ == "__main__":
    main()
