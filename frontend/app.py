"""
RMS Recruiter Portal — Flask backend (multi-tenant + login).

Run:  python frontend/app.py
Then: open http://localhost:5000
"""

import json
import sys
import time
import threading
import traceback
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash

from scraper.utils import CANDIDATES_DIR, DATA_DIR, load_status_counts

app = Flask(__name__, static_folder="static")
app.secret_key = "rms-recruiter-portal-secret-2024-xk9"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

RESUMES_DIR       = CANDIDATES_DIR.parent / "resumes"
PORTAL_USERS_FILE = Path(__file__).parent.parent / "config" / "portal_users.json"

# ── Portal user helpers ───────────────────────────────────────────────────────

def _load_portal_users() -> dict:
    if PORTAL_USERS_FILE.exists():
        try:
            return json.loads(PORTAL_USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _current_company():
    """Return Company object for the logged-in session, or RMS as default."""
    try:
        from agents.company import COMPANIES
        return COMPANIES.get(session.get("company", "rms"))
    except Exception:
        return None


def _cands_dir() -> Path:
    co = _current_company()
    return co.candidates_dir if co else CANDIDATES_DIR


def _data_dir() -> Path:
    co = _current_company()
    return co.data_dir if co else DATA_DIR


def _resumes_dir() -> Path:
    co = _current_company()
    return co.resumes_dir if co else RESUMES_DIR


def _config_dir() -> Path:
    co = _current_company()
    return co.config_dir if co else CANDIDATES_DIR.parent.parent / "config"


# ── Auth decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "company" not in session:
            return jsonify({"ok": False, "error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Live sync state ──────────────────────────────────────────────────────────
_sync_lock  = threading.Lock()
_sync_state = {
    "running":       False,
    "last_synced":   None,
    "new_messages":  0,
    "auto_replied":  0,
    "error":         None,
}
_SYNC_INTERVAL   = 120
_SYNC_TOP_N      = 20
_SINCE_WINDOW    = 10


def _snapshot_threads(conv_dir: Path = None):
    if conv_dir is None:
        conv_dir = DATA_DIR / "conversations"
    snap = {}
    for f in conv_dir.glob("thread-*.json"):
        try:
            d    = json.loads(f.read_text(encoding="utf-8"))
            tid  = d.get("thread_id", "")
            msgs = d.get("messages", [])
            snap[tid] = {
                "count":    d.get("message_count", 0),
                "last_dir": msgs[-1].get("direction", "") if msgs else "",
            }
        except Exception:
            pass
    return snap


def _auto_reply_thread(thread: dict, company=None):
    from scraper.ai_responder import generate_reply
    import scraper.conversations_scraper as _conv
    try:
        if company:
            from agents.context import company_scope
            with company_scope(company):
                reply  = generate_reply(thread)
                result = _conv.send_reply(thread["thread_id"], reply)
        else:
            reply  = generate_reply(thread)
            result = _conv.send_reply(thread["thread_id"], reply)
        if result.get("ok"):
            print(f"[auto-reply] Sent to {thread.get('candidate_name')} ✓")
            return True
        print(f"[auto-reply] send_reply failed: {result.get('error')}")
    except Exception as e:
        print(f"[auto-reply] Error for {thread.get('thread_id')}: {e}")
    return False


def _role_auto_mode(config_dir: Path, role: str) -> bool:
    """Return True if auto mode is enabled for this specific role (default: False/manual)."""
    try:
        path = config_dir / "settings.json"
        if path.exists():
            s = json.loads(path.read_text(encoding="utf-8"))
            return bool(s.get("auto_mode_roles", {}).get(role, False))
    except Exception:
        pass
    return False


def _known_job_postings(cands_dir: Path) -> dict[str, list[str]]:
    """title -> [job_id, ...]. Distinct postings can share the exact same
    title text, so title alone is not a unique key — only job_id is.
    """
    postings: dict[str, set[str]] = {}
    try:
        for f in cands_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            jt, jid = d.get("job_title"), d.get("job_id")
            if jt and jid:
                postings.setdefault(jt, set()).add(jid)
    except Exception:
        pass
    return {t: sorted(ids) for t, ids in postings.items()}


def _resolve_job_ids(job_info: str, postings: dict[str, list[str]]) -> list[str] | None:
    """Conversation threads only carry the job's title as scraped text (no
    job_id available there) — if that title is shared by several postings,
    all of their job_ids are returned together.
    """
    job_info = job_info or ""
    for title in sorted(postings, key=len, reverse=True):  # longest first avoids partial-substring mismatches
        if title and title in job_info:
            return postings[title]
    return None


def _do_sync(n=_SYNC_TOP_N, full=False, company=None):
    with _sync_lock:
        if _sync_state["running"]:
            return
        _sync_state["running"] = True
        _sync_state["error"]   = None
    try:
        if company is None:
            try:
                from agents.company import COMPANIES
                company = COMPANIES.get("rms")
            except Exception:
                pass

        conv_dir = company.conversations_dir if company else DATA_DIR / "conversations"
        before   = _snapshot_threads(conv_dir)

        import scraper.conversations_scraper as _conv
        since = None if full else (datetime.now() - timedelta(minutes=_SINCE_WINDOW))

        if company:
            from agents.context import company_scope
            with company_scope(company):
                threads = _conv.scrape(max_threads=n, since=since)
        else:
            threads = _conv.scrape(max_threads=n, since=since)

        new_msgs = auto_replied = 0
        to_reply = []
        for t in threads:
            tid        = t.get("thread_id", "")
            prev_count = before.get(tid, {}).get("count", 0)
            messages   = t.get("messages", [])
            last_dir   = messages[-1].get("direction", "") if messages else ""
            if t.get("message_count", 0) > prev_count:
                new_msgs += 1
                if last_dir == "inbound":
                    to_reply.append(t)

        cfg_dir   = company.config_dir if company else _config_dir()
        cands_dir = company.candidates_dir if company else _cands_dir()
        postings  = _known_job_postings(cands_dir)
        for t in to_reply:
            # If the thread's title is shared by several distinct postings,
            # require ALL of them to have Auto on before replying — otherwise
            # a manual posting could get auto-replied just because it shares
            # a title with an unrelated auto-enabled one.
            job_ids = _resolve_job_ids(t.get("job_info"), postings)
            if job_ids and all(_role_auto_mode(cfg_dir, jid) for jid in job_ids):
                if _auto_reply_thread(t, company=company):
                    auto_replied += 1
            else:
                print(f"[sync] Manual mode (job_ids={job_ids!r}) — skipping auto-reply for thread {t.get('thread_id')}")

        with _sync_lock:
            _sync_state["last_synced"]  = datetime.now().isoformat()
            _sync_state["new_messages"] = new_msgs
            _sync_state["auto_replied"] = auto_replied
    except Exception as e:
        with _sync_lock:
            _sync_state["error"] = str(e)[:300]
    finally:
        with _sync_lock:
            _sync_state["running"] = False


# Auto-sync loop disabled — agent runner handles scraping on its own schedule.
# Manual sync still works via /api/trigger-sync from the UI.

# ── Messenger import ──────────────────────────────────────────────────────────
_MESSENGER_ERROR = None
try:
    import scraper.messenger as _messenger
    print("[app] scraper.messenger imported OK")
except Exception as _e:
    _MESSENGER_ERROR = traceback.format_exc()
    print(f"[app] WARNING — scraper.messenger import failed:\n{_MESSENGER_ERROR}")


# ── Candidate loader ──────────────────────────────────────────────────────────

def _load_all(cands_dir: Path = None) -> list[dict]:
    if cands_dir is None:
        cands_dir = _cands_dir()
    out = []
    for f in sorted(cands_dir.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    out.sort(key=lambda c: (-(c.get("fit_score") or 0), (c.get("full_name") or "")))
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/api/auth-status")
def api_auth_status():
    if "company" not in session:
        return jsonify({"authenticated": False}), 401
    return jsonify({
        "authenticated": True,
        "company":      session.get("company", "rms"),
        "display_name": session.get("display_name", ""),
    })


@app.route("/api/select-company", methods=["POST"])
def api_select_company():
    """No password needed — just select which company to view."""
    from agents.company import COMPANIES
    body = request.get_json(silent=True) or {}
    slug = (body.get("company") or "").strip().lower()
    co   = COMPANIES.get(slug)
    if not co:
        return jsonify({"ok": False, "error": f"Unknown company: {slug}"}), 400

    session.permanent  = True
    session["company"] = slug
    session["display_name"] = co.display_name
    return jsonify({"ok": True, "company": slug, "display_name": co.display_name})


@app.route("/api/company-status")
def api_company_status():
    """Return session status + candidate count for each company (for the card badges)."""
    from agents.company import COMPANIES
    result = {}
    for slug, co in COMPANIES.items():
        cands = 0
        try:
            cands = sum(1 for _ in co.candidates_dir.glob("*.json"))
        except Exception:
            pass
        result[slug] = {
            "display_name": co.display_name,
            "session_ok":   co.session_state_file.exists(),
            "candidates":   cands,
        }
    return jsonify(result)


@app.route("/api/trigger-login", methods=["POST"])
def api_trigger_login():
    """Launch auto_login.py in background for the given company."""
    import subprocess, sys as _sys
    from agents.company import COMPANIES
    body = request.get_json(silent=True) or {}
    slug = (body.get("company") or "").strip().lower()
    co   = COMPANIES.get(slug)
    if not co:
        return jsonify({"ok": False, "error": f"Unknown company: {slug}"}), 400
    if co.session_state_file.exists():
        return jsonify({"ok": False, "error": "Session already exists"}), 200

    BASE_DIR = Path(__file__).parent.parent
    script   = str(BASE_DIR / "scraper" / "auto_login.py")
    python   = _sys.executable  # same venv that runs Flask
    try:
        subprocess.Popen(
            [python, script, "--slug", slug],
            cwd=str(BASE_DIR),
        )
        return jsonify({
            "ok": True,
            "message": f"Login started for {co.display_name}. OTP will be requested shortly.",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/submit-otp", methods=["POST"])
def api_submit_otp():
    """Write OTP to config/_otp.txt so auto_login.py can pick it up."""
    body = request.get_json(silent=True) or {}
    otp  = (body.get("otp") or "").strip()
    if not otp:
        return jsonify({"ok": False, "error": "No OTP provided"}), 400
    otp_file = Path(__file__).parent.parent / "config" / "_otp.txt"
    otp_file.write_text(otp, encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/login-status")
def api_login_status():
    """Return the current auto_login status string."""
    stat_file = Path(__file__).parent.parent / "config" / "_login_status.txt"
    status = stat_file.read_text(encoding="utf-8").strip() if stat_file.exists() else "IDLE"
    return jsonify({"status": status})


@app.route("/api/login", methods=["POST"])
def api_login():
    """Legacy endpoint kept for compatibility."""
    return api_select_company()


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# ── Candidate routes ──────────────────────────────────────────────────────────

@app.route("/api/candidates")
@login_required
def api_candidates():
    return jsonify(_load_all())


@app.route("/api/candidates/<cid>/status", methods=["PATCH"])
@login_required
def update_status(cid):
    body       = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").strip().lower()
    VALID = {"new", "reviewing", "contacting", "interviewing", "hired", "rejected"}
    if new_status not in VALID:
        return jsonify({"error": f"status must be one of {sorted(VALID)}"}), 400

    cands = _cands_dir()
    path  = cands / f"{cid}.json"
    if not path.exists():
        # also check with job-hash suffix
        matches = list(cands.glob(f"{cid}-*.json"))
        if not matches:
            return jsonify({"error": "candidate not found"}), 404
        path = matches[0]

    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = new_status
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "id": cid, "status": new_status})


@app.route("/api/status_counts")
@login_required
def api_status_counts():
    co = _current_company()
    if co:
        from scraper.utils import load_json
        counts = load_json(co.status_counts_file, default={}) or {}
        return jsonify(counts)
    return jsonify(load_status_counts())


@app.route("/api/job-info/<job_id>")
@login_required
def api_job_info(job_id):
    """Date posted + JD + AI-generated requirements for one specific posting."""
    jd_file = _config_dir() / "job_descriptions.json"
    jobs = json.loads(jd_file.read_text(encoding="utf-8")) if jd_file.exists() else {}
    info = jobs.get(job_id)
    if not info:
        return jsonify({"error": "not found"}), 404

    title = info.get("title", "")
    crit_file = _config_dir() / "generated_criteria.json"
    criteria = []
    if crit_file.exists():
        try:
            crit_data = json.loads(crit_file.read_text(encoding="utf-8"))
            entry = crit_data.get(title.lower(), {})
            criteria = entry.get("criteria", [])
        except Exception:
            pass

    return jsonify({
        "title":           title,
        "date_posted":     info.get("date_posted", ""),
        "job_description": info.get("job_description", ""),
        "requirements": [
            {"label": c.get("label", ""), "max": c.get("max", 0), "key_things": c.get("key_things", [])}
            for c in criteria
        ],
    })


@app.route("/resume/<path:filename>")
@login_required
def resume(filename):
    return send_from_directory(str(_resumes_dir()), filename)


@app.route("/api/settings")
@login_required
def api_settings():
    settings_path = _config_dir() / "settings.json"
    if settings_path.exists():
        return jsonify(json.loads(settings_path.read_text(encoding="utf-8")))
    return jsonify({})


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok":              True,
        "messenger_ok":    _MESSENGER_ERROR is None,
        "messenger_error": _MESSENGER_ERROR,
    })


@app.route("/api/message-templates")
@login_required
def api_message_templates():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    try:
        # Load templates from the current company's config dir
        tpl_file = _config_dir() / "message_templates.json"
        if tpl_file.exists():
            return jsonify(json.loads(tpl_file.read_text(encoding="utf-8")))
        return jsonify(_messenger.get_templates())
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/send-message", methods=["POST"])
@login_required
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
        co = _current_company()
        if co:
            from agents.context import company_scope
            with company_scope(co):
                result = _messenger.send_message(cid, message, new_status=status)
        else:
            result = _messenger.send_message(cid, message, new_status=status)
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/reject", methods=["POST"])
@login_required
def api_reject():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    body = request.get_json(silent=True) or {}
    cid  = (body.get("candidate_id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": "candidate_id is required"}), 400
    try:
        co = _current_company()
        if co:
            from agents.context import company_scope
            with company_scope(co):
                result = _messenger.send_message(cid, "", new_status="rejected")
        else:
            result = _messenger.send_message(cid, "", new_status="rejected")
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/schedule-interview", methods=["POST"])
@login_required
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
        co = _current_company()
        if co:
            from agents.context import company_scope
            with company_scope(co):
                result = _messenger.schedule_interview(cid, message, date, time_str,
                                                       duration=duration, format_=fmt)
        else:
            result = _messenger.schedule_interview(cid, message, date, time_str,
                                                   duration=duration, format_=fmt)
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


# ── Conversation routes ───────────────────────────────────────────────────────

@app.route("/api/conversations")
@login_required
def api_conversations():
    index_path = _data_dir() / "conversations" / "index.json"
    if index_path.exists():
        return jsonify(json.loads(index_path.read_text(encoding="utf-8")))
    return jsonify([])


@app.route("/api/conversations/<thread_id>")
@login_required
def api_conversation(thread_id):
    conv_path = _data_dir() / "conversations" / f"{thread_id}.json"
    if not conv_path.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify(json.loads(conv_path.read_text(encoding="utf-8")))


@app.route("/api/scrape-conversations", methods=["POST"])
@login_required
def api_scrape_conversations():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    try:
        co = _current_company()
        if co:
            from agents.context import company_scope
            with company_scope(co):
                import scraper.conversations_scraper as _conv
                threads = _conv.scrape()
        else:
            import scraper.conversations_scraper as _conv
            threads = _conv.scrape()
        return jsonify({"ok": True, "count": len(threads)})
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/sync-status")
@login_required
def api_sync_status():
    with _sync_lock:
        return jsonify(dict(_sync_state))


@app.route("/api/trigger-sync", methods=["POST"])
@login_required
def api_trigger_sync():
    body = request.get_json(silent=True) or {}
    n    = int(body.get("n", _SYNC_TOP_N))
    full = bool(body.get("full", True))
    co   = _current_company()
    threading.Thread(target=_do_sync, args=(n, full, co), daemon=True).start()
    return jsonify({"ok": True, "n": n})


@app.route("/api/ai-reply", methods=["POST"])
@login_required
def api_ai_reply():
    body      = request.get_json(silent=True) or {}
    thread_id = (body.get("thread_id") or "").strip()
    if not thread_id:
        return jsonify({"ok": False, "error": "thread_id required"}), 400
    conv_path = _data_dir() / "conversations" / f"{thread_id}.json"
    if not conv_path.exists():
        return jsonify({"ok": False, "error": "Thread not found — scrape first"}), 404
    try:
        thread = json.loads(conv_path.read_text(encoding="utf-8"))
        co     = _current_company()
        if co:
            from agents.context import company_scope
            with company_scope(co):
                from scraper.ai_responder import generate_reply
                reply = generate_reply(thread)
        else:
            from scraper.ai_responder import generate_reply
            reply = generate_reply(thread)
        return jsonify({"ok": True, "reply": reply})
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/send-conversation-reply", methods=["POST"])
@login_required
def api_send_conversation_reply():
    if _MESSENGER_ERROR:
        return jsonify({"ok": False, "error": _MESSENGER_ERROR}), 500
    body      = request.get_json(silent=True) or {}
    thread_id = (body.get("thread_id") or "").strip()
    message   = (body.get("message")   or "").strip()
    if not thread_id or not message:
        return jsonify({"ok": False, "error": "thread_id and message required"}), 400
    try:
        co = _current_company()
        if co:
            from agents.context import company_scope
            with company_scope(co):
                from scraper.conversations_scraper import send_reply
                result = send_reply(thread_id, message)
        else:
            from scraper.conversations_scraper import send_reply
            result = send_reply(thread_id, message)
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


# ── Auto-mode toggle ─────────────────────────────────────────────────────────

@app.route("/api/auto-mode", methods=["GET", "POST"])
@login_required
def api_auto_mode():
    settings_path = _config_dir() / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    roles = settings.get("auto_mode_roles", {})

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        role = (body.get("role") or "").strip()
        if not role:
            return jsonify({"error": "role is required"}), 400
        roles[role] = bool(body.get("auto_mode", False))
        settings["auto_mode_roles"] = roles
        settings.pop("auto_mode", None)  # superseded by per-role flags
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return jsonify({"role": role, "auto_mode": roles[role]})

    role = (request.args.get("role") or "").strip()
    if not role:
        return jsonify({"error": "role is required"}), 400
    return jsonify({"role": role, "auto_mode": bool(roles.get(role, False))})


# ── Job posting routes ────────────────────────────────────────────────────────

@app.route("/api/draft-job", methods=["POST"])
@login_required
def api_draft_job():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "text")
    try:
        from scraper.job_drafter import draft_from_text, draft_from_form
        if mode == "text":
            text = (body.get("text") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "text is required"}), 400
            result = draft_from_text(text)
        else:
            form = body.get("form") or {}
            if not (form.get("title") or "").strip():
                return jsonify({"ok": False, "error": "title is required"}), 400
            result = draft_from_form(form)
        return jsonify(result)
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/post-job", methods=["POST"])
@login_required
def api_post_job():
    import subprocess, sys as _sys
    body = request.get_json(silent=True) or {}
    job  = body.get("job") or {}
    if not (job.get("title") or "").strip():
        return jsonify({"ok": False, "error": "job.title is required"}), 400

    BASE_DIR = Path(__file__).parent.parent
    job_file = BASE_DIR / "data" / "_pending_job.json"
    job_file.parent.mkdir(exist_ok=True)
    job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")

    script = str(BASE_DIR / "scraper" / "post_job.py")
    try:
        proc = subprocess.Popen(
            [_sys.executable, script, "--job", str(job_file)],
            cwd=str(BASE_DIR),
        )
        return jsonify({
            "ok":      True,
            "message": "Job posting started — watch the browser window.",
            "pid":     proc.pid,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Criteria generation ───────────────────────────────────────────────────────

@app.route("/api/generate-criteria", methods=["POST"])
@login_required
def api_generate_criteria():
    import os
    body   = request.get_json(silent=True) or {}
    rerun  = bool(body.get("rerun", False))
    co     = _current_company()
    try:
        from openai import OpenAI
        if co:
            creds = co.load_credentials()
        else:
            from scraper.utils import load_credentials
            creds = load_credentials()
        api_key = creds.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return jsonify({"ok": False, "error": "No openai_api_key in credentials"}), 400

        client = OpenAI(api_key=api_key)
        from scraper.criteria_generator import generate_all
        if co:
            from agents.context import company_scope
            with company_scope(co):
                results = generate_all(client, config_dir=co.config_dir, rerun=rerun)
        else:
            results = generate_all(client, rerun=rerun)

        summary = {
            role: {
                "criteria_count": len(data.get("criteria", [])),
                "total_pts":      sum(c["max"] for c in data.get("criteria", [])),
                "labels":         [c["label"] for c in data.get("criteria", [])],
            }
            for role, data in results.items()
        }
        return jsonify({"ok": True, "roles": summary})
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


# ── Agent runner status ───────────────────────────────────────────────────────

@app.route("/api/agent-status")
@login_required
def api_agent_status():
    try:
        from agents.runner import get_status
        return jsonify(get_status())
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── Error handler ─────────────────────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": e.description}), e.code
        return e
    return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Silence Flask's per-request logging for the high-frequency poll endpoints
    import logging as _logging
    _wz = _logging.getLogger("werkzeug")
    _poll_paths = {"/api/agent-status", "/api/candidates", "/api/conversations",
                   "/api/sync-status", "/api/auth-status"}
    _orig_log = _wz.info
    def _filtered_log(msg, *a, **kw):
        # werkzeug request lines look like: "GET /api/foo HTTP/1.1" 200 -
        if any(p in (msg % a if a else msg) for p in _poll_paths):
            return
        _orig_log(msg, *a, **kw)
    _wz.info = _filtered_log

    # Start AutoGen agent runner in background
    try:
        from agents.runner import start as start_runner
        start_runner()
        print("[app] Agent runner started")
    except Exception as e:
        print(f"[app] Agent runner could not start: {e}")

    print("\n  RMS Recruiter Portal")
    print("  Open: http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
