"""
End-to-end test: scrape 3 SM candidates, score each one immediately via
on_candidate_saved callback, screenshot the UI before/after.

Usage: python scraper/test_score_pipeline.py
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

SCREENSHOTS = Path(__file__).parent.parent / "data" / "screenshots"
SCREENSHOTS.mkdir(parents=True, exist_ok=True)


def snap(page, name: str):
    path = SCREENSHOTS / f"test_pipeline_{name}.png"
    page.screenshot(path=str(path), full_page=False)
    print(f"  [snap] {path.name}")
    return path


def main():
    from agents.company import SM
    from agents.context import company_scope

    # ── 1. Check prerequisites ────────────────────────────────────────────────
    if not SM.session_state_file.exists():
        print("ERROR: No SM session. Run: python scraper/auto_login.py --slug sm")
        sys.exit(1)

    api_key = (SM.load_credentials().get("openai_api_key") or
               os.environ.get("OPENAI_API_KEY", ""))
    if not api_key:
        print("ERROR: No OPENAI_API_KEY")
        sys.exit(1)

    print(f"API key: {api_key[:15]}...")

    # ── 2. Build OpenAI client ────────────────────────────────────────────────
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    # ── 3. Track scores written by callback ───────────────────────────────────
    scores_written = []

    def on_candidate_saved(path: Path):
        from scraper.score import process_one
        print(f"\n  [score] Scoring: {path.name}")
        t0 = time.time()
        ok = process_one(path, client)
        elapsed = time.time() - t0
        if ok:
            d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            score = d.get("fit_score")
            name  = d.get("full_name", "?")
            print(f"  [score] {name}: fit_score={score}  ({elapsed:.1f}s)")
            scores_written.append({"name": name, "score": score, "path": str(path)})
        else:
            print(f"  [score] returned False (no JD or no text)")

    # ── 4. Scrape up to 3 new/reviewing candidates ────────────────────────────
    print("\n=== Scraping SM (n=3, reviewing tab) ===")
    with company_scope(SM):
        from scraper.indeed_scraper import run
        run(
            visible=False,
            new_only=False,
            n_candidates=3,
            on_candidate_saved=on_candidate_saved,
        )

    # ── 5. Report results ──────────────────────────────────────────────────────
    print(f"\n=== Results: {len(scores_written)} scored ===")
    for s in scores_written:
        print(f"  {s['name']:35} fit_score={s['score']}")

    # ── 6. Screenshot the UI (app must be running on :5000) ───────────────────
    print("\n=== Taking UI screenshot ===")
    try:
        from playwright.sync_api import sync_playwright
        try:
            from playwright_stealth.stealth import Stealth as _Stealth
            _stealth = True
        except ImportError:
            _stealth = False

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
            )
            page = ctx.new_page()
            if _stealth:
                _Stealth().apply_stealth_sync(page)

            # Hit local Flask app
            page.goto("http://localhost:5000", wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(2000)
            snap(page, "01_before_login")

            # Log in as SM if needed
            if page.locator("text=Sign in").count() > 0 or page.locator("input[type='email']").count() > 0:
                page.locator("input[type='email']").fill("muneeb@speechmasterservices.com")
                page.locator("input[type='password']").fill("")  # portal doesn't need password
                page.locator("button[type='submit'], button:has-text('Sign in')").first.click()
                page.wait_for_timeout(1500)

            # Select SM company if card exists
            sm_card = page.locator("[data-company='sm'], button:has-text('Speech Masters')").first
            if sm_card.count() > 0:
                sm_card.click()
                page.wait_for_timeout(1500)

            page.goto("http://localhost:5000", wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(2500)
            snap(page, "02_candidates_with_scores")

            # Verify at least one scored card is visible
            scored_cards = page.locator("text=/fit[: ]*[0-9]+/i, text=/score[: ]*[0-9]+/i, .score-badge, [data-score]").all()
            print(f"  Visible scored elements: {len(scored_cards)}")

            browser.close()
    except Exception as e:
        print(f"  UI screenshot failed: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
