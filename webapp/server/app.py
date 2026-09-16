from __future__ import annotations

import os
from pathlib import Path
import secrets

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for

from webapp.config import Settings, load_settings
from webapp.db import connect, migrate
from webapp.server.api import api
from webapp.server.auth import csrf_protect, ensure_csrf_token, login_rate_limited, password_matches
from webapp.server.cloud_views import cloud
from webapp.server.metrics import start_sampler


def _secret_key(settings: Settings) -> str:
    if settings.secret_key:
        return settings.secret_key
    path = settings.data_dir / "session-secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="ascii").strip()
    value = secrets.token_hex(32)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(value)
    return value


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings(require_password=True)
    migrate(settings.database_path)
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).with_name("templates")),
        static_folder=str(Path(__file__).with_name("static")),
    )
    app.config.update(
        SECRET_KEY=_secret_key(settings),
        WEBAPP_SETTINGS=settings,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    app.register_blueprint(api)
    app.register_blueprint(cloud)
    start_sampler(settings)

    @app.before_request
    def security_gate():
        if request.endpoint in {"login", "static"}:
            return None
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                abort(401, description="Authentication required")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        ensure_csrf_token()
        csrf_protect()
        return None

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "style-src 'self'; script-src 'self'; connect-src 'self'"
        )
        return response

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def expected_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description, "status": error.code}), error.code
        return render_template("error.html", error=error), error.code

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            address = request.remote_addr or "unknown"
            if login_rate_limited(address):
                return render_template("login.html", error="Too many attempts. Try again in a few minutes."), 429
            if password_matches(request.form.get("password", "")):
                session.clear()
                session["authenticated"] = True
                ensure_csrf_token()
                destination = request.args.get("next", "")
                if not destination.startswith("/") or destination.startswith("//"):
                    destination = url_for("library")
                return redirect(destination)
            return render_template("login.html", error="Incorrect password"), 401
        return render_template("login.html", error=None)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def library():
        return render_template("library.html", page="library")

    @app.get("/titles/<int:title_id>")
    def title_browser(title_id: int):
        with connect(settings.database_path) as db:
            exists = db.execute("SELECT 1 FROM titles WHERE id=?", (title_id,)).fetchone()
        if exists is None:
            abort(404, description="Title not found")
        return render_template("title.html", page="library", title_id=title_id)

    @app.get("/queue")
    def queue():
        return render_template("queue.html", page="queue")

    @app.get("/jobs/<public_id>")
    def job_page(public_id: str):
        with connect(settings.database_path) as db:
            exists = db.execute("SELECT 1 FROM jobs WHERE public_id=?", (public_id,)).fetchone()
        if exists is None:
            abort(404, description="Job not found")
        return render_template("job.html", page="queue", public_id=public_id)

    @app.get("/media/<int:artifact_id>")
    def media(artifact_id: int):
        with connect(settings.database_path) as db:
            artifact = db.execute(
                "SELECT * FROM artifacts WHERE id=? AND validation_state='valid'", (artifact_id,)
            ).fetchone()
        if artifact is None:
            abort(404, description="Media artifact not found")
        path = (settings.data_dir / artifact["relative_path"]).resolve()
        if not path.is_relative_to(settings.data_dir) or not path.is_file():
            abort(404, description="Media artifact file is unavailable")
        return send_file(
            path,
            mimetype=artifact["mime_type"],
            conditional=True,
            as_attachment=request.args.get("download") == "1",
            download_name=path.name,
        )

    return app


def main() -> None:
    settings = load_settings(require_password=True)
    create_app(settings).run(host=settings.host, port=settings.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
