from __future__ import annotations

import argparse
import json
import sys

if __package__ in {None, ""}:
    # Support running as: python src/aljamri_crew/main.py
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from aljamri_crew.orchestration import CrewRunInput, run_orchestration
else:
    from .orchestration import CrewRunInput, run_orchestration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aljamri-crew",
        description="Run Aljamri master/worker orchestration using CrewAI.",
    )
    parser.add_argument("--message", required=True, help="Incoming Telegram group message text.")
    parser.add_argument("--sender", default="group-user", help="Sender username or identity.")
    parser.add_argument("--chat-id", default=None, help="Telegram chat ID.")
    parser.add_argument("--thread-id", default=None, help="Telegram topic/thread ID.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run deterministic local routing without calling an LLM.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        output = run_orchestration(
            CrewRunInput(
                message=args.message,
                sender=args.sender,
                chat_id=args.chat_id,
                thread_id=args.thread_id,
            ),
            dry_run=args.dry_run,
        )
    except Exception as exc:  # pragma: no cover - CLI guard
        error_payload = {"status": "error", "error": str(exc)}
        print(json.dumps(error_payload), file=sys.stderr)
        return 1

    payload = output.to_dict()
    if args.pretty:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
