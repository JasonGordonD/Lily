"""WS-10 (WO-LILY-OMNIBUS-003) — STT segment hygiene.

Session lily-81BCB0-583a0f16: corrupted spans (104s, 206s) finalized
minutes late and out of order; a stale utterance scored into the Black
Panther window 3.5 minutes after it was spoken; talk-time poisoned
(Rhonda 555.5s, Chris 0.4s). The gate: insane finals are quarantined —
logged, excluded from windows and talk-time, game-inert. Window
membership keys on SPOKEN time, never finalization time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from lily_scorekeeper import LilyScorekeeper

T0 = 1_000_000.0


def make_sk():
    sk = LilyScorekeeper(session_id="test-room")
    sk.bind_speaker("S1", "Rhonda")
    sk.bind_speaker("S2", "Chris")
    return sk


def final(sk, text, label, start, end, now, **kwargs):
    return sk.on_transcript_segment(
        text=text,
        speaker_label=label,
        is_final=True,
        segment_start_time=start,
        segment_end_time=end,
        now=now,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Quarantine gate
# ---------------------------------------------------------------------------

def test_long_span_late_final_is_quarantined_and_game_inert():
    """The session bug verbatim: a 104s span finalizing 206s after the
    speech ended lands during an open window. It must not become a
    candidate, must not accrue talk-time, and must appear in the
    quarantine log."""
    sk = make_sk()
    sk.start_question({"canonical_answer": "Black Panther"})
    sk.open_answer_window(duration=15.0, now=T0)
    talk_before = sk.players["Rhonda"]["talk_time_s"]

    result = final(
        sk, "wakanda forever and a lot of stale echo text",
        "S1", start=T0 - 310.0, end=T0 - 206.0, now=T0 + 4.0,
    )

    assert result["quarantined"] is True
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}
    assert sk.players["Rhonda"]["talk_time_s"] == talk_before
    assert len(sk.quarantined_segments) == 1
    entry = sk.quarantined_segments[0]
    assert entry["speaker_label"] == "S1"
    assert entry["span_seconds"] > lily_config.segment_max_span_seconds()
    assert (
        entry["finalization_lag_seconds"]
        > lily_config.segment_max_finalization_lag_seconds()
    )
    assert "span" in entry["reason"] and "lag" in entry["reason"]


def test_sane_span_but_stale_finalization_is_quarantined():
    """Lag-only trigger: a normal-length utterance finalizing minutes
    after it was spoken (the Black Panther stale-scoring bug)."""
    sk = make_sk()
    sk.start_question(None)
    sk.open_answer_window(duration=15.0, now=T0)

    result = final(sk, "black panther", "S2",
                   start=T0 + 1.0, end=T0 + 3.0, now=T0 + 3.0 + 210.0)

    assert result["quarantined"] is True
    assert sk.answer_candidates == {}
    assert sk.quarantined_segments[0]["reason"] == "lag"


def test_long_span_alone_is_quarantined():
    sk = make_sk()
    result = final(sk, "echo " * 50, "S1",
                   start=T0, end=T0 + 104.0, now=T0 + 104.5)
    assert result["quarantined"] is True
    assert sk.quarantined_segments[0]["reason"] == "span"
    assert sk.players["Rhonda"]["talk_time_s"] == 0.0


def test_sane_segment_passes_untouched():
    sk = make_sk()
    sk.start_question(None)
    sk.open_answer_window(duration=15.0, now=T0)

    result = final(sk, "tungsten", "S1",
                   start=T0 + 2.0, end=T0 + 3.5, now=T0 + 4.0)

    assert result.get("quarantined") is False
    assert result["candidate_recorded"] is True
    assert "Rhonda" in sk.answer_candidates
    assert sk.players["Rhonda"]["talk_time_s"] > 0.0
    assert sk.quarantined_segments == []


def test_quarantined_segment_never_flips_overlap():
    """Game-inert includes the crosstalk prior: an insane span must not
    poison overlap detection for the window."""
    sk = make_sk()
    sk.open_answer_window(duration=15.0, now=T0)
    final(sk, "tungsten", "S1", start=T0 + 1.0, end=T0 + 2.0, now=T0 + 2.2)
    final(sk, "stale wall of echo", "S2",
          start=T0 - 200.0, end=T0 + 5.0, now=T0 + 5.1)
    assert sk.overlap_flag is False


def test_segments_without_timing_are_not_quarantined():
    """arrival_time-source finals (no stream timings) carry span 0 / lag 0
    — they must keep working exactly as before."""
    sk = make_sk()
    sk.open_answer_window(duration=15.0, now=T0)
    result = sk.on_transcript_segment(
        text="tungsten", speaker_label="S1", is_final=True, now=T0 + 2.0,
    )
    assert result.get("quarantined") is False
    assert result["candidate_recorded"] is True


def test_thresholds_come_from_config_env(monkeypatch):
    monkeypatch.setenv("LILY_SEGMENT_MAX_SPAN_SECONDS", "5.0")
    monkeypatch.setenv("LILY_SEGMENT_MAX_FINALIZATION_LAG_SECONDS", "2.0")
    assert lily_config.segment_max_span_seconds() == 5.0
    assert lily_config.segment_max_finalization_lag_seconds() == 2.0
    sk = make_sk()
    result = final(sk, "a slightly long answer", "S1",
                   start=T0, end=T0 + 6.0, now=T0 + 6.1)
    assert result["quarantined"] is True


def test_quarantine_log_is_bounded():
    sk = make_sk()
    for i in range(250):
        final(sk, f"echo {i}", "S1",
              start=T0 + i, end=T0 + i + 200.0, now=T0 + i + 200.5)
    assert len(sk.quarantined_segments) == 200


# ---------------------------------------------------------------------------
# Spoken-time window membership
# ---------------------------------------------------------------------------

def test_window_membership_uses_spoken_time_not_finalization_time():
    """A final spoken BEFORE the window opened (but finalizing inside it,
    under the lag limit) is not a member of this window."""
    sk = make_sk()
    sk.start_question(None)
    sk.open_answer_window(duration=15.0, now=T0)
    result = final(sk, "black panther", "S1",
                   start=T0 - 12.0, end=T0 - 10.0, now=T0 + 2.0)
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}
    # Sane segment: talk-time still accrues — only WINDOW membership is
    # spoken-time-scoped.
    assert sk.players["Rhonda"]["talk_time_s"] > 0.0


def test_in_window_speech_counts_even_if_finalized_after_deadline():
    """The inverse: speech spoken inside the window whose final arrives
    just after the deadline (window flag still open) still belongs to the
    window it was spoken in."""
    sk = make_sk()
    sk.start_question(None)
    sk.open_answer_window(duration=15.0, now=T0)
    result = final(sk, "tungsten", "S1",
                   start=T0 + 13.0, end=T0 + 14.5, now=T0 + 17.0)
    assert result["candidate_recorded"] is True
    assert "Rhonda" in sk.answer_candidates


def test_replay_path_bypasses_gate_and_membership():
    """Early-buzz replay (assume_in_window): the segment passed the gate
    at original ingestion; the buffered wait for window open inflates its
    apparent lag and its spoken time precedes opened_at by design.
    Neither may reject it on replay."""
    sk = make_sk()
    sk.start_question(None)
    sk.open_answer_window(duration=15.0, now=T0)
    result = sk.on_transcript_segment(
        text="the nile",
        speaker_label="S1",
        is_final=True,
        segment_start_time=T0 - 25.0,
        segment_end_time=T0 - 23.5,
        now=T0 + 30.0,
        assume_in_window=True,
    )
    assert result["quarantined"] is False
    assert result["candidate_recorded"] is True
    assert sk.quarantined_segments == []


def test_closed_window_admits_nothing():
    sk = make_sk()
    sk.start_question(None)
    sk.open_answer_window(duration=15.0, now=T0)
    sk.close_answer_window()
    result = final(sk, "tungsten", "S1",
                   start=T0 + 2.0, end=T0 + 3.0, now=T0 + 3.2)
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}
