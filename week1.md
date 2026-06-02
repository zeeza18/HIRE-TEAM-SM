# Week 1 — Indeed Scraper + Applicant JSON Schema + Screening Data Model

## Goal
By end of week 1, a Playwright script can log into the Indeed employer account, pull all new SLP applicants, and save each one as a structured JSON file locally — ready for screening logic in Week 2.

---

## Deliverables

### 1. Playwright Indeed Scraper

A Python script (`scraper/indeed_scraper.py`) that:

1. Launches a browser (headless or visible for debugging)
2. Logs into Indeed employer account using stored credentials
3. Navigates to the SLP job posting's applicant list
4. For each new applicant (not already in our local data):
   - Reads their name, contact info, resume text, and any answers to screener questions
   - Saves them to the local JSON store
5. Updates the `last_run.json` so next run only picks up new applicants

**Credentials stored in:** `config/credentials.json` (gitignored)

```json
{
  "indeed_email": "your@email.com",
  "indeed_password": "yourpassword",
  "job_posting_id": "indeed-job-id-here"
}
```

**Last run tracker:** `data/last_run.json`

```json
{
  "last_run_at": "2026-06-10T09:00:00Z",
  "applicants_seen": ["indeed-applicant-id-1", "indeed-applicant-id-2"]
}
```

---

### 2. Candidate JSON Schema

Each applicant scraped from Indeed is saved as `data/candidates/<indeed_id>.json`.

```json
{
  "id": "indeed-abc123",
  "source": "indeed",
  "scraped_at": "2026-06-10T09:05:00Z",

  "full_name": "Jane Smith",
  "phone": null,
  "email": "jane@example.com",
  "indeed_profile_url": "https://employers.indeed.com/...",

  "resume_text": "Licensed SLP with 4 years home health experience...",

  "license_state": null,
  "license_status": null,
  "credential": null,
  "population": [],
  "setting_pref": [],
  "geography": "Naperville, IL",
  "travel_radius_mi": null,
  "availability": null,
  "start_date": null,
  "pay_expectation": null,
  "work_auth": null,

  "status": "new",
  "fit_score": null,
  "flags": [],
  "pay_band_verdict": null,
  "decline_reason": null,

  "indeed_message_sent": false,
  "indeed_message_sent_at": null,

  "interview": null,
  "offer_outcome": null,
  "notes": ""
}
```

**Fields populated at scrape time:** `id`, `source`, `scraped_at`, `full_name`, `email`, `indeed_profile_url`, `resume_text`, `geography` (if on profile)

**Fields populated by screening logic (Week 2):** everything else — Claude reads the resume and extracts or infers the missing fields.

---

### 3. Candidates Index

`data/candidates_index.json` — lightweight list updated every scrape run.

```json
[
  {
    "id": "indeed-abc123",
    "full_name": "Jane Smith",
    "source": "indeed",
    "scraped_at": "2026-06-10T09:05:00Z",
    "status": "new",
    "fit_score": null
  }
]
```

---

### 4. Local Directory Structure

```
config/
  credentials.json        ← gitignored (Indeed login + job ID)

scraper/
  indeed_scraper.py       ← Playwright scraper

data/
  candidates/
    indeed-abc123.json    ← one file per applicant
  candidates_index.json
  last_run.json
  audit_log.json          ← started this week, filled in Week 2
  recruiter_notifications.json
  interview_slots.json
  decline_report.json
  reminder_queue.json
```

---

### 5. What the Scraper Pulls from Indeed

For each applicant on the employer dashboard:

| Data point | Where it comes from |
|------------|-------------------|
| Name | Applicant card |
| Email / phone | Contact info section (if visible) |
| Resume text | Resume tab — full text extracted |
| Indeed profile URL | Applicant card link |
| Location | Profile / resume |
| Applied date | Applicant card timestamp |

Resume text is stored raw — Claude will parse it in Week 2 to extract license, credential, experience, and setting preference.

---

### 6. `.gitignore` Entries

```
config/credentials.json
data/
```

No candidate PII in version control.

---

## Tasks This Week

- [ ] Set up Python project with Playwright (`pip install playwright`)
- [ ] Build `indeed_scraper.py` — login, navigate to applicant list, loop through new applicants
- [ ] Extract and save applicant data to candidate JSON files
- [ ] Update `candidates_index.json` and `last_run.json` on each run
- [ ] Create `config/credentials.json` (gitignored) with Indeed login
- [ ] Confirm `.gitignore` covers credentials and data folder
- [ ] Test: run scraper, verify 3–5 real applicants land in `data/candidates/`
- [ ] Weekly demo to S
