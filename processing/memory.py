"""
processing/memory.py
--------------------
Lightweight, in-RAM conversation memory for a single VoxMed AI call.

Stores only what the LLM and orchestrator need:
  - detected intent
  - accumulated entities
  - last system question (so Stage 2 has context)
  - language code
  - last 3 conversation turns (trimmed automatically)

Deliberately avoids the database; persistence is handled separately
by database.save_conversation() in the orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Required entities per intent (copied from old nlp.py – pure data, no regex).
REQUIRED_ENTITIES: dict[str, list[str]] = {
    "book_appointment":       ["medical_need", "date", "time", "patient_name"],
    "cancel_appointment":     ["patient_name", "date"],
    "reschedule_appointment": ["patient_name", "current_date", "new_date", "new_time"],
    "check_availability":     ["date"],
    "general_inquiry":        [],
    "medicine_information":   [],
    "doctor_information":     [],
    "department_information": [],
    "hospital_timings":       [],
    "insurance_query":        [],
    "parking_query":          [],
    "cost_query":             [],
    "emergency":              [],
    "human_agent":            [],
    "general_chat":           [],
    "out_of_scope":           [],
    "affirm":                 [],
    "deny":                   [],
}

# Maximum turns to keep in memory to bound token usage.
_MAX_TURNS = 3


@dataclass
class ConversationMemory:
    """Holds the minimal state needed for one patient call."""

    intent:        str | None = None
    entities:      dict       = field(default_factory=dict)
    last_question: str | None = None
    language:      str        = "en"
    turns:         list[dict] = field(default_factory=list)

    # ── Mutation helpers ──────────────────────────────────────────────────────

    def add_turn(self, role: str, text: str) -> None:
        """Append a turn (role = 'user' | 'assistant') and trim to last _MAX_TURNS."""
        self.turns.append({"role": role, "content": text})
        if len(self.turns) > _MAX_TURNS:
            self.turns = self.turns[-_MAX_TURNS:]

    def update_entities(self, new: dict[str, Any]) -> None:
        """
        Merge values from *new* into self.entities.
        - Non-None values overwrite existing ones (Problem 3: merge, never replace).
        - Explicit None values delete the key (used to reset a slot after failed booking).
        - Keys absent from *new* are untouched.
        """
        for key, value in new.items():
            if value is None:
                self.entities.pop(key, None)   # intentional reset
            else:
                self.entities[key] = value

    # ── Query helpers ─────────────────────────────────────────────────────────

    def missing_entities(self, required: list[str] | None = None) -> list[str]:
        """
        Return the subset of *required* keys that are absent or None in self.entities.
        If *required* is None, uses REQUIRED_ENTITIES for self.intent.
        """
        if required is None:
            required = REQUIRED_ENTITIES.get(self.intent or "", [])
        return [k for k in required if not self.entities.get(k)]

    def to_context_snippet(self) -> list[dict]:
        """Return the last N turns as LLM message dicts (ready to embed in a prompt)."""
        return list(self.turns)

    # ── Convenience ───────────────────────────────────────────────────────────

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ConversationMemory(intent={self.intent!r}, "
            f"entities={self.entities}, "
            f"turns={len(self.turns)})"
        )
