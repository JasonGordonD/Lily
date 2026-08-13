"""WO-LILY-SELFKNOWLEDGE-INTAKE-001 — offline verification.

Source fixtures: the two architect-probe sessions (11:56 and 12:08,
2026-07-15) whose quoted moments are encoded below. The live-replay
criteria (10× exchange replays) are LLM evals and run against the
deployed agent; THIS suite pins every deterministic mechanism the WO
ships: the manifest↔options-block CI check (Task 3), the delta/stamp
flow (Task 3), the mirror lint (Task 2a), the greeting branch and
ordering contracts (Task 5), and the intake overlap repair (Task 4).
"""

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_capabilities
import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "lily_system.txt"
PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
# The prompt hard-wraps at ~70 cols — phrase assertions run against the
# whitespace-normalized text so a rewrap never fakes a contract change.
PROMPT_NORM = " ".join(PROMPT.split())


def _options_block() -> str:
    start = PROMPT.index("## WHAT THE TABLE CAN ASK FOR")
    end = PROMPT.index("## ", start + 10)
    return PROMPT[start:end]


# -- Task 3: manifest <-> options block can never disagree ---------------------


def test_every_askable_feature_appears_in_the_options_block():
    """Enumeration-from-manifest: the 12:08 user caught her rundown
    omitting pictures entirely. Every askable manifest entry pins a
    marker that MUST appear in WHAT THE TABLE CAN ASK FOR."""
    block = _options_block()
    missing = [
        (key, marker)
        for key, marker in lily_capabilities.lily_askable_markers()
        if marker not in block
    ]
    assert not missing, (
        f"manifest features absent from the options block: {missing} — "
        "either add the option line to WHAT THE TABLE CAN ASK FOR or "
        "correct the manifest's prompt_marker"
    )


def test_feature_version_covers_every_entry():
    version = lily_capabilities.lily_feature_version()
    assert version >= 2  # voice presets landed as version 2
    assert version >= max(
        int(e.get("since", 1)) for e in lily_capabilities.LILY_CAPABILITIES
    )


def test_whats_new_delta():
    # A table stamped at 1 hears about everything since — and ONLY that.
    delta = lily_capabilities.lily_whats_new(1)
    assert any("voice" in line for line in delta)  # v2
    assert any("photo" in line for line in delta)  # v3
    v1_descriptions = [
        e["description"]
        for e in lily_capabilities.LILY_CAPABILITIES
        if int(e.get("since", 1)) <= 1
    ]
    assert not any(line in v1_descriptions for line in delta)
    # Current stamp → silence; unknown stamp → silence (never fabricate
    # "new since last time" for a table whose history we don't know).
    assert lily_capabilities.lily_whats_new(
        lily_capabilities.lily_feature_version()
    ) == []
    assert lily_capabilities.lily_whats_new(None) == []
    assert lily_capabilities.lily_whats_new("garbage") == []


def test_availability_lines_fail_closed_and_stay_honest():
    # Everything on → nothing to caveat.
    all_on = {
        entry["availability_key"]: True
        for entry in lily_capabilities.LILY_CAPABILITIES
        if entry.get("availability_key")
    }
    assert lily_capabilities.lily_availability_lines(all_on) == []
    # Unknown flags are OFF — never overclaim (the 12:08 "I absolutely
    # do!" while the key was unset).
    lines = lily_capabilities.lily_availability_lines({})
    assert len(lines) == len(all_on)
    joined = " ".join(lines)
    assert "not switched on tonight" in joined
    # Pictures caveat is PARTIAL: generated imagery is never gated
    # (analyst correction #3) — only real-photo sourcing rides the key.
    assert "generated pictures work regardless" in joined


# -- Task 2a: mirror lint (log-only) -------------------------------------------

FIXTURE_MIRROR_TURNS = [
    "That is a fantastic point about returning users.",
    "Great question! So, the way it works is—",
    "[excited] Exactly— and that's the perfect sweet spot for a table.",
    "You're absolutely right, and that's exactly what I was—",
    "This is such a great idea for the lobby flow.",
]

CLEAN_TURNS = [
    # Celebrating an ANSWER is warmth working as designed, not a mirror.
    "That was a fantastic answer, Sarah — point!",
    "Sarah, that's it — point.",
    "Two of you at once — you first, then you.",
    "I honestly don't know how that part works — that's one for the builders.",
    "[whispering] Round two. The stakes just doubled.",
    # Mid-turn enthusiasm about content is not an opener.
    "Here's the thing though — and honestly this question is a great one to ask the builders.",
]


def test_mirror_flag_catches_the_fixture_turns():
    for turn in FIXTURE_MIRROR_TURNS:
        assert lily_say_gate.lily_mirror_flag(turn) is not None, turn


def test_mirror_flag_leaves_clean_turns_alone():
    for turn in CLEAN_TURNS:
        assert lily_say_gate.lily_mirror_flag(turn) is None, turn


def test_prompt_carries_the_contract_sections():
    # Prompt-contract tripwires: the sections the WO ships must exist,
    # with their load-bearing sentences intact.
    assert "## NO MIRROR — WARMTH LIVES IN SUBSTANCE" in PROMPT
    assert "## WHAT YOU KNOW ABOUT YOURSELF" in PROMPT
    assert "## WHEN THE TABLE GOES META" in PROMPT
    assert "## INTAKE — NAMES, ONE AT A TIME" in PROMPT
    assert "I honestly don't know how that part works" in PROMPT_NORM
    assert "never deny one you have" in PROMPT_NORM
    assert '"show me" gets shown' in PROMPT_NORM
    assert "one of mine, but it's not switched on tonight" in PROMPT_NORM
    assert "your FIRST sentence" in PROMPT_NORM
    assert "the mirror ban never" in PROMPT_NORM


# -- fixture game --------------------------------------------------------------


def _make_game() -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("selfknowledge-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.group_id = "grp_fixture"
    game.supabase = object()  # persist_prefs is mocked below; non-None gates it in
    game.memory_block = ""
    game.memory_total_games = 0
    game.memory_player_names = []
    game.prefs = {}
    game._prefs_offer_made = False
    game._memory_disclosure_offered = False
    game._whats_new_pending = False
    game.device_candidate_group_id = None
    game.persisted = []
    game.persist_prefs = lambda: game.persisted.append(dict(game.prefs))
    return game


# -- Task 3: delta + stamp flow ------------------------------------------------


def test_unstamped_returning_table_stamps_silently():
    game = _make_game()
    game.memory_block = "[RETURNING TABLE] ..."
    text = game.whats_new_instruction()
    assert text == ""  # no fabricated "new since last time"
    assert game._whats_new_pending is False
    current = lily_capabilities.lily_feature_version()
    assert game.prefs["last_seen_feature_version"] == current
    assert game.persisted  # persisted through the normal prefs path


def test_lagged_table_gets_one_delta_line_and_stamps_after_greet():
    game = _make_game()
    game.memory_block = "[RETURNING TABLE] ..."
    game.prefs = {"pacing": "relaxed", "last_seen_feature_version": 1}
    text = game.whats_new_instruction()
    assert "voice" in text.lower()
    assert "never mention it again tonight" in text
    assert game._whats_new_pending is True
    # Not stamped yet — the mention hasn't played.
    assert game.prefs["last_seen_feature_version"] == 1

    # The greet plays out and confirms → stamp moves forward.
    game.say_registry.claim("session_greet", owner="s1")
    game._resume_preemptive = lambda: None
    game._pending_reveal_event = None
    game._state_note = None
    game.armed_question = None
    game._adjudicating = False
    game.on_agent_speech_finished("hi, I'm Lily", speech_id="s1")
    assert game._whats_new_pending is False
    assert game.prefs["last_seen_feature_version"] == (
        lily_capabilities.lily_feature_version()
    )
    assert game.prefs["pacing"] == "relaxed"  # rest of the dict intact


def test_current_table_hears_nothing():
    game = _make_game()
    game.memory_block = "[RETURNING TABLE] ..."
    game.prefs = {
        "last_seen_feature_version": lily_capabilities.lily_feature_version()
    }
    assert game.whats_new_instruction() == ""
    assert game._whats_new_pending is False
    assert game.persisted == []  # idempotent — no rewrite


def test_feature_stamp_joins_the_forget_cascade():
    """The stamp lives in lily_group_prefs.prefs — hard-deleted whole-row
    by the forget cascade. Pin the interlock: if the prefs table ever
    leaves the cascade, the stamp (recognition data) would survive a
    forget, which is a privacy defect, not just a staleness one."""
    import lily_forget

    assert "lily_group_prefs" in (
        tuple(lily_forget.HARD_DELETE_GROUP_TABLES)
        + tuple(lily_forget.OPTIONAL_GROUP_TABLES)
    )


# -- Task 5: recognition before the first-time question ------------------------


def test_memory_branch_never_asks_first_time():
    game = _make_game()
    game.memory_block = "[RETURNING TABLE] Rami, 4 wins"
    # HOTFIX-010 V3: the recognition beat composes after the first utterance.
    game._first_human_utterance_seen = True
    text = game.greeting_instructions()
    assert "Do NOT ask if it's their first time" in text
    assert "welcome back" in text


def test_cold_branch_reserves_the_first_time_question():
    game = _make_game()
    # HOTFIX-010 V3: the first-time question is deferred to the no-memory
    # branch, which composes after the first utterance (never in the cold
    # opener — that contract is pinned in test_hotfix010_identity_resequence).
    game._first_human_utterance_seen = True
    text = game.greeting_instructions()
    assert "whether it's their first time" in text
    assert "Never claim you remember them" in text


def test_on_enter_awaits_memory_before_the_greeting():
    """Ordering tripwire (Task 5): the greeting composes AFTER the bounded
    memory await — the two-for-two fixture regression was recognition
    arriving after the first-time question had already been asked."""
    source = inspect.getsource(LilyAgent.on_enter)
    await_at = source.index("await_greeting_memory")
    greet_at = source.index('"session_greet"')
    assert await_at < greet_at


# -- Task 4: intake overlap repair ---------------------------------------------


def test_intake_overlap_produces_the_ordering_repair():
    game = _make_game()
    game.game_started = False
    # First voice speaks 10.0→12.0; second voice starts at 11.2 — overlap.
    game.note_intake_overlap("S1", 10.0, 12.0)
    assert game.sk.status_notes == []
    game.note_intake_overlap("S2", 11.2, 13.0)
    assert any("ordering repair" in note for note in game.sk.status_notes)


def test_intake_overlap_same_label_and_sequential_are_silent():
    game = _make_game()
    game.note_intake_overlap("S1", 10.0, 12.0)
    game.note_intake_overlap("S1", 11.0, 13.0)  # same voice — silence
    game.note_intake_overlap("S2", 13.5, 14.0)  # clean hand-off — silence
    assert game.sk.status_notes == []


def test_intake_overlap_is_rate_limited():
    game = _make_game()
    game.note_intake_overlap("S1", 10.0, 12.0)
    game.note_intake_overlap("S2", 11.0, 13.0)
    game.note_intake_overlap("S3", 12.5, 14.0)  # second overlap, <20s later
    assert len(game.sk.status_notes) == 1


def test_prompt_intake_protocol_exempts_solo():
    intake_start = PROMPT.index("## INTAKE — NAMES, ONE AT A TIME")
    intake_end = PROMPT.index("## ", intake_start + 10)
    section = " ".join(PROMPT[intake_start:intake_end].split())
    assert "Solo player: none of this" in section
    assert "one at a time" in section
    assert "you first, then you" in section
    assert "name first, glory after" in section
