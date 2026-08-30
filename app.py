from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response


ATOM = "http://www.w3.org/2005/Atom"
TORZNAB = "http://torznab.com/schemas/2015/feed"
ET.register_namespace("torznab", TORZNAB)

FEED_URL = os.getenv("FEED_URL", "https://720pier.ru/feed/forum/43")
FORUM_URL = os.getenv("FORUM_URL", "https://720pier.ru/viewforum.php?f=43")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8788").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
PIER_COOKIE = os.getenv("PIER_COOKIE", "")
CACHE_SECONDS = max(0, int(os.getenv("CACHE_SECONDS", "300")))
REQUEST_TIMEOUT = max(1, int(os.getenv("REQUEST_TIMEOUT", "20")))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)
IGNORED_SEARCH_TERMS = {"vs", "v", "at"}
logger = logging.getLogger("720pier-adapter")


@dataclass(frozen=True)
class FeedItem:
    item_id: str
    title: str
    guid: str
    details_url: str
    published: str
    attachment_name: str
    download_url: str | None = None
    size: int = 0
    seeders: int = 0
    peers: int = 0


app = FastAPI(title="720pier to Torznab", docs_url=None, redoc_url=None)
_feed_lock = threading.Lock()
_feed_time = 0.0
_feed_items: list[FeedItem] = []
_detail_lock = threading.Lock()
_detail_cache: dict[str, tuple[float, FeedItem]] = {}


def _headers() -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/atom+xml,application/xml,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9,de;q=0.8",
        "Connection": "close",
    }
    if PIER_COOKIE:
        headers["Cookie"] = PIER_COOKIE
    return headers


def _fetch(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read(), response.headers.get("Content-Type", "application/octet-stream")
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("720pier request failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail=f"720pier unavailable: {exc}") from exc


def _rfc822(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return format_datetime(parsed)
    except ValueError:
        return value or format_datetime(datetime.now(timezone.utc))


def _entry_link(entry: ET.Element) -> str:
    for link in entry.findall(f"{{{ATOM}}}link"):
        if link.get("href") and link.get("rel", "alternate") in {"alternate", ""}:
            return link.get("href", "")
    return entry.findtext(f"{{{ATOM}}}id", default="").strip()


def parse_feed(xml_data: bytes | str) -> list[FeedItem]:
    root = ET.fromstring(xml_data)
    found: list[FeedItem] = []
    seen_titles: set[str] = set()
    for entry in root.findall(f"{{{ATOM}}}entry"):
        content = entry.findtext(f"{{{ATOM}}}content", default="")
        attachment = re.search(r'alt=["\']([^"\']+\.torrent)["\']', content, flags=re.I)
        if not attachment:
            continue  # replies are also present in phpBB feeds
        raw_title = entry.findtext(f"{{{ATOM}}}title", default="").strip()
        title = re.sub(r"^NFL\s*[•:-]\s*", "", raw_title, flags=re.I)
        title_key = re.sub(r"\W+", " ", title.casefold()).strip()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        details_url = _entry_link(entry)
        guid = entry.findtext(f"{{{ATOM}}}id", default=details_url).strip() or details_url
        item_id = hashlib.sha256(guid.encode()).hexdigest()[:24]
        published = entry.findtext(f"{{{ATOM}}}published", default="") or entry.findtext(f"{{{ATOM}}}updated", default="")
        found.append(FeedItem(item_id, title, guid, details_url, _rfc822(published), attachment.group(1)))
    return found


class _TorrentRowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_row = False
        self.row_depth = 0
        self.row_text: list[str] = []
        self.row_size_title = ""
        self.row_download = ""
        self.result: tuple[str, str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "tr":
            if not self.in_row:
                self.in_row, self.row_depth = True, 1
                self.row_text, self.row_size_title, self.row_download = [], "", ""
            else:
                self.row_depth += 1
        if not self.in_row:
            return
        if tag == "a" and re.search(r"/download/torrent\?id=\d+", values.get("href") or ""):
            self.row_download = values.get("href") or ""
        if tag == "span" and re.search(r"[\d ]+\s+Bytes", values.get("title") or "", re.I):
            self.row_size_title = values.get("title") or ""

    def handle_data(self, data: str) -> None:
        if self.in_row and data.strip():
            self.row_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag != "tr" or not self.in_row:
            return
        self.row_depth -= 1
        if self.row_depth == 0:
            if self.row_download and self.result is None:
                self.result = (self.row_download, self.row_size_title, " ".join(self.row_text))
            self.in_row = False


class _ForumParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_topic = False
        self.topic_depth = 0
        self.capture_title = False
        self.capture_seeders = False
        self.capture_peers = False
        self.href = ""
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.seeders = 0
        self.peers = 0
        self.rows: list[tuple[str, str, int, int, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "li" and "row" in classes:
            if not self.in_topic:
                self.in_topic, self.topic_depth = True, 1
                self.href, self.title_parts, self.text_parts = "", [], []
                self.seeders, self.peers = 0, 0
            else:
                self.topic_depth += 1
        if not self.in_topic:
            return
        if tag == "a" and "topictitle" in classes:
            self.href = values.get("href") or ""
            self.capture_title = True
        if tag == "span" and "seed" in classes and not self.seeders:
            self.capture_seeders = True
        if tag == "span" and "leech" in classes and not self.peers:
            self.capture_peers = True

    def handle_data(self, data: str) -> None:
        if not self.in_topic:
            return
        value = data.strip()
        if value:
            self.text_parts.append(value)
        if self.capture_title and value:
            self.title_parts.append(value)
        if self.capture_seeders and value.isdigit():
            self.seeders = int(value)
        if self.capture_peers and value.isdigit():
            self.peers = int(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.capture_title = False
        if tag == "span":
            self.capture_seeders = self.capture_peers = False
        if tag != "li" or not self.in_topic:
            return
        self.topic_depth -= 1
        if self.topic_depth:
            return
        title = " ".join(self.title_parts).strip()
        text = " ".join(self.text_parts)
        size_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(GiB|MiB)", text, re.I)
        size = 0
        if size_match:
            factor = 1024**3 if size_match.group(2).casefold() == "gib" else 1024**2
            size = int(float(size_match.group(1).replace(",", ".")) * factor)
        if self.href and re.match(r"^NFL\s+\d{4}", title, re.I):
            self.rows.append((self.href, title, size, self.seeders, self.peers))
        self.in_topic = False


def parse_forum(html_data: bytes | str) -> list[FeedItem]:
    parser = _ForumParser()
    parser.feed(html_data.decode("utf-8", errors="replace") if isinstance(html_data, bytes) else html_data)
    items: list[FeedItem] = []
    now = format_datetime(datetime.now(timezone.utc))
    for href, title, size, seeders, peers in parser.rows:
        details_url = urljoin(FORUM_URL, href)
        item_id = hashlib.sha256(details_url.encode()).hexdigest()[:24]
        items.append(FeedItem(
            item_id=item_id,
            title=title,
            guid=details_url,
            details_url=details_url,
            published=now,
            attachment_name=f"720pier-{item_id}.torrent",
            download_url="pending",
            size=size,
            seeders=seeders,
            peers=peers,
        ))
    return items


def parse_topic(html_data: bytes | str, item: FeedItem) -> FeedItem:
    parser = _TorrentRowParser()
    parser.feed(html_data.decode("utf-8", errors="replace") if isinstance(html_data, bytes) else html_data)
    if not parser.result:
        return replace(item, download_url=None)
    raw_download, size_title, row_text = parser.result
    download_url = urljoin(item.details_url, raw_download)
    exact_size = 0
    match = re.search(r"([\d ]+)\s+Bytes", size_title, re.I)
    if match:
        exact_size = int(match.group(1).replace(" ", ""))
    seeders_match = re.search(r"Seeders\s*(\d+)", row_text, re.I)
    peers_match = re.search(r"Leechers\s*(\d+)", row_text, re.I)
    return replace(
        item,
        download_url=download_url,
        size=exact_size,
        seeders=int(seeders_match.group(1)) if seeders_match else 0,
        peers=int(peers_match.group(1)) if peers_match else 0,
    )


def fetch_feed() -> list[FeedItem]:
    global _feed_time, _feed_items
    now = time.monotonic()
    with _feed_lock:
        if _feed_items and now - _feed_time < CACHE_SECONDS:
            return list(_feed_items)
        data, _ = _fetch(FORUM_URL)
        try:
            parsed = parse_forum(data)
        except Exception as exc:
            if _feed_items:
                return list(_feed_items)
            raise HTTPException(status_code=502, detail=f"Invalid 720pier forum page: {exc}") from exc
        _feed_items, _feed_time = parsed, now
        return list(parsed)


def resolve_item(item: FeedItem) -> FeedItem:
    now = time.monotonic()
    with _detail_lock:
        cached = _detail_cache.get(item.item_id)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
    data, _ = _fetch(item.details_url)
    resolved = parse_topic(data, item)
    with _detail_lock:
        _detail_cache[item.item_id] = (now, resolved)
    return resolved


def resolve_item_safe(item: FeedItem) -> FeedItem:
    """A single slow topic must not fail the complete Torznab search."""
    try:
        return resolve_item(item)
    except HTTPException as exc:
        logger.warning("Could not resolve topic %s: %s", item.details_url, exc.detail)
        return item


def _terms(value: str) -> list[str]:
    return [term for term in re.findall(r"[^\W_]+", value.casefold()) if term not in IGNORED_SEARCH_TERMS]


def _safe_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _check_key(apikey: str | None) -> None:
    if API_KEY and apikey != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _caps() -> bytes:
    root = ET.Element("caps")
    ET.SubElement(root, "server", title="720pier Adapter", version="1.0")
    ET.SubElement(root, "limits", max="100", default="100")
    searching = ET.SubElement(root, "searching")
    ET.SubElement(searching, "search", available="yes", supportedParams="q")
    ET.SubElement(searching, "tv-search", available="yes", supportedParams="q,season,ep")
    categories = ET.SubElement(root, "categories")
    ET.SubElement(categories, "category", id="5000", name="TV")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _proxy_url(item: FeedItem) -> str:
    suffix = f"?apikey={quote(API_KEY)}" if API_KEY else ""
    return f"{BASE_URL}/download/{item.item_id}{suffix}"


def _rss(items: list[FeedItem], offset: int, total: int) -> bytes:
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "720pier Adapter"
    ET.SubElement(channel, "description").text = "Torznab view of the 720pier NFL feed"
    ET.SubElement(channel, "link").text = BASE_URL
    ET.SubElement(channel, f"{{{TORZNAB}}}response", offset=str(offset), total=str(total))
    for item in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = item.title
        ET.SubElement(node, "guid", isPermaLink="false").text = item.guid
        ET.SubElement(node, "link").text = item.details_url
        ET.SubElement(node, "comments").text = item.details_url
        ET.SubElement(node, "pubDate").text = item.published
        ET.SubElement(node, "category").text = "TV"
        if item.download_url:
            ET.SubElement(node, "enclosure", url=_proxy_url(item), length=str(item.size), type="application/x-bittorrent")
        for name, value in (("category", "5000"), ("size", str(item.size)), ("seeders", str(item.seeders)), ("peers", str(item.peers)), ("downloadvolumefactor", "0"), ("uploadvolumefactor", "1")):
            ET.SubElement(node, f"{{{TORZNAB}}}attr", name=name, value=value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api")
def api(
    t: str = Query("search"), q: str = Query(""), apikey: str | None = Query(None),
    limit: str | None = Query("100"), offset: str | None = Query("0"), id: str | None = Query(None),
    season: str | None = Query(None), ep: str | None = Query(None),
) -> Response:
    _check_key(apikey)
    action = t.lower()
    if action == "caps":
        return Response(_caps(), media_type="application/xml")
    base_items = fetch_feed()
    if action == "details":
        selected = [item for item in base_items if item.item_id == id or item.guid == id]
    elif action in {"search", "tvsearch"}:
        terms = _terms(" ".join(part for part in (q, season, ep) if part))
        selected = [item for item in base_items if all(term in item.title.casefold() for term in terms)]
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported Torznab function: {t}")
    page_offset = _safe_int(offset, 0, 0, 1_000_000)
    page_limit = _safe_int(limit, 100, 1, 100)
    page = selected[page_offset:page_offset + page_limit]
    return Response(_rss(page, page_offset, len(selected)), media_type="application/xml")


@app.get("/download/{item_id}")
def download(item_id: str, apikey: str | None = Query(None)) -> Response:
    _check_key(apikey)
    item = next((entry for entry in fetch_feed() if entry.item_id == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Unknown item")
    resolved = resolve_item(item)
    if not resolved.download_url:
        raise HTTPException(status_code=404, detail="Torrent attachment not found")
    data, content_type = _fetch(resolved.download_url)
    if data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise HTTPException(status_code=401, detail="720pier login cookie is missing or expired")
    filename = re.sub(r'[^A-Za-z0-9._-]+', '_', resolved.attachment_name)
    return Response(data, media_type=content_type or "application/x-bittorrent", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
