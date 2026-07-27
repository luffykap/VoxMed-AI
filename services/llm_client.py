"""
services/llm_client.py
----------------------
Single module that owns ALL LLM communication.

Supports a provider failover chain (configured in config.LLM_PROVIDER_CHAIN):
    Gemini → Groq → Cerebras → Mistral → OpenRouter

Public API
----------
understand(memory, user_text, detect_intent) -> dict
    Stage 1:
    - detect_intent=True  (first turn): returns {intent, entities, confidence}
    - detect_intent=False (later turns): returns {entities, intent_switch}
    - Returns {_api_unavailable: True} on any failure — no legacy fallback.

generate_reply(memory, backend_result, language) -> str
    Stage 2: returns one short IVR-style sentence for complex outcomes.
    Slot-filling questions use deterministic templates in orchestrator.py instead.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

import config
from processing.memory import ConversationMemory
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Legacy engine (kept for import compatibility, NOT used in hot path) ────────

def _get_legacy_engine():
    """Lazy-import the legacy regex engine — only used as last-resort offline fallback."""
    try:
        from processing.nlp_legacy import RuleBasedEngine
        return RuleBasedEngine()
    except ImportError:
        return None


_legacy_engine = None


# ── Prompt builders ───────────────────────────────────────────────────────────

# ── System prompt: first turn — detect intent + extract entities ───────────────
_SYSTEM_DETECT_INTENT = (
    "You are an NLP engine for a medical appointment booking system.\n"
    "This is the FIRST message. Detect the user's goal and extract any entities mentioned.\n\n"
    "Return EXACTLY this JSON:\n"
    '{{"intent": "<book_appointment|cancel_appointment|reschedule_appointment|check_availability|general_inquiry>",'
    ' "entities": {{}}, "confidence": 0.0}}\n\n'
    "Entity rules (include ONLY what is explicitly in this message):\n"
    "- patient_name: only if stated ('my name is X', 'for X')\n"
    "- doctor: 'Dr. Lastname' format\n"
    "- department: medical department name\n"
    "- symptoms: list of symptom strings\n"
    "- date: YYYY-MM-DD (today: {today})\n"
    "- time: HH:MM 24h\n"
    "Output raw JSON only. No markdown."
)

# ── System prompt: subsequent turns — extract new entities only ─────────────────
_SYSTEM_EXTRACT_ENTITIES = (
    "You are an entity extractor for a medical appointment booking system.\n"
    "Goal: {intent}\n"
    "Last question asked: \"{last_question}\"\n"
    "Already collected: {collected}\n"
    "Today: {today}\n\n"
    "Extract ONLY new information from the user's reply.\n"
    "Do NOT re-extract entities already in 'Already collected'.\n\n"
    "Return EXACTLY this JSON:\n"
    '{{"entities": {{}}, "intent_switch": false}}\n\n'
    "- Include only entity keys that are NEW or CHANGED in this message.\n"
    "- intent_switch=true ONLY if user explicitly abandons current goal ('cancel instead', 'forget it').\n"
    "- date: YYYY-MM-DD. time: HH:MM 24h. patient_name: only if explicitly stated.\n"
    "Output raw JSON only. No markdown."
)

# ── Stage 2 reply system prompt ────────────────────────────────────────────────
_REPLY_SYSTEM = """\
You are VoxMed AI, an appointment booking voice assistant (IVR-style).
Reply in {language}.
Rules — strictly enforced:
- Maximum 1 sentence, under 15 words.
- Professional, neutral, direct.
- No empathy, sympathy, apologies, or filler.
- Forbidden: "I understand", "I'm sorry", "Certainly", "Of course", "I hope",
  "Thank you for", "I see", "Great", "Sure", "Absolutely".
- No greetings after the first turn.
- State the outcome or ask one thing. Nothing else.\
"""


def _build_understand_messages(
    memory: ConversationMemory,
    user_text: str,
    detect_intent: bool = True,
) -> list[dict]:
    from datetime import date as _date
    today = _date.today().isoformat()

    if detect_intent:
        system = _SYSTEM_DETECT_INTENT.format(today=today)
    else:
        # Filter out None-reset slots so the prompt shows only real collected values
        collected = {k: v for k, v in memory.entities.items() if v is not None}
        system = _SYSTEM_EXTRACT_ENTITIES.format(
            intent=memory.intent or "unknown",
            last_question=memory.last_question or "none",
            collected=json.dumps(collected) if collected else "{}",
            today=today,
        )

    messages = [{"role": "system", "content": system}]
    messages.extend(memory.to_context_snippet())
    messages.append({"role": "user", "content": user_text})
    return messages


def _build_reply_messages(
    memory: ConversationMemory,
    backend_result: dict | None,
    language: str,
) -> list[dict]:
    """Build a compact message list for Stage 2 (complex outcomes only)."""
    system = _REPLY_SYSTEM.format(language=language)
    if backend_result:
        success = backend_result.get("success", False)
        msg     = backend_result.get("message", "")
        ctx     = f"Outcome: {'success' if success else 'failed'}. Relay in one direct sentence: {msg!r}"
    else:
        ctx = "Ask how else you can help."
    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": ctx},
    ]


# ── HTTP helpers ──────────────────────────────────────────────────────────────



def _call_provider(
    provider_name: str,
    provider_cfg: dict,
    messages: list[dict],
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """
    Make one synchronous call to a single LLM provider.
    Returns the assistant message content string.
    Raises httpx.HTTPStatusError on retryable HTTP errors.
    Raises httpx.TimeoutException / httpx.ConnectError on network issues.
    """
    headers = {
        "Authorization": f"Bearer {provider_cfg['api_key']}",
        "Content-Type":  "application/json",
    }
    # OpenRouter requires extra headers
    if provider_name == "openrouter":
        headers["HTTP-Referer"] = "https://voxmed.ai"
        headers["X-Title"] = "VoxMed AI"

    payload: dict[str, Any] = {
        "model":       provider_cfg["model"],
        "messages":    messages,
        "temperature": config.LLM_TEMPERATURE,
    }
    # Gemini thinking models count reasoning tokens against max_tokens,
    # leaving almost nothing for output. Omit the limit and let Gemini
    # use its own default so responses aren't truncated.
    if max_tokens and provider_name != "gemini":
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    t0 = time.perf_counter()
    with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        response = client.post(
            f"{provider_cfg['base_url']}/chat/completions",
            headers=headers,
            json=payload,
        )
    latency = (time.perf_counter() - t0) * 1000

    logger.debug(
        "LLM call | provider=%s | model=%s | json_mode=%s | status=%d | latency=%.0fms",
        provider_name, provider_cfg["model"], json_mode, response.status_code, latency,
    )

    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def _call_with_failover(
    messages: list[dict],
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """
    Try each provider in config.LLM_PROVIDER_CHAIN until one succeeds.
    Skips providers with no API key configured.
    Returns the assistant message content string.
    Raises the last exception if ALL providers fail.
    """
    last_exc: Exception | None = None

    for name in config.LLM_PROVIDER_CHAIN:
        provider_cfg = config.LLM_PROVIDERS.get(name)
        if not provider_cfg or not provider_cfg.get("api_key"):
            logger.debug("Skipping provider %s -- no API key configured", name)
            continue

        logger.info("Trying provider %s (model=%s)", name, provider_cfg.get("model"))

        try:
            result = _call_provider(name, provider_cfg, messages, json_mode, max_tokens)
            logger.info("LLM provider %s succeeded", name)
            return result

        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning(
                "Provider %s failed (%s: %s) -- trying next",
                name, type(exc).__name__, exc,
            )
            last_exc = exc
            continue

    # All providers exhausted
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No LLM providers configured with API keys")


# ── Public API ────────────────────────────────────────────────────────────────

def understand(
    memory: ConversationMemory,
    user_text: str,
    detect_intent: bool = True,
) -> dict:
    """
    Stage 1 – Understanding.

    detect_intent=True  (first turn):  returns {intent, entities, confidence}
    detect_intent=False (later turns): returns {entities, intent_switch}

    Returns {_api_unavailable: True} on ANY failure — never falls back to
    legacy NLP so the reasoning engine stays consistent (Problem 4).
    """
    # Check if at least one provider has an API key
    has_any_key = any(
        config.LLM_PROVIDERS.get(name, {}).get("api_key")
        for name in config.LLM_PROVIDER_CHAIN
    )
    if not has_any_key:
        logger.warning("No LLM API keys configured")
        return {"_api_unavailable": True}

    messages = _build_understand_messages(memory, user_text, detect_intent)

    try:
        raw = _call_with_failover(messages, json_mode=True, max_tokens=config.LLM_MAX_TOKENS_UNDERSTANDING)
        parsed = json.loads(raw)

        if detect_intent:
            result = {
                "intent":     parsed.get("intent", "general_inquiry"),
                "entities":   parsed.get("entities") or {},
                "confidence": float(parsed.get("confidence", 0.7)),
            }
        else:
            result = {
                "entities":      parsed.get("entities") or {},
                "intent_switch": bool(parsed.get("intent_switch", False)),
            }
        # Normalize common LLM entity aliases to our canonical schema
        entities = result.get("entities", {})

        aliases = {
            "appointment_date": "date",
            "booking_date": "date",
            "appointment_time": "time",
            "booking_time": "time",
            "name": "patient_name",
            "patient": "patient_name",
            "doctor_name": "doctor",
            "dept": "department",
        }

        for old_key, new_key in aliases.items():
            if old_key in entities and new_key not in entities:
                entities[new_key] = entities.pop(old_key)

        result["entities"] = entities

        logger.info(
            "LLM understand | mode=%s | new_keys=%s",
            "detect_intent" if detect_intent else "extract_entities",
            list(result.get("entities", {}).keys()),
        )
        return result

    except (httpx.HTTPError, httpx.TimeoutException, RuntimeError) as exc:
        logger.warning("All LLM providers unavailable: %s", exc)
        return {"_api_unavailable": True}
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("LLM malformed response: %s — raw=%r", exc, locals().get("raw", ""))
        return {"_api_unavailable": True}


def generate_reply(
    memory: ConversationMemory,
    backend_result: dict | None,
    language: str = "en",
) -> str:
    """
    Stage 2 – Response generation (complex outcomes only).

    Called for: booking success/fail, cancellation, general inquiry.
    NOT called for slot-filling questions (those use _SLOT_TEMPLATES).
    Falls back to a template if the API is unavailable.
    """
    has_any_key = any(
        config.LLM_PROVIDERS.get(name, {}).get("api_key")
        for name in config.LLM_PROVIDER_CHAIN
    )
    if not has_any_key:
        return _fallback_reply(backend_result)

    messages = _build_reply_messages(memory, backend_result, language)

    try:
        reply = _call_with_failover(messages, json_mode=False, max_tokens=config.LLM_MAX_TOKENS_RESPONSE).strip()
        logger.info("LLM generate_reply | length=%d chars", len(reply))
        return reply
    except (httpx.HTTPError, httpx.TimeoutException, KeyError, RuntimeError) as exc:
        logger.warning("LLM reply generation failed (%s) — using template", exc)
        return _fallback_reply(backend_result)


def _fallback_reply(backend_result: dict | None) -> str:
    """Minimal template when Stage 2 LLM call fails."""
    if backend_result:
        return backend_result.get("message", "Action completed.")
    return "How else can I help you?"
