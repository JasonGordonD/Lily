"""Tests for lily_images (WO-LILY-OMNIBUS-002 sub-agent H): storage
pathing, upload/fetch plumbing, cache-first bank wiring, attempt rows."""

import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_images


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fakes — supabase storage + table clients (no network anywhere)
# ---------------------------------------------------------------------------

class FakeStorageBucket:
    def __init__(self, fail_upload=None):
        self.uploads = []
        self.fail_upload = fail_upload

    def upload(self, path, data, options):
        if self.fail_upload is not None:
            raise self.fail_upload
        self.uploads.append((path, data, options))
        return {"path": path}

    def get_public_url(self, path):
        return f"https://cdn.example/storage/v1/object/public/lily-images/{path}"


class FakeStorage:
    def __init__(self, bucket):
        self.bucket = bucket
        self.requested = []

    def from_(self, name):
        self.requested.append(name)
        return self.bucket


class FakeQuery:
    def __init__(self, table):
        self.table = table
        self._filters = {}
        self._update_payload = None

    def select(self, *_a, **_k):
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def insert(self, payload):
        self.table.inserts.append(payload)
        self._insert = True
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        if self._update_payload is not None:
            self.table.updates.append((self._filters, self._update_payload))

            class R:
                data = [self._update_payload]
            return R()

        class R:
            data = self.table.rows
        return R()


class FakeTable:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.updates = []
        self.inserts = []

    def __call__(self):
        return FakeQuery(self)


class FakeSupabase:
    def __init__(self, bucket=None, rows=None):
        self.storage = FakeStorage(bucket or FakeStorageBucket())
        self._table = FakeTable(rows)

    def table(self, name):
        self._last_table_name = name
        return FakeQuery(self._table)


# ---------------------------------------------------------------------------
# Storage pathing — content-addressed {source}/{sha1}.{ext}
# ---------------------------------------------------------------------------

def test_storage_path_is_content_addressed():
    data = b"png-bytes"
    sha = hashlib.sha1(data).hexdigest()
    assert lily_images.lily_image_storage_path("generated", data, "png") == (
        f"generated/{sha}.png"
    )
    assert lily_images.lily_image_storage_path("web", data, "jpg") == (
        f"web/{sha}.jpg"
    )


def test_storage_path_same_bytes_same_path():
    a = lily_images.lily_image_storage_path("web", b"xyz", "png")
    b = lily_images.lily_image_storage_path("web", b"xyz", "png")
    assert a == b


def test_ext_for_content_type():
    assert lily_images.lily_image_ext("image/png") == "png"
    assert lily_images.lily_image_ext("image/jpeg; charset=binary") == "jpg"
    assert lily_images.lily_image_ext("image/webp") == "webp"
    assert lily_images.lily_image_ext("text/html") is None
    assert lily_images.lily_image_ext(None) is None


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def test_upload_returns_public_url_on_bucket_path():
    supabase = FakeSupabase()
    data = b"image-bytes"
    url = run(lily_images.lily_upload_image_bytes(
        supabase, data, source="generated", content_type="image/png"
    ))
    sha = hashlib.sha1(data).hexdigest()
    assert url == (
        "https://cdn.example/storage/v1/object/public/lily-images/"
        f"generated/{sha}.png"
    )
    assert supabase.storage.requested == [lily_images.LILY_IMAGES_BUCKET]
    (path, sent, options) = supabase.storage.bucket.uploads[0]
    assert path == f"generated/{sha}.png"
    assert sent == data
    assert options["content-type"] == "image/png"


def test_upload_rejects_bad_source_and_empty_bytes():
    supabase = FakeSupabase()
    assert run(lily_images.lily_upload_image_bytes(
        supabase, b"x", source="screenshot"
    )) is None
    assert run(lily_images.lily_upload_image_bytes(
        supabase, b"", source="web"
    )) is None
    assert run(lily_images.lily_upload_image_bytes(
        None, b"x", source="web"
    )) is None
    assert supabase.storage.bucket.uploads == []


def test_upload_already_exists_is_a_cache_hit():
    # Content-addressed path: an already-exists conflict returns the
    # public URL anyway — same bytes, same object.
    bucket = FakeStorageBucket(fail_upload=RuntimeError("The resource already exists"))
    supabase = FakeSupabase(bucket=bucket)
    url = run(lily_images.lily_upload_image_bytes(
        supabase, b"dup", source="web", content_type="image/jpeg"
    ))
    assert url is not None and "/web/" in url


def test_upload_other_error_returns_none():
    bucket = FakeStorageBucket(fail_upload=RuntimeError("permission denied"))
    supabase = FakeSupabase(bucket=bucket)
    assert run(lily_images.lily_upload_image_bytes(
        supabase, b"x", source="web"
    )) is None


def test_upload_size_cap():
    supabase = FakeSupabase()
    big = b"x" * (lily_images.MAX_IMAGE_BYTES + 1)
    assert run(lily_images.lily_upload_image_bytes(
        supabase, big, source="web"
    )) is None


# ---------------------------------------------------------------------------
# Fetch-URL -> bucket
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, content=b"img", content_type="image/jpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class _FakeAsyncClient:
    response = _FakeResponse()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return type(self).response


def test_fetch_url_to_bucket(monkeypatch):
    monkeypatch.setattr(lily_images.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.response = _FakeResponse(content=b"jpeg-bytes")
    supabase = FakeSupabase()
    url = run(lily_images.lily_fetch_url_to_bucket(
        supabase, "https://upload.wikimedia.org/x.jpg", source="web"
    ))
    sha = hashlib.sha1(b"jpeg-bytes").hexdigest()
    assert url.endswith(f"web/{sha}.jpg")


def test_fetch_rejects_non_image_content_type(monkeypatch):
    monkeypatch.setattr(lily_images.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.response = _FakeResponse(content_type="text/html")
    supabase = FakeSupabase()
    assert run(lily_images.lily_fetch_url_to_bucket(
        supabase, "https://example.com/page", source="web"
    )) is None
    assert supabase.storage.bucket.uploads == []


def test_fetch_rejects_bad_status_and_bad_url(monkeypatch):
    monkeypatch.setattr(lily_images.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.response = _FakeResponse(status_code=404)
    supabase = FakeSupabase()
    assert run(lily_images.lily_fetch_url_to_bucket(
        supabase, "https://example.com/x.jpg"
    )) is None
    assert run(lily_images.lily_fetch_url_to_bucket(
        supabase, "ftp://example.com/x.jpg"
    )) is None
    assert run(lily_images.lily_fetch_url_to_bucket(supabase, "")) is None


# ---------------------------------------------------------------------------
# Cache-first bank wiring
# ---------------------------------------------------------------------------

def test_bank_row_id_parses_kb_ids_only():
    assert lily_images.lily_bank_row_id("kb_42") == 42
    assert lily_images.lily_bank_row_id("q_0042") is None
    assert lily_images.lily_bank_row_id("kb_abc") is None
    assert lily_images.lily_bank_row_id(None) is None


def test_cached_bank_image_hit():
    supabase = FakeSupabase(rows=[{
        "image_url": "https://cdn.example/web/abc.jpg",
        "image_source": "web",
        "image_license_note": "web image via Exa: page=https://en.wikipedia.org/x",
    }])
    cached = run(lily_images.lily_cached_bank_image(supabase, "kb_7"))
    assert cached["image_url"] == "https://cdn.example/web/abc.jpg"
    assert cached["image_source"] == "web"
    assert "Exa" in cached["image_license_note"]


def test_cached_bank_image_miss_and_non_bank():
    supabase = FakeSupabase(rows=[{"image_url": None}])
    assert run(lily_images.lily_cached_bank_image(supabase, "kb_7")) is None
    assert run(lily_images.lily_cached_bank_image(supabase, "q_0007")) is None
    assert run(lily_images.lily_cached_bank_image(None, "kb_7")) is None


def test_save_bank_image_writes_back_for_kb_rows_only():
    supabase = FakeSupabase()
    ok = run(lily_images.lily_save_bank_image(
        supabase, "kb_9",
        image_url="https://cdn.example/web/abc.jpg",
        image_source="web",
        image_license_note="note",
    ))
    assert ok is True
    filters, payload = supabase._table.updates[0]
    assert filters == {"id": 9}
    assert payload["image_url"] == "https://cdn.example/web/abc.jpg"
    assert payload["image_source"] == "web"
    # Generated (non-bank) questions have no row to cache against.
    assert run(lily_images.lily_save_bank_image(
        supabase, "q_0001", image_url="u", image_source="generated"
    )) is False


# ---------------------------------------------------------------------------
# Visible attempt rows (no-silent-crash)
# ---------------------------------------------------------------------------

def test_attempt_row_written_with_failure_reason():
    supabase = FakeSupabase()
    run(lily_images.lily_record_image_attempt(
        supabase,
        session_id="room-1",
        question_id="q_0001",
        source="generated",
        prompt="a plausible photo",
        status=lily_images.ATTEMPT_ERROR,
        failure_reason="provider said no: " + "x" * 5000,
        model="gemini-2.5-flash-image",
    ))
    row = supabase._table.inserts[0]
    assert row["status"] == "error"
    assert row["source"] == "generated"
    assert len(row["failure_reason"]) <= 4000
    assert row["model"] == "gemini-2.5-flash-image"


def test_attempt_row_empty_prompt_gets_sentinel():
    supabase = FakeSupabase()
    run(lily_images.lily_record_image_attempt(
        supabase, session_id="s", question_id="q", source="web",
        prompt="", status=lily_images.ATTEMPT_SUCCESS,
        image_url="https://cdn.example/web/a.jpg",
    ))
    row = supabase._table.inserts[0]
    assert row["prompt"] == "(prompt unavailable)"
    assert row["image_url"] == "https://cdn.example/web/a.jpg"


def test_attempt_row_never_raises_without_client():
    run(lily_images.lily_record_image_attempt(
        None, session_id="s", question_id="q", source="web",
        prompt="p", status=lily_images.ATTEMPT_ERROR, failure_reason="boom",
    ))
