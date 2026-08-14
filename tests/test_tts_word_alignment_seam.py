"""WO-LILY-UI-SYNC-TYPEWRITER-001 (1.6.10 merge review): the per-word
timings must survive the AGENT seam, not just the TTS one.

THE DEFECT THIS PINS. LilyChunkedStream decodes per-word TimedString from
/stream/with-timestamps and pushes them with push_timed_transcript. That was
verified in isolation and looked correct — but every spoken turn goes through
Agent.default.tts_node, which wraps any TTS whose capabilities.streaming is
False (LilyTTS is) in tts.StreamAdapter. StreamAdapterWrapper._run forwards
the inner stream as `output_emitter.push(audio.frame.data.tobytes())`: the PCM
survives, `frame.userdata` — which is where the words live — does NOT. It then
substitutes ONE sentence-level TimedString per token. Measured through the real
framework classes before the fix:

    direct synthesize()  -> ('Which ',0.0,0.3) ('planet ',0.3,0.65)
                            ('is ',0.65,0.8)   ('biggest?',0.8,1.2)
    via StreamAdapter    -> ('Which planet is biggest?', 0.0, NOT_GIVEN)

The display still moved (the synchronizer paces words off sentence annotations
plus its speaking-rate estimate), so this failed SILENTLY — the whole
with-timestamps round-trip bought nothing. LilyAgent._lily_aligned_tts_frames
synthesizes per sentence straight off LilyTTS and yields ev.frame, keeping
userdata intact.

These cases drive the real LilyAgent method against fake vendor doubles, so
the regression is caught at the seam that actually broke rather than one layer
below it.
"""

import asyncio
import base64
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents.tts.tts import USERDATA_TIMED_TRANSCRIPT
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

import lily_tts
from lily_agent import LilyAgent

SAMPLE_RATE = 24000
_CHAR_SECS = 0.05


def _ndjson_for(words):
    """One NDJSON object: base64 PCM + per-character alignment, with the
    audio length matching the alignment so frame durations stay honest."""
    chars, starts, ends = [], [], []
    t = 0.0
    for w in words:
        for ch in w:
            chars.append(ch)
            starts.append(round(t, 4))
            ends.append(round(t + _CHAR_SECS, 4))
            t += _CHAR_SECS
    pcm = b"\x01\x00" * int(SAMPLE_RATE * t)
    return json.dumps({
        "audio_base64": base64.b64encode(pcm).decode(),
        "alignment": {
            "characters": chars,
            "character_start_times_seconds": starts,
            "character_end_times_seconds": ends,
        },
    }).encode() + b"\n"


class _Content:
    def __init__(self, payload):
        self._payload = payload

    def __aiter__(self):
        async def gen():
            for line in self._payload.splitlines(keepends=True):
                yield line
        return gen()

    async def iter_chunks(self):
        yield self._payload, True


class _Resp:
    def __init__(self, payload):
        self.status = 200
        self.content = _Content(payload)

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Serves a payload keyed on the text actually requested, and records
    the per-request bodies so the operator-locked config can be asserted."""

    def __init__(self, by_text):
        self._by_text = by_text
        self.texts = []
        self.bodies = []

    def post(self, url, **kwargs):
        body = kwargs.get("json")
        self.bodies.append(body)
        text = body["text"].strip()
        self.texts.append(text)
        return _Resp(self._by_text[text])


def _agent_stub(tts_impl):
    """The real LilyAgent method, bound to a stub carrying only the activity
    it reads. Avoids standing up a whole AgentSession for a seam test."""
    activity = types.SimpleNamespace(
        tts=tts_impl,
        session=types.SimpleNamespace(
            conn_options=types.SimpleNamespace(
                tts_conn_options=DEFAULT_API_CONNECT_OPTIONS
            )
        ),
    )

    class _Stub:
        def _get_activity_or_raise(self):
            return activity

        _lily_aligned_tts_frames = LilyAgent._lily_aligned_tts_frames

    return _Stub()


def _drive(full, by_text):
    tts_impl = lily_tts.LilyTTS(voice_id="W3C2vBPukr5b5jvoXhPK", api_key="k")
    session = _Session(by_text)
    tts_impl._ensure_session = lambda: session
    stub = _agent_stub(tts_impl)

    words, frames = [], 0

    async def _go():
        nonlocal frames
        async for frame in stub._lily_aligned_tts_frames(full, None):
            frames += 1
            words.extend(frame.userdata.get(USERDATA_TIMED_TRANSCRIPT, []))

    asyncio.run(_go())
    return words, frames, session


S1 = "Which planet is biggest?"
S2 = "Take your time."
BY_TEXT = {
    S1: _ndjson_for(["Which ", "planet ", "is ", "biggest? "]),
    S2: _ndjson_for(["Take ", "your ", "time."]),
}


def test_per_word_timings_survive_the_agent_tts_node_seam(monkeypatch):
    monkeypatch.setenv("LILY_VOICE_SYNCED_TRANSCRIPT", "true")
    words, frames, _ = _drive(f"{S1} {S2}", BY_TEXT)

    # The regression: pre-fix this was ONE sentence-level blob per sentence.
    assert [str(w) for w in words] == [
        "Which ", "planet ", "is ", "biggest? ", "Take ", "your ", "time.",
    ]
    assert frames > 0
    # Every word carries a real start AND end — StreamAdapter's substitute
    # carried a start only (end_time was NOT_GIVEN).
    for w in words:
        assert isinstance(w.start_time, float)
        assert isinstance(w.end_time, float)
        assert w.end_time >= w.start_time


def test_word_onsets_stay_monotonic_across_sentences(monkeypatch):
    # Each request's alignment restarts at 0.0. Without the time_offset carry
    # the second sentence re-emits onsets from zero and the synchronizer reads
    # time as going backwards mid-turn.
    monkeypatch.setenv("LILY_VOICE_SYNCED_TRANSCRIPT", "true")
    words, _, _ = _drive(f"{S1} {S2}", BY_TEXT)

    starts = [w.start_time for w in words]
    assert starts == sorted(starts)
    # The second sentence begins after the first sentence's audio, not at 0.
    first_of_s2 = next(w for w in words if str(w).startswith("Take"))
    assert first_of_s2.start_time > 1.0


def test_turn_is_synthesized_per_sentence_with_the_locked_body(monkeypatch):
    # Sentence-by-sentence, exactly as StreamAdapter drove it (so provider
    # chunking and TTFB are unchanged), and every request still carries the
    # operator-locked voice/model/settings.
    monkeypatch.setenv("LILY_VOICE_SYNCED_TRANSCRIPT", "true")
    _, _, session = _drive(f"{S1} {S2}", BY_TEXT)

    assert session.texts == [S1, S2]
    for body in session.bodies:
        assert body["model_id"] == "eleven_v3"
        assert body["apply_text_normalization"] == "auto"
        assert body["voice_settings"] == {
            "stability": 0.5, "similarity_boost": 0.9, "style": 0.0,
            "use_speaker_boost": True, "speed": 0.87,
        }


def test_concatenated_words_reproduce_the_spoken_text(monkeypatch):
    # The transcript panel renders the concatenation, so it must equal the
    # corrected post-TTS text (P0-C) with nothing dropped or doubled.
    monkeypatch.setenv("LILY_VOICE_SYNCED_TRANSCRIPT", "true")
    words, _, _ = _drive(f"{S1} {S2}", BY_TEXT)
    assert "".join(str(w) for w in words) == f"{S1} {S2}"


def test_single_sentence_turn_needs_one_request(monkeypatch):
    monkeypatch.setenv("LILY_VOICE_SYNCED_TRANSCRIPT", "true")
    words, _, session = _drive(S1, {S1: BY_TEXT[S1]})
    assert session.texts == [S1]
    assert [str(w) for w in words] == ["Which ", "planet ", "is ", "biggest? "]
