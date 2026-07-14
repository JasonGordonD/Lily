"""
lily_config.py — LILY environment configuration.

All environment access for the Lily agent lives in this module (ambient
discipline: no raw os.environ reads scattered through the tree). Values are
read lazily so the module imports cleanly in test environments with no env
configured. Anything required at session start is validated fail-fast in
lily_agent.py / lily_persistence.py via the require_* helpers here.
"""

import os
from typing import Optional


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _require(name: str) -> str:
    value = _get(name)
    if value is None:
        raise RuntimeError(f"LILY_INIT | missing required env var: {name}")
    return value


# ---------------------------------------------------------------------------
# LiveKit
# ---------------------------------------------------------------------------

def livekit_url() -> Optional[str]:
    return _get("LIVEKIT_URL")


def livekit_api_key() -> Optional[str]:
    return _get("LIVEKIT_API_KEY")


def livekit_api_secret() -> Optional[str]:
    return _get("LIVEKIT_API_SECRET")


# ---------------------------------------------------------------------------
# ElevenLabs TTS — env var is ELEVEN_API_KEY (never ELEVENLABS_API_KEY)
# ---------------------------------------------------------------------------

def eleven_api_key() -> str:
    return _require("ELEVEN_API_KEY")


def lily_voice_id() -> str:
    """Lily uses Raven's voice: LILY_VOICE_ID with RAVEN_VOICE_ID fallback."""
    voice = _get("LILY_VOICE_ID") or _get("RAVEN_VOICE_ID")
    if not voice:
        raise RuntimeError(
            "LILY_INIT | no voice configured: set LILY_VOICE_ID (or RAVEN_VOICE_ID)"
        )
    return voice


# ---------------------------------------------------------------------------
# Google / Gemini
# ---------------------------------------------------------------------------

def google_api_key() -> str:
    return _require("GOOGLE_API_KEY")


def vocal_model() -> str:
    return _get("LILY_VOCAL_MODEL", "gemini-3.5-flash")


def reasoning_model() -> str:
    return _get("LILY_REASONING_MODEL", "gemini-3.1-pro-preview")


def vocal_max_output_tokens() -> int:
    # Spec §4.4: max_output_tokens >= 600 on live calls.
    return max(600, _get_int("LILY_MAX_OUTPUT_TOKENS", 800))


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def supabase_url() -> Optional[str]:
    return _get("SUPABASE_URL")


def supabase_service_role_key() -> Optional[str]:
    return _get("SUPABASE_SERVICE_ROLE_KEY")


# ---------------------------------------------------------------------------
# Game tunables
# ---------------------------------------------------------------------------

def answer_window_seconds() -> float:
    """Bounded answer window duration (default 15s, per-round configurable)."""
    return _get_float("LILY_ANSWER_WINDOW_SECONDS", 15.0)


def steal_window_seconds() -> float:
    return _get_float("LILY_STEAL_WINDOW_SECONDS", 5.0)


def checkpoint_interval_seconds() -> float:
    return _get_float("LILY_CHECKPOINT_INTERVAL_SECONDS", 60.0)


def shutdown_timeout_seconds() -> float:
    return _get_float("LILY_SHUTDOWN_TIMEOUT_SECONDS", 30.0)


def rounds_total() -> int:
    return _get_int("LILY_ROUNDS", 3)


def kb_only() -> bool:
    """Demo-day fallback: flip question supply to the curated bank only
    (runbook: 'flip to KB-bank-only via the state block')."""
    return (_get("LILY_KB_ONLY", "") or "").strip().lower() in ("1", "true", "yes", "on")


def questions_per_round() -> int:
    return _get_int("LILY_QUESTIONS_PER_ROUND", 6)


def auto_start_min_players() -> int:
    """Roster size at or above which the lobby auto-start safety net can
    fire. Guards single-voice tune-ups from being flipped into game mode."""
    return max(1, _get_int("LILY_AUTO_START_MIN_PLAYERS", 2))


def auto_start_lobby_grace_seconds() -> float:
    """Wall-clock grace inside the lobby before the auto-start safety net
    is allowed to fire. Long enough for names + one lobby fact per player;
    short enough that a table that never touches the UI start button still
    reaches question one."""
    return _get_float("LILY_AUTO_START_LOBBY_GRACE_SECONDS", 60.0)


def group_id_override() -> Optional[str]:
    """Stable group id for voiceprint rematch (v2); defaults to room name."""
    return _get("LILY_GROUP_ID")


# SFX assets — optional file paths; hooks are wired but silent when unset.
def thinking_bed_path() -> Optional[str]:
    """60 BPM ticking thinking-bed played during answer windows."""
    return _get("LILY_THINKING_BED_PATH")


def stinger_correct_path() -> Optional[str]:
    return _get("LILY_STINGER_CORRECT_PATH")


def stinger_incorrect_path() -> Optional[str]:
    return _get("LILY_STINGER_INCORRECT_PATH")


def session_log_dir() -> Optional[str]:
    """Optional per-session log file directory (fleet pattern). None disables."""
    return _get("LILY_SESSION_LOG_DIR")


def job_memory_limit_mb() -> float:
    """Explicit worker memory limit (1.6.4 memory-monitor hardening)."""
    return _get_float("LILY_JOB_MEMORY_LIMIT_MB", 2048.0)
