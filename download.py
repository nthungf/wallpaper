#!/usr/bin/env python3
"""Fetch Wallhaven search metadata and queue image links for review."""

import argparse
import json
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
API = "https://wallhaven.cc/api/v1/search"
WALLPAPER_API = "https://wallhaven.cc/api/v1/w"
HEADERS = {"User-Agent": "personal-wallpaper-downloader/1.0"}
IMAGE_NAME = re.compile(r"^wallhaven-([a-zA-Z0-9]+)\.(?:jpg|jpeg|png|webp)$", re.I)
RESOLUTION = re.compile(r"^[1-9]\d{2,4}x[1-9]\d{2,4}$")
SORTING_OPTIONS = {"date_added", "relevance", "random", "views", "favorites", "toplist"}
TOP_RANGES = {"1d", "3d", "1w", "1M", "3M", "6M", "1y"}


def get(url):
    request = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code != 521 or attempt == 2:
                if error.code == 521:
                    raise RuntimeError(
                        "Wallhaven is temporarily unavailable (HTTP 521: its server "
                        "refused Cloudflare's connection). Please try again in a few minutes."
                    ) from error
                raise
            time.sleep(2 * (attempt + 1))


def known_ids(root):
    found = set()
    for folder in ("candidates", "approved", "rejected"):
        directory = root / folder
        if directory.exists():
            for path in directory.iterdir():
                match = IMAGE_NAME.fullmatch(path.name)
                if path.is_file() and match:
                    found.add(match.group(1))
    with database(root) as connection:
        found.update(row[0] for row in connection.execute("SELECT id FROM images"))
    return found


@contextmanager
def database(root):
    root = Path(root)
    connection = sqlite3.connect(root / "catalog.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS images (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            image_url TEXT,
            thumb_url TEXT,
            page_url TEXT,
            views INTEGER,
            favorites INTEGER,
            captured_at TEXT,
            status TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            tags_fetched_at TEXT,
            api_data TEXT NOT NULL DEFAULT '{}'
        )""")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(images)")}
        if "tags" not in columns:
            connection.execute("ALTER TABLE images ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "tags_fetched_at" not in columns:
            connection.execute("ALTER TABLE images ADD COLUMN tags_fetched_at TEXT")
        if "api_data" not in columns:
            connection.execute("ALTER TABLE images ADD COLUMN api_data TEXT NOT NULL DEFAULT '{}'")
        connection.execute("""CREATE TABLE IF NOT EXISTS review_times (
            name TEXT PRIMARY KEY,
            bucket TEXT NOT NULL,
            decided_at TEXT NOT NULL
        )""")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def catalog_rows(root, status):
    with database(root) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM images WHERE status=? ORDER BY name", (status,))]


def catalog_all(root):
    with database(root) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM images")]


def catalog_record(root, name):
    with database(root) as connection:
        row = connection.execute("SELECT * FROM images WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def normalize_tags(values):
    tags = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            continue
        name = value["name"].strip()
        if not name:
            continue
        tag = {"name": name}
        for key in ("alias", "category"):
            if isinstance(value.get(key), str) and value[key].strip():
                tag[key] = value[key].strip()
        if type(value.get("id")) is int:
            tag["id"] = value["id"]
        tags.append(tag)
    return tags


def missing_tag_count(root):
    with database(root) as connection:
        return connection.execute("""SELECT COUNT(*) FROM images
            WHERE tags_fetched_at IS NULL""").fetchone()[0]


def refresh_missing_tags(root, progress=None, stop=None, *, interval=1.5):
    """Fetch and store the complete Wallhaven detail response for older catalog rows."""
    with database(root) as connection:
        rows = [tuple(row) for row in connection.execute(
            "SELECT id FROM images WHERE tags_fetched_at IS NULL ORDER BY captured_at, id")]
    target = len(rows)
    completed = failed = 0
    last_request = 0.0
    for (image_id,) in rows:
        if stop and stop.is_set():
            break
        delay = interval - (time.monotonic() - last_request)
        if delay > 0:
            if stop:
                if stop.wait(delay):
                    break
            else:
                time.sleep(delay)
        tags = []
        fetched_at = None
        api_data = None
        try:
            response = json.loads(get(f"{WALLPAPER_API}/{urllib.parse.quote(image_id)}"))
            info = response.get("data", {})
            tags = normalize_tags(info.get("tags"))
            fetched_at = datetime.now(timezone.utc).isoformat()
            api_data = json.dumps({"detail": response}, ensure_ascii=False)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            failed += 1
            message = f"Fetching tags: {image_id} failed ({error})"
        else:
            message = f"Fetched tags for {image_id}"
        last_request = time.monotonic()
        if fetched_at:
            with database(root) as connection:
                connection.execute("""UPDATE images SET tags=?, tags_fetched_at=?, api_data=?
                    WHERE id=?""",
                    (json.dumps(tags, ensure_ascii=False), fetched_at, api_data, image_id))
        completed += 1
        if progress:
            progress(completed, target, message)
    return {"completed": completed, "failed": failed, "target": target}


def set_catalog_status(root, image_id, status):
    with database(root) as connection:
        connection.execute("UPDATE images SET status=? WHERE id=?", (status, image_id))


def review_times(root):
    with database(root) as connection:
        return {row["name"]: dict(row) for row in connection.execute(
            "SELECT name, bucket, decided_at FROM review_times")}


def record_review(root, name, bucket=None, *, replace=True):
    with database(root) as connection:
        if bucket is None:
            connection.execute("DELETE FROM review_times WHERE name=?", (name,))
        elif replace:
            connection.execute("""INSERT INTO review_times (name, bucket, decided_at)
                VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET
                bucket=excluded.bucket, decided_at=excluded.decided_at""",
                (name, bucket, datetime.now(timezone.utc).isoformat()))
        else:
            connection.execute("""INSERT OR IGNORE INTO review_times (name, bucket, decided_at)
                VALUES (?, ?, ?)""", (name, bucket, datetime.now(timezone.utc).isoformat()))


def mark_cleaned(root, image_id, name):
    with database(root) as connection:
        connection.execute("""INSERT INTO images (id, name, status) VALUES (?, ?, 'cleaned')
            ON CONFLICT(id) DO UPDATE SET status='cleaned'""", (image_id, name))


def download_batch(count, query="", atleast="1920x1080", *, root=None, progress=None, stop=None,
                   max_views=None, discovery="random", order="desc", top_range="1M",
                   search_interval=1.5):
    """Fetch search results and save up to count unseen image links; return the count."""
    if not isinstance(count, int) or count < 1 or count > 2000:
        raise ValueError("Count must be between 1 and 2000")
    if not RESOLUTION.fullmatch(atleast):
        raise ValueError("Resolution must look like 1920x1080")
    if not isinstance(query, str) or len(query) > 100:
        raise ValueError("Search term must be at most 100 characters")
    if max_views is not None and (type(max_views) is not int or max_views < 0):
        raise ValueError("Maximum views must be zero or greater")
    if discovery == "least_viewed":
        discovery, order = "views", "asc"
    if discovery not in SORTING_OPTIONS or order not in {"asc", "desc"} or top_range not in TOP_RANGES:
        raise ValueError("Invalid discovery, order, or toplist range")

    root = Path(root) if root is not None else HERE
    seen = known_ids(root)
    topics = list(dict.fromkeys(part.strip() for part in query.split(",") if part.strip())) or [""]
    searches_by_topic = [{"query": topic, "page": 1, "seed": None, "buffer": [],
                          "last_page": 1, "added": 0, "exhausted": False} for topic in topics]
    added = 0
    tag_failures = 0
    searches = 0
    max_searches = max(8 * len(topics), ((count + 23) // 24) * 5)
    topic_index = 0
    topic_limit = (count + len(topics) - 1) // len(topics)
    last_request = 0.0

    def update(message):
        if progress:
            progress(added, count, message)

    def fetch_api(url):
        nonlocal last_request
        delay = search_interval - (time.monotonic() - last_request)
        if delay > 0:
            if stop:
                if stop.wait(delay):
                    return None
            else:
                time.sleep(delay)
        result = json.loads(get(url))
        last_request = time.monotonic()
        return result

    update("Fetching Wallhaven search results…")
    while added < count:
        if stop and stop.is_set():
            update(f"Stopped after saving {added} image links")
            return added
        if all(topic["exhausted"] for topic in searches_by_topic):
            break
        if not any(not topic["exhausted"] and topic["added"] < topic_limit
                   for topic in searches_by_topic):
            topic_limit += (count + len(topics) - 1) // len(topics)
        while (searches_by_topic[topic_index]["exhausted"]
               or searches_by_topic[topic_index]["added"] >= topic_limit):
            topic_index = (topic_index + 1) % len(topics)
        topic = searches_by_topic[topic_index]
        topic_index = (topic_index + 1) % len(topics)
        if not topic["buffer"]:
            if searches >= max_searches:
                topic["exhausted"] = True
                continue
            if searches and topic["page"] > topic["last_page"]:
                if discovery != "random":
                    topic["exhausted"] = True
                    continue
                topic["page"], topic["seed"] = 1, None
            params = {"q": topic["query"], "categories": "100", "purity": "100",
                      "sorting": discovery,
                      "atleast": atleast, "page": topic["page"]}
            if discovery != "random":
                params["order"] = order
            if discovery == "toplist":
                params["topRange"] = top_range
            if topic["seed"] and discovery == "random":
                params["seed"] = topic["seed"]
            result = fetch_api(f"{API}?{urllib.parse.urlencode(params)}")
            if result is None:
                update(f"Stopped after saving {added} image links")
                return added
            searches += 1
            data = result.get("data", [])
            meta = result.get("meta", {})
            if not data:
                topic["exhausted"] = True
                continue
            topic["seed"] = meta.get("seed") or topic["seed"]
            topic["last_page"] = max(1, int(meta.get("last_page") or 1))
            random.shuffle(data)
            topic["buffer"] = data
            topic["page"] += 1
        while (topic["buffer"] and added < count and topic["added"] < topic_limit
               and not (stop and stop.is_set())):
            image = topic["buffer"].pop()
            image_id = image.get("id", "")
            if not re.fullmatch(r"[a-zA-Z0-9]+", image_id) or image_id in seen:
                continue
            views = image.get("views")
            favorites = image.get("favorites")
            if max_views is not None and (type(views) is not int or views > max_views):
                continue
            source_url = image.get("path", "")
            thumb_url = (image.get("thumbs") or {}).get("original", "")
            suffix = Path(urllib.parse.urlparse(source_url).path).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            full_parts = urllib.parse.urlparse(source_url)
            thumb_parts = urllib.parse.urlparse(thumb_url)
            if (full_parts.scheme != "https" or full_parts.hostname != "w.wallhaven.cc"
                    or thumb_parts.scheme != "https" or thumb_parts.hostname != "th.wallhaven.cc"):
                continue
            page_url = image.get("url") or f"https://wallhaven.cc/w/{image_id}"
            page_parts = urllib.parse.urlparse(page_url)
            if page_parts.scheme != "https" or page_parts.hostname != "wallhaven.cc":
                page_url = f"https://wallhaven.cc/w/{image_id}"
            filename = f"wallhaven-{image_id}{suffix}"
            with database(root) as connection:
                inserted = connection.execute("""INSERT OR IGNORE INTO images
                    (id, name, image_url, thumb_url, page_url, views, favorites, captured_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidates')""",
                    (image_id, filename, source_url, thumb_url, page_url, views, favorites,
                     datetime.now(timezone.utc).isoformat())).rowcount
            seen.add(image_id)
            if not inserted:
                continue
            update(f"Fetching full metadata for {image_id}…")
            tags = normalize_tags(image.get("tags"))
            tags_fetched_at = None
            detail = None
            try:
                detail = fetch_api(f"{WALLPAPER_API}/{urllib.parse.quote(image_id)}")
                if detail is None:
                    detail = None
                else:
                    tags = normalize_tags((detail.get("data") or {}).get("tags")) or tags
                    tags_fetched_at = datetime.now(timezone.utc).isoformat()
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                tag_failures += 1
            api_data = {"search": image}
            if detail is not None:
                api_data["detail"] = detail
            added += 1
            topic["added"] += 1
            with database(root) as connection:
                connection.execute("""UPDATE images SET tags=?, tags_fetched_at=?, api_data=?
                    WHERE id=?""",
                    (json.dumps(tags, ensure_ascii=False), tags_fetched_at,
                     json.dumps(api_data, ensure_ascii=False), image_id))
            update(f"Saved {added} of {count} image links")

    suffix = f"; {tag_failures} tag lookups need retry" if tag_failures else ""
    update(f"Search complete: saved {added} of {count} image links{suffix}; no wallpaper files saved")
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1, help="new image links to queue (default: 1)")
    parser.add_argument("--query", default="", help="comma-separated topics, e.g. nature,city")
    parser.add_argument("--atleast", default="1920x1080", help="minimum resolution")
    parser.add_argument("--max-views", type=int, help="skip images above this view count")
    parser.add_argument("--sorting", choices=sorted(SORTING_OPTIONS), default="random")
    parser.add_argument("--order", choices=("asc", "desc"), default="desc")
    parser.add_argument("--top-range", choices=sorted(TOP_RANGES), default="1M")
    parser.add_argument("--least-viewed", action="store_true", help="search least viewed first")
    args = parser.parse_args()
    try:
        added = download_batch(args.count, args.query, args.atleast,
                               max_views=args.max_views,
                               discovery="least_viewed" if args.least_viewed else args.sorting,
                               order=args.order, top_range=args.top_range,
                               progress=lambda done, total, message: print(message, flush=True))
    except (OSError, ValueError, KeyError) as error:
        print(f"Search failed: {error}", file=sys.stderr)
        return 1
    return 0 if added == args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
