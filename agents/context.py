"""
Per-company context manager: redirects all scraper module-level path constants
to the correct company directories for the duration of the block.

Each company gets its own threading.Lock so RMS and SM can run simultaneously
(they use different Indeed accounts, so concurrent sessions are fine).
"""
import threading
from contextlib import contextmanager
from agents.company import Company

# One lock per company so different companies can run at the same time
_COMPANY_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


def _company_lock(slug: str) -> threading.Lock:
    with _LOCKS_LOCK:
        if slug not in _COMPANY_LOCKS:
            _COMPANY_LOCKS[slug] = threading.Lock()
        return _COMPANY_LOCKS[slug]


@contextmanager
def company_scope(company: Company):
    import scraper.utils as _u
    import scraper.indeed_scraper as _s
    import scraper.messenger as _m
    import scraper.score as _sc
    import scraper.enrich as _en
    import scraper.conversations_scraper as _cv
    import scraper.ai_responder as _ar

    with _company_lock(company.slug):
        company.bootstrap()

        # snapshot
        saved = {
            "u_SESSION":       _u.SESSION_STATE_FILE,
            "u_CANDS":         _u.CANDIDATES_DIR,
            "u_DATA":          _u.DATA_DIR,
            "u_CONFIG":        _u.CONFIG_DIR,
            "u_CREDS":         _u.CREDENTIALS_FILE,
            "u_CONV":          _u.CONVERSATIONS_DIR,
            "u_INDEX":         _u.CANDIDATES_INDEX_FILE,
            "u_LASTRUN":       _u.LAST_RUN_FILE,
            "u_AUDIT":         _u.AUDIT_LOG_FILE,
            "u_COUNTS":        _u.STATUS_COUNTS_FILE,
            "s_SESSION":       _s.SESSION_STATE_FILE,
            "s_CANDS":         _s.CANDIDATES_DIR,
            "s_RESUMES":       _s.RESUMES_DIR,
            "s_JD":            _s.JD_FILE,
            "m_SESSION":       _m.SESSION_STATE_FILE,
            "m_CANDS":         _m.CANDIDATES_DIR,
            "m_TEMPLATES":     _m.TEMPLATES_FILE,
            "m_SETTINGS":      _m.SETTINGS_FILE,
            "sc_CANDS":        _sc.CANDIDATES_DIR,
            "sc_JD":           _sc.JD_FILE,
            "en_CANDS":        _en.CANDIDATES_DIR,
            "cv_SESSION":      _cv.SESSION_STATE_FILE,
            "cv_CONV":         _cv.CONVERSATIONS_DIR,
            "cv_DATA":         _cv.DATA_DIR,
            "cv_CONFIG":       _cv.CONFIG_DIR,
            "ar_SETTINGS":     _ar.SETTINGS_FILE,
        }

        # patch all local path constants in every scraper module
        _u.SESSION_STATE_FILE  = company.session_state_file
        _u.CANDIDATES_DIR      = company.candidates_dir
        _u.DATA_DIR            = company.data_dir
        _u.CONFIG_DIR          = company.config_dir
        _u.CREDENTIALS_FILE    = company.credentials_file
        _u.CONVERSATIONS_DIR   = company.conversations_dir
        _u.CANDIDATES_INDEX_FILE = company.candidates_index_file
        _u.LAST_RUN_FILE       = company.last_run_file
        _u.AUDIT_LOG_FILE      = company.audit_log_file
        _u.STATUS_COUNTS_FILE  = company.status_counts_file
        _s.SESSION_STATE_FILE  = company.session_state_file
        _s.CANDIDATES_DIR      = company.candidates_dir
        _s.RESUMES_DIR         = company.resumes_dir
        _s.JD_FILE             = company.job_descriptions_file
        _m.SESSION_STATE_FILE  = company.session_state_file
        _m.CANDIDATES_DIR      = company.candidates_dir
        _m.TEMPLATES_FILE      = company.config_dir / "message_templates.json"
        _m.SETTINGS_FILE       = company.config_dir / "settings.json"
        _sc.CANDIDATES_DIR     = company.candidates_dir
        _sc.JD_FILE            = company.job_descriptions_file
        _en.CANDIDATES_DIR     = company.candidates_dir
        _cv.SESSION_STATE_FILE = company.session_state_file
        _cv.CONVERSATIONS_DIR  = company.conversations_dir
        _cv.DATA_DIR           = company.data_dir    # used by snap() for screenshots path
        _cv.CONFIG_DIR         = company.config_dir  # used by _sender_name()
        _ar.SETTINGS_FILE      = company.config_dir / "settings.json"

        try:
            yield company
        finally:
            # restore
            _u.SESSION_STATE_FILE  = saved["u_SESSION"]
            _u.CANDIDATES_DIR      = saved["u_CANDS"]
            _u.DATA_DIR            = saved["u_DATA"]
            _u.CONFIG_DIR          = saved["u_CONFIG"]
            _u.CREDENTIALS_FILE    = saved["u_CREDS"]
            _u.CONVERSATIONS_DIR   = saved["u_CONV"]
            _u.CANDIDATES_INDEX_FILE = saved["u_INDEX"]
            _u.LAST_RUN_FILE       = saved["u_LASTRUN"]
            _u.AUDIT_LOG_FILE      = saved["u_AUDIT"]
            _u.STATUS_COUNTS_FILE  = saved["u_COUNTS"]
            _s.SESSION_STATE_FILE  = saved["s_SESSION"]
            _s.CANDIDATES_DIR      = saved["s_CANDS"]
            _s.RESUMES_DIR         = saved["s_RESUMES"]
            _s.JD_FILE             = saved["s_JD"]
            _m.SESSION_STATE_FILE  = saved["m_SESSION"]
            _m.CANDIDATES_DIR      = saved["m_CANDS"]
            _m.TEMPLATES_FILE      = saved["m_TEMPLATES"]
            _m.SETTINGS_FILE       = saved["m_SETTINGS"]
            _sc.CANDIDATES_DIR     = saved["sc_CANDS"]
            _sc.JD_FILE            = saved["sc_JD"]
            _en.CANDIDATES_DIR     = saved["en_CANDS"]
            _cv.SESSION_STATE_FILE = saved["cv_SESSION"]
            _cv.CONVERSATIONS_DIR  = saved["cv_CONV"]
            _cv.DATA_DIR           = saved["cv_DATA"]
            _cv.CONFIG_DIR         = saved["cv_CONFIG"]
            _ar.SETTINGS_FILE      = saved["ar_SETTINGS"]
