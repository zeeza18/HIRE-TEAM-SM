"""
AutoGen multi-agent pipelines.

  run_candidate_pipeline(slug)  — scrape → score → contact/reject
  run_messaging_pipeline(slug)  — scrape conversations → auto-reply

Falls back to direct tool calls if pyautogen is not installed.
"""
import logging
import os

from agents.company import COMPANIES
from agents.tools import (
    scrape_new_candidates, score_candidates, enrich_candidates, recruit_candidates,
    scrape_conversations, auto_reply_conversations,
)

log = logging.getLogger("agents.pipeline")


def _llm_config(slug: str) -> dict | None:
    co = COMPANIES.get(slug)
    if not co:
        return None
    creds   = co.load_credentials()
    api_key = creds.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return None
    return {
        "config_list": [{
            "model":    "llama-3.3-70b-versatile",
            "api_key":  api_key,
            "base_url": "https://api.groq.com/openai/v1",
            "api_type": "openai",
        }],
        "temperature": 0,
        "timeout":     120,
    }


# ── Candidate pipeline ───────────────────────────────────────────────────────

def run_candidate_pipeline(slug: str, threshold: int = 80, new_only: bool = True):
    co = COMPANIES.get(slug)
    if not co:
        log.error(f"Unknown company: {slug}"); return
    mode = "new-only" if new_only else "FULL"
    log.info(f"[{slug}] Candidate pipeline start (threshold={threshold}, mode={mode})")

    try:
        import autogen
        llm = _llm_config(slug)
        if not llm:
            return _direct_candidate(slug, threshold, new_only)

        agent = autogen.AssistantAgent(
            name="CandidatePipeline",
            system_message=(
                f"You are the automated recruiter for {co.display_name}. "
                f"Run these 4 steps in order by calling each function once:\n"
                f"1. scrape_new_candidates(company_slug='{slug}', new_only={new_only})\n"
                f"2. score_candidates(company_slug='{slug}')\n"
                f"3. enrich_candidates(company_slug='{slug}')\n"
                f"4. recruit_candidates(company_slug='{slug}', threshold={threshold})\n"
                "After all 4 are done reply with: PIPELINE_COMPLETE"
            ),
            llm_config=llm,
        )
        proxy = autogen.UserProxyAgent(
            name="Executor",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=12,
            code_execution_config=False,
            is_termination_msg=lambda m: "PIPELINE_COMPLETE" in (m.get("content") or ""),
        )
        autogen.register_function(scrape_new_candidates, caller=agent, executor=proxy,
            name="scrape_new_candidates",
            description="Scrape candidates from Indeed. new_only=False = all tabs, True = New tab only")
        autogen.register_function(score_candidates, caller=agent, executor=proxy,
            name="score_candidates",
            description="AI-score all unscored candidates (0-100)")
        autogen.register_function(enrich_candidates, caller=agent, executor=proxy,
            name="enrich_candidates",
            description="Fill profile text fields (summary/experience/certifications/skills) from resume_text")
        autogen.register_function(recruit_candidates, caller=agent, executor=proxy,
            name="recruit_candidates",
            description="Contact candidates >= threshold; reject those below")

        proxy.initiate_chat(agent, message=(
            f"Run the candidate pipeline for company='{slug}' "
            f"threshold={threshold} new_only={new_only}."
        ))

    except ImportError:
        log.warning("pyautogen not installed — direct pipeline")
        _direct_candidate(slug, threshold, new_only)
    except Exception as e:
        log.error(f"[{slug}] Candidate pipeline error: {e}")
        _direct_candidate(slug, threshold, new_only)


def _direct_candidate(slug: str, threshold: int = 80, new_only: bool = True):
    log.info(f"[{slug}] Direct candidate pipeline (new_only={new_only})")
    for fn, args in [
        (scrape_new_candidates, (slug, new_only)),
        (score_candidates,      (slug,)),
        (enrich_candidates,     (slug,)),
        (recruit_candidates,    (slug, threshold)),
    ]:
        result = fn(*args)
        log.info(f"  {fn.__name__}: {result[:120]}")


# ── Messaging pipeline ───────────────────────────────────────────────────────

def run_messaging_pipeline(slug: str):
    co = COMPANIES.get(slug)
    if not co:
        log.error(f"Unknown company: {slug}"); return
    log.info(f"[{slug}] Messaging pipeline start")

    try:
        import autogen
        llm = _llm_config(slug)
        if not llm:
            return _direct_messaging(slug)

        agent = autogen.AssistantAgent(
            name="MessagingAgent",
            system_message=(
                f"You are the automated messaging agent for {co.display_name}. "
                f"Run these 2 steps in order:\n"
                f"1. scrape_conversations(company_slug='{slug}')\n"
                f"2. auto_reply_conversations(company_slug='{slug}')\n"
                "After both steps reply with: MESSAGING_COMPLETE"
            ),
            llm_config=llm,
        )
        proxy = autogen.UserProxyAgent(
            name="Executor",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=6,
            code_execution_config=False,
            is_termination_msg=lambda m: "MESSAGING_COMPLETE" in (m.get("content") or ""),
        )
        autogen.register_function(scrape_conversations, caller=agent, executor=proxy,
            name="scrape_conversations",
            description="Scrape all conversations from the Indeed inbox")
        autogen.register_function(auto_reply_conversations, caller=agent, executor=proxy,
            name="auto_reply_conversations",
            description="Send AI replies to all unanswered inbound messages")

        proxy.initiate_chat(agent, message=(
            f"Run the messaging pipeline for company='{slug}'."
        ))

    except ImportError:
        log.warning("pyautogen not installed — direct messaging pipeline")
        _direct_messaging(slug)
    except Exception as e:
        log.error(f"[{slug}] Messaging pipeline error: {e}")
        _direct_messaging(slug)


def _direct_messaging(slug: str):
    log.info(f"[{slug}] Direct messaging pipeline")
    for fn in [scrape_conversations, auto_reply_conversations]:
        result = fn(slug)
        log.info(f"  {fn.__name__}: {result[:120]}")
