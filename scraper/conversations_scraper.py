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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, DATA_DIR, CONFIG_DIR, get_logger, save_json

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
    const vw = window.innerWidth || 1280;

    // Find the horizontal centre of the conversation thread panel (not the full viewport).
    // Indeed's left panel occupies ~300 px, so all message centres sit right of vw/2 —
    // we must measure against the thread panel's own centre instead.
    function getThreadPanelCenter() {
        const firstMsg = document.querySelector(
            "[data-testid='indeed-messaging--ConversationEvents__messageContent']"
        );
        if (firstMsg) {
            let node = firstMsg;
            for (let i = 0; i < 30; i++) {
                if (!node.parentElement) break;
                node = node.parentElement;
                const s = window.getComputedStyle(node);
                const r = node.getBoundingClientRect();
                // The scrollable thread container: overflows vertically,
                // is at least 30 % of vw wide, but narrower than 95 % of vw.
                if ((s.overflowY === 'scroll' || s.overflowY === 'auto') &&
                    r.width > vw * 0.3 && r.width < vw * 0.95) {
                    return r.left + r.width / 2;
                }
            }
        }
        // Fallback: derive from the conversation-list panel if present
        const listPanel = document.getElementById('indeed-messaging--conversation-list-div');
        if (listPanel) {
            const lr = listPanel.getBoundingClientRect();
            return lr.right + (vw - lr.right) / 2;
        }
        return vw / 2;
    }

    const panelCenterX = getThreadPanelCenter();

    // Right of panel centre = outbound (employer sent), left = inbound (candidate).
    function isOutbound(el) {
        try {
            const rect = el.getBoundingClientRect();
            if (rect.width > 10) {
                return (rect.left + rect.width / 2) > panelCenterX;
            }
        } catch(e) {}
        // CSS fallback for zero-width / off-screen elements
        let node = el;
        for (let i = 0; i < 12; i++) {
            if (!node.parentElement) break;
            const s = window.getComputedStyle(node);
            if (s.alignSelf === 'flex-end')        return true;
            if (s.alignSelf === 'flex-start')      return false;
            if (s.flexDirection === 'row-reverse') return true;
            if (s.justifyContent === 'flex-end' &&
                (s.display === 'flex' || s.display === 'inline-flex')) return true;
            node = node.parentElement;
        }
        return false;
    }

    // Extract timestamp by walking up from the message element
    function getTimestamp(el, stopEl) {
        let blk = el;
        for (let i = 0; i < 8; i++) {
            if (!blk.parentElement || blk === stopEl) break;
            blk = blk.parentElement;
            const tEl = blk.querySelector(
                "[data-testid='indeed-messaging--ConversationEventSender__timestamp']"
            );
            if (tEl) return tEl.innerText?.trim() || '';
        }
        return '';
    }

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
                msgs.push({
                    content:    txt,
                    direction:  isOutbound(el) ? 'outbound' : 'inbound',
                    timestamp:  getTimestamp(el, section),
                    date_group: dg,
                });
            });
        });
    } else {
        document.querySelectorAll(
            "[data-testid='indeed-messaging--ConversationEvents__messageContent']"
        ).forEach(el => {
            const txt = el.innerText?.trim();
            if (!txt) return;
            msgs.push({
                content:    txt,
                direction:  isOutbound(el) ? 'outbound' : 'inbound',
                timestamp:  getTimestamp(el, null),
                date_group: '',
            });
        });
    }
    return msgs;
}
"""


def _sender_name() -> str:
    """Return the recruiter's sender name from config/settings.json."""
    try:
        s = json.loads((CONFIG_DIR / "settings.json").read_text(encoding="utf-8"))
        return s.get("sender_name", "Recruiter") or "Recruiter"
    except Exception:
        return "Recruiter"


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
        r"Applied to (.+?)\s+"
        r"(\d+:\d+\s*(?:AM|PM)"                              # "2:30 PM"
        r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+(?:,\s*\d{4})?)"  # "Jun 3" or "Nov 4, 2025"
        r"\s+(.+?)\s+(.*)",
        text, re.I | re.S,
    )
    if m:
        meta["job_info"]       = m.group(1).strip()
        meta["timestamp"]      = m.group(2).strip()
        meta["candidate_name"] = m.group(3).strip()
        meta["preview"]        = m.group(4).strip()[:150]
    return meta


def _is_recent_timestamp(ts_str: str, since: datetime) -> bool:
    """
    Return True if the option-list timestamp is newer than `since`.
    - "2:30 PM"  → today's time → compare to since
    - "Jun 3"    → a past date  → False (old, stop scanning)
    - unknown    → True (include to be safe)
    """
    if not ts_str:
        return True

    # Time-of-day → message is from today
    m = re.match(r'(\d+):(\d+)\s*(AM|PM)', ts_str.strip(), re.I)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ampm == 'PM' and hour != 12:
            hour += 12
        elif ampm == 'AM' and hour == 12:
            hour = 0
        now    = datetime.now()
        msg_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if msg_dt > now:          # edge case: just past midnight
            msg_dt -= timedelta(days=1)
        return msg_dt >= since

    # Month-day format → not today → definitely old
    if re.match(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+', ts_str.strip(), re.I):
        return False

    return True   # unknown format — include to be safe


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


def scrape(max_threads: int = 50, since: datetime = None) -> list:
    """
    Scrape Indeed conversations.

    since: if provided, only open conversations whose list-timestamp is newer
           than this datetime. Conversations with an older date-stamp are skipped
           immediately (Indeed shows newest first, so we stop at the first old one).
           Pass None (default) for a full scan (manual refresh, first run, etc.).
    """
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

        _pause(0.5, 1.0)
        snap(page, "01_list_ready")

        total = page.get_by_role("option").count()
        log.info(f"Found {total} conversation(s) in list")

        # ── Quick-scan: read option timestamps without clicking ──────────────
        # Indeed shows most-recent conversations first.  We read each option's
        # text, parse the timestamp, and stop as soon as we see one that is
        # older than `since`.  Only options that pass the check get clicked.
        recent_indices = []
        scan_limit = min(total, max_threads)

        if since is None:
            # Full scan (manual refresh / first run) — check everything
            recent_indices = list(range(scan_limit))
            log.info(f"Full scan: will open {scan_limit} conversation(s)")
        else:
            log.info(f"Time-filtered scan since {since.strftime('%H:%M:%S')} — reading timestamps…")
            for i in range(scan_limit):
                try:
                    opt  = page.get_by_role("option").nth(i)
                    txt  = opt.inner_text(timeout=5_000)
                    meta = _parse_option_text(txt)
                    ts   = meta.get("timestamp", "")
                    is_recent = _is_recent_timestamp(ts, since)
                    log.info(f"  [{i+1}] ts={ts!r} → {'RECENT' if is_recent else 'OLD — stopping'}")
                    if is_recent:
                        recent_indices.append(i)
                    else:
                        # Newest-first ordering: everything below is even older
                        break
                except Exception as e:
                    log.warning(f"  [{i+1}] Could not read option text: {e} — including to be safe")
                    recent_indices.append(i)

        if not recent_indices:
            log.info("No recent conversations — nothing to do")
            browser.close()
            return []

        log.info(f"Opening {len(recent_indices)} recent conversation(s): indices {recent_indices}")

        threads = []
        index   = []

        for pos, i in enumerate(recent_indices):
            try:
                # Re-navigate before each click so the list panel is fully visible
                if pos > 0:
                    _open_messages_page(page)
                    try:
                        page.wait_for_selector('[role="option"]', timeout=15_000)
                    except Exception:
                        log.warning(f"[pos {pos}] Conversation list not ready, skipping index {i}")
                        continue
                    _pause(0.5, 1.0)

                _pause(0.8, 1.4)

                opt = page.get_by_role("option").nth(i)
                opt.scroll_into_view_if_needed(timeout=10_000)
                option_text = opt.inner_text(timeout=10_000)
                log.info(f"[{pos+1}/{len(recent_indices)}] {option_text[:80].strip()}")

                opt.click()
                _pause(1.5, 2.5)
                snap(page, f"{i:02d}_open")

                header   = _get_header(page)
                meta     = _parse_option_text(option_text)
                cname    = header.get("candidate_name") or meta["candidate_name"]
                job_info = header.get("job_info")       or meta["job_info"]
                tid      = _thread_id(cname, job_info)
                messages = _extract_messages(page)

                recruiter = _sender_name()
                for msg in messages:
                    msg["sender"] = recruiter if msg.get("direction") == "outbound" else (cname or "Candidate")

                log.info(f"  → {len(messages)} message(s) for {cname}")

                thread = {
                    "thread_id":              tid,
                    "candidate_name":         cname,
                    "job_info":               job_info,
                    "last_updated":           datetime.now(timezone.utc).isoformat(),
                    "last_message_preview":   meta["preview"] or (messages[-1]["content"][:100] if messages else ""),
                    "last_message_timestamp": meta["timestamp"],
                    "message_count":          len(messages),
                    "messages":               messages,
                    "_option_text":           option_text[:200],
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
                log.warning(f"Error on conversation index {i}: {e}")
                continue

        # Merge new results into the existing index so a time-filtered scan
        # (which only opens recent conversations) doesn't erase the other entries.
        index_file = CONVERSATIONS_DIR / "index.json"
        if index_file.exists() and index:
            try:
                existing = json.loads(index_file.read_text(encoding="utf-8"))
                updated_ids = {e["thread_id"] for e in index}
                # Keep old entries that weren't re-scraped, prepend fresh ones
                merged = index + [e for e in existing if e["thread_id"] not in updated_ids]
                save_json(index_file, merged)
            except Exception:
                save_json(index_file, index)
        else:
            save_json(index_file, index)

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

        # Dismiss any overlay that pops up after opening the thread
        _dismiss_overlays(page)

        # Wait for the thread messages to fully render before touching compose box
        try:
            page.wait_for_selector(
                "[data-testid='indeed-messaging--ConversationEvents__messageContent']",
                timeout=10_000,
            )
        except Exception:
            pass   # thread may have no messages yet — compose box still usable

        _pause(1.0, 1.5)
        snap(page, f"reply_{thread_id[:8]}_open")

        try:
            page.wait_for_selector(
                "[data-testid='indeed-messaging--compose-message-textarea']",
                timeout=10_000,
            )

            # Dismiss compose-area overlays that intercept pointer events
            # (css-vvmf5x and css-krfgjm alternate blocking the textarea click)
            for sel in [".css-vvmf5x", ".css-krfgjm"]:
                try:
                    page.evaluate(
                        f"""const el = document.querySelector('{sel}');
                        if (el) el.dispatchEvent(new MouseEvent('click', {{bubbles:true, cancelable:true}}));"""
                    )
                    _pause(0.1, 0.2)
                except Exception:
                    pass

            ta = page.get_by_test_id("indeed-messaging--compose-message-textarea")
            try:
                ta.click(timeout=5_000, force=True)
            except Exception as e:
                log.warning(f"textarea force-click failed, continuing with JS focus: {e}")
            page.evaluate(
                """const ta = document.querySelector('[data-testid="indeed-messaging--compose-message-textarea"]');
                if (ta) ta.focus();"""
            )
            _pause(0.2, 0.3)

            # Use the JS native value setter so React's onChange fires and
            # the send button becomes enabled (ta.fill() bypasses React state)
            page.evaluate(
                """(msg) => {
                    const ta = document.querySelector(
                        '[data-testid="indeed-messaging--compose-message-textarea"]'
                    );
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(ta, msg);
                    ta.dispatchEvent(new Event('input',  {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                message,
            )
            _pause(0.5, 0.8)

            send_btn = page.get_by_test_id("indeed-messaging--ComposeBox__sendButton")
            if send_btn.count() > 0:
                send_btn.click()
                _pause(1.2, 1.8)
            else:
                raise RuntimeError("Send button not found after filling message")

            snap(page, f"reply_{thread_id[:8]}_sent")
            log.info(f"Reply sent to {candidate_name}")

        except Exception as e:
            snap(page, f"reply_{thread_id[:8]}_fail")
            log.error(f"send_reply failed for {candidate_name}: {e}")
            browser.close()
            return {"ok": False, "error": str(e)}

        browser.close()

    # Update local JSON
    now = datetime.now()
    ts  = now.strftime("%I:%M %p").lstrip("0")
    thread["messages"].append({
        "content":    message,
        "direction":  "outbound",
        "sender":     _sender_name(),
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
                    entry["auto_replied_at"]        = thread["last_updated"]
                    break
            save_json(index_file, idx)
        except Exception:
            pass

    return {"ok": True, "candidate_name": candidate_name, "thread_id": thread_id,
            "auto_replied_at": thread["last_updated"]}


if __name__ == "__main__":
    results = scrape()
    print(f"\nScraped {len(results)} conversation(s)")
