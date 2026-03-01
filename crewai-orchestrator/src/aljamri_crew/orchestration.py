from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from json import JSONDecodeError
from typing import Any

from dotenv import load_dotenv

from .exa_client import ExaClient, ExaQuotaExceededError


PAYLO_BOT = "paylobot"
SKYHUB_BOT = "skyhubtravelbot"
MASTER_BOT = "aljamrigroupbot"

PAYLO_KEYWORDS = (
    "paylo",
    "transfer",
    "remittance",
    "send money",
    "recipient",
    "wallet",
    "kyc",
    "compliance",
    "rate",
)

SKYHUB_KEYWORDS = (
    "skyhub",
    "travel",
    "trip",
    "visa",
    "hotel",
    "flight",
    "booking",
    "itinerary",
    "tour",
)


@dataclass(slots=True)
class CrewRunInput:
    message: str
    sender: str = "group-user"
    chat_id: str | None = None
    thread_id: str | None = None


@dataclass(slots=True)
class CrewBotReply:
    bot: str
    text: str


@dataclass(slots=True)
class CrewRunOutput:
    master_bot: str = MASTER_BOT
    master_reply: str = ""
    replies: list[CrewBotReply] = field(default_factory=list)
    routing: dict[str, bool] = field(default_factory=dict)
    cross_improvement_notes: list[str] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["replies"] = [asdict(reply) for reply in self.replies]
        return result


def run_orchestration(payload: CrewRunInput, dry_run: bool = False) -> CrewRunOutput:
    load_dotenv()
    if dry_run:
        return _run_dry(payload)

    web_context = _maybe_fetch_web_context(payload.message)

    # Lazy import keeps --dry-run functional without external dependencies.
    from crewai import Agent, Crew, Process, Task

    manager_llm = _build_llm("ALJAMRI_MANAGER_MODEL", "gpt-4o-mini")
    paylo_llm = _build_llm("PAYLO_AGENT_MODEL", "gpt-4o-mini")
    skyhub_llm = _build_llm("SKYHUB_AGENT_MODEL", "gpt-4o-mini")

    manager = Agent(
        role="Aljamri Group Master Orchestrator",
        goal=(
            "Coordinate Paylo and Skyhub specialist agents in Telegram group workflows, "
            "enforce master policy, and keep all collaboration messages intact."
        ),
        backstory=(
            "You are the master of three company agents in AljamriGroup. "
            "You assign work to Paylo and Skyhub agents, ensure both can learn from each "
            "other, and make sure no one deletes or suppresses messages."
        ),
        allow_delegation=True,
        verbose=False,
        llm=manager_llm,
    )

    paylo = Agent(
        role="Paylo Financial Operations Agent",
        goal=(
            "Handle remittance, wallet, KYC, recipient, transfer, and compliance requests "
            "for Paylo while sharing reusable improvements with other agents."
        ),
        backstory=(
            "You are the Paylo specialist and must obey AljamriGroupBot master directives. "
            "You answer only with content relevant to Paylo and avoid travel-specific claims."
        ),
        allow_delegation=False,
        verbose=False,
        llm=paylo_llm,
    )

    skyhub = Agent(
        role="Skyhub Travel Operations Agent",
        goal=(
            "Handle Skyhub travel topics including flight, hotel, visa, itinerary, and "
            "booking support while sharing reusable improvements with other agents."
        ),
        backstory=(
            "You are the Skyhub specialist and must obey AljamriGroupBot master directives. "
            "You answer only with content relevant to travel and avoid fintech-specific claims."
        ),
        allow_delegation=False,
        verbose=False,
        llm=skyhub_llm,
    )

    route_task = Task(
        description=(
            "Incoming Telegram group message:\n"
            "sender={sender}\nchat_id={chat_id}\nthread_id={thread_id}\n"
            "message={message}\n"
            "web_context={web_context}\n\n"
            "Decide whether Paylo, Skyhub, or both should respond.\n"
            "Output ONLY JSON:\n"
            "{\n"
            '  "routing": {"paylo": true|false, "skyhub": true|false},\n'
            '  "master_reply": "short acknowledgement from master",\n'
            '  "instructions_for_paylo": "what Paylo should do",\n'
            '  "instructions_for_skyhub": "what Skyhub should do",\n'
            '  "cross_improvement_goal": "shared learning objective"\n'
            "}\n"
            "If web_context starts with WEB_CONTEXT:, use it for factual grounding.\n"
            "If web_context starts with WEB_CONTEXT_LIMIT: or WEB_CONTEXT_ERROR:, continue without web lookup.\n"
            "Rules: keep all messages, no deletion, no suppression."
        ),
        expected_output="Strict JSON routing plan for both worker agents.",
        agent=manager,
    )

    paylo_task = Task(
        description=(
            "Use the manager routing output and craft Paylo's reply.\n"
            "If routing.paylo is false, return exactly JSON: {\"paylo_reply\": \"SKIP\"}.\n"
            "If true, provide a concise operational reply for Paylo users.\n"
            "Output ONLY JSON: {\"paylo_reply\": \"...\"}."
        ),
        expected_output="JSON with paylo_reply.",
        context=[route_task],
        agent=paylo,
    )

    skyhub_task = Task(
        description=(
            "Use the manager routing output and craft Skyhub's reply.\n"
            "If routing.skyhub is false, return exactly JSON: {\"skyhub_reply\": \"SKIP\"}.\n"
            "If true, provide a concise travel support reply for Skyhub users.\n"
            "Output ONLY JSON: {\"skyhub_reply\": \"...\"}."
        ),
        expected_output="JSON with skyhub_reply.",
        context=[route_task],
        agent=skyhub,
    )

    merge_task = Task(
        description=(
            "Merge manager, Paylo, and Skyhub outputs into one final payload for Telegram dispatch.\n"
            "Output ONLY JSON and include every field below:\n"
            "{\n"
            '  "master_reply": "...",\n'
            '  "paylo_reply": "... or SKIP",\n'
            '  "skyhub_reply": "... or SKIP",\n'
            '  "routing": {"paylo": true|false, "skyhub": true|false},\n'
            '  "cross_improvement_notes": ["note1", "note2"]\n'
            "}\n"
            "Ensure cross_improvement_notes are practical function improvements shared between agents."
        ),
        expected_output="Final strict JSON output for dispatcher.",
        context=[route_task, paylo_task, skyhub_task],
        agent=manager,
    )

    crew = Crew(
        agents=[manager, paylo, skyhub],
        tasks=[route_task, paylo_task, skyhub_task, merge_task],
        process=Process.sequential,
        verbose=False,
    )

    result = crew.kickoff(
        inputs={
            "sender": payload.sender,
            "chat_id": payload.chat_id or "unknown",
            "thread_id": payload.thread_id or "none",
            "message": payload.message,
            "web_context": web_context,
        }
    )
    raw = _result_to_text(result)
    parsed = _extract_json(raw)
    return _parsed_to_output(parsed, raw=raw)


def _run_dry(payload: CrewRunInput) -> CrewRunOutput:
    lowered = payload.message.lower()
    paylo = any(keyword in lowered for keyword in PAYLO_KEYWORDS)
    skyhub = any(keyword in lowered for keyword in SKYHUB_KEYWORDS)

    if not paylo and not skyhub:
        paylo = True
        skyhub = True

    replies: list[CrewBotReply] = []
    if paylo:
        replies.append(
            CrewBotReply(
                bot=PAYLO_BOT,
                text=(
                    "Paylo agent online. I can handle transfers, recipients, KYC, and rate "
                    "checks. Share any Paylo ticket and I will process it."
                ),
            )
        )
    if skyhub:
        replies.append(
            CrewBotReply(
                bot=SKYHUB_BOT,
                text=(
                    "Skyhub agent online. I can handle travel requests including flights, "
                    "hotels, visa support, and itinerary updates."
                ),
            )
        )

    return CrewRunOutput(
        master_reply=(
            "AljamriGroupBot coordinating all agents. Keep communication in-group and "
            "preserve every message for shared learning."
        ),
        replies=replies,
        routing={"paylo": paylo, "skyhub": skyhub},
        cross_improvement_notes=[
            "Share successful response templates between Paylo and Skyhub.",
            "Log unresolved cases for master review and workflow updates.",
        ],
        raw=json.dumps({"mode": "dry-run", "message": payload.message}),
    )


def _result_to_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    raw = getattr(result, "raw", None)
    if isinstance(raw, str) and raw.strip():
        return raw
    return str(result)


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate.removeprefix("json").strip()
    try:
        loaded = json.loads(candidate)
        if isinstance(loaded, dict):
            return loaded
    except JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            loaded, _ = decoder.raw_decode(text[index:])
        except JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded

    raise ValueError(f"Could not parse JSON from Crew output: {text}")


def _parsed_to_output(parsed: dict[str, Any], raw: str) -> CrewRunOutput:
    routing_data = parsed.get("routing") if isinstance(parsed.get("routing"), dict) else {}
    routing = {
        "paylo": bool(routing_data.get("paylo")),
        "skyhub": bool(routing_data.get("skyhub")),
    }

    replies: list[CrewBotReply] = []
    paylo_reply = str(parsed.get("paylo_reply", "")).strip()
    skyhub_reply = str(parsed.get("skyhub_reply", "")).strip()

    if paylo_reply and paylo_reply.upper() != "SKIP":
        replies.append(CrewBotReply(bot=PAYLO_BOT, text=paylo_reply))
    if skyhub_reply and skyhub_reply.upper() != "SKIP":
        replies.append(CrewBotReply(bot=SKYHUB_BOT, text=skyhub_reply))

    notes_raw = parsed.get("cross_improvement_notes")
    if isinstance(notes_raw, list):
        notes = [str(item) for item in notes_raw if str(item).strip()]
    else:
        notes = []

    return CrewRunOutput(
        master_reply=str(parsed.get("master_reply", "")).strip(),
        replies=replies,
        routing=routing,
        cross_improvement_notes=notes,
        raw=raw,
    )


def _build_llm(model_env: str, fallback_model: str) -> Any:
    # CrewAI accepts either model strings or an LLM object depending on runtime version.
    model = (
        os.getenv(model_env)
        or os.getenv("ALJAMRI_CREW_MODEL")
        or os.getenv("OPENAI_MODEL")
        or fallback_model
    )
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")

    try:
        from crewai import LLM
    except Exception:
        return model

    kwargs: dict[str, Any] = {"model": model}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    try:
        return LLM(**kwargs)
    except Exception:
        return model


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_fetch_web_context(message: str) -> bool:
    lowered = message.lower()
    trigger_terms = (
        "latest",
        "today",
        "recent",
        "news",
        "update",
        "current",
        "price",
        "rate",
        "trend",
        "research",
        "source",
        "citation",
    )
    return any(term in lowered for term in trigger_terms)


def _maybe_fetch_web_context(message: str) -> str:
    if not ExaClient.enabled():
        return "WEB_CONTEXT: not enabled."
    if not _truthy(os.getenv("ALJAMRI_EXA_AUTO_SEARCH", "true")):
        return "WEB_CONTEXT: auto-search disabled."
    if not _should_fetch_web_context(message):
        return "WEB_CONTEXT: not requested by message context."

    try:
        exa_client = ExaClient()
        result = exa_client.search(message)
    except ExaQuotaExceededError as exc:
        return f"WEB_CONTEXT_LIMIT: {exc}"
    except Exception as exc:
        return f"WEB_CONTEXT_ERROR: {exc}"

    source_domains: list[str] = []
    for url in result.source_urls:
        domain = url.split("://", 1)[-1].split("/", 1)[0].strip()
        if domain and domain not in source_domains:
            source_domains.append(domain)
        if len(source_domains) >= 5:
            break

    domains = ", ".join(source_domains) if source_domains else "n/a"
    return (
        "WEB_CONTEXT:\n"
        f"{result.summary}\n"
        f"Exa usage: {result.usage_count}/{result.usage_limit} this month.\n"
        f"Source domains: {domains}"
    )
