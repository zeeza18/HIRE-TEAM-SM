# Week 2 — Screening Logic, Fit Scoring, Pay-Band Flags, Audit Log, Message Generation

## Goal
By end of week 2, the agent reads each scraped candidate's resume, extracts screening fields using Claude, runs hard filters and fit scoring, flags edge cases, and generates a personalized outreach message ready to send on Indeed — all logged to the audit trail.

---

## Deliverables

### 1. Resume Parsing with Claude

For each candidate with `status: "new"`, send their `resume_text` to the Claude API with a structured prompt. Claude returns a JSON object with the extracted screening fields.

**Prompt approach:**
> "Read this SLP resume. Extract: IL license status, credential type (CCC-SLP/CF/SLPA), preferred settings, geography, pay expectation if mentioned, availability, work authorization. Return as JSON."

Claude's output is merged into the candidate JSON, filling the null fields from Week 1.

**After parsing, candidate JSON looks like:**

```json
{
  "license_state": "IL",
  "license_status": "active",
  "credential": "CCC-SLP",
  "population": ["adult", "pediatric"],
  "setting_pref": ["home_health", "outpatient"],
  "geography": "Naperville, IL",
  "travel_radius_mi": 25,
  "availability": "PT",
  "pay_expectation": 52,
  "work_auth": "yes"
}
```

If Claude can't confidently extract a field from the resume, it returns `null` — those candidates go to `"recruiter_review"` so a human can fill the gap.

---

### 2. Hard Filters (Auto-Disqualify)

Run immediately after resume parsing. If any trigger, set `status: "declined"`, write `decline_reason`, log to audit. No message sent.

| Filter | Condition | Logged Reason |
|--------|-----------|---------------|
| No IL license | `license_state` ≠ IL OR `license_status === "none"` | `"no_il_license"` |
| No work auth | `work_auth === "no"` | `"no_work_auth"` |
| Won't travel | `setting_pref` excludes `"home_health"` AND `travel_radius_mi < 10` | `"no_travel"` |

---

### 3. Fit Score (0–100)

Computed after passing hard filters. Written to `fit_score` and `scoring_breakdown` in the candidate JSON.

| Dimension | Max Points | Scoring Logic |
|-----------|-----------|---------------|
| Credential | 25 | CCC-SLP = 25, CF = 15, SLPA = 5 |
| Pay expectation | 25 | $50–55 = 25, $55–60 = 15, >$60 = 5 + human flag |
| Geography | 20 | Within Lombard/service area = 20, within 30mi = 12, further = 5 |
| Availability + start date | 20 | FT + starts ≤30 days = 20, PT + soon = 14, PRN or far start = 8 |
| Setting match | 10 | Home health in `setting_pref` = 10, other = 5 |

---

### 4. Routing Logic

```
fit_score >= 70 AND pay_expectation <= 55  →  status: "fast_track"
pay_expectation > 60                        →  status: "flagged"  + flag: "high_pay"
any required field still null              →  status: "recruiter_review"
fit_score < 50                              →  status: "recruiter_review"
everything else                             →  status: "recruiter_review"
```

**Fast-track** → message gets generated and queued for sending (Week 3 sends it)  
**Flagged** → recruiter notification written with full comp snapshot, no auto-message  
**Recruiter review** → notification written, human checks the record

---

### 5. Updated Candidate JSON After Scoring

```json
{
  "status": "fast_track",
  "fit_score": 82,
  "flags": [],
  "pay_band_verdict": "in_band",
  "scoring_breakdown": {
    "credential": 25,
    "pay": 25,
    "geography": 12,
    "availability": 20,
    "setting_match": 0
  },
  "outreach_message": "Hi Jane — saw your application for the SLP role at Speech Masters...",
  "outreach_message_generated_at": "2026-06-11T10:00:00Z"
}
```

`pay_band_verdict`: `"in_band"` | `"above_band"` | `"high_flag"`

---

### 6. Outreach Message Generation (Claude)

For every `fast_track` candidate, Claude generates a personalized Indeed message based on:
- Candidate's name and background (from resume)
- The role details and pay band
- A Calendly (or similar) self-scheduling link for interview booking

**Prompt approach:**
> "Write a friendly, brief Indeed message to [name], an SLP who applied to our home health role. Mention their background briefly. Invite them to book a quick call. Include this scheduling link: [link]. Keep it under 100 words."

Message saved to `outreach_message` in the candidate JSON. Actually sent in Week 3.

---

### 7. Audit Log

Every decision written to `data/audit_log.json`. Append-only.

```json
[
  {
    "timestamp": "2026-06-11T10:05:00Z",
    "candidate_id": "indeed-abc123",
    "candidate_name": "Jane Smith",
    "event": "resume_parsed",
    "triggered_by": "claude"
  },
  {
    "timestamp": "2026-06-11T10:05:10Z",
    "candidate_id": "indeed-abc123",
    "candidate_name": "Jane Smith",
    "event": "scored",
    "fit_score": 82,
    "status": "fast_track",
    "pay_band_verdict": "in_band",
    "triggered_by": "system"
  },
  {
    "timestamp": "2026-06-11T10:06:00Z",
    "candidate_id": "indeed-def456",
    "candidate_name": "Bob Lee",
    "event": "hard_filter_decline",
    "reason": "no_il_license",
    "triggered_by": "system"
  }
]
```

---

### 8. Recruiter Notifications

Written to `data/recruiter_notifications.json` for flagged and recruiter_review candidates.

```json
[
  {
    "notification_id": "notif-001",
    "created_at": "2026-06-11T10:10:00Z",
    "candidate_id": "indeed-xyz789",
    "candidate_name": "Mark D",
    "status": "flagged",
    "fit_score": 74,
    "pay_expectation": 72,
    "pay_band_verdict": "high_flag",
    "flags": ["high_pay"],
    "summary": "Strong CCC-SLP, home health experience, but asking $72/visit. Above Larry territory. Route to S.",
    "read": false
  }
]
```

---

## Tasks This Week

- [ ] Build Claude resume parser — extract all screening fields from `resume_text`
- [ ] Implement hard filter logic — decline + audit log
- [ ] Implement fit score across all 5 dimensions
- [ ] Implement routing logic and update candidate JSON
- [ ] Build Claude message generator for fast-track candidates
- [ ] Write all decisions to `audit_log.json`
- [ ] Write recruiter notifications for flagged/review candidates
- [ ] Test: run 5+ scraped candidates through the full pipeline, verify scores + routing
- [ ] Weekly demo to S
