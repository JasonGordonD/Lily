"""Bake-off arms: the incumbent (Speechmatics ENHANCED, tuned) and the
challenger (pyannoteAI Live-1). Both vendor SDKs are lazy-imported so importing
this module is free; nothing here touches the agent boot path.

Incumbent produces (start, end, text, label) words directly — STT + built-in
diarization in one stream. Challenger produces a diarization-only turn stream
that game_metrics.word_align reconciles against the incumbent's STT words.
"""

from __future__ import annotations

import os
from typing import Optional


class ChallengerUnavailable(RuntimeError):
    """Raised when the Live-1 arm cannot be scored on this box (no credential /
    beta access / model reachability). Recorded verbatim in the scorecard —
    never substituted with fabricated numbers."""


# ---------------------------------------------------------------------------
# Incumbent — Speechmatics ENHANCED with the WS-13 tuned diarization config
# ---------------------------------------------------------------------------

class SpeechmaticsIncumbent:
    """Live Speechmatics RT decode. Uses the WS-13 tuned levers (stt_tuned.json)
    so the incumbent arm is exactly what Lily ships today."""

    def __init__(self, api_key: Optional[str] = None, tuned: Optional[dict] = None):
        self.api_key = api_key or os.environ.get("SPEECHMATICS_API_KEY")
        self.tuned = tuned or {}

    def available(self) -> bool:
        return bool(self.api_key)

    async def decode(self, pcm16, sample_rate: int = 16000) -> list[tuple]:
        if not self.api_key:
            raise ChallengerUnavailable("speechmatics-unavailable: no SPEECHMATICS_API_KEY")
        # Lazy — vendor SDK only loaded when a decode is actually requested.
        import io

        from speechmatics.rt import (
            AsyncClient,
            AudioEncoding,
            AudioFormat,
            Model,
            ServerMessageType,
            SpeakerDiarizationConfig,
            TranscriptionConfig,
            TranscriptResult,
        )

        c = self.tuned.get("constructor", {})
        words: list[tuple] = []
        client = AsyncClient(api_key=self.api_key)

        @client.on(ServerMessageType.ADD_TRANSCRIPT)
        def _on_final(msg):
            res = TranscriptResult.from_message(msg)
            for r in res.results:
                alts = getattr(r, "alternatives", None)
                if alts:
                    a = alts[0]
                    words.append(
                        (r.start_time, r.end_time, a.content,
                         str(getattr(a, "speaker", None)))
                    )

        cfg = TranscriptionConfig(
            language=c.get("language", "en"),
            model=Model.ENHANCED,
            diarization="speaker",
            max_delay=c.get("max_delay", 1.5),
            enable_partials=c.get("include_partials", True),
            speaker_diarization_config=SpeakerDiarizationConfig(
                max_speakers=c.get("max_speakers", 7),
                speaker_sensitivity=c.get("speaker_sensitivity", 0.35),
                prefer_current_speaker=c.get("prefer_current_speaker", True),
            ),
        )
        fmt = AudioFormat(
            encoding=AudioEncoding.PCM_S16LE, sample_rate=sample_rate, chunk_size=4096
        )
        await client.transcribe(
            io.BytesIO(pcm16.tobytes()), transcription_config=cfg, audio_format=fmt
        )
        return words


# ---------------------------------------------------------------------------
# Challenger — pyannoteAI Live-1 streaming diarization (WebSocket)
# ---------------------------------------------------------------------------

class PyannoteLive1Challenger:
    """pyannoteAI Live-1 streaming diarization arm.

    API shape (docs.pyannote.ai, verified 2026-08-05): 16 kHz mono, 100 ms
    chunks streamed over WebSocket; server emits `diarization_speaker_start` /
    `diarization_speaker_end` events, each carrying a timestamp + speaker label;
    up to 8 speakers, speaker-consistency layer only. NOTE: Live-1 has NO
    known-speaker enrollment / voiceprint / speaker-exclusion — those are
    Precision-2 (batch) features. Auth is a Bearer API key.

    This box has no pyannoteAI credential, so `diarize` raises
    ChallengerUnavailable rather than inventing a turn stream. The streaming
    body below is written to the documented protocol so the integration cost is
    priced against the real client shape, not a guess.
    """

    WS_URL = "wss://api.pyannote.ai/v1/diarize/live"  # documented Live-1 stream
    CHUNK_MS = 100

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (
            api_key
            or os.environ.get("PYANNOTEAI_API_KEY")
            or os.environ.get("PYANNOTE_API_KEY")
        )

    def available(self) -> bool:
        return bool(self.api_key)

    async def diarize(self, pcm16, sample_rate: int = 16000) -> list[dict]:
        """Returns diarization turns [{"speaker","start","end"}]. Raises
        ChallengerUnavailable when no credential is present on this box."""
        if not self.api_key:
            raise ChallengerUnavailable(
                "pyannote-live1-unavailable: no PYANNOTEAI_API_KEY on this box "
                "(Live-1 is closed-beta / paid; batch v1/diarize and the WS "
                "stream both require a pyannoteAI account key)"
            )
        # Lazy — only import a websocket client if we actually have a key.
        import json

        import websockets  # eval-only; not a runtime dep

        starts: dict[str, float] = {}
        turns: list[dict] = []
        chunk = int(sample_rate * self.CHUNK_MS / 1000)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with websockets.connect(self.WS_URL, extra_headers=headers) as ws:
            raw = pcm16.tobytes()
            step = chunk * 2  # int16
            for i in range(0, len(raw), step):
                await ws.send(raw[i : i + step])
                try:
                    msg = json.loads(await ws.recv())
                except Exception:
                    continue
                kind = msg.get("type")
                spk = msg.get("speaker")
                ts = msg.get("timestamp")
                if kind == "diarization_speaker_start":
                    starts[spk] = ts
                elif kind == "diarization_speaker_end" and spk in starts:
                    turns.append({"speaker": str(spk), "start": starts.pop(spk),
                                  "end": ts})
            await ws.send(json.dumps({"type": "end"}))
        return sorted(turns, key=lambda t: t["start"])
