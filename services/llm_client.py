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

import re

logger = get_logger(__name__)


def _extract_json(raw: str) -> dict:
    """Extract a JSON object from a raw string, tolerating markdown code fences."""
    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    stripped = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    # Try direct parse first
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Find first {...} block in the string (handles models that add prose)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise json.JSONDecodeError("No JSON object found", raw, 0)


def _get_legacy_engine():
    """Lazy-import the legacy regex engine — only used as last-resort offline fallback."""
    try:
        from processing.nlp_legacy import RuleBasedEngine
        return RuleBasedEngine()
    except ImportError:
        return None


_legacy_engine = None

# Tracks when a provider is allowed to be used again (Unix timestamp)
# Used to temporarily skip rate-limited providers.
_provider_cooldowns: dict[str, float] = {}
COOLDOWN_SECONDS = 60.0

# ── Prompt builders ───────────────────────────────────────────────────────────

# ── System prompt: first turn — detect intent + extract entities ───────────────
_SYSTEM_DETECT_INTENT = (
    "You are an NLP engine for a medical appointment booking system.\n"
    "This is the FIRST message. Detect the user's goal and extract any entities mentioned.\n"
    "The user may speak in Hindi, Telugu, Kannada, or English. Always extract entity values in English (dates as YYYY-MM-DD, names transliterated to English).\n\n"
    "Return EXACTLY this JSON:\n"
    '{{"intent": "<book_appointment|cancel_appointment|reschedule_appointment|check_availability|medicine_information|doctor_information|department_information|hospital_timings|insurance_query|parking_query|cost_query|emergency|human_agent|general_chat|out_of_scope>",'
    ' "entities": {{}}, "confidence": 0.0}}\n\n'
    "Entity rules (include ONLY what is explicitly in this message):\n"
    "- patient_name: only if stated ('my name is X', 'for X')\n"
    "- doctor: 'Dr. Lastname' format\n"
    "- department: medical department name\n"
    "- symptoms: list of symptom strings\n"
    "- date: YYYY-MM-DD. If user says 'anytime', 'whenever', or 'any day', extract 'ANY'.\n"
    "  IMPORTANT: explicitly resolve relative words like 'tomorrow', 'next week', 'monday' into YYYY-MM-DD using Today ({today}) as the reference. Ignore past dates.\n"
    "- time: HH:MM 24h. If user says 'anytime', 'whenever', or 'any time', extract 'ANY'.\n"
    "- For reschedule_appointment: use 'current_date' for the existing appointment date, "
    "'new_date' for the desired new date, 'new_time' for the desired new time (all YYYY-MM-DD / HH:MM).\n"
    "Output raw JSON only. No markdown."
)

# ── System prompt: subsequent turns — extract new entities only ─────────────────
_SYSTEM_EXTRACT_ENTITIES = (
    "You are an entity extractor for a medical appointment booking system.\n"
    "Goal: {intent}\n"
    "Last question asked: \"{last_question}\"\n"
    "Already collected: {collected}\n"
    "Today: {today}\n"
    "The user may speak in Hindi, Telugu, Kannada, or English. Always extract entity values in English (dates as YYYY-MM-DD, names transliterated to English).\n\n"
    "Extract ONLY new information from the user's reply.\n"
    "Do NOT re-extract entities already in 'Already collected'.\n\n"
    "Return EXACTLY this JSON:\n"
    '{{"entities": {{}}, "intent_switch": false, "side_query": null}}\n\n'
    "- side_query: If the user asks a question instead of answering (e.g. 'what slots are free today?'), extract the core intent here (e.g., 'check_availability').\n"
    "- Include only entity keys that are NEW or CHANGED in this message.\n"
    "- intent_switch=true ONLY if user explicitly abandons current goal ('cancel instead', 'forget it').\n"
    "- date: YYYY-MM-DD. time: HH:MM 24h. If user says 'anytime' or 'whichever', extract 'ANY'. If they ask about general dates, extract 'ANY' for date.\n"
    "  IMPORTANT: explicitly resolve relative words like 'tomorrow', 'next week', 'monday' into YYYY-MM-DD using Today ({today}) as the reference. Ignore past dates.\n"
    "- For reschedule_appointment: use 'current_date' for the existing appointment date, "
    "'new_date' for the desired new date, 'new_time' for the desired new time.\n"
    "- patient_name: only if explicitly stated.\n"
    "- IMPORTANT: If the assistant previously offered alternative slots (e.g. '09:00 with Dr. Sharma') and the user accepts one (e.g. 'Dr. Sharma is okay'), you MUST extract the implied 'time' (09:00) and 'date' from that context.\n"
    "Output raw JSON only. No markdown."
)

# ── Stage 2 reply system prompt ────────────────────────────────────────────────
_REPLY_SYSTEM = """\
You are VoxMed AI, an appointment booking voice assistant (IVR-style).
You MUST reply strictly in {language}. Output zero English words unless {language} is English. Translate any system instructions naturally into {language}.
Rules — strictly enforced:
- Maximum 2 sentences, under 25 words.
- Professional, neutral, direct.
- No empathy, sympathy, apologies, or filler.
- Forbidden: "I understand", "I'm sorry", "Certainly", "Of course", "I hope",
  "Thank you for", "I see", "Great", "Sure", "Absolutely".
- No greetings after the first turn.
- IMPORTANT: If a requested slot or date is unavailable, you MUST explicitly state that it is unavailable first, before asking the user to choose an alternative.\
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
    return messages


def _build_reply_messages(
    memory: ConversationMemory,
    backend_result: dict | None,
    language: str,
) -> list[dict]:
    """Build a compact message list for Stage 2 (complex outcomes only)."""
    system = _REPLY_SYSTEM.format(language=language)
    if backend_result:
        if backend_result.get("is_qa"):
            user_question = memory.turns[-1]["content"] if memory.turns else ""
            facts = backend_result.get("facts", "")
            ctx = (
                f"The user asked: {user_question!r}\n"
                f"Answer their question in one direct sentence using ONLY these facts: {facts}"
            )
        else:
            msg = backend_result.get("message", "")
            ctx = f"Relay this information to the user in one direct sentence: {msg!r}"
    else:
        ctx = "Ask how else you can help."
    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": ctx},
    ]


# ── HTTP helpers ──────────────────────────────────────────────────────────────



async def _call_provider(
    provider_name: str,
    provider_cfg: dict,
    messages: list[dict],
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """
    Make one asynchronous call to a single LLM provider.
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
    if json_mode and provider_cfg.get("json_mode", True):
        payload["response_format"] = {"type": "json_object"}

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as client:
        response = await client.post(
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


async def _call_with_failover(
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

        if time.time() < _provider_cooldowns.get(name, 0):
            logger.debug("Skipping provider %s -- currently on cooldown", name)
            continue

        logger.info("Trying provider %s (model=%s)", name, provider_cfg.get("model"))

        try:
            result = await _call_provider(name, provider_cfg, messages, json_mode, max_tokens)
            logger.info("LLM provider %s succeeded", name)
            # Clear cooldown on success, just in case
            _provider_cooldowns.pop(name, None)
            return result

        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as exc:
            # If rate limited, apply cooldown
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                logger.warning(
                    "Provider %s rate-limited (429) -- placing on %ds cooldown",
                    name, COOLDOWN_SECONDS
                )
                _provider_cooldowns[name] = time.time() + COOLDOWN_SECONDS
            else:
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

async def understand(
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
        raw = await _call_with_failover(messages, json_mode=True, max_tokens=config.LLM_MAX_TOKENS_UNDERSTANDING)
        parsed = _extract_json(raw)

        if detect_intent:
            result = {
                "intent":     parsed.get("intent", "out_of_scope"),
                "entities":   parsed.get("entities") or {},
                "confidence": float(parsed.get("confidence", 0.7)),
            }
        else:
            result = {
                "entities":      parsed.get("entities") or {},
                "intent_switch": bool(parsed.get("intent_switch", False)),
                "side_query":    parsed.get("side_query"),
            }
        # Normalize common LLM entity aliases to our canonical schema
        entities = result.get("entities", {})
        if not isinstance(entities, dict):
            logger.warning("LLM returned non-dict entities: %s", entities)
            entities = {}
            
        aliases = {
            "appointment_date": "date",
            "booking_date": "date",
            "appointment_time": "time",
            "booking_time": "time",
            "name": "patient_name",
            "patient": "patient_name",
            "doctor_name": "doctor",
            "dept": "department",
            "symptom": "symptoms",
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


async def generate_reply(
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
        reply = (await _call_with_failover(messages, json_mode=False, max_tokens=config.LLM_MAX_TOKENS_RESPONSE)).strip()
        if not reply:  # guard: some models return empty content strings
            logger.warning("LLM generate_reply returned empty string — using template fallback")
            return _fallback_reply(backend_result)
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

async def infer_department(symptoms: list[str]) -> str:
    """
    Auto-learning feature: Infer the best medical department from a list of unknown symptoms.
    Uses the LLM to classify.
    """
    has_any_key = any(
        config.LLM_PROVIDERS.get(name, {}).get("api_key")
        for name in config.LLM_PROVIDER_CHAIN
    )
    if not has_any_key:
        logger.warning("No LLM API keys for infer_department, defaulting to %s.", config.DEFAULT_DEPARTMENT)
        return config.DEFAULT_DEPARTMENT
        
    symptoms_str = ", ".join(symptoms)
    
    departments = [
        "General Medicine", "Cardiology", "Dermatology", "Neurology",
        "Orthopedics", "ENT", "Ophthalmology", "Pediatrics",
        "Gynecology", "Psychiatry", "Gastroenterology"
    ]
    
    system_prompt = (
        "You are a medical AI assistant classifying symptoms to medical departments.\n"
        "Given the symptoms, return ONLY the exact name of the most appropriate department from this list:\n"
        f"{departments}\n\n"
        f"If you are unsure, return '{config.DEFAULT_DEPARTMENT}'.\n"
        "Do not include any other text or formatting."
    )
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Symptoms: {symptoms_str}"}
    ]
    
    try:
        reply = (await _call_with_failover(messages, json_mode=False, max_tokens=15)).strip()
        logger.info("LLM infer_department inferred: %s", reply)
        for d in departments:
            if d.lower() in reply.lower():
                return d
        return config.DEFAULT_DEPARTMENT
    except Exception as exc:
        logger.warning("LLM infer_department failed (%s) — defaulting", exc)
        return config.DEFAULT_DEPARTMENT
