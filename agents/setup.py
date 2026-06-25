"""
One-time setup: create company dirs + portal_users.json.
Run once before starting the portal:
    python agents/setup.py
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from werkzeug.security import generate_password_hash
from agents.company import RMS, SM, COMPANIES

_BASE = Path(__file__).parent.parent


def main():
    print("Setting up multi-company config...\n")

    for co in COMPANIES.values():
        co.bootstrap()
        print(f"  Dirs OK: {co.slug}")

    # SM settings template
    if not SM.settings_file.exists():
        SM.settings_file.write_text(json.dumps({
            "scheduling_link": "",
            "sender_name":  "Muneeb",
            "sender_title": "Recruiter",
            "company":      "Speech Masters",
        }, indent=2), encoding="utf-8")
        print("  Created config/sm/settings.json  (fill in scheduling_link)")

    # SM credentials template
    if not SM.credentials_file.exists():
        SM.credentials_file.write_text(json.dumps({
            "indeed_email":   "muneeb@speechmasterservices.com",
            "job_posting_id": "",
            "groq_api_key":   "",
        }, indent=2), encoding="utf-8")
        print("  Created config/sm/credentials.json  (add groq_api_key)")

    # SM message templates — copy from RMS if available
    if not SM.message_templates_file.exists():
        src = RMS.message_templates_file
        if src.exists():
            shutil.copy2(src, SM.message_templates_file)
        else:
            SM.message_templates_file.write_text(
                json.dumps({"interview_invite": [], "rejection": []}, indent=2),
                encoding="utf-8",
            )
        print("  Created config/sm/message_templates.json")

    # Portal users
    users_file = _BASE / "config" / "portal_users.json"
    if not users_file.exists():
        users = {
            "rmshomehealth@gmail.com": {
                "password_hash": generate_password_hash("rms@2024"),
                "company":       "rms",
                "display_name":  "Reliable Medical Services",
            },
            "muneeb@speechmasterservices.com": {
                "password_hash": generate_password_hash("miamif1121!"),
                "company":       "sm",
                "display_name":  "Speech Masters",
            },
        }
        users_file.write_text(json.dumps(users, indent=2), encoding="utf-8")
        print("\n  Created config/portal_users.json")
        print("  Portal login credentials:")
        print("  RMS : rmshomehealth@gmail.com      password: rms@2024")
        print("  SM  : muneeb@speechmasterservices.com / miamif1121!")
    else:
        print("  config/portal_users.json already exists — skipping")

    print("\nSetup complete.")
    print("  Install AutoGen:  pip install pyautogen")
    print("  Start portal   :  python frontend/app.py\n")


if __name__ == "__main__":
    main()
