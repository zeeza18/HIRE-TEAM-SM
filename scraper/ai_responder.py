"""
OpenAI-based AI reply generator for Indeed message conversations.

Usage:
    from scraper.ai_responder import generate_reply
    reply = generate_reply(thread)        # thread is a dict from conversations_scraper
"""
import json
import os
from pathlib import Path

# utils loads .env automatically when imported
from scraper.utils import CONFIG_DIR

_MODEL        = "gpt-4o"
SETTINGS_FILE = CONFIG_DIR / "settings.json"   # patched by context.py for each company


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {}


def generate_reply(thread: dict) -> str:
    """
    Generate a contextual reply for a conversation thread using OpenAI.

    thread keys used: candidate_name, job_info, messages
      messages: list of {direction, content, timestamp, date_group}
    Returns the suggested reply as a plain string.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY not set. Add it to your .env file."
        )

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    settings        = _load_settings()
    candidate_name  = thread.get("candidate_name", "the candidate")
    first_name      = candidate_name.split()[0] if candidate_name else "there"
    job_info        = thread.get("job_info", "the position")
    messages        = thread.get("messages", [])

    sender_name     = settings.get("sender_name", "Ayesha")
    sender_title    = settings.get("sender_title", "Assistant Administrator")
    company         = settings.get("company", "Reliable Medical Services")
    scheduling_link = settings.get("scheduling_link", "")

    # Build readable conversation history
    lines = []
    for msg in messages:
        direction = msg.get("direction", "unknown")
        content   = msg.get("content", "").strip()
        ts        = msg.get("timestamp", "")
        dg        = msg.get("date_group", "")
        label     = msg.get("sender") or (sender_name if direction == "outbound" else candidate_name)
        date_str  = f"{dg} {ts}".strip()
        prefix    = f"[{date_str}] " if date_str else ""
        lines.append(f"{prefix}{label}: {content}")
    convo_text = "\n".join(lines) if lines else "(No messages yet)"

    # Last inbound message for quick context
    last_inbound = ""
    for msg in reversed(messages):
        if msg.get("direction") == "inbound":
            last_inbound = msg.get("content", "")
            break

    system_prompt = f"""You are {sender_name}, the {sender_title} at {company}, a home health care company based in Illinois.
You are messaging a job candidate named {first_name} who applied for: {job_info}.

Your role is a RECRUITER. You have already read the full conversation history below and you must reply naturally and helpfully as a real recruiter would.

Core recruiter behaviors:
1. SCHEDULING: If the candidate asks about scheduling, available times, or wants to talk — always provide the booking link: {scheduling_link}
   Tell them the call is about 30 minutes and they can pick a time that works for them.
2. ZOOM / VIDEO LINK: Explain that after booking via the link, a unique Zoom/call link is emailed to them automatically.
3. ROLE QUESTIONS: Answer questions about the position honestly. {job_info} is a home health care / administrator role at {company}.
   Common facts you can share: flexible scheduling, training provided, competitive pay, supportive team environment.
   If you don't know a specific detail (exact pay rate, hours), say you'd love to discuss it on the call.
4. FOLLOW-UP: If it's been a while since the candidate replied or if they expressed interest but haven't booked — gently follow up and re-share the booking link.
5. ENTHUSIASM: Show genuine interest in the candidate. Reference what they said in earlier messages to make it personal.
6. RESCHEDULING: If they need to reschedule or cancel, be understanding and resend the booking link so they can pick a new slot.
7. QUESTIONS BACK TO YOU: If they ask something you can't answer fully, invite them to bring it up on the call.

SHORT / ACKNOWLEDGEMENT replies (candidate says "Thank you", "OK", "Got it", "Sounds good", "See you then", etc.):
- Keep your reply SHORT (1–3 sentences max). Do not dump information.
- Acknowledge warmly and look forward to the call, or confirm the next step.
- Example: if they say "Thank you!" after receiving info → "You're welcome, {first_name}! Looking forward to speaking with you. Feel free to reach out if anything comes up before then."
- Example: if they say "OK sounds good" after booking → "Perfect! You'll receive a confirmation email shortly. See you then!"
- Never re-explain things they've already been told in the same conversation.

GUARDRAILS — you MUST follow these at all times:
- DO NOT promise or state specific salaries, hourly rates, or pay ranges. Say "We'd love to discuss compensation on the call."
- DO NOT make commitments about start dates, hours, benefits, or job offers. These are decided by the hiring manager.
- DO NOT share any patient information, employee personal details, or internal company data.
- DO NOT continue engaging if the candidate clearly says they are not interested — simply acknowledge and wish them well.
- DO NOT reply to messages that are spam, sales pitches, or completely unrelated to the job application. If the message makes no sense for a job context, reply: "SKIP"
- DO NOT agree to terms, contracts, NDAs, or any legal language in messages.
- DO NOT use all-caps, excessive exclamation marks, or overly casual language (no "Hey!", "Awesome!!!", etc.).
- Keep replies focused on scheduling the call and answering genuine job questions.

Tone: Warm, professional, concise (1–3 sentences for short replies, 3–6 for questions or new topics). Write like a real person, not a template.

Always sign off exactly as:
{sender_name}
{sender_title}
{company}

Do NOT include template placeholders like {{{{scheduling_link}}}}. Use the actual value.
Output the reply text only — no preamble, no explanation. If your guardrails require skipping this message, output exactly: SKIP"""

    user_prompt = f"""Conversation so far:

{convo_text}

---
Latest message from the candidate:
\"{last_inbound}\"

Write your reply:"""

    response = client.chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=512,
        temperature=0.55,
    )

    return response.choices[0].message.content.strip()


if __name__ == "__main__":
    test_thread = {
        "candidate_name": "Neha Patel",
        "job_info": "Administrator in Lombard, IL",
        "messages": [
            {
                "direction": "outbound",
                "content": "Hi Neha, thank you for applying! Please book a call here: https://calendly.com/rmshomehealth/30min",
                "timestamp": "1:09 PM",
                "date_group": "June 3",
            },
            {
                "direction": "inbound",
                "content": "Hi, I am available Monday afternoon or Tuesday morning. Can we do a Zoom call?",
                "timestamp": "10:10 AM",
                "date_group": "June 5",
            },
        ],
    }
    reply = generate_reply(test_thread)
    print("Generated reply:\n")
    print(reply)
