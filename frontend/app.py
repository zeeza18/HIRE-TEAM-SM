"""
RMS Recruiter Portal — Flask backend.

Run:  python frontend/app.py
Then: open http://localhost:5000
"""

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from scraper.utils import CANDIDATES_DIR, DATA_DIR, load_status_counts

app = Flask(__name__, static_folder="static")
RESUMES_DIR = CANDIDATES_DIR.parent / "resumes"

# Try importing messenger at startup so any errors are visible immediately
_MESSENGER_ERROR = None
try:
    import scraper.messenger as _messenger
    print("[app] scraper.messenger imported OK")
except Exception as _e:
    _MESSENGER_ERROR = traceback.format_exc()
    print(f"[app] WARNING — scraper.messenger import failed:\n{_MESSENGER_ERROR}")


def _load_all() -> list[dict]:
    out = []
    for f in sorted(CANDIDATES_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    out.sort(key=lambda c: (-(c.get("fit_score") or 0), (c.get("full_name") or "")))
    return out


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/candidates")
def api_candidates():
    return jsonify(_load_all())


@app.route("/api/candidates/<cid>/status", methods=["PATCH"])
def update_status(cid):
    body = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").strip().lower()
    VALID = {"new", "reviewing", "contacting", "interviewing", "hired", "rejected"}
    if new_status not in VALID:
        return jsonify({"error": f"status must be one of {sorted(VALID)}"}), 400

    path = CANDIDATES_DIR / f"{cid}.json"
    if not path.exists():
        return jsonify({"error": "candidate not found"}), 404

    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = new_status
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "id": cid, "status": new_status})


@app.route("/api/status_counts")
def api_status_counts():
    return jsonify(load_status_counts())


@app.route("/resume/<path:filename>")
def resume(filename):
    return send_from_directory(str(RESUMES_DIR), filename)


@app.route("/api/settings")
def api_settings():
    settings_path = Path(__file__).parent.parent / "config" / "settings.json"
    if settings_path.exists():
        return jsonify(json.loads(settings_path.read_text(encoding="utf-8")))
    return jsonify({})


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "messenger_ok": _MESSENGER_ERROR is None,
                    "messenger_error": _MESSENGER_ERROR})


@app.route("/api/message-templates")
def api_message_templates():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    try:
        return jsonify(_messenger.get_templates())
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/send-message", methods=["POST"])
def api_send_message():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    body    = request.get_json(silent=True) or {}
    cid     = (body.get("candidate_id") or "").strip()
    message = (body.get("message") or "").strip()
    status  = (body.get("new_status") or "contacting").strip()
    if not cid or not message:
        return jsonify({"ok": False, "error": "candidate_id and message are required"}), 400
    try:
        result = _messenger.send_message(cid, message, new_status=status)
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/reject", methods=["POST"])
def api_reject():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    body = request.get_json(silent=True) or {}
    cid  = (body.get("candidate_id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": "candidate_id is required"}), 400
    try:
        result = _messenger.send_message(cid, "", new_status="rejected")
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/schedule-interview", methods=["POST"])
def api_schedule_interview():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    body     = request.get_json(silent=True) or {}
    cid      = (body.get("candidate_id") or "").strip()
    message  = (body.get("message") or "").strip()
    date     = (body.get("interview_date") or "").strip()
    time_str = (body.get("start_time") or "09:00").strip()
    duration = str(body.get("duration") or "30")
    fmt      = (body.get("format") or "Phone").strip()
    if not cid or not date:
        return jsonify({"ok": False, "error": "candidate_id and interview_date are required"}), 400
    try:
        result = _messenger.schedule_interview(cid, message, date, time_str, duration=duration, format_=fmt)
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/conversations")
def api_conversations():
    index_path = DATA_DIR / "conversations" / "index.json"
    if index_path.exists():
        return jsonify(json.loads(index_path.read_text(encoding="utf-8")))
    return jsonify([])


@app.route("/api/conversations/<thread_id>")
def api_conversation(thread_id):
    conv_path = DATA_DIR / "conversations" / f"{thread_id}.json"
    if not conv_path.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify(json.loads(conv_path.read_text(encoding="utf-8")))


@app.route("/api/scrape-conversations", methods=["POST"])
def api_scrape_conversations():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    try:
        import scraper.conversations_scraper as _conv
        threads = _conv.scrape()
        return jsonify({"ok": True, "count": len(threads)})
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/ai-reply", methods=["POST"])
def api_ai_reply():
    body      = request.get_json(silent=True) or {}
    thread_id = (body.get("thread_id") or "").strip()
    if not thread_id:
        return jsonify({"ok": False, "error": "thread_id required"}), 400
    conv_path = DATA_DIR / "conversations" / f"{thread_id}.json"
    if not conv_path.exists():
        return jsonify({"ok": False, "error": "Thread not found — scrape first"}), 404
    try:
        thread = json.loads(conv_path.read_text(encoding="utf-8"))
        from scraper.ai_responder import generate_reply
        reply = generate_reply(thread)
        return jsonify({"ok": True, "reply": reply})
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/send-conversation-reply", methods=["POST"])
def api_send_conversation_reply():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    body      = request.get_json(silent=True) or {}
    thread_id = (body.get("thread_id") or "").strip()
    message   = (body.get("message")   or "").strip()
    if not thread_id or not message:
        return jsonify({"ok": False, "error": "thread_id and message required"}), 400
    try:
        from scraper.conversations_scraper import send_reply
        result = send_reply(thread_id, message)
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": e.description}), e.code
        return e
    return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


if __name__ == "__main__":
    print("\n  RMS Recruiter Portal")
    print("  Open: http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
