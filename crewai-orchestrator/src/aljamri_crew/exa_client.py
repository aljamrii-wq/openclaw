from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


class ExaQuotaExceededError(RuntimeError):
    """Raised when the local monthly Exa hardcap is reached."""


@dataclass(slots=True)
class ExaSearchContext:
    summary: str
    source_urls: list[str]
    usage_count: int
    usage_limit: int

    @property
    def remaining(self) -> int:
        return max(self.usage_limit - self.usage_count, 0)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


class ExaUsageStore:
    def __init__(self, path: Path, monthly_limit: int) -> None:
        self._path = path
        self._monthly_limit = monthly_limit

    def _month_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"month": self._month_key(), "count": 0}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {"month": self._month_key(), "count": 0}
        if not isinstance(payload, dict):
            return {"month": self._month_key(), "count": 0}
        month = str(payload.get("month", self._month_key()))
        count_raw = payload.get("count", 0)
        try:
            count = int(count_raw)
        except (TypeError, ValueError):
            count = 0
        if month != self._month_key():
            return {"month": self._month_key(), "count": 0}
        return {"month": month, "count": max(count, 0)}

    def _write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

    def count(self) -> int:
        payload = self._read()
        return int(payload["count"])

    def limit(self) -> int:
        return self._monthly_limit

    def ensure_within_limit(self) -> None:
        count = self.count()
        if count >= self._monthly_limit:
            raise ExaQuotaExceededError(
                f"Exa hardcap reached ({count}/{self._monthly_limit}) for {self._month_key()}."
            )

    def mark_successful_request(self) -> int:
        payload = self._read()
        payload["count"] = int(payload["count"]) + 1
        self._write(payload)
        return int(payload["count"])


class ExaClient:
    def __init__(self) -> None:
        api_key = os.getenv("EXA_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("EXA_API_KEY is not set.")

        monthly_limit = _int_env("ALJAMRI_EXA_HARDCAP_REQUESTS", 1000)
        usage_file = Path(
            os.getenv("ALJAMRI_EXA_USAGE_FILE", ".runtime/exa_usage.json").strip()
            or ".runtime/exa_usage.json"
        )
        if not usage_file.is_absolute():
            usage_file = (Path.cwd() / usage_file).resolve()

        self._api_key = api_key
        self._search_url = "https://api.exa.ai/search"
        self._num_results = _int_env("ALJAMRI_EXA_NUM_RESULTS", 5)
        self._timeout_seconds = float(os.getenv("ALJAMRI_EXA_TIMEOUT_SECONDS", "20").strip() or 20)
        self._usage = ExaUsageStore(path=usage_file, monthly_limit=monthly_limit)

    @staticmethod
    def enabled() -> bool:
        return _truthy(os.getenv("ALJAMRI_EXA_ENABLED", "false")) and bool(
            os.getenv("EXA_API_KEY", "").strip()
        )

    def search(self, query: str) -> ExaSearchContext:
        self._usage.ensure_within_limit()

        response = httpx.post(
            self._search_url,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            json={
                "query": query,
                "numResults": self._num_results,
                "type": "auto",
                "contents": {"text": True},
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Exa API returned invalid payload.")
        results = payload.get("results")
        if not isinstance(results, list):
            results = []

        lines: list[str] = []
        source_urls: list[str] = []
        for idx, item in enumerate(results[: self._num_results], start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            title = str(item.get("title") or url or f"Result {idx}").strip()
            text = str(item.get("text", "")).strip().replace("\n", " ")
            if len(text) > 260:
                text = f"{text[:257]}..."

            if url:
                source_urls.append(url)

            if text:
                lines.append(f"{idx}. {title}: {text}")
            else:
                lines.append(f"{idx}. {title}")

        if not lines:
            lines.append("No relevant web results returned by Exa.")

        usage_count = self._usage.mark_successful_request()
        return ExaSearchContext(
            summary="\n".join(lines),
            source_urls=source_urls,
            usage_count=usage_count,
            usage_limit=self._usage.limit(),
        )
