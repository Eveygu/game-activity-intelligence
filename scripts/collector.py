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

def parse_date(value, reference_year=None):
    """Return a validated YYYY-MM-DD date; never substitute today's date."""
    if not value:
        return None
    value = str(value).strip()
    # Full year first: 2026-11-03 / 2026年11月3日 / 20261103
    for pattern in DATE_PATTERNS:
        m = pattern.search(value)
        if m:
            try:
                y, mo, d = map(int, m.groups())
                return datetime(y, mo, d, tzinfo=timezone.utc).date().isoformat()
            except ValueError:
                return None
    # Month/day only is accepted only when an explicit reference year exists.
    m = re.search(r"(?<!\d)(\d{1,2})[月./-](\d{1,2})日?(?!\d)", value)
    if m and reference_year:
        try:
            return datetime(int(reference_year), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc).date().isoformat()
        except ValueError:
            return None
    return None

def normalize_published(value):
    """Normalize publication date to YYYY-MM-DD, preserving no guessed timezone."""
    if not value:
        return None
    raw = str(value).strip()
    parsed = parse_date(raw)
    return parsed

def extract_dates(soup, text):
    # Publication date: only accept explicitly labelled publication metadata.
    published = None
    card_time = re.search(
        r"发布时间\\s*[：:]\\s*((?:20\\d{2})[年./-]\\s?\\d{1,2}[月./-]\\s?\\d{1,2}日?(?:\\s+\\d{1,2}:\\d{2}(?::\\d{2})?)?|20\\d{2}\\d{4})",
        text or ""
    )
    if card_time:
        published = normalize_published(card_time.group(1))
    if not published and soup:
        for selector, attr in [
            ('meta[property="article:published_time"]', "content"),
            ('meta[name="pubdate"]', "content"),
            ('meta[name="publishdate"]', "content"),
            ('meta[name="date"]', "content"),
        ]:
            node = soup.select_one(selector)
            if node:
                published = normalize_published(node.get(attr))
                if published:
                    break

    # Activity periods require both a date range and nearby activity wording.
    start = end = None
    source_text = re.sub(r"\\s+", " ", text or "")
    date_token = r"(?:20\\d{2}\\s*[年./-]\\s*)?\\d{1,2}\\s*[月./-]\\s*\\d{1,2}日?"
    range_re = re.compile(r"(" + date_token + r")\\s*(?:至|到|~|～|—|–|〜|-)\\s*(" + date_token + r")")
    activity_context = re.compile(
        r"活动|测试|开放|招募|报名|预约|上线|上架|售卖|发售|领取|兑换|比赛|赛事|赛程|持续时间|開催|実施|イベント|キャンペーン|販売|募集|受付|期間",
        re.I
    )
    reference_year = int(published[:4]) if published else None
    for match in range_re.finditer(source_text):
        context = source_text[max(0, match.start()-100):min(len(source_text), match.end()+100)]
        if not activity_context.search(context):
            continue
        candidate_start = parse_date(match.group(1), reference_year)
        candidate_end = parse_date(match.group(2), reference_year)
        if not candidate_start or not candidate_end:
            continue
        if candidate_end < candidate_start and not re.search(r"20\\d{2}", match.group(2)):
            try:
                candidate_end = datetime(int(candidate_start[:4])+1, int(candidate_end[5:7]), int(candidate_end[8:10]), tzinfo=timezone.utc).date().isoformat()
            except ValueError:
                continue
        start, end = candidate_start, candidate_end
        break
    return published, start, end

def classify(text, title=""):
    title = title or ""
    text = text or ""
    # 标题命中优先，避免详情页模板/推荐区关键词覆盖当前公告主题。
    title_rules = [
        ("游戏治理", "反外挂/封禁", r"反外挂|外挂打击|封禁名单|作弊处罚|违规账号|チート|不正行為"),
        ("品牌营销", "IP联动", r"联动|コラボ"),
        ("赛事电竞", "职业/官方赛事", r"赛事|比赛|电竞|eスポーツ|锦标赛"),
        ("版本内容", "版本/玩法更新", r"アップデート|更新公告|维护公告|版本更新|玩法更新"),
        ("商业化", "外观上新", r"皮肤|外观|时装|スキン"),
        ("运营活动", "节日/主题活动", r"节日|周年庆|主题活动|季节活动"),
        ("运营活动", "福利活动", r"福利|签到|登录奖励|免费领取|抽奖|评论赢|周边"),
        ("运营活动", "限时运营活动", r"活动|キャンペーン|イベント|開催|招募|测试资格|开测|首测|二测|実況者|配信者|クリエイター|募集"),
    ]
    for category, subcategory, pattern in title_rules:
        if re.search(pattern, title, re.I):
            return category, subcategory
    # 对日服官网，页面模板固定展示联动横幅，分类只采用公告标题。
    if "コラボ" in text and not title:
        return "待核验", "未归类官方资讯"
    if re.search(r"反外挂|外挂打击|封禁名单|作弊处罚|违规账号|チート|不正行為", text, re.I):
        return "游戏治理", "反外挂/封禁"
    rules = [
        ("品牌营销", "品牌合作", r"品牌合作|合作伙伴|コラボレーション"),
        ("品牌营销", "线下营销", r"线下|快闪|展台|线下活动"),
        ("赛事电竞", "职业/官方赛事", r"赛事|比赛|电竞|eスポーツ|锦标赛"),
        ("商业化", "外观上新", r"皮肤|外观|时装|服装|武器外观|スキン"),
        ("商业化", "付费礼包/直购", r"礼包|直购|充值|售价|价格|販売|ショップ"),
        ("商业化", "通行证/赛季付费", r"通行证|赛季手册|战令|パス"),
        ("版本内容", "版本/玩法更新", r"版本|玩法|新地图|新模式|アップデート"),
        ("版本内容", "平衡性调整", r"平衡性|数值调整|调整说明|武器调整"),
        ("版本内容", "维护修复", r"维护|修复|停服|更新公告|メンテナンス"),
        ("运营活动", "节日/主题活动", r"节日|周年庆|主题活动|季节活动"),
        ("运营活动", "福利活动", r"福利|签到|登录奖励|免费领取|抽奖|评论赢|周边"),
        ("运营活动", "限时运营活动", r"活动|キャンペーン|イベント|開催|招募|测试资格"),
    ]
    for category, subcategory, pattern in rules:
        if re.search(pattern, text, re.I):
            return category, subcategory
    return "待核验", "未归类官方资讯"

def classify_channel(source, title, text, author="", url=""):
    if source.get("label") == "TapTap官方公告" or "/activities/lottery/" in (url or ""):
        return "官方动作"
    if source.get("type") not in ("官方社区", "社区论坛"):
        return "官方动作"
    author = (author or "").strip()
    if re.search(r"官方版主|官方账号|官方运营|官方人员|ECC组委会|运营团队", author, re.I):
        return "官方动作"
    if author:
        return "舆论信息"
    return "待核验"

def extract_taptap_card(card):
    link = card.select_one('a[href*="/moment/"]')
    if not link:
        return None
    href = link.get("href", "")
    title_node = card.select_one(".moment-article__title, .moment-card__title, .moment-summary-text__content, .moment-article__summary--content")
    title = " ".join((title_node or link).get_text(" ", strip=True).split())
    author_node = card.select_one(".user-name__text, [itemprop='name']")
    author = " ".join(author_node.get_text(" ", strip=True).split()) if author_node else ""
    moderator = card.select_one(".user-name__moderator-tag")
    if moderator:
        author = (author + " " + moderator.get_text(" ", strip=True)).strip()
    time_node = card.select_one(".moment-card__time[title], time[datetime]")
    published = (time_node.get("title") or time_node.get("datetime")) if time_node else ""
    body_node = card.select_one(".moment-summary-text__content, .tap-rich-content__body, .moment-article__summary--content")
    body = " ".join(body_node.get_text(" ", strip=True).split()) if body_node else title
    context = "作者：" + author + " 发布时间：" + published + " 正文：" + body
    return title[:180], href, context[:700]


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def candidate_links(soup, base_url):
    found = []
    if "taptap.cn" in urlparse(base_url).netloc and "/topic" in urlparse(base_url).path:
        cards = soup.select(".moment-card.moment-feed-card, .moment-card--isfeed")
        for card in cards:
            parsed_card = extract_taptap_card(card)
            if parsed_card:
                title, href, context = parsed_card
                href = clean_url(urljoin(base_url, href))
                if title and len(title) >= 2:
                    found.append((title, href, context))
        if found:
            return found
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
    # requests may guess an incorrect legacy encoding on Chinese game sites.
    try:
        response.encoding = response.apparent_encoding or "utf-8"
    except Exception:
        response.encoding = "utf-8"
    return response, BeautifulSoup(response.content, "html.parser", from_encoding=response.encoding)

def main():
    data = load_json(DISCOVERY_FILE, {"updated_at": None, "items": []})
    items = data.get("items", [])
    known = {clean_url(x.get("url", "")) for x in items if x.get("url")}
    existing_by_url = {clean_url(x.get("url", "")): x for x in items if x.get("url")}
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
                "new_items": 0, "refreshed_items": 0, "error": None
            }
            try:
                response, soup = fetch_page(session, url)
                status["http_status"] = response.status_code
                links = candidate_links(soup, response.url)
                status["candidate_links"] = len(links)
                for title, href, context in links[:20]:
                    normalized = clean_url(href)
                    # Re-read known links as well, so metadata fixes backfill historical records.
                    detail_title, detail_text = title, context
                    detail_soup = None
                    detail_url = href
                    try:
                        detail_response, detail_soup = fetch_page(session, href)
                        status["details_read"] += 1
                        is_taptap = "taptap.cn" in urlparse(detail_url).netloc
                        if is_taptap:
                            # 列表卡片已提供当前帖子的独立正文/作者；详情页常含整个论坛推荐流，
                            # 因此不再用详情页全量文本覆盖干净的卡片上下文。
                            detail_text = context[:1200]
                        else:
                            main = detail_soup.select_one("article, .article-content, .news-content, .news_detail, .post-content, .detail-content, .content-detail, .article_body") or detail_soup.select_one("main") or detail_soup
                            for noise in main.select("header, footer, nav, aside, script, style, .nav, .header, .footer, .breadcrumb, .share, .recommend, .related"):
                                noise.decompose()
                            detail_text = " ".join(main.get_text(" ", strip=True).split())[:3500]
                        h = detail_soup.select_one("h1")
                        if h and h.get_text(" ", strip=True):
                            candidate_title = h.get_text(" ", strip=True)
                            if len(candidate_title) >= 5 and not re.search(r"官网|官方网站|首页|网站首页", candidate_title):
                                detail_title = candidate_title[:180]
                    except Exception as detail_error:
                        detail_text = context + " [详情页读取失败: " + str(detail_error)[:160] + "]"
                    # 清理日服全站固定展示的联动横幅，避免污染摘要与证据。
                    if game.get("id") == "G002":
                        detail_text = re.sub(r"(?:荒野行動[ｘx×]BLEACHコラボ\s*)+", "", detail_text).strip()
                    combined = detail_title + " " + detail_text
                    # 日服分类只依据公告标题，避免站点模板污染。
                    classification_text = detail_title if game.get("id") == "G002" else combined
                    author_match = re.search(r"作者：(.+?) 发布时间：", context or "")
                    author = author_match.group(1).strip() if author_match else ""
                    published, start_date, end_date = extract_dates(detail_soup or soup, combined)
                    # Many official game news URLs embed their publication date as /YYYYMMDD/.
                    if not published:
                        url_date = re.search(r"/(20\d{2})(\d{2})(\d{2})/", href)
                        if url_date:
                            try:
                                published = datetime(*map(int, url_date.groups()), tzinfo=timezone.utc).date().isoformat()
                            except ValueError:
                                pass
                    age_days = None
                    if published:
                        try:
                            age_days = (now().date() - datetime.fromisoformat(published[:10]).date()).days
                        except ValueError:
                            pass
                    category, subcategory = classify(classification_text[:1800], detail_title)
                    channel = classify_channel(source, detail_title, detail_text, author, detail_url)
                    # 玩家帖子作为舆论信息单独保留，不参与运营/版本/商业化等官方动作分类。
                    if channel == "舆论信息":
                        category, subcategory = "待核验", "玩家讨论/反馈"
                    elif channel == "官方动作" and game.get("id") == "G006" and re.search(r"开测|首测|二测|测试资格|招募|报名|预下载", detail_title):
                        category, subcategory = "运营活动", "限时运营活动"
                    item = {
                        "id": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
                        "game_id": game["id"], "game_name": game["name"],
                        "title": detail_title, "summary": detail_text[:900],
                        "published_at": published, "start_date": start_date, "end_date": end_date,
                        "age_days": age_days,
                        "historical_candidate": bool(age_days is not None and age_days > 180),
                        "category": category,
                        "subcategory": subcategory,
                        "channel": channel,
                        "author": author or None,
                        "commercial_mechanism": None, "price": None,
                        "source_type": source["type"], "source_label": source["label"],
                        "url": detail_url, "evidence": detail_text[:1200],
                        "confidence": 0.72 if detail_soup else 0.45,
                        "review_status": "疑似历史信息，待核验" if age_days is not None and age_days > 180 else "待人工核验",
                        "discovered_at": iso()
                    }
                    if normalized in existing_by_url:
                        previous = existing_by_url[normalized]
                        for key, value in item.items():
                            if value is not None and value != "":
                                previous[key] = value
                        status["refreshed_items"] += 1
                    else:
                        additions.append(item)
                        existing_by_url[normalized] = item
                        known.add(normalized)
                        status["new_items"] += 1
                    time.sleep(0.5)
                status["status"] = "success"
            except Exception as exc:
                status["status"] = "failed"
                status["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            statuses.append(status)
            time.sleep(1)

    # 历史记录口径回填：修正此前已入库记录，不依赖源站本轮再次发现。
    for record in items + additions:
        record_url = record.get("url", "") or ""
        record_title = record.get("title", "") or ""
        record_summary = record.get("summary", "") or ""
        if record.get("source_label") == "TapTap官方公告" or "/activities/lottery/" in record_url:
            record["channel"] = "官方动作"
        if record.get("channel") == "舆论信息":
            record["category"] = "待核验"
            record["subcategory"] = "玩家讨论/反馈"
        if record.get("game_id") == "G006" and record.get("channel") == "官方动作":
            if re.search(r"封禁|作弊|反外挂|违规账号|封禁名单", record_title):
                record["category"] = "游戏治理"
                record["subcategory"] = "反外挂/封禁"
            elif re.search(r"首测结束|测试结束|关服公告", record_title):
                record["category"] = "待核验"
                record["subcategory"] = "未归类官方资讯"
            elif re.search(r"开测|首测|二测|测试资格|招募|报名|预下载", record_title):
                record["category"] = "运营活动"
                record["subcategory"] = "限时运营活动"
        if record.get("category") == "官方资讯":
            record["category"] = "待核验"
            record["subcategory"] = "未归类官方资讯"
        # Normalize legacy publication formats and backfill explicit activity periods.
        existing_published = record.get("published_at")
        normalized_published = normalize_published(existing_published)
        if normalized_published:
            record["published_at"] = normalized_published
        elif existing_published:
            record["published_at"] = None
        if not record.get("published_at"):
            card_time = re.search(
                r"发布时间\\s*[：:]\\s*((?:20\\d{2})[年./-]\\s?\\d{1,2}[月./-]\\s?\\d{1,2}日?(?:\\s+\\d{1,2}:\\d{2}(?::\\d{2})?)?)",
                record_summary or record.get("evidence", "")
            )
            if card_time:
                record["published_at"] = normalize_published(card_time.group(1))
        if record.get("published_at"):
            record["age_days"] = (now().date() - datetime.fromisoformat(record["published_at"]).date()).days
            record["historical_candidate"] = record["age_days"] > 180
            if record["historical_candidate"]:
                record["review_status"] = "疑似历史信息，待核验"
        stored_text = " ".join([str(record.get("title") or ""), str(record.get("summary") or ""), str(record.get("evidence") or "")])
        _, extracted_start, extracted_end = extract_dates(None, stored_text)
        if extracted_start:
            record["start_date"], record["end_date"] = extracted_start, extracted_end
        # Fill missing information attributes conservatively.
        if not record.get("channel"):
            if record.get("source_label") == "TapTap官方公告" or "/activities/lottery/" in record_url:
                record["channel"] = "官方动作"
            elif record.get("source_type") not in ("官方社区", "社区论坛"):
                record["channel"] = "官方动作"
            elif re.search(r"官方版主|官方账号|官方运营|官方人员|ECC组委会|运营团队", str(record.get("author") or ""), re.I):
                record["channel"] = "官方动作"
            elif record.get("author"):
                record["channel"] = "舆论信息"
            else:
                record["channel"] = "待核验"


    data["updated_at"] = iso()
    data["items"] = additions + items
    DISCOVERY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATUS_FILE.write_text(json.dumps({"updated_at": iso(), "sources": statuses}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Sources checked: {}; successful: {}; failed: {}; detail pages read: {}; new candidates: {}; existing records refreshed: {}".format(
        len(statuses), sum(s["status"] == "success" for s in statuses),
        sum(s["status"] == "failed" for s in statuses),
        sum(s["details_read"] for s in statuses), len(additions),
        sum(s["refreshed_items"] for s in statuses)))
    for s in statuses:
        print("{} | {} | {} | candidates={} | details={} | new={} | {}".format(
            s["game_id"], s["source_label"], s["status"], s["candidate_links"],
            s["details_read"], s["new_items"], s["error"] or ""))

if __name__ == "__main__":
    main()
