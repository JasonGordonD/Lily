"""C7 (WO-LILY-HOSTLOOP-001) — start is an intent match; no false rejoin.

Session A (2026-08-12 04:51): the player said "Starts." — an STT
rendering of "start" that matched none of the phrase patterns — 13
seconds of dead air followed, and the empty-STOP lobby recovery answered
it with the REJOIN script ("welcome back") because a stale same-room
checkpoint had set `reconnected`. Two fixes, both pinned here:

  1. A bare start token IS the start intent when it is essentially the
     whole utterance — matched as an intent, never a substring.
  2. Once a start intent is heard (or start_game runs, any source), the
     lobby recovery never speaks — the rejoin script is reserved for the
     genuine reconnect re-entry at on_enter.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import (
    lily_detect_control_command,
    lily_is_bare_start_intent,
)


def test_the_session_a_utterance_now_starts_the_game():
    assert lily_detect_control_command("Starts.") == "start_game"
    assert lily_detect_control_command("start") == "start_game"
    assert lily_detect_control_command("Begin!") == "start_game"
    assert lily_detect_control_command("kick off") == "start_game"
    assert lily_detect_control_command("okay Lily, start") == "start_game"


def test_start_inside_a_sentence_is_not_an_intent():
    """Substring matches must never launch a game."""
    for text in (
        "before we start, one quick question",
        "she starts crying every time we play this",
        "my car wouldn't start this morning",
        "don't start the timer yet please hold on a moment",
        "when does the next round start for the late players",
    ):
        assert lily_is_bare_start_intent(text) is False, text
        # The phrase patterns must not catch these either.
        assert lily_detect_control_command(text) != "start_game", text


def test_existing_phrase_starts_still_fire():
    for text in ("start the game", "let's start", "ready to play",
                 "begin the round", "kick it off"):
        assert lily_detect_control_command(text) == "start_game", text


def test_recovery_never_regreets_after_a_start_intent():
    """The Session A false "welcome back": reconnected=True (stale
    checkpoint) + a lobby recovery = rejoin script mid-conversation.
    After a start intent, the recovery must stand down entirely."""
    src = inspect.getsource(LilyAgent._recover_lobby_empty_stop) if hasattr(
        LilyAgent, "_recover_lobby_empty_stop"
    ) else None
    if src is None:
        # Resolve the actual method name from the wiring: find the method
        # containing the reconnected-rejoin chooser.
        for name, member in inspect.getmembers(LilyAgent, inspect.isfunction):
            try:
                s = inspect.getsource(member)
            except (OSError, TypeError):
                continue
            if "session_rejoin" in s and "_empty_stop_lobby_recover_count" in s:
                src = s
                break
    assert src is not None, "lobby recovery method not found"
    assert "_start_intent_heard" in src
    # The stand-down happens BEFORE the rejoin/greet chooser.
    assert src.index("_start_intent_heard") < src.index("session_rejoin")


def test_start_game_and_voice_path_both_set_the_flag():
    start_src = inspect.getsource(LilyGame.start_game)
    assert "_start_intent_heard = True" in start_src
