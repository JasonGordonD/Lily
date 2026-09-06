"""WS-2 (WO-LILY-STREAM-INTEGRITY-002) — chunk-safe TTS dispatch, on the
TTD websocket (WO-FLEET-LKA-171-TTD-PORT-001).

Two guarantees:
  1. _split_text keeps every piece comfortably under the ElevenLabs
     per-request character cap, splitting at sentence boundaries and — when
     a single sentence is longer than the cap — at the last whitespace so it
     never cuts a WORD.
  2. Delivery is tracked claim-vs-delivery at utterance granularity: a say()
     whose first byte never arrives records the whole text as undelivered
     (LILY_TTS | TAIL_CHUNK_UNDELIVERED) and degrades to SILENCE — never a
     substitute voice, never a re-raised timeout.

Framework-free: LilyChunkedStream is built via __new__ and _run_impl is
driven against a fake inner websocket stream and emitter.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_tts
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
    assert chunks[0].endswith(". ")


def test_boundaryless_long_sentence_splits_on_whitespace_not_midword():
    words = ("supercalifragilistic " * 400).strip()
    assert len(words) > MAX_CHUNK_SIZE
    chunks = LilyChunkedStream._split_text(words)
    assert len(chunks) >= 2
    assert all(len(c) <= MAX_CHUNK_SIZE for c in chunks)
    assert "".join(chunks) == words
    for c in chunks[:-1]:
        assert c.endswith(" ")


def test_split_never_exceeds_cap_on_huge_input():
    text = "word " * 5000
    chunks = LilyChunkedStream._split_text(text)
    assert all(len(c) <= MAX_CHUNK_SIZE for c in chunks)
    assert "".join(chunks) == text


# --------------------------------------------------------------------------
# say() on the websocket — delivery accounting
# --------------------------------------------------------------------------

class _FakeEv:
    def __init__(self, n=240):
        self.frame = type("F", (), {"data": b"\x01\x00" * n})()


class _FakeInner:
    """A plugin SynthesizeStream double: flush() queues audio unless told
    to stay silent; end_input() closes the context."""

    def __init__(self, *, silent=False):
        self.ops = []
        self.texts = []
        self._q = asyncio.Queue()
        self._silent = silent
        self.closed = False

    def push_text(self, text):
        self.ops.append("push_text")
        self.texts.append(text)

    def flush(self):
        self.ops.append("flush")
        if not self._silent:
            self._q.put_nowait(_FakeEv())

    def end_input(self):
        self.ops.append("end_input")
        self._q.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        ev = await self._q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev

    async def aclose(self):
        self.closed = True


class _FakeInnerTTS:
    def __init__(self, inner):
        self._inner_stream = inner
        self.prewarmed = 0

    async def _current_connection(self):
        self.prewarmed += 1
        return (object(), 0.0, True)

    def stream(self, conn_options=None):
        return self._inner_stream


class _FakeTTS:
    def __init__(self, inner_tts):
        self._inner_tts = inner_tts

    def _ensure_inner(self):
        return self._inner_tts


class _FakeEmitter:
    def __init__(self):
        self.pushed = b""
        self.flushed = False
        self.initialized = 0

    def initialize(self, **kwargs):
        self.initialized += 1

    def push(self, data):
        self.pushed += data

    def flush(self):
        self.flushed = True


class _FakeConnOpts:
    timeout = 5.0


def _make_stream(text, inner, *, skip_empty=False):
    stream = LilyChunkedStream.__new__(LilyChunkedStream)
    stream._opts = _TTSOpts(
        voice_id="v1", api_key="k", model_id=lily_tts.MODEL_ID,
        output_format="pcm_24000",
    )
    stream._tts = _FakeTTS(_FakeInnerTTS(inner))
    stream._input_text = text
    stream._conn_options = _FakeConnOpts()
    stream._skip_empty = skip_empty
    stream._time_offset = 0.0
    stream.chunks_total = 0
    stream.chunks_delivered = 0
    stream.undelivered_remainder = ""
    return stream


def _quiet_receipts(monkeypatch):
    rows = []
    monkeypatch.setattr(lily_tts, "record_tts_event", lambda **kw: rows.append(kw))
    return rows


def test_say_rides_the_websocket_push_flush_first_byte_then_close(monkeypatch):
    rows = _quiet_receipts(monkeypatch)
    inner = _FakeInner()
    stream = _make_stream("Which planet is biggest?", inner)
    emitter = _FakeEmitter()
    asyncio.run(stream._run(emitter))
    assert inner.ops[:3] == ["push_text", "flush", "end_input"]
    assert emitter.pushed and emitter.flushed and emitter.initialized == 1
    assert stream.chunks_total == 1 and stream.chunks_delivered == 1
    assert stream.undelivered_remainder == ""
    assert inner.closed
    assert rows and rows[0]["transport"] == "ws_ttd"
    assert rows[0]["error"] is None and rows[0]["audio_ms"] > 0


def test_say_ttfb_timeout_degrades_to_silence_and_records_undelivered(monkeypatch):
    rows = _quiet_receipts(monkeypatch)
    monkeypatch.setattr(lily_tts, "TTS_TTFB_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(lily_tts, "_drive_ttd_say", _fast_timeout_drive)
    inner = _FakeInner(silent=True)
    text = "A question nobody will hear."
    stream = _make_stream(text, inner)
    emitter = _FakeEmitter()
    asyncio.run(stream._run(emitter))  # never raises: no framework retry stack
    # Silence placeholder aired (one frame), nothing else.
    assert emitter.pushed == b"\x00" * (lily_tts.SAMPLE_RATE // 100 * 2)
    assert stream.chunks_delivered == 0
    assert stream.undelivered_remainder == text
    assert rows[0]["error"] == "ttfb-timeout"
    assert lily_tts.tts_circuit_is_open()
    lily_tts._tts_circuit_open_until = 0.0


async def _fast_timeout_drive(inner, text, ttfb, extra_pieces=()):
    """The watchdog floor is 5 s in production; the test wants the same
    branch without the wait."""
    inner.push_text(text)
    inner.flush()
    inner_iter = inner.__aiter__()
    first = await asyncio.wait_for(inner_iter.__anext__(), timeout=0.05)
    inner.end_input()
    return first, inner_iter


def test_tag_only_text_emits_silent_placeholder_without_a_socket(monkeypatch):
    _quiet_receipts(monkeypatch)
    inner = _FakeInner()
    stream = _make_stream("<speaker id='1'></speaker>", inner, skip_empty=True)
    emitter = _FakeEmitter()
    asyncio.run(stream._run(emitter))
    assert inner.ops == []  # the websocket was never touched
    assert emitter.pushed  # the one-frame contract holds


def test_over_cap_utterance_rides_one_context(monkeypatch):
    _quiet_receipts(monkeypatch)
    inner = _FakeInner()
    sent_a = "A" * 2500 + ". "
    sent_b = "B" * 2500 + "."
    stream = _make_stream(sent_a + sent_b, inner)
    emitter = _FakeEmitter()
    asyncio.run(stream._run(emitter))
    # Both pieces pushed before the single close_context.
    assert inner.ops.count("push_text") == 2
    assert inner.ops.count("end_input") == 1
    assert "".join(inner.texts) == sent_a + sent_b
    assert stream.chunks_delivered == 1
