#!/usr/bin/env python3
"""
Trade Feed: one clean feed of the trade stories you actually want.

Each run (GitHub Actions runs it every 15 minutes):
  1. reads the RSS feeds listed in config.yml
  2. sends headlines it hasn't seen before to Claude Haiku, which keeps only the ones
     that match the plain-English brief in criteria.md and tags each with a category
  3. merges reports of the same news from different outlets into one story
  4. writes docs/feed.xml (for any RSS reader), docs/index.html (a page for your phone)
     and the month-by-month archive in docs/archive/ (browsable at archive.html)
  5. if backfill_days is set, spends a few minutes filling the archive with older stories
     listed in each site's sitemap (the list of articles sites publish for search engines)

state.json remembers every article already checked, so nothing is classified twice.
backfill.json tracks how far the backfill has got.
"""

from __future__ import annotations

import bisect
import calendar
import difflib
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import unicodedata
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from string import Template
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.sax.saxutils import escape as _xml_escape
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ARCHIVE_DIR = DOCS / "archive"
STATE_FILE = ROOT / "state.json"
BACKFILL_FILE = ROOT / "backfill.json"

API_URL = "https://api.anthropic.com/v1/messages"
BATCH_SIZE = 40          # headlines per Claude request
MAX_BATCHES = 15         # requests per run at most; any backlog is finished on later runs
RECHECK_HOURS = 48       # editing criteria.md re-checks articles first seen this recently
SEEN_DAYS = 14           # rejected articles are remembered this long, so they're never re-checked
KEEP_DAYS = 30           # kept stories stay in state this long (the archive keeps them for good)
MAX_KEPT = 1500          # hard cap on kept articles held in state
PAGE_DAYS = 7            # the front page shows this many days of stories
MAX_AGE_DAYS = 4         # articles already older than this when first seen are skipped
DUPE_HOURS = 96          # reports of the same news this close together become one story
MAX_ATTEMPTS = 3         # an article Claude can't answer usably about is dropped after this
STALE_HOURS = 48         # a feed with nothing new for this long is flagged on the page
MAX_PAGES = 3            # if every article in a feed is new, also read ?paged=2 and 3
FILTERED_HOURS = 24      # the page lists what was filtered out in this window
DETAILS = ("category", "project", "people", "company", "based_on", "tags")

BACKFILL_SECONDS = 600   # time each run may spend filling in the archive
BACKFILL_WORKERS = 4     # sitemaps worked on at once
PAST_BATCH = 100         # older articles per Claude request (they're just a few words each)
ITEM_BATCHES = 10        # requests per sitemap per turn; huge sitemaps carry on next turn
SITE_GAP = 1.0           # seconds between requests to the same site
PLAN_FETCHES = 60        # sitemap-index requests per site while planning
ROBOT = "TradeFeed"      # the name checked against robots.txt rules
PHOTO_FETCHES = 40       # article pages read per run to find photos for kept stories without one

# Category colours on the page (light, dark), assigned in config.yml order.
PALETTE = [("#2563eb", "#7aa7ff"), ("#7c3aed", "#b794ff"), ("#15803d", "#5fd08b"),
           ("#b45309", "#f2a65a"), ("#be185d", "#ff7ab6"), ("#0e7490", "#4fd1e8"),
           ("#4d7c0f", "#a3d977"), ("#9f1239", "#ff8fa3"), ("#6d28d9", "#c4a5ff"),
           ("#475569", "#a7b4c6"), ("#c2410c", "#ff9a66"), ("#0f766e", "#5eead4")]


class FetchError(Exception):
    """A feed or sitemap couldn't be read this run."""


class ApiProblem(Exception):
    """Claude couldn't be used this run; waiting articles are tried again next run."""

    def __init__(self, message: str, urgent: bool = False):
        super().__init__(message)
        self.urgent = urgent  # urgent problems (bad key, no credit) alert the owner at once


class BadAnswer(Exception):
    """Claude answered, but not usably, for one batch."""


class TooLong(BadAnswer):
    """The answer was cut off; the batch gets split and retried."""


# ------------------------------------------------------------------ settings & state

def load_config() -> dict:
    cfg = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8")) or {}
    cfg.setdefault("title", "Trade Feed")
    cfg.setdefault("description", "Adaptations, series orders, greenlights and casting news.")
    cfg.setdefault("timezone", "UTC")
    cfg.setdefault("model", "claude-haiku-5-5")
    cfg.setdefault("effort", "low")
    cfg.setdefault("feed_items", 150)
    cfg.setdefault("site_url", "")
    cfg["archive_months"] = max(0, int(cfg.get("archive_months") or 0))
    cfg["backfill_days"] = max(0, min(3650, int(cfg.get("backfill_days") or 0)))
    prices = cfg.get("prices") or {}
    cfg["prices"] = {"input": float(prices.get("input", 0.10)), "output": float(prices.get("output", 0.50))}
    try:
        cfg["tz"] = ZoneInfo(str(cfg["timezone"]))
    except Exception:
        sys.exit(f"config.yml: unknown timezone {cfg['timezone']!r} (use a name like America/New_York)")
    cats = {}
    for key, val in (cfg.get("categories") or {}).items():
        val = val or {}
        cats[str(key).strip().lower()] = {"emoji": str(val.get("emoji", "•")),
                                          "label": str(val.get("label", key))}
    if not cats:
        sys.exit("config.yml: add at least one category")
    cfg["categories"] = cats
    tags = {}
    for key, val in (cfg.get("tags") or {}).items():
        val = val or {}
        key = str(key).strip().lower()
        if key in cats or key == "all":
            sys.exit(f"config.yml: tag {key!r} has the same name as a category; rename one")
        tags[key] = {"emoji": str(val.get("emoji", "•")), "label": str(val.get("label", key))}
    cfg["tags"] = tags
    feeds = []
    for f in cfg.get("feeds") or []:
        if f and f.get("url"):
            url = str(f["url"]).strip()
            feeds.append({"url": url, "name": str(f.get("name") or urlsplit(url).hostname)})
    if not feeds:
        sys.exit("config.yml: add at least one feed")
    cfg["feeds"] = feeds
    return cfg


def load_brief() -> str:
    """criteria.md minus <!-- comments -->, which are notes for you, not for Claude."""
    text = (ROOT / "criteria.md").read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("meta", {})
        data.setdefault("items", {})
        return data
    return {"meta": {}, "items": {}}


def dump_state(state: dict) -> str:
    """One article per line, so each commit's diff stays small."""
    items = sorted(state["items"].values(), key=lambda it: (it.get("first_seen", 0), it["id"]))
    lines = ["{", f'"meta": {json.dumps(state["meta"], sort_keys=True, ensure_ascii=False)},', '"items": {']
    for i, it in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f'{json.dumps(it["id"])}: {json.dumps(it, sort_keys=True, ensure_ascii=False)}{comma}')
    lines += ["}", "}"]
    return "\n".join(lines) + "\n"


def write_if_changed(path: Path, text: str) -> bool:
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


# ------------------------------------------------------------------ text helpers

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TAGS = re.compile(r"<[^>]+>")
WP_FOOTER = re.compile(r"\s*The post .*? appeared first on [^.]*\.?\s*$", re.S | re.I)
READ_MORE = re.compile(r"\s*(\[(…|\.\.\.|&hellip;)\]|(Continue reading|Read more)\b.*)$", re.I)


def clean_text(s) -> str:
    s = TAGS.sub(" ", str(s or ""))
    s = html.unescape(s)
    s = CONTROL_CHARS.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_summary(s, limit: int = 280) -> str:
    s = clean_text(s)
    s = WP_FOOTER.sub("", s)
    s = READ_MORE.sub("", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:—–-") + "…"
    return s


def clean_link(url) -> str:
    """Absolute http(s) links only, minus tracking parameters and #fragments."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    query = "&".join(p for p in parts.query.split("&")
                     if p and not p.lower().startswith(("utm_", "fbclid=", "gclid=")))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def article_id(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return hashlib.sha1((host + parts.path.rstrip("/").lower()).encode()).hexdigest()[:16]


def item_time(it: dict) -> int:
    return int(it.get("published") or it.get("first_seen") or 0)


def x(s) -> str:
    """Escape for XML text and attributes."""
    return _xml_escape(str(s), {'"': "&quot;"})


def h(s) -> str:
    """Escape for HTML text and attributes."""
    return html.escape(str(s), quote=True)


# ------------------------------------------------------------------ reading feeds

def user_agent() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    contact = f"; +https://github.com/{repo}" if repo else ""
    return f"Mozilla/5.0 (compatible; TradeFeed/1.0; personal RSS reader{contact})"


def fetch_bytes(url: str) -> bytes:
    headers = {"User-Agent": user_agent(),
               "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise FetchError(f"couldn't connect ({type(e).__name__})") from None
    if "tollbit" in (urlsplit(r.url).hostname or ""):
        raise FetchError("the site sent the request to its paid bot gateway")
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code}")
    return r.content


def parse_feed(raw: bytes, feed: dict) -> list[dict]:
    parsed = feedparser.parse(raw)
    if not parsed.entries:
        if parsed.bozo:
            raise FetchError("the response isn't a valid RSS feed")
        return []
    articles = []
    for e in parsed.entries:
        link = clean_link(e.get("link")) or clean_link(e.get("id"))
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        articles.append({
            "id": article_id(link),
            "outlet": feed["name"],
            "feed": feed["url"],
            "title": title,
            "link": link,
            "summary": clean_summary(e.get("summary") or e.get("description")),
            "published": calendar.timegm(stamp) if stamp else None,
            "image": feed_image(e),
        })
    return articles


IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)", re.I)


def feed_image(e) -> str:
    """The article's photo as the feed gives it: media tags, an image enclosure, or the first <img>."""
    for media in (e.get("media_content") or []) + (e.get("media_thumbnail") or []):
        if media.get("medium", "image") == "image" and clean_link(html.unescape(media.get("url") or "")):
            return clean_link(html.unescape(media["url"]))
    for enc in e.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image/") and clean_link(enc.get("href")):
            return clean_link(enc["href"])
    for part in [c.get("value", "") for c in e.get("content") or []] + [e.get("summary") or ""]:
        found = IMG_SRC.search(part)
        if found and clean_link(html.unescape(found.group(1))):
            return clean_link(html.unescape(found.group(1)))
    return ""


def page_url(url: str, n: int) -> str:
    return f"{url}{'&' if '?' in url else '?'}paged={n}"


def read_feed(feed: dict, is_known, first_time: bool) -> tuple[list[dict], bool]:
    """A feed's articles, plus whether some may have scrolled off before we saw them.

    Feeds only list their latest ~10 articles. If none of them is one we already know,
    articles may have been missed, so older pages are read too (WordPress ?paged=N)."""
    articles = parse_feed(fetch_bytes(feed["url"]), feed)
    if first_time or not articles:
        return articles, False
    seen_ids = {a["id"] for a in articles}
    page = 1
    while page < MAX_PAGES and not any(is_known(a) for a in articles):
        page += 1
        try:
            more = [a for a in parse_feed(fetch_bytes(page_url(feed["url"], page)), feed)
                    if a["id"] not in seen_ids]
        except FetchError:
            break
        if not more:
            break
        articles += more
        seen_ids.update(a["id"] for a in more)
    return articles, not any(is_known(a) for a in articles)


def feed_labels(feeds: list[dict]) -> dict[str, str]:
    """'TheWrap (movies)' style labels when several feeds share an outlet name."""
    counts: dict[str, int] = {}
    for f in feeds:
        counts[f["name"]] = counts.get(f["name"], 0) + 1
    labels = {}
    for f in feeds:
        parts = [p for p in urlsplit(f["url"]).path.split("/") if p and p != "feed"]
        if counts[f["name"]] > 1 and parts:
            labels[f["url"]] = f"{f['name']} ({parts[-1].replace('-', ' ')})"
        else:
            labels[f["url"]] = f["name"]
    return labels


def intake(articles: list[dict], items: dict, now: float) -> int:
    added = 0
    for a in articles:
        if a["id"] in items:
            continue
        if a["published"] and a["published"] < now - MAX_AGE_DAYS * 86400:
            continue  # old news: not worth checking
        if a["published"] and a["published"] > now + 3600:
            a["published"] = int(now)  # a clock in the future; don't let it sit on top forever
        a.update(status="pending", attempts=0, first_seen=int(now))
        items[a["id"]] = a
        added += 1
    return added


# ------------------------------------------------------------------ asking Claude

def category_keys(cats: dict) -> str:
    return "\n".join(f"- {k} ({v['label']})" for k, v in cats.items())


def tag_lines(tags: dict) -> str:
    if not tags:
        return ""
    return f"\n- tags: which of the extra tags in the brief apply ({', '.join(tags)}); [] if none"


FIELDS = """- id: the article's number
- category: the matching category key
- project: the film or show's title as written, without quotes; "" if it's untitled or unclear
- people: the people the news is about, such as the actors cast; [] if none
- company: the network, streamer or studio involved; "" if none
- based_on: for adaptations, the source material, worded to follow "Based on" (for example "the novel by Emily Henry"); otherwise \"\""""


def system_prompt(brief: str, cats: dict, tags: dict) -> str:
    return f"""You filter Hollywood trade news for one reader. You'll get a numbered list of new articles from outlets such as Deadline, Variety, The Hollywood Reporter, TheWrap and IndieWire. Each article is a line "[number] outlet: headline", usually followed by an indented line with its one-sentence summary.

Decide which articles belong in the reader's feed by following their brief exactly:

<brief>
{brief}
</brief>

Category keys:
{category_keys(cats)}

Put every article whose main news fits a category in "kept", with:
{FIELDS}{tag_lines(tags)}

Put the numbers of all other articles in "rejected". Judge each article by its main news, mostly from the headline, using the summary to clarify. Every number goes in exactly one list."""


def past_prompt(brief: str, cats: dict, tags: dict) -> str:
    return f"""You sort older Hollywood trade news into one reader's archive. You'll get a numbered list of articles from outlets such as Deadline, Variety, The Hollywood Reporter, TheWrap and IndieWire. All that's known about each one is the wording of its web address, given as "[number] outlet · date · site section: words from the address".

Decide which articles belong in the archive by following the reader's brief exactly:

<brief>
{brief}
</brief>

Category keys:
{category_keys(cats)}

Put every article whose main news clearly fits a category in "kept", with:
{FIELDS}{tag_lines(tags)}
- headline: a readable headline made only from the address words: restore capitals, punctuation and quotation marks around titles, but add nothing that isn't in the words

Put the numbers of all other articles in "rejected". Address words are often shortened, so keep an article only when they make its main news clear. Every number goes in exactly one list."""


def output_schema(cats: dict, tags: dict, headline: bool = False) -> dict:
    fields = {
        "id": {"type": "integer"},
        "category": {"type": "string", "enum": list(cats)},
        "project": {"type": "string"},
        "people": {"type": "array", "items": {"type": "string"}},
        "company": {"type": "string"},
        "based_on": {"type": "string"},
    }
    if tags:
        fields["tags"] = {"type": "array", "items": {"type": "string", "enum": list(tags)}}
    if headline:
        fields["headline"] = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "kept": {"type": "array", "items": {
                "type": "object",
                "properties": fields,
                "required": list(fields),
                "additionalProperties": False,
            }},
            "rejected": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["kept", "rejected"],
        "additionalProperties": False,
    }


def api_error_text(r: requests.Response) -> str:
    try:
        return str(r.json()["error"]["message"])[:200]
    except Exception:
        return r.text[:200].strip()


def call_claude(body: dict, key: str) -> dict:
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for attempt in range(3):
        last = attempt == 2
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=180)
        except requests.RequestException:
            if last:
                raise ApiProblem("couldn't reach the Claude API") from None
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                raise BadAnswer("the API sent back something unreadable") from None
        message = api_error_text(r)
        if r.status_code in (408, 429, 500, 502, 503, 504, 529):
            if last:
                raise ApiProblem(f"the Claude API is busy or having trouble ({r.status_code})")
            try:
                wait_for = float(r.headers.get("retry-after") or 0)
            except ValueError:
                wait_for = 0
            time.sleep(min(max(wait_for, 10 * (attempt + 1)), 60))
            continue
        if r.status_code == 401:
            raise ApiProblem("the API key was rejected; check the ANTHROPIC_API_KEY secret", urgent=True)
        if "credit" in message.lower():
            raise ApiProblem("your Claude API credit balance is too low; add credit in the Claude Console",
                             urgent=True)
        raise ApiProblem(f"the Claude API refused the request ({r.status_code}: {message})", urgent=True)
    raise ApiProblem("couldn't reach the Claude API")


def classify_batch(batch: list[dict], brief: str, cfg: dict, key: str, usage: dict,
                   past: bool = False) -> dict[str, dict]:
    """Returns {article id: details} for the articles Claude kept.

    past=True is for backfilled articles, known only by the words in their address."""
    numbered = dict(enumerate(batch, 1))
    lines = []
    for n, it in numbered.items():
        if past:
            day = datetime.fromtimestamp(it["published"], timezone.utc).strftime("%Y-%m-%d")
            section = f" · {it['section']}" if it.get("section") else ""
            lines.append(f"[{n}] {it['outlet']} · {day}{section}: {it['words']}")
        else:
            lines.append(f"[{n}] {it['outlet']}: {it['title']}")
            if it.get("summary"):
                lines.append(f"    {it['summary']}")
    output_config: dict = {"format": {"type": "json_schema",
                                      "schema": output_schema(cfg["categories"], cfg["tags"], headline=past)}}
    if cfg.get("effort"):
        output_config["effort"] = str(cfg["effort"])
    body = {
        "model": cfg["model"],
        "max_tokens": 16000,
        "system": (past_prompt if past else system_prompt)(brief, cfg["categories"], cfg["tags"]),
        "messages": [{"role": "user", "content": "\n".join(lines)}],
        "output_config": output_config,
    }
    data = call_claude(body, key)
    u = data.get("usage") or {}
    usage["input"] += sum(int(u.get(k) or 0) for k in
                          ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    usage["output"] += int(u.get("output_tokens") or 0)
    stop = data.get("stop_reason")
    if stop == "max_tokens":
        raise TooLong("the answer was cut off")
    if stop == "refusal":
        raise BadAnswer("Claude declined to sort this batch")
    text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    try:
        picks = json.loads(text)["kept"]
        assert isinstance(picks, list)
    except Exception:
        raise BadAnswer("Claude's answer couldn't be read") from None
    kept = {}
    for p in picks:
        if not isinstance(p, dict):
            continue
        it = numbered.get(p.get("id"))
        category = str(p.get("category", "")).strip().lower()
        if not it or category not in cfg["categories"]:
            continue
        people = p.get("people") if isinstance(p.get("people"), list) else []
        details = {
            "category": category,
            "project": str(p.get("project") or "").strip()[:120],
            "people": [str(n).strip()[:60] for n in people if str(n).strip()][:8],
            "company": str(p.get("company") or "").strip()[:60],
            "based_on": str(p.get("based_on") or "").strip()[:140],
            "tags": [t for t in cfg["tags"] if t in (p.get("tags") or [])],
        }
        if past:
            details["headline"] = clean_text(p.get("headline"))[:200]
        kept[it["id"]] = details
    return kept


def waiting(items: dict) -> list[dict]:
    return [it for it in items.values() if it["status"] == "pending" or it.get("recheck")]


def classify_waiting(items: dict, brief: str, cfg: dict, key: str, usage: dict, log) -> ApiProblem | None:
    # Newest first, and at most MAX_BATCHES per run, so a backlog (say, after the API key
    # was missing for a while) catches up over a few runs instead of timing one out.
    queue_items = sorted(waiting(items), key=item_time, reverse=True)[: BATCH_SIZE * MAX_BATCHES]
    queue = [queue_items[i:i + BATCH_SIZE] for i in range(0, len(queue_items), BATCH_SIZE)]
    while queue:
        batch = queue.pop(0)
        try:
            kept = classify_batch(batch, brief, cfg, key, usage)
        except TooLong:
            if len(batch) > 1:
                mid = len(batch) // 2
                queue[:0] = [batch[:mid], batch[mid:]]
                continue
            note_failure(batch)
            continue
        except BadAnswer as e:
            log(f"  ! {e}; those {len(batch)} articles will be retried next run")
            note_failure(batch)
            continue
        except ApiProblem as e:
            return e  # stop here; everything still waiting is tried next run
        for it in batch:
            it.pop("recheck", None)
            it.pop("attempts", None)
            if it["id"] in kept:
                it["status"] = "kept"
                it.update(kept[it["id"]])
            else:
                it["status"] = "rejected"
                for k in DETAILS:
                    it.pop(k, None)
    return None


def note_failure(batch: list[dict]) -> None:
    for it in batch:
        it["attempts"] = it.get("attempts", 0) + 1
        if it["attempts"] >= MAX_ATTEMPTS:
            it.pop("attempts", None)
            if it.pop("recheck", None):
                continue  # keep its earlier decision
            it["status"] = "error"


def criteria_signature(brief: str, cfg: dict) -> str:
    blob = json.dumps({"brief": brief, "categories": cfg["categories"], "tags": cfg["tags"], "model": cfg["model"]}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def mark_recheck(items: dict, meta: dict, signature: str, now: float, forced: bool) -> int:
    """When the brief changes, recent articles are judged again (their old decision stands meanwhile)."""
    previous = meta.get("criteria")
    meta["criteria"] = signature
    if not forced and (previous is None or previous == signature):
        return 0
    count = 0
    for it in items.values():
        if it["status"] in ("kept", "rejected", "error") and it.get("title") \
                and it.get("first_seen", 0) >= now - RECHECK_HOURS * 3600:
            it["recheck"] = True
            it["attempts"] = 0
            count += 1
    return count


# ------------------------------------------------------------------ housekeeping

def prune(items: dict, now: float) -> None:
    for id_, it in list(items.items()):
        age = now - it.get("first_seen", now)
        status = it["status"]
        if status == "kept":
            if age > KEEP_DAYS * 86400:
                del items[id_]
        elif age > SEEN_DAYS * 86400:
            del items[id_]
        elif status in ("rejected", "error") and age > RECHECK_HOURS * 3600 and not it.get("recheck"):
            items[id_] = {"id": id_, "status": status, "first_seen": it["first_seen"]}  # just remember the id
    kept = sorted((it for it in items.values() if it["status"] == "kept"), key=lambda it: it["first_seen"])
    for it in kept[:-MAX_KEPT]:
        del items[it["id"]]


def record_api(meta: dict, problem: ApiProblem | None, attempted: bool, now: float) -> int:
    """Remembers API trouble for the page banner. Returns 1 the first time a problem needs
    the owner's attention (GitHub then emails them once), otherwise 0."""
    if problem is None:
        if attempted:
            meta.pop("api", None)
        return 0
    api = meta.get("api") or {"since": int(now)}
    api["error"] = str(problem)
    meta["api"] = api
    if (problem.urgent or now - api["since"] >= 3600) and not api.get("alerted"):
        api["alerted"] = True
        return 1
    return 0


def record_usage(meta: dict, usage: dict, now: float) -> None:
    if not usage["input"] and not usage["output"]:
        return
    month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    book = meta.setdefault("usage", {})
    entry = book.setdefault(month, {"input": 0, "output": 0})
    entry["input"] += usage["input"]
    entry["output"] += usage["output"]
    for old in sorted(book)[:-3]:
        del book[old]


# ------------------------------------------------------------------ merging duplicates

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^(the|a|an) ", "", s)


def surnames(people) -> set[str]:
    out = set()
    for person in people or []:
        parts = [p for p in norm(person).split() if p not in SUFFIXES]
        if parts:
            out.add(parts[-1])
    return out


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def same_news(a: dict, b: dict) -> bool:
    if a.get("category") != b.get("category"):
        return False
    pa, pb = (p if not p.startswith("untitled") else "" for p in (norm(a.get("project")), norm(b.get("project"))))
    project = similar(pa, pb)
    shared_people = bool(surnames(a.get("people")) & surnames(b.get("people")))
    if a["category"] in ("casting", "creative"):
        return shared_people and (project >= 0.8 or not (pa and pb))
    if pa and pb:
        return project >= 0.85
    same_company = bool(norm(a.get("company"))) and norm(a.get("company")) == norm(b.get("company"))
    return similar(norm(a["title"]), norm(b["title"])) >= 0.6 or (shared_people and same_company)


def build_stories(items: dict) -> list[dict]:
    kept = sorted((it for it in items.values() if it["status"] == "kept"), key=lambda it: (item_time(it), it["id"]))
    stories: list[dict] = []
    for it in kept:
        t = item_time(it)
        match = None
        for story in reversed(stories):
            if t - item_time(story["main"]) > DUPE_HOURS * 3600:
                break
            if same_news(story["main"], it) or any(same_news(o, it) for o in story["also"]):
                match = story
                break
        if match is None:
            stories.append({"main": it, "also": [], "merged": []})
            continue
        match["merged"].append(it)
        if it["outlet"] != match["main"]["outlet"] and all(o["outlet"] != it["outlet"] for o in match["also"]):
            match["also"].append(it)
    stories.sort(key=lambda s: (item_time(s["main"]), s["main"]["id"]), reverse=True)
    return stories


def story_photo(story: dict) -> str:
    """The lead report's photo, or failing that one from another outlet's report."""
    return next((it["image"] for it in [story["main"], *story["merged"]] if it.get("image")), "")


def story_tags(story: dict) -> list[str]:
    """Extra tags from any report of the story, in the order they first appear."""
    out: list[str] = []
    for it in [story["main"], *story["merged"]]:
        out += [t for t in it.get("tags") or [] if t not in out]
    return out


def tag_spans(keys: list[str], tags: dict) -> str:
    return "".join(f'<span class="xtag">{h(tags[k]["emoji"])} {h(tags[k]["label"])}</span>' for k in keys if k in tags)


def photo_html(story: dict, link: str) -> str:
    photo = story_photo(story)
    if not photo:
        return ""
    return (f'<a class="photo" href="{h(link)}" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true">'
            f'<img src="{h(photo)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" '
            f'onerror="this.parentNode.remove()"></a>')


# ------------------------------------------------------------------ the archive

def month_shift(month: str, delta: int) -> str:
    year, mon = map(int, month.split("-"))
    total = year * 12 + mon - 1 + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


class Archive:
    """Every kept story, one file per month: docs/archive/YYYY-MM.json (+ index.json)."""

    def __init__(self, tz):
        self.tz = tz
        self.months: dict[str, dict] = {}
        self.timeline: dict[str, list] = {}
        self.dirty: set[str] = set()

    def month_of(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m")

    def around(self, ts: float) -> set[str]:
        return {self.month_of(ts + d * 3600) for d in (-DUPE_HOURS, 0, DUPE_HOURS)}

    def load(self, month: str) -> dict:
        if month not in self.months:
            path = ARCHIVE_DIR / f"{month}.json"
            entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            self.months[month] = {e["id"]: e for e in entries}
        return self.months[month]

    def put(self, entry: dict) -> None:
        month = self.month_of(entry["time"])
        self.load(month)[entry["id"]] = entry
        self.timeline.pop(month, None)
        self.dirty.add(month)

    def touch(self, entry: dict) -> None:
        self.dirty.add(self.month_of(entry["time"]))

    def remove(self, id_: str, ts: float) -> None:
        for month in self.around(ts):
            if self.load(month).pop(id_, None) is not None:
                self.timeline.pop(month, None)
                self.dirty.add(month)

    def near(self, ts: float):
        """Stories within DUPE_HOURS of ts."""
        for month in self.around(ts):
            if month not in self.timeline:
                self.timeline[month] = sorted((e["time"], e["id"]) for e in self.load(month).values())
            line = self.timeline[month]
            lo = bisect.bisect_left(line, (ts - DUPE_HOURS * 3600, ""))
            hi = bisect.bisect_right(line, (ts + DUPE_HOURS * 3600, "~"))
            for _, id_ in line[lo:hi]:
                yield self.months[month][id_]

    def add(self, entry: dict) -> bool:
        """Adds a backfilled story, or merges it into a story already there. True if new."""
        people, project = surnames(entry.get("people")), norm(entry.get("project"))
        words = set(re.findall(r"[a-z0-9]{4,}", norm(entry["title"])))
        for other in self.near(entry["time"]):
            if other["id"] == entry["id"]:
                return False
            if other.get("category") != entry["category"]:
                continue
            if not (people & surnames(other.get("people")) or (project and project == norm(other.get("project")))
                    or len(words & set(re.findall(r"[a-z0-9]{4,}", norm(other["title"])))) >= 3):
                continue
            if same_news(other, entry):
                if entry["time"] < other["time"] and other.get("rebuilt"):
                    # the earlier report leads, as on the live feed
                    also = [{"outlet": other["outlet"], "link": other["link"]}] + other.get("also", [])
                    entry = {**entry, "also": [a for a in also if a["outlet"] != entry["outlet"]]}
                    self.remove(other["id"], other["time"])
                    self.put(entry)
                    return False
                outlets = {other["outlet"]} | {a["outlet"] for a in other.get("also", [])}
                if entry["outlet"] not in outlets:
                    other.setdefault("also", []).append({"outlet": entry["outlet"], "link": entry["link"]})
                    self.touch(other)
                return False
        self.put(entry)
        return True

    def save(self, keep_months: int, now: float) -> dict[str, int]:
        index_path = ARCHIVE_DIR / "index.json"
        counts = json.loads(index_path.read_text(encoding="utf-8")).get("months", {}) if index_path.exists() else {}
        for month in sorted(self.dirty):
            entries = sorted(self.months[month].values(), key=lambda e: (e["time"], e["id"]), reverse=True)
            path = ARCHIVE_DIR / f"{month}.json"
            if entries:
                write_if_changed(path, "[\n" + ",\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True)
                                                         for e in entries) + "\n]\n")
                counts[month] = len(entries)
            else:
                path.unlink(missing_ok=True)
                counts.pop(month, None)
        if keep_months:
            oldest = month_shift(self.month_of(now), -(keep_months - 1))
            for month in [m for m in counts if m < oldest]:
                (ARCHIVE_DIR / f"{month}.json").unlink(missing_ok=True)
                del counts[month]
        counts = dict(sorted(counts.items(), reverse=True))
        write_if_changed(index_path, json.dumps({"months": counts}, indent=1) + "\n")
        return counts


def entry_from_story(story: dict) -> dict:
    m = story["main"]
    entry = {"id": m["id"], "time": item_time(m), "category": m["category"], "title": m["title"],
             "link": m["link"], "outlet": m["outlet"]}
    for k in ("summary", "based_on", "project", "company", "people"):
        if m.get(k):
            entry[k] = m[k]
    if story_tags(story):
        entry["tags"] = story_tags(story)
    if story_photo(story):
        entry["image"] = story_photo(story)
    if story["also"]:
        entry["also"] = [{"outlet": o["outlet"], "link": o["link"]} for o in story["also"]]
    return entry


def sync_archive(archive: Archive, stories: list[dict], items: dict) -> None:
    """Copies the stories still held in state into the archive, as they currently stand."""
    for story in stories:
        archive.put(entry_from_story(story))
        for other in story["merged"]:
            archive.remove(other["id"], item_time(other))
    for it in items.values():
        if it["status"] in ("rejected", "error") and it.get("title"):
            archive.remove(it["id"], item_time(it))


# ------------------------------------------------------------------ backfilling from sitemaps

SITEMAP_DATES = [
    re.compile(r"yyyy=(\d{4})&mm=(\d{1,2})(?:&dd=(\d{1,2}))?"),
    re.compile(r"/(\d{4})/(\d{2})(?:/(\d{2}))?(?=[/.?]|$)"),
    re.compile(r"(?<![\d])(\d{4})[-_](\d{2})(?:[-_](\d{2}))?(?![\d])"),
    re.compile(r"sitemap[-_]?(\d{4})(\d{2})(\d{2})?(?![\d])"),
]
SKIP_SITEMAP = re.compile(r"news-sitemap|duration-|page-sitemap|category|tag|author|attachment|video|image|"
                          r"galler|list|vertical|topic|taxonom|person|people|event|sponsor|shop", re.I)
SKIP_PATH = re.compile(r"/(tag|tags|t|c|v|author|authors|category|categories|video|videos|gallery|galleries|"
                       r"photos|pictures|lists|list|shop|shopping|feed|page|search)/", re.I)
LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.S | re.I)
LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)
BLOCK = re.compile(r"<(sitemap|url)\b[^>]*>(.*?)</\1>", re.S | re.I)


def parse_iso(s: str) -> int | None:
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse_sitemap(raw: bytes) -> tuple[str, list[tuple[str, int | None]]]:
    if raw[:2] == b"\x1f\x8b":
        raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, 80_000_000)
    text = raw.decode("utf-8", "replace")
    if re.search(r"<sitemapindex\b", text, re.I):
        kind = "index"
    elif re.search(r"<urlset\b", text, re.I):
        kind = "urlset"
    else:
        raise FetchError("not a sitemap")
    entries = []
    for _, body in BLOCK.findall(text):
        loc = LOC.search(body)
        if loc:
            lastmod = LASTMOD.search(body)
            entries.append((html.unescape(loc.group(1)), parse_iso(lastmod.group(1)) if lastmod else None))
    return kind, entries


def sitemap_span(url: str) -> tuple[int, int] | None:
    """The dates a sitemap covers, when its address says (daily or monthly sitemaps)."""
    for pattern in SITEMAP_DATES:
        m = pattern.search(url)
        if not m:
            continue
        year, month, day = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (1995 <= year <= 2100 and 1 <= month <= 12):
            continue
        try:
            if day:
                start = datetime(year, month, int(day), tzinfo=timezone.utc)
                return int(start.timestamp()), int(start.timestamp()) + 86400
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            nxt = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
            return int(start.timestamp()), int(nxt.timestamp())
        except ValueError:
            continue
    return None


def slug_words(link: str) -> tuple[str, str]:
    """('mister miracle adds josh hutcherson', 'creative-content/tv-shows') from an article address."""
    parts = [p for p in urlsplit(link).path.split("/") if p]
    if not parts:
        return "", ""
    slug = re.sub(r"\.(html?|php)$", "", parts[-1])
    slug = re.sub(r"-\d{5,}$", "", slug)  # trailing post ids
    words = re.sub(r"[-_+]+", " ", unquote(slug)).strip()
    section = "/".join(p for p in parts[:-1] if not re.fullmatch(r"\d{1,4}", p))
    return words, section


def blocked(error: str) -> bool:
    return "bot gateway" in error or error in ("HTTP 401", "HTTP 403", "HTTP 429", "HTTP 451")


def transient(error: str) -> bool:
    """Worth trying again later: the site was unreachable or briefly broken."""
    return error.startswith("couldn't connect") or bool(re.fullmatch(r"HTTP 5\d\d", error))


class Polite:
    """Fetches with at least SITE_GAP seconds between requests to the same site."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next_at: dict[str, float] = {}

    def get(self, url: str) -> bytes:
        host = urlsplit(url).netloc.lower()
        with self.lock:
            now = time.time()
            start = max(now, self.next_at.get(host, 0))
            self.next_at[host] = start + SITE_GAP
        if start > now:
            time.sleep(start - now)
        return fetch_bytes(url)


META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
META_IMAGE = re.compile(r"""(?:property|name)\s*=\s*["'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["']""", re.I)
META_CONTENT = re.compile(r"""\bcontent\s*=\s*["']([^"']+)""", re.I)


def page_photo(raw: bytes) -> str:
    """The photo an article page offers for link previews (og:image / twitter:image)."""
    head = raw[:2_000_000].decode("utf-8", "replace")  # THR's tag sits ~400KB in, after inline styles
    head = head.split("</head>", 1)[0]
    for tag in META_TAG.findall(head):
        if META_IMAGE.search(tag):
            found = META_CONTENT.search(tag)
            if found and clean_link(html.unescape(found.group(1))):
                return clean_link(html.unescape(found.group(1)))
    return ""


def find_photos(items: dict, log) -> int:
    """Reads the article page of kept stories whose feed had no photo (THR and IndieWire
    don't include one), at most PHOTO_FETCHES a run, newest first, honouring robots.txt."""
    todo = sorted((it for it in items.values()
                   if it["status"] == "kept" and not it.get("image") and not it.get("photo_tried")),
                  key=item_time, reverse=True)[:PHOTO_FETCHES]
    polite, robots, found = Polite(), {}, 0
    for it in todo:
        parts = urlsplit(it["link"])
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in robots:
            robots[root] = RobotFileParser()
            try:
                robots[root].parse(polite.get(root + "/robots.txt").decode("utf-8", "replace").splitlines())
            except FetchError:
                robots[root].parse([])
        it["photo_tried"] = True
        if not robots[root].can_fetch(ROBOT, it["link"]):
            continue
        try:
            it["image"] = page_photo(polite.get(it["link"]))
        except FetchError as e:
            if transient(str(e)):
                it.pop("photo_tried")  # try again next run
            continue
        found += bool(it["image"])
    if todo:
        log(f"Looked for photos on {len(todo)} article pages, found {found}.")
    return found


def backfill_sites(cfg: dict) -> list[dict]:
    sites: dict[str, dict] = {}
    for f in cfg["feeds"]:
        parts = urlsplit(f["url"])
        root = f"{parts.scheme}://{parts.netloc}"
        sites.setdefault(root, {"name": f["name"], "root": root, "host": parts.netloc.lower()})
    return list(sites.values())


def plan_site(site: dict, start: int, end: int, polite: Polite) -> tuple[list[dict], str, bool]:
    """Finds the site's sitemaps covering [start, end), honouring its robots.txt.
    Returns (sitemaps, note, whether to try planning again on a later run)."""
    try:
        robots_txt = polite.get(site["root"] + "/robots.txt").decode("utf-8", "replace")
    except FetchError as e:
        if blocked(str(e)):
            return [], f"blocked ({e})", False
        if transient(str(e)):
            return [], f"couldn't reach the site ({e}); will try again", True
        robots_txt = ""
    robots = RobotFileParser()
    robots.parse(robots_txt.splitlines())
    todo = [line.split(":", 1)[1].strip() for line in robots_txt.splitlines()
            if line.lower().startswith("sitemap:")]
    if not todo:
        todo = [site["root"] + p for p in ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml")]
    found, seen, fetches, problems = [], set(), 0, []
    while todo and fetches < PLAN_FETCHES:
        url = todo.pop(0)
        if url in seen or SKIP_SITEMAP.search(url) or urlsplit(url).netloc.lower() != site["host"]:
            continue
        seen.add(url)
        if not robots.can_fetch(ROBOT, url):
            problems.append("robots.txt asks not to")
            continue
        span = sitemap_span(url)
        if span:
            if span[1] > start and span[0] < end:
                found.append({"url": url, "site": site["name"], "start": max(span[0], start),
                              "end": min(span[1], end), "offset": 0})
            continue
        try:
            kind, entries = parse_sitemap(polite.get(url))
        except FetchError as e:
            if blocked(str(e)):
                return [], f"blocked ({e})", False
            problems.append(str(e))
            continue
        fetches += 1
        if kind == "index":
            todo += [loc for loc, lastmod in entries if not (lastmod and lastmod < start and not sitemap_span(loc))]
        else:
            found.append({"url": url, "site": site["name"], "start": start, "end": end, "offset": 0})
    if not found:
        again = any(transient(p) for p in problems)
        return [], ("no article sitemap found" + (f" ({problems[0]})" if problems else "")
                    + ("; will try again" if again else "")), again
    return found, f"{len(found)} sitemaps to read", False


def read_sitemap(job: dict, site: dict, known: set, cfg: dict, brief: str, key: str, polite: Polite) -> dict:
    """Reads one sitemap and sorts (up to ITEM_BATCHES batches of) its articles. Runs in a worker thread."""
    out = {"kept": [], "checked": 0, "offset": job["offset"], "done": False, "usage": {"input": 0, "output": 0}}
    try:
        return sort_sitemap(job, site, known, cfg, brief, key, polite, out)
    except FetchError as e:
        out["error"] = str(e)
    except ApiProblem as e:
        out["problem"] = e
    except Exception as e:  # never let one odd sitemap stop the run
        out["error"] = f"couldn't read it ({type(e).__name__})"
    return out


def sort_sitemap(job, site, known, cfg, brief, key, polite, out) -> dict:
    kind, entries = parse_sitemap(polite.get(job["url"]))
    if kind == "index":
        out["expand"] = [loc for loc, _ in entries]
        out["done"] = True
        return out
    daily = job["end"] - job["start"] <= 86400
    articles, seen = [], set()
    for loc, lastmod in entries:
        link = clean_link(loc)
        parts = urlsplit(link)
        if not link or parts.netloc.lower() != site["host"] or SKIP_PATH.search(parts.path + "/"):
            continue
        middle = job["start"] + (job["end"] - job["start"]) // 2
        when = lastmod if lastmod and job["start"] <= lastmod < job["end"] else (middle if daily else None)
        if when is None or not (job["start"] <= when < job["end"]):
            continue
        id_ = article_id(link)
        words, section = slug_words(link)
        if id_ in known or id_ in seen or len(words.split()) < 3:
            continue
        seen.add(id_)
        articles.append({"id": id_, "link": link, "outlet": site["name"], "published": when,
                         "words": words, "section": section})
    articles.sort(key=lambda a: (-a["published"], a["link"]))
    todo = articles[job["offset"]: job["offset"] + ITEM_BATCHES * PAST_BATCH]
    for i in range(0, len(todo), PAST_BATCH):
        batch = todo[i:i + PAST_BATCH]
        kept = classify_halving(batch, brief, cfg, key, out["usage"])  # ApiProblem: caller pauses the backfill
        for a in batch:
            d = kept.get(a["id"])
            if d:
                entry = {"id": a["id"], "time": a["published"], "category": d["category"],
                         "title": d.get("headline") or a["words"].capitalize(), "link": a["link"],
                         "outlet": a["outlet"], "rebuilt": True}
                for k in ("based_on", "project", "company", "people", "tags"):
                    if d.get(k):
                        entry[k] = d[k]
                out["kept"].append(entry)
        out["offset"] += len(batch)
        out["checked"] += len(batch)
    out["done"] = out["offset"] >= len(articles)
    return out


def classify_halving(batch, brief, cfg, key, usage) -> dict:
    """classify_batch for backfill: a cut-off answer is retried in halves; unreadable ones are skipped."""
    try:
        return classify_batch(batch, brief, cfg, key, usage, past=True)
    except TooLong:
        if len(batch) == 1:
            return {}
        mid = len(batch) // 2
        return {**classify_halving(batch[:mid], brief, cfg, key, usage),
                **classify_halving(batch[mid:], brief, cfg, key, usage)}
    except BadAnswer:
        return {}


def load_backfill() -> dict:
    if not BACKFILL_FILE.exists():
        return {}
    data = json.loads(BACKFILL_FILE.read_text(encoding="utf-8"))
    return {**data.get("status", {}), "queue": data.get("queue", [])}


def dump_backfill(status: dict) -> str:
    head = {k: v for k, v in status.items() if k != "queue"}
    queue = status.get("queue", [])
    lines = ["{", f'"status": {json.dumps(head, sort_keys=True, ensure_ascii=False)},', '"queue": [']
    lines += [json.dumps(q, sort_keys=True) + ("," if i < len(queue) - 1 else "") for i, q in enumerate(queue)]
    lines += ["]", "}"]
    return "\n".join(lines) + "\n"


def run_backfill(cfg, brief, items, archive, key, usage, deadline, log) -> tuple[dict, ApiProblem | None]:
    """Spends until `deadline` filling the archive back to backfill_days before the first run."""
    status = load_backfill()
    now = time.time()
    if not status.get("until"):
        status = {"until": int(now), "from": int(now), "queue": [], "sites": {}, "checked": 0, "kept": 0,
                  "planned": 0}
    polite = Polite()
    sites = {s["name"]: s for s in backfill_sites(cfg)}
    # Ranges to plan: a newly requested stretch for every site, plus sites that couldn't be
    # reached last time. Whole days, so a later extension never splits one.
    want_from = (status["until"] - cfg["backfill_days"] * 86400) // 86400 * 86400
    retry = status.setdefault("retry", {})  # site name -> [from, to, tries]
    ranges = [(name, frm, to, tries) for name, (frm, to, tries) in retry.items() if name in sites]
    retry.clear()
    if want_from < status["from"]:
        ranges += [(name, want_from, status["from"], 0) for name in sites]
        status["from"] = want_from
    if ranges:
        log(f"Planning the backfill back to {datetime.fromtimestamp(status['from'], timezone.utc):%Y-%m-%d}…")
        with ThreadPoolExecutor(len(ranges)) as pool:
            plans = list(pool.map(lambda r: plan_site(sites[r[0]], r[1], r[2], polite), ranges))
        for (name, frm, to, tries), (found, note, again) in zip(ranges, plans):
            if again and tries + 1 < 8:
                retry[name] = [frm, to, tries + 1]
            elif again:
                note += " (gave up)"
            status["sites"][name] = note
            status["queue"] += found
            status["planned"] = status.get("planned", 0) + len(found)
            log(f"  {name}: {note}")
        status["queue"].sort(key=lambda q: (-q["end"], q["url"]))
    status["days"] = cfg["backfill_days"]

    queue, known, problem = status["queue"], set(items), None
    position, running = 0, {}
    with ThreadPoolExecutor(BACKFILL_WORKERS) as pool:
        while True:
            while problem is None and len(running) < BACKFILL_WORKERS and position < len(queue) \
                    and time.time() < deadline:
                job = queue[position]
                position += 1
                if job["site"] not in sites or job.get("done"):
                    job["done"] = True
                    continue
                running[pool.submit(read_sitemap, job, sites[job["site"]], known, cfg, brief, key, polite)] = job
            if not running:
                break
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                job = running.pop(future)
                result = future.result()
                usage["input"] += result["usage"]["input"]
                usage["output"] += result["usage"]["output"]
                status["checked"] += result["checked"]
                status["kept"] += sum(archive.add(e) for e in result["kept"])
                job["offset"] = result["offset"]
                if result.get("error"):
                    if blocked(result["error"]):
                        status["sites"][job["site"]] = f"blocked ({result['error']})"
                        for other in queue:
                            if other["site"] == job["site"]:
                                other["done"] = True
                    else:
                        job["tries"] = job.get("tries", 0) + 1
                        job["done"] = job["tries"] >= (8 if transient(result["error"]) else 3)
                elif result.get("problem"):
                    problem = result["problem"]
                elif result["done"]:
                    job["done"] = True
                for loc in result.get("expand", []):
                    span = sitemap_span(loc) or (job["start"], job["end"])
                    if not SKIP_SITEMAP.search(loc) and span[1] > status["from"] and span[0] < status["until"]:
                        queue.append({"url": loc, "site": job["site"], "start": max(span[0], status["from"]),
                                      "end": min(span[1], status["until"]), "offset": 0})
                        status["planned"] += 1
    status["queue"] = [q for q in queue if not q.get("done")]
    if problem:
        status["state"] = f"paused: {problem}"
    else:
        status["state"] = "running" if status["queue"] or status["retry"] else "done"
    log(f"Backfill: {status['checked']:,} older articles checked, {status['kept']:,} stories added, "
        f"{len(status['queue'])} sitemaps to go.")
    return status, problem


def backfill_progress(status: dict) -> float:
    planned = status.get("planned") or 0
    return 1.0 if not planned else max(0.0, min(1.0, 1 - len(status.get("queue", [])) / planned))


# ------------------------------------------------------------------ output

def site_info(cfg: dict) -> dict:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    base = str(cfg.get("site_url") or "").strip()
    if not base and "/" in repo:
        owner, name = repo.lower().split("/", 1)
        base = f"https://{owner}.github.io/" if name == f"{owner}.github.io" else f"https://{owner}.github.io/{name}/"
    if base and not base.endswith("/"):
        base += "/"
    return {"base": base,
            "repo": f"https://github.com/{repo}" if repo else "",
            "edit": f"https://github.com/{repo}/edit/{branch}/criteria.md" if repo else ""}


def utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0").lower()


def day_name(dt: datetime) -> str:
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')}"


def short_date(dt: datetime) -> str:
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}, {clock(dt)}"


def long_date(dt: datetime) -> str:
    return f"{dt.day} {dt.strftime('%B %Y')}"


def category_of(it: dict, cats: dict) -> dict:
    return cats.get(it.get("category"), {"emoji": "•", "label": str(it.get("category", "")).title()})


def colours(cats: dict) -> dict[str, int]:
    return {key: i % len(PALETTE) for i, key in enumerate(cats)}


def model_name(cfg: dict) -> str:
    named = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)", str(cfg["model"]))
    return f"Claude {named[1].title()} {named[2]}.{named[3]}" if named else str(cfg["model"])


def render_feed(stories: list[dict], cfg: dict, site: dict) -> str:
    cats = cfg["categories"]
    shown = stories[: int(cfg["feed_items"])]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">',
           "<channel>",
           f"<title>{x(cfg['title'])}</title>",
           f"<link>{x(site['base'] or site['repo'] or 'https://github.com/')}</link>",
           f"<description>{x(cfg['description'])}</description>",
           "<language>en</language>",
           "<ttl>15</ttl>"]
    if site["base"]:
        out.append(f'<atom:link href="{x(site["base"])}feed.xml" rel="self" type="application/rss+xml"/>')
    if shown:
        out.append(f"<lastBuildDate>{format_datetime(utc(item_time(shown[0]['main'])))}</lastBuildDate>")
    for story in shown:
        m = story["main"]
        cat = category_of(m, cats)
        photo = story_photo(story)
        body = [f'<p><img src="{h(photo)}" alt=""></p>'] if photo else []
        extra = [cfg["tags"][t] for t in story_tags(story) if t in cfg["tags"]]
        labels = " · ".join(h(f"{c['emoji']} {c['label']}") for c in [cat, *extra])
        body.append(f"<p><b>{labels}</b> · {h(m['outlet'])}</p>")
        if m.get("summary"):
            body.append(f"<p>{h(m['summary'])}</p>")
        if m.get("based_on"):
            body.append(f"<p>📖 Based on {h(m['based_on'])}</p>")
        if story["also"]:
            links = " · ".join(f'<a href="{h(o["link"])}">{h(o["outlet"])}</a>' for o in story["also"])
            body.append(f"<p>Also reported by {links}</p>")
        out += ["<item>",
                f"<title>{x(cat['emoji'] + ' ' + m['title'] + ' · ' + m['outlet'])}</title>",
                f"<link>{x(m['link'])}</link>",
                f'<guid isPermaLink="true">{x(m["link"])}</guid>',
                f"<pubDate>{format_datetime(utc(item_time(m)))}</pubDate>",
                f"<category>{x(cat['label'])}</category>",
                *(f"<category>{x(t['label'])}</category>" for t in extra)]
        if m.get("feed"):
            out.append(f'<source url="{x(m["feed"])}">{x(m["outlet"])}</source>')
        if photo:
            out.append(f'<media:content url="{x(photo)}" medium="image"/>')
        out += [f"<description>{x(''.join(body))}</description>", "</item>"]
    out += ["</channel>", "</rss>"]
    return "\n".join(out) + "\n"


def feed_status(health: dict, now: float, tz) -> tuple[bool, str]:
    """(ok, note) for one source, for the page."""
    if health.get("error"):
        since = short_date(datetime.fromtimestamp(health.get("failing_since", now), tz))
        return False, f"not loading since {since}: {health['error']}"
    newest = health.get("newest")
    if newest and now - newest > STALE_HOURS * 3600:
        return False, f"nothing new since {short_date(datetime.fromtimestamp(newest, tz))}; the feed address may have changed"
    if health.get("overflow_at") and now - health["overflow_at"] < 86400:
        return False, "busy enough that a few articles may have been missed"
    if not health.get("loaded"):
        return False, "not checked yet"
    return True, ""


def backfill_note(backfill: dict, cfg: dict) -> str:
    if not backfill.get("until") or backfill.get("state") == "done":
        return ""
    pct = round(backfill_progress(backfill) * 100)
    state = backfill.get("state", "running")
    if not backfill.get("queue") and backfill.get("retry"):
        return (f"Filling in the past {cfg['backfill_days']} days of the archive: waiting to reach "
                f"{', '.join(sorted(backfill['retry']))}.")
    if state.startswith("paused"):
        return f"Filling in the past {cfg['backfill_days']} days of the archive is paused ({state[8:]}); {pct}% done."
    return f"Filling in the past {cfg['backfill_days']} days of the archive: {pct}% done."


def money(meta: dict, cfg: dict, now: float) -> str:
    used = meta.get("usage", {}).get(datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m"))
    if not used:
        return ""
    dollars = (used["input"] * cfg["prices"]["input"] + used["output"] * cfg["prices"]["output"]) / 1e6
    return f" · AI cost this month ≈ {'<US$0.01' if dollars < 0.01 else f'US${dollars:.2f}'}"


def render_page(stories, items, meta, cfg, site, labels, now, backfill) -> str:
    tz, cats = cfg["tz"], cfg["categories"]
    color = colours(cats)
    checked = [it.get("first_seen", 0) for it in items.values()]
    anchor = max(checked) if checked else now  # "now" as of the last check, so the page only changes with news

    recent = [s for s in stories if item_time(s["main"]) >= anchor - PAGE_DAYS * 86400]
    counts = {key: sum(1 for s in recent if s["main"]["category"] == key) for key in cats}
    tags = cfg["tags"]
    tag_counts = {key: sum(1 for s in recent if key in story_tags(s)) for key in tags}
    chips = [f'<button type="button" data-cat="all" aria-pressed="true">All<span class="n">{len(recent)}</span></button>']
    for key, cat in cats.items():
        if counts[key]:
            chips.append(f'<button type="button" data-cat="{h(key)}" aria-pressed="false">{h(cat["emoji"])} '
                         f'{h(cat["label"])}<span class="n">{counts[key]}</span></button>')
    for key, tag in tags.items():
        if tag_counts[key]:
            chips.append(f'<button type="button" data-cat="{h(key)}" aria-pressed="false">{h(tag["emoji"])} '
                         f'{h(tag["label"])}<span class="n">{tag_counts[key]}</span></button>')

    days: list[str] = []
    current = None
    for story in recent:
        m = story["main"]
        dt = datetime.fromtimestamp(item_time(m), tz)
        if dt.date() != current:
            if current is not None:
                days.append("</section>")
            current = dt.date()
            days.append(f'<section class="day"><h2 class="day" data-day="{dt.date().isoformat()}">{h(day_name(dt))}</h2>')
        cat = category_of(m, cats)
        card = [f'<article class="story" data-cat="{h(m["category"])}" data-tags="{h(" ".join(story_tags(story)))}" '
                f'style="--c: var(--cat{color.get(m["category"], 0)})">',
                photo_html(story, m["link"]),
                f'<div class="meta"><span class="tag">{h(cat["emoji"])} {h(cat["label"])}</span>'
                f'{tag_spans(story_tags(story), tags)}<span>{h(m["outlet"])} · {clock(dt)}</span></div>',
                f'<h3><a href="{h(m["link"])}" target="_blank" rel="noopener">{h(m["title"])}</a></h3>']
        if m.get("summary"):
            card.append(f'<p class="dek">{h(m["summary"])}</p>')
        if m.get("based_on"):
            card.append(f'<p class="basis">📖 Based on {h(m["based_on"])}</p>')
        if story["also"]:
            links = " · ".join(f'<a href="{h(o["link"])}" target="_blank" rel="noopener">{h(o["outlet"])}</a>'
                               for o in story["also"])
            card.append(f'<p class="also">Also reported by {links}</p>')
        card.append("</article>")
        days.append("".join(card))
    if current is not None:
        days.append("</section>")
    if not recent:
        days.append('<p class="empty">Nothing has made the cut yet. Stories appear here as the trades publish them.</p>')
    days.append('<p class="more"><a href="archive.html">Older stories are in the archive →</a></p>')

    queued = waiting(items)
    banners = []
    api = meta.get("api")
    if api:
        banners.append(f"<strong>The AI filter couldn't run:</strong> {h(api['error'])}. "
                       f"{len(queued)} article{'s are' if len(queued) != 1 else ' is'} waiting and will be "
                       "checked as soon as it's fixed.")
    sources, problems = [], 0
    for feed in cfg["feeds"]:
        ok, note = feed_status(meta.get("feeds", {}).get(feed["url"], {}), now, tz)
        problems += not ok
        mark = '<span class="ok">✓</span>' if ok else '<span class="bad">⚠</span>'
        sources.append(f"<li>{mark} {h(labels[feed['url']])}{' · ' + h(note) if note else ''}</li>")
    if problems:
        banners.append(f"{problems} source{'s need' if problems > 1 else ' needs'} attention; see Sources below.")
    banner = "".join(f'<div class="banner">{b}</div>' for b in banners)

    filtered = sorted((it for it in items.values()
                       if it["status"] in ("rejected", "error") and it.get("title")
                       and it.get("first_seen", 0) >= anchor - FILTERED_HOURS * 3600),
                      key=lambda it: (item_time(it), it["id"]), reverse=True)
    filtered_rows = "".join(
        f'<li><span class="small">{h(it["outlet"])}</span><br>'
        f'<a href="{h(it["link"])}" target="_blank" rel="noopener">{h(it["title"])}</a></li>'
        for it in filtered[:300])
    edit_hint = (f' <a href="{h(site["edit"])}">Edit criteria.md</a> to describe it in plain English.'
                 if site["edit"] else " Describe it in plain English in criteria.md.")
    filtered_html = (f'<details><summary>Filtered out in the last 24 hours ({len(filtered)})</summary>'
                     f'<p class="small">Something here you wanted?{edit_hint}</p><ul>{filtered_rows}</ul></details>')
    sources_html = (f'<details{" open" if problems else ""}><summary>Sources ({len(cfg["feeds"])})'
                    f'{" · all loading" if not problems else ""}</summary><ul>{"".join(sources)}</ul></details>')

    note = backfill_note(backfill, cfg)
    footer = (f"Checks the trades every 15 minutes · sorted by {h(model_name(cfg))}{money(meta, cfg, anchor)}"
              + (f' · <a href="{h(site["repo"])}">Repo</a>' if site["repo"] else "")
              + (f"<br>{h(note)}" if note else ""))
    last = short_date(datetime.fromtimestamp(anchor, tz)) if checked else "not yet"
    status = (f"{len(recent)} stor{'ies' if len(recent) != 1 else 'y'} in the last {PAGE_DAYS} days · "
              f"updated {h(last)} · <a href=\"archive.html\">Archive</a> · <a href=\"feed.xml\">RSS feed</a>")
    return PAGE.substitute(
        css=css(), title=h(cfg["title"]), description=h(cfg["description"]), status=status, banner=banner,
        chips="".join(chips), days="".join(days), filtered=filtered_html, sources=sources_html,
        footer=footer, timezone=h(str(cfg["timezone"])))


def render_archive(cfg: dict, meta: dict, backfill: dict, site: dict, counts: dict) -> str:
    cats = cfg["categories"]
    color = colours(cats)
    cat_json = json.dumps({k: {"emoji": v["emoji"], "label": v["label"], "color": color[k]} for k, v in cats.items()},
                          ensure_ascii=False).replace("</", "<\\/")
    chips = ['<button type="button" data-cat="all" aria-pressed="true">All</button>']
    chips += [f'<button type="button" data-cat="{h(k)}" aria-pressed="false">{h(v["emoji"])} {h(v["label"])}</button>'
              for k, v in [*cats.items(), *cfg["tags"].items()]]
    tag_json = json.dumps(cfg["tags"], ensure_ascii=False).replace("</", "<\\/")
    total = sum(counts.values())
    note = f"{total:,} stor{'ies' if total != 1 else 'y'} kept so far."
    started = meta.get("started")
    if backfill.get("kept") and started:
        note += (f" Stories from before {long_date(datetime.fromtimestamp(started, cfg['tz']))} were filled in "
                 "afterwards from each site's list of articles, so their titles are rebuilt from the link.")
    progress = backfill_note(backfill, cfg)
    if progress:
        note += " " + progress
    about = [f"<li>Kept {'for ' + str(cfg['archive_months']) + ' months' if cfg['archive_months'] else 'indefinitely'}"
             " (change <code>archive_months</code> in config.yml).</li>"]
    for name, site_note in sorted((backfill.get("sites") or {}).items()):
        about.append(f"<li>{h(name)}: {h(site_note)}</li>")
    about_html = f'<details><summary>About the archive</summary><ul>{"".join(about)}</ul></details>'
    footer = ('<a href="./">Latest stories</a> · <a href="feed.xml">RSS feed</a>'
              + (f' · <a href="{h(site["repo"])}">Repo</a>' if site["repo"] else ""))
    return ARCHIVE.substitute(css=css(), title=h(cfg["title"]), note=h(note), chips="".join(chips),
                              about=about_html, footer=footer, timezone=h(str(cfg["timezone"])), cats=cat_json,
                              tags=tag_json)


def css() -> str:
    light = "".join(f"--cat{i}: {c[0]}; " for i, c in enumerate(PALETTE))
    dark = "".join(f"--cat{i}: {c[1]}; " for i, c in enumerate(PALETTE))
    return CSS.substitute(light=light, dark=dark)


CSS = Template("""
:root { --bg: #faf9f6; --text: #1d1c1a; --muted: #6e6a64; --line: #e7e3dc; --chip: #efece6; --chip-on: #1d1c1a;
  --chip-on-text: #faf9f6; --warn-bg: #fff3e0; --warn-text: #7a4300; --ok: #15803d; --field: #ffffff; $light color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #131312; --text: #ecebe8; --muted: #a29e97; --line: #2b2a27; --chip: #23221f; --chip-on: #ecebe8;
    --chip-on-text: #131312; --warn-bg: #3a2a12; --warn-text: #ffd59e; --ok: #5fd08b; --field: #1c1b19; $dark }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text); -webkit-text-size-adjust: 100%;
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
a { color: inherit; }
.wrap { max-width: 680px; margin: 0 auto; padding: 0 16px 56px; }
header { padding: 28px 0 14px; }
.kicker { margin: 0 0 6px; font-size: 12px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
.kicker a { text-decoration: none; }
h1 { margin: 0; font: 700 32px/1.05 ui-serif, Georgia, "Times New Roman", serif; letter-spacing: -.01em; }
.sub { margin: 8px 0 0; color: var(--muted); font-size: 15px; }
.status { margin: 10px 0 0; color: var(--muted); font-size: 13px; }
nav { position: sticky; top: 0; z-index: 2; display: flex; gap: 8px; overflow-x: auto; scrollbar-width: none;
  margin: 0 -16px; padding: 10px 16px; background: var(--bg); border-bottom: 1px solid var(--line); }
nav::-webkit-scrollbar { display: none; }
nav button { flex: none; border: 0; border-radius: 999px; padding: 8px 12px; background: var(--chip); color: var(--text);
  font: 500 14px/1 inherit; white-space: nowrap; cursor: pointer; }
nav button[aria-pressed="true"] { background: var(--chip-on); color: var(--chip-on-text); }
nav .n { margin-left: 6px; opacity: .6; font-variant-numeric: tabular-nums; }
.banner { margin: 14px 0 0; padding: 12px 14px; border-radius: 10px; background: var(--warn-bg); color: var(--warn-text); font-size: 14px; }
.banner:last-of-type { margin-bottom: 14px; }
h2.day { margin: 28px 0 0; padding-bottom: 8px; border-bottom: 1px solid var(--line); color: var(--muted);
  font-size: 12px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; }
.story { display: flow-root; padding: 14px 0; border-bottom: 1px solid var(--line); }
.photo { float: right; width: 112px; height: 84px; margin: 2px 0 6px 14px; border-radius: 8px; overflow: hidden; background: var(--chip); }
.photo img { display: block; width: 100%; height: 100%; object-fit: cover; }
@media (min-width: 560px) { .photo { width: 152px; height: 100px; } }
main section:last-of-type .story:last-child { border-bottom: 0; }
.meta { display: flex; flex-wrap: wrap; gap: 2px 10px; align-items: baseline; color: var(--muted); font-size: 13px; }
.tag { color: var(--c); font-weight: 600; }
.xtag { padding: 0 7px; border-radius: 999px; background: var(--chip); color: var(--text); font-size: 12px; font-weight: 500; }
.rebuilt { font-style: italic; }
.story h3 { margin: 4px 0 0; font-size: 17px; font-weight: 600; line-height: 1.35; }
.story h3 a { text-decoration: none; }
.story h3 a:hover { text-decoration: underline; }
.dek { margin: 4px 0 0; color: var(--muted); font-size: 14.5px; display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; }
.basis { margin: 6px 0 0; font-size: 14px; }
.also { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
.empty { padding: 40px 0; color: var(--muted); text-align: center; }
.more { margin: 20px 0 0; color: var(--muted); font-size: 14px; }
.controls { display: flex; gap: 8px; margin: 4px 0 12px; }
.controls input, .controls select { min-width: 0; padding: 9px 12px; border: 1px solid var(--line); border-radius: 10px;
  background: var(--field); color: var(--text); font: inherit; font-size: 16px; }
.controls input { flex: 1 1 auto; }
.controls select { flex: 0 1 auto; max-width: 48%; }
.controls select:disabled { opacity: .5; }
details { margin-top: 28px; padding-top: 12px; border-top: 1px solid var(--line); }
summary { cursor: pointer; font-size: 14px; font-weight: 600; }
details ul { margin: 10px 0 0; padding: 0; list-style: none; font-size: 14px; }
details li { padding: 7px 0; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.small { color: var(--muted); font-size: 12.5px; }
.ok { color: var(--ok); }
.bad { color: var(--warn-text); }
footer { margin-top: 28px; color: var(--muted); font-size: 13px; }
""")

ICON = ("<link rel=\"icon\" href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
        "<text y='.9em' font-size='90'>🎬</text></svg>\">")

PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>$title</title>
<meta name="description" content="$description">
<link rel="alternate" type="application/rss+xml" title="$title" href="feed.xml">
""" + ICON + """
<style>$css</style>
</head>
<body>
<div class="wrap">
<header>
<p class="kicker">Development &amp; casting desk</p>
<h1>$title</h1>
<p class="sub">$description</p>
<p class="status">$status</p>
</header>
$banner
<nav aria-label="Filter by category">$chips</nav>
<main>$days</main>
$filtered
$sources
<footer>$footer</footer>
</div>
<script>
(function () {
  var zone = "$timezone";
  try {
    var day = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" });
    var today = day.format(new Date()), yesterday = day.format(new Date(Date.now() - 864e5));
    document.querySelectorAll("h2.day").forEach(function (el) {
      var d = el.getAttribute("data-day");
      if (d === today) el.textContent = "Today";
      else if (d === yesterday) el.textContent = "Yesterday";
    });
  } catch (e) {}
  var buttons = document.querySelectorAll("nav button");
  function show(cat) {
    buttons.forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-cat") === cat)); });
    document.querySelectorAll(".story").forEach(function (s) {
      s.hidden = cat !== "all" && s.getAttribute("data-cat") !== cat &&
        (" " + (s.getAttribute("data-tags") || "") + " ").indexOf(" " + cat + " ") === -1;
    });
    document.querySelectorAll("section.day").forEach(function (sec) {
      sec.hidden = !sec.querySelector(".story:not([hidden])");
    });
    try { localStorage.setItem("trade-feed-category", cat); } catch (e) {}
  }
  buttons.forEach(function (b) { b.addEventListener("click", function () { show(b.getAttribute("data-cat")); }); });
  var saved = "all";
  try { saved = localStorage.getItem("trade-feed-category") || "all"; } catch (e) {}
  var exists = false;
  buttons.forEach(function (b) { if (b.getAttribute("data-cat") === saved) exists = true; });
  show(exists ? saved : "all");
})();
</script>
</body>
</html>
""")

ARCHIVE = Template(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Archive · $title</title>
""" + ICON + r"""
<style>$css</style>
</head>
<body>
<div class="wrap">
<header>
<p class="kicker"><a href="./">← Latest stories</a></p>
<h1>Archive</h1>
<p class="sub">$note</p>
</header>
<div class="controls">
<input id="q" type="search" placeholder="Search titles, people, studios" autocomplete="off" enterkeyhint="search" aria-label="Search the archive">
<select id="month" aria-label="Month"></select>
</div>
<nav aria-label="Filter by category">$chips</nav>
<main id="list"><p class="empty">Loading…</p></main>
<p class="more" id="more"></p>
$about
<footer>$footer</footer>
</div>
<script>
(function () {
  var zone = "$timezone";
  var cats = $cats;
  var tags = $tags;
  var list = document.getElementById("list"), more = document.getElementById("more");
  var input = document.getElementById("q"), select = document.getElementById("month");
  var buttons = document.querySelectorAll("nav button");
  var view = { cat: "all", month: "", q: "" }, months = [], counts = {}, cache = {}, ticket = 0, timer = null;
  var dayKey = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" });
  var dayName = new Intl.DateTimeFormat("en-GB", { timeZone: zone, weekday: "long", day: "numeric", month: "long", year: "numeric" });
  var clock = new Intl.DateTimeFormat("en-US", { timeZone: zone, hour: "numeric", minute: "2-digit" });
  var monthName = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", month: "long", year: "numeric" });

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function link(u) { return /^https?:\/\//i.test(u || "") ? esc(u) : "#"; }
  function load(m) {
    if (!cache[m]) {
      cache[m] = fetch("archive/" + m + ".json").then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
    }
    return cache[m];
  }
  function card(e) {
    var c = cats[e.category] || { emoji: "•", label: e.category, color: 0 };
    var when = e.rebuilt ? "" : " · " + clock.format(new Date(e.time * 1000)).toLowerCase();
    var out = '<article class="story" style="--c: var(--cat' + c.color + ')">' +
      (link(e.image) !== "#" ? '<a class="photo" href="' + link(e.link) +
        '" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true"><img src="' + link(e.image) +
        '" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.parentNode.remove()"></a>' : "") +
      '<div class="meta">' +
      '<span class="tag">' + esc(c.emoji + " " + c.label) + "</span>" +
      (e.tags || []).filter(function (t) { return tags[t]; }).map(function (t) {
        return '<span class="xtag">' + esc(tags[t].emoji + " " + tags[t].label) + "</span>";
      }).join("") +
      "<span>" + esc(e.outlet) + esc(when) + "</span>" +
      (e.rebuilt ? '<span class="rebuilt">title from link</span>' : "") + "</div>" +
      '<h3><a href="' + link(e.link) + '" target="_blank" rel="noopener">' + esc(e.title) + "</a></h3>";
    if (e.summary) out += '<p class="dek">' + esc(e.summary) + "</p>";
    if (e.based_on) out += '<p class="basis">📖 Based on ' + esc(e.based_on) + "</p>";
    if (e.also && e.also.length) {
      out += '<p class="also">Also reported by ' + e.also.map(function (o) {
        return '<a href="' + link(o.link) + '" target="_blank" rel="noopener">' + esc(o.outlet) + "</a>";
      }).join(" · ") + "</p>";
    }
    return out + "</article>";
  }
  function render(entries, words) {
    var out = [], current = null;
    entries.forEach(function (e) {
      var d = new Date(e.time * 1000), key = dayKey.format(d);
      if (key !== current) {
        if (current !== null) out.push("</section>");
        current = key;
        out.push('<section class="day"><h2 class="day">' + esc(dayName.format(d)) + "</h2>");
      }
      out.push(card(e));
    });
    if (current !== null) out.push("</section>");
    list.innerHTML = out.length ? out.join("") :
      '<p class="empty">' + (words.length ? "Nothing matches that search." : "Nothing here yet.") + "</p>";
  }
  function matches(e, words) {
    var hay = [e.title, e.outlet, e.based_on, e.project, e.company, (e.people || []).join(" ")].join(" ").toLowerCase();
    return words.every(function (w) { return hay.indexOf(w) !== -1; });
  }
  function refresh() {
    var words = view.q.toLowerCase().split(/\s+/).filter(Boolean), mine = ++ticket;
    var wanted = words.length ? months : (view.month ? [view.month] : []);
    select.disabled = words.length > 0;
    more.textContent = "";
    if (!wanted.length) { list.innerHTML = '<p class="empty">Nothing in the archive yet.</p>'; return; }
    list.innerHTML = '<p class="empty">Loading…</p>';
    Promise.all(wanted.map(load)).then(function (parts) {
      if (mine !== ticket) return;
      var all = [];
      parts.forEach(function (p) { all = all.concat(p); });
      all = all.filter(function (e) {
        return (view.cat === "all" || e.category === view.cat || (e.tags || []).indexOf(view.cat) !== -1) &&
          (!words.length || matches(e, words));
      });
      all.sort(function (a, b) { return b.time - a.time; });
      var shown = all.slice(0, 400);
      render(shown, words);
      if (words.length) {
        more.textContent = all.length > shown.length
          ? "Showing the newest 400 of " + all.length.toLocaleString() + " matches. Add a word to narrow it down."
          : all.length + (all.length === 1 ? " match" : " matches") + " across the whole archive.";
      }
    }).catch(function () {
      if (mine === ticket) list.innerHTML = '<p class="empty">The archive couldn’t be loaded. Try again in a minute.</p>';
    });
  }
  fetch("archive/index.json").then(function (r) { return r.json(); }).then(function (idx) {
    counts = idx.months || {};
    months = Object.keys(counts).sort().reverse();
    select.innerHTML = months.map(function (m) {
      var p = m.split("-");
      return '<option value="' + m + '">' + esc(monthName.format(new Date(Date.UTC(+p[0], +p[1] - 1, 1)))) +
        " (" + counts[m] + ")</option>";
    }).join("");
    view.month = months[0] || "";
    refresh();
  }).catch(function () { list.innerHTML = '<p class="empty">Nothing in the archive yet.</p>'; });
  select.addEventListener("change", function () { view.month = select.value; refresh(); });
  input.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { view.q = input.value.trim(); refresh(); }, 250);
  });
  buttons.forEach(function (b) {
    b.addEventListener("click", function () {
      view.cat = b.getAttribute("data-cat");
      buttons.forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); });
      refresh();
    });
  });
})();
</script>
</body>
</html>
""")


def write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------ main

def main() -> int:
    cfg = load_config()
    brief = load_brief()
    first_run = not STATE_FILE.exists()
    state = load_state()
    items, meta = state["items"], state["meta"]
    now = time.time()
    meta.setdefault("started", int(now))
    labels = feed_labels(cfg["feeds"])
    summary = ["### Trade Feed run"]
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    forced = os.environ.get("RECHECK", "").strip().lower() in ("1", "true", "yes")
    rechecking = mark_recheck(items, meta, criteria_signature(brief, cfg), now, forced)
    if rechecking:
        print(f"The brief changed: re-checking {rechecking} recent articles.")
        summary.append(f"- Brief changed: re-checking {rechecking} recent articles")

    known = set(items)
    oldest_wanted = now - MAX_AGE_DAYS * 86400

    def is_known(a: dict) -> bool:
        return a["id"] in known or bool(a["published"] and a["published"] < oldest_wanted)

    health_book = meta.setdefault("feeds", {})
    fresh = 0
    for feed in cfg["feeds"]:
        health = health_book.setdefault(feed["url"], {})
        label = labels[feed["url"]]
        try:
            articles, overflow = read_feed(feed, is_known, first_time=not health.get("loaded"))
        except FetchError as e:
            health.setdefault("failing_since", int(now))
            health["error"] = str(e)
            print(f"✗ {label}: {e}")
            summary.append(f"- ✗ {label}: {e}")
            continue
        added = intake(articles, items, now)
        fresh += added
        health.pop("error", None)
        health.pop("failing_since", None)
        health["loaded"] = True
        newest = max((min(a["published"], int(now)) for a in articles if a["published"]), default=0)
        if newest > health.get("newest", 0):
            health["newest"] = newest
        if overflow:
            health["overflow_at"] = int(now)
        print(f"✓ {label}: {len(articles)} articles, {added} new" + (" (some may have been missed)" if overflow else ""))
    for url in list(health_book):
        if url not in labels:
            del health_book[url]

    usage = {"input": 0, "output": 0}
    queued = waiting(items)
    problem = None
    if queued:
        if not key:
            problem = ApiProblem("the ANTHROPIC_API_KEY secret isn't set", urgent=True)
        else:
            print(f"Asking Claude about {len(queued)} articles…")
            problem = classify_waiting(items, brief, cfg, key, usage, print)
    exit_code = record_api(meta, problem, attempted=bool(queued), now=now)
    if first_run and exit_code:
        # The run that starts when the files are first uploaded often comes before the key
        # is added. Give setup a grace period: alert next run if it's still a problem.
        meta["api"].pop("alerted", None)
        exit_code = 0

    find_photos(items, print)
    prune(items, now)
    stories = build_stories(items)
    archive = Archive(cfg["tz"])
    sync_archive(archive, stories, items)

    backfill = load_backfill()
    if cfg["backfill_days"] and key and not (problem and problem.urgent):
        backfill, trouble = run_backfill(cfg, brief, items, archive, key, usage, time.time() + BACKFILL_SECONDS, print)
        if trouble and trouble.urgent:
            exit_code = max(exit_code, record_api(meta, trouble, attempted=True, now=now))
        summary.append(f"- Backfill: {round(backfill_progress(backfill) * 100)}% done, "
                       f"{backfill.get('kept', 0):,} older stories added so far")
    record_usage(meta, usage, now)

    counts = archive.save(cfg["archive_months"], now)
    site = site_info(cfg)
    feed_xml = render_feed(stories, cfg, site)
    page = render_page(stories, items, meta, cfg, site, labels, now, backfill)
    archive_page = render_archive(cfg, meta, backfill, site, counts)
    write_if_changed(DOCS / "feed.xml", feed_xml)
    write_if_changed(DOCS / "index.html", page)
    write_if_changed(DOCS / "archive.html", archive_page)
    if backfill.get("until"):
        write_if_changed(BACKFILL_FILE, dump_backfill(backfill))
    write_if_changed(STATE_FILE, dump_state(state))

    kept_now = sum(1 for it in items.values() if it["status"] == "kept" and it.get("first_seen", 0) >= now - 1)
    print(f"{fresh} new articles, {kept_now} kept. {len(stories)} stories in the feed.")
    summary.append(f"- New articles: {fresh}, kept: {kept_now}; stories in the feed: {len(stories)}")
    if usage["input"]:
        summary.append(f"- Claude tokens: {usage['input']:,} in / {usage['output']:,} out")
    if problem:
        print(f"! {problem}")
        summary.append(f"- ⚠ Claude: {problem}")
    write_summary(summary)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
