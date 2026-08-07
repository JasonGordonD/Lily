"""Gemini built-in grounding as ADDITIONAL reasoning-node sources —
google_search grounding + url_context reading, alongside Exa/Tavily (never
replacing them). Parsing/shaping is pinned here with fake genai responses;
the live API call is exercised in the live smoke.
"""

import asyncio
import sys
import types as pytypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_search


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# -- fake genai response shapes -----------------------------------------------

def _obj(**kw):
    return pytypes.SimpleNamespace(**kw)


def _search_resp(text):
    gm = _obj(
        web_search_queries=["euro 2024 winner"],
        grounding_chunks=[
            _obj(web=_obj(uri="https://uefa.com/x", title="uefa.com")),
            _obj(web=_obj(uri="https://aljazeera.com/y", title="aljazeera.com")),
        ],
    )
    return _obj(text=text, candidates=[_obj(grounding_metadata=gm,
                                            url_context_metadata=None)])


def _url_resp(text):
    ucm = _obj(url_metadata=[
        _obj(retrieved_url="https://foodnetwork.com/r", url_retrieval_status="SUCCESS"),
        _obj(retrieved_url="https://allrecipes.com/r", url_retrieval_status="SUCCESS"),
    ])
    return _obj(text=text, candidates=[_obj(grounding_metadata=None,
                                            url_context_metadata=ucm)])


# -- parser -------------------------------------------------------------------

def test_parse_search_extracts_citations_and_queries():
    r = lily_search.lily_parse_google_grounding(_search_resp("Spain won."))
    assert r["text"] == "Spain won."
    assert r["queries"] == ["euro 2024 winner"]
    assert [c["url"] for c in r["citations"]] == [
        "https://uefa.com/x", "https://aljazeera.com/y"]
    assert r["retrieved_urls"] == []


def test_parse_url_context_extracts_retrieved_urls():
    r = lily_search.lily_parse_google_grounding(_url_resp("Recipe A vs B."))
    assert r["text"] == "Recipe A vs B."
    assert [u["url"] for u in r["retrieved_urls"]] == [
        "https://foodnetwork.com/r", "https://allrecipes.com/r"]
    assert r["retrieved_urls"][0]["status"] == "SUCCESS"


def test_parse_empty_text_is_none():
    assert lily_search.lily_parse_google_grounding(_obj(text="", candidates=[])) is None


def test_parse_is_defensive_against_missing_fields():
    r = lily_search.lily_parse_google_grounding(_obj(text="ok", candidates=[]))
    assert r["text"] == "ok" and r["citations"] == [] and r["retrieved_urls"] == []


# -- formatter ----------------------------------------------------------------

def test_formatter_includes_answer_and_sources():
    r = lily_search.lily_parse_google_grounding(_search_resp("Spain won Euro 2024."))
    out = lily_search.lily_format_google_grounding(r)
    assert "Spain won Euro 2024." in out
    assert "uefa.com" in out and "Sources:" in out


def test_formatter_no_results():
    assert lily_search.lily_format_google_grounding(None) == "No results found."


# -- config gating + additive (never replaces Exa/Tavily) ---------------------

def test_disabled_returns_none_without_call(monkeypatch):
    monkeypatch.setattr(lily_config, "google_grounding_enabled", lambda: False)
    monkeypatch.setattr(lily_config, "url_context_enabled", lambda: False)
    assert _run(lily_search.lily_google_grounded_search("x")) is None
    assert _run(lily_search.lily_url_context_read("read https://a.com")) is None


def test_search_and_url_context_still_exist_alongside_exa_tavily():
    # Additive: the incumbent sources remain the module's public surface.
    for fn in ("lily_exa_search", "lily_tavily_search",
               "lily_google_grounded_search", "lily_url_context_read"):
        assert hasattr(lily_search, fn)


def test_config_defaults_on_with_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.delenv("LILY_GOOGLE_GROUNDING", raising=False)
    monkeypatch.delenv("LILY_URL_CONTEXT", raising=False)
    assert lily_config.google_grounding_enabled() is True
    assert lily_config.url_context_enabled() is True
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert lily_config.google_grounding_enabled() is False  # no key => off
