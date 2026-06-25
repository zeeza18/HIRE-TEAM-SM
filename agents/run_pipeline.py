"""
Standalone pipeline runner — one company, one pipeline, one process.
Called by runner.py so each company gets isolated globals (no race conditions).

Usage:
    python agents/run_pipeline.py --slug rms --pipeline candidate
    python agents/run_pipeline.py --slug sm  --pipeline messaging --new-only
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug",      required=True, choices=["rms", "sm"])
    parser.add_argument("--pipeline",  required=True, choices=["candidate", "messaging"])
    parser.add_argument("--new-only",  action="store_true", dest="new_only")
    parser.add_argument("--threshold", type=int, default=80)
    args = parser.parse_args()

    if args.pipeline == "candidate":
        from agents.pipeline import run_candidate_pipeline
        run_candidate_pipeline(args.slug, threshold=args.threshold, new_only=args.new_only)
    else:
        from agents.pipeline import run_messaging_pipeline
        run_messaging_pipeline(args.slug)


if __name__ == "__main__":
    main()
