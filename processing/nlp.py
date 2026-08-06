"""
processing/nlp.py
-----------------
Intent detection and entity extraction for VoxMed AI.

Architecture
------------
BaseNLPEngine      – abstract interface
RuleBasedEngine    – semantic pattern + regex engine, fully offline
analyse(text)      – module-level convenience function

Design principles
-----------------
- English-only pipeline (STT layer handles native translation to English)
- Patterns are phrase-level, not single keywords, so paraphrases match
- Each intent has an explicit priority so specific intents beat generic ones
- Date extraction finds ALL date mentions then picks the one tied to the action
- Patient name extraction only fires on explicit introduction phrases
- Confidence is computed from match strength, entity coverage, and conflicts
- follow_up_question returns ONE natural question for the first missing entity
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

# ── Intents ───────────────────────────────────────────────────────────────────

INTENTS = (
    "book_appointment",
    "cancel_appointment",
    "reschedule_appointment",
    "check_availability",
    "general_inquiry",
    "affirm",
    "deny",
)

# Higher number = higher priority when scores tie
_INTENT_PRIORITY: dict[str, int] = {
    "reschedule_appointment": 5,
    "cancel_appointment":     4,
    "check_availability":     3,
    "book_appointment":       2,
    "general_inquiry":        1,
    "affirm":                 1,
    "deny":                   1,
}

REQUIRED_ENTITIES: dict[str, list[str]] = {
    "book_appointment":       ["medical_need", "date", "time", "patient_name"],
    "cancel_appointment":     ["patient_name", "date"],
    "reschedule_appointment": ["date", "time", "patient_name"],
    "check_availability":     ["date"],
    "general_inquiry":        [],
    "affirm":                 [],
    "deny":                   [],
}

# Single follow-up question per missing entity (priority order matters)
_FOLLOW_UP: dict[str, str] = {
    "medical_need": "Could you tell me your symptoms, or which department or doctor you'd like to visit?",
    "date":         "What date would you prefer?",
    "time":         "What time works best for you?",
    "patient_name": "May I know the patient's name?",
}

# ── Symptom guard — words that must NOT be mistaken for patient names ─────────

_SYMPTOM_WORDS = {
    "feeling", "well", "sick", "fever", "pain", "cold", "cough", "tired",
    "weak", "dizzy", "nausea", "vomiting", "headache", "unwell", "ill",
    "hurt", "hurting", "sore", "ache", "aching", "disease", "infection", "not"
}

# ── Abstract interface ────────────────────────────────────────────────────────

class BaseNLPEngine(ABC):
    @abstractmethod
    def analyse(self, text: str) -> dict[str, Any]:
        """
        Returns:
        {
            "intent":            str,
            "confidence":        float,
            "entities":          dict,
            "missing_entities":  list[str],
            "follow_up_question": str | None,
        }
        """

# ── Rule-based engine ─────────────────────────────────────────────────────────

class RuleBasedEngine(BaseNLPEngine):

    # ── Weighted semantic pattern groups ──────────────────────────────────────
    # Each entry is a (regex_pattern, weight) tuple.
    # Weights reflect pattern specificity:
    #   3 = Definitive   – unambiguously signals this intent
    #   2 = Strong       – clear indicator but slightly less specific
    #   1 = Weak/Generic – could loosely match other intents
    # Confidence = matched_weight / max_possible_weight

    _PATTERNS: dict[str, list[tuple[str, int]]] = {

        "book_appointment": [
            (r"\bbook\b.*\b(appointment|consultation|session)\b",   3),  # definitive
            (r"\bschedule\b.*\b(appointment|consultation|session)\b", 3),  # definitive
            (r"\bmake\b.*\b(appointment|booking)\b",                  3),  # definitive
            (r"\bfix\b.*\bappointment\b",                             3),  # definitive
            (r"\bset up\b.*\bappointment\b",                          3),  # definitive
            (r"\barrange\b.*\bappointment\b",                         3),  # definitive
            (r"\bregister\b.*\bappointment\b",                        3),  # definitive
            (r"\bi('d| would) like an appointment\b",                 3),  # definitive
            (r"\bi need (to see|to consult|a doctor|an appointment)\b", 2),  # strong
            (r"\bi want to (see|meet|consult|visit) (dr\.?|doctor)\b", 2),  # strong
            (r"\bcan i see (dr\.?|doctor)\b",                         2),  # strong
            (r"\b(get|take)\b.*\bappointment\b",                      2),  # strong
            (r"\blooking for an appointment\b",                        2),  # strong
            (r"\bconsult with a doctor\b",                             2),  # strong
        ],

        "cancel_appointment": [
            (r"\bcancel\b.*\b(appointment|booking|slot|consultation)\b", 3),  # definitive
            (r"\b(appointment|booking)\b.*\bcancel\b",                   3),  # definitive
            (r"\bcall off\b.*\bappointment\b",                            3),  # definitive
            (r"\bcancel my visit\b",                                       3),  # definitive
            (r"\bdelete my booking\b",                                     3),  # definitive
            (r"\bwithdraw my appointment\b",                               3),  # definitive
            (r"\bremove\b.*\bappointment\b",                               2),  # strong
            (r"\bdrop\b.*\bappointment\b",                                 2),  # strong
            (r"\bdon't want the appointment\b",                            2),  # strong
            (r"\bi (won't|will not|wont) be (coming|there|able to make it)\b", 1),  # weak
            (r"\bnot coming\b",                                            1),  # weak
        ],

        "reschedule_appointment": [
            (r"\breschedule\b",                                                    3),  # definitive
            (r"\bdelay (my|the) appointment\b",                                    3),  # definitive
            (r"\b(change|shift|move|push|postpone|defer)\b.*(appointment|slot|booking|it|date|time)\b", 2),  # strong
            (r"\b(appointment|slot|booking|it)\b.*(change|shift|move|push|postpone)\b",               2),  # strong
            (r"\bunable to (come|make it|attend)\b",                               2),  # strong
            (r"\bwon'?t be able to make it\b",                                    2),  # strong
            (r"\banother (day|time|slot|date)\b",                                  1),  # weak
            (r"\bdifferent (day|time|slot|date)\b",                                1),  # weak
            (r"\bcan('t| not|not) make it\b",                                     1),  # weak
            (r"\bput it off\b",                                                    1),  # weak
            (r"\bmake it later\b",                                                 1),  # weak
            (r"\bmake it earlier\b",                                               1),  # weak
        ],

        "check_availability": [
            (r"\bwhen is (the next|a) (slot|appointment|opening)\b",      3),  # definitive
            (r"\bdo you have (any|an) (opening|slot|appointment)\b",      3),  # definitive
            (r"\bcan i get a slot\b",                                       3),  # definitive
            (r"\bwhat times are available\b",                              3),  # definitive
            (r"\b(any|is there a?|check)\b.*(slot|opening|appointment|availability)\b", 2),  # strong
            (r"\b(slot|opening|appointment)\b.*(available|open|free|there)\b",          2),  # strong
            (r"\bis (dr\.?|doctor)\b.*\bfree\b",                          2),  # strong
            (r"\bany (slots?|appointments?)\b",                            2),  # strong
            (r"\bwhen can i come\b",                                        1),  # weak
        ],

        "general_inquiry": [
            (r"\b(what|when|where|how|which|who)\b.*(clinic|hospital|doctor|fee|cost|hour|timing|address|location)\b", 2),  # strong
            (r"\b(fee|cost|price|charge)\b.*(consult|appointment|visit)\b",  2),  # strong
            (r"\b(timing|hours?|open|close)\b.*(clinic|hospital|doctor)\b",  2),  # strong
            (r"\bwhere (is|are)\b.*(clinic|hospital|doctor)\b",               2),  # strong
            (r"\bhow much does it cost\b",                                     2),  # strong
            (r"\btell me (about|the)\b",                                       1),  # weak
            (r"\bare you open\b",                                               1),  # weak
        ],

        "affirm": [
            (r"\byes\b", 2), (r"\byeah\b", 2), (r"\byep\b", 2), (r"\byup\b", 2),
            (r"\bcorrect\b", 2), (r"\bthat's right\b", 3), (r"\bexactly\b", 2),
            (r"\bsure\b", 1), (r"\bokay\b", 1), (r"\bok\b", 1),
            (r"\bgo ahead\b", 2), (r"\bdo it\b", 2), (r"\bplease do\b", 2),
        ],

        "deny": [
            (r"\bno\b", 2), (r"\bnope\b", 2), (r"\bwrong\b", 2), (r"\bincorrect\b", 2),
            (r"\bcancel that\b", 3), (r"\bstop\b", 1), (r"\bwait\b", 1),
            (r"\bnot\b", 1), (r"\bdon't\b", 1),
        ],
    }

    # ── Date vocabulary ───────────────────────────────────────────────────────

    _WEEKDAYS: dict[str, int] = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }

    _MONTHS: dict[str, int] = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9,
        "oct": 10, "nov": 11, "dec": 12,
    }

    # Action prepositions — a date following these belongs to the requested action
    _ACTION_PREPS = re.compile(
        r"\b(to|for|on|at|by|next)\b\s*(.{0,40})",
        re.IGNORECASE,
    )

    # ── Time vocabulary ───────────────────────────────────────────────────────

    _TIME_KEYWORDS: dict[str, str] = {
        "morning": "09:00", "afternoon": "14:00",
        "evening": "17:00", "night":     "20:00",
        "noon":    "12:00", "midnight":  "00:00",
    }

    # ── Specializations ───────────────────────────────────────────────────────

    _SPECIALIZATIONS: list[str] = [
        "cardiologist", "dermatologist", "neurologist", "orthopedic",
        "pediatrician", "gynecologist", "psychiatrist", "dentist",
        "ophthalmologist", "general physician", "surgeon", "urologist",
        "endocrinologist", "oncologist", "radiologist", "therapist",
    ]

    # ── Symptoms & Departments ──────────────────────────────────────────────

    _DEPARTMENTS: list[str] = [
        "cardiology", "general medicine", "neurology", "dermatology",
        "orthopedics", "pediatrics", "gynecology", "psychiatry"
    ]
    
    _SYMPTOMS_LIST: list[str] = [
        "fever", "cough", "chest pain", "heart palpitation", "headache",
        "stomach ache", "nausea", "vomiting", "dizzy", "weakness",
        "joint pain", "skin rash", "breathing issue"
    ]

    # ── Public API ────────────────────────────────────────────────────────────

    def analyse(self, text: str) -> dict[str, Any]:
        logger.info("NLP analysis started | text_length=%d", len(text))

        lower = text.strip().lower()

        intent, confidence = self._detect_intent(lower)
        entities           = self._extract_entities(text, lower)
        required           = REQUIRED_ENTITIES.get(intent, [])
        missing            = [k for k in required if not entities.get(k)]
        follow_up          = self._follow_up_question(missing)

        # Refine confidence using entity coverage
        confidence = self._adjust_confidence(confidence, intent, entities, missing)

        result: dict[str, Any] = {
            "intent":            intent,
            "confidence":        confidence,
            "entities":          entities,
            "missing_entities":  missing,
            "follow_up_question": follow_up,
        }

        logger.info(
            "NLP analysis completed | intent=%s | confidence=%.2f | missing=%s",
            intent, confidence, missing,
        )
        return result

    # ── Intent detection ──────────────────────────────────────────────────────

    def _detect_intent(self, lower: str) -> tuple[str, float]:
        """
        Weighted Regex Scoring:
        - Each matching pattern contributes its weight (3/2/1) to the intent score.
        - Ties are broken by _INTENT_PRIORITY so specific intents beat generic ones.
        - Confidence = matched_weight / max_possible_weight, penalised for ambiguity.
        """
        scores: dict[str, int] = {}

        for intent, patterns in self._PATTERNS.items():
            scores[intent] = sum(
                weight
                for pattern, weight in patterns
                if re.search(pattern, lower, re.IGNORECASE)
            )

        best_score = max(scores.values())

        if best_score == 0:
            return "general_inquiry", 0.4

        # Among intents with the best score, pick the highest-priority one
        candidates = [i for i, s in scores.items() if s == best_score]
        best_intent = max(candidates, key=lambda i: _INTENT_PRIORITY[i])

        # Confidence: matched weight / max possible weight for that intent
        max_possible_weight = sum(w for _, w in self._PATTERNS[best_intent])
        raw_confidence = best_score / max_possible_weight

        # Penalise when a competing intent also scored
        competing = [i for i, s in scores.items() if s > 0 and i != best_intent]
        penalty = 0.05 * len(competing)

        confidence = round(max(0.1, min(1.0, raw_confidence - penalty)), 4)
        return best_intent, confidence

    # ── Confidence adjustment ─────────────────────────────────────────────────

    def _adjust_confidence(
        self,
        base: float,
        intent: str,
        entities: dict,
        missing: list[str],
    ) -> float:
        """Boost confidence when entities are present; penalise when many are missing."""
        filled  = sum(1 for k in REQUIRED_ENTITIES.get(intent, []) if entities.get(k))
        total   = len(REQUIRED_ENTITIES.get(intent, [])) or 1
        boost   = 0.05 * filled
        penalty = 0.04 * len(missing)
        return round(max(0.1, min(1.0, base + boost - penalty)), 4)

    # ── Entity extraction ─────────────────────────────────────────────────────

    def _extract_entities(self, original: str, lower: str) -> dict[str, Any]:
        doctor = self._extract_doctor(original, lower)
        department = self._extract_department(lower)
        symptoms = self._extract_symptoms(lower)
        
        # medical_need is satisfied if any of these are present
        medical_need = bool(doctor or department or symptoms)
        
        return {
            "patient_name": self._extract_patient_name(original),
            "doctor":       doctor,
            "department":   department,
            "symptoms":     symptoms,
            "medical_need": medical_need,
            "date":         self._extract_date(lower),
            "time":         self._extract_time(lower),
        }

    def _extract_department(self, lower: str) -> str | None:
        for dept in self._DEPARTMENTS:
            if dept in lower:
                return dept.title()
        return None
        
    def _extract_symptoms(self, lower: str) -> list[str] | None:
        found = []
        for symp in self._SYMPTOMS_LIST:
            if symp in lower:
                found.append(symp)
        return found if found else None

    # ── Patient name ──────────────────────────────────────────────────────────

    def _extract_patient_name(self, original: str) -> str | None:
        """
        Only extract a name when the sentence explicitly introduces a person.
        Guards against symptom words being captured as names.
        """
        patterns = [
            # English — must follow an introduction phrase
            r"(?:my name is|i am|i'm|name is|patient(?:'s)? name is|patient is|for)\s+"
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            # Fallback for just first name if capitalized correctly
            r"(?:my name is|i am|i'm|name is)\s+([A-Z][a-z]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, original, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                # Reject if any word in the name is a symptom word
                if not any(w.lower() in _SYMPTOM_WORDS for w in name.split()):
                    return name
        return None

    # ── Doctor ────────────────────────────────────────────────────────────────

    def _extract_doctor(self, original: str, lower: str) -> str | None:
        # Named: "Dr. Sharma", "Dr Rao", "doctor Kapoor"
        m = re.search(r"\b(?:dr\.?|doctor)\s+([A-Za-z]+)", original, re.IGNORECASE)
        if m:
            return f"Dr. {m.group(1).capitalize()}"

        # Specialization fallback
        for spec in self._SPECIALIZATIONS:
            if spec in lower:
                return spec

        return None

    # ── Date ──────────────────────────────────────────────────────────────────

    def _extract_date(self, text: str) -> str | None:
        """
        Find all date mentions in the text, then return the one that is
        closest to an action preposition (to/for/on).
        This prevents "today" in "I can't come today, move it to tomorrow"
        from being returned instead of "tomorrow".
        """
        candidates: list[tuple[int, str]] = []  # (position, iso_date)

        today = date.today()

        # ── Collect all date mentions with their position ─────────────────────

        for m in re.finditer(r"\btoday\b", text):
            candidates.append((m.start(), today.isoformat()))

        for m in re.finditer(r"\btomorrow\b", text):
            candidates.append((m.start(), (today + timedelta(days=1)).isoformat()))

        for m in re.finditer(r"\bday after tomorrow\b", text):
            candidates.append((m.start(), (today + timedelta(days=2)).isoformat()))

        # "next <weekday>"
        for m in re.finditer(r"\bnext\s+(\w+)", text):
            day_name = m.group(1).lower()
            if day_name in self._WEEKDAYS:
                delta = (self._WEEKDAYS[day_name] - today.weekday() + 7) % 7 or 7
                candidates.append((m.start(), (today + timedelta(days=delta)).isoformat()))

        # Weekday names
        for day_name, day_num in self._WEEKDAYS.items():
            for m in re.finditer(r"\b" + re.escape(day_name) + r"\b", text):
                delta = (day_num - today.weekday() + 7) % 7 or 7
                candidates.append((m.start(), (today + timedelta(days=delta)).isoformat()))

        # "15th April 2026", "April 15"
        ordinal = r"(\d{1,2})(?:st|nd|rd|th)?"
        month_re = "|".join(self._MONTHS)

        for m in re.finditer(
            ordinal + r"\s+(?:of\s+)?(" + month_re + r")(?:\s+(\d{4}))?", text
        ):
            day   = int(m.group(1))
            month = self._MONTHS[m.group(2)]
            year  = int(m.group(3)) if m.group(3) else today.year
            d = _safe_date(year, month, day)
            if d:
                candidates.append((m.start(), d))

        for m in re.finditer(
            r"\b(" + month_re + r")\s+" + ordinal + r"(?:\s+(\d{4}))?", text
        ):
            month = self._MONTHS[m.group(1)]
            day   = int(m.group(2))
            year  = int(m.group(3)) if m.group(3) else today.year
            d = _safe_date(year, month, day)
            if d:
                candidates.append((m.start(), d))

        # DD/MM/YYYY
        for m in re.finditer(r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})", text):
            d = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if d:
                candidates.append((m.start(), d))

        # YYYY-MM-DD
        for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})", text):
            d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d:
                candidates.append((m.start(), d))

        if not candidates:
            return None

        if len(candidates) == 1:
            return candidates[0][1]

        # ── Pick the date closest (after) an action preposition ───────────────
        action_positions = [m.start() for m in self._ACTION_PREPS.finditer(text)]

        if action_positions:
            # For each candidate, find the nearest action prep that precedes it
            def _score(pos_date: tuple[int, str]) -> int:
                pos = pos_date[0]
                preceding = [ap for ap in action_positions if ap <= pos]
                if not preceding:
                    return 9999  # no preceding action prep → deprioritise
                return pos - max(preceding)

            candidates.sort(key=_score)
            return candidates[0][1]

        # Fallback: return the last date mentioned (most likely the target)
        return candidates[-1][1]

    # ── Time ──────────────────────────────────────────────────────────────────

    def _extract_time(self, text: str) -> str | None:
        for keyword, value in self._TIME_KEYWORDS.items():
            if keyword in text:
                return value

        # "3 PM", "3:30 PM", "3pm"
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.IGNORECASE)
        if m:
            hour     = int(m.group(1))
            minute   = int(m.group(2)) if m.group(2) else 0
            meridiem = m.group(3).lower()
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            return f"{hour:02d}:{minute:02d}"

        # 24-hour "14:30"
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
        if m:
            return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"

        return None

    # ── Follow-up question ────────────────────────────────────────────────────

    def _follow_up_question(self, missing: list[str]) -> str | None:
        """Return ONE question for the highest-priority missing entity."""
        for entity in _FOLLOW_UP:          # dict is insertion-ordered by priority
            if entity in missing:
                return _FOLLOW_UP[entity]
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


# ── Module-level singleton + convenience function ─────────────────────────────

_engine = RuleBasedEngine()


def analyse(text: str) -> dict[str, Any]:
    """Module-level convenience wrapper. Public API — do not rename."""
    return _engine.analyse(text)


def extract_date(text: str) -> str | None:
    """Extract only a date from text. Used by DialogueManager in GET_DATE state."""
    return _engine._extract_date(text.lower())


def extract_time(text: str) -> str | None:
    """Extract only a time from text. Used by DialogueManager in GET_TIME state."""
    return _engine._extract_time(text.lower())
