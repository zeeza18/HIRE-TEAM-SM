"""
Score candidate fit: 50% requirements match + 50% JD evidence match.

Anti-hallucination design:
  - LLM must quote exact resume text as evidence for every point awarded
  - Python enforces: if evidence is null → score = 0, regardless of LLM output
  - Temperature = 0 for maximum determinism
  - fit_score is deterministic Python math from grounded LLM output

Scoring:
  requirements_score (0-50)  = (met / total) * 50   ← pure Python from scraped data
  jd_score          (0-50)   = per-criterion evidence scoring against JD
  fit_score         (0-100)  = requirements_score + jd_score

Usage:
    python scraper/score.py
    python scraper/score.py --rerun
"""

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, ValidationError, model_validator

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.utils import (
    CANDIDATES_DIR, CONFIG_DIR, get_logger, load_json, save_json, load_credentials,
)

log = get_logger("score")

MODEL = "gpt-4o"
JD_FILE = CONFIG_DIR / "job_descriptions.json"

# ── Pydantic models for LLM response validation ───────────────────────────────

class CriterionScore(BaseModel):
    score: int = 0
    max: int = 0
    evidence: Optional[str] = None
    reasoning: str = ""

    @model_validator(mode="after")
    def enforce_evidence_rule(self):
        if not self.evidence or str(self.evidence).strip().lower() in ("null", "none", "fill_quote_or_null", ""):
            self.evidence = None
            self.score = 0
        return self


class ScoreResponse(BaseModel):
    criteria: Dict[str, CriterionScore] = {}
    credential: Optional[str] = None
    license_state: Optional[str] = None
    license_status: Optional[str] = None
    work_auth: Optional[str] = None
    availability: Optional[str] = None
    population: List[str] = []
    setting_pref: List[str] = []
    start_date: Optional[str] = None
    pay_expectation: Optional[str] = None
    flags: List[str] = []


# Fields this module owns — enrich.py must never touch these
SCORE_FIELDS = {
    "fit_score", "score_breakdown", "flags", "pay_band_verdict",
    "credential", "license_state", "license_status",
    "work_auth", "availability", "population", "setting_pref",
    "start_date", "pay_expectation",
}


# ── per-job scoring criteria ───────────────────────────────────────────────────

_CRITERIA = {
    "administrator": [
        {
            "key": "education",
            "max": 10,
            "instruction": (
                "Award 10 if resume shows Master's degree in healthcare, nursing, business, "
                "or related field. Award 7 if Bachelor's in those fields — this includes degrees "
                "titled Healthcare Administration, Healthcare Management, Health Care Compliance, "
                "Nursing, Business, or any healthcare/clinical/regulatory subject. "
                "Award 4 if Bachelor's in a clearly unrelated field (e.g. art, history, engineering). "
                "Award 0 if no degree mentioned. Quote the exact degree line."
            ),
        },
        {
            "key": "home_health_experience",
            "max": 15,
            "instruction": (
                "Award 15 if candidate held an Administrator or Director title at a home health agency. "
                "Award 10 if they were a Manager or Coordinator at a home health agency. "
                "Award 5 if they worked in home health in any role. "
                "Award 3 if they worked in a related setting (hospice, skilled nursing, home care — NOT hospital or clinic). "
                "Award 0 if no home health or home care experience. "
                "Quote the exact job title and employer."
            ),
        },
        {
            "key": "leadership_experience",
            "max": 10,
            "instruction": (
                "Award 10 if resume shows 5+ years managing teams of 10+ people. "
                "Award 7 if 2-4 years of team leadership with direct reports. "
                "Award 4 if less than 2 years leadership or team size unclear. "
                "Award 0 if no leadership role found. "
                "Quote the title and duration."
            ),
        },
        {
            "key": "compliance_regulatory",
            "max": 10,
            "instruction": (
                "Award 10 if resume explicitly mentions Medicare, Medicaid, AND home health/agency regulations. "
                "Award 6 if mentions Medicare or Medicaid but not home health compliance specifically. "
                "Award 3 if mentions compliance or regulations generally — including a degree titled "
                "'Health Care Compliance', 'Healthcare Compliance', or 'Compliance and Regulation'. "
                "Award 0 if not mentioned. Quote the exact text."
            ),
        },
        {
            "key": "financial_budget",
            "max": 8,
            "instruction": (
                "Award 8 if resume shows explicit budget ownership or financial management responsibility. "
                "Award 4 if financial tasks are mentioned but scope is unclear. "
                "Award 0 if not mentioned. Quote the exact line."
            ),
        },
        {
            "key": "emr_systems",
            "max": 4,
            "instruction": (
                "Award 4 if resume names specific EMR/clinical software (e.g. Epic, Cerner, Athena, WellSky). "
                "Award 2 if EMR or EHR is mentioned without naming the system. "
                "Award 0 if not mentioned. Quote the exact text."
            ),
        },
        {
            "key": "il_location",
            "max": 3,
            "instruction": (
                "Award 3 if resume or profile shows candidate is currently in Illinois — "
                "any Illinois city, town, or IL zip code counts (e.g. 'Chicago, IL', 'West Dundee, IL 60118'). "
                "Award 2 if they explicitly state willingness to relocate to Chicago or Illinois. "
                "Award 0 if in another state with no relocation mention. "
                "Quote the location text."
            ),
        },
    ],

    "intake coordinator": [
        {
            "key": "education",
            "max": 8,
            "instruction": (
                "Award 8 if Bachelor's or Associate's degree in healthcare or related. "
                "Award 5 if HS diploma plus clear healthcare administrative experience. "
                "Award 3 if HS diploma only. Quote the exact degree."
            ),
        },
        {
            "key": "intake_admissions_experience",
            "max": 15,
            "instruction": (
                "Award 15 if 2+ years of intake, admissions, or referral coordination in home health or hospice. "
                "Award 10 if 2+ years intake/admissions in any healthcare setting. "
                "Award 5 if less than 2 years intake experience. "
                "Award 0 if no intake or admissions experience. Quote the exact role and employer."
            ),
        },
        {
            "key": "insurance_verification",
            "max": 12,
            "instruction": (
                "Award 12 if resume explicitly mentions insurance verification, Medicare/Medicaid eligibility checks. "
                "Award 6 if mentions insurance or billing generally without verification specifics. "
                "Award 0 if not mentioned. Quote exactly."
            ),
        },
        {
            "key": "emr_proficiency",
            "max": 10,
            "instruction": (
                "Award 10 if resume names specific EMR systems used (Epic, Cerner, WellSky, etc.). "
                "Award 5 if EMR or EHR proficiency is mentioned without system names. "
                "Award 0 if not mentioned. Quote exactly."
            ),
        },
        {
            "key": "communication_organization",
            "max": 2,
            "instruction": (
                "Award 2 if resume demonstrates strong communication and organizational skills through "
                "concrete experience (managing referrals, coordinating with teams, etc.). "
                "Award 0 if not evident. Quote the example."
            ),
        },
        {
            "key": "il_location",
            "max": 3,
            "instruction": (
                "Award 3 if candidate is currently in Illinois — any IL city or zip code counts. "
                "Award 2 if willing to relocate to IL. "
                "Award 0 otherwise. Quote the location text."
            ),
        },
    ],

    "director of nursing": [
        {
            "key": "rn_license",
            "max": 20,
            "instruction": (
                "Award 20 if resume explicitly states Active RN license in Illinois. "
                "Award 14 if RN license is mentioned but state is unclear or inactive. "
                "Award 8 if LPN license. "
                "Award 0 if no nursing license mentioned. Quote the exact licensure line."
            ),
        },
        {
            "key": "education",
            "max": 10,
            "instruction": (
                "Award 10 if Master's degree in nursing or healthcare. "
                "Award 8 if BSN. Award 5 if ADN. Award 2 if other degree. "
                "Award 0 if no degree mentioned. Quote the exact degree line."
            ),
        },
        {
            "key": "clinical_leadership",
            "max": 10,
            "instruction": (
                "Award 10 if DON, Clinical Manager, or equivalent title at a home health agency. "
                "Award 7 if clinical leadership role in a different healthcare setting. "
                "Award 3 if staff RN with charge/lead responsibilities. "
                "Award 0 if no clinical leadership. Quote the title and employer."
            ),
        },
        {
            "key": "compliance_regulatory",
            "max": 7,
            "instruction": (
                "Award 7 if resume mentions IDPH, Medicare Conditions of Participation, or home health "
                "compliance/survey readiness explicitly. "
                "Award 3 if general compliance or regulatory mentions. "
                "Award 0 if not mentioned. Quote exactly."
            ),
        },
        {
            "key": "quality_improvement",
            "max": 3,
            "instruction": (
                "Award 3 if resume mentions QI initiatives, accreditation (Joint Commission/ACHC), or "
                "clinical audits. Award 0 if not mentioned. Quote exactly."
            ),
        },
    ],

    "home visiting nurse": [
        {
            "key": "rn_license",
            "max": 15,
            "instruction": (
                "Award 15 if Active RN license mentioned. Award 8 if LPN/LVN. "
                "Award 4 if other clinical license. Award 0 if no clinical license. "
                "Quote the exact licensure text."
            ),
        },
        {
            "key": "home_health_clinical",
            "max": 15,
            "instruction": (
                "Award 15 if direct home health or visiting nurse experience. "
                "Award 10 if community health or visiting nursing experience. "
                "Award 5 if acute care nursing (hospital). "
                "Award 0 if no clinical nursing experience. Quote the exact role."
            ),
        },
        {
            "key": "clinical_assessment",
            "max": 10,
            "instruction": (
                "Award 10 if resume explicitly shows assessment, nursing diagnosis, or care planning skills. "
                "Award 5 if general clinical skills mentioned. Award 0 if not present. Quote exactly."
            ),
        },
        {
            "key": "patient_teaching",
            "max": 5,
            "instruction": (
                "Award 5 if patient education, health teaching, or self-care instruction explicitly mentioned. "
                "Award 0 if not mentioned. Quote exactly."
            ),
        },
        {
            "key": "il_location",
            "max": 5,
            "instruction": (
                "Award 5 if candidate is currently in Chicago or Illinois — any IL city or zip code counts. "
                "Award 3 if willing to travel or relocate to IL. "
                "Award 0 otherwise. Quote the location."
            ),
        },
    ],

    "occupational therapist": [
        {
            "key": "ot_license",
            "max": 20,
            "instruction": (
                "Award 20 if resume explicitly states OTR/L (Occupational Therapist Registered/Licensed) "
                "or active OT license in Illinois. "
                "Award 14 if OT license is mentioned but state is unclear or inactive. "
                "Award 8 if COTA (Certified OT Assistant) license. "
                "Award 0 if no OT licensure mentioned. Quote the exact licensure line."
            ),
        },
        {
            "key": "education",
            "max": 10,
            "instruction": (
                "Award 10 if Master's degree in Occupational Therapy (MOT, MSOT, OTD). "
                "Award 7 if Bachelor's in OT. Award 4 if Associate's or OTA program. "
                "Award 0 if no OT degree mentioned. Quote the exact degree line."
            ),
        },
        {
            "key": "ot_experience",
            "max": 15,
            "instruction": (
                "Award 15 if 2+ years OT experience in home health, pediatrics, or school settings. "
                "Award 10 if 2+ years OT experience in any clinical setting. "
                "Award 5 if less than 2 years OT experience. "
                "Award 0 if no OT clinical experience. Quote the exact role and employer."
            ),
        },
        {
            "key": "pediatrics_school",
            "max": 10,
            "instruction": (
                "Award 10 if resume shows direct experience with pediatric patients or school-based OT. "
                "Award 5 if experience with adults only in rehabilitation or acute care. "
                "Award 0 if no clinical caseload mentioned. Quote the exact setting or population."
            ),
        },
        {
            "key": "evaluation_treatment",
            "max": 8,
            "instruction": (
                "Award 8 if resume explicitly mentions conducting OT evaluations, writing treatment plans, "
                "or goal-setting for functional improvement. "
                "Award 4 if general therapy skills mentioned without specifics. "
                "Award 0 if not mentioned. Quote exactly."
            ),
        },
        {
            "key": "il_location",
            "max": 5,
            "instruction": (
                "Award 5 if candidate is currently in Illinois — any IL city or zip code counts. "
                "Award 3 if willing to relocate to IL. "
                "Award 0 otherwise. Quote the location text."
            ),
        },
    ],

    "speech language pathologist": [
        {
            "key": "slp_license",
            "max": 20,
            "instruction": (
                "Award 20 if resume states CCC-SLP (ASHA Certificate of Clinical Competence) "
                "or licensed SLP in Illinois. "
                "Award 14 if SLP license mentioned but state or CCC status unclear. "
                "Award 8 if CF-SLP (Clinical Fellowship) or CFY status. "
                "Award 0 if no SLP licensure or certification mentioned. Quote exactly."
            ),
        },
        {
            "key": "education",
            "max": 10,
            "instruction": (
                "Award 10 if Master's degree in Speech-Language Pathology (MS-SLP, MA-SLP, or equivalent). "
                "Award 6 if Bachelor's in Communication Sciences or related. "
                "Award 0 if no relevant degree. Quote the exact degree line."
            ),
        },
        {
            "key": "slp_experience",
            "max": 15,
            "instruction": (
                "Award 15 if 2+ years SLP experience in home health, medical, or school setting. "
                "Award 10 if 2+ years SLP experience in any clinical setting. "
                "Award 5 if CFY or less than 2 years SLP experience. "
                "Award 0 if no SLP clinical experience. Quote the role and setting."
            ),
        },
        {
            "key": "specialty_skills",
            "max": 10,
            "instruction": (
                "Award 10 if resume shows experience with dysphagia, AAC, aphasia, or pediatric speech disorders. "
                "Award 5 if general SLP clinical skills mentioned without specialization. "
                "Award 0 if no clinical skills mentioned. Quote exactly."
            ),
        },
        {
            "key": "il_location",
            "max": 5,
            "instruction": (
                "Award 5 if candidate is currently in Illinois — any IL city or zip code counts. "
                "Award 3 if willing to relocate to IL. "
                "Award 0 otherwise. Quote the location text."
            ),
        },
    ],

    "physician": [
        {
            "key": "md_do_license",
            "max": 20,
            "instruction": (
                "Award 20 if resume states active MD or DO license in Illinois. "
                "Award 14 if MD/DO license mentioned but state unclear. "
                "Award 8 if medical resident or fellow. "
                "Award 0 if no medical license mentioned. Quote exactly."
            ),
        },
        {
            "key": "education",
            "max": 10,
            "instruction": (
                "Award 10 if MD or DO degree from accredited medical school. "
                "Award 5 if currently in residency or fellowship. "
                "Award 0 if no medical degree. Quote the exact degree."
            ),
        },
        {
            "key": "clinical_experience",
            "max": 15,
            "instruction": (
                "Award 15 if 2+ years post-residency clinical experience. "
                "Award 10 if residency completed with relevant specialty. "
                "Award 5 if currently in residency. "
                "Award 0 if no clinical experience. Quote the role and duration."
            ),
        },
        {
            "key": "specialty_fit",
            "max": 10,
            "instruction": (
                "Award 10 if specialty is ENT, Otolaryngology, Pulmonology, Neurology, Physiatry, "
                "or any specialty relevant to swallowing/speech disorders. "
                "Award 5 if internal medicine or general practice. "
                "Award 0 if unrelated specialty. Quote the specialty."
            ),
        },
        {
            "key": "il_location",
            "max": 5,
            "instruction": (
                "Award 5 if candidate is currently in Illinois. "
                "Award 3 if willing to relocate to IL. "
                "Award 0 otherwise. Quote the location."
            ),
        },
    ],
}

# Fallback for any job title not in the above dict
_CRITERIA["default"] = _CRITERIA["administrator"]


def _match_criteria(job_title: str) -> list:
    title_lower = job_title.lower()
    title_norm  = title_lower.replace("-", " ")
    for key in _CRITERIA:
        if key in title_norm:
            return _CRITERIA[key]
    return _CRITERIA["default"]


def _load_criteria(job_title: str) -> list:
    """Check generated_criteria.json first; fall back to hardcoded."""
    try:
        crit_file = JD_FILE.parent / "generated_criteria.json"
        if crit_file.exists():
            all_c     = json.loads(crit_file.read_text(encoding="utf-8"))
            role_key  = job_title.lower().strip()
            # exact match first, then substring
            entry = all_c.get(role_key)
            if not entry:
                for key in all_c:
                    if key in role_key or role_key in key:
                        entry = all_c[key]
                        break
            if entry:
                crits = entry.get("criteria", [])
                if crits:
                    log.info(f"  [criteria] using generated rubric for '{job_title}' ({len(crits)} criteria)")
                    return crits
    except Exception as e:
        log.warning(f"  [criteria] could not load generated criteria: {e}")
    return _match_criteria(job_title)


def _clean_jd(raw_jd: str) -> str:
    """Strip Indeed UI boilerplate, keep only the actual job description."""
    for marker in ["Job description:", "Job Description:"]:
        idx = raw_jd.find(marker)
        if idx != -1:
            raw_jd = raw_jd[idx + len(marker):]
            break
    for cutoff in [
        "All analytics data provided",
        "Indeed reserves the right",
        "This information does not constitute",
    ]:
        ci = raw_jd.find(cutoff)
        if ci != -1:
            raw_jd = raw_jd[:ci]
    return raw_jd.strip()


def _build_prompt(job_title: str, jd_clean: str, resume_text: str,
                  criteria: list) -> str:
    scoring_rules = "\n".join(
        f"- {c['key']} (max {c['max']}): {c['instruction']}"
        for c in criteria
    )
    entries = []
    for c in criteria:
        entries.append(
            f'    "{c["key"]}": {{"score": FILL_INT, "max": {c["max"]}, "evidence": FILL_QUOTE_OR_null, "reasoning": "FILL_WHY"}}'
        )
    criteria_block = ",\n".join(entries)

    return f"""Score this job candidate. Return ONLY valid JSON, no markdown.

JOB: {job_title} (Lombard/Chicago IL)

RESUME:
{resume_text}

SCORING RULES (read resume carefully for each):
{scoring_rules}

RULES:
- evidence must be an exact quoted string from the resume, or JSON null (not the string "null")
- score MUST be 0 when evidence is null
- do not infer — only use what is explicitly written

Fill in this JSON (replace FILL_INT with integer, FILL_QUOTE_OR_null with "exact quote" or null):
{{
  "criteria": {{
{criteria_block}
  }},
  "credential": null,
  "license_state": null,
  "license_status": null,
  "work_auth": null,
  "availability": null,
  "population": [],
  "setting_pref": [],
  "start_date": null,
  "pay_expectation": null,
  "flags": []
}}"""


def _call(client, prompt: str) -> ScoreResponse:
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=2000,
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw.strip())
            return ScoreResponse(**parsed)
        except (json.JSONDecodeError, ValidationError) as e:
            if attempt < 3:
                log.warning(f"  [score] invalid response attempt {attempt + 1}/4 — retrying ({type(e).__name__})")
                time.sleep(2 * (attempt + 1))
            else:
                raise
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = (attempt + 1) * 8
                log.warning(f"  rate limit — retrying in {wait}s")
                time.sleep(wait)
            else:
                raise


def _compute_scores(result: Optional[ScoreResponse], requirements: list, criteria: list) -> dict:
    """Deterministic scoring. Pass result=None to score from requirements only."""
    met   = sum(1 for r in requirements if r.get("match") == "met")
    total = len(requirements)
    req_score_50 = round((met / total) * 50) if total > 0 else 25

    jd_total_max = sum(c["max"] for c in criteria)
    jd_raw = 0
    scored_criteria = {}

    if result is not None:
        for c in criteria:
            key = c["key"]
            cr  = result.criteria.get(key, CriterionScore(max=c["max"]))
            awarded = cr.score  # Pydantic already enforced null evidence → 0
            jd_raw += awarded
            scored_criteria[key] = {
                "score":     awarded,
                "max":       c["max"],
                "evidence":  cr.evidence,
                "reasoning": cr.reasoning,
            }
        if jd_raw == 0 and jd_total_max > 0:
            log.warning("  [score] all JD criteria returned null evidence")
    else:
        for c in criteria:
            scored_criteria[c["key"]] = {"score": 0, "max": c["max"], "evidence": None, "reasoning": ""}

    jd_score_50 = round((jd_raw / jd_total_max) * 50) if jd_total_max > 0 else 0
    fit_score   = req_score_50 + jd_score_50

    return {
        "requirements_score":  req_score_50,
        "requirements_detail": f"{met}/{total} met",
        "jd_score":            jd_score_50,
        "jd_raw":              f"{jd_raw}/{jd_total_max}",
        "fit_score":           fit_score,
        "criteria":            scored_criteria,
    }


def _load_jd(job_id: str) -> str:
    if not JD_FILE.exists():
        return ""
    jd_data = load_json(JD_FILE, {})
    entry = jd_data.get(job_id, {})
    return _clean_jd(entry.get("job_description", ""))


def process_one(path: Path, client, lock: threading.Lock = None) -> bool:
    """Score one candidate file. Thread-safe when lock is provided."""
    if lock:
        lock.acquire()
    try:
        data = load_json(path)
    finally:
        if lock:
            lock.release()

    if not data:
        return False

    job_title    = data.get("job_title", "")
    job_id       = data.get("job_id", "")
    requirements = data.get("requirements", [])
    criteria     = _load_criteria(job_title)
    jd_clean     = _load_jd(job_id)

    # Build profile text: resume PDF first, fall back to scraped profile fields
    profile_text = (data.get("resume_text") or "").strip()
    if not profile_text:
        parts = [
            data.get("experience") or "",
            data.get("skills") or "",
            data.get("certifications") or "",
            data.get("education") or "",
            data.get("professional_summary") or "",
        ]
        profile_text = "\n\n".join(p.strip() for p in parts if p.strip())

    result: Optional[ScoreResponse] = None

    if profile_text and jd_clean:
        prompt = _build_prompt(job_title, jd_clean, profile_text, criteria)
        try:
            result = _call(client, prompt)
        except (json.JSONDecodeError, ValidationError) as e:
            log.warning(f"  score parse error [{data.get('full_name')}]: {e}")
        except Exception as e:
            log.warning(f"  score API error [{data.get('full_name')}]: {e}")
    elif not profile_text:
        log.info(f"  [score] no text for {data.get('full_name')} — scoring from requirements only")
    else:
        log.info(f"  [score] no JD for {data.get('full_name')} — scoring from requirements only")

    breakdown = _compute_scores(result, requirements, criteria)

    updates = {
        "fit_score":       breakdown["fit_score"],
        "score_breakdown": breakdown,
        "flags":           result.flags if result else ["No resume or profile text — scored from requirements only"],
        "pay_band_verdict": (
            "within" if breakdown["fit_score"] >= 70
            else "below" if breakdown["fit_score"] < 40
            else "unknown"
        ),
        "credential":      result.credential if result else None,
        "license_state":   result.license_state if result else None,
        "license_status":  result.license_status if result else None,
        "work_auth":       result.work_auth if result else None,
        "availability":    result.availability if result else None,
        "population":      result.population if result else [],
        "setting_pref":    result.setting_pref if result else [],
        "start_date":      result.start_date if result else None,
        "pay_expectation": result.pay_expectation if result else None,
    }

    if lock:
        lock.acquire()
    try:
        fresh = load_json(path) or data
        fresh.update(updates)
        save_json(path, fresh)
    finally:
        if lock:
            lock.release()

    return True


def main():
    parser = argparse.ArgumentParser(description="Score candidates 50% requirements + 50% JD")
    parser.add_argument("--rerun", action="store_true",
                        help="Re-score candidates that already have a fit_score")
    args = parser.parse_args()

    creds = load_credentials()
    openai_key = creds.get("openai_api_key")
    if not openai_key:
        log.error("openai_api_key missing from credentials / .env")
        sys.exit(1)

    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai not installed — run: pip install openai")
        sys.exit(1)

    client = OpenAI(api_key=openai_key)
    all_paths = sorted(CANDIDATES_DIR.glob("*.json"))

    if not all_paths:
        log.info("No candidate files found.")
        return

    if args.rerun:
        to_process = all_paths
    else:
        to_process = [p for p in all_paths if load_json(p, {}).get("fit_score") is None]

    log.info(f"Score: {len(to_process)} / {len(all_paths)} candidates to process")
    if not to_process:
        log.info("Nothing to do. Use --rerun to re-score.")
        return

    ok = fail = 0
    for i, path in enumerate(to_process, 1):
        data = load_json(path, {})
        name = data.get("full_name", path.stem)
        log.info(f"[{i}/{len(to_process)}] {name}")
        if process_one(path, client):
            scored = load_json(path, {})
            bd = scored.get("score_breakdown", {})
            log.info(
                f"  fit={bd.get('fit_score')}  "
                f"req={bd.get('requirements_score')}/50  "
                f"jd={bd.get('jd_score')}/50  "
                f"({bd.get('requirements_detail')}  jd_raw={bd.get('jd_raw')})"
            )
            ok += 1
        else:
            fail += 1
        if i < len(to_process):
            time.sleep(1.5)

    log.info(f"\nScore done. {ok} scored, {fail} failed.")


if __name__ == "__main__":
    main()
