#!/usr/bin/env python3
"""Local browser interface for reviewing desktop wallpapers."""

import argparse
import io
import json
import mimetypes
import queue
import secrets
import subprocess
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageOps
import download
import sync_approved


ROOT = Path(__file__).resolve().parent
BUCKETS = ("candidates", "approved", "rejected")
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MOVE_LOCK = threading.Lock()


class DownloadJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state = {"status": "idle", "added": 0, "target": 0, "message": "Ready"}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, count, query, atleast, max_views, discovery, order, top_range):
        with self.lock:
            if self.state["status"] == "running":
                return False
            self.stop_event = threading.Event()
            self.state = {"status": "running", "added": 0, "target": count,
                          "message": "Starting search…"}
        threading.Thread(target=self._run,
                         args=(count, query, atleast, max_views, discovery, order, top_range),
                         daemon=True).start()
        return True

    def start_tag_refresh(self):
        with self.lock:
            if self.state["status"] == "running":
                return False
            target = download.missing_tag_count(ROOT)
            if target == 0:
                self.state = {"status": "complete", "added": 0, "target": 0,
                              "message": "All catalog links already have saved API metadata"}
                return True
            self.stop_event = threading.Event()
            self.state = {"status": "running", "added": 0, "target": target,
                          "message": "Fetching full API metadata for saved links…", "kind": "tags"}
        threading.Thread(target=self._run_tag_refresh, daemon=True).start()
        return True

    def start_sync(self):
        with self.lock:
            if self.state["status"] == "running":
                return False
            target = len(sync_approved.rows_for_review_sync(ROOT))
            self.stop_event = threading.Event()
            self.state = {"status": "running", "added": 0, "target": target,
                          "message": "Syncing approved image files…", "kind": "sync"}
        threading.Thread(target=self._run_sync, daemon=True).start()
        return True

    def _run_sync(self):
        def progress(done, target, message):
            with self.lock:
                self.state.update(added=done, target=target, message=message)
        try:
            result = sync_approved.sync_for_review(ROOT, progress=progress, stop=self.stop_event)
            message = (f"Approved sync finished: {result['completed']} available; "
                       f"{result['removed']} removed; {result['failed']} failed")
            if result["stopped"]:
                message = f"Approved sync stopped after {result['completed']} images; local files were kept"
            with self.lock:
                status = "stopped" if result["stopped"] else "complete"
                self.state.update(status=status, message=message, added=result["completed"],
                                  failed=result["failed"])
        except Exception as error:
            with self.lock:
                self.state.update(status="error", message=f"Approved sync failed: {error}")

    def _run_tag_refresh(self):
        def progress(done, target, message):
            with self.lock:
                self.state.update(added=done, target=target, message=message)
        try:
            result = download.refresh_missing_tags(ROOT, progress=progress, stop=self.stop_event)
            failed = result["failed"]
            message = f"Fetched API metadata for {result['completed'] - failed} of {result['target']} saved links"
            if failed:
                message += f"; {failed} failed and can be retried"
            with self.lock:
                status = "stopped" if self.stop_event.is_set() else "complete"
                self.state.update(status=status, message=message, added=result["completed"])
        except Exception as error:
            with self.lock:
                self.state.update(status="error", message=f"Metadata lookup failed: {error}")

    def stop(self):
        with self.lock:
            if self.state["status"] != "running":
                return False
            self.stop_event.set()
            self.state["message"] = "Stopping after the current request…"
            return True

    def _run(self, count, query, atleast, max_views, discovery, order, top_range):
        def progress(added, target, message):
            with self.lock:
                self.state.update(added=added, target=target, message=message)
        try:
            added = download.download_batch(count, query, atleast, root=ROOT,
                                             progress=progress, stop=self.stop_event,
                                             max_views=max_views, discovery=discovery,
                                             order=order, top_range=top_range)
            with self.lock:
                self.state.update(status="stopped" if self.stop_event.is_set() else "complete",
                                  added=added)
        except Exception as error:
            with self.lock:
                self.state.update(status="error", message=f"Search failed: {error}")


class BatchJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state = {"status": "idle", "done": 0, "target": 0, "failed": [], "message": "Ready"}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, bucket, action, selections):
        with self.lock:
            if self.state["status"] == "running":
                return False
            self.stop_event = threading.Event()
            self.state = {"status": "running", "done": 0, "target": len(selections),
                          "failed": [], "message": f"Processing {len(selections)} images…"}
        threading.Thread(target=self._run, args=(bucket, action, selections), daemon=True).start()
        return True

    def stop(self):
        with self.lock:
            if self.state["status"] != "running":
                return False
            self.stop_event.set()
            self.state["message"] = "Stopping after the current image…"
            return True

    def _run(self, bucket, action, selections):
        failures = []
        for selection in selections:
            if self.stop_event.is_set():
                break
            try:
                process_selection(bucket, action, selection)
            except Exception as error:
                failures.append({"name": selection["name"], "error": str(error)})
            with self.lock:
                self.state.update(done=self.state["done"] + 1, failed=failures.copy(),
                                  message=f"Processed {self.state['done'] + 1} of {len(selections)}")
        with self.lock:
            done = self.state["done"]
            self.state.update(status="stopped" if self.stop_event.is_set() else "complete",
                              message=f"Processed {done - len(failures)} of {len(selections)}; {len(failures)} failed")


class DecisionQueue:
    """Save approved remote images without making review wait for the download."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.lock = threading.Lock()
        self.pending = 0
        self.failures = []
        self.failed_total = 0
        threading.Thread(target=self._run, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return {"pending": self.pending, "failed_total": self.failed_total,
                    "failures": self.failures[-10:]}

    def submit(self, bucket, action, selection):
        if bucket == "candidates" and action == "approve":
            if not selection["remote"]:
                process_selection(bucket, action, selection)
                return False
            with self.lock:
                record = download.catalog_record(ROOT, selection["name"])
                if not record or record["status"] != bucket or not record["image_url"]:
                    raise ValueError("Preview is no longer in this folder")
                local_bucket = next((folder for folder in BUCKETS
                                     if (ROOT / folder / selection["name"]).is_file()), None)
                if local_bucket:
                    if local_bucket == "candidates":
                        process_selection(bucket, action, {**selection, "remote": False})
                        return False
                    download.set_catalog_status(ROOT, record["id"], local_bucket)
                    download.record_review(ROOT, selection["name"],
                                           local_bucket if local_bucket in {"approved", "rejected"} else None,
                                           replace=False)
                    return {"queued": False, "stale": True, "bucket": local_bucket}
                # Hide the candidate now, while the original is fetched in the worker.
                download.set_catalog_status(ROOT, record["id"], "approving")
                download.record_review(ROOT, selection["name"], "approved")
                self.pending += 1
                self.jobs.put((bucket, action, selection))
            return True
        process_selection(bucket, action, selection)
        return False

    def _run(self):
        while True:
            bucket, action, selection = self.jobs.get()
            try:
                process_selection(bucket, action, selection)
            except Exception as error:
                record = download.catalog_record(ROOT, selection["name"])
                if record and record["status"] == "approving":
                    download.set_catalog_status(ROOT, record["id"], "candidates")
                    download.record_review(ROOT, selection["name"])
                with self.lock:
                    self.failed_total += 1
                    self.failures.append({"name": selection["name"], "error": str(error)})
            finally:
                with self.lock:
                    self.pending -= 1
                self.jobs.task_done()


def process_selection(bucket, action, selection):
    name = selection["name"]
    remote = selection["remote"]
    if remote:
        record = download.catalog_record(ROOT, name)
        if not record or (record["status"] != bucket and not (
                bucket == "candidates" and action == "approve" and record["status"] == "approving"
        )) or not record["image_url"]:
            raise ValueError("Preview is no longer in this folder")
        if any((ROOT / folder / name).exists() for folder in BUCKETS):
            raise ValueError("Image is stored locally; refresh the page")
        if action == "approve":
            target = ROOT / "approved" / name
            if target.exists():
                raise ValueError("Approved file already exists")
            url = urllib.parse.urlparse(record["image_url"])
            if url.scheme != "https" or url.hostname != "w.wallhaven.cc":
                raise ValueError("Unexpected image URL")
            payload = download.get(record["image_url"])
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
            temp = target.with_suffix(target.suffix + ".part")
            try:
                temp.write_bytes(payload)
                with MOVE_LOCK:
                    if target.exists():
                        raise ValueError("Approved file already exists")
                    temp.rename(target)
                    download.set_catalog_status(ROOT, record["id"], "approved")
                    download.record_review(ROOT, name, "approved", replace=False)
            finally:
                temp.unlink(missing_ok=True)
        else:
            new_status = "rejected" if action == "reject" else "candidates" if action == "restore" else "cleaned"
            download.set_catalog_status(ROOT, record["id"], new_status)
            download.record_review(ROOT, name, new_status if new_status == "rejected" else None)
        return

    source = ROOT / bucket / name
    if not source.is_file():
        raise ValueError("Local file is no longer in this folder")
    if action == "clean":
        result = subprocess.run(["gio", "trash", str(source)], capture_output=True, text=True,
                                timeout=30, check=False)
        if result.returncode:
            raise OSError(result.stderr.strip() or "Could not move file to Trash")
        match = download.IMAGE_NAME.fullmatch(name)
        if match:
            download.mark_cleaned(ROOT, match.group(1), name)
        download.record_review(ROOT, name)
        return
    destination = {"approve": "approved", "reject": "rejected", "restore": "candidates"}[action]
    target = ROOT / destination / name
    with MOVE_LOCK:
        if target.exists():
            raise ValueError("File already exists in destination")
        source.rename(target)
        match = download.IMAGE_NAME.fullmatch(name)
        if match:
            download.set_catalog_status(ROOT, match.group(1), destination)
        download.record_review(ROOT, name, destination if destination in {"approved", "rejected"} else None)


def valid_name(name):
    return (
        isinstance(name, str)
        and name == Path(name).name
        and name not in {".", ".."}
        and Path(name).suffix.lower() in EXTENSIONS
    )


def backfill_review_times(root=ROOT):
    """Give preexisting decisions a timestamp without changing later timestamps."""
    now = datetime.now(timezone.utc).isoformat()
    with download.database(root) as connection:
        for bucket in ("approved", "rejected"):
            names = {path.name for path in (root / bucket).iterdir()
                     if path.is_file() and valid_name(path.name)}
            names.update(row[0] for row in connection.execute(
                "SELECT name FROM images WHERE status=? AND image_url IS NOT NULL", (bucket,)))
            connection.executemany("""INSERT OR IGNORE INTO review_times
                (name, bucket, decided_at) VALUES (?, ?, ?)""",
                ((name, bucket, now) for name in names))


def stored_tags(record):
    try:
        return download.normalize_tags(json.loads(record.get("tags") or "[]"))
    except (TypeError, ValueError):
        return []


def stored_api_data(record):
    try:
        value = json.loads(record.get("api_data") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def items(bucket, tag_terms=()):
    links = source_links()
    legacy = image_metadata()
    catalog = download.catalog_all(ROOT)
    times = download.review_times(ROOT) if bucket in {"approved", "rejected"} else {}
    by_name = {record["name"]: record for record in catalog}
    entries = []
    for path in (ROOT / bucket).iterdir():
        if not path.is_file() or not valid_name(path.name):
            continue
        record = by_name.get(path.name, {})
        old = legacy.get(path.name, {})
        entries.append({"name": path.name, "remote": False,
                        "source": record.get("page_url") or links.get(path.name, ""),
                        "views": record.get("views", old.get("views")),
                        "favorites": record.get("favorites", old.get("favorites")),
                        "tags": stored_tags(record),
                        "api_data": stored_api_data(record),
                        "reviewed_at": times.get(path.name, {}).get("decided_at")})
    for record in catalog:
        if record["status"] != bucket:
            continue
        if not record["image_url"]:
            continue
        if any((ROOT / folder / record["name"]).exists() for folder in BUCKETS):
            continue
        thumb_url = record["thumb_url"] or ""
        # Older catalog rows used Wallhaven's cropped large thumbnail.
        if thumb_url.startswith("https://th.wallhaven.cc/lg/"):
            thumb_url = thumb_url.replace("https://th.wallhaven.cc/lg/",
                                          "https://th.wallhaven.cc/orig/", 1)
        entries.append({"name": record["name"], "remote": True,
                        "source": record["page_url"], "image_url": record["image_url"],
                        "thumb_url": thumb_url, "views": record["views"],
                        "favorites": record["favorites"],
                        "tags": stored_tags(record),
                        "api_data": stored_api_data(record),
                        "reviewed_at": times.get(record["name"], {}).get("decided_at")})
    if bucket in {"approved", "rejected"}:
        entries.sort(key=lambda entry: (entry["reviewed_at"] or "", entry["name"].lower()),
                     reverse=True)
    else:
        entries.sort(key=lambda entry: entry["name"].lower())
    if tag_terms:
        def matches(entry):
            values = [str(tag.get(key, "")).casefold()
                      for tag in entry["tags"] for key in ("name", "alias", "category")]
            return all(any(term in value for value in values) for term in tag_terms)
        entries = [entry for entry in entries if matches(entry)]
    return entries


def source_links():
    links = {}
    log = ROOT / "sources.tsv"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            name, separator, url = line.partition("\t")
            if separator and valid_name(name) and url.startswith("https://wallhaven.cc/w/"):
                links[name] = url
    return links


def image_metadata():
    values = {}
    log = ROOT / "metadata.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and valid_name(entry.get("name")):
                values[entry["name"]] = {"views": entry.get("views"),
                                         "favorites": entry.get("favorites"),
                                         "captured_at": entry.get("captured_at")}
    return values


@lru_cache(maxsize=256)
def thumbnail(path, mtime_ns):
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((480, 320), Image.Resampling.LANCZOS)
        if image.mode != "RGB":
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=80, optimize=True)
        return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        pass

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status=200):
        self.send_bytes(json.dumps(value).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            html = (ROOT / "review.html").read_text(encoding="utf-8")
            html = html.replace("__REVIEW_TOKEN__", self.server.review_token)
            self.send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/items":
            query = urllib.parse.parse_qs(parsed.query)
            bucket = query.get("bucket", ["candidates"])[0]
            if bucket not in BUCKETS:
                self.send_json({"error": "Invalid folder"}, 400)
                return
            raw_tags = query.get("tags", [""])[0]
            tag_terms = [term.strip().casefold() for term in raw_tags.split(",") if term.strip()]
            if len(tag_terms) > 12 or any(len(term) > 60 for term in tag_terms):
                self.send_json({"error": "Use up to 12 tags, each 60 characters or less"}, 400)
                return
            try:
                page = max(0, int(query.get("page", ["0"])[0]))
            except ValueError:
                self.send_json({"error": "Invalid page"}, 400)
                return
            counts = {name: len(items(name)) for name in BUCKETS}
            filtered = items(bucket, tag_terms)
            entries = filtered[page * 48:(page + 1) * 48]
            self.send_json({"counts": counts, "filtered_count": len(filtered),
                            "page": page, "page_size": 48,
                            "items": entries})
            return
        if parsed.path == "/api/download/status":
            self.send_json(self.server.download_job.snapshot())
            return
        if parsed.path == "/api/batch/status":
            self.send_json(self.server.batch_job.snapshot())
            return
        if parsed.path == "/api/decision/status":
            self.send_json(self.server.decision_queue.snapshot())
            return
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] in {"thumb", "image"} and parts[1] in BUCKETS:
            bucket, name = parts[1], urllib.parse.unquote(parts[2])
            if not valid_name(name):
                self.send_json({"error": "Invalid image"}, 400)
                return
            path = ROOT / bucket / name
            if not path.is_file():
                self.send_json({"error": "Image not found"}, 404)
                return
            try:
                if parts[0] == "thumb":
                    body = thumbnail(str(path), path.stat().st_mtime_ns)
                    content_type = "image/jpeg"
                else:
                    body = path.read_bytes()
                    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_bytes(body, content_type)
            except OSError as error:
                self.send_json({"error": str(error)}, 500)
            return
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path not in {"/api/move", "/api/download/start", "/api/download/stop",
                             "/api/tags/start",
                             "/api/sync/start", "/api/batch/start", "/api/batch/stop", "/api/decision"}:
            self.send_json({"error": "Not found"}, 404)
            return
        if self.headers.get("X-Review-Token") != self.server.review_token:
            self.send_json({"error": "Invalid review session"}, 403)
            return
        if self.path == "/api/download/stop":
            self.server.download_job.stop()
            self.send_json(self.server.download_job.snapshot())
            return
        if self.path == "/api/tags/start":
            if not self.server.download_job.start_tag_refresh():
                self.send_json({"error": "A background job is already running"}, 409)
                return
            self.send_json(self.server.download_job.snapshot())
            return
        if self.path == "/api/sync/start":
            if not self.server.download_job.start_sync():
                self.send_json({"error": "A background job is already running"}, 409)
                return
            self.send_json(self.server.download_job.snapshot())
            return
        if self.path == "/api/batch/stop":
            self.server.batch_job.stop()
            self.send_json(self.server.batch_job.snapshot())
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 16384:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(size))
            if self.path == "/api/decision":
                bucket, action, selection = data["bucket"], data["action"], data["item"]
                allowed = {"candidates": {"approve", "reject"},
                           "approved": {"restore"}, "rejected": {"restore"}}
                if (bucket not in allowed or action not in allowed[bucket]
                        or not isinstance(selection, dict) or not valid_name(selection.get("name"))
                        or type(selection.get("remote")) is not bool):
                    raise ValueError("Invalid review decision")
                result = self.server.decision_queue.submit(bucket, action, selection)
                response = {"ok": True, **result} if isinstance(result, dict) else {"ok": True, "queued": result}
                self.send_json(response)
                return
            if self.path == "/api/batch/start":
                bucket = data["bucket"]
                action = data["action"]
                selections = data["items"]
                allowed = {"candidates": {"approve", "reject", "clean"},
                           "approved": {"restore", "clean"},
                           "rejected": {"restore", "clean"}}
                if (bucket not in allowed or action not in allowed[bucket]
                        or not isinstance(selections, list) or not 1 <= len(selections) <= 48
                        or any(not isinstance(item, dict) or not valid_name(item.get("name"))
                               or type(item.get("remote")) is not bool for item in selections)
                        or len({item["name"] for item in selections}) != len(selections)):
                    raise ValueError("Select 1–48 images from the current folder")
                if not self.server.batch_job.start(bucket, action, selections):
                    self.send_json({"error": "A batch action is already running"}, 409)
                    return
                self.send_json(self.server.batch_job.snapshot())
                return
            if self.path == "/api/download/start":
                count = data["count"]
                query = data.get("query", "")
                atleast = data.get("atleast", "1920x1080")
                max_views = data.get("max_views")
                discovery = data.get("discovery", "random")
                order = data.get("order", "desc")
                top_range = data.get("top_range", "1M")
                if (type(count) is not int or not 1 <= count <= 2000
                        or not isinstance(query, str) or len(query) > 100
                        or not isinstance(atleast, str) or not download.RESOLUTION.fullmatch(atleast)
                        or (max_views is not None and (type(max_views) is not int or max_views < 0))
                        or not isinstance(discovery, str)
                        or discovery not in download.SORTING_OPTIONS | {"least_viewed"}
                        or not isinstance(order, str) or order not in {"asc", "desc"}
                        or not isinstance(top_range, str) or top_range not in download.TOP_RANGES):
                    raise ValueError("Check count, resolution, views, and discovery options")
                if not self.server.download_job.start(count, query, atleast, max_views,
                                                      discovery, order, top_range):
                    self.send_json({"error": "A download is already running"}, 409)
                    return
                self.send_json(self.server.download_job.snapshot())
                return
            name, old, new = data["name"], data["from"], data["to"]
            if not valid_name(name) or old not in BUCKETS or new not in BUCKETS or old == new:
                raise ValueError("Invalid move")
            with MOVE_LOCK:
                source, target = ROOT / old / name, ROOT / new / name
                if not source.is_file():
                    self.send_json({"error": "Image no longer exists"}, 404)
                    return
                if target.exists():
                    self.send_json({"error": "Image already exists in destination"}, 409)
                    return
                source.rename(target)
                download.record_review(ROOT, name, new if new in {"approved", "rejected"} else None)
            self.send_json({"ok": True})
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)
        except OSError as error:
            self.send_json({"error": str(error)}, 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="local port (default: automatic)")
    parser.add_argument("--no-browser", action="store_true", help="print URL without opening it")
    args = parser.parse_args()
    for bucket in BUCKETS:
        (ROOT / bucket).mkdir(exist_ok=True)
    backfill_review_times()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.review_token = secrets.token_urlsafe(32)
    server.download_job = DownloadJob()
    server.batch_job = BatchJob()
    server.decision_queue = DecisionQueue()
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Wallpaper review: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
