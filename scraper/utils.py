"""Shared utilities: paths, JSON helpers, logging."""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")          # load .env once when utils is imported

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"

CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
SESSION_STATE_FILE = CONFIG_DIR / "session_state.json"
CANDIDATES_DIR    = DATA_DIR / "candidates"
CONVERSATIONS_DIR = DATA_DIR / "conversations"
CANDIDATES_INDEX_FILE = DATA_DIR / "candidates_index.json"
LAST_RUN_FILE = DATA_DIR / "last_run.json"
AUDIT_LOG_FILE = DATA_DIR / "audit_log.json"
STATUS_COUNTS_FILE = DATA_DIR / "job_status_counts.json"

JOBS_URL = (
    "https://employers.indeed.com/jobs"
    "?status=open%2Cpaused&claimed=false&createdOnIndeed=true"
    "&tab=0&sortDirection=DESC&sortField=datePostedOnIndeed"
)


def get_logger(name: str) -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = logging.FileHandler(LOGS_DIR / "scraper.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_credentials() -> dict:
    # Start with the JSON file (may not exist on fresh clones)
    creds = load_json(CREDENTIALS_FILE) or {}

    # Overlay env vars — these always win over the JSON values
    env_map = {
        "GROQ_API_KEY":     "groq_api_key",
        "ANTHROPIC_API_KEY": "anthropic_api_key",
        "INDEED_EMAIL":     "indeed_email",
    }
    for env_var, key in env_map.items():
        val = os.environ.get(env_var, "").strip()
        if val:
            creds[key] = val

    if not creds:
        raise FileNotFoundError(
            "No credentials found. Create a .env file (copy .env.example) "
            "or copy config/credentials.template.json → config/credentials.json."
        )
    return creds


def load_last_run() -> dict:
    result = load_json(LAST_RUN_FILE, default={"last_run_at": None, "applicants_seen": []})
    return result or {"last_run_at": None, "applicants_seen": []}


def save_last_run(data: dict):
    save_json(LAST_RUN_FILE, data)


def load_candidates_index() -> list:
    result = load_json(CANDIDATES_INDEX_FILE, default=[])
    return result or []


def save_candidates_index(data: list):
    save_json(CANDIDATES_INDEX_FILE, data)


def load_status_counts() -> dict:
    return load_json(STATUS_COUNTS_FILE, default={}) or {}


def save_status_counts(data: dict):
    save_json(STATUS_COUNTS_FILE, data)


def append_audit_log(entry: dict):
    log: list = load_json(AUDIT_LOG_FILE, default=[]) or []
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    log.append(entry)
    save_json(AUDIT_LOG_FILE, log)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
