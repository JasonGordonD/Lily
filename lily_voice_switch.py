"""
lily_voice_switch.py — Runtime voice-preset switching.

Port of Zuna's voice_switch_tool (WO-ZUNA-VOICE-SWITCH-TOOL-001) to the
Lily agent, trimmed to Lily's two presets:

  - `voice1` → lily_config.lily_voice_1()  (locked primary/default —
               hardcoded LILY_VOICE_1_DEFAULT, overridable via the
               LILY_VOICE_1 env var; always populated)
  - `voice2` → lily_config.lily_voice_2()  (Raven's voice, the former
               default: LILY_VOICE_ID with RAVEN_VOICE_ID fallback)

Exposes two `function_tool`s:

  - `lily_list_voices`  — reports which presets are configured and which
                          is currently active on the live TTS instance.
  - `lily_switch_voice` — swaps the active preset by mutating
                          `LilyTTS._opts.voice_id` on the live TTS
                          instance. Subsequent turns re-target the new
                          voice on the next `synthesize()` call.

Design contract (unchanged from the Zuna original):
  - Reads the live TTS off `context.session` — the current agent's `.tts`
    property, with the session-level TTS as fallback (Lily wires a single
    session-level LilyTTS in the entrypoint; there are no per-node
    overrides).
  - Tool discovery is docstring/description-only — no prompt-file edits.
  - Failure semantics — every path returns a plain string. Never raises.
    * Unconfigured preset  → "voice2 is not configured …"
    * TTS not a LilyTTS    → "voice switching unavailable …"
    * ValueError from set_voice → caught + surfaced as string
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from livekit.agents import RunContext, function_tool

import lily_config
from lily_tts import LilyTTS

logger = logging.getLogger("lily_voice_switch")


_PRESET_ENV_NAME = {
    "voice1": "LILY_VOICE_1",
    "voice2": "LILY_VOICE_ID",
}

_PRESET_LABEL = {
    "voice1": "voice1 (primary)",
    "voice2": "voice2",
}


def _preset_values() -> dict[str, str | None]:
    """Snapshot the preset voice IDs at call time.

    Reads via the lily_config accessors (lazy env reads), matching how
    every other module in this package sees env state. voice1 is always
    populated — lily_voice_1() falls back to the hardcoded default.
    """
    return {
        "voice1": lily_config.lily_voice_1() or None,
        "voice2": lily_config.lily_voice_2() or None,
    }


def _current_tts(context: RunContext) -> LilyTTS | None:
    """Reach the LilyTTS instance active on the current session.

    `Agent.tts` returns whichever TTS is live on the current agent;
    Lily pins a single session-level LilyTTS in the entrypoint, so the
    session's own `.tts` is checked as a fallback. Mutating that
    instance's `_opts.voice_id` re-targets the NEXT stream request.
    """
    session = getattr(context, "session", None)
    if session is None:
        return None
    current_agent = (
        getattr(session, "current_agent", None)
        or getattr(session, "agent", None)
    )
    candidates: list[Any] = []
    if current_agent is not None:
        candidates.append(getattr(current_agent, "tts", None))
    candidates.append(getattr(session, "tts", None))
    for tts_instance in candidates:
        if isinstance(tts_instance, LilyTTS):
            return tts_instance
    return None


def _active_preset_key(tts_instance: LilyTTS) -> str | None:
    """Reverse-lookup which preset key the live voice_id corresponds to.

    Returns None when the live voice_id doesn't match any configured
    preset (e.g. after some other module mutated the instance outside
    this tool). Never raises.
    """
    live_id = getattr(getattr(tts_instance, "_opts", None), "voice_id", None)
    if not live_id:
        return None
    for key, val in _preset_values().items():
        if val and val == live_id:
            return key
    return None


@function_tool(
    name="lily_list_voices",
    description=(
        "List Lily's configured voice presets and report which one is "
        "currently active. Use this whenever a player asks 'which voices "
        "do you have', 'showcase your voices', 'what voice options', or "
        "'which voice are you using'. Returns a plain-English summary "
        "naming voice1 and voice2 with their status."
    ),
)
async def lily_list_voices(context: RunContext) -> str:
    """Return a human-readable summary of the voice presets."""
    values = _preset_values()
    tts_instance = _current_tts(context)
    active_key = (
        _active_preset_key(tts_instance) if tts_instance is not None else None
    )

    parts: list[str] = []
    for key in ("voice1", "voice2"):
        label = _PRESET_LABEL[key]
        env_name = _PRESET_ENV_NAME[key]
        val = values[key]
        if not val:
            parts.append(f"{label} (not configured — {env_name} unset)")
            continue
        if active_key == key:
            parts.append(f"{label} (active)")
        else:
            parts.append(f"{label} (available)")

    if active_key is None and tts_instance is not None:
        parts.append("note: currently active voice does not match any preset")
    if tts_instance is None:
        parts.append("note: voice switching unavailable in this session")

    return ", ".join(parts)


@function_tool(
    name="lily_switch_voice",
    description=(
        "Switch Lily's active speaking voice to one of the configured "
        "presets: voice1 (primary) or voice2. Use this when a player says "
        "'switch to voice two', 'use your other voice', 'go back to your "
        "first voice', or similar. The change takes effect on the NEXT "
        "turn — your reply after calling this tool will be spoken in the "
        "new voice."
    ),
)
async def lily_switch_voice(
    context: RunContext,
    preset: Literal["voice1", "voice2"],
) -> str:
    """Swap the active TTS voice preset. Returns a plain-string confirmation."""
    values = _preset_values()
    target_voice_id = values.get(preset)
    env_name = _PRESET_ENV_NAME[preset]

    if not target_voice_id:
        return (
            f"cannot switch: {preset} is not configured "
            f"({env_name} unset in this environment)"
        )

    tts_instance = _current_tts(context)
    if tts_instance is None:
        return "voice switching unavailable: no LilyTTS on the current session"

    try:
        tts_instance.set_voice(target_voice_id)
    except ValueError as exc:
        logger.warning("lily_switch_voice | rejected voice_id: %s", exc)
        return f"cannot switch: {exc}"
    except Exception as exc:
        logger.warning("lily_switch_voice | unexpected error: %s", exc)
        return f"cannot switch: {type(exc).__name__}"

    logger.info(
        "VOICE_SWITCH | preset=%s voice_id=%s",
        preset,
        target_voice_id,
    )
    # P0-2: voice setup is complete only after the live TTS mutation
    # succeeds. Reach the current LilyAgent without importing lily_agent
    # (avoids a cycle) and clear its game-level setup job.
    session = getattr(context, "session", None)
    current_agent = (
        getattr(session, "current_agent", None)
        or getattr(session, "agent", None)
        if session is not None
        else None
    )
    game = getattr(current_agent, "_game", None)
    mark_applied = getattr(game, "mark_setup_applied", None)
    if callable(mark_applied):
        mark_applied("voice")
    return f"switched to {preset}. next turn will use the new voice."


__all__ = ["lily_list_voices", "lily_switch_voice"]
