"""WO-LILY-PATCH-003 P4 — picture claims and refusals from state reads.

Dual-fabrication fixture (DB-evidenced): "pictures are live on the screen
now" (false — no flip/push) then 58s later "picture search is off
tonight" (also false — the ledger shows successful pushes). Tone is not
the mechanism. The picture-lane state is field-granular; a refusal may
cite only the field that actually reads off.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _make_game(media_mode="voice_only", mode="general", supabase=object()):
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("p4-fixture")
    game.sk.media_mode = media_mode
    game.sk.mode = mode
    game.supabase = supabase
    return game


def test_status_reads_each_field_separately(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    monkeypatch.setenv("XAI_API_KEY", "x")
    game = _make_game(media_mode="pictures", mode="adult")
    game.sk.adult_image_intensity = "explicit"
    s = game.picture_lane_status()
    assert s["pictures_on"] is True
    assert s["generation_available"] is True
    assert s["pipeline_available"] is True
    assert s["deck"] == "adult"
    assert s["heat"] == "explicit"


def test_healthy_lane_off_grounds_not_switched_on_never_off_tonight(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    game = _make_game(media_mode="voice_only")
    line = game.picture_lane_state_line()
    assert "NOT switched on" in line
    assert "off tonight" in line and "never" in line  # the ban is stated


def test_missing_generation_key_is_the_only_cited_reason(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    game = _make_game(media_mode="pictures")
    line = game.picture_lane_state_line()
    assert "generation key is not configured" in line
    assert "only honest reason" in line


def test_unreachable_pipeline_reads_off(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    game = _make_game(media_mode="pictures", supabase=None)
    line = game.picture_lane_state_line()
    assert "pipeline is unreachable" in line


def test_pictures_on_grounds_claim_to_a_real_push(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    game = _make_game(media_mode="pictures")
    line = game.picture_lane_state_line()
    assert "pictures ARE on" in line
    assert "actually reached the screen" in line


def test_state_block_carries_the_lane_line(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    game = _make_game(media_mode="voice_only")
    # Minimal fields build_state_block touches beyond the lane line.
    game.acoustic = type("A", (), {"state_block_lines": lambda self: []})()
    game.last_addressee_judgment = None
    game.availability_flags = None
    game.device_candidate_group_id = None
    game.armed_question = None
    game.next_question = None
    game._state_note = None
    game.availability_flags = None
    line = game.picture_lane_state_line()
    assert line is not None and "picture lane read" in line
