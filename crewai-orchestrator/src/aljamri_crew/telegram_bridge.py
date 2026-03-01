from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

if __package__ in {None, ""}:
    # Support running as: python src/aljamri_crew/telegram_bridge.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from aljamri_crew.orchestration import (
        MASTER_BOT,
        PAYLO_BOT,
        SKYHUB_BOT,
        CrewRunInput,
        run_orchestration,
    )
else:
    from .orchestration import (
        MASTER_BOT,
        PAYLO_BOT,
        SKYHUB_BOT,
        CrewRunInput,
        run_orchestration,
    )

TOKEN_ENV_BY_BOT = {
    MASTER_BOT: "ALJAMRI_MASTER_BOT_TOKEN",
    PAYLO_BOT: "ALJAMRI_PAYLO_BOT_TOKEN",
    SKYHUB_BOT: "ALJAMRI_SKYHUB_BOT_TOKEN",
}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_allowed_chat_ids(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


class TelegramClient:
    def __init__(self, token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._http = httpx.Client(timeout=35.0)

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        response = self._http.post(f"{self._base_url}/{method}", json=payload or {})
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            description = data.get("description", "Unknown Telegram API error")
            raise RuntimeError(f"Telegram API {method} failed: {description}")
        return data.get("result")

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = self.request(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message"],
            },
        )
        if isinstance(result, list):
            return result
        return []

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        thread_id: int | None,
        reply_to_message_id: int | None,
        inline_buttons: bool,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if inline_buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": "Ask Paylo", "callback_data": "route:paylo"},
                        {"text": "Ask Skyhub", "callback_data": "route:skyhub"},
                        {"text": "Ask Both", "callback_data": "route:both"},
                    ]
                ]
            }
        self.request("sendMessage", payload)


def _sender_label(sender: dict[str, Any]) -> str:
    username = sender.get("username")
    if isinstance(username, str) and username.strip():
        return f"@{username.strip()}"
    first_name = str(sender.get("first_name", "")).strip()
    last_name = str(sender.get("last_name", "")).strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if full_name:
        return full_name
    return str(sender.get("id", "group-user"))


def _read_state(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return int(payload.get("offset", 0))


def _write_state(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aljamri-telegram-bridge",
        description="Poll Telegram group messages and dispatch CrewAI-managed bot replies.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use deterministic routing without LLM calls.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=25,
        help="Long polling timeout in seconds (default: 25).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Sleep between poll cycles in seconds (default: 0.5).",
    )
    parser.add_argument(
        "--state-file",
        default=".runtime/telegram_bridge_state.json",
        help="Offset checkpoint file.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one poll cycle and exit.",
    )
    return parser


def _validate_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    missing: list[str] = []
    for bot, env_name in TOKEN_ENV_BY_BOT.items():
        token = os.getenv(env_name, "").strip()
        if not token:
            missing.append(env_name)
            continue
        tokens[bot] = token
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
    return tokens


def _process_update(
    update: dict[str, Any],
    *,
    clients: dict[str, TelegramClient],
    allowed_chat_ids: set[str],
    process_bot_messages: bool,
    force_inline_buttons: bool,
    dry_run: bool,
) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return

    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_type = str(chat.get("type", ""))
    if chat_type not in {"group", "supergroup"}:
        return

    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return
    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        return

    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    if sender.get("is_bot") and not process_bot_messages:
        return

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return

    thread_id = message.get("message_thread_id")
    if not isinstance(thread_id, int):
        thread_id = None

    reply_to_message_id = message.get("message_id")
    if not isinstance(reply_to_message_id, int):
        reply_to_message_id = None

    sender_label = _sender_label(sender)
    orchestrated = run_orchestration(
        CrewRunInput(
            message=text.strip(),
            sender=sender_label,
            chat_id=chat_id,
            thread_id=str(thread_id) if thread_id is not None else None,
        ),
        dry_run=dry_run,
    )

    if orchestrated.master_reply:
        clients[MASTER_BOT].send_message(
            chat_id=chat_id,
            text=orchestrated.master_reply,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            inline_buttons=force_inline_buttons,
        )

    for reply in orchestrated.replies:
        client = clients.get(reply.bot)
        if client is None:
            continue
        client.send_message(
            chat_id=chat_id,
            text=reply.text,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            inline_buttons=force_inline_buttons,
        )


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    state_file = Path(args.state_file)
    dry_run = args.dry_run or _truthy(os.getenv("ALJAMRI_CREW_DRY_RUN"))
    process_bot_messages = _truthy(os.getenv("ALJAMRI_PROCESS_BOT_MESSAGES"))
    force_inline_buttons = _truthy(os.getenv("ALJAMRI_FORCE_INLINE_BUTTONS"))
    allowed_chat_ids = _parse_allowed_chat_ids(os.getenv("ALJAMRI_ALLOWED_CHAT_IDS"))

    tokens = _validate_tokens()
    clients = {bot: TelegramClient(token) for bot, token in tokens.items()}

    offset = _read_state(state_file)

    try:
        while True:
            updates = clients[MASTER_BOT].get_updates(offset=offset, timeout=args.poll_timeout)
            for update in updates:
                update_id = int(update.get("update_id", offset))
                offset = max(offset, update_id + 1)
                _write_state(state_file, offset)
                try:
                    _process_update(
                        update,
                        clients=clients,
                        allowed_chat_ids=allowed_chat_ids,
                        process_bot_messages=process_bot_messages,
                        force_inline_buttons=force_inline_buttons,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    print(
                        f"[aljamri-telegram-bridge] update {update_id} failed: {exc}",
                        flush=True,
                    )

            if args.once:
                break
            time.sleep(args.poll_interval)
    finally:
        for client in clients.values():
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
