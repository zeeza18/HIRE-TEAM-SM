"""
Enrich candidate JSONs — profile text fields only.

Fills: professional_summary, experience, certifications, skills
Does NOT touch fit_score or any scoring field (score.py handles those).

Zero-hallucination rule: LLM must only use text explicitly in the resume.
Temperature = 0 to maximise determinism.

Usage:
    python scraper/enrich.py
    python scraper/enrich.py --rerun
"""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.utils import CANDIDATES_DIR, get_logger, load_json, save_json, load_credentials

log = get_logger("enrich")

MODEL = "gpt-4o-mini"

# Fields this module owns — score.py must never touch these
ENRICH_FIELDS = {"professional_summary", "experience", "certifications", "skills"}

_PROMPT = """Extract professional profile fields from the resume text below.

RULES — read carefully:
1. ONLY include information explicitly written in the resume text
2. Do NOT add, infer, reword, or improve anything not present word-for-word
3. If a section has no content, return an empty string ""
4. Return ONLY valid JSON — no markdown, no explanation

RESUME TEXT:
{resume_text}

Return this exact JSON structure:
{{
  "professional_summary": "1-3 sentence factual summary using only phrases/facts from the resume",
  "experience": "work history summary, max 500 chars, verbatim job titles and employers from resume",
  "certifications": "certifications and licenses exactly as written in resume, comma-separated, or empty string",
  "skills": "skills exactly as listed in the resume, comma-separated, or empty string"
}}"""


def _call(client, prompt: str) -> dict:
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=800,
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            raise
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = (attempt + 1) * 6
                log.warning(f"  rate limit -- retrying in {wait}s")
                time.sleep(wait)
            else:
                raise


def _needs_enrich(data: dict) -> bool:
    return not data.get("professional_summary")


def process_one(path: Path, client, lock: threading.Lock = None) -> bool:
    """Enrich one candidate file. Thread-safe when lock is provided."""
    if lock:
        lock.acquire()
    try:
        data = load_json(path)
    finally:
        if lock:
            lock.release()

    if not data:
        return False

    if not _needs_enrich(data):
        log.debug(f"  enrich skip (already done): {data.get('full_name')}")
        return True

    resume_text = (data.get("resume_text") or "")[:6000]
    if not resume_text:
        log.warning(f"  enrich skip (no resume_text): {data.get('full_name')}")
        return False

    prompt = _PROMPT.format(resume_text=resume_text[:4000])

    try:
        result = _call(client, prompt)
    except json.JSONDecodeError as e:
        log.warning(f"  enrich JSON error [{data.get('full_name')}]: {e}")
        return False
    except Exception as e:
        log.warning(f"  enrich Groq error [{data.get('full_name')}]: {e}")
        return False

    if lock:
        lock.acquire()
    try:
        # Re-read to get freshest data before writing
        fresh = load_json(path) or data
        for field in ENRICH_FIELDS:
            if field in result and not fresh.get(field):
                fresh[field] = result[field]
        save_json(path, fresh)
    finally:
        if lock:
            lock.release()

    return True


def main():
    parser = argparse.ArgumentParser(description="Enrich candidate profile text fields")
    parser.add_argument("--rerun", action="store_true",
                        help="Re-process candidates that already have a professional_summary")
    args = parser.parse_args()

    creds = load_credentials()
    api_key = creds.get("openai_api_key")
    if not api_key:
        log.error("openai_api_key missing from config/credentials.json")
        sys.exit(1)

    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    all_paths = sorted(CANDIDATES_DIR.glob("*.json"))

    if not all_paths:
        log.info("No candidate files found.")
        return

    if args.rerun:
        to_process = all_paths
    else:
        to_process = [p for p in all_paths
                      if not load_json(p, {}).get("professional_summary")]

    log.info(f"Enrich: {len(to_process)} / {len(all_paths)} candidates to process")
    if not to_process:
        log.info("Nothing to do. Use --rerun to re-process.")
        return

    ok = fail = 0
    for i, path in enumerate(to_process, 1):
        name = load_json(path, {}).get("full_name", path.stem)
        log.info(f"[{i}/{len(to_process)}] {name}")
        if process_one(path, client):
            ok += 1
        else:
            fail += 1
        if i < len(to_process):
            time.sleep(0.3)

    log.info(f"\nEnrich done. {ok} enriched, {fail} failed.")


if __name__ == "__main__":
    main()
