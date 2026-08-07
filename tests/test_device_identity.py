"""Device identity quarantine and voice-verification regressions."""

import asyncio

import lily_agent
from lily_agent import LilyAgent, LilyGame


class _Speaker:
    def __init__(self, identifier):
        self.label = "S1"
        self.speaker_identifiers = [identifier]


class _STT:
    def __init__(self, identifiers):
        self.identifiers = identifiers

    async def get_speaker_ids(self):
        return self.identifiers


def _game():
    game = LilyGame.__new__(LilyGame)
    game.sk = type("Scorekeeper", (), {"roster_size": lambda self: 1})()
    game.group_id = "room-new"
    game.group_id_source = "room_name"
    game.supabase = object()
    game.stt = None
    game.forget_state = "idle"
    game.device_candidate_group_id = None
    game.device_candidate_source = None
    game.device_identity_verified = False
    game.device_identity_rejected = False
    game._device_candidate_memory = None
    game._device_candidate_memory_block = ""
    game._device_candidate_prefs = {}
    game._device_candidate_voiceprints = []
    game._device_verify_task = None
    game.memory_block = ""
    game.memory_total_games = 0
    game.memory_player_names = []
    game.memory_settled = asyncio.Event()
    game.prefs = {}
    game._memory_disclosure_offered = False
    game._prefs_offer_made = False
    return game


def _stage_fields(game):
    game.device_candidate_group_id = "device-old"
    game.device_candidate_source = "participant_metadata"
    game._device_candidate_memory = {
        "total_games": 4,
        "player_names": ["Rami"],
    }
    game._device_candidate_memory_block = (
        "[RETURNING TABLE]\nplayers: Rami\ntotal games: 4"
    )
    game._device_candidate_prefs = {"pacing": "relaxed"}
    game._device_candidate_voiceprints = [{
        "group_id": "device-old",
        "player_name": "Rami",
        "speaker_label": "Rami",
        "speaker_identifiers": ["voice-rami"],
    }]


def test_initial_device_metadata_stays_on_room_provisional_id():
    assert lily_agent._quarantine_initial_device_identity(
        "device-old", "participant_metadata", "room-new"
    ) == ("room-new", "room_name", "device-old")
    assert lily_agent._quarantine_initial_device_identity(
        "operator-group", "env_override", "room-new"
    ) == ("operator-group", "env_override", None)


def test_device_candidate_greeting_is_soft_and_contains_no_memory_details():
    game = _game()
    _stage_fields(game)
    instructions = game.greeting_instructions()
    assert "DEVICE looks familiar" in instructions
    assert "no current voice has been verified" in instructions
    assert "Reference last game's winner" not in instructions
    assert "your memory KNOWS this table" not in instructions


def test_memory_tool_discloses_no_counts_before_voice_verification():
    game = _game()
    _stage_fields(game)
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    result = asyncio.run(
        LilyAgent.lily_explain_memory.__wrapped__(agent, None)
    )
    assert "no current voice has been verified" in result
    assert "Do NOT disclose counts" in result
    assert "four games" not in result
    assert "Rami" not in result


def test_matching_voice_promotes_quarantined_memory():
    game = _game()
    _stage_fields(game)
    game.stt = _STT([_Speaker("voice-rami")])

    async def _upgrade(new_group_id, source):
        game.group_id = new_group_id
        game.group_id_source = source

    game.upgrade_group_id = _upgrade
    result = asyncio.run(game.verify_device_candidate("game_start"))

    assert result is True
    assert game.group_id == "device-old"
    assert game.group_id_source == "voiceprint_match"
    assert game.device_identity_verified is True
    assert game.device_candidate_group_id is None
    assert "[RETURNING TABLE]" in game.memory_block
    assert game.memory_player_names == ["Rami"]
    assert game.prefs == {"pacing": "relaxed"}


def test_mismatched_voice_rejects_candidate_without_disclosure():
    game = _game()
    _stage_fields(game)
    game.stt = _STT([_Speaker("voice-stranger")])
    result = asyncio.run(game.verify_device_candidate("game_start"))

    assert result is False
    assert game.group_id == "room-new"
    assert game.device_identity_rejected is True
    assert game.device_candidate_group_id is None
    assert game.memory_block == ""
    assert game.memory_player_names == []
    assert game.prefs == {}


def test_unmatched_voice_before_game_start_does_not_reject_mixed_table():
    game = _game()
    _stage_fields(game)
    game.stt = _STT([_Speaker("voice-new-guest")])
    result = asyncio.run(game.verify_device_candidate("final_transcript"))

    assert result is None
    assert game.device_candidate_group_id == "device-old"
    assert game.device_identity_rejected is False
    assert game.memory_block == ""


def test_missing_current_voice_ids_keeps_candidate_quarantined():
    game = _game()
    _stage_fields(game)
    game.stt = _STT([])
    result = asyncio.run(game.verify_device_candidate("test"))

    assert result is None
    assert game.device_candidate_group_id == "device-old"
    assert game.device_identity_rejected is False
    assert game.memory_block == ""


def test_stage_device_candidate_never_activates_loaded_data(monkeypatch):
    game = _game()

    async def _memory(*_args):
        return {"total_games": 2, "player_names": ["Rami"]}

    async def _prefs(*_args):
        return {"pacing": "relaxed"}

    async def _voices(*_args):
        return [{
            "label": "Rami",
            "speaker_identifiers": ["voice-rami"],
        }]

    monkeypatch.setattr(lily_agent.lily_memory, "lily_load_group_memory", _memory)
    monkeypatch.setattr(lily_agent.lily_persistence, "lily_load_group_prefs", _prefs)
    monkeypatch.setattr(lily_agent.lily_persistence, "lily_load_voiceprints", _voices)
    monkeypatch.setattr(
        lily_agent.lily_memory,
        "lily_build_memory_block",
        lambda memory, prefs=None: "[RETURNING TABLE]\nsecret",
    )

    staged = asyncio.run(
        game.stage_device_candidate("device-old", "dispatch_metadata")
    )

    assert staged is True
    assert game.device_candidate_group_id == "device-old"
    assert game.memory_block == ""
    assert game.memory_player_names == []
    assert game.prefs == {}


def test_verified_voiceprint_name_survives_without_game_memory_row(monkeypatch):
    game = _game()

    async def _memory(*_args):
        return {}

    async def _prefs(*_args):
        return {}

    async def _voices(*_args):
        return [{
            "label": "Rami",
            "speaker_identifiers": ["voice-rami"],
        }]

    async def _upgrade(new_group_id, source):
        game.group_id = new_group_id
        game.group_id_source = source

    monkeypatch.setattr(lily_agent.lily_memory, "lily_load_group_memory", _memory)
    monkeypatch.setattr(lily_agent.lily_persistence, "lily_load_group_prefs", _prefs)
    monkeypatch.setattr(lily_agent.lily_persistence, "lily_load_voiceprints", _voices)
    game.upgrade_group_id = _upgrade

    assert asyncio.run(
        game.stage_device_candidate("device-old", "dispatch_metadata")
    ) is True
    asyncio.run(game._promote_device_candidate("voice_identity_match"))

    assert game.memory_player_names == ["Rami"]
    assert "Returning players: Rami" in game.memory_block
    assert "no prior game result" in game.memory_block
