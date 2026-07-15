"""Tests for lily_search (WO-LILY-OMNIBUS-002 sub-agent I + K-b): the
Exa/Tavily native lifts, the conservative real-entity image filter
(reject on doubt -> text-only fallback), and the picture-question builder."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import lily_images
import lily_search


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# httpx fakes
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeAsyncClient:
    response = _FakeResponse()
    last_request = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_request = {"url": url, "json": json, "headers": headers}
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    monkeypatch.setattr(lily_search.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.response = _FakeResponse()
    _FakeAsyncClient.last_request = None
    return _FakeAsyncClient


# ---------------------------------------------------------------------------
# Exa lift
# ---------------------------------------------------------------------------

def test_exa_search_parses_results(fake_http):
    fake_http.response = _FakeResponse(payload={"results": [
        {"title": "Eiffel Tower - Wikipedia",
         "url": "https://en.wikipedia.org/wiki/Eiffel_Tower",
         "image": "https://upload.wikimedia.org/eiffel.jpg",
         "highlights": ["the tower"], "text": "wrought-iron lattice tower"},
    ]})
    data = run(lily_search.lily_exa_search("eiffel tower", api_key="k"))
    assert data["results"][0]["image"] == "https://upload.wikimedia.org/eiffel.jpg"
    assert data["results"][0]["title"].startswith("Eiffel Tower")
    sent = fake_http.last_request
    assert sent["url"] == lily_search.EXA_SEARCH_URL
    assert sent["headers"]["x-api-key"] == "k"
    assert 1 <= sent["json"]["numResults"] <= 10


def test_exa_search_without_key_returns_error(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    data = run(lily_search.lily_exa_search("anything"))
    assert "error" in data


def test_exa_search_http_error(fake_http):
    fake_http.response = _FakeResponse(status_code=500, text="boom")
    data = run(lily_search.lily_exa_search("q", api_key="k"))
    assert data == {"error": "HTTP 500"}


# ---------------------------------------------------------------------------
# Tavily lift
# ---------------------------------------------------------------------------

def test_tavily_search_parses_answer_and_results(fake_http):
    fake_http.response = _FakeResponse(payload={
        "answer": "The Bosporus.",
        "results": [
            {"title": "Bosporus", "content": "a strait", "url": "https://x"},
        ],
    })
    data = run(lily_search.lily_tavily_search("bosporus", api_key="k"))
    assert data["answer"] == "The Bosporus."
    assert data["results"][0]["title"] == "Bosporus"
    sent = fake_http.last_request
    assert sent["url"] == lily_search.TAVILY_SEARCH_URL
    assert sent["headers"]["Authorization"] == "Bearer k"


def test_tavily_without_key_returns_error(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert "error" in run(lily_search.lily_tavily_search("anything"))


def test_format_tavily_results_bounded():
    data = {"answer": "A" * 3000, "results": []}
    out = lily_search.lily_format_tavily_results(data, max_chars=100)
    assert len(out) <= 100
    assert lily_search.lily_format_tavily_results({"error": "x"}) == "Search error: x"


def test_web_verification_context_none_on_error(fake_http):
    fake_http.response = _FakeResponse(status_code=403, text="denied")
    assert run(lily_search.lily_web_verification_context(
        "Which strait?", "Bosporus", api_key="k"
    )) is None


def test_web_verification_context_returns_facts(fake_http):
    fake_http.response = _FakeResponse(payload={
        "answer": "The Bosporus separates Europe and Asia.",
        "results": [],
    })
    out = run(lily_search.lily_web_verification_context(
        "Which strait?", "Bosporus", api_key="k"
    ))
    assert "Bosporus" in out


def test_current_events_brief(fake_http):
    fake_http.response = _FakeResponse(payload={
        "answer": "", "results": [
            {"title": "News", "content": "thing happened", "url": "https://n"},
        ],
    })
    out = run(lily_search.lily_current_events_brief("current events", api_key="k"))
    assert "thing happened" in out
    assert run(lily_search.lily_current_events_brief("", api_key="k")) is None


# ---------------------------------------------------------------------------
# Conservative real-entity image filter — reject on doubt
# ---------------------------------------------------------------------------

GOOD_RESULT = {
    "title": "Eiffel Tower - Wikipedia",
    "url": "https://en.wikipedia.org/wiki/Eiffel_Tower",
    "image": "https://upload.wikimedia.org/wikipedia/commons/eiffel.jpg",
    "text": "The Eiffel Tower is a wrought-iron lattice tower in Paris.",
}


def test_filter_accepts_safelisted_unambiguous_candidate():
    cand = lily_search.lily_filter_entity_image_candidate(
        "Eiffel Tower", GOOD_RESULT
    )
    assert cand["image_url"].startswith("https://upload.wikimedia.org/")
    assert cand["page_url"].startswith("https://en.wikipedia.org/")


def test_filter_rejects_offlist_host():
    result = dict(GOOD_RESULT, image="https://someblog.example/eiffel.jpg")
    assert lily_search.lily_filter_entity_image_candidate(
        "Eiffel Tower", result
    ) is None
    result = dict(GOOD_RESULT, url="https://someblog.example/eiffel")
    assert lily_search.lily_filter_entity_image_candidate(
        "Eiffel Tower", result
    ) is None


def test_filter_rejects_missing_or_insecure_image():
    assert lily_search.lily_filter_entity_image_candidate(
        "Eiffel Tower", dict(GOOD_RESULT, image="")
    ) is None
    assert lily_search.lily_filter_entity_image_candidate(
        "Eiffel Tower", dict(GOOD_RESULT, image="http://upload.wikimedia.org/e.jpg")
    ) is None


def test_filter_rejects_ambiguous_page():
    # Not every entity token appears on the page -> ambiguous -> reject.
    result = dict(GOOD_RESULT, title="Tower - Wikipedia",
                  url="https://en.wikipedia.org/wiki/Tower",
                  text="A tower is a tall structure.")
    assert lily_search.lily_filter_entity_image_candidate(
        "Eiffel Tower", result
    ) is None


def test_filter_rejects_non_name_entities():
    # Bare numbers/dates are not picture material (recognizability rule).
    result = dict(GOOD_RESULT, title="1985 - Wikipedia",
                  url="https://en.wikipedia.org/wiki/1985", text="1985")
    assert lily_search.lily_filter_entity_image_candidate("1985", result) is None


def test_find_real_entity_image_first_survivor_wins(fake_http):
    fake_http.response = _FakeResponse(payload={"results": [
        {"title": "Eiffel blog", "url": "https://blog.example/eiffel",
         "image": "https://blog.example/e.jpg", "text": "eiffel tower"},
        GOOD_RESULT,
    ]})
    cand = run(lily_search.lily_find_real_entity_image(
        "Eiffel Tower", api_key="k"
    ))
    assert cand["page_url"].startswith("https://en.wikipedia.org/")


def test_find_real_entity_image_rejects_all_returns_none(fake_http):
    fake_http.response = _FakeResponse(payload={"results": [
        {"title": "Eiffel blog", "url": "https://blog.example/eiffel",
         "image": "https://blog.example/e.jpg", "text": "eiffel tower"},
    ]})
    assert run(lily_search.lily_find_real_entity_image(
        "Eiffel Tower", api_key="k"
    )) is None
    assert run(lily_search.lily_find_real_entity_image("", api_key="k")) is None


# ---------------------------------------------------------------------------
# Real-entity picture question builder — text-only fallback on failure
# ---------------------------------------------------------------------------

class _AttemptCapture:
    def __init__(self):
        self.rows = []

    async def record(self, supabase, **kw):
        self.rows.append(kw)


def test_build_real_entity_question_success(monkeypatch):
    subject = lily_search.REAL_ENTITY_SUBJECTS[0]

    async def fake_find(entity, **kw):
        assert entity == subject["entity"]
        return {"image_url": "https://upload.wikimedia.org/e.jpg",
                "page_url": "https://en.wikipedia.org/wiki/E",
                "title": "E"}

    async def fake_fetch(supabase, url, **kw):
        return "https://cdn.example/lily-images/web/abc.jpg"

    capture = _AttemptCapture()
    monkeypatch.setattr(lily_search, "lily_find_real_entity_image", fake_find)
    monkeypatch.setattr(
        lily_search.lily_images, "lily_fetch_url_to_bucket", fake_fetch
    )
    monkeypatch.setattr(
        lily_search.lily_images, "lily_record_image_attempt", capture.record
    )
    q = run(lily_search.lily_build_real_entity_picture_question(
        object(), index=0, session_id="room-1"
    ))
    assert q["image_source"] == "web"
    assert q["image_url"] == "https://cdn.example/lily-images/web/abc.jpg"
    assert q["canonical_answer"] == subject["entity"]
    assert "page=https://en.wikipedia.org/wiki/E" in q["image_license_note"]
    assert q["prompt"].endswith(f"Name this {subject['kind']}.")
    assert capture.rows[0]["status"] == lily_images.ATTEMPT_SUCCESS


def test_build_real_entity_question_text_only_fallback(monkeypatch):
    async def fake_find(entity, **kw):
        return None  # conservative filter passed on everything

    capture = _AttemptCapture()
    monkeypatch.setattr(lily_search, "lily_find_real_entity_image", fake_find)
    monkeypatch.setattr(
        lily_search.lily_images, "lily_record_image_attempt", capture.record
    )
    q = run(lily_search.lily_build_real_entity_picture_question(
        object(), index=1, session_id="room-1"
    ))
    assert q is None  # caller falls back to the standard text supply
    # A conservative pass is a decision, not an error — no error row.
    assert capture.rows == []


def test_build_real_entity_question_store_failure_writes_error_row(monkeypatch):
    async def fake_find(entity, **kw):
        return {"image_url": "https://upload.wikimedia.org/e.jpg",
                "page_url": "https://en.wikipedia.org/wiki/E", "title": "E"}

    async def fake_fetch(supabase, url, **kw):
        return None  # storage failed

    capture = _AttemptCapture()
    monkeypatch.setattr(lily_search, "lily_find_real_entity_image", fake_find)
    monkeypatch.setattr(
        lily_search.lily_images, "lily_fetch_url_to_bucket", fake_fetch
    )
    monkeypatch.setattr(
        lily_search.lily_images, "lily_record_image_attempt", capture.record
    )
    q = run(lily_search.lily_build_real_entity_picture_question(
        object(), index=2, session_id="room-1"
    ))
    assert q is None
    assert capture.rows[0]["status"] == lily_images.ATTEMPT_ERROR
    assert "fetch/store failed" in capture.rows[0]["failure_reason"]


def test_build_real_entity_question_never_raises(monkeypatch):
    async def fake_find(entity, **kw):
        raise RuntimeError("exa exploded")

    capture = _AttemptCapture()
    monkeypatch.setattr(lily_search, "lily_find_real_entity_image", fake_find)
    monkeypatch.setattr(
        lily_search.lily_images, "lily_record_image_attempt", capture.record
    )
    q = run(lily_search.lily_build_real_entity_picture_question(
        object(), index=3, session_id="room-1"
    ))
    assert q is None
    assert capture.rows[0]["status"] == lily_images.ATTEMPT_ERROR
    assert "exa exploded" in capture.rows[0]["failure_reason"]


# ---------------------------------------------------------------------------
# Import tripwire (pure check; full inspection lives in test_web_guardrails)
# ---------------------------------------------------------------------------

def test_forbid_vocal_import_raises_for_direct_vocal_import():
    # A direct `import lily_search` in lily_agent (module body or lazy,
    # in-function): the first real frame under the machinery is lily_agent.
    with pytest.raises(RuntimeError):
        lily_search.lily_forbid_vocal_import(
            ["lily_search", "importlib._bootstrap", "lily_agent", "main"]
        )


def test_forbid_vocal_import_allows_the_reasoning_seam():
    # The designed path — lily_agent -> lily_reasoning -> lily_search —
    # is legal: the DIRECT importer is the reasoning module.
    lily_search.lily_forbid_vocal_import([
        "lily_search", "importlib._bootstrap", "importlib._bootstrap",
        "lily_reasoning", "importlib._bootstrap", "lily_agent", "main",
    ])
    lily_search.lily_forbid_vocal_import(["lily_imagegen", "main"])


def test_direct_importer_resolution():
    assert lily_search.lily_direct_importer(
        ["lily_search", "importlib._bootstrap", "lily_reasoning", "lily_agent"]
    ) == "lily_reasoning"
    assert lily_search.lily_direct_importer(["", "lily_search"]) == ""
