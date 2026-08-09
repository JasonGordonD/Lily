"""One configurable-filter policy for every remaining Gemini call.

PROHIBITED_CONTENT/SPII are provider-controlled and handled separately.
These settings remove automated blocking for the four configurable text
harm categories on every Gemini lane while the Grok migration proceeds.
"""

from __future__ import annotations

from google.genai import types as genai_types


_CATEGORIES = (
    genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
    genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
    genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
    genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
)


def lily_gemini_safety_settings() -> list[genai_types.SafetySetting]:
    return [
        genai_types.SafetySetting(
            category=category,
            threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
        )
        for category in _CATEGORIES
    ]


def lily_gemini_safety_dicts() -> list[dict[str, str]]:
    """LiveKit Google plugin uses JSON-shaped settings, not SDK objects."""
    return [
        {
            "category": category.name,
            "threshold": "BLOCK_NONE",
        }
        for category in _CATEGORIES
    ]

