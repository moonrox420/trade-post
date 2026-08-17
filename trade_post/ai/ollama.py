"""Hardened Ollama client. The only AI provider. Falls back deterministically on failure."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from ..core.config import Settings
from ..core.errors import AICircuitOpen, AIProviderError, AIResponseInvalid
from .prompts import SYSTEM_TEMPLATE, USER_TEMPLATE

log = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown_sec: float) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_sec
        self._failures: list[float] = []
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            self._opened_at = None
            self._failures = []
            return False
        return True

    async def record_failure(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._failures = [t for t in self._failures if now - t < 60.0]
            self._failures.append(now)
            if len(self._failures) >= self._threshold:
                self._opened_at = now
                log.warning("AI circuit breaker OPEN after %d failures", len(self._failures))

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = []
            self._opened_at = None


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.ollama_max_concurrent)
        self._breaker = CircuitBreaker(
            settings.ollama_circuit_breaker_threshold,
            settings.ollama_circuit_breaker_cooldown_sec,
        )
        self._http: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.ollama_timeout_sec, connect=10.0),
            limits=httpx.Limits(max_connections=self._settings.ollama_max_concurrent * 2,
                                max_keepalive_connections=4),
            headers={"Content-Type": "application/json"},
        )

    async def disconnect(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def is_available(self) -> bool:
        if self._breaker.is_open or self._http is None:
            return False
        try:
            r = await self._http.get(self._settings.ollama_tags_url, timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        if self._http is None:
            await self.connect()
        assert self._http is not None
        r = await self._http.get(self._settings.ollama_tags_url)
        r.raise_for_status()
        data = r.json()
        return [m.get("name") for m in data.get("models", []) if m.get("name")]

    async def ensure_model(self, model: str) -> None:
        try:
            models = await self.list_models()
            if not any(m.startswith(model) for m in models):
                log.warning("Ollama model '%s' not found; available: %s", model, models)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not verify Ollama models: %s", exc)



    async def chat_json(
        self,
        prompt: str,
        *,
        model: str | None = None,
        extra_system: str | None = None,
    ) -> dict:
        """Send a chat request and return parsed JSON. Retries with backoff."""
        if self._breaker.is_open:
            raise AICircuitOpen("AI circuit breaker is open")
        if self._http is None:
            await self.connect()
        assert self._http is not None
        chosen_model = model or self._settings.ollama_model
        system_content = SYSTEM_TEMPLATE
        if extra_system:
            system_content += "\n\n" + extra_system
        payload = {
            "model": chosen_model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": prompt},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1},
        }
        headers: dict = {}
        if self._settings.ollama_api_key:
            headers["Authorization"] = f"Bearer {self._settings.ollama_api_key}"
        attempt = 0
        last_exc: Exception | None = None
        async with self._sem:
            while attempt <= self._settings.ollama_max_retries:
                try:
                    r = await self._http.post(
                        self._settings.ollama_chat_url, json=payload, headers=headers
                    )
                    if r.status_code == 404:
                        raise AIProviderError(f"Model '{chosen_model}' not found at Ollama")
                    r.raise_for_status()
                    data = r.json()
                    content = data.get("message", {}).get("content", "")
                    parsed = _extract_json(content)
                    if parsed is None:
                        raise AIResponseInvalid(
                            f"Could not extract JSON (first 200 chars): {content[:200]!r}"
                        )
                    await self._breaker.record_success()
                    return parsed
                except (httpx.HTTPError, AIProviderError, AIResponseInvalid) as exc:
                    last_exc = exc
                    attempt += 1
                    if attempt > self._settings.ollama_max_retries:
                        await self._breaker.record_failure()
                        raise
                    await asyncio.sleep(min(2 ** attempt, 8))
        if last_exc is not None:
            raise last_exc
        raise AIProviderError("AI chat failed for unknown reason")


def _extract_json(text: str):
    """Tolerate fenced code blocks and stray prose. Returns parsed dict or None."""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    if not cleaned.startswith("{"):
        first = cleaned.find("{")
        if first < 0:
            return None
        cleaned = cleaned[first:]
    end = cleaned.rfind("}")
    if end < 0:
        return None
    cleaned = cleaned[: end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def build_user_prompt(
    *,
    symbol: str,
    last_price: float,
    indicators: dict,
    spread_bps,
    equity: float,
    drawdown_pct: float,
    available_margin: float,
    max_position_pct: float,
    recent_evaluations: list,
) -> str:
    return USER_TEMPLATE.format(
        symbol=symbol,
        last_price=last_price,
        rsi=indicators.get("rsi", "n/a"),
        ema_fast=indicators.get("ema_fast", "n/a"),
        ema_slow=indicators.get("ema_slow", "n/a"),
        macd_h=(indicators.get("macd") or {}).get("histogram", "n/a"),
        bollinger=indicators.get("bollinger", "n/a"),
        atr=indicators.get("atr", "n/a"),
        vol=indicators.get("volatility", "n/a"),
        spread=spread_bps if spread_bps is not None else "n/a",
        equity=equity,
        dd_pct=round(drawdown_pct, 3),
        margin=available_margin,
        max_pct=max_position_pct,
        recent="\n".join(recent_evaluations) or "none",
    )
