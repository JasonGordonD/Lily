"""WS-2 (WO-LILY-STREAM-INTEGRITY-002): chunk-safe TTS dispatch.

Two guarantees:
  1. _split_text keeps every chunk comfortably under the ElevenLabs
     per-request character cap (a chunk over the cap 4xx's mid-turn and,
     because earlier chunks already aired, kills the turn mid-sentence),
     splitting at sentence boundaries and — when a single sentence is
     longer than the cap — at the last whitespace so it never cuts a WORD.
  2. Tail-chunk delivery is tracked claim-vs-delivery: if synthesis dies
     after the first chunk aired, the undelivered remainder is recorded and
     logged (LILY_TTS | TAIL_CHUNK_UNDELIVERED) so the cut surfaces as a
     partial-delivery gap the cut-recovery path regenerates — never silent
     mid-sentence death.

Framework-free: LilyChunkedStream is built via __new__ and _run is driven
against fake aiohttp/emitter doubles, so no livekit runtime is needed.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from livekit.agents import APIStatusError

from lily_tts import (
    ELEVENLABS_REQUEST_CHAR_CAP,
    MAX_CHUNK_SIZE,
    LilyChunkedStream,
    _TTSOpts,
)


# --------------------------------------------------------------------------
# _split_text
# --------------------------------------------------------------------------

def test_split_margin_below_platform_cap():
    # The split cap must leave headroom under the hard ElevenLabs cap.
    assert MAX_CHUNK_SIZE < ELEVENLABS_REQUEST_CHAR_CAP


def test_short_text_single_chunk():
    text = "How does that sound to you? If everyone's ready, let's go!"
    assert LilyChunkedStream._split_text(text) == [text]


def test_multisentence_splits_on_boundaries_under_cap():
    sent_a = "A" * 2500 + ". "
    sent_b = "B" * 2500 + "."
    chunks = LilyChunkedStream._split_text(sent_a + sent_b)
    assert len(chunks) == 2
    assert "".join(chunks) == sent_a + sent_b
    assert all(len(c) <= MAX_CHUNK_SIZE for c in chunks)
    # First chunk ends at the sentence boundary, not mid-content.
    assert chunks[0].endswith(". ")


def test_boundaryless_long_sentence_splits_on_whitespace_not_midword():
    # One "sentence" with no . ! ? under the cap — a wall of words. It must
    # still split, at a space, so no word is sliced in half.
    words = ("supercalifragilistic " * 400).strip()  # ~8000 chars, no punct
    assert len(words) > MAX_CHUNK_SIZE
    chunks = LilyChunkedStream._split_text(words)
    assert len(chunks) >= 2
    assert all(len(c) <= MAX_CHUNK_SIZE for c in chunks)
    assert "".join(chunks) == words
    # No chunk boundary lands inside a word: each non-final chunk ends on a
    # space (the whitespace split), so re-joining loses nothing and no
    # fragment word is orphaned.
    for c in chunks[:-1]:
        assert c.endswith(" ")


def test_split_never_exceeds_cap_on_huge_input():
    text = "word " * 5000  # 25k chars
    chunks = LilyChunkedStream._split_text(text)
    assert all(len(c) <= MAX_CHUNK_SIZE for c in chunks)
    assert "".join(chunks) == text


# --------------------------------------------------------------------------
# Tail-chunk delivery tracking in _run
# --------------------------------------------------------------------------

class _FakeContent:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def iter_chunks(self):
        if self._payload:
            yield self._payload, True


class _FakeResp:
    def __init__(self, status: int, payload: bytes = b"", err: str = ""):
        self.status = status
        self.content = _FakeContent(payload)
        self._err = err

    async def text(self):
        return self._err

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Serves a scripted response per POST call (one per chunk)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, **kwargs):
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


class _FakeTTS:
    def __init__(self, session):
        self._session = session

    def _ensure_session(self):
        return self._session


class _FakeEmitter:
    def __init__(self):
        self.pushed = b""
        self.flushed = False
        self.initialized = False

    def initialize(self, **kwargs):
        self.initialized = True

    def push(self, data):
        self.pushed += data

    def flush(self):
        self.flushed = True


class _FakeConnOpts:
    timeout = 5.0


def _make_stream(text: str, session: _FakeSession) -> LilyChunkedStream:
    stream = LilyChunkedStream.__new__(LilyChunkedStream)
    stream._opts = _TTSOpts(
        voice_id="v1", api_key="k", model_id="eleven_v3",
        output_format="pcm_24000",
    )
    stream._tts = _FakeTTS(session)
    stream._input_text = text
    stream._conn_options = _FakeConnOpts()
    stream._skip_empty = False
    stream.chunks_total = 0
    stream.chunks_delivered = 0
    stream.undelivered_remainder = ""
    return stream


def test_tail_chunk_failure_records_undelivered_remainder():
    # Two chunks; chunk 1 airs, chunk 2's POST 500s. The first chunk's audio
    # must have aired, and the undelivered tail (chunk 2) must be recorded
    # so cut-recovery can regenerate it — never a silent mid-sentence death.
    sent_a = "A" * 2500 + ". "
    sent_b = "B" * 2500 + "."
    text = sent_a + sent_b
    session = _FakeSession([
        _FakeResp(200, payload=b"\x01\x02\x03\x04"),   # chunk 1 airs
        _FakeResp(500, err="boom"),                     # chunk 2 dies
    ])
    stream = _make_stream(text, session)
    emitter = _FakeEmitter()

    with pytest.raises(APIStatusError):
        asyncio.run(stream._run(emitter))

    assert stream.chunks_total == 2
    assert stream.chunks_delivered == 1
    assert stream.undelivered_remainder == sent_b
    assert emitter.pushed  # chunk 1 audio reached the air
    assert not emitter.flushed  # never completed cleanly


def test_full_delivery_leaves_no_remainder():
    sent_a = "A" * 2500 + ". "
    sent_b = "B" * 2500 + "."
    text = sent_a + sent_b
    session = _FakeSession([
        _FakeResp(200, payload=b"\x01\x02"),
        _FakeResp(200, payload=b"\x03\x04"),
    ])
    stream = _make_stream(text, session)
    emitter = _FakeEmitter()

    asyncio.run(stream._run(emitter))

    assert stream.chunks_total == 2
    assert stream.chunks_delivered == 2
    assert stream.undelivered_remainder == ""
    assert emitter.flushed
