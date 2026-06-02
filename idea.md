# SLP Recruiter Agent — Project Idea

## What We're Building

An automated recruiter agent for **Speech Masters Inc.** (home health therapy staffing, Lombard IL) that:
1. Watches Indeed for new SLP applicants
2. Screens them automatically against our criteria
3. Messages qualified candidates directly on Indeed
4. Books interviews
5. Rewrites the job posting to attract more leads

No manual checking of Indeed. No office staff in the loop until a candidate is ready to interview.

---

## The Real Problem

We're rejecting referrals because we're short **~2 SLPs**. We need to hire fast.

But here's the honest truth: **the problem is NOT finding or screening candidates**. Internal analysis shows 12+ job offers were already extended — and most were **rejected by candidates**. That's a pay/value-proposition problem, not a pipeline problem.

So this agent has two jobs:
1. **Speed up and automate** the screening + outreach so strong candidates don't sit waiting
2. **Capture the data** on why candidates decline, so we can fix the real blocker (comp, schedule, travel, competing offers)

---

## Who We're Hiring

**Role:** Speech-Language Pathologist (SLP)  
**Setting:** Home health (travel required)  
**Location:** Lombard, IL area  
**Pay target:** $50–$55/visit (net ~$59,685/year profit per hire at ~80 visits/month)

Pay reference:
| SLP | Rate/visit | Cost % | Verdict |
|-----|-----------|--------|---------|
| Gabby Djupstrom | $48.66 | 46% | Target band |
| Anyia Clayton | $51.34 | 49% | Target band |
| Meghan Flynn-Colan | $55.23 | 53% | Acceptable ceiling |
| Larry Panozzo | $75.91 | 72% | Too high — flag for human |

---

## How It Works (Technical Overview)

```
Indeed Employer Dashboard
        ↓
  Playwright script logs in + checks new applicants
        ↓
  Reads each applicant's profile / resume
        ↓
  Runs screening criteria (license, travel, pay, geography, credential)
        ↓
  Hard reject → logged, no message sent
  Qualified → message sent on Indeed with interview booking link
  High pay / edge case → flagged for S to review
        ↓
  Interview booked → confirmation + reminder sent
        ↓
  Job posting optimizer → reads current posting, rewrites for more leads
```

**Tech stack (local-first, JSON storage, no server yet):**
- **Playwright** (Python) — browser automation to drive the Indeed employer dashboard
- **Claude API** — screening logic, message generation, job posting improvement
- **JSON files** — all candidate data, audit log, notifications stored locally
- **Calendly (or similar)** — interview self-scheduling link dropped into messages
- Migrate to server + database later

---

## What the Agent Does

### 1. Watches Indeed
Logs into the Indeed employer account on a schedule. Checks the SLP job posting for new applicants since last run.

### 2. Screens
For each new applicant, reads their profile and resume. Runs structured criteria:
- IL license (active or pending)
- CCC-SLP / CF / SLPA credential
- Willingness to do home health / travel
- Geography / proximity to service area
- Pay expectations vs. our $50–$55 band
- Availability and start date
- Work authorization

### 3. Acts
- **Hard reject:** auto-logs, no message sent
- **Qualified:** sends a personalized message on Indeed with a self-scheduling link
- **High pay / edge case:** flags for S with full profile snapshot — human decides

### 4. Books
Candidate clicks the scheduling link, picks a slot. Confirmation and 24h reminder sent.

### 5. Optimizes the Job Posting
Reads the current Indeed posting → Claude rewrites it to be more compelling for SLPs → outputs a revised draft for review.

---

## What the Agent Does NOT Do

- Make the final hire/offer decision (human only — always)
- Handle non-SLP roles in v1
- Make clinical judgments
- Decide on worker classification (W-2 vs 1099) — flags and routes to human

---

## Note on Indeed Automation

Indeed's ToS technically prohibits bot activity. Risk is low at small scale (one employer, light activity), but worth knowing. If Indeed ever restricts access, the fallback is applying for Indeed's official employer API. Architecture is the same either way.

---

## Definition of Done (v1)

- [ ] Agent logs into Indeed and detects new applicants automatically
- [ ] Hard filters applied — unqualified candidates logged, no message sent
- [ ] Qualified candidates receive a personalized Indeed message with booking link
- [ ] High-pay candidates flagged for S
- [ ] Interview booked end-to-end with reminder
- [ ] All decisions written to audit log (JSON)
- [ ] Decline-reason capture working and feeding a report
- [ ] Job posting optimizer produces a revised draft
- [ ] 10 dry-run candidates processed cleanly

---

## Timeline

| Week | Focus |
|------|-------|
| Week 1 | Playwright Indeed scraper + applicant JSON schema + screening data model |
| Week 2 | Screening logic, fit scoring, pay-band flags, audit log, message generation |
| Week 3 | Indeed messaging bot, interview booking, reminders, recruiter notifications, job posting optimizer, dry runs |
