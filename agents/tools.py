"""
Tool functions called by AutoGen agents.
Each function is one atomic pipeline step for a specific company.
All return "OK: ..." on success or "ERROR: ..." on failure.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

from dotenv import load_dotenv as _load_dotenv
from pathlib import Path as _Path
_load_dotenv(_Path(__file__).parent.parent / ".env")

from agents.company import COMPANIES
from agents.context import company_scope


# ── Role-specific auto/manual mode ───────────────────────────────────────────
# Automated actions (auto-contact, auto-reject, auto-reply) are off by default
# for every role. A role only runs automatically once its flag is explicitly
# set to True via the "Auto" toggle in the UI for that role's tab.

def _role_auto_mode(co, role: str) -> bool:
    try:
        s = json.loads((co.config_dir / "settings.json").read_text(encoding="utf-8"))
        return bool(s.get("auto_mode_roles", {}).get(role, False))
    except Exception:
        return False


def _known_job_postings(co) -> dict[str, list[str]]:
    """title -> [job_id, ...]. Distinct Indeed postings can share the exact
    same title text (a role reposted, several near-identical listings), so a
    title alone is not a unique key for auto-mode gating — only job_id is.
    """
    postings: dict[str, set[str]] = {}
    try:
        for f in co.candidates_dir.glob("*.json"):
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
    """Conversation threads only ever carry the job TITLE as scraped text
    (Indeed's messaging UI doesn't expose the posting's job_id), so this can
    only match by title — if that title is shared by several distinct
    postings, all of their job_ids are returned together.
    """
    job_info = job_info or ""
    for title in sorted(postings, key=len, reverse=True):  # longest first avoids partial-substring mismatches
        if title and title in job_info:
            return postings[title]
    return None


# ── 1. Scrape new candidates ─────────────────────────────────────────────────

def scrape_new_candidates(company_slug: str, new_only: bool = True) -> str:
    co = COMPANIES.get(company_slug)
    if not co:
        return f"ERROR: Unknown company '{company_slug}'"
    if not co.session_state_file.exists():
        return f"ERROR: No Indeed session for {co.display_name}. Run scraper/indeed_login.py first."
    try:
        # Build an OpenAI client once so each saved candidate is scored immediately
        creds   = co.load_credentials()
        api_key = creds.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        score_client = None
        if api_key:
            try:
                from openai import OpenAI
                score_client = OpenAI(api_key=api_key)
            except Exception as _ex:
                import sys as _sys
                print(f"[score-cb] OpenAI client init failed: {_ex}", flush=True, file=_sys.stderr)

        def _score_on_save(path):
            import logging as _log, sys as _sys
            print(f"[score-cb] fired: {path.name}", flush=True, file=_sys.stderr)
            _slog = _log.getLogger("score")
            _slog.info(f"  [score-cb] callback fired: {path.name}")
            if score_client is None:
                print("[score-cb] no client!", flush=True, file=_sys.stderr)
                return
            try:
                from scraper.score import process_one
                process_one(path, score_client)
            except Exception as _e:
                print(f"[score-cb] ERROR: {_e}", flush=True, file=_sys.stderr)
                _slog.warning(f"  [score-cb] error: {_e}")

        with company_scope(co):
            from scraper.indeed_scraper import run, has_new_candidates
            if new_only:
                if not has_new_candidates():
                    return f"OK: No new candidates for {co.display_name} — skipped"
            run(new_only=new_only, on_candidate_saved=_score_on_save)
        mode = "new-only" if new_only else "all tabs"
        return f"OK: Scraped {mode} for {co.display_name}"
    except Exception as e:
        return f"ERROR: Scrape failed for {co.display_name}: {e}"


# ── 2. Score unscored candidates ─────────────────────────────────────────────

def score_candidates(company_slug: str) -> str:
    co = COMPANIES.get(company_slug)
    if not co:
        return f"ERROR: Unknown company '{company_slug}'"
    creds   = co.load_credentials()
    api_key = creds.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return f"ERROR: No OPENAI_API_KEY for {co.display_name}"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        with company_scope(co):
            from scraper.score import process_one
            to_score = [
                p for p in sorted(co.candidates_dir.glob("*.json"))
                if json.loads(p.read_text(encoding="utf-8")).get("fit_score") is None
            ]
            if not to_score:
                return f"OK: No unscored candidates for {co.display_name}"
            ok = fail = 0
            for p in to_score:
                try:
                    result = process_one(p, client)
                    ok   += 1 if result else 0
                    fail += 0 if result else 1
                except Exception:
                    fail += 1
                time.sleep(1)
        return f"OK: Scored {ok} candidates for {co.display_name} ({fail} failed)"
    except Exception as e:
        return f"ERROR: Scoring failed for {co.display_name}: {e}"


# ── 2b. Enrich profile text fields (summary/experience/certs/skills) ────────

def enrich_candidates(company_slug: str) -> str:
    co = COMPANIES.get(company_slug)
    if not co:
        return f"ERROR: Unknown company '{company_slug}'"
    creds   = co.load_credentials()
    api_key = creds.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return f"ERROR: No OPENAI_API_KEY for {co.display_name}"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        with company_scope(co):
            from scraper.enrich import process_one, _needs_enrich
            to_enrich = [
                p for p in sorted(co.candidates_dir.glob("*.json"))
                if _needs_enrich(json.loads(p.read_text(encoding="utf-8")))
            ]
            if not to_enrich:
                return f"OK: No candidates need enrichment for {co.display_name}"
            ok = fail = 0
            for p in to_enrich:
                try:
                    result = process_one(p, client)
                    ok   += 1 if result else 0
                    fail += 0 if result else 1
                except Exception:
                    fail += 1
                time.sleep(0.3)
        return f"OK: Enriched {ok} candidates for {co.display_name} ({fail} failed)"
    except Exception as e:
        return f"ERROR: Enrichment failed for {co.display_name}: {e}"


# ── 3. Recruit: auto-contact ≥ threshold, auto-reject < threshold ────────────

def recruit_candidates(company_slug: str, threshold: int = 80) -> str:
    co = COMPANIES.get(company_slug)
    if not co:
        return f"ERROR: Unknown company '{company_slug}'"
    if not co.session_state_file.exists():
        return f"ERROR: No Indeed session for {co.display_name}"

    contacted = rejected = skipped = errors = 0
    lines = []

    try:
        for p in sorted(co.candidates_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue

            score  = d.get("fit_score")
            status = (d.get("status") or "new").lower()
            cid    = d.get("id", "")
            name   = d.get("full_name", "")

            if score is None:
                skipped += 1; continue
            if d.get("indeed_message_sent") or d.get("indeed_rejected"):
                skipped += 1; continue
            if status in ("interviewing", "hired", "contacting", "rejected"):
                skipped += 1; continue
            if not _role_auto_mode(co, d.get("job_id", "")):
                skipped += 1; continue

            with company_scope(co):
                from scraper.messenger import send_message, get_drafts
                if score >= threshold:
                    drafts  = get_drafts(cid)
                    invites = drafts.get("interview_invite", [])
                    msg = invites[0]["body"] if invites else (
                        f"Hi {name.split()[0] if name else 'there'}, "
                        "thank you for applying! We'd love to connect. "
                        "Please book a call at your convenience."
                    )
                    r = send_message(cid, msg, new_status="contacting")
                    if r.get("ok"):
                        contacted += 1
                        lines.append(f"  Contacted: {name} (score={score})")
                    else:
                        errors += 1
                        lines.append(f"  Contact FAILED: {name}: {r.get('error')}")
                else:
                    r = send_message(cid, "", new_status="rejected")
                    if r.get("ok"):
                        rejected += 1
                        lines.append(f"  Rejected: {name} (score={score})")
                    else:
                        errors += 1
                        lines.append(f"  Reject FAILED: {name}: {r.get('error')}")

        summary = (
            f"OK: {co.display_name} — contacted={contacted}, "
            f"rejected={rejected}, skipped={skipped}, errors={errors}"
        )
        return summary + ("\n" + "\n".join(lines[:30]) if lines else "")
    except Exception as e:
        return f"ERROR: Recruiter action failed for {co.display_name}: {e}"


# ── 4. Scrape conversations ──────────────────────────────────────────────────

def scrape_conversations(company_slug: str) -> str:
    co = COMPANIES.get(company_slug)
    if not co:
        return f"ERROR: Unknown company '{company_slug}'"
    if not co.session_state_file.exists():
        return f"ERROR: No Indeed session for {co.display_name}"
    try:
        # If we already have a saved index, only re-check today's conversations.
        # Conversations with "Jun 3" / "Jun 8" list-timestamps are skipped
        # automatically by _is_recent_timestamp — no need to re-open them.
        since = None
        idx_file = co.conversations_dir / "index.json"
        if idx_file.exists():
            try:
                if json.loads(idx_file.read_text(encoding="utf-8")):
                    since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            except Exception:
                pass

        with company_scope(co):
            from scraper.conversations_scraper import scrape
            threads = scrape(max_threads=50, since=since)
        return f"OK: Scraped {len(threads)} conversation(s) for {co.display_name}"
    except Exception as e:
        return f"ERROR: Conversation scrape failed for {co.display_name}: {e}"


# ── 5. Auto-reply to all unanswered inbound messages ────────────────────────

def auto_reply_conversations(company_slug: str) -> str:
    co = COMPANIES.get(company_slug)
    if not co:
        return f"ERROR: Unknown company '{company_slug}'"
    if not co.session_state_file.exists():
        return f"ERROR: No Indeed session for {co.display_name}"

    postings = _known_job_postings(co)

    creds   = co.load_credentials()
    api_key = creds.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return f"ERROR: No OPENAI_API_KEY for {co.display_name}"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    replied = failed = skipped = 0
    try:
        index_file = co.conversations_dir / "index.json"
        if not index_file.exists():
            return f"OK: No conversations yet for {co.display_name}"

        index = json.loads(index_file.read_text(encoding="utf-8"))
        for entry in index:
            tid  = entry.get("thread_id", "")
            tfile = co.conversations_dir / f"{tid}.json"
            if not tfile.exists():
                continue

            thread   = json.loads(tfile.read_text(encoding="utf-8"))
            messages = thread.get("messages", [])
            if not messages or messages[-1].get("direction") != "inbound":
                skipped += 1; continue

            # Conversation threads only carry the job's title text (Indeed's
            # messaging UI has no job_id), so if that title is shared by
            # several distinct postings, ALL of them must have Auto on —
            # otherwise a manual posting could get auto-replied just because
            # it shares a title with an unrelated auto-enabled one.
            job_ids = _resolve_job_ids(thread.get("job_info"), postings)
            if not job_ids or not all(_role_auto_mode(co, jid) for jid in job_ids):
                skipped += 1; continue

            # skip if already replied after the last update
            if entry.get("auto_replied_at", "") >= thread.get("last_updated", "z"):
                skipped += 1; continue

            # Only auto-reply if the Indeed list shows a time-of-day ("2:30 PM"),
            # which means the last message arrived today.
            # Old dates ("Jun 3", "Nov 4, 2025"), empty, or anything else → skip.
            list_ts = entry.get("last_message_timestamp", "")
            if not re.match(r"\d+:\d+\s*(AM|PM)", list_ts.strip(), re.I):
                skipped += 1; continue

            try:
                os.environ["OPENAI_API_KEY"] = api_key
                with company_scope(co):
                    from scraper.ai_responder import generate_reply
                    from scraper.conversations_scraper import send_reply
                    reply = generate_reply(thread)

                # ── Guardrails ────────────────────────────────────────────
                r = reply.strip()

                # AI instructed to skip this message
                if r.upper() == "SKIP":
                    skipped += 1; continue

                # Too short or looks like an error/AI hallucination
                if len(r) < 20:
                    skipped += 1; continue

                _BAD = ("as an ai", "i'm an ai", "i don't have access",
                        "i cannot", "i'm unable", "undefined")
                if any(b in r.lower() for b in _BAD):
                    skipped += 1; continue

                # one auto-reply per thread per day
                replied_at = entry.get("auto_replied_at", "")
                if replied_at and replied_at[:10] >= today:
                    skipped += 1; continue
                # ─────────────────────────────────────────────────────────

                with company_scope(co):
                    from scraper.conversations_scraper import send_reply
                    result = send_reply(tid, reply)
                if result.get("ok"):
                    replied += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        return (
            f"OK: {co.display_name} — replied={replied}, "
            f"failed={failed}, skipped={skipped}"
        )
    except Exception as e:
        return f"ERROR: Auto-reply failed for {co.display_name}: {e}"
