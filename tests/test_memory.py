"""Tests for lily_memory — pure logic only (block builder, summary template,
group-id metadata parsing, KB-bank mode guard). No livekit, no network."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_memory import (
    MEMORY_BLOCK_MARKER,
    MEMORY_BLOCK_MAX_CHARS,
    lily_bank_mode_filter,
    lily_build_memory_block,
    lily_build_session_summary,
    lily_parse_group_id_from_metadata,
    lily_session_winner,
)


def make_memory(**overrides):
    memory = {
        "sessions": [
            {
                "session_id": "room-2",
                "winner": "Sarah",
                "question_count": 18,
                "players": [
                    {"name": "Sarah", "score": 12, "streak": 3},
                    {"name": "Dave", "score": 8, "streak": 0},
                ],
                "summary": "Sarah won with 12 point(s) over 18 question(s).",
            },
            {
                "session_id": "room-1",
                "winner": "Dave",
                "question_count": 12,
                "players": [
                    {"name": "Dave", "score": 9, "streak": 1},
                    {"name": "Priya", "score": 4, "streak": 0},
                ],
                "summary": "Dave won with 9 point(s) over 12 question(s).",
            },
        ],
        "facts": [
            {"player_name": "Dave", "fact": "owns 40 typewriters"},
            {"player_name": "Sarah", "fact": "afraid of geese"},
        ],
        "player_names": ["Sarah", "Dave", "Priya"],
        "last_winner": "Sarah",
        "total_games": 5,
    }
    memory.update(overrides)
    return memory


# ---------------------------------------------------------------------------
# Memory block builder
# ---------------------------------------------------------------------------

def test_block_renders_marker_names_winner_facts():
    block = lily_build_memory_block(make_memory())
    assert block.startswith(MEMORY_BLOCK_MARKER)
    assert "Sarah" in block and "Dave" in block and "Priya" in block
    assert "Sarah won" in block
    assert "owns 40 typewriters" in block
    assert "afraid of geese" in block
    assert "5 time(s)" in block


def test_block_instructs_greeting_by_name():
    block = lily_build_memory_block(make_memory())
    assert "BY NAME" in block
    assert "won last time" in block


def test_block_none_and_empty_render_empty():
    assert lily_build_memory_block(None) == ""
    assert lily_build_memory_block({}) == ""
    assert lily_build_memory_block({"sessions": [], "facts": []}) == ""


def test_block_facts_only_group_still_renders():
    memory = make_memory(sessions=[], player_names=[], last_winner=None,
                         total_games=0)
    block = lily_build_memory_block(memory)
    assert block.startswith(MEMORY_BLOCK_MARKER)
    assert "owns 40 typewriters" in block


def test_block_last_game_tie_falls_back_to_summary():
    memory = make_memory(last_winner=None)
    memory["sessions"][0]["winner"] = None
    memory["sessions"][0]["summary"] = "No sole winner over 18 question(s)."
    block = lily_build_memory_block(memory)
    assert "No sole winner" in block


def test_block_caps_length():
    memory = make_memory(
        facts=[
            {"player_name": f"Player{i}", "fact": "x" * 120}
            for i in range(10)
        ],
        player_names=[f"Verylongplayername{i}" for i in range(12)],
    )
    block = lily_build_memory_block(memory)
    assert len(block) <= MEMORY_BLOCK_MAX_CHARS
    assert block.startswith(MEMORY_BLOCK_MARKER)


# ---------------------------------------------------------------------------
# Summary template + winner
# ---------------------------------------------------------------------------

def test_summary_with_winner():
    standings = [
        {"name": "Sarah", "score": 12, "streak": 3},
        {"name": "Dave", "score": 8, "streak": 0},
    ]
    summary = lily_build_session_summary(standings, "Sarah", 18)
    assert summary == (
        "Sarah won with 12 point(s) over 18 question(s). "
        "Final scores: Sarah 12, Dave 8."
    )


def test_summary_tie_has_no_winner():
    standings = [
        {"name": "Sarah", "score": 8},
        {"name": "Dave", "score": 8},
    ]
    assert lily_session_winner(standings) is None
    summary = lily_build_session_summary(standings, None, 12)
    assert "No sole winner" in summary
    assert "Sarah 8, Dave 8" in summary


def test_summary_empty_standings():
    summary = lily_build_session_summary([], None, 0)
    assert "no players bound" in summary


def test_winner_sole_top_scorer():
    standings = [
        {"name": "Sarah", "score": 12},
        {"name": "Dave", "score": 8},
    ]
    assert lily_session_winner(standings) == "Sarah"


def test_winner_none_when_no_points():
    assert lily_session_winner([{"name": "Sarah", "score": 0}]) is None
    assert lily_session_winner([]) is None


# ---------------------------------------------------------------------------
# Group-id metadata parsing
# ---------------------------------------------------------------------------

def test_parse_group_id_from_metadata():
    meta = json.dumps({"lily_group_id": "abc-123-uuid"})
    assert lily_parse_group_id_from_metadata(meta) == "abc-123-uuid"


def test_parse_group_id_tolerates_garbage():
    assert lily_parse_group_id_from_metadata(None) is None
    assert lily_parse_group_id_from_metadata("") is None
    assert lily_parse_group_id_from_metadata("not json {") is None
    assert lily_parse_group_id_from_metadata(json.dumps(["a", "b"])) is None
    assert lily_parse_group_id_from_metadata(json.dumps({"other": "x"})) is None
    assert lily_parse_group_id_from_metadata(json.dumps({"lily_group_id": ""})) is None
    assert lily_parse_group_id_from_metadata(json.dumps({"lily_group_id": 42})) is None


def test_parse_group_id_strips_whitespace():
    meta = json.dumps({"lily_group_id": "  uuid-1  "})
    assert lily_parse_group_id_from_metadata(meta) == "uuid-1"


# ---------------------------------------------------------------------------
# KB-bank mode guard (consent-safety)
# ---------------------------------------------------------------------------

BANK_ROWS = [
    {"id": 1, "question": "clean q1", "adult": False},
    {"id": 2, "question": "adult q", "adult": True},
    {"id": 3, "question": "clean q2"},          # column absent -> not adult
    {"id": 4, "question": "clean q3", "adult": None},
]


def test_general_mode_excludes_adult_rows():
    filtered = lily_bank_mode_filter(BANK_ROWS, "general")
    assert [r["id"] for r in filtered] == [1, 3, 4]


def test_adult_mode_returns_all_rows():
    filtered = lily_bank_mode_filter(BANK_ROWS, "adult")
    assert [r["id"] for r in filtered] == [1, 2, 3, 4]


def test_missing_or_unknown_mode_defaults_safe():
    assert all(not r.get("adult") for r in lily_bank_mode_filter(BANK_ROWS, None))
    assert all(not r.get("adult") for r in lily_bank_mode_filter(BANK_ROWS, ""))
    assert all(not r.get("adult") for r in lily_bank_mode_filter(BANK_ROWS, "weird"))


def test_filter_handles_empty_rows():
    assert lily_bank_mode_filter([], "general") == []
    assert lily_bank_mode_filter(None, "adult") == []
