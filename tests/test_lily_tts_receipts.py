"""WO-FLEET-LKA-171-TTD-PORT-001 — fleet_tts_events writer (Lily).
Port of MinkaMoor tests/test_minkamin_tts_receipts.py; AGENT_NAME='lily'."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lily_tts_receipts as rec  # noqa: E402


class ReceiptWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import aiohttp  # noqa: F401
        except ImportError as e:
            raise unittest.SkipTest(f"aiohttp missing: {e}")

    def test_keep_row_constant_is_s3_verify_keep_and_never_deletes(self):
        self.assertEqual(rec.KEEP_SESSION_ID, "__s3_verify_keep__")
        self.assertEqual(rec.AGENT_NAME, "lily")
        self.assertEqual(rec.TABLE, "fleet_tts_events")
        src = Path(rec.__file__).read_text(encoding="utf-8")
        self.assertNotIn("http.delete", src)
        self.assertNotIn(".delete(", src)
        self.assertNotIn("session_id=eq.__s3_verify_keep__", src)

    def test_record_tts_event_posts_expected_payload_and_never_raises(self):
        posted = {}

        class _Resp:
            status = 201

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Sess:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                posted["url"] = url
                posted["json"] = json
                posted["headers"] = headers
                return _Resp()

        rec.bind_session_id("lily-TEST-room")
        rec.bind_node_name("LilyAgent")

        async def _go():
            with mock.patch.object(rec, "_credentials", return_value=("https://sb.example", "key")):
                with mock.patch.object(rec.aiohttp, "ClientSession", _Sess):
                    rec.record_tts_event(
                        transport="ws_ttd",
                        model_id="eleven_v3_conversational",
                        voice_id="W3C2vBPukr5b5jvoXhPK",
                        chars=12,
                        ttfb_ms=381.5,
                        total_ms=900.4,
                        audio_ms=800.0,
                        chunks=4,
                        interrupted=False,
                        contexts_open_max=1,
                        speech_id="sp-1",
                        raw={"setup_frame": {"voice_settings": {"stability": 0.5}}},
                    )
                    await asyncio.sleep(0.05)

        asyncio.run(_go())
        self.assertTrue(posted["url"].endswith("/rest/v1/fleet_tts_events"))
        body = posted["json"]
        self.assertEqual(body["agent_name"], "lily")
        self.assertEqual(body["session_id"], "lily-TEST-room")
        self.assertEqual(body["node_name"], "LilyAgent")
        self.assertEqual(body["transport"], "ws_ttd")
        self.assertEqual(body["model_id"], "eleven_v3_conversational")
        self.assertEqual(body["speech_id"], "sp-1")
        self.assertEqual(body["chars"], 12)
        self.assertEqual(body["ttfb_ms"], 382)
        self.assertIsInstance(body["ttfb_ms"], int)
        self.assertEqual(body["total_ms"], 900)
        self.assertEqual(body["audio_ms"], 800)
        self.assertFalse(body["interrupted"])
        self.assertEqual(body["raw"]["setup_frame"]["voice_settings"]["stability"], 0.5)
        self.assertNotEqual(body["session_id"], rec.KEEP_SESSION_ID)
        self.assertEqual(posted["headers"]["Prefer"], "return=minimal")

    def test_write_failure_is_swallowed(self):
        async def _boom(_payload):
            raise RuntimeError("nope")

        async def _go():
            with mock.patch.object(rec, "_insert", _boom):
                rec.record_tts_event(
                    transport="ws_ttd",
                    model_id="eleven_v3_conversational",
                    voice_id="v",
                    chars=1,
                    ttfb_ms=None,
                    total_ms=None,
                    audio_ms=None,
                    chunks=0,
                    interrupted=True,
                    contexts_open_max=0,
                    error="boom",
                )
                await asyncio.sleep(0.05)

        asyncio.run(_go())  # must not raise

    def test_missing_creds_skips_without_raising(self):
        async def _go():
            with mock.patch.object(rec, "_credentials", return_value=("", "")):
                rec.record_tts_event(
                    transport="ws_ttd", model_id="m", voice_id="v", chars=0,
                    ttfb_ms=None, total_ms=None, audio_ms=None, chunks=0,
                    interrupted=False, contexts_open_max=0,
                )
                await asyncio.sleep(0.05)

        asyncio.run(_go())
