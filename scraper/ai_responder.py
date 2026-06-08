"""
Groq-based AI reply generator for Indeed message conversations.

Usage:
    from scraper.ai_responder import generate_reply
    reply = generate_reply(thread)        # thread is a dict from conversations_scraper
"""
import json
import os
from pathlib import Path

ROOT         = Path(__file__).parent.parent
SETTINGS_FILE = ROOT / "config" / "settings.json"

_MODEL = "llama-3.3-70b-versatile"


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {}


def generate_reply(thread: dict) -> str:
    """
    Generate a contextual reply for a conversation thread using Groq.

    thread keys used: candidate_name, job_info, messages
      messages: list of {direction, content, timestamp, date_group}
    Returns the suggested reply as a plain string.
    """
    settings = _load_settings()
    api_key  = settings.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise ValueError(
            "Groq API key not set. Add 'groq_api_key' to config/settings.json "
            "or set the GROQ_API_KEY environment variable."
        )

    from groq import Groq
    client = Groq(api_key=api_key)

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
        label     = "YOU (employer)" if direction == "outbound" else f"CANDIDATE"
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

    system_prompt = f"""You are {sender_name}, the {sender_title} at {company}, a home health care company.
You are replying to a job candidate named {first_name} who applied for: {job_info}.

Guidelines:
- Warm, professional, and concise tone (3-6 sentences unless more is needed).
- Address the candidate by first name: {first_name}.
- If they ask to reschedule or ask about available times, provide the scheduling link: {scheduling_link}
- If they ask about a Zoom / video call link: explain that a unique Zoom link will be sent automatically via email after they book using the scheduling link above.
- If they confirm availability or ask a role question, respond helpfully and encourage them to book.
- Always sign off:
  {sender_name}
  {sender_title}
  {company}
- Do NOT include any template placeholders like {{{{scheduling_link}}}}. Use the actual URL if needed.
- Output the reply text only — no preamble, no explanation."""

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
