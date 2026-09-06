"""Closure instrumentation for the two lily-639007 open items — neither
needs a log export again:

1. The 17s composite (answer -> air, COMMIT_TO_DISPATCH_MS read 0, the
   time hid between dispatch and first frame): note_playout_started now
   logs DISPATCH_TO_AIR_MS per composite off the existing flight stamp.
2. The 2.5-minute recognition (centroid 2.5h fresh, and nothing persisted
   said whether the biometric match MISSED or never RAN): the outcome and
   V2 timing now ride the session report metadata.
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game():
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("latency-obs")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    return game


def test_dispatch_to_air_logs_for_a_composite(caplog):
    game = _game()
    game._composite_flight_state = {
        "act": "verdict", "owner": "speech_9",
        "key": None, "qnum": 1, "at": time.monotonic() - 1.5,
    }
    with caplog.at_level(logging.INFO, logger="lily.agent"):
        game.note_playout_started("speech_9")
    line = next(
        r.getMessage() for r in caplog.records
        if "DISPATCH_TO_AIR_MS" in r.getMessage()
    )
    assert "act=verdict" in line


def test_no_flight_no_latency_line(caplog):
    game = _game()
    with caplog.at_level(logging.INFO, logger="lily.agent"):
        game.note_playout_started("speech_x")
    assert not any(
        "DISPATCH_TO_AIR_MS" in r.getMessage() for r in caplog.records
    )


def test_voice_id_outcome_stamps_exist_in_the_match_path():
    import inspect

    import lily_agent

    src = inspect.getsource(
        lily_agent.LilyGame._voice_identity_match_at_start
    )
    assert '_voice_id_outcome' in src
    assert '"no_match"' in src
    # And the metadata lane carries the block — WO-LILY-DELIVERY-TRUTH-001
    # made both write sites (heartbeat + session end) ONE builder, and
    # WO-LILY-VOICE-TRUTH-001 rule (f) made the block the full receipt: it
    # is driven here instead of source-counted.
    from lily_scorekeeper import LilyScorekeeper

    game = lily_agent.LilyGame.bare(sk=LilyScorekeeper("lily-voice-id-lane"))
    payload = lily_agent.lily_session_metadata(game, game.sk, {}, None)
    assert payload["voice_identity"] == game.voice_identity_receipt()
    # bare(): no embedder → the receipt says so ('disabled'); a configured
    # game that never probed says 'never_ran' — both first-class values.
    assert payload["voice_identity"]["outcome"] in ("never_ran", "disabled")
    game._voice_identity_attempted = True
    game._voice_id_outcome = "no_match"
    game._voice_id_embed_ms = 12.5
    game._voice_id_resolve_ms = 40.0
    receipt = lily_agent.lily_session_metadata(game, game.sk, {}, None)["voice_identity"]
    assert receipt["outcome"] == "no_match"
    assert receipt["embed_ms"] == 12.5 and receipt["resolve_ms"] == 40.0
