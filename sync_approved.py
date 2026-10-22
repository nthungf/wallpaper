#!/usr/bin/env python3
"""Export approved Wallhaven links or sync them onto another machine."""

import argparse
import contextlib
import re
import sqlite3
import sys
import time
import urllib.parse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parent
IMAGE_NAME = re.compile(r"^wallhaven-([a-zA-Z0-9]+)\.(?:jpg|jpeg|png|webp)$", re.I)
IMAGE_HEADERS = {
    ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff",
    ".png": b"\x89PNG\r\n\x1a\n", ".webp": b"RIFF",
}


@contextlib.contextmanager
def catalog_database(root):
    connection = sqlite3.connect(Path(root) / "catalog.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS images (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, image_url TEXT, thumb_url TEXT,
            page_url TEXT, views INTEGER, favorites INTEGER, captured_at TEXT,
            status TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS review_times (
            name TEXT PRIMARY KEY, bucket TEXT NOT NULL, decided_at TEXT NOT NULL
        )""")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fetch(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": "personal-wallpaper-sync/1.0",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def valid_image_payload(payload, suffix):
    if suffix in {".jpg", ".jpeg", ".png"}:
        return payload.startswith(IMAGE_HEADERS[suffix])
    return (suffix == ".webp" and len(payload) >= 12
            and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")


def approved_rows(root):
    with catalog_database(root) as connection:
        times = {row["name"]: dict(row) for row in connection.execute(
            "SELECT name, bucket, decided_at FROM review_times")}
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM images WHERE status='approved' AND image_url IS NOT NULL ORDER BY name")]
    output = []
    for row in rows:
        match = IMAGE_NAME.fullmatch(row["name"])
        image_url = urllib.parse.urlparse(row["image_url"] or "")
        page_url = urllib.parse.urlparse(row.get("page_url") or "")
        thumb_url = urllib.parse.urlparse(row.get("thumb_url") or "")
        if (not match or match.group(1).lower() != row["id"].lower()
                or image_url.scheme != "https" or image_url.hostname != "w.wallhaven.cc"
                or Path(image_url.path).suffix.lower() != Path(row["name"]).suffix.lower()):
            continue
        row["page_url"] = row["page_url"] if (
            page_url.scheme == "https" and page_url.hostname == "wallhaven.cc"
        ) else f"https://wallhaven.cc/w/{row['id']}"
        row["thumb_url"] = row["thumb_url"] if (
            thumb_url.scheme == "https" and thumb_url.hostname == "th.wallhaven.cc"
        ) else None
        output.append({**row, "approved_at": times.get(row["name"], {}).get("decided_at")
                       or datetime.now(timezone.utc).isoformat()})
    return output


def export_bundle(root, destination):
    root, destination = Path(root), Path(destination)
    if destination.resolve() == (root / "catalog.sqlite").resolve():
        raise ValueError("Choose a separate path for the distributable database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".part")
    temp.unlink(missing_ok=True)
    rows = approved_rows(root)
    try:
        with sqlite3.connect(temp) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("CREATE TABLE bundle_info (schema_version INTEGER NOT NULL)")
            connection.execute("INSERT INTO bundle_info VALUES (?)", (SCHEMA_VERSION,))
            connection.execute("""CREATE TABLE approved_images (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, image_url TEXT NOT NULL,
                thumb_url TEXT, page_url TEXT, views INTEGER, favorites INTEGER,
                captured_at TEXT, approved_at TEXT NOT NULL
            )""")
            connection.executemany("""INSERT INTO approved_images
                (id, name, image_url, thumb_url, page_url, views, favorites, captured_at, approved_at)
                VALUES (:id, :name, :image_url, :thumb_url, :page_url, :views, :favorites,
                        :captured_at, :approved_at)""", rows)
        temp.replace(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return len(rows)


def read_bundle(path):
    bundle = Path(path).resolve()
    if not bundle.is_file():
        raise ValueError(f"Bundle not found: {bundle}")
    uri = bundle.as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            connection.row_factory = sqlite3.Row
            if "bundle_info" in tables:
                info = connection.execute("SELECT schema_version FROM bundle_info").fetchone()
                if not info or info[0] != SCHEMA_VERSION:
                    raise ValueError("Unsupported approved links database version")
                columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(approved_images)")}
                required = {"id", "name", "image_url", "thumb_url", "page_url", "views",
                            "favorites", "captured_at", "approved_at"}
                if not required <= columns:
                    raise ValueError("Approved links database is missing required columns")
                return [dict(row) for row in connection.execute(
                    "SELECT * FROM approved_images ORDER BY name")]
            if "images" in tables:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(images)")}
                required = {"id", "name", "image_url", "thumb_url", "page_url", "views",
                            "favorites", "captured_at", "status"}
                if not required <= columns:
                    raise ValueError("Wallpaper catalog is missing required columns")
                times_join = "LEFT JOIN review_times t ON t.name=i.name" if "review_times" in tables else ""
                reviewed = "t.decided_at" if "review_times" in tables else "NULL"
                query = f"""SELECT i.id, i.name, i.image_url, i.thumb_url, i.page_url,
                    i.views, i.favorites, i.captured_at, COALESCE({reviewed}, ?) AS approved_at
                    FROM images i {times_join} WHERE i.status='approved'
                    AND i.image_url IS NOT NULL ORDER BY i.name"""
                now = datetime.now(timezone.utc).isoformat()
                return [dict(row) for row in connection.execute(query, (now,))]
            raise ValueError("Database has no approved links or wallpaper catalog")
    except sqlite3.DatabaseError as error:
        raise ValueError(f"Invalid approved links database: {error}") from error


def validate_row(row):
    match = IMAGE_NAME.fullmatch(row.get("name", ""))
    image_url = urllib.parse.urlparse(row.get("image_url") or "")
    page_url = urllib.parse.urlparse(row.get("page_url") or "")
    thumb_url = urllib.parse.urlparse(row.get("thumb_url") or "")
    if (not re.fullmatch(r"[A-Za-z0-9]+", str(row.get("id", ""))) or not match
            or match.group(1).lower() != row["id"].lower()
            or image_url.scheme != "https" or image_url.hostname != "w.wallhaven.cc"
            or Path(image_url.path).suffix.lower() != Path(row["name"]).suffix.lower()
            or page_url.scheme != "https" or page_url.hostname != "wallhaven.cc"
            or (row.get("thumb_url") and
                (thumb_url.scheme != "https" or thumb_url.hostname != "th.wallhaven.cc"))):
        raise ValueError(f"Invalid approved image record: {row.get('name', '(unnamed)')}")


def sync_rows(rows, root=ROOT, delay=0.5, progress=None, stop=None):
    if delay < 0:
        raise ValueError("Delay must be zero or greater")
    root = Path(root)
    approved_dir = root / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    complete, errors = [], []
    last_download = 0.0
    stopped = False
    for index, row in enumerate(rows):
        if stop and stop.is_set():
            stopped = True
            break
        try:
            validate_row(row)
            target = approved_dir / row["name"]
            existed = target.is_file()
            if not existed:
                wait = delay - (time.monotonic() - last_download)
                if last_download and wait > 0:
                    if stop and stop.wait(wait):
                        stopped = True
                        break
                    elif not stop:
                        time.sleep(wait)
                payload = fetch(row["image_url"])
                last_download = time.monotonic()
                if not valid_image_payload(payload, target.suffix.lower()):
                    raise ValueError("Downloaded data is not a valid image of the expected type")
                temp = target.with_suffix(target.suffix + ".part")
                try:
                    temp.write_bytes(payload)
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)
            complete.append(row)
            message = f"Already local: {row['name']}" if existed else f"Downloaded {row['name']}"
        except Exception as error:
            errors.append((row.get("name", "(unnamed)"), str(error)))
            message = f"Failed {row.get('name', '(unnamed)')}: {error}"
        if progress:
            progress(index + 1, len(rows), message)

    if stop and stop.is_set():
        stopped = True
    removed = 0
    if not errors and not stopped:
        keep = {row["name"] for row in complete}
        for path in approved_dir.iterdir():
            if not path.is_file() or not IMAGE_NAME.fullmatch(path.name) or path.name in keep:
                continue
            path.unlink()
            removed += 1
            if progress:
                progress(len(rows), len(rows), f"Removed no-longer-approved image: {path.name}")
    return complete, errors, removed, stopped


def save_synced_rows(root, complete):
    with catalog_database(root) as connection:
        for row in complete:
            connection.execute("""INSERT INTO images
                (id, name, image_url, thumb_url, page_url, views, favorites, captured_at, status)
                VALUES (:id, :name, :image_url, :thumb_url, :page_url, :views, :favorites,
                        :captured_at, 'approved')
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, image_url=excluded.image_url,
                    thumb_url=excluded.thumb_url, page_url=excluded.page_url, views=excluded.views,
                    favorites=excluded.favorites, status='approved'""", row)
            connection.execute("""INSERT INTO review_times (name, bucket, decided_at)
                VALUES (?, 'approved', ?) ON CONFLICT(name) DO UPDATE SET
                bucket='approved', decided_at=excluded.decided_at""",
                (row["name"], row["approved_at"] or datetime.now(timezone.utc).isoformat()))


def sync_bundle(bundle_path, root=ROOT, delay=0.5, progress=None, stop=None):
    root = Path(root)
    rows = read_bundle(bundle_path)
    complete, errors, removed, stopped = sync_rows(rows, root, delay, progress, stop)
    save_synced_rows(root, complete)
    if not progress:
        print(f"Finished: {len(complete)} approved images available; {removed} removed; {len(errors)} failed")
    return 1 if errors or stopped else 0


def sync_catalog(root=ROOT, delay=0.5, progress=None, stop=None):
    """Sync the current local approved catalog into its approved folder."""
    root = Path(root)
    rows = approved_rows(root)
    complete, errors, removed, stopped = sync_rows(rows, root, delay, progress, stop)
    save_synced_rows(root, complete)
    return {"completed": len(complete), "failed": len(errors), "removed": removed,
            "target": len(rows), "stopped": stopped, "errors": errors}


def rows_for_review_sync(root=ROOT):
    """Prefer live local approvals, or the portable bundle on a receiving machine."""
    root = Path(root)
    bundle = root / "approved-links.sqlite"
    if (root / ".approved-links-sync-mode").is_file() and bundle.is_file():
        return read_bundle(bundle)
    rows = approved_rows(root)
    if rows:
        return rows
    return read_bundle(bundle) if bundle.is_file() else rows


def sync_for_review(root=ROOT, delay=0.5, progress=None, stop=None):
    """Sync the review catalog, falling back to the portable bundle when needed."""
    root = Path(root)
    bundle = root / "approved-links.sqlite"
    local_rows = approved_rows(root)
    bundle_mode = ((root / ".approved-links-sync-mode").is_file() and bundle.is_file())
    if bundle_mode or (not local_rows and bundle.is_file()):
        rows = read_bundle(bundle)
        bundle_mode = True
    else:
        rows = local_rows
    complete, errors, removed, stopped = sync_rows(rows, root, delay, progress, stop)
    save_synced_rows(root, complete)
    if bundle_mode and not errors and not stopped:
        (root / ".approved-links-sync-mode").touch()
    return {"completed": len(complete), "failed": len(errors), "removed": removed,
            "target": len(rows), "stopped": stopped, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="create a portable approved links database")
    export.add_argument("destination", type=Path)
    export.add_argument("--root", type=Path, default=ROOT, help="wallpaper data directory")
    sync = commands.add_parser("sync", help="download approved images from a portable database")
    sync.add_argument("bundle", type=Path)
    sync.add_argument("--root", type=Path, default=ROOT, help="wallpaper data directory")
    sync.add_argument("--delay", type=float, default=0.5, help="seconds between image downloads")
    args = parser.parse_args()
    try:
        if args.command == "export":
            count = export_bundle(args.root, args.destination)
            print(f"Exported {count} approved image links to {args.destination}")
            return 0
        return sync_bundle(args.bundle, args.root, args.delay)
    except (OSError, sqlite3.DatabaseError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
