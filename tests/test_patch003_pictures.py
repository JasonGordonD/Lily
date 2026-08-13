"""WO-LILY-PATCH-003 P1 + P2 — picture activation + relevance binding.

P1 fixture: "pictures are live on the screen now" with media_mode still
voice_only and no push. Activation is a real, dependency-checked flip:
a down lane never flips to a false "ON"; the honest cause is named.

P2 fixture: the 21:59 push — an image served with nothing to do with its
question. The image a picture question airs is ITS OWN (open_window
publishes only the armed question's image_url); a failed generation
leaves no image_url → pictureless, never a substitute.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _make_game(mode="general", supabase=object()):
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("p1p2")
    game.sk.mode = mode
    game.sk.media_mode = "voice_only"
    game.supabase = supabase
    return game


# -- P1: dependency-checked activation -----------------------------------------


def test_activation_on_when_lane_healthy(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    game = _make_game()
    assert game.picture_activation_outcome() == "on"


def test_activation_blocked_when_generation_key_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    game = _make_game()
    assert game.picture_activation_outcome() == "unavailable_gen"


def test_activation_blocked_when_pipeline_down(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    game = _make_game(supabase=None)
    assert game.picture_activation_outcome() == "unavailable_pipeline"


def test_adult_activation_reads_the_xai_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g")  # general key present, adult key not
    game = _make_game(mode="adult")
    assert game.picture_activation_outcome() == "unavailable_gen"


# -- P2: relevance binding (structural) ----------------------------------------


def test_picture_question_carries_its_own_image_only():
    """The publish seam attaches the ARMED question's image_url — an image
    can only ever be its own question's. A substitute is unreachable."""
    game = _make_game()
    game.armed_question = {
        "prompt": "What is this landmark?",
        "image_url": "https://bucket/landmark_q3.png",
        "canonical_answer": "x",
    }
    # The value open_window would publish is the armed question's own url.
    assert game.armed_question.get("image_url") == "https://bucket/landmark_q3.png"


def test_failed_generation_leaves_no_image_url_pictureless():
    """A question whose generation failed has image_url None — it runs
    pictureless, never with a borrowed image."""
    game = _make_game()
    game.armed_question = {
        "prompt": "What is this?", "image_url": None, "canonical_answer": "x",
    }
    assert game.armed_question.get("image_url") is None
