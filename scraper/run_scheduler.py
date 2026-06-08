"""
Scheduler — runs the Indeed scraper on a regular interval, then exports to Excel.

Keeps running until you press Ctrl+C.

Usage:
    python scraper/run_scheduler.py                  # every 4 hours (default)
    python scraper/run_scheduler.py --hours 2        # every 2 hours
    python scraper/run_scheduler.py --run-now        # run once immediately then schedule

What it does each cycle:
  1. python scraper/indeed_scraper.py   — scrape new applicants across all roles
  2. python scraper/excel_export.py     — update Excel files (per-role + ALL_ROLES)
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import schedule
except ImportError:
    print("schedule not installed. Run: pip install schedule")
    sys.exit(1)

from scraper.utils import get_logger

log = get_logger("scheduler")

PYTHON   = sys.executable          # same venv python that's running this script
ROOT_DIR = Path(__file__).parent.parent
SCRAPER  = ROOT_DIR / "scraper" / "indeed_scraper.py"
EXPORTER = ROOT_DIR / "scraper" / "excel_export.py"


def _run(label: str, script: Path, extra_args: list[str] | None = None) -> bool:
    log.info(f"  Running {label}...")
    cmd = [PYTHON, str(script)] + (extra_args or [])
    result = subprocess.run(cmd, cwd=str(ROOT_DIR))
    ok = result.returncode == 0
    if not ok:
        log.warning(f"  {label} exited with code {result.returncode}")
    return ok


def run_cycle(new_only: bool = True):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"\n{'='*60}")
    log.info(f"Cycle start: {now}  [mode: {'new-only' if new_only else 'full'}]")
    log.info(f"{'='*60}")

    # --visible required: Indeed blocks headless browsers with anti-bot detection
    extra = ["--visible", "--ask-roles"]
    if new_only:
        extra += ["--new-only", "--role-timeout", "600"]   # 10-min auto-yes in cron
    else:
        extra += ["--role-timeout", "1800"]                # 30-min auto-yes on manual run

    scrape_ok = _run("Scraper", SCRAPER, extra)
    if scrape_ok:
        log.info("  Scraper finished — exporting to Excel...")
    else:
        log.warning("  Scraper had errors — exporting whatever was saved...")

    _run("Excel export", EXPORTER)

    log.info(f"Cycle complete: {datetime.now().strftime('%H:%M:%S')}")


def main():
    parser = argparse.ArgumentParser(description="Scheduled Indeed scraper + Excel export")
    parser.add_argument("--hours",    type=float, default=2.0,
                        help="Interval between scrape runs in hours (default: 2)")
    parser.add_argument("--run-now",  action="store_true",
                        help="Run one full cycle immediately before starting the schedule")
    parser.add_argument("--full",     action="store_true",
                        help="Scrape all status tabs each cycle (default: new-only)")
    args = parser.parse_args()

    interval_h = max(0.5, args.hours)   # minimum 30 minutes
    new_only   = not args.full

    log.info(f"Scheduler starting — interval: every {interval_h:.1f} hour(s)  mode: {'new-only' if new_only else 'full'}")
    log.info(f"Scraper : {SCRAPER}")
    log.info(f"Exporter: {EXPORTER}")
    log.info(f"Press Ctrl+C to stop.\n")

    if args.run_now:
        run_cycle(new_only=False)   # first manual run always does full scrape

    # Schedule recurring job (new-only by default)
    interval_min = int(interval_h * 60)
    schedule.every(interval_min).minutes.do(run_cycle, new_only=new_only)

    next_run = schedule.next_run()
    log.info(f"Next run scheduled at: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)          # check every 30 seconds
    except KeyboardInterrupt:
        log.info("\nScheduler stopped by user.")


if __name__ == "__main__":
    main()
