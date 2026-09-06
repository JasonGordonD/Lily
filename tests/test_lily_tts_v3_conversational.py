"""WO-FLEET-LKA-171-TTD-PORT-001 — livekit-agents 1.7.1 +
eleven_v3_conversational on the text-to-dialogue websocket (Lily).

Port of MinkaMoor tests/test_minkamin_tts_v3_conversational.py (9a6ee3f).
These are the runbook's tripwires; they block deploy. Lily's own
sanitizer (speaker-tag strip), its two voices' settings and PATCH-003 pace
are pinned alongside. No substitute voice: nothing but ElevenLabs makes
audio in this repo.
"""

from __future__ import annotations

import asyncio
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOICE_1 = "W3C2vBPukr5b5jvoXhPK"


class PinAndModelCutoverTests(unittest.TestCase):
    def test_requirements_pin_livekit_171_including_elevenlabs_plugin(self):
        req = (ROOT / "requirements.txt").read_text()
        self.assertIn("livekit-agents==1.7.1", req)
        for plugin in ("speechmatics", "google", "silero", "openai", "elevenlabs"):
            self.assertIn(f"livekit-plugins-{plugin}==1.7.1", req)
        self.assertNotIn("==1.6.10", req)
        self.assertNotIn("==1.7.0", req)

    def test_model_id_is_eleven_v3_conversational(self):
        src = (ROOT / "lily_tts.py").read_text()
        self.assertIn('MODEL_ID = "eleven_v3_conversational"', src)
        self.assertNotIn('MODEL_ID = "eleven_v3"', src)

    def test_say_and_stream_stay_on_websocket_not_http(self):
        src = (ROOT / "lily_tts.py").read_text()
        self.assertNotIn("/text-to-speech/{self._opts.voice_id}", src)
        self.assertNotIn("/stream/with-timestamps", src)
        self.assertNotIn("/text-to-dialogue/stream", src)
        self.assertIn("multi-stream-input", src)
        self.assertIn("_unlock_dialogue_voice_settings", src)
        self.assertIn("record_tts_event", src)

    def test_greeting_question_flushes_without_trailing_space(self):
        src = (ROOT / "lily_tts.py").read_text()
        self.assertIn("_SENTENCE_FLUSH", src)
        self.assertIn(r"\s*$", src)
        self.assertIn("first_text", src)
        self.assertIn("if inner is None:", src)
        from lily_tts import _SENTENCE_FLUSH
        self.assertIsNotNone(_SENTENCE_FLUSH.search("Ready for the next one?"))
        self.assertIsNotNone(_SENTENCE_FLUSH.search("Locked in… "))
        self.assertIsNone(_SENTENCE_FLUSH.search("Divorced, beheaded"))

    def test_say_flushes_before_end_input_and_end_input_after_first_audio(self):
        src = (ROOT / "lily_tts.py").read_text()
        helper_at = src.find("async def _drive_ttd_say")
        self.assertGreater(helper_at, 0)
        start = src.find("class LilyChunkedStream")
        self.assertGreater(start, helper_at)
        impl_at = src.find("async def _run_impl", start)
        self.assertGreater(impl_at, start)
        helper = src[helper_at:start]
        impl = src[impl_at:]
        self.assertIn("await _drive_ttd_say(", impl)
        push = helper.find("inner.push_text(")
        flush = helper.find("inner.flush()")
        wait = helper.find("inner_iter.__anext__()")
        end = helper.find("inner.end_input()")
        self.assertGreater(push, 0)
        self.assertGreater(flush, push)
        self.assertGreater(wait, flush)
        self.assertGreater(end, wait)

    def test_streaming_capability_true_and_stream_adapter_dropped(self):
        src = (ROOT / "lily_tts.py").read_text()
        self.assertIn("streaming=True", src)
        self.assertNotIn("TTSCapabilities(streaming=False", src)
        agent = (ROOT / "lily_agent.py").read_text()
        self.assertNotIn("tts.StreamAdapter(", agent)
        self.assertNotIn("StreamAdapter(", agent.replace("StreamAdapter(", "", 0))

    def test_uses_official_elevenlabs_plugin(self):
        src = (ROOT / "lily_tts.py").read_text()
        self.assertIn("livekit.plugins.elevenlabs", src)
        self.assertIn("pcm_24000", src)

    def test_incident_patches_survive(self):
        src = (ROOT / "lily_tts.py").read_text()
        for needle in (
            "trip_tts_circuit",
            "tts_circuit_is_open",
            "_sanitize_tts_text",
            "_fallback_after_elevenlabs",
            "TTS_TTFB_TIMEOUT_SECS",
            "_degrade_without_voice",
            "max(5.0, TTS_TTFB_TIMEOUT_SECS)",
        ):
            self.assertIn(needle, src)

    def test_no_substitute_tts_voice_anywhere_in_the_repo(self):
        """Company rule: no voice but ElevenLabs. Whole repo, not one file."""
        offenders = []
        for path in ROOT.rglob("*.py"):
            if ".claude" in path.parts or "venv" in path.parts:
                continue
            src = path.read_text(errors="ignore")
            for needle in (
                "api.x.ai/v1/tts", "XAI_TTS_URL", "XAI_TTS_VOICE_ID",
                "_synthesize_via_xai", "_run_xai_buffered",
                "livekit.plugins.openai import TTS", "openai.TTS(",
                "google.TTS(", "cartesia.TTS", "deepgram.TTS", "azure.TTS",
            ):
                if needle in src and path.name != Path(__file__).name:
                    offenders.append(f"{path.name}:{needle}")
        self.assertEqual(offenders, [])

    def test_agent_still_constructs_lily_tts_and_binds_receipts(self):
        src = (ROOT / "lily_agent.py").read_text()
        self.assertIn("LilyTTS()", src)
        self.assertIn("lily_tts_receipts.bind_session_id(room_name)", src)
        self.assertIn('lily_tts_receipts.bind_node_name("LilyAgent")', src)
        self.assertIn("lily_prewarm_tts_connection(lily_tts_instance)", src)

    def test_dockerfile_and_deploy_carry_the_two_halves(self):
        docker = (ROOT / "Dockerfile").read_text()
        self.assertIn("_DIALOGUE_VOICE_SETTINGS_FIELDS = frozenset", docker)
        self.assertIn("assert old in src", docker)
        # the patch runs before the user drop, or it cannot write site-packages
        self.assertLess(docker.find("_DIALOGUE_VOICE_SETTINGS_FIELDS"), docker.find("\nUSER appuser"))
        deploy = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertIn("from livekit.plugins.elevenlabs import TTS as _ElevenLabsTTS", deploy)
        for key in ("ELEVEN_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
            self.assertRegex(deploy, rf'-e {key}="\$\{{{key}\}}"')


class LilyTTSRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import aiohttp  # noqa: F401
            from livekit.agents import tts  # noqa: F401
            import livekit.plugins.elevenlabs  # noqa: F401
        except ImportError as e:
            raise unittest.SkipTest(f"runtime TTS deps missing: {e}")

    def setUp(self):
        os.environ.setdefault("ELEVEN_API_KEY", "test-key-not-used")

    def test_constructed_tts_reports_conversational_model_and_streaming(self):
        from lily_tts import LilyTTS

        tts = LilyTTS(voice_id=VOICE_1, api_key="k")
        self.assertEqual(tts.model, "eleven_v3_conversational")
        self.assertTrue(tts.capabilities.streaming)
        self.assertEqual(tts.sample_rate, 24000)

    def test_sanitizer_still_strips_speaker_tags(self):
        from lily_tts import LilyTTS

        out = LilyTTS._sanitize_tts_text("<speaker id='1'>Point to Rami.</speaker>")
        self.assertEqual(out.strip(), "Point to Rami.")
        self.assertIsNone(LilyTTS._sanitize_tts_text("<speaker id='1'></speaker>"))
        self.assertTrue(LilyTTS._is_empty_after_strip("<speaker id='2'>  </speaker>"))

    def test_dialogue_setup_frame_carries_all_five_voice_settings_per_voice(self):
        import livekit.plugins.elevenlabs.tts as el_tts
        from lily_tts import LilyTTS, _unlock_dialogue_voice_settings

        _unlock_dialogue_voice_settings()
        os.environ.pop("LILY_VOICE_1", None)
        tts = LilyTTS(voice_id=VOICE_1, api_key="k")
        inner = tts._ensure_inner()
        pkt = el_tts._build_dialogue_context_init_packet(inner._opts, context_id="ctx-test")
        settings = pkt.get("voice_settings") or {}
        for key in ("stability", "similarity_boost", "style", "use_speaker_boost", "speed"):
            self.assertIn(key, settings, pkt)
        # voice1's own tuning, unchanged by the port
        self.assertEqual(settings["stability"], 0.5)
        self.assertEqual(settings["similarity_boost"], 0.9)
        self.assertEqual(settings["style"], 0.0)
        self.assertTrue(settings["use_speaker_boost"])
        self.assertEqual(settings["speed"], 0.87)
        self.assertEqual(pkt.get("voices"), [VOICE_1])
        # switching to Raven's voice moves the frame to the baseline tuning
        tts.set_voice("raven_voice_id")
        pkt2 = el_tts._build_dialogue_context_init_packet(inner._opts, context_id="ctx-2")
        self.assertEqual(pkt2.get("voices"), ["raven_voice_id"])
        self.assertEqual(pkt2["voice_settings"]["stability"], 0.4)
        self.assertEqual(pkt2["voice_settings"]["speed"], 0.90)

    def test_pace_rides_the_setup_frame_speed(self):
        import livekit.plugins.elevenlabs.tts as el_tts
        from lily_tts import LilyTTS, _unlock_dialogue_voice_settings

        _unlock_dialogue_voice_settings()
        tts = LilyTTS(voice_id="raven_voice_id", api_key="k")
        inner = tts._ensure_inner()
        self.assertTrue(tts.set_pace("slow"))
        pkt = el_tts._build_dialogue_context_init_packet(inner._opts, context_id="c")
        self.assertEqual(pkt["voice_settings"]["speed"], round(0.90 * 0.88, 3))
        self.assertTrue(tts.set_pace("normal"))
        pkt = el_tts._build_dialogue_context_init_packet(inner._opts, context_id="c")
        self.assertEqual(pkt["voice_settings"]["speed"], 0.90)

    def test_drive_ttd_say_does_not_close_context_before_first_audio(self):
        from lily_tts import _drive_ttd_say

        class _FakeEv:
            def __init__(self):
                self.frame = type("F", (), {"data": b"\x00\x00" * 240})()

        class _FakeInner:
            def __init__(self):
                self.ops: list[str] = []
                self._q: asyncio.Queue = asyncio.Queue()
                self._closed = False

            def push_text(self, text: str) -> None:
                self.ops.append("push_text")

            def flush(self) -> None:
                self.ops.append("flush")
                if not self._closed:
                    self._q.put_nowait(_FakeEv())

            def end_input(self) -> None:
                self.ops.append("end_input")
                self._closed = True
                self._q.put_nowait(None)

            def __aiter__(self):
                return self

            async def __anext__(self):
                ev = await self._q.get()
                if ev is None:
                    raise StopAsyncIteration
                return ev

        inner = _FakeInner()
        first, rest = asyncio.run(
            _drive_ttd_say(inner, "Hi, I'm Lily. Who's at the table tonight?", 1.0)
        )
        self.assertIsNotNone(first)
        self.assertEqual(inner.ops[:3], ["push_text", "flush", "end_input"])
        self.assertIsNotNone(getattr(first, "frame", None))

    def test_stream_opens_the_socket_only_on_first_nonempty_flush(self):
        """The 1eeb8d2 hazard: an empty open trips the plugin watchdog."""
        import lily_tts
        from lily_tts import LilyTTS, LilySynthesizeStream
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

        opened = []

        class _InnerStream:
            def __init__(self):
                self.pushed = []
                self._q = asyncio.Queue()

            def push_text(self, t):
                self.pushed.append(t)

            def flush(self):
                self._q.put_nowait(type("E", (), {"frame": type("F", (), {"data": b"\x01\x00" * 240})()})())

            def end_input(self):
                self._q.put_nowait(None)

            def __aiter__(self):
                return self

            async def __anext__(self):
                ev = await self._q.get()
                if ev is None:
                    raise StopAsyncIteration
                return ev

            async def aclose(self):
                pass

        class _InnerTTS:
            def stream(self, conn_options=None):
                opened.append(1)
                return _InnerStream()

        rows = []
        lily_tts.record_tts_event = lambda **kw: rows.append(kw)
        tts = LilyTTS(voice_id=VOICE_1, api_key="k")
        tts._inner = _InnerTTS()

        class _Emitter:
            def __init__(self):
                self.pushed = b""
                self.flushed = False

            def initialize(self, **kw):
                pass

            def start_segment(self, **kw):
                pass

            def end_segment(self):
                pass

            def push(self, data):
                self.pushed += data

            def flush(self):
                self.flushed = True

        async def _go(tokens):
            stream = LilySynthesizeStream(tts=tts, conn_options=DEFAULT_API_CONNECT_OPTIONS)
            for t in tokens:
                stream.push_text(t)
            stream.end_input()
            em = _Emitter()
            await stream._run(em)
            return em

        # whitespace-only turn: socket never opened, silence placeholder
        em = asyncio.run(_go(["   ", "<speaker id='1'></speaker>"]))
        self.assertEqual(opened, [])
        self.assertTrue(em.pushed)
        self.assertEqual(rows[-1]["error"], "no-text")
        # a real greeting ending in "?" opens exactly one socket and airs
        em = asyncio.run(_go(["Ready for ", "the next one?"]))
        self.assertEqual(len(opened), 1)
        self.assertTrue(em.flushed and em.pushed)
        self.assertIsNone(rows[-1]["error"])
        self.assertEqual(rows[-1]["chars"], len("Ready for the next one?"))
