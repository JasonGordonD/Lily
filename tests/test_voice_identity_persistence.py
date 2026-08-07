"""WO-LILY-VOICE-IDENTITY-001 — persistence + config + embedder seam.

Checkpoint layer (matcher core is separate, test_voice_identity.py): the
durable-centroid store (load/upsert/retire against a fake supabase mirroring
the lily_voice_identity DDL), the config defaults, and the embedder's
graceful-degradation contract (inert with no ML dep present, as in CI).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_persistence
import lily_voice_embedder


# -- fake supabase (select/eq/limit/insert/update/execute) --------------------


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self._store, self._table = store, table
        self._filters, self._limit = [], None
        self._insert, self._update = None, None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val)); return self

    def limit(self, n):
        self._limit = n; return self

    def insert(self, row):
        self._insert = row; return self

    def update(self, patch):
        self._update = patch; return self

    def _match(self, r):
        return all(r.get(c) == v for c, v in self._filters)

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._insert is not None:
            new = dict(self._insert); new.setdefault("id", f"id_{len(rows)}")
            rows.append(new); return _Result([new])
        if self._update is not None:
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._update)
            return _Result(hit)
        matched = [r for r in rows if self._match(r)]
        if self._limit is not None:
            matched = matched[: self._limit]
        return _Result(matched)


class _FakeSupabase:
    def __init__(self):
        self.store = {lily_persistence.VOICE_IDENTITY_TABLE: []}

    def table(self, name):
        return _Query(self.store, name)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


TAG = "ecapa-192-v1"


# -- persistence: enroll -> load -> re-enroll(update) -> retire ---------------


def test_enroll_then_load_roundtrips():
    sb = _FakeSupabase()
    ok = _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="g1", centroid=[0.1, 0.2, 0.3], sample_count=1, model_tag=TAG
    ))
    assert ok is True
    loaded = _run(lily_persistence.lily_load_voice_identities(sb, TAG))
    assert len(loaded) == 1
    assert loaded[0]["group_id"] == "g1"
    assert loaded[0]["centroid"] == [0.1, 0.2, 0.3]
    assert loaded[0]["sample_count"] == 1


def test_second_enrollment_updates_in_place():
    sb = _FakeSupabase()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="g1", centroid=[0.1, 0.2], sample_count=1, model_tag=TAG))
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="g1", centroid=[0.5, 0.6], sample_count=2, model_tag=TAG))
    rows = sb.store[lily_persistence.VOICE_IDENTITY_TABLE]
    assert len(rows) == 1  # updated, not duplicated
    assert rows[0]["sample_count"] == 2
    assert rows[0]["centroid"] == [0.5, 0.6]


def test_load_filters_by_model_tag_and_status():
    sb = _FakeSupabase()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="g1", centroid=[0.1], sample_count=1, model_tag=TAG))
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="g2", centroid=[0.2], sample_count=1, model_tag="other-model"))
    only_tag = _run(lily_persistence.lily_load_voice_identities(sb, TAG))
    assert [r["group_id"] for r in only_tag] == ["g1"]


def test_retire_removes_from_active_load():
    sb = _FakeSupabase()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="g1", centroid=[0.1, 0.2], sample_count=1, model_tag=TAG))
    assert _run(lily_persistence.lily_retire_voice_identity(sb, "g1")) is True
    # Retired rows are excluded from the active match pool.
    assert _run(lily_persistence.lily_load_voice_identities(sb, TAG)) == []
    # Row still exists (provenance), just status='retired'.
    assert sb.store[lily_persistence.VOICE_IDENTITY_TABLE][0]["status"] == "retired"


def test_persistence_defensive_on_none_client():
    assert _run(lily_persistence.lily_load_voice_identities(None, TAG)) == []
    assert _run(lily_persistence.lily_upsert_voice_identity(
        None, group_id="g", centroid=[1], sample_count=1, model_tag=TAG)) is False
    assert _run(lily_persistence.lily_retire_voice_identity(None, "g")) is False


def test_parse_vector_handles_pgvector_string():
    # supabase-py returns pgvector as a JSON-ish string.
    assert lily_persistence._parse_vector("[0.1, 0.2, 0.3]") == [0.1, 0.2, 0.3]
    assert lily_persistence._parse_vector([1, 2]) == [1.0, 2.0]
    assert lily_persistence._parse_vector(None) is None
    assert lily_persistence._parse_vector("garbage") is None


# -- config defaults -----------------------------------------------------------


def test_config_defaults():
    import os
    for k in [
        "LILY_VOICE_IDENTITY_ENABLED", "LILY_VOICE_IDENTITY_MODEL_TAG",
        "LILY_VOICE_IDENTITY_MATCH_THRESHOLD", "LILY_VOICE_IDENTITY_MATCH_MARGIN",
        "LILY_VOICE_IDENTITY_ENROLL_MIN_SPEECH_SECONDS",
    ]:
        os.environ.pop(k, None)
    assert lily_config.voice_identity_enabled() is True
    assert lily_config.voice_identity_model_tag() == "ecapa-192-v1"
    assert lily_config.voice_identity_match_threshold() == 0.75
    assert lily_config.voice_identity_match_margin() == 0.06
    assert lily_config.voice_identity_enroll_min_speech_seconds() == 8.0


# -- embedder seam: inert without the ML dependency (CI condition) -------------


def test_embedder_unavailable_without_model_is_graceful():
    # No torch/speechbrain in the test image: available() is False and
    # extraction returns None, never raises — the feature no-ops.
    assert lily_voice_embedder.lily_voice_embedder_available() is False
    assert lily_voice_embedder.lily_extract_embedding([0.0, 0.1, 0.2]) is None
    assert lily_voice_embedder.lily_extract_embedding(None) is None
