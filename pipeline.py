"""
Pipeline: scrape (ask per role) → enrich+score per candidate → export → cron loop
Run: python pipeline.py --visible
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scraper.utils import CANDIDATES_DIR, get_logger, load_json, load_credentials

log = get_logger("pipeline")
EXPORTS_DIR = CANDIDATES_DIR.parent / "exports"


# ── Enrich one candidate ──────────────────────────────────────────────────────

def enrich_one(path: Path, client):
    from scraper.enrich import process_one, _needs_enrich
    data = load_json(path, {})
    if not _needs_enrich(data):
        log.info(f"  [enrich] already done — skipping")
        return
    name = data.get("full_name", path.stem)
    log.info(f"  [enrich] {name}...")
    process_one(path, client)


# ── Score one candidate ───────────────────────────────────────────────────────

def score_one(path: Path, client):
    from scraper.score import process_one
    data = load_json(path, {})
    if data.get("fit_score") is not None:
        log.info(f"  [score]  already done — skipping")
        return
    name = data.get("full_name", path.stem)
    log.info(f"  [score]  {name}...")
    if process_one(path, client):
        bd = load_json(path, {}).get("score_breakdown", {})
        log.info(f"  [score]  fit={bd.get('fit_score')}  req={bd.get('requirements_score')}/50  jd={bd.get('jd_score')}/50")


# ── Step 1: scrape + immediate enrich+score per candidate ─────────────────────

def step_scrape(client, visible: bool = False, n_candidates: int | None = None):
    from scraper.indeed_scraper import run as scraper_run
    log.info("\n=== STEP 1: SCRAPE + ENRICH + SCORE (per candidate) ===")

    def on_candidate_saved(path: Path):
        enrich_one(path, client)
        score_one(path, client)

    scraper_run(
        visible=visible,
        ask_roles=True,
        role_timeout=1800,          # 30-min auto-yes on first run
        n_candidates=n_candidates,
        on_candidate_saved=on_candidate_saved,
    )


# ── Step 2: catch-up pass (any candidates missed due to errors) ───────────────

def step_catchup(client):
    log.info("\n=== STEP 2: CATCH-UP (enrich+score any missed candidates) ===")
    from scraper.enrich import _needs_enrich

    paths = sorted(CANDIDATES_DIR.glob("*.json"))
    missed_enrich = [p for p in paths if _needs_enrich(load_json(p, {}))]
    missed_score  = [p for p in paths if load_json(p, {}).get("fit_score") is None]

    if not missed_enrich and not missed_score:
        log.info("  Nothing missed — all caught up.")
        return

    done = set()
    for path in missed_enrich:
        log.info(f"  Catch-up enrich: {load_json(path,{}).get('full_name', path.stem)}")
        enrich_one(path, client)
        done.add(path)
        time.sleep(0.3)

    for path in missed_score:
        log.info(f"  Catch-up score:  {load_json(path,{}).get('full_name', path.stem)}")
        score_one(path, client)
        time.sleep(0.3)


# ── Step 3: export ────────────────────────────────────────────────────────────

def step_export():
    log.info("\n=== STEP 3: EXPORT ===")
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    by_role: dict[str, list] = {}
    for p in sorted(CANDIDATES_DIR.glob("*.json")):
        d = load_json(p, {})
        if not d:
            continue
        role = d.get("job_title") or "Unknown"
        by_role.setdefault(role, []).append(d)

    for role, candidates in sorted(by_role.items()):
        candidates.sort(key=lambda c: c.get("fit_score") or 0, reverse=True)
        out = EXPORTS_DIR / f"{role.replace('/', '-').strip()}.json"
        out.write_text(json.dumps(candidates, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info(f"  {role}: {len(candidates)} candidates → {out.name}")

    try:
        from scraper.excel_export import export_all
        export_all()
    except Exception as e:
        log.warning(f"Excel export failed: {e}")


# ── Cron cycle (new-only check every 2h) ─────────────────────────────────────

def cron_cycle(client, visible: bool):
    """Single cron tick: check New tab → enrich+score any new → export."""
    from scraper.indeed_scraper import run as scraper_run
    log.info(f"\n{'='*60}")
    log.info(f"CRON  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  [new-only]")
    log.info(f"{'='*60}")

    new_paths: list[Path] = []

    def on_candidate_saved(path: Path):
        new_paths.append(path)
        enrich_one(path, client)
        score_one(path, client)

    scraper_run(
        visible=visible,
        ask_roles=True,
        new_only=True,
        role_timeout=600,           # 10-min auto-yes in cron
        on_candidate_saved=on_candidate_saved,
    )

    if new_paths:
        log.info(f"  Cron: {len(new_paths)} new candidate(s) — exporting...")
        step_export()
    else:
        log.info("  Cron: no new candidates this cycle.")

    next_at = datetime.now().strftime('%H:%M:%S')
    log.info(f"  Next check in 2 hours (around {next_at})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Full pipeline: scrape → enrich → score → export → cron")
    parser.add_argument("--visible", action="store_true", help="Show browser window")
    parser.add_argument("-n", "--n-candidates", type=int, default=None,
                        help="Random-pick N candidates per role (test mode)")
    args = parser.parse_args()

    creds = load_credentials()
    groq_key = creds.get("groq_api_key")
    if not groq_key:
        log.error("groq_api_key missing from config/credentials.json")
        sys.exit(1)
    try:
        from groq import Groq
    except ImportError:
        log.error("pip install groq")
        sys.exit(1)

    client = Groq(api_key=groq_key)

    # ── Phase 1: full scrape ──────────────────────────────────────────────────
    step_scrape(client, visible=args.visible, n_candidates=args.n_candidates)
    step_catchup(client)
    step_export()
    log.info("\nInitial pipeline complete.")

    # ── Phase 2: cron loop (every 2 hours, new-only) ─────────────────────────
    if args.n_candidates is not None:
        log.info("Test mode (-n): skipping cron loop.")
        return

    log.info("\nEntering cron loop — checking for new applicants every 2 hours.")
    log.info("Press Ctrl+C to stop.\n")

    INTERVAL = 2 * 60 * 60   # 2 hours in seconds

    try:
        while True:
            time.sleep(INTERVAL)
            try:
                cron_cycle(client, visible=args.visible)
            except Exception as e:
                log.warning(f"Cron cycle error (will retry next tick): {e}")
    except KeyboardInterrupt:
        log.info("\nCron loop stopped by user.")


if __name__ == "__main__":
    main()
