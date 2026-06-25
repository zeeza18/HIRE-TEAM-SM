"""
Background scheduler: runs pipelines for all companies on fixed intervals.

Each company pipeline runs as its own subprocess so that module-level global
variables (SESSION_STATE_FILE, CANDIDATES_DIR, etc.) are completely isolated.

Sequential execution order (no parallelism between companies):
  1. RMS candidates (scrape + score)
  2. SM candidates  (scrape + score)
  3. RMS messaging
  4. SM messaging

Startup sequence:
  1. Full candidate pipeline for each company in order (new_only=False)
  2. Then messaging for each company in order

Subsequent schedule:
  - Candidate pipeline: every 2 hours, sequential RMS → SM
  - Messaging pipeline: every 1 hour,  sequential RMS → SM
"""
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from agents.company import COMPANIES

log = logging.getLogger("agents.runner")

CANDIDATE_INTERVAL_H = 2
MESSAGING_INTERVAL_H = 1
SCORE_THRESHOLD      = 80

_BASE = Path(__file__).parent.parent
_SCRIPT = str(Path(__file__).parent / "run_pipeline.py")

# Persists candidate_runs / messaging_runs / first_done across Flask restarts,
# so a company that already had its full first-time scrape doesn't repeat one
# every time app.py starts back up — it just resumes incremental (new_only) runs.
_RUNNER_STATE_FILE = _BASE / "config" / "runner_state.json"


def _load_runner_state() -> dict:
    try:
        if _RUNNER_STATE_FILE.exists():
            return json.loads(_RUNNER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"[runner] Failed to load persisted state: {e}")
    return {}


def _save_runner_state():
    try:
        with _lock:
            data = {
                "candidate_runs": dict(_state["candidate_runs"]),
                "messaging_runs": dict(_state["messaging_runs"]),
                "first_done":     dict(_state["first_done"]),
            }
        _RUNNER_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log.error(f"[runner] Failed to persist state: {e}")


_persisted = _load_runner_state()

_lock = threading.Lock()
_state: dict = {
    "running":        False,
    "active":         0,      # pipelines currently executing
    "candidate_runs": _persisted.get("candidate_runs", {}),  # slug -> ISO string
    "messaging_runs": _persisted.get("messaging_runs", {}),  # slug -> ISO string
    "last_errors":    {},                                    # slug -> str
    "first_done":     _persisted.get("first_done", {}),       # slug -> bool
}


def _spawn(slug: str, pipeline: str, new_only: bool = False) -> int:
    """Run one company's pipeline in its own process. Returns exit code."""
    cmd = [sys.executable, _SCRIPT, "--slug", slug, "--pipeline", pipeline]
    if new_only:
        cmd.append("--new-only")
    try:
        result = subprocess.run(cmd, cwd=str(_BASE))
        return result.returncode
    except Exception as e:
        log.error(f"[runner] subprocess error [{slug}/{pipeline}]: {e}")
        return 1


def _run_candidate(slug: str, new_only: bool = True):
    log.info(f"[runner] Candidate pipeline ({'new-only' if new_only else 'FULL'}) -> {slug}")
    with _lock: _state["active"] += 1
    try:
        rc = _spawn(slug, "candidate", new_only=new_only)
        if rc == 0:
            with _lock:
                _state["candidate_runs"][slug] = datetime.now().isoformat()
                _state["first_done"][slug]     = True
            _save_runner_state()
        else:
            with _lock:
                _state["last_errors"][slug] = f"candidate subprocess exited {rc}"
    except Exception as e:
        log.error(f"[runner] Candidate error [{slug}]: {e}")
        with _lock:
            _state["last_errors"][slug] = str(e)
    finally:
        with _lock: _state["active"] = max(0, _state["active"] - 1)


def _run_messaging(slug: str):
    log.info(f"[runner] Messaging pipeline -> {slug}")
    with _lock: _state["active"] += 1
    try:
        rc = _spawn(slug, "messaging")
        if rc == 0:
            with _lock:
                _state["messaging_runs"][slug] = datetime.now().isoformat()
            _save_runner_state()
        else:
            with _lock:
                _state["last_errors"][slug] = f"messaging subprocess exited {rc}"
    except Exception as e:
        log.error(f"[runner] Messaging error [{slug}]: {e}")
        with _lock:
            _state["last_errors"][slug] = str(e)
    finally:
        with _lock: _state["active"] = max(0, _state["active"] - 1)


def _active_companies():
    return [(slug, co) for slug, co in COMPANIES.items()
            if co.session_state_file.exists()]


def _run_sequential(target, slugs: list[str], **kwargs):
    """Run target(slug) for each slug one at a time in order."""
    for slug in slugs:
        target(slug, **kwargs)


def _loop():
    time.sleep(15)  # let Flask finish starting up

    # ── First run: candidates, sequential RMS → SM ────────────────────────────
    # A company already marked first_done (persisted from a prior full scrape,
    # whether run by the scheduler or manually) gets an incremental (new_only)
    # run instead of repeating a full scrape every time the app restarts.
    active = _active_companies()
    if active:
        slugs = [slug for slug, _ in active]
        log.info(f"[runner] First run — candidates (sequential): {slugs}")
        for slug in slugs:
            with _lock:
                already_done = _state["first_done"].get(slug, False)
            _run_candidate(slug, new_only=already_done)

        log.info(f"[runner] First run — messaging (sequential): {slugs}")
        _run_sequential(_run_messaging, slugs)

    # ── Ongoing schedule ──────────────────────────────────────────────────────
    while True:
        time.sleep(300)  # check every 5 minutes
        now    = datetime.now()
        active = _active_companies()

        cand_due = []
        msg_due  = []

        for slug, _ in active:
            with _lock:
                lc = _state["candidate_runs"].get(slug)
                lm = _state["messaging_runs"].get(slug)

            if lc is None or (now - datetime.fromisoformat(lc)) >= timedelta(hours=CANDIDATE_INTERVAL_H):
                cand_due.append(slug)
            if lm is None or (now - datetime.fromisoformat(lm)) >= timedelta(hours=MESSAGING_INTERVAL_H):
                msg_due.append(slug)

        if cand_due:
            threading.Thread(
                target=_run_sequential, args=(_run_candidate, cand_due),
                kwargs={"new_only": True}, daemon=True
            ).start()

        if msg_due:
            threading.Thread(
                target=_run_sequential, args=(_run_messaging, msg_due),
                daemon=True
            ).start()


def start():
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    threading.Thread(target=_loop, daemon=True).start()
    log.info("[runner] Agent runner started — each company runs in its own process (no global races)")


def get_status() -> dict:
    with _lock:
        return {
            "running":        _state["running"],
            "active":         _state["active"],
            "candidate_runs": dict(_state["candidate_runs"]),
            "messaging_runs": dict(_state["messaging_runs"]),
            "last_errors":    dict(_state["last_errors"]),
        }
