"""
lily_stt_tuning.py — STT configuration study + tuned-config artifact
(WO-LILY-OMNIBUS-003 WS-13, as amended by AMENDMENT-001/-002).

VERIFIED against the pinned installed stack (source read, not docs/training
data): livekit-plugins-speechmatics 1.6.8, speechmatics-voice 0.2.8,
speechmatics-rt 1.1.0. Evidence base: session lily-81BCB0-583a0f16
(2026-08-05, 4-player echo room) — see tests/fixtures/echo_room_81BCB0.json.

Installed-surface facts this module is built on:

  * `audio_filtering_config` is HARDCODED to `{"volume_threshold": 0.0}` by
    the voice client (speechmatics/voice/_client.py, `_prepare_config`) on
    EVERY session — the plugin exposes no kwarg for it. Because the field is
    already on the wire, overriding its value is schema-safe (unlike
    `max_alternatives`, which the voice endpoint REJECTED at the protocol
    level — live incident 2026-07-14, see lily_config.stt_max_alternatives).
  * `speaker_diarization_config.get_speakers` is a documented RT-API
    StartRecognition field ("If true, speaker identifiers will be returned
    at the end of transcript" -> unsolicited SpeakersResult message) but has
    NO field on the installed SDK's SpeakerDiarizationConfig dataclass, and
    `TranscriptionConfig.to_dict()` (dataclasses.asdict) drops undeclared
    attributes — so, exactly as with lily_nbest's `max_alternatives`, the
    StartRecognition wire dict is the only injection point that survives
    serialization.
  * In FIXED end-of-utterance mode (Lily's mode) the client's
    `end_of_utterance_max_delay` ceiling is NEVER applied: the clamp lives in
    `_calculate_finalize_delay`, which returns early through
    `_calculate_fixed_finalize_delay` for FIXED — finalization depends
    entirely on the SERVER's silence-triggered EndOfUtterance. Continuous
    cross-talk/reverb starves that trigger; that is the 206-second-segment
    mechanism, and no config value at this pin caps it. Span sanity is
    therefore a QUARANTINE concern (WS-10), not a config lever.
  * `additional_vocab` and every diarization kwarg are constructor-only:
    `update_speakers()` carries focus/ignore/focus_mode ONLY, so player
    names can ride additional_vocab exclusively when known BEFORE
    construction (device-candidate voiceprints). Mid-session bind cannot
    add vocab without a full STT swap (`Agent.update_options(stt=...)`,
    1.6.6 — WS-8's lever, recommended not wired here).

Scoring (AMENDMENT-002, program-wide): matrix scoring uses machine metrics —
WER and DER against fixture ground truth — never perceptual quality.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

import lily_config

logger = logging.getLogger("lily_stt_tuning")

# ---------------------------------------------------------------------------
# Tuned configuration artifact (WS-15 incumbent arm; WS-8/WS-10 thresholds)
# ---------------------------------------------------------------------------

# The chosen config. Every value either matches the session baseline (kept
# deliberately, rationale in README close-out table) or is a tuned change
# grounded in the echo-room evidence. `volume_threshold` stays 0.0 live:
# without recoverable session audio there is no calibration ground truth,
# and an uncalibrated pre-ASR floor risks dropping quiet REAL speech (the
# session already lost answers to client-side mic-ducking). The lever is
# wired and schema-safe; WS-15 sweeps LILY_STT_MATRIX_AXES cells on the
# bake-off fixture before a non-zero floor ships.
LILY_STT_TUNED: dict[str, Any] = {
    "artifact": "lily-stt-tuned",
    "source_ws": "WO-LILY-OMNIBUS-003/WS-13",
    "evidence_session": "lily-81BCB0-583a0f16",
    "stack_pins": {
        "livekit-plugins-speechmatics": "1.6.8",
        "speechmatics-voice": "0.2.8",
        "speechmatics-rt": "1.1.0",
    },
    "constructor": {
        "language": "en",
        "model": "enhanced",
        "enable_diarization": True,
        "speaker_active_format": "[{speaker_id}] {text}",
        # 0.5 (default) minted 3 phantom labels + 1 label-continuity split
        # for 4 players in one reverberant session. Lower sensitivity
        # biases matching toward existing/enrolled voices over minting
        # generic ones; 0.35 is the matrix center of the 0.3-0.5 band.
        "speaker_sensitivity": 0.35,
        "prefer_current_speaker": True,
        # Roster-aware: bound players + 1 margin (see max_speakers_for).
        # The session's S7 dumping ground was the fixed 7-cap exhausting;
        # enrolled speakers do NOT consume the generic budget.
        "max_speakers": 7,  # fallback when roster unknown at construction
        "max_delay": 1.5,
        "end_of_utterance_silence_trigger": 0.8,
        "turn_detection_mode": "fixed",
        "include_partials": True,
        "ignore_speakers": ["__ASSISTANT__"],
    },
    "wire_injection": {
        # Injected into the StartRecognition wire dict (schema-verified
        # against the live voice endpoint — see README close-out).
        "get_speakers": True,
        "volume_threshold": 0.0,
    },
    # WS-10 quarantine threshold: longest legitimate player span in the
    # evidence session was 20.1s; the corrupted spans were 104.1s and
    # 206.0s. 30s catches every observed corruption with margin over
    # every observed legitimate turn.
    "ws10_span_quarantine_seconds": 30.0,
    # WS-8 ghost-fold input: per-word `volume` labels ride AddTranscript
    # results whenever audio_filtering_config is present (it always is —
    # the SDK sends it unconditionally); echo copies run quieter.
    "ws8_per_word_volume_available": True,
}

# Matrix axes for the WS-15 bake-off (incumbent arm sweeps these on the
# acoustic fixture; this module's scorers are the shared metric set).
LILY_STT_MATRIX_AXES: dict[str, list] = {
    "speaker_sensitivity": [0.3, 0.4, 0.5],
    "max_speakers": [5, 6, 7],
    "volume_threshold": [0.0, 1.6, 3.2],
}




def lily_max_speakers_for(roster_size: Optional[int]) -> int:
    """Roster-aware generic-speaker cap: bound players + 1 margin, clamped
    to the plugin validator's floor (2) and the product cap fallback (7).
    Enrolled (known) speakers do not consume this budget."""
    fallback = int(LILY_STT_TUNED["constructor"]["max_speakers"])
    if not roster_size or roster_size < 1:
        return fallback
    return max(2, min(fallback, int(roster_size) + 1))


def lily_tuned_stt_kwargs(
    roster_size: Optional[int] = None,
    player_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Constructor kwargs for SpeechmaticsSTT from the tuned artifact.

    Returns plain-value kwargs only (enums/vocab/known_speakers stay at the
    call site, which owns the plugin imports). `player_names` becomes
    additional_vocab content — the bounded, stable set the pin allows at
    construction time; NEVER answer nouns."""
    c = LILY_STT_TUNED["constructor"]
    kwargs: dict[str, Any] = {
        "language": c["language"],
        "enable_diarization": c["enable_diarization"],
        "speaker_active_format": c["speaker_active_format"],
        "speaker_sensitivity": c["speaker_sensitivity"],
        "prefer_current_speaker": c["prefer_current_speaker"],
        "max_speakers": lily_max_speakers_for(roster_size),
        "max_delay": c["max_delay"],
        "end_of_utterance_silence_trigger": c["end_of_utterance_silence_trigger"],
        "include_partials": c["include_partials"],
        "ignore_speakers": list(c["ignore_speakers"]),
    }
    return kwargs


_ENGINE_SPEAKER_LABEL_RE = re.compile(r"[Ss]\d+")


def lily_is_engine_speaker_label(label) -> bool:
    """True for Speechmatics' own diarization labels (S1, S2, … — any
    case). The engine forbids them as known-speaker labels in
    StartRecognition; the whole STT session fails validation if one is
    injected (HOTFIX-ENGINE-LABEL-001)."""
    return bool(_ENGINE_SPEAKER_LABEL_RE.fullmatch(str(label or "").strip()))


def lily_filter_enrollable_speakers(rows: list[dict]) -> list[dict]:
    """Enrollment hygiene chokepoint for known-speaker rows.

    1. Drop dunder-wrapped labels (`__ASSISTANT__`-style). The engine
       reserves dunder labels for ignore semantics; a corrupt voiceprint
       row must never enroll real identifiers under one — that would
       convert the inert echo-guard label into an ACTIVE matcher for a
       real voice and silently drop that player's speech.
    2. MERGE duplicate labels (WO-LILY-HOTFIX-002 Defect 2). A group can
       hold several voiceprint rows for the same player name (one per
       engine label they were heard under — the live 41dfc215 group
       carried Chris twice, via S1 and S4). Duplicate labels inside
       StartRecognition's speakers list are undefined engine behaviour;
       the same-name rows are the same human, so their identifier blobs
       merge under one label (all blobs kept as match hints).
    3. HOTFIX-ENGINE-LABEL-001 (live 2026-09-06 12:21, three sessions
       deaf — lily_speaker_voiceprints row 427, label "S1", player_name
       null): Speechmatics reserves its own diarization labels (S1, S2, …)
       and StartRecognition REJECTS a known speaker carrying one —
       "speakers.0.label: Must not validate the schema (not)" — which kills
       the whole STT pump: VAD hears the room, nothing is ever transcribed.
       Two rules, each logged with the row id: never inject a row whose
       label is an engine label, and never inject a row whose player_name
       is null (an unbound voice is not an identity; rows that carry no
       player_name key at all — synthetic, pre-loader — are not judged by
       the second rule).
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for row in rows or []:
        row = row or {}
        label = str(row.get("label") or "")
        row_id = row.get("id")
        if label.startswith("__") and label.endswith("__"):
            logger.warning(
                "LILY_STT_TUNING | dunder label dropped from enrollment: %s "
                "row_id=%s", label, row_id,
            )
            continue
        if lily_is_engine_speaker_label(label):
            logger.warning(
                "LILY_STT_TUNING | ENGINE_LABEL_SKIPPED | label=%s row_id=%s "
                "(Speechmatics rejects S<n> known-speaker labels; the row is "
                "unbound — bind the player to enroll under a name)",
                label, row_id,
            )
            continue
        if "player_name" in row and not str(row.get("player_name") or "").strip():
            logger.warning(
                "LILY_STT_TUNING | NULL_PLAYER_NAME_SKIPPED | label=%s row_id=%s "
                "(voiceprint row has no bound player; not injected)",
                label, row_id,
            )
            continue
        identifiers = (row or {}).get("speaker_identifiers") or []
        if not isinstance(identifiers, list):
            identifiers = [identifiers]
        if label not in merged:
            merged[label] = dict(row)
            merged[label]["speaker_identifiers"] = list(identifiers)
            order.append(label)
            continue
        existing = merged[label]["speaker_identifiers"]
        added = [i for i in identifiers if i not in existing]
        if added:
            existing.extend(added)
        logger.warning(
            "LILY_STT_TUNING | duplicate enrollment label merged: %s "
            "(+%d identifier(s))", label, len(added),
        )
    return [merged[label] for label in order]


# ---------------------------------------------------------------------------
# StartRecognition wire injection (get_speakers + volume_threshold)
# ---------------------------------------------------------------------------

_PATCH_FLAG = "_lily_stt_tuning_patched"
_SPEAKERS_FLAG = "_lily_speakers_capture_patched"

# Last SpeakersResult captured from the server (end-of-transcript push when
# get_speakers is injected, or any GetSpeakers reply). Survives STT stream
# teardown — the enrollment fallback reads it after the websocket closes.
_captured_speakers: list[dict] = []


def lily_captured_speakers() -> list[dict]:
    """The most recent SpeakersResult payload ([{label, speaker_identifiers}]
    dicts). Empty until the server has pushed one."""
    return list(_captured_speakers)


def _lily_reset_captured_speakers() -> None:
    """Test hook."""
    _captured_speakers.clear()


def lily_install_stt_tuning_patch(
    get_speakers: bool = True,
    volume_threshold: float = 0.0,
    _base_client_module: Any = None,
    _voice_client_cls: Any = None,
) -> bool:
    """Arm the two-part tuning patch:

      1. CONFIG INJECTION — wrap
         `speechmatics.rt._base_client.build_start_recognition_message` so
         the outgoing StartRecognition `transcription_config` dict carries
         `speaker_diarization_config.get_speakers` and
         `audio_filtering_config.volume_threshold`. Composes with the
         lily_nbest wrapper (each wraps whatever is currently bound, under
         its own idempotency flag).
      2. SPEAKERS CAPTURE — wrap `VoiceAgentClient.connect` to register a
         raw `SpeakersResult` handler that stores the payload in a
         module-level slot which SURVIVES stream teardown (the plugin's own
         `_speaker_result` needs a live websocket to be read via
         get_speaker_ids; session-close enrollment always found it dead —
         LILY_ENROLL 2026-07-15 finding).

    Same failure contract as lily_nbest: any failure logs
    `LILY_STT_TUNING | patch=failed` and returns False; the session runs on
    the un-injected config. NEVER raises."""
    try:
        vac = _voice_client_cls
        if vac is None:
            from speechmatics.voice import VoiceAgentClient as vac  # type: ignore

        orig_connect: Callable = vac.connect
        if not callable(orig_connect):
            raise TypeError("VoiceAgentClient.connect is not callable")

        bc = _base_client_module
        if bc is None:
            import speechmatics.rt._base_client as bc  # type: ignore
        orig_build: Callable = bc.build_start_recognition_message
        if not callable(orig_build):
            raise TypeError("build_start_recognition_message is not callable")

        # 1) Config injection (idempotent under this module's flag).
        if not getattr(bc, _PATCH_FLAG, False):
            def _lily_build_start_recognition_tuned(*args: Any, **kwargs: Any) -> Any:
                msg = orig_build(*args, **kwargs)
                try:
                    tc = msg.get("transcription_config")
                    if isinstance(tc, dict):
                        if get_speakers:
                            dz = tc.setdefault("speaker_diarization_config", {})
                            if isinstance(dz, dict):
                                dz["get_speakers"] = True
                        af = tc.setdefault("audio_filtering_config", {})
                        if isinstance(af, dict):
                            af["volume_threshold"] = float(volume_threshold)
                        # WO-LILY-HOTFIX-002 Defect 2: wire-level truth of
                        # known-speaker enrollment. "VOICEPRINT | injected"
                        # at construction says what Lily HANDED the plugin;
                        # this says what actually rode StartRecognition —
                        # the discriminator between "injection broken" and
                        # "engine didn't match" when recognition fails.
                        dz_out = tc.get("speaker_diarization_config")
                        enrolled = (
                            dz_out.get("speakers")
                            if isinstance(dz_out, dict)
                            else getattr(dz_out, "speakers", None)
                        )
                        logger.info(
                            "LILY_STT_TUNING | config_injected "
                            "get_speakers=%s volume_threshold=%.2f "
                            "wire_known_speakers=%d",
                            get_speakers, float(volume_threshold),
                            len(enrolled) if isinstance(enrolled, list) else 0,
                        )
                    else:
                        logger.warning(
                            "LILY_STT_TUNING | config_injection skipped — "
                            "unexpected StartRecognition shape"
                        )
                except Exception as e:
                    logger.warning(
                        "LILY_STT_TUNING | config_injection failed: %s", e
                    )
                return msg

            bc.build_start_recognition_message = _lily_build_start_recognition_tuned
            setattr(bc, _PATCH_FLAG, True)

        # 2) SpeakersResult capture (idempotent).
        if not getattr(vac, _SPEAKERS_FLAG, False):
            def _capture(message: dict) -> None:
                try:
                    speakers = message.get("speakers")
                    if isinstance(speakers, list):
                        _captured_speakers.clear()
                        _captured_speakers.extend(
                            s for s in speakers if isinstance(s, dict)
                        )
                        logger.info(
                            "LILY_STT_TUNING | speakers_captured n=%d",
                            len(_captured_speakers),
                        )
                except Exception as e:
                    logger.warning(
                        "LILY_STT_TUNING | speakers capture failed: %s", e
                    )

            async def _lily_connect_tuned(self: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    self.on("SpeakersResult", _capture)
                except Exception as e:
                    logger.warning(
                        "LILY_STT_TUNING | SpeakersResult tap failed: %s", e
                    )
                return await orig_connect(self, *args, **kwargs)

            vac.connect = _lily_connect_tuned
            setattr(vac, _SPEAKERS_FLAG, True)

        logger.info(
            "LILY_STT_TUNING | patch=armed get_speakers=%s volume_threshold=%.2f",
            get_speakers, float(volume_threshold),
        )
        return True
    except Exception as e:
        logger.warning("LILY_STT_TUNING | patch=failed reason=%s", e)
        return False


# ---------------------------------------------------------------------------
# Session-start STT wiring helpers (moved from lily_agent, REFACTOR Stage 1a)
# ---------------------------------------------------------------------------

def lily_stt_focus_kwargs(known_speakers) -> dict:
    """WO-LILY-STT-001 Q0: the Speechmatics focus kwargs. Returns
    focus_speakers + focus_mode=IGNORE ONLY when focus is enabled AND the
    enrolled set has usable labels; {} otherwise. The non-empty guard is the
    safety invariant — focus_mode=IGNORE with no focus set drops every voice,
    muting the whole table, so it is withheld (loudly) rather than risked."""
    if lily_config.stt_focus_mode() != "ignore":
        return {}
    # Lazy: keeps this module importable without the speechmatics plugin
    # (eval/ scripts); the enum is only needed on the enabled path.
    from livekit.plugins.speechmatics import SpeakerFocusMode

    labels = [s.label for s in (known_speakers or []) if getattr(s, "label", None)]
    if not labels:
        logger.warning(
            "LILY_STT_FOCUS | WITHHELD | reason=no_enrolled_speakers — "
            "focus_mode=IGNORE never enabled on an empty set (would mute the "
            "table)"
        )
        return {}
    return {"focus_speakers": labels, "focus_mode": SpeakerFocusMode.IGNORE}


def lily_stt_config_applied(stt) -> dict:
    """WO-LILY-STT-001 Q3: the EFFECTIVE Speechmatics config, read off the
    constructed STT's _stt_options (what the wire will actually carry) — not
    what we intended to set. Logged at session start and asserted
    intended==applied by test, so the audit's claimed-but-unwired class (the
    max_speakers=7 ghost that was never wired to roster) reads red at build
    time instead of hiding live. Defensive: returns {} if the options object
    isn't present (test stubs)."""
    opts = getattr(stt, "_stt_options", None)
    if opts is None:
        return {}

    def _name(v):
        return getattr(v, "value", None) or getattr(v, "name", None) or str(v)

    return {
        "model": str(getattr(stt, "model", "enhanced")),
        "turn_detection_mode": _name(getattr(opts, "turn_detection_mode", None)),
        "max_delay": getattr(opts, "max_delay", None),
        "speaker_sensitivity": getattr(opts, "speaker_sensitivity", None),
        "max_speakers": getattr(opts, "max_speakers", None),
        "prefer_current_speaker": getattr(opts, "prefer_current_speaker", None),
        "enable_diarization": getattr(opts, "enable_diarization", None),
        "focus_mode": _name(getattr(opts, "focus_mode", None)),
        "focus_speakers": len(getattr(opts, "focus_speakers", None) or []),
        "known_speakers": len(getattr(opts, "known_speakers", None) or []),
        "additional_vocab": len(getattr(opts, "additional_vocab", None) or []),
    }


# ---------------------------------------------------------------------------
# Offline scoring tooling — moved to scripts/lily_stt_scoring.py (REFACTOR
# Stage 1a). Lazy shims keep the historical import names working for tests
# and eval/ without putting scripts/ on the live agent's import path.
# ---------------------------------------------------------------------------

def lily_matrix_cells() -> list[dict[str, Any]]:
    """See scripts/lily_stt_scoring.lily_matrix_cells."""
    from scripts.lily_stt_scoring import lily_matrix_cells as _impl
    return _impl()


def lily_wer(reference: str, hypothesis: str) -> float:
    """See scripts/lily_stt_scoring.lily_wer."""
    from scripts.lily_stt_scoring import lily_wer as _impl
    return _impl(reference, hypothesis)


def lily_der(
    reference_segments: list[dict],
    hypothesis_segments: list[dict],
) -> float:
    """See scripts/lily_stt_scoring.lily_der."""
    from scripts.lily_stt_scoring import lily_der as _impl
    return _impl(reference_segments, hypothesis_segments)


def lily_score_fixture(
    rows: list[dict],
    ground_truth: dict,
    span_quarantine_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """See scripts/lily_stt_scoring.lily_score_fixture."""
    from scripts.lily_stt_scoring import lily_score_fixture as _impl
    return _impl(rows, ground_truth, span_quarantine_seconds)


def lily_assistant_leak_scan(
    rows: list[dict],
    assistant_label: str,
    min_words: int = 8,
) -> list[dict]:
    """See scripts/lily_stt_scoring.lily_assistant_leak_scan."""
    from scripts.lily_stt_scoring import lily_assistant_leak_scan as _impl
    return _impl(rows, assistant_label, min_words)
