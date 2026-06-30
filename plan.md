# AI Interviewer Agent — Implementation Plan

## Overview

Replace the Calendly scheduling link with a self-hosted AI Interviewer portal.
When a candidate is liked (manually or via auto-mode), the outreach message sends
a unique interview link (`/interview/<token>`) instead of a Calendly link.
The candidate opens that link and is interviewed live by an animated 3D AI agent
for a structured 30-minute session, including document uploads. Results are
surfaced back in the recruiter dashboard.

---

## Phase 1 — Unique Interview Link Generation (Backend)

### 1.1  Interview Token Table
- Add `data/<company>/interviews/<token>.json` per candidate session.
- Schema:
  ```json
  {
    "token": "abc123",
    "candidate_name": "Neha Shah",
    "candidate_profile_url": "...",
    "job_id": "...",
    "role": "Home Health Aide",
    "company_key": "rms",
    "status": "pending",          // pending | in_progress | completed | expired
    "created_at": "ISO8601",
    "expires_at": "ISO8601 +7d",
    "questions": [],              // populated from question bank at creation
    "transcript": [],             // filled during interview
    "uploads": [],                // file paths saved server-side
    "score": null,                // AI score after completion
    "summary": null
  }
  ```
- Token: 16-char URL-safe random string, unique per candidate+job.

### 1.2  Token Generation Trigger Points
- **Manual like** — recruiter clicks thumb-up in dashboard → token created → link injected into invite message.
- **Auto-mode approve** — `indeed_scraper.py` auto-approves → same token creation step runs before `messenger.py` fires the outreach.
- **Re-invite** — dashboard button regenerates a fresh token (old one expires immediately).

### 1.3  Config Change
- `config/settings.json`: add key `"interview_base_url": "http://localhost:5000/interview"` (prod: real domain).
- `messenger.py`: when `interview_base_url` is set, replace `{{scheduling_link}}` with `{interview_base_url}/{token}` instead of the Calendly link. Calendly link stays as fallback if the key is absent.
- `ai_responder.py`: update the scheduling prompt line to say "here is a 30-min AI video interview link" instead of "Zoom/call link".

---

## Phase 2 — Question Bank & Interview Script

### 2.1  Question Bank Config
- New file: `config/interview_questions.json`
- Structure:
  ```json
  {
    "common": [
      "Tell me about yourself and your background.",
      "Why are you interested in this role?",
      "What is your availability? Are you looking for full-time or part-time?",
      "Do you have reliable transportation?",
      "Tell me about a difficult situation at work and how you handled it."
    ],
    "roles": {
      "Home Health Aide": [
        "Do you have a valid HHA or CNA certification? Are you comfortable uploading a copy today?",
        "Have you cared for patients with dementia or Alzheimer's?",
        "How do you handle emergencies while working alone with a patient?",
        "Describe your experience with personal care tasks (bathing, dressing, transfers).",
        "Do you have any physical limitations that would prevent lifting up to 50 lbs?"
      ],
      "Registered Nurse": [
        "What nursing licenses do you currently hold? Please upload your RN license.",
        "How many years of clinical experience do you have post-licensure?",
        "Describe your experience with home health documentation systems.",
        "How do you prioritize when managing multiple high-acuity patients?",
        "Are you CPR/BLS certified? Upload your certification card."
      ]
    },
    "closing": [
      "Do you have any questions about the role or the company?",
      "What is your expected start date if selected?",
      "Is there anything else you would like us to know about you?"
    ]
  }
  ```
- Questions are merged: common + role-specific + closing = ~12–15 questions.
- Question order is randomized within each section to avoid scripted answers.

### 2.2  Upload Requests (woven into questions)
- Certain questions carry a `"request_upload": true` flag and a `"upload_label"` (e.g., "HHA Certificate", "RN License", "CPR Card", "Resume/CV").
- The interviewer pauses after asking the question and waits up to 60 seconds for a file upload before proceeding.

---

## Phase 3 — Frontend: Animated 3D Interviewer UI

### 3.1  Tech Stack
- **Three.js** — 3D rendering in browser, no plugin needed.
- **Ready Player Me** (free tier) — pre-built humanoid avatar GLB models, lip-sync compatible.
- **Web Speech API** (browser built-in) — text-to-speech for interviewer voice, speech-to-text for candidate responses. Fallback: typed text input.
- Single HTML page served by Flask at `/interview/<token>`.

### 3.2  Page Layout
```
┌─────────────────────────────────────────────────────┐
│  [Company Logo]          INTERVIEW IN PROGRESS  30:00│
├──────────────────────────┬──────────────────────────┤
│                          │                          │
│   3D Avatar (Three.js)   │  Transcript / subtitles  │
│   lip-synced to TTS      │  (scrolling live)        │
│                          │                          │
├──────────────────────────┴──────────────────────────┤
│  [Upload File]   [🎤 Start Speaking]   [Type Answer] │
│  Progress: Q3 of 14           [Next →]              │
└─────────────────────────────────────────────────────┘
```

### 3.3  Interview Flow (client-side state machine)
```
INTRO → Q1 → [await answer] → Q2 → ... → UPLOAD_REQUEST → [await file]
→ ... → CLOSING_Q → WRAP_UP → SUBMIT → THANK_YOU screen
```

States:
- `intro` — avatar greets candidate by name, explains 30-min session.
- `asking` — avatar speaks question via TTS, displays text subtitle.
- `listening` — mic active, STT transcribes; candidate can also type.
- `upload_prompt` — file picker appears, upload progress shown.
- `transition` — brief pause, avatar nods/moves naturally (idle animation).
- `wrap_up` — avatar thanks candidate, submission spinner.
- `done` — static "Thank you" card shown.

### 3.4  Avatar Animations
- Idle loop (subtle breathing + head movement) — always playing.
- Talking animation — plays while TTS is speaking.
- Nodding — plays when candidate finishes speaking.
- Thinking — 2-second pause animation between question and speaking.

### 3.5  Timer
- 30-minute countdown displayed in header.
- At 25 minutes: avatar says "We have a few minutes left, let me ask the last questions."
- At 30 minutes: gracefully wraps up mid-question, moves to closing.

---

## Phase 4 — Backend: Interview Session API

New Flask routes added to `frontend/app.py`:

| Route | Method | Purpose |
|-------|--------|---------|
| `/interview/<token>` | GET | Serve interviewer HTML page |
| `/api/interview/<token>` | GET | Return session metadata + question list |
| `/api/interview/<token>/answer` | POST | Save transcript entry `{question_index, text, duration_s}` |
| `/api/interview/<token>/upload` | POST | Accept file upload, save to `data/<co>/uploads/<token>/` |
| `/api/interview/<token>/complete` | POST | Mark session done, trigger AI scoring |
| `/api/interview/<token>/status` | GET | Dashboard polling endpoint |

### 4.1  File Uploads
- Accept: PDF, JPG, PNG, DOCX — max 10 MB per file.
- Saved to: `data/<company_key>/uploads/<token>/<original_filename>`.
- File list stored in `interviews/<token>.json → uploads[]`.

### 4.2  Post-Interview AI Scoring
After `/complete` is called:
1. Load full transcript.
2. Call OpenAI with a scoring prompt:
   - Rate communication clarity (1–10).
   - Rate relevant experience match (1–10).
   - Identify red flags or strong positives.
   - Generate 3-sentence summary.
3. Write `score` and `summary` back to `interviews/<token>.json`.
4. Append an audit log entry so dashboard can surface the result.

---

## Phase 5 — Dashboard Integration

### 5.1  Candidate Card Changes
- **Status badge**: `Interview Pending` / `Interview In Progress` / `Interview Done`.
- **"View Interview"** button on completed sessions → opens a modal with:
  - Full transcript (Q&A pairs).
  - AI score breakdown.
  - Uploaded files (download links).
  - AI summary paragraph.

### 5.2  Notification
- When an interview completes, a toast/alert appears in the recruiter dashboard: "Neha Shah completed her interview — Score 7.8/10. View results."
- Dashboard polls `/api/interview/<token>/status` every 30 seconds while the tab is open (or use SSE).

### 5.3  Settings UI
- Add field in the settings panel: **Interview Base URL** (default `http://localhost:5000/interview`).
- When this field is filled, the UI shows "Self-hosted AI Interview" mode; when empty, falls back to Calendly link.

---

## Phase 6 — Multi-Company Support

- Each company (`rms`, `sm`) has its own `config/<co>/settings.json` (already the case).
- Each company can configure:
  - Its own `interview_base_url`.
  - Its own question bank override (or inherit from global).
  - Avatar skin / company logo shown on the interview page.

---

## Data Flow Summary

```
Recruiter likes profile
        ↓
Generate token → write interviews/<token>.json (status: pending)
        ↓
messenger.py sends: "Please complete your interview: /interview/<token>"
        ↓
Candidate opens link → Flask serves interviewer page
        ↓
JS fetches /api/interview/<token> → loads questions + candidate name
        ↓
30-min interview runs (avatar speaks → candidate answers → files upload)
        ↓
/api/interview/<token>/complete → OpenAI scores → token.json updated
        ↓
Dashboard surfaces result in candidate card
```

---

## File Changes Summary

| File | Change |
|------|--------|
| `config/settings.json` | Add `interview_base_url` key |
| `config/sm/settings.json` | Add `interview_base_url` key |
| `config/interview_questions.json` | **New** — question bank |
| `scraper/messenger.py` | Use interview link when `interview_base_url` is set |
| `scraper/ai_responder.py` | Update scheduling prompt to reference AI interview |
| `scraper/interview_tokens.py` | **New** — token gen, session CRUD helpers |
| `frontend/app.py` | Add 5 new `/interview` + `/api/interview` routes |
| `frontend/static/interview.html` | **New** — full 3D interviewer page (Three.js + avatar) |
| `frontend/static/interview.js` | **New** — interview state machine, TTS/STT, upload handling |
| `frontend/static/index.html` | Add "View Interview" modal + status badge to candidate cards |

---

## Open Questions / Decisions Needed

1. **Avatar source** — Use Ready Player Me free GLB, or a custom static model? RPM requires a free account; a static model is fully offline.
2. **TTS voice** — Browser Web Speech API (free, robotic) vs OpenAI TTS API (better quality, paid per character). Recommend OpenAI TTS streamed as audio.
3. **STT** — Browser `SpeechRecognition` API (free, Chrome-only) vs OpenAI Whisper API (cross-browser, costs ~$0.006/min). Recommend Whisper for reliability.
4. **Hosting** — Interview page only needs the Flask server to be publicly reachable. Options: ngrok for dev, a small VPS / Railway for prod.
5. **Recording** — Should we record video/audio of the candidate? Adds storage + privacy/consent requirements. Recommend transcript-only for v1.

---

## Build Order

1. Phase 1 (token generation + messenger swap) — backend only, low risk
2. Phase 2 (question bank) — config file, no code risk
3. Phase 4 (API routes) — Flask additions, testable immediately
4. Phase 3 (3D UI) — largest chunk, frontend-only
5. Phase 5 (dashboard integration) — wire results back in
6. Phase 6 (multi-company) — straightforward after single-company works
