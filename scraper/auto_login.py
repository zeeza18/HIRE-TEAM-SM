"""
Automated Indeed login — fills email, waits for OTP via file handoff.
Run:  python scraper/auto_login.py --slug rms
Then: paste OTP in chat; Claude will write it to config/_otp.txt
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BASE      = Path(__file__).parent.parent
OTP_FILE  = BASE / "config" / "_otp.txt"
STAT_FILE = BASE / "config" / "_login_status.txt"

def _status(msg: str):
    STAT_FILE.write_text(msg, encoding="utf-8")
    print(f"[{msg}]", flush=True)

def _wait_otp(timeout=300) -> str:
    if OTP_FILE.exists():
        OTP_FILE.unlink()
    _status("WAITING_FOR_OTP")
    start = time.time()
    while time.time() - start < timeout:
        if OTP_FILE.exists():
            code = OTP_FILE.read_text(encoding="utf-8").strip()
            OTP_FILE.unlink()
            return code
        time.sleep(1)
    raise TimeoutError("OTP not provided in time")

def main(slug: str):
    from agents.company import COMPANIES
    co = COMPANIES.get(slug)
    if not co:
        print(f"Unknown slug: {slug}"); sys.exit(1)

    creds = json.loads(co.credentials_file.read_text(encoding="utf-8"))
    email = creds["indeed_email"].strip()
    session_file = co.session_state_file
    session_file.parent.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    _status("STARTING")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )
        ctx  = browser.new_context(viewport=None)
        page = ctx.new_page()

        _status("NAVIGATING")
        page.goto("https://secure.indeed.com/auth?hl=en_US&co=US",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2000)

        # Fill email
        _status("ENTERING_EMAIL")
        try:
            inp = page.locator("input[type='email'], input[name='__email']").first
            inp.wait_for(state="visible", timeout=10_000)
            inp.fill(email)
            page.wait_for_timeout(500)
            inp.press("Enter")
            page.wait_for_timeout(3000)
        except Exception as e:
            _status(f"EMAIL_STEP_FAILED: {e}")

        # If Google OAuth button appeared, click it
        try:
            google_btn = page.locator("text=Continue with Google, button:has-text('Google')").first
            if google_btn.is_visible(timeout=3000):
                _status("GOOGLE_OAUTH_DETECTED_CLICK_IN_BROWSER")
                page.wait_for_timeout(8000)  # give user time to click Google in browser
        except Exception:
            pass

        # Wait for OTP field
        _status("WAITING_FOR_OTP_FIELD")
        otp_sel = "input[name='__verification_code'], input[autocomplete='one-time-code'], input[type='number'][maxlength], input[inputmode='numeric']"
        try:
            page.locator(otp_sel).first.wait_for(state="visible", timeout=30_000)
            _status("OTP_FIELD_VISIBLE")
        except PWTimeout:
            _status("OTP_FIELD_NOT_FOUND_CONTINUING")

        # Get OTP from file (Claude writes it)
        otp = _wait_otp(timeout=300)
        _status(f"GOT_OTP")

        try:
            otp_inp = page.locator(otp_sel).first
            otp_inp.fill(otp)
            page.wait_for_timeout(500)
            otp_inp.press("Enter")
            page.wait_for_timeout(4000)
        except Exception as e:
            _status(f"OTP_ENTRY_FAILED: {e}")

        # Navigate to employer jobs page to capture employer cookies
        _status("NAVIGATING_TO_EMPLOYER_DASHBOARD")
        try:
            page.goto(
                "https://employers.indeed.com/jobs?status=open%2Cpaused",
                wait_until="domcontentloaded", timeout=30_000
            )
            page.wait_for_timeout(3000)
        except Exception:
            pass

        ctx.storage_state(path=str(session_file))
        _status(f"SAVED")

        print(f"\nSession saved to: {session_file}")
        print("You can close this window.")
        browser.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True, choices=["rms", "sm"])
    args = p.parse_args()
    main(args.slug)
