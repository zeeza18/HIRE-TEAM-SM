"""
Scrape Indeed employer message conversations → data/conversations/

Usage (standalone):  python scraper/conversations_scraper.py
Usage (send reply):  from scraper.conversations_scraper import send_reply
"""
import hashlib
import json
import re
import sys
import time
import random
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, DATA_DIR, get_logger, save_json

log = get_logger("conversations_scraper")

JOBS_URL = (
    "https://employers.indeed.com/jobs"
    "?status=open%2Cpaused&claimed=false&createdOnIndeed=true"
    "&tab=0&sortDirection=DESC&sortField=datePostedOnIndeed"
)
CONVERSATIONS_DIR = DATA_DIR / "conversations"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# JS that walks each message content element → returns list of {content, direction, timestamp, date_group}
_JS_EXTRACT = """
() => {
    const msgs = [];
    const sections = document.querySelectorAll('[aria-label^="Messages from"]');
    if (sections.length > 0) {
        sections.forEach(section => {
            const h = section.querySelector('h2,[role="heading"]');
            const dg = h?.innerText?.trim() ||
                       section.getAttribute('aria-label')?.replace('Messages from ', '') || '';
            section.querySelectorAll(
                "[data-testid='indeed-messaging--ConversationEvents__messageContent']"
            ).forEach(el => {
                const txt = el.innerText?.trim();
                if (!txt) return;
                let blk = el, sent = false, ts = '';
                for (let i = 0; i < 8; i++) {
                    if (!blk.parentElement || blk === section) break;
                    blk = blk.parentElement;
                    const sEl = blk.querySelector(
                        "[data-testid='indeed-messaging--ConversationEventSender__statusLabel']"
                    );
                    if (sEl?.innerText?.toLowerCase().includes('sent')) sent = true;
                    const tEl = blk.querySelector(
                        "[data-testid='indeed-messaging--ConversationEventSender__timestamp']"
                    );
                    if (tEl) ts = tEl.innerText?.trim() || '';
                }
                msgs.push({ content: txt, direction: sent ? 'outbound' : 'inbound', timestamp: ts, date_group: dg });
            });
        });
    } else {
        document.querySelectorAll(
            "[data-testid='indeed-messaging--ConversationEvents__messageContent']"
        ).forEach(el => {
            const txt = el.innerText?.trim();
            if (!txt) return;
            let blk = el, sent = false, ts = '';
            for (let i = 0; i < 8; i++) {
                if (!blk.parentElement) break;
                blk = blk.parentElement;
                const sEl = blk.querySelector(
                    "[data-testid='indeed-messaging--ConversationEventSender__statusLabel']"
                );
                if (sEl?.innerText?.toLowerCase().includes('sent')) sent = true;
                const tEl = blk.querySelector(
                    "[data-testid='indeed-messaging--ConversationEventSender__timestamp']"
                );
                if (tEl) ts = tEl.innerText?.trim() || '';
            }
            msgs.push({ content: txt, direction: sent ? 'outbound' : 'inbound', timestamp: ts, date_group: '' });
        });
    }
    return msgs;
}
"""


def _pause(lo=0.6, hi=1.4):
    time.sleep(random.uniform(lo, hi))


def snap(page, name: str):
    out = DATA_DIR / "screenshots" / f"conv_{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))
    log.info(f"Screenshot: {out}")


def _thread_id(candidate_name: str, job_info: str) -> str:
    return "thread-" + hashlib.md5(f"{candidate_name}|{job_info}".encode()).hexdigest()[:10]


def _parse_option_text(text: str) -> dict:
    meta = {"job_info": "", "candidate_name": "", "preview": "", "timestamp": ""}
    m = re.match(
        r"Applied to (.+?)\s+(\d+:\d+\s*(?:AM|PM)|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+)\s+(.+?)\s+(.*)",
        text, re.I | re.S,
    )
    if m:
        meta["job_info"]       = m.group(1).strip()
        meta["timestamp"]      = m.group(2).strip()
        meta["candidate_name"] = m.group(3).strip()
        meta["preview"]        = m.group(4).strip()[:150]
    return meta


def _get_header(page) -> dict:
    try:
        h = page.get_by_test_id("indeed-messaging--Conversation__header")
        if h.count():
            lines = [l.strip() for l in h.first.inner_text().strip().splitlines() if l.strip()]
            return {
                "candidate_name": lines[0] if lines else "",
                "job_info":       lines[1] if len(lines) > 1 else "",
            }
    except Exception:
        pass
    return {}


def _extract_messages(page) -> list:
    try:
        page.wait_for_selector(
            "[data-testid='indeed-messaging--ConversationEvents__messageContent']",
            timeout=8_000,
        )
        _pause(0.4, 0.7)
        return page.evaluate(_JS_EXTRACT) or []
    except Exception as e:
        log.warning(f"Message extraction failed: {e}")
        return []


def _dismiss_overlays(page):
    """Dismiss any popups or overlays that block the conversation list."""
    for locator in [
        page.get_by_test_id("messagingPolicyAndTermsModal-closeButton"),
        page.get_by_role("button", name="Got it"),
        page.get_by_role("button", name="Got It"),
        page.get_by_role("button", name="Dismiss"),
        page.get_by_test_id("onboarding-popup-close"),
    ]:
        try:
            if locator.count() > 0:
                locator.first.click()
                log.info("Dismissed overlay")
                _pause(0.6, 1.0)
        except Exception:
            pass


def _open_messages_page(page):
    # Navigate from jobs page then click Messages icon — matches the working codegen flow
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    _pause(2.0, 3.5)

    # Click the Messages nav link
    page.get_by_test_id("messages").click()
    _pause(1.5, 2.5)

    # Dismiss "Messaging policy and terms" modal
    _dismiss_overlays(page)

    # Ensure Inbox tab is active
    try:
        inbox = page.get_by_role("tab", name="Inbox")
        if inbox.count():
            inbox.click()
            _pause(1.0, 1.5)
    except Exception:
        pass

    _dismiss_overlays(page)   # second pass — some popups appear after tab click


def scrape(max_threads: int = 50) -> list:
    if not SESSION_STATE_FILE.exists():
        log.error("No session. Run: python scraper/indeed_login.py")
        return []

    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(SESSION_STATE_FILE),
            user_agent=_UA,
        )
        page = context.new_page()

        log.info("Loading messages page…")
        _open_messages_page(page)

        # Wait for the list container, then wait for at least one option to render
        try:
            page.wait_for_selector("#indeed-messaging--conversation-list-div", timeout=15_000)
        except Exception:
            snap(page, "01_no_list")
            log.error("Conversation list container not found")
            browser.close()
            return []

        try:
            page.wait_for_selector('[role="option"]', timeout=15_000)
        except Exception:
            snap(page, "01_no_options")
            log.error("No conversation options rendered — see screenshot")
            browser.close()
            return []

        _pause(0.5, 1.0)   # let full list paint
        snap(page, "01_list_ready")

        total = page.get_by_role("option").count()
        count = min(total, max_threads)
        log.info(f"Found {total} conversation(s)")

        threads = []
        index   = []

        for i in range(count):
            try:
                _pause(0.4, 0.8)   # let list settle after previous click
                # Re-query each time — the list re-renders after every click
                opt = page.get_by_role("option").nth(i)
                option_text = opt.inner_text(timeout=8_000)
                log.info(f"[{i+1}/{count}] {option_text[:80].strip()}")

                opt.click()
                _pause(1.5, 2.5)
                snap(page, f"{i:02d}_open")

                header     = _get_header(page)
                meta       = _parse_option_text(option_text)
                cname      = header.get("candidate_name") or meta["candidate_name"]
                job_info   = header.get("job_info")       or meta["job_info"]
                tid        = _thread_id(cname, job_info)
                messages   = _extract_messages(page)

                log.info(f"  → {len(messages)} message(s) for {cname}")

                thread = {
                    "thread_id":             tid,
                    "candidate_name":        cname,
                    "job_info":              job_info,
                    "last_updated":          datetime.now(timezone.utc).isoformat(),
                    "last_message_preview":  meta["preview"] or (messages[-1]["content"][:100] if messages else ""),
                    "last_message_timestamp": meta["timestamp"],
                    "message_count":         len(messages),
                    "messages":              messages,
                    "_option_text":          option_text[:200],
                }

                save_json(CONVERSATIONS_DIR / f"{tid}.json", thread)
                summary_keys = (
                    "thread_id", "candidate_name", "job_info",
                    "last_updated", "last_message_preview",
                    "last_message_timestamp", "message_count",
                )
                index.append({k: thread[k] for k in summary_keys})
                threads.append(thread)

            except Exception as e:
                log.warning(f"Error on conversation {i}: {e}")
                continue

        save_json(CONVERSATIONS_DIR / "index.json", index)
        log.info(f"Saved {len(threads)} conversation(s) to {CONVERSATIONS_DIR}")
        browser.close()

    return threads


def send_reply(thread_id: str, message: str) -> dict:
    """Send a reply to an existing conversation thread via Indeed messages page."""
    thread_file = CONVERSATIONS_DIR / f"{thread_id}.json"
    if not thread_file.exists():
        return {"ok": False, "error": f"Thread {thread_id} not found. Run scrape first."}

    thread         = json.loads(thread_file.read_text(encoding="utf-8"))
    candidate_name = thread.get("candidate_name", "")

    if not SESSION_STATE_FILE.exists():
        return {"ok": False, "error": "No session. Run indeed_login.py"}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(SESSION_STATE_FILE),
            user_agent=_UA,
        )
        page = context.new_page()

        _open_messages_page(page)

        try:
            page.wait_for_selector('[role="option"]', timeout=15_000)
        except Exception:
            browser.close()
            return {"ok": False, "error": "Conversation list not found"}

        # Find the right conversation by candidate name
        clicked = False
        options = page.get_by_role("option").all()
        for opt in options:
            try:
                txt = opt.inner_text()
                if candidate_name.lower() in txt.lower():
                    opt.click()
                    clicked = True
                    _pause(1.5, 2.5)
                    break
            except Exception:
                continue

        if not clicked:
            browser.close()
            return {"ok": False, "error": f"Conversation for '{candidate_name}' not found in list"}

        snap(page, f"reply_{thread_id[:8]}_open")

        try:
            ta = page.get_by_test_id("indeed-messaging--compose-message-textarea")
            ta.wait_for(timeout=8_000)
            ta.click()
            ta.fill(message)
            _pause(0.8, 1.2)

            send_btn = page.get_by_test_id("indeed-messaging--ComposeBox__sendButton")
            send_btn.click()
            _pause(1.2, 1.8)

            snap(page, f"reply_{thread_id[:8]}_sent")
            log.info(f"Reply sent to {candidate_name}")

        except Exception as e:
            snap(page, f"reply_{thread_id[:8]}_fail")
            browser.close()
            return {"ok": False, "error": str(e)}

        browser.close()

    # Update local JSON
    now = datetime.now()
    ts  = now.strftime("%I:%M %p").lstrip("0")
    thread["messages"].append({
        "content":    message,
        "direction":  "outbound",
        "timestamp":  ts,
        "date_group": "Today",
    })
    thread["last_updated"]          = datetime.now(timezone.utc).isoformat()
    thread["last_message_preview"]  = message[:100]
    thread["message_count"]         = len(thread["messages"])
    save_json(thread_file, thread)

    # Update index
    index_file = CONVERSATIONS_DIR / "index.json"
    if index_file.exists():
        try:
            idx = json.loads(index_file.read_text(encoding="utf-8"))
            for entry in idx:
                if entry["thread_id"] == thread_id:
                    entry["last_message_preview"]  = message[:100]
                    entry["last_updated"]           = thread["last_updated"]
                    entry["message_count"]          = thread["message_count"]
                    break
            save_json(index_file, idx)
        except Exception:
            pass

    return {"ok": True, "candidate_name": candidate_name, "thread_id": thread_id}


if __name__ == "__main__":
    results = scrape()
    print(f"\nScraped {len(results)} conversation(s)")
