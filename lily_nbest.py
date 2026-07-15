"""
lily_nbest.py — n-best ASR hypothesis recovery (WO-LILY-ADDRESSEE-H1-001 Task 1).

VERIFIED against the pinned installed stack (source read, not docs/training
data): livekit-plugins-speechmatics 1.6.4, speechmatics-voice 0.2.8,
speechmatics-rt 1.1.0.

  * There is NO per-utterance n-best anywhere in the stack. The Speechmatics
    voice client collapses every recognition result to `alternatives[0]`
    (speechmatics/voice/_client.py, `_add_speech_fragments`) and the LiveKit
    plugin emits exactly one `SpeechData` per segment
    (livekit/plugins/speechmatics/stt.py, `_send_frames`).
  * Per-WORD alternatives DO ride the raw `AddTranscript` messages
    (`results[i].alternatives`, each {content, confidence, speaker, ...}) and
    the raw message is emitted on the client's EventEmitter by message name
    (`_BaseClient._recv_loop` emits `msg["message"]`), so an extra
    `client.on("AddTranscript", cb)` handler recovers them losslessly.
  * There is NO supported config knob for the alternatives count: the plugin's
    `transcription_config=` kwarg is DEPRECATED AND IGNORED at 1.6.4, and the
    `VoiceAgentConfig.advanced_engine_control` merge sets attributes that
    `TranscriptionConfig.to_dict()` (dataclasses.asdict — declared fields
    only) silently DROPS from the wire. The only reliable injection point is
    the StartRecognition message builder
    (`speechmatics.rt._base_client.build_start_recognition_message`), wrapped
    here to add `"max_alternatives"` to the outgoing `transcription_config`
    dict.

So this module implements PER-WORD recovery and synthesizes utterance-level
hypothesis strings conservatively (1-best backbone + bounded
single-substitution variants). The synthesized hypotheses widen what the
player may have SAID — never what the answer IS (judge-never-invents is
enforced at the prompt layer in lily_evaluation).

Everything here is defensive: if the plugin internals shift, the patch
installer logs a `LILY_NBEST | patch=failed` warning and returns False, and
the pipeline degrades cleanly to 1-best. Nothing in this module may raise
into the live session.

Pure-data core (dispersion, synthesis, collector) has zero livekit /
speechmatics imports — importable and testable anywhere. The installer
imports speechmatics lazily inside its try block.
"""

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("lily_nbest")

# Hard ceiling on synthesized hypotheses regardless of config — the judge
# prompt must stay bounded (config asks for 3–5; 8 is the absolute lid).
MAX_HYPOTHESES_CEILING = 8

# Collector buffer lid: ~200 words is minutes of table talk; anything older
# was never drained (no final segment consumed it) and is stale.
MAX_BUFFER_WORDS = 200


# ---------------------------------------------------------------------------
# Dispersion — the deliberation signal (report Track 1 feature table)
# ---------------------------------------------------------------------------

def lily_nbest_dispersion(confidences: Optional[list]) -> Optional[float]:
    """Population variance of hypothesis confidences. High dispersion means
    the recognizer itself was torn — a deliberation / fractured-speech
    signal. Edge contract: empty/None -> None (no signal), single
    hypothesis -> 0.0 (a confident recognizer is not deliberating).
    Accepts floats or hypothesis dicts carrying a "confidence" key."""
    if not confidences:
        return None
    vals: list[float] = []
    for c in confidences:
        if isinstance(c, dict):
            c = c.get("confidence")
        if isinstance(c, bool):  # bools are ints; never confidences
            continue
        if isinstance(c, (int, float)):
            vals.append(float(c))
    if not vals:
        return None
    if len(vals) == 1:
        return 0.0
    mean = sum(vals) / len(vals)
    return round(sum((v - mean) ** 2 for v in vals) / len(vals), 6)


# ---------------------------------------------------------------------------
# Utterance synthesis from per-word alternatives
# ---------------------------------------------------------------------------

def lily_synthesize_hypotheses(
    words: list[dict],
    max_hypotheses: int = 3,
) -> list[dict]:
    """Synthesize bounded utterance-level hypotheses from per-word
    alternatives.

    `words` is an ordered list of {"alternatives": [{"content", "confidence"},
    ...]} dicts (raw AddTranscript result shape, word-type results only).

    Conservative by construction: hypothesis 0 is always the 1-best backbone
    (top alternative of every word); the remaining slots are SINGLE-word
    substitutions of the backbone ranked by mean word confidence. No
    combinatorial explosion — at most sum(len(alts)-1) variants are even
    considered, and the output is capped at min(max_hypotheses,
    MAX_HYPOTHESES_CEILING). Hypothesis confidence is the mean word
    confidence. Returns [] when no usable words."""
    max_hypotheses = max(1, min(int(max_hypotheses), MAX_HYPOTHESES_CEILING))

    # Clean: keep words with at least one non-empty alternative content.
    cleaned: list[list[dict]] = []
    for w in words or []:
        alts = [
            {
                "content": str(a.get("content", "")).strip(),
                "confidence": float(a["confidence"])
                if isinstance(a.get("confidence"), (int, float))
                else 1.0,
            }
            for a in (w or {}).get("alternatives") or []
            if isinstance(a, dict) and str(a.get("content", "")).strip()
        ]
        if alts:
            cleaned.append(alts)
    if not cleaned:
        return []

    top_confs = [alts[0]["confidence"] for alts in cleaned]
    n = len(cleaned)

    def _mean(vals: list[float]) -> float:
        return round(sum(vals) / len(vals), 4)

    backbone_text = " ".join(alts[0]["content"] for alts in cleaned)
    hypotheses: list[dict] = [
        {"text": backbone_text, "confidence": _mean(top_confs)}
    ]
    seen = {backbone_text.lower()}

    # Single-substitution variants, ranked by mean confidence.
    variants: list[dict] = []
    for i, alts in enumerate(cleaned):
        for alt in alts[1:]:
            text = " ".join(
                alt["content"] if j == i else cleaned[j][0]["content"]
                for j in range(n)
            )
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            confs = list(top_confs)
            confs[i] = alt["confidence"]
            variants.append({"text": text, "confidence": _mean(confs)})
    variants.sort(key=lambda h: h["confidence"], reverse=True)
    hypotheses.extend(variants[: max_hypotheses - 1])
    return hypotheses


# ---------------------------------------------------------------------------
# Collector — buffers per-word alternatives from raw AddTranscript messages
# ---------------------------------------------------------------------------

class LilyNBestCollector:
    """Per-session buffer of per-word alternatives tapped off raw
    `AddTranscript` messages. The agent drains it once per finalized
    transcript segment; drain synthesizes the utterance hypotheses and the
    dispersion signal. One collector per worker job process (LiveKit runs
    one job per process). All entry points are exception-proof — a
    malformed message can never take down the STT receive loop."""

    def __init__(
        self,
        max_hypotheses: int = 3,
        max_buffer_words: int = MAX_BUFFER_WORDS,
    ) -> None:
        self.max_hypotheses = max(1, min(int(max_hypotheses), MAX_HYPOTHESES_CEILING))
        self.max_buffer_words = max(1, int(max_buffer_words))
        self._words: list[dict] = []

    def ingest_message(self, message: Any) -> None:
        """EventEmitter callback for raw AddTranscript messages. Synchronous
        (the emitter rejects coroutines), cheap, and never raises."""
        try:
            if not isinstance(message, dict):
                return
            for result in message.get("results") or []:
                if not isinstance(result, dict):
                    continue
                if result.get("type") != "word":
                    continue  # punctuation is transcriber-inferred, skip
                alts = [
                    a for a in result.get("alternatives") or []
                    if isinstance(a, dict) and str(a.get("content", "")).strip()
                ]
                if not alts:
                    continue
                self._words.append({
                    "start_time": result.get("start_time", 0.0),
                    "end_time": result.get("end_time", 0.0),
                    "speaker": alts[0].get("speaker"),
                    "alternatives": [
                        {
                            "content": str(a.get("content", "")).strip(),
                            "confidence": float(a["confidence"])
                            if isinstance(a.get("confidence"), (int, float))
                            else 1.0,
                        }
                        for a in alts
                    ],
                })
            if len(self._words) > self.max_buffer_words:
                self._words = self._words[-self.max_buffer_words:]
        except Exception as e:
            logger.debug("LILY_NBEST | ingest failed: %s", e)

    def drain(self, speaker_label: Optional[str] = None) -> Optional[dict]:
        """Consume buffered words (optionally only the given diarization
        speaker's, e.g. "S1") and synthesize the utterance-level n-best dict:

          {"hypotheses": [{"text", "confidence"}, ...],   # slot 0 = 1-best
           "dispersion": float,                            # variance signal
           "word_count": int,
           "source": "per_word_synthesis"}

        Returns None when nothing was buffered. Never raises."""
        try:
            if speaker_label:
                taken = [
                    w for w in self._words
                    if w.get("speaker") in (speaker_label, None)
                ]
                self._words = [w for w in self._words if w not in taken]
            else:
                taken, self._words = self._words, []
            if not taken:
                return None
            taken.sort(key=lambda w: w.get("start_time", 0.0))
            hypotheses = lily_synthesize_hypotheses(taken, self.max_hypotheses)
            if not hypotheses:
                return None
            return {
                "hypotheses": hypotheses,
                "dispersion": lily_nbest_dispersion(hypotheses),
                "word_count": len(taken),
                "source": "per_word_synthesis",
            }
        except Exception as e:
            logger.warning("LILY_NBEST | drain failed: %s — 1-best only", e)
            return None


# ---------------------------------------------------------------------------
# Config-injection + raw-message-tap patch installer
# ---------------------------------------------------------------------------

_PATCH_FLAG = "_lily_nbest_patched"


def lily_install_nbest_stt_patch(
    collector: LilyNBestCollector,
    max_alternatives: int,
    _base_client_module: Any = None,
    _voice_client_cls: Any = None,
) -> bool:
    """Arm the two-part n-best recovery patch:

      1. CONFIG INJECTION — wrap
         `speechmatics.rt._base_client.build_start_recognition_message` so
         the outgoing StartRecognition `transcription_config` dict carries
         `max_alternatives` (there is no declared field for it at
         speechmatics-rt 1.1.0; `TranscriptionConfig.to_dict()` drops
         undeclared attributes, so the wire dict is the only injection
         point that survives serialization).
      2. RAW TAP — wrap `VoiceAgentClient.connect` to register the
         collector's `ingest_message` for raw `AddTranscript` events before
         connecting (the voice client itself keeps only `alternatives[0]`).

    Returns True when armed. `max_alternatives < 2` disables the patch
    (nothing to recover). Any failure — plugin internals shifted, imports
    missing — logs `LILY_NBEST | patch=failed` and returns False; the
    pipeline degrades cleanly to 1-best. NEVER raises.

    `_base_client_module` / `_voice_client_cls` are test injection points;
    production callers pass neither and the real speechmatics modules are
    imported lazily here."""
    try:
        max_alternatives = int(max_alternatives)
        if max_alternatives < 2:
            logger.info(
                "LILY_NBEST | patch=disabled max_alternatives=%d (1-best)",
                max_alternatives,
            )
            return False

        bc = _base_client_module
        if bc is None:
            import speechmatics.rt._base_client as bc  # type: ignore
        vac = _voice_client_cls
        if vac is None:
            from speechmatics.voice import VoiceAgentClient as vac  # type: ignore

        orig_build: Callable = bc.build_start_recognition_message
        if not callable(orig_build):
            raise TypeError("build_start_recognition_message is not callable")
        orig_connect: Callable = vac.connect
        if not callable(orig_connect):
            raise TypeError("VoiceAgentClient.connect is not callable")

        # 1) Config injection (idempotent).
        if not getattr(bc, _PATCH_FLAG, False):
            def _lily_build_start_recognition(*args: Any, **kwargs: Any) -> Any:
                msg = orig_build(*args, **kwargs)
                try:
                    tc = msg.get("transcription_config")
                    if isinstance(tc, dict):
                        tc["max_alternatives"] = max_alternatives
                        logger.info(
                            "LILY_NBEST | config_injected max_alternatives=%d",
                            max_alternatives,
                        )
                    else:
                        logger.warning(
                            "LILY_NBEST | config_injection skipped — "
                            "unexpected StartRecognition shape; 1-best only"
                        )
                except Exception as e:
                    logger.warning(
                        "LILY_NBEST | config_injection failed: %s — 1-best only", e
                    )
                return msg

            bc.build_start_recognition_message = _lily_build_start_recognition
            setattr(bc, _PATCH_FLAG, True)

        # 2) Raw AddTranscript tap (idempotent).
        if not getattr(vac, _PATCH_FLAG, False):
            async def _lily_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    self.on("AddTranscript", collector.ingest_message)
                except Exception as e:
                    logger.warning(
                        "LILY_NBEST | AddTranscript tap failed: %s — 1-best only",
                        e,
                    )
                return await orig_connect(self, *args, **kwargs)

            vac.connect = _lily_connect
            setattr(vac, _PATCH_FLAG, True)

        logger.info(
            "LILY_NBEST | patch=armed max_alternatives=%d", max_alternatives
        )
        return True
    except Exception as e:
        logger.warning(
            "LILY_NBEST | patch=failed reason=%s — degrading to 1-best", e
        )
        return False
