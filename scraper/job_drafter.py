"""
OpenAI-powered job drafter.
- draft_from_text: extracts structured fields + writes JD from plain text
- draft_from_form: writes JD from filled form fields
"""
import json
import re
from openai import OpenAI
from scraper.utils import load_credentials, get_logger

log = get_logger("job_drafter")

_SYSTEM = """You are an expert HR copywriter and assistant for Reliable Medical Services, Inc. (RMS), a Medicare-certified home health agency in Lombard, Illinois.

Given free-text notes about a job opening, extract structured fields AND write a full, detailed job description in RMS brand style.

Return ONLY a valid JSON object — no markdown, no code fences, no explanation:

{
  "title": "exact job title",
  "location_type": "In person",
  "location_query": "city name only e.g. Lombard",
  "location_pick": "City, ST ZIPCODE e.g. Lombard, IL 60148",
  "hiring_timeline": "to 4 weeks",
  "hires_needed": 1,
  "job_types": ["Full-time"],
  "pay_min": null,
  "pay_max": null,
  "pay_period": "per hour",
  "pay_negotiable": false,
  "benefits": ["Health insurance", "Dental insurance", "Vision insurance"],
  "description": "FULL JD — see rules below",
  "missing": []
}

FIELD RULES:
- location_type: "In person" | "Hybrid" | "Fully remote". Default "In person".
- hiring_timeline: "to 7 days" | "to 2 weeks" | "to 4 weeks" | "More than 4 weeks". ASAP/urgent → "to 7 days", default → "to 4 weeks".
- job_types: ["Full-time"] | ["Part-time"] | ["Contract"] | ["Temporary"]. Default ["Full-time"].
- pay_period: "per hour" | "per day" | "per week" | "per month" | "per year".
- If salary is "fixed" at one number, set pay_min and pay_max to the SAME value.
- If salary is "N/A", "negotiable", "competitive", "TBD" → pay_negotiable=true, pay_min=null, pay_max=null.
- pay_min and pay_max: numeric strings (e.g. "60") or null. Never include "$" or "/hr".
- missing: ["title"] if title unclear, ["location"] if no city, ["salary"] if no pay AND not negotiable.
- benefits: ONLY include benefits explicitly mentioned by the user. If none mentioned, return an empty array [].

DESCRIPTION RULES — write a LONG, DETAILED, SPECIFIC job description with ALL of these sections:

1. TAGLINE: One punchy 1-2 sentence hook. Must be specific to the role (not generic).
   Example: "Lead with Purpose. Transform Lives Every Day."

2. COMPANY INTRO: 2-3 sentences about Reliable Medical Services, Inc. — home health agency in Illinois, patient-centered care, growing team.

3. "What You'll Do:" — 6 to 8 detailed bullet points starting with action verbs. Be SPECIFIC to the role. Never write vague bullets like "Strong skills required."

4. "What You Bring:" — 5 to 7 bullet points covering required qualifications, experience, certifications, and skills. Be specific.

5. "Bonus Points If You Have:" — 2 to 4 nice-to-have qualifications.

6. "Why Join Reliable Medical Services, Inc.?" — 4 to 5 bullet points selling the company: mission, growth, team, impact, compensation.

7. CLOSING: One sentence CTA like "Apply today and make a difference."

IMPORTANT:
- Minimum 350 words in the description.
- Never write placeholder text like "Strong technical skills" — always be specific.
- Adapt all sections to the actual role (ML Engineer, Nurse, Coordinator, etc.).
- Use professional but approachable tone. Mission-driven, growth-focused.
- The description value in JSON must use \\n for newlines and must NOT contain unescaped double quotes.
- Return ONLY the JSON object. No text before or after it. No markdown. No code fences.
"""

_FORM_SYSTEM = """You are an expert HR copywriter for Reliable Medical Services, Inc., a home health agency in Illinois.
Write a professional job description given structured job data. Follow RMS brand style:
1. Punchy 1-2 line tagline header
2. Short company intro (1-2 sentences)
3. "What You'll Do" section with action-verb bullet points
4. "What You Bring" section with qualification bullet points
5. "Why Join Reliable Medical Services" section
6. Closing call-to-action sentence

Return ONLY the job description text — no JSON, no markdown headers, just the formatted job description.
"""


def _call_openai(messages: list, temperature=0.2, max_tokens=4000) -> str:
    creds  = load_credentials()
    client = OpenAI(api_key=creds["openai_api_key"])
    resp   = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()


def _parse_json(raw: str) -> dict:
    # Extract the first JSON object even if surrounded by text/markdown
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON object found in response:\n{raw[:300]}")
    return json.loads(raw[start:end])


def draft_from_text(text: str) -> dict:
    """Extract job fields + write JD from free-form user text."""
    raw = _call_openai([
        {"role": "system", "content": _SYSTEM},
        {"role": "user",   "content": f"Extract job details from this text:\n\n{text}"},
    ])
    try:
        job = _parse_json(raw)
    except Exception as e:
        log.error(f"OpenAI JSON parse failed: {e}\nRaw:\n{raw}")
        return {"ok": False, "error": "AI returned invalid JSON — try rephrasing your description."}

    # Enforce guardrail: server-side missing field check
    missing = list(job.get("missing") or [])
    if not (job.get("title") or "").strip():
        if "title" not in missing:
            missing.append("title")
    if not (job.get("location_query") or "").strip():
        if "location" not in missing:
            missing.append("location")
    if not job.get("pay_negotiable") and not job.get("pay_min"):
        if "salary" not in missing:
            missing.append("salary")

    job["missing"] = missing
    job["ok"] = True
    return job


def draft_from_form(form: dict) -> dict:
    """Generate a JD description from filled form fields."""
    pay_str = "Competitive / Negotiable"
    if form.get("pay_negotiable"):
        pay_str = "Negotiable"
    elif form.get("pay_min") and form.get("pay_max"):
        pay_str = f"${form['pay_min']} – ${form['pay_max']} {form.get('pay_period', 'per hour')}"
    elif form.get("pay_min"):
        pay_str = f"From ${form['pay_min']} {form.get('pay_period', 'per hour')}"

    prompt = (
        f"Write a job description for this role:\n\n"
        f"Title: {form.get('title', '')}\n"
        f"Company: Reliable Medical Services, Inc.\n"
        f"Location: {form.get('location_query', 'Lombard, IL')} — {form.get('location_type', 'In person')}\n"
        f"Job Type: {', '.join(form.get('job_types', ['Full-time']))}\n"
        f"Pay: {pay_str}\n"
        f"Additional notes: {form.get('notes', '').strip() or 'None'}\n"
    )
    description = _call_openai([
        {"role": "system", "content": _FORM_SYSTEM},
        {"role": "user",   "content": prompt},
    ], temperature=0.5, max_tokens=1200)

    result = dict(form)
    result["description"] = description
    result["ok"]      = True
    result["missing"] = []
    # Set defaults for optional fields if not provided
    result.setdefault("hiring_timeline", "2 to 4 weeks")
    result.setdefault("hires_needed",    1)
    result.setdefault("pay_period",      "per hour")
    result.setdefault("benefits",        [])
    result.setdefault("location_type",   "In person")
    result.setdefault("location_pick",   f"{result.get('location_query', 'Lombard')}, IL 60148")
    return result
