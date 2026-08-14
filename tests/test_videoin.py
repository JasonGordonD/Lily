"""WO-LILY-VIDEOIN-001 — sparse user-initiated video-in (show-and-tell).

The camera lane is off by default, opens only on an explicit trigger, is
structurally unavailable in the adult deck, attaches exactly one recent
frame to one turn and retains nothing, and never describes people. The
live rtc.VideoStream frame sink is the one seam not exercised here; the
state machine, detector, grounding, and one-frame semantics are.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_scorekeeper import lily_detect_camera_request, LilyScorekeeper
from lily_agent import LilyGame


# -- V1: the spoken trigger ---------------------------------------------------

def test_camera_requests_fire():
    for t in ["look at this", "can you see this?", "check this out",
              "let me show you something", "look at my drawing",
              "watch this", "turn on the camera", "do you see this"]:
        assert lily_detect_camera_request(t) is True, t


def test_game_state_reads_never_open_the_camera():
    for t in ["look at the score", "can you see question three",
              "see the board", "look at the clock", "turn off the camera",
              "no camera please"]:
        assert lily_detect_camera_request(t) is False, t


# -- V2: grounded lane status + honest lines ----------------------------------

def _game(mode="general", lane="off", frame=None):
    g = LilyGame.bare()
    g.sk = LilyScorekeeper("v")
    g.sk.mode = mode
    g.sk.camera_lane = lane
    g._latest_video_frame = frame
    return g


def test_line_offers_when_closed_available():
    line = _game().camera_lane_state_line()
    assert "NOT on" in line and "OFFER" in line


def test_line_open_no_frame_is_honest():
    line = _game(lane="open").camera_lane_state_line()
    assert "no frame" in line and "come through yet" in line


def test_line_open_with_frame_carries_person_constraint():
    line = _game(lane="open", frame=object()).camera_lane_state_line()
    assert "OBJECT or SCENE" in line
    assert "never identify" in line and "person" in line.lower()


# -- one frame, this turn, then gone (no retention) ---------------------------

def test_take_frame_consumes_and_clears():
    g = _game(lane="open", frame=object())
    f1 = g.take_camera_frame()
    assert f1 is not None
    assert g._camera_frame_shown is True
    # Second take is None — the frame rode exactly one turn.
    assert g.take_camera_frame() is None


def test_take_frame_none_when_empty():
    g = _game(lane="open", frame=None)
    assert g.take_camera_frame() is None
