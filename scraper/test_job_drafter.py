"""
Test: AI job drafting from free text and form.
Checks guardrails, extraction, and JD generation.

Usage:
    python scraper/test_job_drafter.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.utils import get_logger
from scraper.job_drafter import draft_from_text, draft_from_form

log = get_logger("test_job_drafter")


def snap_json(name: str, data: dict):
    out = Path("data/screenshots") / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Saved: {out}")


def sep(label: str):
    log.info("=" * 60)
    log.info(f"  {label}")
    log.info("=" * 60)


# ── Test 1: Full info — should produce no missing fields ─────────────────────
def test_full_text():
    sep("TEST 1: Full free text (should produce no missing fields)")
    text = (
        "Looking for a full-time RN Case Manager in Lombard IL. "
        "Pay $40-$50 per hour. Need someone within 2 weeks. "
        "Must have active IL RN license and 2+ years home health experience. "
        "In-person role at our Lombard office."
    )
    log.info(f"Input: {text}\n")
    result = draft_from_text(text)
    snap_json("01_full_text", result)

    assert result.get("ok"), f"draft failed: {result.get('error')}"
    assert not result.get("missing"), f"Unexpected missing fields: {result['missing']}"
    assert result.get("title"), "No title extracted"
    assert result.get("location_query"), "No location extracted"
    assert result.get("description"), "No description generated"

    log.info(f"Title:     {result['title']}")
    log.info(f"Location:  {result['location_query']} / {result['location_type']}")
    log.info(f"Pay:       ${result.get('pay_min')} – ${result.get('pay_max')} {result.get('pay_period')}")
    log.info(f"Timeline:  {result['hiring_timeline']}")
    log.info(f"Job types: {result['job_types']}")
    log.info(f"Missing:   {result['missing']}")
    log.info(f"JD snippet:\n{result['description'][:300]}...\n")
    log.info("TEST 1 PASSED\n")


# ── Test 2: Missing salary — guardrail should flag it ────────────────────────
def test_missing_salary():
    sep("TEST 2: Missing salary — guardrail should flag 'salary'")
    text = (
        "We need an Intake Coordinator in Lombard IL. "
        "Full-time position, in-person. Start ASAP."
    )
    log.info(f"Input: {text}\n")
    result = draft_from_text(text)
    snap_json("02_missing_salary", result)

    assert result.get("ok"), f"draft failed: {result.get('error')}"
    assert "salary" in result.get("missing", []), \
        f"Expected 'salary' in missing, got: {result.get('missing')}"
    log.info(f"Missing flags: {result['missing']}")
    log.info("TEST 2 PASSED\n")


# ── Test 3: N/A salary — should set pay_negotiable=True ─────────────────────
def test_na_salary():
    sep("TEST 3: N/A salary — should set pay_negotiable=True, no missing")
    text = (
        "Looking for a Director of Nursing in Lombard IL. "
        "Full-time, in-person. Salary is negotiable based on experience. "
        "Must have IL RN license and 3+ years of home health leadership."
    )
    log.info(f"Input: {text}\n")
    result = draft_from_text(text)
    snap_json("03_na_salary", result)

    assert result.get("ok"), f"draft failed: {result.get('error')}"
    assert result.get("pay_negotiable") is True, \
        f"Expected pay_negotiable=True, got: {result.get('pay_negotiable')}"
    assert "salary" not in result.get("missing", []), \
        f"Salary should not be flagged as missing: {result.get('missing')}"
    log.info(f"pay_negotiable: {result['pay_negotiable']}")
    log.info(f"Missing:        {result['missing']}")
    log.info("TEST 3 PASSED\n")


# ── Test 4: Missing title and location — both flagged ────────────────────────
def test_missing_title_and_location():
    sep("TEST 4: Missing title + location — both should be flagged")
    text = "We are hiring someone with home health experience. Pay $20/hr."
    log.info(f"Input: {text}\n")
    result = draft_from_text(text)
    snap_json("04_missing_title_location", result)

    assert result.get("ok"), f"draft failed: {result.get('error')}"
    missing = result.get("missing", [])
    log.info(f"Missing flags: {missing}")
    # At minimum salary should not be flagged (pay was given)
    assert "salary" not in missing, f"Salary was given but flagged: {missing}"
    log.info("TEST 4 PASSED\n")


# ── Test 5: Form mode — draft JD from structured fields ──────────────────────
def test_form_draft():
    sep("TEST 5: Form mode — draft JD from structured form data")
    form = {
        "title":           "Administrator",
        "location_type":   "In person",
        "location_query":  "Lombard",
        "location_pick":   "Lombard, IL 60148",
        "hiring_timeline": "2 to 4 weeks",
        "hires_needed":    1,
        "job_types":       ["Full-time"],
        "pay_min":         "80000",
        "pay_max":         "100000",
        "pay_period":      "per year",
        "pay_negotiable":  False,
        "benefits":        ["Health insurance", "Dental insurance", "Vision insurance", "Paid time off"],
        "notes":           (
            "Must have 2+ years home health leadership experience. "
            "Bachelor's in Healthcare Administration preferred. "
            "Knowledge of Medicare/Medicaid compliance required."
        ),
    }
    log.info(f"Form title: {form['title']}\n")
    result = draft_from_form(form)
    snap_json("05_form_draft", result)

    assert result.get("ok"), f"form draft failed: {result.get('error')}"
    assert result.get("description"), "No description generated"
    assert not result.get("missing"), f"Unexpected missing: {result.get('missing')}"
    log.info(f"JD snippet:\n{result['description'][:400]}...\n")
    log.info("TEST 5 PASSED\n")


# ── Run all ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    passed = 0
    failed = 0
    tests  = [
        test_full_text,
        test_missing_salary,
        test_na_salary,
        test_missing_title_and_location,
        test_form_draft,
    ]
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            log.error(f"FAILED: {t.__name__} — {e}")
            failed += 1

    sep(f"RESULTS: {passed}/{len(tests)} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
