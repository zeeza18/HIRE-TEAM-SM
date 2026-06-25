"""
Auto-generate 50-point scoring criteria from a Job Description.

One rubric per role title, stored in config/generated_criteria.json.
score.py checks this file first before falling back to hardcoded criteria.

Usage:
    python scraper/criteria_generator.py            # generate for all roles in job_descriptions.json
    python scraper/criteria_generator.py --rerun    # regenerate even if already exists
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, ValidationError, model_validator

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("criteria_generator")


# ── Pydantic models ───────────────────────────────────────────────────────────

class Criterion(BaseModel):
    key: str           # snake_case identifier, e.g. "rn_license"
    label: str         # human label shown in UI, e.g. "RN License"
    max: int           # points this criterion is worth (all must sum to 50)
    key_things: List[str]   # bullet list of what makes a strong match
    instruction: str   # full scoring instruction for GPT: Award X if ... Award 0 if ...


class RoleCriteria(BaseModel):
    role: str                  # normalized lowercase role title
    criteria: List[Criterion]

    @model_validator(mode="after")
    def validate_total(self):
        total = sum(c.max for c in self.criteria)
        if total != 50:
            raise ValueError(
                f"Criteria must sum to exactly 50 points, got {total}. "
                f"Adjust the max values so they total 50."
            )
        if not (4 <= len(self.criteria) <= 8):
            raise ValueError(f"Expected 4-8 criteria, got {len(self.criteria)}")
        return self


# ── Storage helpers ───────────────────────────────────────────────────────────

def _criteria_file(config_dir: Path = None) -> Path:
    from scraper.utils import CONFIG_DIR
    return (config_dir or CONFIG_DIR) / "generated_criteria.json"


def load_all(config_dir: Path = None) -> dict:
    f = _criteria_file(config_dir)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_all(data: dict, config_dir: Path = None):
    f = _criteria_file(config_dir)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_for_role(role: str, config_dir: Path = None) -> Optional[List[dict]]:
    """Return the criteria list for a role if generated, else None."""
    entry = load_all(config_dir).get(role.lower().strip())
    return entry.get("criteria") if entry else None


def save_for_role(role: str, role_criteria: RoleCriteria, config_dir: Path = None):
    all_c = load_all(config_dir)
    all_c[role.lower().strip()] = role_criteria.model_dump()
    _save_all(all_c, config_dir)


# ── GPT-4o generation ─────────────────────────────────────────────────────────

_SYSTEM = """You are a senior recruiter and HR analyst specializing in healthcare staffing.

Given a job description you will generate a scoring rubric with 5-7 criteria that sum to EXACTLY 50 points.

Rules:
- criteria MUST sum to exactly 50 — check your math before outputting
- weight more important requirements higher (e.g. licensure > location)
- each criterion must be concretely measurable from a resume or profile text
- key_things: 2-4 short bullet strings describing what makes a top candidate for that criterion
- instruction: explicit award tiers (Award X if ..., Award Y if ..., Award 0 if not mentioned) ending with "Quote the exact text."
- key: snake_case, no spaces
- output only valid JSON, no markdown fences
"""

_PROMPT_TEMPLATE = """Job Title: {job_title}

Job Description:
{jd_text}

Generate a scoring rubric. Output this exact JSON structure:
{{
  "role": "<job title lowercase, normalized>",
  "criteria": [
    {{
      "key": "snake_case_key",
      "label": "Human Readable Label",
      "max": <integer points>,
      "key_things": ["thing 1 to look for", "thing 2", "thing 3"],
      "instruction": "Award <max> if ... Award <partial> if ... Award 0 if not mentioned. Quote the exact text."
    }}
  ]
}}

The criteria array must have 5-7 items whose max values sum to EXACTLY 50."""


def _generate_once(job_title: str, jd_text: str, client) -> RoleCriteria:
    prompt = _PROMPT_TEMPLATE.format(
        job_title=job_title,
        jd_text=jd_text[:6000],
    )
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": prompt},
        ],
        temperature=0,
        max_tokens=2500,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content.strip()
    parsed = json.loads(raw)
    return RoleCriteria(**parsed)


def generate(job_title: str, jd_text: str, client, retries: int = 3) -> RoleCriteria:
    """Generate and validate criteria for one role. Retries up to `retries` times."""
    last_err = None
    for attempt in range(retries):
        try:
            result = _generate_once(job_title, jd_text, client)
            log.info(
                f"  [{job_title}] generated {len(result.criteria)} criteria, "
                f"total={sum(c.max for c in result.criteria)} pts"
            )
            return result
        except (json.JSONDecodeError, ValidationError, Exception) as e:
            last_err = e
            log.warning(f"  [{job_title}] attempt {attempt+1}/{retries} failed: {e}")
    raise RuntimeError(f"criteria generation failed for '{job_title}': {last_err}")


# ── Bulk generation from JD file ──────────────────────────────────────────────

def generate_all(client, config_dir: Path = None, rerun: bool = False) -> dict:
    """
    Generate criteria for every unique role in job_descriptions.json.
    Skips roles already generated unless rerun=True.
    Returns {role_key: RoleCriteria} for every role processed.
    """
    from scraper.utils import CONFIG_DIR
    from scraper.score import _clean_jd

    jd_path = (config_dir or CONFIG_DIR) / "job_descriptions.json"
    if not jd_path.exists():
        log.error(f"JD file not found: {jd_path}")
        return {}

    jds      = json.loads(jd_path.read_text(encoding="utf-8"))
    existing = load_all(config_dir)
    seen     = set()
    results  = {}

    for job_id, entry in jds.items():
        job_title = (entry.get("title") or entry.get("job_title") or "").strip()
        role_key  = job_title.lower()
        if not job_title or role_key in seen:
            continue
        seen.add(role_key)

        if role_key in existing and not rerun:
            log.info(f"  [{job_title}] already generated — skipping (use rerun=True to overwrite)")
            results[role_key] = existing[role_key]
            continue

        jd_text = _clean_jd(entry.get("job_description") or "")
        if not jd_text:
            log.warning(f"  [{job_title}] no JD text — skipping")
            continue

        try:
            rc = generate(job_title, jd_text, client)
            save_for_role(role_key, rc, config_dir)
            results[role_key] = rc.model_dump()
        except Exception as e:
            log.error(f"  [{job_title}] failed: {e}")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Generate 50-pt scoring criteria from JDs")
    parser.add_argument("--rerun", action="store_true", help="Regenerate even if already exists")
    parser.add_argument("--slug", default="rms", choices=["rms", "sm"], help="Company slug")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")

    from agents.company import COMPANIES
    from agents.context import company_scope

    co      = COMPANIES.get(args.slug)
    creds   = co.load_credentials()
    api_key = creds.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("ERROR: no openai_api_key in credentials or .env"); sys.exit(1)

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    with company_scope(co):
        results = generate_all(client, config_dir=co.config_dir, rerun=args.rerun)

    print(f"\nDone. Generated criteria for {len(results)} role(s):")
    for role, data in results.items():
        crits = data.get("criteria", [])
        total = sum(c["max"] for c in crits)
        print(f"  {role}: {len(crits)} criteria, {total} pts")
        for c in crits:
            print(f"    [{c['max']:>2}] {c['label']}")


if __name__ == "__main__":
    main()
