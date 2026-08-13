"""P0-C: client/durable transcript uses exact post-TTS text."""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit import rtc

import lily_agent
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _Publication:
    kind = rtc.TrackKind.KIND_AUDIO
    sid = "TR_AGENT_AUDIO"


class _Participant:
    def __init__(self) -> None:
        self.identity = "agent-test"
        self.track_publications = {"audio": _Publication()}
        self.transcriptions = []

    async def publish_transcription(self, transcription) -> None:
        self.transcriptions.append(transcription)


def _game() -> tuple[LilyGame, _Participant]:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("post-tts")
    game._post_tts_text_by_speech_id = {}
    participant = _Participant()
    room = type("Room", (), {"local_participant": participant})()
    game.ctx = type("Ctx", (), {"room": room})()
    return game, participant


def test_post_tts_map_is_handle_scoped_and_consumed():
    game, _ = _game()
    game.note_post_tts_text("s1", "Actual sheet.")
    game.note_post_tts_text("s2", "Actual verdict.")

    assert game.consume_post_tts_text("s1", "raw") == "Actual sheet."
    assert game.consume_post_tts_text("s1", "fallback") == "fallback"
    assert game.consume_post_tts_text("s2", "raw") == "Actual verdict."


def test_manual_rtc_transcript_publishes_authoritative_text():
    game, participant = _game()

    async def scenario() -> None:
        game.publish_agent_transcription_nowait(
            "Assassinated on the Ides of March—who was this Roman dictator?",
            speech_id="speech-q6",
            interrupted=False,
        )
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(participant.transcriptions) == 1
    event = participant.transcriptions[0]
    assert event.participant_identity == "agent-test"
    assert event.track_sid == "TR_AGENT_AUDIO"
    assert len(event.segments) == 1
    segment = event.segments[0]
    assert segment.id == "speech-q6"
    assert "Roman dictator" in segment.text
    assert segment.final is True


def test_interrupted_rtc_transcript_is_marked_cut_off():
    game, participant = _game()

    async def scenario() -> None:
        game.publish_agent_transcription_nowait(
            "The transformed sentence",
            speech_id="speech-cut",
            interrupted=True,
        )
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert participant.transcriptions[0].segments[0].text.endswith(
        "…[cut off]"
    )


def test_default_pre_tts_room_text_output_is_disabled():
    source = inspect.getsource(lily_agent.entrypoint)
    assert "text_output=False" in source
