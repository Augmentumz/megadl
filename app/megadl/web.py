"""Flask app: JSON API used by the GUI, plus the static GUI itself."""

import hmac
import os

from flask import Flask, Response, jsonify, request, send_from_directory

from . import __version__, config, paths

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(manager):
    app = Flask(__name__, static_folder=None)

    # ------------------------------------------------------------ security
    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "megacmd": manager.server_state})

    @app.before_request
    def require_password():
        if not config.WEBUI_PASSWORD or request.path == "/healthz":
            return None
        auth = request.authorization
        if auth and hmac.compare_digest(auth.username or "", config.WEBUI_USER) and \
                hmac.compare_digest(auth.password or "", config.WEBUI_PASSWORD):
            return None
        return Response("Sign in to MegaDL", 401,
                        {"WWW-Authenticate": 'Basic realm="MegaDL"'})

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
        data["auth_enabled"] = bool(config.WEBUI_PASSWORD)
        return jsonify(data)

    @app.post("/api/downloads")
    def add_downloads():
        data = body()
        ids, skipped = manager.add_links(
            data.get("links", ""), data.get("dest"), data.get("password") or None)
        return jsonify({"ids": ids, "skipped": skipped}), 201

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
        email, password = (data.get("email") or "").strip(), data.get("password") or ""
        if not email or not password:
            return error("Enter your MEGA e-mail and password.")
        result = manager.login(email, password, (data.get("code") or "").strip() or None)
        return jsonify(result), (200 if result["ok"] else 400)

    @app.post("/api/account/logout")
    def logout():
        return jsonify(manager.logout())

    return app
