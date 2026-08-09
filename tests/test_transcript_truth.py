"""Y5 (WO-LILY-HOTFIX-007) — the record follows aired text, pinned.

The WO's diagnosis was that the one-question yield lint trims AFTER audio
airs and rewrites the record to match ("POST_TTS_REWRITE"). Archaeology
inverted it: the clip runs in tts_node BEFORE synthesis, the clipped text
is bound to the speech handle (note_post_tts_text), that same text is
what synthesizes, and playout completion consumes the binding for both
the RTC and durable transcripts. The record follows the TTS input — i.e.
aired truth — by construction. The log line was named as if it falsified
(it corrects the record TOWARD aired text, replacing the model's pre-clip
prose); it is renamed RECORD_BOUND_TO_TTS_INPUT.

What these tests pin so the construction can never silently regress:
  1. source order in tts_node: yield-clip -> note_post_tts_text ->
     synthesis replay (the WO's "trim before synthesis", as a source pin);
  2. the note/consume pair: the recorded text IS the TTS input, exactly,
     for a clipped turn; unknown handles fall back to the framework text;
     the binding is one-shot.

Known, documented limit (belongs to Y7, not Y5): with text_output=False
there is no transcript synchronizer, so on an INTERRUPTED turn no text
source knows the aired portion — the "…[cut off]" marker is the honest
partial-airing signal, and audio remains the only ground truth for how
much of a cut turn played.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("y5-truth")
    return game


def test_trim_precedes_synthesis_in_source():
    """The WO's binding rule as a source-order pin: the yield clip and the
    TTS-input binding both happen before the synthesis replay inside
    tts_node (same idiom as the watchdog source-order pin)."""
    src = inspect.getsource(lily_agent.LilyAgent.tts_node)
    clip = src.index("lily_yield_after_first_question")
    bind = src.index("note_post_tts_text")
    replay = src.index("async def _replay")
    assert clip < bind < replay


def test_recorded_text_is_the_tts_input_for_a_clipped_turn():
    game = _game()
    raw = (
        "Fair enough — I'll keep an ear out for the good stuff myself. "
        "What should I call you tonight? And one more thing after."
    )
    clipped = (
        "Fair enough — I'll keep an ear out for the good stuff myself. "
        "What should I call you tonight?"
    )
    game.note_post_tts_text("speech_1", clipped)
    # Playout completion consumes with the framework's raw text as the
    # fallback — the record must come out as EXACTLY what entered TTS.
    assert game.consume_post_tts_text("speech_1", raw) == clipped


def test_unknown_handle_falls_back_to_framework_text():
    game = _game()
    assert (
        game.consume_post_tts_text("never_noted", "framework text")
        == "framework text"
    )


def test_the_binding_is_one_shot():
    game = _game()
    game.note_post_tts_text("speech_2", "bound text")
    assert game.consume_post_tts_text("speech_2", "raw") == "bound text"
    # Consumed — a second playout event for the same id gets the fallback,
    # never a stale binding.
    assert game.consume_post_tts_text("speech_2", "raw") == "raw"
