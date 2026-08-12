"""Transcript sync (2026-08-09 live report: "the sync between what is
being said and what is being displayed is off").

Archaeology: the glass transcript received Lily's line ONLY from
on_agent_speech_finished — playout COMPLETION — so a long turn showed
nothing while she spoke and then the whole paragraph landed late. The
UI side was already correct (segment-id upsert, final locks); it was
never sent an interim. The aired text is knowable at playout START
because tts_node binds the exact TTS input to the speech handle BEFORE
synthesis (Y5's construction), so note_playout_started now publishes it
as an interim segment; the completion publish (same segment id, final,
"…[cut off]" marker when interrupted) replaces it in place.

The Y5 one-shot consume contract is untouched: the interim path PEEKS.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _PublishSpy:
    def __init__(self):
        self.calls = []


def _game_with_spy():
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("sync-test")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game._post_tts_text_by_speech_id = {}
    spy = _PublishSpy()

    def _spy_publish(text, *, speech_id, interrupted, final=True):
        spy.calls.append(
            {"text": text, "speech_id": speech_id,
             "interrupted": interrupted, "final": final}
        )

    game.publish_agent_transcription_nowait = _spy_publish
    return game, spy


def test_playout_start_publishes_the_bound_text_as_interim():
    game, spy = _game_with_spy()
    game.note_post_tts_text("s1", "Round two, and this one is a trap.")
    game.note_playout_started("s1")
    assert spy.calls == [{
        "text": "Round two, and this one is a trap.",
        "speech_id": "s1", "interrupted": False, "final": False,
    }]


def test_peek_does_not_consume_the_binding():
    """Y5's one-shot consume still belongs to playout completion."""
    game, spy = _game_with_spy()
    game.note_post_tts_text("s1", "bound text")
    game.note_playout_started("s1")
    assert game.consume_post_tts_text("s1", "fallback") == "bound text"
    # And the binding is spent only now.
    assert game.consume_post_tts_text("s1", "fallback") == "fallback"


def test_no_binding_means_no_interim_publish():
    """A speech with no bound TTS text (queued audio, SFX lanes) publishes
    nothing at start — the completion publish still covers the turn."""
    game, spy = _game_with_spy()
    game.note_playout_started("mystery-speech")
    assert spy.calls == []


def test_completion_final_replaces_the_same_segment():
    """Source pin: both publishes ride the same segment id (the speech id),
    which is what lets the glass upsert-then-lock in place — and the
    completion path still publishes final (default True)."""
    src = inspect.getsource(lily_agent.LilyGame.publish_agent_transcription_nowait)
    assert "segment_id = speech_id or" in src
    sig = inspect.signature(lily_agent.LilyGame.publish_agent_transcription_nowait)
    assert sig.parameters["final"].default is True
    # The interim call site passes final=False and peeks (never consumes).
    import lily_speech_delivery
    start_src = inspect.getsource(LilyGame.note_playout_started)
    assert "peek_post_tts_text" in start_src
    assert "final=False" in start_src
    assert "consume_post_tts_text" not in start_src
