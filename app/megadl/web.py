"""Flask app: JSON API used by the GUI, plus the static GUI itself."""

import os
import time

from flask import Flask, jsonify, redirect, request, send_from_directory

from . import __version__, auth, config, paths

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(manager):
    app = Flask(__name__, static_folder=None)

    # ------------------------------------------------------------ security
    OPEN_PATHS = {"/healthz", "/login", "/api/session"}

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "megacmd": manager.server_state})

    @app.before_request
    def protect():
        path = request.path
        # Changes must come from MegaDL's own page: browsers won't let other
        # websites add this header to a cross-site request.
        if path.startswith("/api/") and request.method in ("POST", "PUT", "DELETE") \
                and request.headers.get("X-MegaDL") != "1":
            return jsonify({"error": "Requests must come from the MegaDL page."}), 403
        if not auth.enabled() or path in OPEN_PATHS or path.startswith("/static/"):
            return None
        if auth.valid_token(request.cookies.get(auth.COOKIE)):
            return None
        if path.startswith("/api/"):
            return jsonify({"error": "Sign in to MegaDL first.", "login": True}), 401
        return redirect("/login")

    @app.get("/login")
    def login_page():
        if not auth.enabled() or auth.valid_token(request.cookies.get(auth.COOKIE)):
            return redirect("/")
        return send_from_directory(STATIC_DIR, "login.html")

    @app.post("/api/session")
    def sign_in():
        if not auth.enabled():
            return jsonify({"ok": True})
        ip = request.remote_addr or "?"
        if auth.throttle.blocked(ip):
            return jsonify({"error": "Too many wrong tries. Wait 5 minutes and try again."}), 429
        data = body()
        if not auth.check_credentials(data.get("user"), data.get("secret")):
            auth.throttle.failed(ip)
            time.sleep(0.6)
            return jsonify({"error": "That user name and password don't match."}), 401
        auth.throttle.succeeded(ip)
        token, max_age = auth.make_token(bool(data.get("remember")))
        resp = jsonify({"ok": True})
        resp.set_cookie(auth.COOKIE, token, max_age=max_age, httponly=True,
                        samesite="Lax", secure=request.is_secure, path="/")
        return resp

    @app.delete("/api/session")
    def sign_out():
        resp = jsonify({"ok": True})
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def error(message, status=400):
        return jsonify({"error": message}), status

    @app.errorhandler(KeyError)
    def not_found(_exc):
        return error("That download no longer exists.", 404)

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return error(str(exc) or "Invalid request.")

    # --------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        resp = send_from_directory(STATIC_DIR, filename)
        if filename.startswith("fonts/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000"
        return resp

    # ----------------------------------------------------------------- api
    @app.get("/api/state")
    def state():
        data = manager.snapshot()
        data["version"] = __version__
        data["root_label"] = config.HOST_PATH_LABEL
        data["auth_enabled"] = auth.enabled()
        return jsonify(data)

    @app.post("/api/downloads")
    def add_downloads():
        data = body()
        ids, skipped = manager.add_links(
            data.get("links", ""), data.get("dest"), data.get("password") or None,
            start_paused=bool(data.get("start_paused")))
        return jsonify({"ids": ids, "skipped": skipped}), 201

    @app.get("/api/downloads/<int:job_id>/files")
    def job_files(job_id):
        return jsonify(manager.files(job_id))

    @app.post("/api/downloads/<int:job_id>/select")
    def select_files(job_id):
        data = body()
        manager.select_files(job_id, data.get("paths"), bool(data.get("selected")))
        return jsonify({"ok": True})

    @app.post("/api/downloads/<int:job_id>/pause")
    def pause(job_id):
        manager.pause(job_id)
        return jsonify({"ok": True})

    @app.post("/api/downloads/<int:job_id>/resume")
    def resume(job_id):
        manager.resume(job_id)
        return jsonify({"ok": True})

    @app.post("/api/downloads/<int:job_id>/retry")
    def retry(job_id):
        manager.retry(job_id)
        return jsonify({"ok": True})

    @app.delete("/api/downloads/<int:job_id>")
    def delete(job_id):
        delete_files = request.args.get("files") in ("1", "true", "yes")
        manager.delete(job_id, delete_files=delete_files)
        return jsonify({"ok": True})

    @app.post("/api/downloads/pause-all")
    def pause_all():
        manager.pause_all()
        return jsonify({"ok": True})

    @app.post("/api/downloads/resume-all")
    def resume_all():
        manager.resume_all()
        return jsonify({"ok": True})

    @app.post("/api/downloads/clear-finished")
    def clear_finished():
        return jsonify({"cleared": manager.clear_finished()})

    @app.get("/api/folders")
    def folders():
        try:
            data = paths.list_folders(request.args.get("path", ""))
        except paths.PathError as exc:
            return error(str(exc))
        data["disk"] = paths.disk_usage(paths.resolve(data["path"]))
        return jsonify(data)

    @app.post("/api/folders")
    def make_folder():
        data = body()
        try:
            new_path = paths.make_folder(data.get("path", ""), data.get("name", ""))
        except paths.PathError as exc:
            return error(str(exc))
        except OSError as exc:
            return error(f"Couldn't create the folder: {exc.strerror}")
        return jsonify({"path": new_path}), 201

    @app.put("/api/settings")
    def settings():
        return jsonify(manager.update_settings(**body()))

    @app.post("/api/account/login")
    def login():
        data = body()
        email = (data.get("email") or "").strip()
        password = data.get("secret") or data.get("password") or ""
        if not email or not password:
            return error("Enter your MEGA e-mail and password.")
        result = manager.login(email, password, (data.get("code") or "").strip() or None)
        return jsonify(result), (200 if result["ok"] else 400)

    @app.post("/api/account/logout")
    def logout():
        return jsonify(manager.logout())

    return app
