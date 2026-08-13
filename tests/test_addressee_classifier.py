"""WO-LILY-FLOOR-001 FL-1 — the per-utterance addressee classifier.

Evidence base: session `lily-81BCB0-583a0f16` (2026-08-05, extracted
verbatim from Supabase `lily_transcripts` + `lily_addressee_log` into
tests/fixtures/ — the REAL session, not a reconstruction). Two recorded
derailments: Lily barged into a player-to-player tangent ("Okay. Carry
on. Lily. We're not talking to you.") and into the players' feedback
conversation ("Carry on. Lily. We're having a conversation."). Root
conditions: agent_classification null on every utterance (every heard
utterance defaulted to host-directed), no scope boundary on
speak-by-default, supply stalls pushing her to fill gaps that belonged
to the table.

What the real session taught the classifier (vs. the WO's idealized
description):
  - Both derailment beats are SOLO-attributed runs (S2 carrying the
    tangent / the feedback monologue) — diarization only captures the
    audible side of a conversation, so a solo run addressing the table
    ("Have you guys seen Loki?") or fully cohering with itself can lock
    a side-cluster.
  - The recorded corrections are FLOOR-HOLD declarations: host-directed
    speech whose content asserts the side conversation ("we're not
    talking to you" / "we're having a conversation") — they lock or
    sustain the cluster instead of breaking it, unlike a plain vocative.
  - Real intra-run gaps run to 12.5s (one 11-second utterance), so the
    cluster gap bounds are 15s intra-run / 25s break.

The fixture replay drives every player utterance through the PRODUCTION
path (real scorekeeper -> on_transcript_event -> classify_addressee ->
lily_addressee_log row) with answer windows reconstructed from the
addressee log's own ground truth (utterance_ts - seconds_into_window),
and pins the WO's verification bullets:

  1. both derailment beats classify as SIDE-CLUSTER before Lily would
     have spoken (her barge turns are the defect, so the replay does not
     anchor them — legitimate prompt state rides the window priors);
  2. every scored answer classifies HOST-DIRECTED — no name needed;
  3. the addressee log populates per utterance, agent_classification
     never null.

WS-11/WS-13 acoustic surfaces (per-word volume, arousal/energy) are not
live yet — the LilyAcousticRegister interface is driven in the unit
tests with fixture-recorded features, exactly as the WO directs.

The pure-module tests run stdlib-only; the fixture-replay section imports
lily_agent (and therefore livekit) — same boundary note as
test_say_gate_dispatch.py.
"""

import asyncio
import datetime
import json
import re
import sys
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
    lily_floor_hold,
    lily_name_evidence,
    lily_register_score,
    lily_table_address,
)


# ---------------------------------------------------------------------------
# Name evidence — vocative vs referential at the language layer
# ---------------------------------------------------------------------------

def test_name_vocative_carry_on_comma():
    # Verbatim from the 81BCB0 derailment: address, not talk about her.
    assert lily_name_evidence(
        "Okay. Carry on. Lily. We're not talking to you."
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
# Floor-hold and table-address — the language-layer cluster signals
# ---------------------------------------------------------------------------

def test_floor_hold_both_recorded_corrections():
    assert lily_floor_hold("Okay. Carry on. Lily. We're not talking to you.")
    assert lily_floor_hold("Carry on. Lily. We're having a conversation.")


def test_floor_hold_not_plain_carry_on():
    # Rami also uses "carry on" as plain "proceed" ("On. Carry on.") —
    # the hold is carried by the we-clause, never by "carry on" alone.
    assert not lily_floor_hold("On. Carry on.")
    assert not lily_floor_hold("Could you please continue?")


def test_table_address():
    assert lily_table_address("Have you guys seen Loki?")
    assert not lily_table_address("I think it's Saturn")


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
    # arousal flows today (dimension.arousal); energy lands at the
    # contract key prosody.energy once 003 produces it.
    register = LilyAcousticRegister.from_snapshot({
        "dimension": {"arousal": 0.7},
        "prosody": {"energy": 0.6},
        "features": {},
    })
    assert register is not None
    assert register.arousal == 0.7
    assert register.energy == 0.6


def test_register_snapshot_adapter_arousal_only_today():
    # Today's real snapshot: only dimension.arousal is a scalar;
    # prosody.loudness is a nested dict and must NOT be read as energy
    # (landing contract — overwriting loudness would break its readers).
    register = LilyAcousticRegister.from_snapshot({
        "dimension": {"arousal": 0.42},
        "prosody": {"loudness": {"mean": 0.6, "max": 0.9}},
        "features": {},
    })
    assert register is not None
    assert register.arousal == 0.42
    assert register.energy is None


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


def test_mid_delivery_match_is_host_directed_before_window_opens():
    j = LilyAddresseeClassifier().classify(_sig(
        "hydrogen", window_open=False, expectation_match=True,
        phase="question",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.score >= 0.95
    assert j.reason == "delivery+match"


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
    assert adjacent.classification == CLASS_HOST_DIRECTED


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
# Side-cluster machine — alternation route (synthetic two-speaker tangent)
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


def test_plain_vocative_breaks_the_cluster():
    clf = LilyAddresseeClassifier()
    judgments = _run_tangent(clf)
    j = clf.classify(_sig(
        "Lily, what's the score?", speaker="S1", ts=8.0, phase="idle",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.reason == "vocative"
    assert j.cluster_event == CLUSTER_BREAK
    assert j.cluster_id == judgments[2].cluster_id


def test_floor_hold_sustains_the_cluster():
    # The 81BCB0 beat-1 correction: vocative in form, but its content
    # asserts the side conversation — the cluster survives it.
    clf = LilyAddresseeClassifier()
    judgments = _run_tangent(clf)
    j = clf.classify(_sig(
        "Okay. Carry on. Lily. We're not talking to you.",
        speaker="S1", ts=8.0, phase="idle",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.reason == "floor-hold"
    assert j.cluster_event == CLUSTER_EXTEND
    assert j.cluster_id == judgments[2].cluster_id
    follow = clf.classify(_sig(
        "So anyway, the clock thing.", speaker="S2", ts=10.0, phase="idle",
    ))
    assert follow.classification == CLASS_SIDE_CLUSTER


def test_floor_hold_declares_the_cluster():
    # The 81BCB0 beat-2 shape: only two side utterances heard before the
    # correction lands — the declaration itself locks the cluster.
    clf = LilyAddresseeClassifier()
    clf.classify(_sig(
        "You know, when you go to sometimes trivia like, like a trivia.",
        speaker="S2", ts=0.0, phase="idle",
    ))
    clf.classify(_sig(
        "Sometimes they tell you don't shove the answer until we say.",
        speaker="S2", ts=12.5, phase="idle",
    ))
    j = clf.classify(_sig(
        "Carry on. Lily. We're having a conversation.",
        speaker="Rami", ts=13.0, phase="idle",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.reason == "floor-hold"
    assert j.cluster_event == CLUSTER_LOCK
    follow = clf.classify(_sig(
        "Maybe you have to say, you know, listen to the question.",
        speaker="S2", ts=13.2, phase="idle",
    ))
    assert follow.classification == CLASS_SIDE_CLUSTER
    assert follow.cluster_id == j.cluster_id


def test_floor_hold_with_no_recent_side_speech_locks_nothing():
    j = LilyAddresseeClassifier().classify(_sig(
        "We're having a conversation.", speaker="S1", ts=0.0, phase="idle",
    ))
    assert j.classification == CLASS_HOST_DIRECTED
    assert j.cluster_event is None


def test_solo_run_with_table_address_locks():
    # 81BCB0 beat 1: S2 alone on mic, addressing the table.
    clf = LilyAddresseeClassifier()
    clf.classify(_sig("Have you guys seen Loki?", speaker="S2", ts=0.0,
                      phase="idle"))
    clf.classify(_sig("Okay. You reminded me of.", speaker="S2", ts=1.7,
                      phase="idle"))
    j = clf.classify(_sig("The. The clock.", speaker="S2", ts=6.2,
                          phase="idle"))
    assert j.classification == CLASS_SIDE_CLUSTER
    assert j.cluster_event == CLUSTER_LOCK


def test_solo_run_without_anchor_never_locks():
    clf = LilyAddresseeClassifier()
    for i, text in enumerate((
        "so I was at the store", "the weather turned", "my knee hurts",
    )):
        j = clf.classify(_sig(text, speaker="S2", ts=float(i * 2),
                              phase="idle"))
    assert j.classification == CLASS_SIDE_CHATTER
    assert j.cluster_event is None


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
        "anyway that was a weird week", speaker="S2", ts=45.0, phase="idle",
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
                              (20.0, "S2", TANGENT[1][2]),
                              (40.0, "S1", TANGENT[2][2])):
        out.append(clf.classify(
            _sig(text, speaker=speaker, ts=ts, phase="idle")
        ))
    assert all(j.classification == CLASS_SIDE_CHATTER for j in out)


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
# Fixture replay — the REAL session lily-81BCB0-583a0f16 through the
# PRODUCTION path (scorekeeper result -> classify_addressee ->
# lily_addressee_log row). Imports lily_agent / livekit.
# ---------------------------------------------------------------------------

import lily_audeering_consumers
import lily_persistence
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SESSION_ID = "lily-81BCB0-583a0f16"

# Event ids of the load-bearing fixture rows (stable keys into the real
# transcript).
BEAT1_TANGENT = (
    "61777d5c-b9e3-4f88-822d-f62e4b56d98a",  # "Have you guys seen Loki?"
    "f8aaa46c-6d0a-43b4-b4f7-4ec03d98a3f6",  # "Okay. You reminded me of."
    "a28743be-8378-47f1-9c20-0cf4d6099388",  # "The. The clock."
)
BEAT1_CORRECTION = "36f920cd-9171-43cb-93b4-142ada3b14b0"
BEAT2_FEEDBACK = (
    "8cbbf685-fa62-45a0-ad50-e52de4142e28",  # "You know, when you go to..."
    "082af639-ff17-4721-bdd6-a76b1d0f7d41",  # "Sometimes they tell you..."
)
BEAT2_CORRECTION = "e9e08e28-e241-4dcb-a506-0f87a79f0217"
BEAT2_CONTINUATION = "6348f178-d213-4c88-8afb-726248d2d41c"


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
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(SESSION_ID)
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

    async def _publish_attributes(*a, **k):
        pass

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    return game


def _epoch(iso: str) -> float:
    if iso.endswith("+00"):
        iso += ":00"
    return datetime.datetime.fromisoformat(iso).timestamp()


_TAG_RE = re.compile(r"^\s*\[[^\]]+\]\s*")


def _clean(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _load_fixture():
    transcripts = json.loads(
        (FIXTURE_DIR / f"{SESSION_ID}.transcripts.json").read_text()
    )
    addressee = json.loads(
        (FIXTURE_DIR / f"{SESSION_ID}.addressee_log.json").read_text()
    )
    return transcripts, addressee


def _window_schedule(addressee: list) -> list:
    """Answer windows reconstructed from the addressee log's own ground
    truth: each scored in-window row pins its window's open time at
    utterance_ts - seconds_into_window; the window closes just after its
    last scored row (adjudication committed there — a nominal 30s
    duration would swallow the beat-1 tangent that in reality ran
    post-reveal)."""
    windows: list = []
    for row in addressee:
        if row["agent_action"] != "scored" or not row["answer_window_open"]:
            continue
        ts = _epoch(row["utterance_ts"])
        open_ts = ts - float(row["seconds_into_window"] or 0.0)
        answer = _clean(row["transcript"]).lower().strip(".?! ")
        matched = bool(row["fuzzy_matched_answer"])
        # Comfortable close pad so an utterance stamped AT the open
        # (seconds_into_window == 0) still lands inside the window.
        for w in windows:
            if abs(w["open"] - open_ts) < 2.0:
                w["close"] = max(w["close"], ts + 3.0)
                if matched:
                    w["answers"].append(answer)
                break
        else:
            windows.append({
                "open": open_ts,
                "close": ts + 3.0,
                "answers": [answer] if matched else [],
            })
    windows.sort(key=lambda w: w["open"])
    return windows


def _replay_81bcb0(monkeypatch):
    """Drive every player utterance of the real session through the
    production wiring. Lily's own turns are NOT anchored: her barge turns
    ARE the recorded defect, and question-delivery state rides the
    reconstructed windows — the replay judges what the classifier knew
    before she would have spoken."""
    rows_written: list = []

    async def _capture(supabase, row):
        rows_written.append(row)
        return None

    monkeypatch.setattr(lily_persistence, "lily_log_addressee", _capture)

    judgments: dict = {}
    current = {"eid": None}
    original = LilyGame.classify_addressee

    def _tap(self, *args, **kwargs):
        j = original(self, *args, **kwargs)
        judgments[current["eid"]] = j
        return j

    monkeypatch.setattr(LilyGame, "classify_addressee", _tap)

    transcripts, addressee = _load_fixture()
    windows = _window_schedule(addressee)
    # Drive by segment_start (the STT segment clock) — it is the SAME
    # clock the addressee log's utterance_ts is stamped on (verified
    # equal to the row's utterance_ts epoch), so the reconstructed
    # windows and the replayed utterances share one timeline; created_at
    # is the ~2s-later event-arrival clock and would desync them.
    player_rows = sorted(
        (
            r for r in transcripts
            if r["speaker_label"] != "LILY"
            and (r["text"] or "").strip()
            and r.get("segment_start") is not None
        ),
        key=lambda r: float(r["segment_start"]),
    )
    game = _make_game()

    async def scenario():
        wi = 0
        open_w = None
        for r in player_rows:
            ts = float(r["segment_start"])
            if open_w is not None and ts > open_w["close"]:
                game.sk.close_answer_window()
                open_w = None
            while wi < len(windows) and ts >= windows[wi]["open"]:
                if open_w is not None:
                    game.sk.close_answer_window()
                open_w = windows[wi]
                acceptable = open_w["answers"] or ["zz-unmatchable"]
                question = {
                    "prompt": f"fixture window {wi}",
                    "canonical_answer": acceptable[0],
                    "acceptable_answers": acceptable,
                }
                game.armed_question = question
                game.sk.start_question(question)
                game.sk.open_answer_window(
                    duration=(open_w["close"] - open_w["open"]) + 10.0,
                    now=open_w["open"],
                )
                wi += 1
            text = _clean(r["text"])
            current["eid"] = r["event_id"]
            result = game.sk.on_transcript_segment(
                text=text, speaker_label=r["speaker_label"], is_final=True,
                now=ts, segment_start_time=ts,
            )
            game.on_transcript_event(
                result, text, speaker_label=r["speaker_label"], segment_ts=ts
            )
        await asyncio.sleep(0.05)  # drain fire-and-forget log tasks

    asyncio.run(scenario())
    return game, rows_written, judgments, player_rows, addressee


def test_81bcb0_derailment_beats_classify_side_cluster(monkeypatch):
    """Verification bullet 1: both recorded derailment beats classify as
    side-cluster before Lily would have spoken."""
    game, rows, judgments, player_rows, addressee = _replay_81bcb0(monkeypatch)
    SIDE = {CLASS_SIDE_CHATTER, CLASS_SIDE_CLUSTER}

    # Beat 1 — the tangent. Every heard line is TABLE TALK, never the
    # old host-by-default: the run classifies non-host throughout (the
    # opening "Have you guys seen Loki?" already rides a live side-cluster
    # from the preceding banter). Historically Lily talked straight over
    # this beat.
    for eid in BEAT1_TANGENT:
        assert judgments[eid].classification in SIDE
    # The recorded correction ("...We're not talking to you.") is
    # host-directed by form but a FLOOR-HOLD by content — it keeps the
    # floor with the table by locking/sustaining a side-cluster, never a
    # bare host command that would license a reply.
    corr1 = judgments[BEAT1_CORRECTION]
    assert corr1.classification == CLASS_HOST_DIRECTED
    assert corr1.reason == "floor-hold"
    assert corr1.cluster_event in (CLUSTER_LOCK, CLUSTER_EXTEND)
    assert corr1.cluster_id is not None

    # Beat 2 — the feedback conversation. The audible side lines classify
    # non-host; the correction ("...We're having a conversation.")
    # DECLARES the cluster, and the players' continuation rides it as a
    # cluster, not one-by-one.
    for eid in BEAT2_FEEDBACK:
        assert judgments[eid].classification in SIDE
    corr2 = judgments[BEAT2_CORRECTION]
    assert corr2.classification == CLASS_HOST_DIRECTED
    assert corr2.reason == "floor-hold"
    assert corr2.cluster_event == CLUSTER_LOCK
    cont2 = judgments[BEAT2_CONTINUATION]
    assert cont2.classification == CLASS_SIDE_CLUSTER
    assert cont2.cluster_id == corr2.cluster_id
    # Distinct beats, distinct clusters.
    assert corr2.cluster_id != corr1.cluster_id


def test_81bcb0_scored_answers_classify_host_directed(monkeypatch):
    """Verification bullet 2: every scored answer from the session
    classifies host-directed."""
    game, rows, judgments, player_rows, addressee = _replay_81bcb0(monkeypatch)
    scored = [
        r for r in addressee
        if r["agent_action"] == "scored" and r["answer_window_open"]
    ]
    assert len(scored) == 12
    checked = 0
    for row in scored:
        # utterance_ts IS the segment_start clock (verified equal), so the
        # scored row maps to exactly the player utterance whose
        # segment_start coincides — not a same-text utterance elsewhere in
        # the session (there are two distinct "Mark." / "Femur." lines).
        row_ts = _epoch(row["utterance_ts"])
        row_text = _clean(row["transcript"])
        matches = [
            judgments[p["event_id"]]
            for p in player_rows
            if _clean(p["text"]) == row_text
            and p.get("segment_start") is not None
            and abs(float(p["segment_start"]) - row_ts) < 0.5
            and p["event_id"] in judgments
        ]
        assert matches, f"no judgment matched scored row {row['id']}"
        for j in matches:
            assert j.classification == CLASS_HOST_DIRECTED, (
                row_text, j.reason, j.score,
            )
        checked += 1
    assert checked == 12


def test_81bcb0_addressee_log_populates_per_utterance_no_nulls(monkeypatch):
    """Verification bullet 3: one addressee-log row per player utterance,
    agent_classification never null (the live session's 14 rows carried
    agent_classification null on every one)."""
    game, rows, judgments, player_rows, addressee = _replay_81bcb0(monkeypatch)
    assert len(player_rows) == 78
    assert len(rows) == 78 == len(judgments)
    for row in rows:
        assert row["agent_classification"] in (
            CLASS_HOST_DIRECTED, CLASS_SIDE_CHATTER, CLASS_SIDE_CLUSTER
        )
        assert row["addressee_score"] is not None
        assert row["addressee_score_components"]["prior"] is not None
        assert row["session_id"] == SESSION_ID


def test_81bcb0_state_block_carries_the_floor_read(monkeypatch):
    """The judgment conditions the reply BEFORE generation: a
    side-cluster judgment injects the floor read; a host-directed one
    injects nothing (speak-by-default holds inside its scope). She never
    narrates the classification itself."""
    game, rows, judgments, player_rows, addressee = _replay_81bcb0(monkeypatch)
    game.last_addressee_judgment = judgments[BEAT1_CORRECTION]  # host
    assert "floor read" not in game.build_state_block()
    game.last_addressee_judgment = judgments[BEAT2_CONTINUATION]  # cluster
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
