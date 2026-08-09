"""
Image content gate (OR amendment W1): Exa-retrieved images had no
moderation path — the conservative source filter checks WHERE an image
comes from, not WHAT it shows, and one bad cached image serves forever.
The reasoning node now approves image-vs-question BEFORE anything is
cached; rejection and failure both degrade to text-only with a visible
attempt row. Fail closed.
"""

import asyncio

import lily_images
import lily_search
import lily_imagegen


class _FakeStorageBucket:
    def __init__(self):
        self.uploads = []

    def upload(self, path, data, opts=None, file_options=None):
        self.uploads.append(path)
        return {"path": path}

    def get_public_url(self, path):
        return f"https://cdn.example/{path}"


class _FakeStorage:
    def __init__(self):
        self.bucket = _FakeStorageBucket()

    def from_(self, name):
        return self.bucket


class _FakeTable:
    def __init__(self, sink):
        self.sink = sink

    def insert(self, row):
        self.sink.append(row)
        return self

    def update(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        class _R:
            data = []
        return _R()


class _FakeSupabase:
    def __init__(self):
        self.storage = _FakeStorage()
        self.attempt_rows = []

    def table(self, name):
        return _FakeTable(self.attempt_rows)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_pipeline(monkeypatch, image_bytes=b"\x89PNG fake"):
    async def fake_find(entity):
        return {
            "image_url": "https://en.wikipedia.org/x.jpg",
            "page_url": "https://en.wikipedia.org/wiki/x",
        }

    async def fake_fetch(url, timeout=None, **kw):
        return image_bytes, "image/jpeg"

    monkeypatch.setattr(lily_search, "lily_find_real_entity_image", fake_find)
    monkeypatch.setattr(lily_images, "lily_fetch_image_bytes", fake_fetch)


def test_gate_rejection_blocks_cache_and_writes_rejected_row(monkeypatch):
    _patch_pipeline(monkeypatch)
    supabase = _FakeSupabase()

    async def deny(image_bytes, content_type, entity):
        return False, "not the entity"

    q = _run(lily_search.lily_build_real_entity_picture_question(
        supabase, index=0, session_id="s1", approve=deny,
    ))
    assert q is None
    assert supabase.storage.bucket.uploads == []  # nothing cached
    rejected = [r for r in supabase.attempt_rows if r.get("status") == "rejected"]
    assert rejected and "content gate" in rejected[0]["failure_reason"]


def test_gate_approval_caches_and_returns_question(monkeypatch):
    _patch_pipeline(monkeypatch)
    supabase = _FakeSupabase()

    async def allow(image_bytes, content_type, entity):
        return True, "clearly the entity"

    q = _run(lily_search.lily_build_real_entity_picture_question(
        supabase, index=0, session_id="s1", approve=allow,
    ))
    assert q is not None
    assert q["image_source"] == "web"
    assert q["image_url"].startswith("https://cdn.example/")
    assert supabase.storage.bucket.uploads  # cached exactly after approval


def test_gate_applies_to_real_or_imagined_real_branch(monkeypatch):
    _patch_pipeline(monkeypatch)
    supabase = _FakeSupabase()

    async def deny(image_bytes, content_type, entity):
        return False, "inappropriate"

    q = _run(lily_imagegen.lily_build_real_or_imagined_question(
        supabase, index=0, session_id="s1", approve=deny,  # even index = REAL
    ))
    assert q is None
    assert supabase.storage.bucket.uploads == []
    assert any(r.get("status") == "rejected" for r in supabase.attempt_rows)


def test_no_approver_proceeds_with_visible_skip(monkeypatch, caplog):
    # Test/direct-call path: no approver means no gate — but never silently.
    _patch_pipeline(monkeypatch)
    supabase = _FakeSupabase()
    with caplog.at_level("WARNING"):
        q = _run(lily_search.lily_build_real_entity_picture_question(
            supabase, index=0, session_id="s1",
        ))
    assert q is not None
    assert any("CONTENT_GATE_SKIPPED" in r.message for r in caplog.records)


def test_reasoning_gate_fails_closed_on_error(monkeypatch):
    # approve_entity_image rejects on ANY internal failure.
    import lily_reasoning

    node = lily_reasoning.LilyReasoning.__new__(lily_reasoning.LilyReasoning)

    async def _boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        lily_reasoning.lily_vision,
        "lily_classify_image_bytes",
        _boom,
    )
    approved, reason = _run(
        node.approve_entity_image(b"bytes", "image/jpeg", "Eiffel Tower")
    )
    assert approved is False
    assert "gate error" in reason
