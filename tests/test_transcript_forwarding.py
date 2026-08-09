"""Glass transcript forwarding (2026-08-09 live report: panel ran EMPTY).

P0-C set RoomOptions text_output=False so RoomIO's pre-TTS prose can't
race the corrected transcript — but that switch also turned off the
framework's USER transcript forwarding, and the agent's own final
transcript went out on the LEGACY rtc.Transcription API only, while the
glass panel consumes lk.transcription TEXT STREAMS (useTranscriptions).
Net: a live session showed an open transcript with nothing in it.

These tests pin the repaired wire shape offline:
  - the user forwarder publishes a final text stream impersonating the
    speaking device's participant (sender_identity), with the bound
    player name on prmpt.speaker_label when the roster resolves one;
  - the agent publisher mirrors its final text onto the stream wire
    (same segment id, final=true) alongside the legacy publish.

Same import boundary note as test_hotfix006_transitions.py.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit import rtc

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeWriter:
    def __init__(self, log: list, topic: str, sender_identity, attributes):
        self._log = log
        self.topic = topic
        self.sender_identity = sender_identity
        self.attributes = dict(attributes or {})
        self.chunks: list[str] = []
        self.closed = False

    async def write(self, text: str) -> None:
        self.chunks.append(text)

    async def aclose(self) -> None:
        self.closed = True
        self._log.append(self)


class _FakeLocalParticipant:
    def __init__(self):
        self.identity = "lily-agent"
        self.streams: list[_FakeWriter] = []
        self.legacy: list = []
        self.track_publications = {}

    async def stream_text(self, *, topic="", sender_identity=None,
                          attributes=None, **_kwargs):
        return _FakeWriter(self.streams, topic, sender_identity, attributes)

    async def publish_transcription(self, transcription) -> None:
        self.legacy.append(transcription)


class _FakeRemote:
    def __init__(self, identity: str, kind):
        self.identity = identity
        self.kind = kind


class _FakeRoom:
    def __init__(self, local, remotes):
        self.local_participant = local
        self.remote_participants = {r.identity: r for r in remotes}


class _FakeCtx:
    def __init__(self, room):
        self.room = room


def _make_game() -> tuple[LilyGame, _FakeLocalParticipant]:
    game = LilyGame.__new__(LilyGame)
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("transcript-fwd")
    local = _FakeLocalParticipant()
    room = _FakeRoom(local, [
        _FakeRemote("lily-user-abc", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD),
    ])
    game.ctx = _FakeCtx(room)
    return game, local


def _drain() -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()


def test_user_final_forwards_as_a_final_stream_from_the_user():
    game, local = _make_game()

    async def _go():
        game.publish_user_transcript_nowait(
            "It's Jupiter!", speaker_label="S1", utterance_id="utt_1",
        )
        await asyncio.sleep(0)

    asyncio.new_event_loop().run_until_complete(_go())

    assert len(local.streams) == 1
    stream = local.streams[0]
    assert stream.topic == "lk.transcription"
    # Attributed to the TABLE's participant, never to Lily.
    assert stream.sender_identity == "lily-user-abc"
    assert stream.attributes["lk.transcription_final"] == "true"
    assert stream.attributes["lk.segment_id"] == "utt_1"
    assert stream.chunks == ["It's Jupiter!"]
    assert stream.closed


def test_user_forward_carries_the_bound_player_name():
    game, local = _make_game()
    game.sk.bind_speaker("S1", "Maya")

    async def _go():
        game.publish_user_transcript_nowait(
            "Saturn?", speaker_label="S1", utterance_id="utt_2",
        )
        await asyncio.sleep(0)

    asyncio.new_event_loop().run_until_complete(_go())

    assert local.streams[0].attributes.get("prmpt.speaker_label") == "Maya"


def test_agent_final_mirrors_onto_the_stream_wire():
    game, local = _make_game()

    class _AudioPub:
        kind = rtc.TrackKind.KIND_AUDIO
        sid = "TR_audio1"

    local.track_publications = {"TR_audio1": _AudioPub()}

    async def _go():
        game.publish_agent_transcription_nowait(
            "Correct — Jupiter!", speech_id="speech_9", interrupted=False,
        )
        await asyncio.sleep(0)

    asyncio.new_event_loop().run_until_complete(_go())

    # Legacy publish still goes out for older clients…
    assert len(local.legacy) == 1
    # …and the SAME final text now rides the stream wire the glass reads.
    assert len(local.streams) == 1
    stream = local.streams[0]
    assert stream.topic == "lk.transcription"
    assert stream.attributes["lk.transcription_final"] == "true"
    assert stream.attributes["lk.segment_id"] == "speech_9"
    assert stream.chunks == ["Correct — Jupiter!"]
