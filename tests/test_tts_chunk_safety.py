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
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from livekit.agents import APIConnectionError, APIStatusError

from lily_tts import (
    ELEVENLABS_REQUEST_CHAR_CAP,
    MAX_CHUNK_SIZE,
    LilyChunkedStream,
    _TTSOpts,
    _WordTimingAggregator,
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
    # Audio seconds already aired before this request (the per-sentence
    # direct-synthesis path sets it; a single-request turn leaves it 0.0).
    stream._time_offset = 0.0
    # These cases script raw-PCM responses (the legacy /stream path); the
    # timed /stream/with-timestamps path has its own cases below.
    stream._timed = False
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


# --------------------------------------------------------------------------
# WO-LILY-UI-SYNC-TYPEWRITER-001: word-level aligned transcript
# (/stream/with-timestamps). The timed path decodes base64 PCM, aggregates
# per-character alignment into per-word TimedString, and preserves every
# accounting invariant the raw path has.
# --------------------------------------------------------------------------

class _FakeTimedContent:
    """Async-iterable of newline-delimited JSON lines (with-timestamps)."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line


class _FakeTimedResp:
    def __init__(self, status=200, objs=None, err=""):
        self.status = status
        lines = [(json.dumps(o) + "\n").encode() for o in (objs or [])]
        self.content = _FakeTimedContent(lines)
        self._err = err

    async def text(self):
        return self._err

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingSession:
    """Records the POST url + json body, serves scripted timed responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.urls = []
        self.bodies = []

    def post(self, url, **kwargs):
        self.urls.append(url)
        self.bodies.append(kwargs.get("json"))
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


class _TimedEmitter(_FakeEmitter):
    def __init__(self):
        super().__init__()
        self.timed = []

    def push_timed_transcript(self, delta):
        if isinstance(delta, list):
            self.timed.extend(delta)
        else:
            self.timed.append(delta)


def _pcm(n_samples: int) -> bytes:
    return b"\x01\x00" * n_samples


def _align_obj(chars, starts, ends, pcm_samples):
    return {
        "audio_base64": base64.b64encode(_pcm(pcm_samples)).decode(),
        "alignment": {
            "characters": list(chars),
            "character_start_times_seconds": list(starts),
            "character_end_times_seconds": list(ends),
        },
    }


def _make_timed_stream(text, session):
    stream = _make_stream(text, session)
    stream._timed = True
    return stream


def test_timed_path_hits_with_timestamps_endpoint_and_body_is_identical():
    text = "Hi there."
    obj = _align_obj(
        list("Hi there."),
        [0.0, 0.04, 0.08, 0.12, 0.16, 0.20, 0.24, 0.28, 0.32],
        [0.04, 0.08, 0.12, 0.16, 0.20, 0.24, 0.28, 0.32, 0.36],
        10,
    )
    session = _RecordingSession([_FakeTimedResp(200, [obj])])
    stream = _make_timed_stream(text, session)
    emitter = _TimedEmitter()
    asyncio.run(stream._run(emitter))

    assert session.urls[0].startswith(
        "https://api.elevenlabs.io/v1/text-to-speech/v1/stream/with-timestamps"
    )
    # Body identity: the operator-locked keys are exactly what the raw path
    # sends — the endpoint swap must not alter voice/model/settings/format.
    body = session.bodies[0]
    assert body["text"] == text
    assert body["model_id"] == "eleven_v3"
    assert body["apply_text_normalization"] == "auto"
    assert set(body["voice_settings"]) == {
        "stability", "similarity_boost", "style", "use_speaker_boost", "speed",
    }
    assert emitter.pushed  # audio reached the air
    assert emitter.flushed


def test_timed_path_emits_monotonic_words_that_reconstruct_the_text():
    text = "Hi there."
    obj = _align_obj(
        list("Hi there."),
        [0.0, 0.04, 0.08, 0.12, 0.16, 0.20, 0.24, 0.28, 0.32],
        [0.04, 0.08, 0.12, 0.16, 0.20, 0.24, 0.28, 0.32, 0.36],
        10,
    )
    session = _RecordingSession([_FakeTimedResp(200, [obj])])
    stream = _make_timed_stream(text, session)
    emitter = _TimedEmitter()
    asyncio.run(stream._run(emitter))

    assert [str(t) for t in emitter.timed] == ["Hi ", "there."]
    starts = [t.start_time for t in emitter.timed]
    assert starts == sorted(starts)  # monotonic non-decreasing
    assert "".join(str(t) for t in emitter.timed) == text


def test_timed_word_straddling_two_objects_emits_one_token():
    # "there" split across two streamed objects must not become two words.
    obj1 = _align_obj(list("the"), [0.0, 0.04, 0.08], [0.04, 0.08, 0.12], 4)
    obj2 = _align_obj(list("re "), [0.12, 0.16, 0.20], [0.16, 0.20, 0.24], 4)
    session = _RecordingSession([_FakeTimedResp(200, [obj1, obj2])])
    stream = _make_timed_stream("there ", session)
    emitter = _TimedEmitter()
    asyncio.run(stream._run(emitter))
    assert [str(t) for t in emitter.timed] == ["there "]
    assert abs(emitter.timed[0].start_time - 0.0) < 1e-9


def test_timed_path_records_undelivered_tail_on_second_chunk_death():
    # Same claim-vs-delivery invariant as the raw path, on with-timestamps.
    sent_a = "A" * 2500 + ". "
    sent_b = "B" * 2500 + "."
    text = sent_a + sent_b
    obj = _align_obj(["A"], [0.0], [0.04], 4)
    session = _RecordingSession([
        _FakeTimedResp(200, [obj]),          # chunk 1 airs
        _FakeTimedResp(500, err="boom"),     # chunk 2 dies
    ])
    stream = _make_timed_stream(text, session)
    emitter = _TimedEmitter()
    with pytest.raises(APIStatusError):
        asyncio.run(stream._run(emitter))
    assert stream.chunks_total == 2
    assert stream.chunks_delivered == 1
    assert stream.undelivered_remainder == sent_b
    assert emitter.pushed
    assert not emitter.flushed


def test_word_timing_aggregator_offset_makes_second_chunk_monotonic():
    agg = _WordTimingAggregator()
    out = agg.feed(list("Hi "), [0.0, 0.05, 0.10], [0.05, 0.10, 0.15], 0.0)
    out += agg.flush()
    assert [str(t) for t in out] == ["Hi "]
    # a later chunk's onsets are offset past the audio already aired
    agg2 = _WordTimingAggregator()
    out2 = agg2.feed(list("bye"), [0.0, 0.05, 0.10], [0.05, 0.10, 0.15], 2.0)
    out2 += agg2.flush()
    assert abs(out2[0].start_time - 2.0) < 1e-9


# --------------------------------------------------------------------------
# Silent-mute guard (WO-LILY-UI-SYNC-TYPEWRITER-001, 1.6.10 merge review)
#
# A 200 from /stream/with-timestamps whose body is NOT newline-delimited JSON
# — a vendor framing change, a plan downgrade serving raw /stream bytes, an
# error envelope with a 200 — parses to nothing. Before the guard the chunk
# loop completed normally and chunks_delivered advanced, so a MUTE turn was
# recorded as a clean delivery and cut-recovery never armed. These cases pin
# the failure onto the transport path a torn stream already takes.
# --------------------------------------------------------------------------


class _RawBodyTimedResp:
    """A 200 carrying raw PCM instead of NDJSON (the pre-fix silent mute)."""

    def __init__(self, payload: bytes):
        self.status = 200
        self.content = _FakeTimedContent([payload])

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_timed_empty_200_is_a_chunk_failure_not_a_clean_delivery():
    text = "Which planet is biggest?"
    # Two responses: the guard reuses the HOTFIX-005 X5 zero-bytes retry, so
    # the chunk is re-fetched once before it is given up on.
    session = _RecordingSession([
        _RawBodyTimedResp(_pcm(24000)),
        _RawBodyTimedResp(_pcm(24000)),
    ])
    stream = _make_timed_stream(text, session)
    emitter = _TimedEmitter()

    with pytest.raises(APIConnectionError):
        asyncio.run(stream._run(emitter))

    # No audio ever reached the air...
    assert emitter.pushed == b""
    # ...so the chunk is UNDELIVERED and its text is handed to cut-recovery.
    # Pre-fix this read chunks_delivered == 1/1 with an empty remainder.
    assert stream.chunks_delivered == 0
    assert stream.chunks_total == 1
    assert stream.undelivered_remainder == text
    # One clean re-fetch was attempted, exactly as for a torn stream.
    assert session.calls == 2


def test_timed_empty_200_second_chunk_leaves_only_the_tail_undelivered():
    # The guard must not over-claim: a chunk that DID air stays delivered.
    sent_a = "A" * 2500 + ". "
    sent_b = "B" * 2500 + "."
    obj = _align_obj(["A"], [0.0], [0.04], 4)
    session = _RecordingSession([
        _FakeTimedResp(200, [obj]),        # chunk 1 airs normally
        _RawBodyTimedResp(_pcm(24000)),    # chunk 2 returns an empty 200
        _RawBodyTimedResp(_pcm(24000)),    # ...and again on the retry
    ])
    stream = _make_timed_stream(sent_a + sent_b, session)
    emitter = _TimedEmitter()

    with pytest.raises(APIConnectionError):
        asyncio.run(stream._run(emitter))

    assert emitter.pushed  # chunk 1's audio did air
    assert stream.chunks_delivered == 1
    assert stream.undelivered_remainder == sent_b


def test_raw_path_empty_200_is_untouched_by_the_guard():
    # Flag-off byte-identity: the guard is timed-path only. A raw /stream 200
    # with no bytes behaves exactly as it did before the WO — no raise.
    session = _FakeSession([_FakeResp(200, b"")])
    stream = _make_stream("Hi there.", session)  # _timed is False
    emitter = _FakeEmitter()
    asyncio.run(stream._run(emitter))
    assert stream.chunks_delivered == 1
    assert stream.undelivered_remainder == ""
