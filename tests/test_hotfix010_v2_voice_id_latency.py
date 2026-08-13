"""WO-LILY-HOTFIX-010 V2 — voice-identity latency instrumentation + the
centroid-pool fetch moved off the recognition critical path.

Three things this locks:
  1. The two stage timings (embed_ms, resolve_ms) are stamped on the game and
     logged once per session — the number that was missing for weeks.
  2. When the centroid pool is preloaded at connect, the first-utterance match
     reads it from memory and does NO DB round-trip; a cold first utterance
     (preload not yet ready) still falls back to the inline fetch so the
     feature is never silently inert.
  3. V2's masking verify: while the probe is outstanding, no memory-status
     narration is permitted (unchanged by V2).

Embedder, captured-audio probe, and supabase are injected fakes (same shape as
test_voice_identity_wiring.py).
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_persistence
import lily_voice_embedder
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self._store, self._table = store, table
        self._filters, self._limit = [], None
        self._insert = self._update = None

    def select(self, *a, **k): return self
    def eq(self, c, v): self._filters.append((c, v)); return self
    def limit(self, n): self._limit = n; return self
    def insert(self, r): self._insert = r; return self
    def update(self, p): self._update = p; return self

    def _m(self, r): return all(r.get(c) == v for c, v in self._filters)

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._insert is not None:
            n = dict(self._insert); n.setdefault("id", f"id_{len(rows)}")
            rows.append(n); return _Result([n])
        if self._update is not None:
            for r in [r for r in rows if self._m(r)]:
                r.update(self._update)
            return _Result([])
        m = [r for r in rows if self._m(r)]
        return _Result(m[: self._limit] if self._limit else m)


class _FakeSB:
    def __init__(self): self.store = {lily_persistence.VOICE_IDENTITY_TABLE: []}
    def table(self, n): return _Query(self.store, n)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


TAG = "ecapa-192-v1"


def _game(sb):
    g = LilyGame.bare()
    g.sk = LilyScorekeeper("vi")
    g.supabase = sb
    g.group_id = "voiceA"
    g.group_id_source = "participant_metadata"
    g.device_identity_verified = False
    g.forget_state = None
    g._voice_identity_pcm = [0.1, 0.2, 0.3]
    return g


def _enable(monkeypatch, *, available=True, embedding=None):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    monkeypatch.setattr(lily_config, "voice_identity_model_tag", lambda: TAG)
    monkeypatch.setattr(
        lily_voice_embedder, "lily_voice_embedder_available", lambda: available
    )
    monkeypatch.setattr(
        lily_voice_embedder, "lily_voice_embedder_loaded", lambda: available
    )
    monkeypatch.setattr(
        lily_voice_embedder, "lily_voice_embedder_load_attempted", lambda: True
    )
    monkeypatch.setattr(
        lily_voice_embedder, "lily_extract_embedding",
        lambda samples, sample_rate=16000: embedding,
    )


async def _stage_true(gid, source):
    return True


async def _promote_noop(trigger):
    return None


# -- 1. the two stage timings are stamped + logged ----------------------------


def test_latency_stamps_embed_and_resolve_ms(monkeypatch, caplog):
    sb = _FakeSB()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="returning_table", centroid=[1.0, 0.0, 0.0],
        sample_count=5, model_tag=TAG))
    _enable(monkeypatch, available=True, embedding=[0.99, 0.02, 0.0])
    g = _game(sb)
    g.stage_device_candidate = _stage_true
    g._promote_device_candidate = _promote_noop
    # The frame sink stamps t0 at the first match_ready crossing; emulate it.
    g._voice_identity_match_t0 = time.monotonic()
    with caplog.at_level(logging.INFO):
        assert _run(g._voice_identity_match_at_start()) is True
    # Both deltas stamped on the game (what the session-close fold reads into
    # pipeline_latency).
    assert isinstance(g._voice_id_embed_ms, float)
    assert isinstance(g._voice_id_resolve_ms, float)
    assert g._voice_id_embed_ms >= 0.0
    assert g._voice_id_resolve_ms >= 0.0
    assert any(
        "LILY_VOICE_ID | LATENCY | embed_ms=" in r.getMessage()
        and "resolve_ms=" in r.getMessage()
        for r in caplog.records
    )


def test_latency_not_stamped_without_t0(monkeypatch):
    # No frame-sink stamp (t0 absent) -> no latency attrs, and the match is
    # otherwise unaffected.
    sb = _FakeSB()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="returning_table", centroid=[1.0, 0.0, 0.0],
        sample_count=5, model_tag=TAG))
    _enable(monkeypatch, available=True, embedding=[0.99, 0.02, 0.0])
    g = _game(sb)
    g.stage_device_candidate = _stage_true
    g._promote_device_candidate = _promote_noop
    assert _run(g._voice_identity_match_at_start()) is True
    assert getattr(g, "_voice_id_embed_ms", None) is None
    assert getattr(g, "_voice_id_resolve_ms", None) is None


# -- 2. pool preloaded at connect; match does NO DB round-trip ----------------


def test_preload_populates_pool_at_connect(monkeypatch):
    sb = _FakeSB()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="returning_table", centroid=[1.0, 0.0, 0.0],
        sample_count=5, model_tag=TAG))
    _enable(monkeypatch, available=True)
    g = _game(sb)

    async def scenario():
        g._preload_voice_identities()
        # let the fire-and-forget load (to_thread) settle
        for _ in range(100):
            if getattr(g, "_voice_identity_pool_loaded", False):
                break
            await asyncio.sleep(0.01)

    _run(scenario())
    assert g._voice_identity_pool_loaded is True
    assert [p["group_id"] for p in g._voice_identity_pool] == ["returning_table"]


def test_match_uses_preloaded_pool_no_db_roundtrip(monkeypatch):
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[0.99, 0.02, 0.0])
    g = _game(sb)
    # Pool already in memory from the connect-time preload.
    g._voice_identity_pool = [
        {"group_id": "returning_table", "centroid": [1.0, 0.0, 0.0],
         "sample_count": 5}
    ]
    g._voice_identity_pool_loaded = True
    g.stage_device_candidate = _stage_true
    g._promote_device_candidate = _promote_noop

    def _boom(*a, **k):
        raise AssertionError(
            "match must NOT hit the DB when the pool is preloaded")

    monkeypatch.setattr(lily_persistence, "lily_load_voice_identities", _boom)
    assert _run(g._voice_identity_match_at_start()) is True


def test_match_cold_path_falls_back_to_db(monkeypatch):
    # First utterance beat the preload: the inline fetch keeps the feature
    # live rather than silently inert.
    sb = _FakeSB()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="returning_table", centroid=[1.0, 0.0, 0.0],
        sample_count=5, model_tag=TAG))
    _enable(monkeypatch, available=True, embedding=[0.99, 0.02, 0.0])
    g = _game(sb)
    # _voice_identity_pool_loaded absent -> cold path
    g.stage_device_candidate = _stage_true
    g._promote_device_candidate = _promote_noop
    assert _run(g._voice_identity_match_at_start()) is True


# -- 3. masking verify: no memory-status narration while probe outstanding ----


def test_masking_holds_while_probe_outstanding(monkeypatch):
    sb = _FakeSB()
    _enable(monkeypatch, available=True)
    g = _game(sb)
    g._returner_claim_seen = False
    g.memory_block = None
    g.device_candidate_group_id = None
    # probe not resolved yet
    assert g.identity_probe_outstanding() is True
    assert g.can_claim_empty_memory() is False
