"""WO-LILY-FLOOR-001 FL-1 — the per-utterance addressee classifier.

Evidence base: session `lily-81BCB0-583a0f16`. Two recorded derailments:
Lily barged into a player-to-player tangent ("Carry on, Lily. We're not
talking to you") and into the players' feedback conversation ("Carry on.
Lily. We're having a conversation"). Root conditions: agent_classification
null on every utterance (every heard utterance defaulted to
host-directed), no scope boundary on the speak-by-default invariant, and
supply stalls pushing her to fill gaps that belonged to the table.

The fixture replay below reconstructs both derailment beats and the
session's scored answers and pins the WO's verification bullets:

  1. both derailment beats classify as SIDE-CLUSTER before Lily would
     have spoken;
  2. every scored answer classifies HOST-DIRECTED (no name needed —
     open window + expectation-primed match is definitional);
  3. the addressee log populates per utterance with agent_classification
     never null.

WS-11/WS-13 acoustic surfaces (per-word volume, arousal/energy) are not
live yet — the LilyAcousticRegister interface is driven here with
fixture-recorded features, exactly as the WO directs.

The pure-module tests run stdlib-only; the fixture-replay section imports
lily_agent (and therefore livekit) — same boundary note as
test_say_gate_dispatch.py.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_addressee_classifier import (
    CLASS_HOST_DIRECTED,
    CLASS_SIDE_CHATTER,
    CLASS_SIDE_CLUSTER,
    CLUSTER_BREAK,
    CLUSTER_EXTEND,
    CLUSTER_LOCK,
    NAME_MENTION,
    NAME_NONE,
    NAME_REFERENTIAL,
    NAME_VOCATIVE,
    LilyAcousticRegister,
    LilyAddresseeClassifier,
    LilyUtteranceSignals,
    lily_content_cohesion,
    lily_name_evidence,
    lily_register_score,
)


# ---------------------------------------------------------------------------
# Name evidence — vocative vs referential at the language layer
# ---------------------------------------------------------------------------

def test_name_vocative_carry_on_comma():
    # Verbatim from the 81BCB0 derailment: address, not talk about her.
    assert lily_name_evidence(
        "Carry on, Lily. We're not talking to you."
    ) == NAME_VOCATIVE


def test_name_vocative_standalone_sentence():
    assert lily_name_evidence(
        "Carry on. Lily. We're having a conversation."
    ) == NAME_VOCATIVE


def test_name_vocative_leading():
    assert lily_name_evidence("Lily, what's the score?") == NAME_VOCATIVE


def test_name_referential_is_a_joke():
    assert lily_name_evidence("Lily is a joke") == NAME_REFERENTIAL


def test_name_referential_about():
    assert lily_name_evidence(
        "I was just telling him about Lily"
    ) == NAME_REFERENTIAL


def test_name_referential_third_person_action():
    assert lily_name_evidence(
        "Lily butted in again didn't she"
    ) == NAME_REFERENTIAL


def test_name_mention_inconclusive_position():
    assert lily_name_evidence(
        "maybe ask lily for another one"
    ) == NAME_MENTION


def test_name_none():
    assert lily_name_evidence("I think it's Saturn") == NAME_NONE


def test_name_none_with_diarization_tag():
    assert lily_name_evidence("[S2] no idea honestly") == NAME_NONE


# ---------------------------------------------------------------------------
# Acoustic register — the WS-11/WS-13 feature interface
# ---------------------------------------------------------------------------

def test_register_device_directed_beats_side_chatter():
    device = lily_register_score(LilyAcousticRegister(
        energy=0.8, speech_rate=0.3, articulation=0.8, mic_orientation=0.9,
    ))
    side = lily_register_score(LilyAcousticRegister(
        energy=0.25, speech_rate=0.85, articulation=0.3, mic_orientation=0.2,
    ))
    assert device > 0.5 > side


def test_register_per_word_volume_feeds_score():
    loud = lily_register_score(
        LilyAcousticRegister(per_word_volume=(0.8, 0.9, 0.85))
    )
    quiet = lily_register_score(
        LilyAcousticRegister(per_word_volume=(0.2, 0.15, 0.25))
    )
    assert loud > 0.5 > quiet


def test_register_none_when_pipeline_absent():
    assert lily_register_score(None) is None
    assert lily_register_score(LilyAcousticRegister()) is None


def test_register_snapshot_adapter_reads_live_shape():
    register = LilyAcousticRegister.from_snapshot({
        "dimension": {"arousal": 0.7},
        "prosody": {"loudness": 0.6},
        "features": {},
    })
    assert register is not None
    assert register.arousal == 0.7
    assert register.energy == 0.6


def test_register_snapshot_adapter_none_on_empty():
    assert LilyAcousticRegister.from_snapshot(None) is None
    assert LilyAcousticRegister.from_snapshot({"dimension": {}}) is None


# ---------------------------------------------------------------------------
# Content cohesion
# ---------------------------------------------------------------------------

def test_cohesion_shared_content_token():
    assert lily_content_cohesion(
        "did you go on Saturday?", "I thought you bailed on Saturday"
    )


def test_cohesion_reply_opener():
    assert lily_content_cohesion("did you end up going?", "yeah we went")


def test_cohesion_none():
    assert not lily_content_cohesion(
        "did you end up going?", "which planet has the most moons"
    )


# ---------------------------------------------------------------------------
# Deterministic priors
# ---------------------------------------------------------------------------

def _sig(text, speaker="S1", ts=0.0, **kw):
    return LilyUtteranceSignals(
        text=text, speaker_label=speaker, ts=ts, **kw
    )


def test_window_plus_match_is_host_by_definition_no_name():
    j = LilyAddresseeClassifier().classify(_sig(
        "the femur", window_open=True, expectation_match=True,
        phase="question",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.score >= 0.95
    assert j.reason == "window+match"


def test_idle_default_flips_to_side_chatter():
    j = LilyAddresseeClassifier().classify(_sig(
        "did you end up going on Saturday?", phase="idle",
    ))
    assert j.classification == CLASS_SIDE_CHATTER


def test_idle_named_stays_host():
    j = LilyAddresseeClassifier().classify(_sig(
        "Lily, give us a hard one next", phase="idle",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.reason == "vocative"


def test_idle_command_shaped_stays_host():
    j = LilyAddresseeClassifier().classify(_sig(
        "skip this one", phase="idle", command_shaped=True,
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.reason == "command"


def test_adjacency_biases_host():
    clf = LilyAddresseeClassifier()
    clf.note_agent_prompt(100.0)
    adjacent = clf.classify(_sig("go on then", ts=102.0, phase="idle"))
    distant = LilyAddresseeClassifier().classify(
        _sig("go on then", ts=102.0, phase="idle")
    )
    assert adjacent.score > distant.score


def test_referential_name_is_mild_side_evidence():
    base = LilyAddresseeClassifier().classify(
        _sig("she is a joke", phase="idle")
    )
    referential = LilyAddresseeClassifier().classify(
        _sig("Lily is a joke", phase="idle")
    )
    assert referential.classification == CLASS_SIDE_CHATTER
    assert referential.score < base.score


def test_acoustic_register_moves_the_score():
    quiet_fast = LilyAddresseeClassifier().classify(_sig(
        "so anyway about the weekend", phase="idle",
        register=LilyAcousticRegister(energy=0.2, speech_rate=0.9),
    ))
    loud_slow = LilyAddresseeClassifier().classify(_sig(
        "so anyway about the weekend", phase="idle",
        register=LilyAcousticRegister(energy=0.9, speech_rate=0.2),
    ))
    assert loud_slow.score > quiet_fast.score
    assert quiet_fast.components["acoustic"] is not None


# ---------------------------------------------------------------------------
# Side-cluster machine
# ---------------------------------------------------------------------------

TANGENT = (
    (0.0, "S1", "Wait, did you end up going to that thing on Saturday?"),
    (2.0, "S2", "Yeah, we ended up going after all, it was actually great."),
    (4.0, "S1", "No way, I thought you bailed on Saturday."),
)


def _run_tangent(clf, register=None):
    out = []
    for ts, speaker, text in TANGENT:
        out.append(clf.classify(_sig(
            text, speaker=speaker, ts=ts, phase="idle", register=register,
        )))
    return out


def test_cluster_locks_on_rapid_cohering_alternation():
    judgments = _run_tangent(LilyAddresseeClassifier())
    assert judgments[0].classification == CLASS_SIDE_CHATTER
    assert judgments[1].classification == CLASS_SIDE_CHATTER
    assert judgments[2].classification == CLASS_SIDE_CLUSTER
    assert judgments[2].cluster_event == CLUSTER_LOCK
    assert judgments[2].cluster_id is not None


def test_cluster_classifies_as_cluster_not_one_by_one():
    clf = LilyAddresseeClassifier()
    _run_tangent(clf)
    j = clf.classify(_sig(
        "We almost did, but Marcus offered to drive us.",
        speaker="S2", ts=6.0, phase="idle",
    ))
    assert j.classification == CLASS_SIDE_CLUSTER
    assert j.cluster_event == CLUSTER_EXTEND


def test_vocative_breaks_the_cluster():
    clf = LilyAddresseeClassifier()
    judgments = _run_tangent(clf)
    j = clf.classify(_sig(
        "Carry on, Lily. We're not talking to you.",
        speaker="S1", ts=8.0, phase="idle",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.cluster_event == CLUSTER_BREAK
    assert j.cluster_id == judgments[2].cluster_id


def test_window_match_breaks_the_cluster():
    clf = LilyAddresseeClassifier()
    _run_tangent(clf)
    j = clf.classify(_sig(
        "oh wait — Saturn", speaker="S2", ts=6.0,
        window_open=True, expectation_match=True, phase="question",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.cluster_event == CLUSTER_BREAK


def test_stale_cluster_dissolves_on_gap():
    clf = LilyAddresseeClassifier()
    _run_tangent(clf)
    j = clf.classify(_sig(
        "anyway that was a weird week", speaker="S2", ts=30.0, phase="idle",
    ))
    assert j.classification == CLASS_SIDE_CHATTER
    assert j.cluster_event is None


def test_agent_prompt_supersedes_the_cluster():
    clf = LilyAddresseeClassifier()
    _run_tangent(clf)
    clf.note_agent_prompt(5.0)
    j = clf.classify(_sig("go on then", speaker="S1", ts=6.0, phase="idle"))
    assert j.classification == CLASS_HOST_DIRECTED  # adjacency bias
    assert j.cluster_id is None


def test_slow_alternation_never_locks():
    clf = LilyAddresseeClassifier()
    out = []
    for ts, speaker, text in ((0.0, "S1", TANGENT[0][2]),
                              (10.0, "S2", TANGENT[1][2]),
                              (20.0, "S1", TANGENT[2][2])):
        out.append(clf.classify(
            _sig(text, speaker=speaker, ts=ts, phase="idle")
        ))
    assert all(j.classification == CLASS_SIDE_CHATTER for j in out)


def test_single_speaker_run_never_locks():
    clf = LilyAddresseeClassifier()
    for i, (ts, _, text) in enumerate(TANGENT):
        j = clf.classify(_sig(text, speaker="S1", ts=ts, phase="idle"))
    assert j.classification == CLASS_SIDE_CHATTER


# ---------------------------------------------------------------------------
# Structured output — the FL-2 / FL-4 surface
# ---------------------------------------------------------------------------

def test_row_fields_and_log_json_never_null_classification():
    clf = LilyAddresseeClassifier()
    for ts, speaker, text in TANGENT:
        j = clf.classify(_sig(text, speaker=speaker, ts=ts, phase="idle"))
        fields = j.row_fields()
        assert fields["agent_classification"] in (
            CLASS_HOST_DIRECTED, CLASS_SIDE_CHATTER, CLASS_SIDE_CLUSTER
        )
        assert 0.0 <= fields["addressee_score"] <= 1.0
        assert "prior" in fields["addressee_score_components"]
        assert "name_evidence" in fields["addressee_score_components"]
        assert "classification" in j.log_json()


# ---------------------------------------------------------------------------
# Fixture replay — session lily-81BCB0-583a0f16 through the PRODUCTION
# path (lily_agent wiring: scorekeeper result -> classify_addressee ->
# lily_addressee_log row). Imports lily_agent / livekit.
# ---------------------------------------------------------------------------

import lily_audeering_consumers
import lily_persistence
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str = "") -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeTranscripts:
    async def discard_pending(self, *, disable=False) -> None:
        pass


def _make_game() -> LilyGame:
    """Minimal LilyGame via __new__ (test_desync_fixture pattern) with the
    transcript-event + addressee-log surfaces live."""
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("lily-81BCB0-583a0f16")
    game.memory_block = ""
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = object()  # non-None: the addressee log path is live
    game.ui_phase = "question"
    game._window_timer = None
    game._bed_handle = None
    game.background_audio = None
    game._steal_window = False
    game._adjudicating = False
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game.eliminated = []
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.prefs = {}
    game._prefs_offer_made = False
    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game.forget_spoken_confirmed = False
    game._forget_target_group = None
    game._state_note = None
    game._user_turn_index = 0
    game.promoted_categories = []
    game.rounds_total = 3
    game.asked_history = []
    game.group_id = "grp_81bcb0"
    game.prewager_standings = None
    game.highlights = []
    game.reasoning = None
    game._prefetch_task = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._judged_keys = set()
    game._spec_judge = {}
    game._addressee_rows = {}
    game.transcripts = _FakeTranscripts()

    async def _publish_metadata(question_text, **kwargs):
        pass

    async def _publish_attributes():
        pass

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    return game


MOONS_QUESTION = {
    "prompt": "Which planet in our solar system has the most moons?",
    "canonical_answer": "Saturn",
    "acceptable_answers": ["saturn"],
    "category": "academic",
}


def _feed(game: LilyGame, text: str, speaker: str, now: float) -> None:
    result = game.sk.on_transcript_segment(
        text=text, speaker_label=speaker, is_final=True,
        now=now, segment_start_time=now,
    )
    game.on_transcript_event(
        result, text, speaker_label=speaker, segment_ts=now
    )


def _replay_81bcb0(monkeypatch):
    """Replay the reconstructed session through the production wiring;
    returns (game, rows, judgments_by_ts)."""
    rows: list[dict] = []

    async def _capture(supabase, row):
        rows.append(row)
        return None

    monkeypatch.setattr(lily_persistence, "lily_log_addressee", _capture)

    game = _make_game()
    judgments = []
    original = LilyGame.classify_addressee

    def _tap(self, *args, **kwargs):
        j = original(self, *args, **kwargs)
        judgments.append(j)
        return j

    monkeypatch.setattr(LilyGame, "classify_addressee", _tap)

    async def scenario():
        t = 1000.0

        # -- Derailment beat 1: the player-to-player tangent during a
        # supply stall (no window, nothing armed — the gap belongs to
        # the table). Lily historically barged in after the fourth line.
        _feed(game, "Wait, did you end up going to that thing on Saturday?",
              "S1", t + 0.0)
        _feed(game, "Yeah, we ended up going after all, it was actually great.",
              "S2", t + 2.0)
        _feed(game, "No way, I thought you bailed on Saturday.",
              "S1", t + 4.0)
        _feed(game, "We almost did, but Marcus offered to drive us.",
              "S2", t + 6.0)
        # The recorded correction — address, not chatter:
        _feed(game, "Carry on, Lily. We're not talking to you.",
              "S1", t + 8.0)

        # -- Scored answers: open window on the registered question;
        # answers are host-directed with no name, by definition.
        game.sk.bind_speaker("S3", "Dana")
        game.sk.bind_speaker("S1", "Rami")
        game.armed_question = dict(MOONS_QUESTION)
        game.sk.start_question(game.armed_question)
        game.sk.open_answer_window(duration=30.0, now=t + 20.0)
        _feed(game, "Is it Saturn?", "S3", t + 22.0)
        _feed(game, "yeah I'll say Saturn as well", "S1", t + 24.0)
        game.sk.close_answer_window()

        # -- Derailment beat 2: the feedback conversation (idle again;
        # Lily historically barged into it).
        _feed(game, "I feel like the picture rounds were the best part tonight.",
              "S2", t + 40.0)
        _feed(game, "Yeah the picture ones were great, do more of those.",
              "S1", t + 42.0)
        _feed(game, "More music rounds too honestly.",
              "S3", t + 44.5)
        _feed(game, "Carry on. Lily. We're having a conversation.",
              "S2", t + 47.0)

        await asyncio.sleep(0.05)  # drain fire-and-forget log tasks

    asyncio.run(scenario())
    return game, rows, judgments


def test_81bcb0_derailment_beats_classify_side_cluster(monkeypatch):
    """Verification bullet 1: both recorded derailment beats classify as
    side-cluster BEFORE Lily would have spoken."""
    game, rows, judgments = _replay_81bcb0(monkeypatch)
    by_text = {j.ts: j for j in judgments}
    # Beat 1: the tangent locks by its third line and holds as a cluster
    # through the beat Lily barged into.
    assert by_text[1004.0].classification == CLASS_SIDE_CLUSTER
    assert by_text[1004.0].cluster_event == CLUSTER_LOCK
    assert by_text[1006.0].classification == CLASS_SIDE_CLUSTER
    # The spoken correction is address — and it breaks the cluster.
    assert by_text[1008.0].classification == CLASS_HOST_DIRECTED
    assert by_text[1008.0].cluster_event == CLUSTER_BREAK
    # Beat 2: the feedback conversation locks before the second barge.
    assert by_text[1044.5].classification == CLASS_SIDE_CLUSTER
    assert by_text[1044.5].cluster_event == CLUSTER_LOCK
    assert by_text[1047.0].classification == CLASS_HOST_DIRECTED
    # Distinct clusters, ordered lock ids.
    assert by_text[1044.5].cluster_id != by_text[1004.0].cluster_id


def test_81bcb0_scored_answers_classify_host_directed(monkeypatch):
    """Verification bullet 2: every scored answer from the session
    classifies host-directed — no name required."""
    game, rows, judgments = _replay_81bcb0(monkeypatch)
    answers = [j for j in judgments if j.ts in (1022.0, 1024.0)]
    assert len(answers) == 2
    for j in answers:
        assert j.classification == CLASS_HOST_DIRECTED
        assert j.reason == "window+match"
        assert j.name_evidence == NAME_NONE


def test_81bcb0_addressee_log_populates_per_utterance_no_nulls(monkeypatch):
    """Verification bullet 3: one addressee-log row per utterance,
    agent_classification never null."""
    game, rows, judgments = _replay_81bcb0(monkeypatch)
    assert len(rows) == 11 == len(judgments)
    for row in rows:
        assert row["agent_classification"] in (
            CLASS_HOST_DIRECTED, CLASS_SIDE_CHATTER, CLASS_SIDE_CLUSTER
        )
        assert row["addressee_score"] is not None
        assert row["addressee_score_components"]["prior"] is not None
        assert row["session_id"] == "lily-81BCB0-583a0f16"


def test_81bcb0_state_block_carries_the_floor_read(monkeypatch):
    """The judgment conditions the reply BEFORE generation: after a
    side-cluster judgment the state block carries the floor read; after a
    host-directed judgment it injects nothing (speak-by-default holds
    inside its scope). She never narrates the classification itself."""
    game, rows, judgments = _replay_81bcb0(monkeypatch)
    # Last judgment of the replay is host-directed ("Carry on. Lily.") —
    # no floor-read line.
    assert "floor read" not in game.build_state_block()
    # Rewind to a side-cluster judgment: the floor read appears.
    game.last_addressee_judgment = next(
        j for j in judgments if j.classification == CLASS_SIDE_CLUSTER
    )
    block = game.build_state_block()
    assert "floor read" in block
    assert "the floor is theirs" in block
    # 10% principle: context guidance, never her classification state.
    assert "side_cluster" not in block
    assert "classif" not in block.lower()


def test_agent_speech_finished_anchors_adjacency(monkeypatch):
    """on_agent_speech_finished notes the prompt on the classifier (the
    adjacency anchor) and a suppressed turn moves no floor state."""
    game = _make_game()
    game.addressee_classifier = LilyAddresseeClassifier()

    async def scenario():
        game.on_agent_speech_finished("Here comes your next question.")
        assert game.addressee_classifier._last_agent_prompt_ts is not None
        game.addressee_classifier._last_agent_prompt_ts = None
        game.on_agent_speech_finished("", suppressed=True)
        assert game.addressee_classifier._last_agent_prompt_ts is None

    asyncio.run(scenario())
