#!/usr/bin/env python3
"""Fetch configured public game news pages and retain source health separately."""
import json, re, hashlib, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
DISCOVERY_FILE = ROOT / "data/discoveries.json"
STATUS_FILE = ROOT / "data/source_status.json"
HEADERS = {"User-Agent": "GameActivityIntelBot/0.1 (public competitive-intelligence; contact: repository owner)"}
TIMEOUT = 18
KEYWORDS = re.compile(r"活动|公告|更新|版本|赛季|联动|上新|礼包|通行证|充值|签到|福利|维护|赛事|展会|キャンペーン|イベント|アップデート|コラボ|シーズン|お知らせ|開催", re.I)
DATE_RE = re.compile(r"(20\d{2}[年./-]\s?\d{1,2}[月./-]\s?\d{1,2}日?)")

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def classify(text):
    rules = [
        ("联动合作", r"联动|合作|コラボ"),
        ("商业化上新", r"礼包|通行证|皮肤|外观|上新|商城|充值|販売"),
        ("版本更新", r"更新|版本|维护|アップデート"),
        ("运营活动", r"活动|福利|签到|キャンペーン|イベント|開催"),
        ("赛事展会", r"赛事|比赛|展会|线下|大会"),
    ]
    for label, pattern in rules:
        if re.search(pattern, text, re.I):
            return label
    return "官方资讯"

def extract_date(text):
    m = DATE_RE.search(text)
    return m.group(0) if m else None

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def main():
    existing = load_json(DISCOVERY_FILE, {"updated_at": None, "items": []})
    items = existing.get("items", [])
    known = {x.get("url") for x in items if x.get("url")}
    statuses = []
    new_items = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for game in CONFIG["games"]:
        for source in game.get("sources", []):
            url = source["url"]
            checked = now_iso()
            status = {
                "game_id": game["id"], "game_name": game["name"],
                "source_type": source["type"], "source_label": source["label"],
                "url": url, "checked_at": checked, "status": "unknown",
                "http_status": None, "items_found": 0, "error": None
            }
            try:
                response = session.get(url, timeout=TIMEOUT)
                status["http_status"] = response.status_code
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                candidates = []
                for a in soup.select("a[href]"):
                    title = " ".join(a.get_text(" ", strip=True).split())
                    href = urljoin(response.url, a.get("href", ""))
                    if not title or len(title) < 5 or len(title) > 180:
                        continue
                    if urlparse(href).netloc != urlparse(response.url).netloc:
                        continue
                    context = a.parent.get_text(" ", strip=True) if a.parent else title
                    if KEYWORDS.search(title + " " + context):
                        candidates.append((title, href, context[:500]))
                seen_local = set()
                for title, href, context in candidates:
                    if href in known or href in seen_local:
                        continue
                    seen_local.add(href)
                    if href.rstrip("/") == response.url.rstrip("/"):
                        continue
                    item = {
                        "id": hashlib.sha256(href.encode("utf-8")).hexdigest()[:16],
                        "game_id": game["id"], "game_name": game["name"],
                        "title": title, "summary": context,
                        "published_at": None,
                        "start_date": extract_date(context),
                        "end_date": None,
                        "category": classify(title + " " + context),
                        "commercial_mechanism": None, "price": None,
                        "source_type": source["type"], "source_label": source["label"],
                        "url": href, "evidence": context,
                        "confidence": 0.55, "review_status": "待人工核验",
                        "discovered_at": now_iso()
                    }
                    new_items.append(item)
                    known.add(href)
                status["items_found"] = len(candidates)
                status["status"] = "success"
            except Exception as exc:
                status["status"] = "failed"
                status["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            statuses.append(status)
            time.sleep(1)

    # Preserve history; newest discoveries first.
    items = new_items + items
    existing["updated_at"] = now_iso()
    existing["items"] = items[:5000]
    DISCOVERY_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATUS_FILE.write_text(json.dumps({"updated_at": now_iso(), "sources": statuses}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Sources checked: {len(statuses)}; successful: {sum(s['status']=='success' for s in statuses)}; failed: {sum(s['status']=='failed' for s in statuses)}; new candidate links: {len(new_items)}")

if __name__ == "__main__":
    main()
