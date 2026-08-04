#!/usr/bin/env python3
"""Disposable web UI for blind-comparing AI-upscaling outputs.

Reads a manifest (re-read from disk on every request), serves the referenced
assets, and appends votes as JSON lines to votes.jsonl. No DB, no auth.
"""
import json
import os
import time
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    send_file,
    send_from_directory,
)

app = Flask(__name__, static_folder="static", static_url_path="/static")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")
VOTES_PATH = os.path.join(DATA_DIR, "votes.jsonl")
ASSETS_DIR = os.path.join(DATA_DIR, "assets")


def read_manifest():
    """Always read fresh from disk — the manifest is edited while we run."""
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_votes():
    """Return list of parsed vote records (skips malformed lines)."""
    votes = []
    if not os.path.exists(VOTES_PATH):
        return votes
    with open(VOTES_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                votes.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return votes


def last_vote_per_set():
    """Map set-id -> most recent vote record (last line wins)."""
    latest = {}
    for v in read_votes():
        sid = v.get("set")
        if sid is not None:
            latest[sid] = v
    return latest


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/manifest")
def api_manifest():
    try:
        return jsonify(read_manifest())
    except FileNotFoundError:
        abort(404, "manifest.json not found")
    except json.JSONDecodeError as exc:
        abort(500, f"manifest.json is invalid JSON: {exc}")


@app.route("/api/progress")
def api_progress():
    """Latest vote per set so the UI can show which sets are done."""
    return jsonify(last_vote_per_set())


@app.route("/api/vote", methods=["POST"])
def api_vote():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "expected a JSON object")

    set_id = payload.get("set")
    best = payload.get("best")
    if not set_id or not best:
        abort(400, "both 'set' and 'best' are required")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "set": set_id,
        "best": best,
    }
    ranking = payload.get("ranking")
    if isinstance(ranking, list) and ranking:
        record["ranking"] = ranking
    note = payload.get("note")
    if isinstance(note, str) and note.strip():
        record["note"] = note.strip()
    # Optional: record what letter-order the user saw, for auditing.
    order = payload.get("order")
    if isinstance(order, list) and order:
        record["shown_order"] = order

    line = json.dumps(record, ensure_ascii=False)
    # Append-only. Never truncate/overwrite.
    with open(VOTES_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())

    return jsonify({"ok": True, "record": record})


@app.route("/assets/<path:relpath>")
def assets(relpath):
    """Serve manifest-referenced files with HTTP Range support (video seek)."""
    full = os.path.normpath(os.path.join(ASSETS_DIR, relpath))
    # Prevent path traversal outside ASSETS_DIR.
    if not full.startswith(os.path.abspath(ASSETS_DIR) + os.sep) and full != os.path.abspath(ASSETS_DIR):
        abort(403)
    if not os.path.isfile(full):
        abort(404)
    return send_file(full, conditional=True)


@app.route("/healthz")
def healthz():
    return Response("ok\n", mimetype="text/plain")


if __name__ == "__main__":
    os.makedirs(ASSETS_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=8092, threaded=True)
