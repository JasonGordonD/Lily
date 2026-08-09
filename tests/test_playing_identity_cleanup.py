"""P0-E3 migration is narrow, idempotent and preserves session evidence."""

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "025_cleanup_playing_identity.sql"
).read_text(encoding="utf-8")


def test_cleanup_retires_rival_centroid_instead_of_merging_it():
    assert "SET status = 'retired'" in MIGRATION
    assert "sample_count = 1" in MIGRATION
    assert "WHERE session_id = 'lily-9337B1-331ff234'" in MIGRATION
    # The borderline sample is never folded into canonical Rami blindly.
    assert "UPDATE lily_voice_identity" in MIGRATION
    assert "grp_0b07f989673dcf11e62da96343a39fd4006c1405" not in MIGRATION


def test_cleanup_deletes_only_proven_playing_derivatives():
    assert "lower(player_name) = 'playing'" in MIGRATION
    assert "lower(COALESCE(winner, '')) = 'playing'" in MIGRATION
    # Generated PKs/UUIDs vary by environment and are never migration keys.
    assert "WHERE id IN (" not in MIGRATION
    assert "::uuid" not in MIGRATION


def test_cleanup_preserves_session_audit_rows():
    for table in (
        "lily_sessions",
        "lily_transcripts",
        "lily_answers",
        "lily_session_reports",
        "lily_addressee_log",
    ):
        assert f"DELETE FROM {table}" not in MIGRATION
