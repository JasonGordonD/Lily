"""WO-LILY-VOICE-IDENTITY-001 — agent-side enroll/match/forget wiring.

The orchestration that makes "know my voice" real: match a joining voice at
session start (promote its memory on a confident hit), fold the session's
voice into the group's centroid at close, and retire it on forget. The
embedder, the captured-audio probe, and supabase are injected fakes here —
the one live-infra seam (a track frame sink filling the PCM buffer) is
mocked so the decision logic is fully exercised.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_persistence
import lily_voice_embedder
import lily_voice_identity
from lily_agent import LilyGame, lily_claim_voice_probe
from lily_scorekeeper import LilyScorekeeper


# -- fake supabase (reused shape from the persistence tests) -------------------


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
    g = LilyGame.__new__(LilyGame)
    g.sk = LilyScorekeeper("vi")
    g.supabase = sb
    g.group_id = "voiceA"
    g.device_identity_verified = False
    g.forget_state = None
    g._voice_identity_pcm = [0.1, 0.2, 0.3]  # injected probe
    return g


def _enable(monkeypatch, *, available=True, embedding=None):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    monkeypatch.setattr(lily_config, "voice_identity_model_tag", lambda: TAG)
    monkeypatch.setattr(
        lily_voice_embedder, "lily_voice_embedder_available", lambda: available
    )
    monkeypatch.setattr(
        lily_voice_embedder, "lily_extract_embedding",
        lambda samples, sample_rate=16000: embedding,
    )


# -- inert-until-ready --------------------------------------------------------


def test_enroll_noop_when_embedder_unavailable(monkeypatch):
    sb = _FakeSB()
    _enable(monkeypatch, available=False)
    g = _game(sb)
    g.identity_persistence_allowed = lambda: True
    assert _run(g._voice_identity_enroll_at_close()) is False
    assert sb.store[lily_persistence.VOICE_IDENTITY_TABLE] == []


def test_enroll_noop_without_audio_probe(monkeypatch):
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])
    g = _game(sb)
    g.identity_persistence_allowed = lambda: True
    g._voice_identity_pcm = None  # no captured audio yet
    assert _run(g._voice_identity_enroll_at_close()) is False


# -- enroll folds the centroid ------------------------------------------------


def test_enroll_writes_centroid(monkeypatch):
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])
    g = _game(sb)
    g.identity_persistence_allowed = lambda: True
    assert _run(g._voice_identity_enroll_at_close()) is True
    rows = sb.store[lily_persistence.VOICE_IDENTITY_TABLE]
    assert len(rows) == 1
    assert rows[0]["group_id"] == "voiceA"
    assert rows[0]["sample_count"] == 1


def test_enroll_skipped_when_persistence_disallowed(monkeypatch):
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])
    g = _game(sb)
    g.identity_persistence_allowed = lambda: False  # forget in progress
    assert _run(g._voice_identity_enroll_at_close()) is False
    assert sb.store[lily_persistence.VOICE_IDENTITY_TABLE] == []


# -- match at start promotes a recognized voice -------------------------------


def test_match_promotes_recognized_group(monkeypatch):
    sb = _FakeSB()
    # Seed a stored identity for a DIFFERENT group whose centroid the probe
    # will match.
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="returning_table", centroid=[1.0, 0.0, 0.0],
        sample_count=5, model_tag=TAG))
    _enable(monkeypatch, available=True, embedding=[0.99, 0.02, 0.0])
    g = _game(sb)
    promoted = {}

    async def fake_stage(gid, source):
        promoted["staged"] = gid; return True

    async def fake_promote(trigger):
        promoted["promoted"] = True

    g.stage_device_candidate = fake_stage
    g._promote_device_candidate = fake_promote

    assert _run(g._voice_identity_match_at_start()) is True
    assert promoted["staged"] == "returning_table"
    assert promoted.get("promoted") is True


def test_match_noop_when_no_confident_hit(monkeypatch):
    sb = _FakeSB()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="someone_else", centroid=[0.0, 1.0, 0.0],
        sample_count=5, model_tag=TAG))
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])  # orthogonal
    g = _game(sb)
    g.stage_device_candidate = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not stage on a non-match"))
    assert _run(g._voice_identity_match_at_start()) is False


def test_match_noop_when_already_verified(monkeypatch):
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])
    g = _game(sb)
    g.device_identity_verified = True  # already recognized this session
    assert _run(g._voice_identity_match_at_start()) is False


def test_match_attempt_waits_for_probe_and_then_schedules(monkeypatch):
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])
    g = _game(sb)
    g._voice_identity_pcm = None
    g._voice_identity_attempted = False
    calls = []

    async def fake_match():
        calls.append("match")
        return False

    g._voice_identity_match_at_start = fake_match

    async def scenario():
        # The early final must not consume the session's only attempt.
        assert g.maybe_start_voice_identity_match() is False
        assert g._voice_identity_attempted is False
        g._voice_identity_pcm = [0.1, 0.2, 0.3]
        assert g.maybe_start_voice_identity_match() is True
        await asyncio.sleep(0)
        assert g.maybe_start_voice_identity_match() is False

    _run(scenario())
    assert calls == ["match"]


def test_production_image_activates_and_prewarms_voice_identity():
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements-voice-identity.txt" in dockerfile
    assert "download.pytorch.org/whl/cpu" in dockerfile
    assert "lily_voice_embedder_available()" in dockerfile


def test_voice_probe_claim_is_independent_and_single_shot():
    game = type("Game", (), {
        "_voice_identity_ready": lambda self: True,
        "_voice_probe_forked": False,
    })()
    assert lily_claim_voice_probe(game) is True
    assert lily_claim_voice_probe(game) is False


# -- forget retires the voiceprint --------------------------------------------


def test_retire_excludes_from_future_match(monkeypatch):
    sb = _FakeSB()
    _run(lily_persistence.lily_upsert_voice_identity(
        sb, group_id="voiceA", centroid=[1.0, 0.0, 0.0], sample_count=3,
        model_tag=TAG))
    assert _run(lily_persistence.lily_retire_voice_identity(sb, "voiceA")) is True
    # A later match load sees no active rows for the forgotten voice.
    assert _run(lily_persistence.lily_load_voice_identities(sb, TAG)) == []


# -- orphan-centroid guard -----------------------------------------------------


def test_enroll_never_founds_an_identity_on_a_throwaway_room_name(monkeypatch):
    """The voiceprint was being SHREDDED, not lost. enroll_at_close wrote to
    self.group_id unconditionally, so a session whose group resolution had
    fallen back to the ROOM NAME minted a fresh 1-sample orphan instead of
    adding to the real centroid. An orphan keyed to a room name can never be
    matched TO — that room never recurs — so it survives only as an extra
    candidate thinning the margin check for every genuine match after it.

    Live 2026-08-08: three rows where there should have been one, while the
    operator was telling Lily she ought to know his voice."""
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])
    g = _game(sb)
    g.group_id = "lily-1D27C8-974ff7ce"
    g.group_id_source = "room_name"
    g.identity_persistence_allowed = lambda: True
    # Nothing stored, so nothing to match -> the sample is DROPPED.
    assert _run(g._voice_identity_enroll_at_close()) is False
    assert sb.store[lily_persistence.VOICE_IDENTITY_TABLE] == []


def test_enroll_folds_into_the_identity_the_voice_matches(monkeypatch):
    """On a throwaway group the BIOMETRIC decides where its sample lands —
    it is the signature, so a confident match redirects enrollment into the
    real identity instead of founding a rival one beside it."""
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])
    sb.store[lily_persistence.VOICE_IDENTITY_TABLE] = [{
        "id": "vi-real-1", "group_id": "grp_real",
        "centroid": [1.0, 0.0, 0.0],
        "sample_count": 4, "model_tag": TAG, "status": "active",
    }]
    g = _game(sb)
    g.group_id = "lily-THROWAWAY-0001"
    g.group_id_source = "room_name"
    g.identity_persistence_allowed = lambda: True
    assert _run(g._voice_identity_enroll_at_close()) is True
    rows = sb.store[lily_persistence.VOICE_IDENTITY_TABLE]
    assert len(rows) == 1, "no rival identity beside the real one"
    assert rows[0]["group_id"] == "grp_real"
    assert rows[0]["sample_count"] == 5, "5th sample, not a new 1-sample orphan"


def test_a_real_group_id_may_still_found_its_first_centroid(monkeypatch):
    """The guard is narrow on purpose. A group id off participant metadata
    recurs, so it CAN be matched to later and is a legitimate home for a
    first centroid. Only the room-name fallback is barred."""
    sb = _FakeSB()
    _enable(monkeypatch, available=True, embedding=[1.0, 0.0, 0.0])
    g = _game(sb)
    g.group_id = "grp_0b07f989"
    g.group_id_source = "participant_metadata"
    g.identity_persistence_allowed = lambda: True
    assert _run(g._voice_identity_enroll_at_close()) is True
    rows = sb.store[lily_persistence.VOICE_IDENTITY_TABLE]
    assert rows[0]["group_id"] == "grp_0b07f989"
