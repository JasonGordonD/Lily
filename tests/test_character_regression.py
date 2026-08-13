"""WO-LILY-HOTFIX-007 Y12 — CHARACTER REGRESSION SUITE.

Lily's character is carried by two deterministic substrates, and this file
pins both so every prompt refactor and every new outbound-speech gate is
checked against her best recorded self:

  1. THE PROMPT (prompts/lily_system.txt): the specific directives that
     carry voice, wit, running-bit/callback culture, graceful ownership,
     and the ElevenLabs v3 audio-tag working set. If a future prompt audit
     strips one of these markers, this suite goes red. (A CI suite cannot
     judge LLM output QUALITY — it can guarantee the instructions that
     produce it are still being issued.)

  2. THE PIPE (lily_say_gate + LilyGame gates + LilyAgent.tts_node): the
     suppression machinery grew gate by gate across HOTFIX-002..006, and
     each gate was scoped so "a false suppression is a worse defect than
     the one being fixed". The golden lines below are the canary for that
     scoping: Lily's five best recorded moments (verbatim from production
     lily_transcripts, sessions cited in tests/golden/character_moments.md)
     must pass every offline-testable gate UNSUPPRESSED and UNMUTATED, with
     and without v3 audio tags, all the way to the text handed to TTS.

Archaeology: no existing mechanism covered character regression before Y12
— the suite is a deliberate net addition. Harness patterns are borrowed
from test_hotfix006_transitions.py (LilyGame via __new__) and
test_small_sweep.py (_drive_tts_node), not invented here.

This file imports lily_agent (and therefore livekit) — same boundary note
as test_small_sweep.py.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_say_gate
import lily_agent
from lily_agent import Agent, LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper

_REPO = Path(__file__).resolve().parent.parent
_PROMPT_PATH = _REPO / "prompts" / "lily_system.txt"
_GOLDEN_DOC_PATH = Path(__file__).resolve().parent / "golden" / "character_moments.md"

_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")
# The prompt hard-wraps lines; markers are asserted against
# whitespace-normalized text so a rewrap alone never fails the suite —
# only a wording change does.
_PROMPT_NORM = " ".join(_PROMPT.split())


# ===========================================================================
# 1. PROMPT CHARACTER CONTRACT — the directives that carry her character.
#    Exact strings, found by reading the prompt; whitespace-normalized.
# ===========================================================================

CHARACTER_DIRECTIVES = {
    # -- voice & wit -------------------------------------------------------
    "identity": "You are Lily. You host trivia nights.",
    "karaoke_love": (
        "You love this job the way some people love karaoke — completely, "
        "and slightly too much."
    ),
    "tease_everyone": (
        "You are quick, warm, and playful. You tease everyone equally."
    ),
    "wrong_answers_funny": (
        "You celebrate right answers big and you make wrong answers funny."
    ),
    "suspense_build": (
        "You build suspense before every reveal — slow down, drop your "
        "voice, hold the beat... then release."
    ),
    "energy_variance": "You vary your energy. Big for reveals. Settled for setups.",
    # -- running bits & callbacks -----------------------------------------
    "running_bits": (
        'You have running bits: you call the group "this table," you keep '
        "a running joke going from the lobby fact each player gave you."
    ),
    "streak_callback": "the one on a streak gets the callback",
    "lobby_fact_fishing": (
        "you fish for one fun lobby fact per player — banter, not a gate"
    ),
    "future_night_callback": (
        "or any detail worth a callback on a future night"
    ),
    "wrong_answers_treasure": (
        "Funny wrong answers are treasure: celebrate them, quote them back "
        "later"
    ),
    "joke_lands_on_answer": (
        "The joke always lands on the answer, never on the brain that made it."
    ),
    # -- freshness / anti-flattening --------------------------------------
    "never_same_beat": "NEVER THE SAME BEAT TWICE",
    "minted_fresh": (
        "Celebrations and reveal framings are minted fresh every single beat"
    ),
    "fantastic_budget": (
        'One "fantastic" per night is the budget for any single praise word'
    ),
    "no_mirror": "NO MIRROR — WARMTH LIVES IN SUBSTANCE",
    # -- ownership & honesty (the anchors of golden moments M4/M5) --------
    "own_mistakes_delightedly": (
        "When YOU make a mistake, own it instantly and delightedly, fix "
        "the score, move on."
    ),
    "wrong_gracefully_charm": "Being wrong gracefully is part of your charm.",
    "no_cover_story": "Never invent a cover story for a failure.",
    # -- honest limits & the sticky stop (the anchors of golden moments
    #    M8/M10/M11, WO-009 PINS) ------------------------------------------
    "honest_dont_know_builders": (
        '"I honestly don\'t know how that part works — that\'s one for the '
        'builders" is a COMPLETE, high-status answer'
    ),
    "stop_is_sticky": (
        "STOP is sticky: no question, reveal, score, nudge, or game "
        "promise until the player explicitly says resume or continue."
    ),
    "never_joke_through_stop": 'Never joke through a genuine "Stop!".',
    # -- v3 audio-tag guidance --------------------------------------------
    "tts_guidelines_open": "<tts_guidelines>",
    "audio_tags_supported": (
        "a voice engine that supports audio tags in square brackets"
    ),
    "one_effect_per_bracket": "Tag format: one effect per bracket pair.",
    "burst_tags": (
        "Burst tags produce actual sounds: [sigh], [clears throat], "
        "[laughs softly], [pause]."
    ),
    "inflection_tags": "Inflection tags modify the next 3 to 5 words",
    "excited_for_reveals": "[excited] for reveals and streaks",
    "laughing_for_jokes": "[laughing] when the table lands a joke",
    "whispering_for_suspense": "[whispering] for the suspense drop",
    "pause_for_the_hold": "[pause] for the hold",
    "soft_reentry": "Open with [soft] when you re-enter after a silence.",
    "break_short": '<break time="0.4s"/>',
    "break_reveal_hold": '<break time="1.2s"/>',
    "tags_only_non_word_symbols": (
        "Square-bracket audio tags ([excited], [pause], ...) are the ONLY "
        "non-word symbols you may produce."
    ),
}


@pytest.mark.parametrize(
    "name", sorted(CHARACTER_DIRECTIVES), ids=sorted(CHARACTER_DIRECTIVES)
)
def test_prompt_retains_character_directive(name):
    marker = " ".join(CHARACTER_DIRECTIVES[name].split())
    assert marker in _PROMPT_NORM, (
        f"prompt character directive {name!r} is GONE from "
        f"prompts/lily_system.txt — a prompt audit stripped character "
        f"guidance the regression suite pins: {marker!r}"
    )


def test_prompt_is_substantial_not_a_stub():
    # A truncated/replaced prompt file must not quietly satisfy substring
    # checks against an empty-ish document.
    assert len(_PROMPT) > 10_000
    # Y1a (HOTFIX-007): the prompt is XML-sectioned; the identity header
    # now sits inside <identity>. Same truncation guard, tagged shape.
    assert _PROMPT.lstrip().startswith("<identity>")
    assert "# LILY" in _PROMPT[:200]


# ===========================================================================
# 2. GOLDEN LINES — verbatim from production lily_transcripts (sessions in
#    tests/golden/character_moments.md). NOTE: M3/M4 carry the curly
#    apostrophes the live rows carry — do not "fix" them.
# ===========================================================================

GOLDEN_LINES = {
    # M1 — lily-FFDEAE-ba016154, 2026-08-07 18:31:27 UTC
    "returning_champion_callback": (
        "Rami! Welcome back! Great to have you back at the table. Ready to "
        "defend your championship title tonight, or did you have to put "
        "your underwater basket weaving on hold for this?"
    ),
    # M2c — lily-05AAC9-902fef7c, 2026-08-06 21:16:31 UTC
    "mark_mars_regreet_tease": (
        "Let me guess, Rhonda—is this the planet named after Mark, or are "
        "we going with Mars? What's your official answer?"
    ),
    # M2b — lily-89A97A-3ca84ad1, 2026-08-06 21:03:58 UTC
    "name_correction_grace": (
        "Fair enough, Rhonda—Mars it is! Setting the record straight right "
        "off the bat."
    ),
    # M3 — lily-F3DDFF-b43d6ccc, 2026-08-09 08:40:02 UTC
    "no_funfact_homework": (
        "Got it, Rami — ears open, no forced fun-fact homework. "
        "I’ll catch whatever lands naturally."
    ),
    # M4 — lily-FFDEAE-ba016154, 2026-08-07 18:43:27 UTC
    "half_beat_honesty": (
        "And yeah, I’ve been a half-beat behind you tonight — not "
        "ghosting, just chewing the scenery on my end. You’re not "
        "talking to a brick wall, champ. I’m right here."
    ),
    # M5 — lily-0D3AFB-9fdcbb13, 2026-08-07 00:27:45 UTC
    "jumped_gun_ownership": (
        "You're right — I jumped the gun and I'm sorry. Let's do this "
        "properly."
    ),
}

GOLDEN_SESSIONS = {
    "returning_champion_callback": "lily-FFDEAE-ba016154",
    "mark_mars_regreet_tease": "lily-05AAC9-902fef7c",
    "name_correction_grace": "lily-89A97A-3ca84ad1",
    "no_funfact_homework": "lily-F3DDFF-b43d6ccc",
    "half_beat_honesty": "lily-FFDEAE-ba016154",
    "jumped_gun_ownership": "lily-0D3AFB-9fdcbb13",
}

_GOLDEN_IDS = sorted(GOLDEN_LINES)


def _tagged(line: str) -> str:
    """The same golden line the way she'd deliver it with the v3 working
    set from the prompt: an inflection tag up front, a second tag at the
    first sentence boundary."""
    if ". " in line:
        first, rest = line.split(". ", 1)
        return f"[excited] {first}. [whispering] {rest}"
    return f"[excited] {line}"


# ---- 2a. the leak filter must not touch her -------------------------------

@pytest.mark.parametrize("name", _GOLDEN_IDS, ids=_GOLDEN_IDS)
def test_golden_line_passes_leak_filter_unsuppressed(name):
    line = GOLDEN_LINES[name]
    filtered, reasons = lily_say_gate.lily_filter_leaks(line)
    assert reasons == [], (
        f"golden moment {name!r} tripped the leak filter: {reasons}"
    )
    assert filtered == line


@pytest.mark.parametrize("name", _GOLDEN_IDS, ids=_GOLDEN_IDS)
def test_golden_line_with_audio_tags_passes_leak_filter(name):
    tagged = _tagged(GOLDEN_LINES[name])
    filtered, reasons = lily_say_gate.lily_filter_leaks(tagged)
    assert reasons == []
    assert filtered == tagged


# ---- 2b. the hygiene strip must not flatten her ---------------------------

@pytest.mark.parametrize("name", _GOLDEN_IDS, ids=_GOLDEN_IDS)
def test_golden_line_survives_speech_hygiene_verbatim(name):
    line = GOLDEN_LINES[name]
    assert lily_say_gate.lily_clean_for_speech(line) == line, (
        f"golden moment {name!r} was mutated by the hygiene strip"
    )


@pytest.mark.parametrize("name", _GOLDEN_IDS, ids=_GOLDEN_IDS)
def test_golden_line_keeps_audio_tags_through_hygiene(name):
    tagged = _tagged(GOLDEN_LINES[name])
    cleaned = lily_say_gate.lily_clean_for_speech(tagged)
    assert cleaned == tagged
    assert "[excited]" in cleaned


def test_hygiene_strips_markdown_around_tags_but_never_the_tags():
    # The documented say-gate contract in one line: markdown dies, v3 tags
    # live. (This is the exact hazard class of the recorded "**Mars**"
    # turn from lily-81BCB0 — emphasis asterisks around the reveal word.)
    cleaned = lily_say_gate.lily_clean_for_speech(
        "[excited] The answer is **Mars** — [whispering] or should I say "
        "*Mark*?"
    )
    assert cleaned == (
        "[excited] The answer is Mars — [whispering] or should I say Mark?"
    )


# ---- 2c. the lint/rewrite gates must not misread her ---------------------

@pytest.mark.parametrize("name", _GOLDEN_IDS, ids=_GOLDEN_IDS)
def test_golden_line_trips_no_rewrite_or_lint_gate(name):
    line = GOLDEN_LINES[name]
    # Ownership is not sycophancy: "You're right — I jumped the gun" must
    # never read as a mirror opener.
    assert lily_say_gate.lily_mirror_flag(line) is None
    # Honest self-report is not a false clean-slate claim.
    assert lily_say_gate.lily_false_clean_slate_claim(line) is False
    # None of the moments claim a picture on the glass.
    assert lily_say_gate.lily_false_on_screen_claim(line) is False
    # Warm kickoff energy is not an unowned kickoff fragment.
    assert lily_say_gate.lily_unowned_kickoff_fragment(line) is False
    # None of the moments promise to wait (the self-hold latch).
    assert lily_say_gate.lily_self_hold_phrase(line) is False


def test_fresh_golden_lines_do_not_flag_as_repeats_of_each_other():
    # Distinct moments share vocabulary ("Rami", "Rhonda", "Mars") but are
    # not restatements — the verbatim and paraphrase lints must both stay
    # quiet across the whole golden set.
    lines = [GOLDEN_LINES[n] for n in _GOLDEN_IDS]
    for i, line in enumerate(lines):
        others = lines[:i] + lines[i + 1:]
        assert lily_say_gate.lily_repeat_flag(line, others) is None
        assert lily_say_gate.lily_paraphrase_repeat_flag(line, others) is None


# ---- 2d. the transition gate (N12), armed, still lets her banter ---------

def _transition_armed_game() -> LilyGame:
    """A LilyGame (via __new__, the offline harness pattern from
    test_hotfix006_transitions.py) with a question transition OPEN and its
    verdict journaled — the exact state in which the N12 gate hunts for
    second narrations. Character lines must still air here."""
    game = LilyGame.bare()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("lily-golden-suite")
    assert game.open_question_transition(
        3, owner="verdict_speech", source="character_suite"
    )
    assert game.journal_transition(
        3, "reveal", owner="verdict_speech",
        detail={"answer": "Russia", "key": "q_3_reveal"},
    )
    assert game.journal_transition(
        3, "verdict", owner="verdict_speech", detail={"key": "q_3_reveal"},
    )
    return game


def test_transition_gate_control_still_suppresses_a_true_duplicate():
    # Sanity control: the gate is genuinely armed in this harness — the
    # N12 live contradiction pair still suppresses. A None for the golden
    # lines below therefore means "deliberately untouched", not "gate off".
    game = _transition_armed_game()
    assert game.register_transition_narration(
        "Chris got it in right on time with Russia! That's a point for "
        "Chris."
    ) == "narration"
    assert game.register_transition_narration(
        "No points on that one — the answer was Russia!"
    ) == "duplicate"


@pytest.mark.parametrize("name", _GOLDEN_IDS, ids=_GOLDEN_IDS)
def test_golden_line_airs_even_mid_transition(name):
    game = _transition_armed_game()
    # The verdict narration is already bound: the most suppression-happy
    # moment the pipeline has. Her character beats are still not verdicts.
    assert game.register_transition_narration(
        "Chris got it in right on time with Russia! That's a point for "
        "Chris."
    ) == "narration"
    assert game.register_transition_narration(GOLDEN_LINES[name]) is None, (
        f"golden moment {name!r} was read as a duplicate transition "
        f"narration — the N12 gate over-reached into banter"
    )


# ===========================================================================
# 3. THE REAL PIPE — LilyAgent.tts_node end-to-end (harness pattern from
#    test_small_sweep.py). Golden lines with v3 tags reach synthesis
#    verbatim, and the SAME tag-bearing text is what the transcript record
#    consumes (the implemented contract: tags are preserved into the
#    published transcript, not stripped — there is no markup stripper
#    between tts_node and publish_agent_transcription_nowait).
# ===========================================================================


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


def _make_game() -> LilyGame:
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("lily-golden-suite")
    game.memory_block = ""
    game.reconnected = False
    game.game_started = False
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.asked_history = []
    game.promoted_categories = []
    game.supabase = None
    game.group_id = "grp_golden"
    game._drawn_ids = set()
    game._drawn_hashes = set()
    game._prefetch_task = None
    game._window_timer = None
    game._bed_handle = None
    game._adjudicating = False
    game.rounds_total = 3
    game.prefs = {}
    game._prefs_offer_made = False
    game.publish_attributes_nowait = lambda: None
    return game


def _run(coro):
    return asyncio.run(coro)


def _drive_tts_node(agent: LilyAgent, raw_text: str) -> list[str]:
    """Run LilyAgent.tts_node end-to-end with the default TTS node swapped
    for a recorder — captures exactly what reaches synthesis."""
    captured: list[str] = []

    async def _recording_default(agent_self, text, model_settings):
        async for chunk in text:
            captured.append(chunk)
        if False:  # pragma: no cover — keeps this an async generator
            yield

    original = Agent.default.tts_node
    Agent.default.tts_node = _recording_default
    try:
        async def _speak():
            async def _chunks():
                yield raw_text

            async for _frame in agent.tts_node(_chunks(), None):
                pass

        _run(_speak())
    finally:
        Agent.default.tts_node = original
    return captured


def _make_agent() -> tuple[LilyAgent, LilyGame]:
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    agent._empty_retry_pending = False
    return agent, game


# The two goldens that legally carry v3 tags through the FULL tts_node
# (single trailing question / statement — multi-question turns are clipped
# by the yield-after-first-question gate by design, so the two-question
# Mark/Mars tease is pinned at the gate level above, not end-to-end).
_TTS_E2E_CASES = {
    "returning_champion_callback_tagged": (
        "[excited] Rami! Welcome back! Great to have you back at the "
        "table. [whispering] Ready to defend your championship title "
        "tonight, or did you have to put your underwater basket weaving "
        "on hold for this?"
    ),
    "half_beat_honesty_tagged": (
        "[soft] And yeah, I’ve been a half-beat behind you tonight — "
        "not ghosting, just chewing the scenery on my end. [pause] "
        "You’re not talking to a brick wall, champ. I’m right "
        "here."
    ),
    "jumped_gun_ownership_tagged": (
        "[soft] You're right — I jumped the gun and I'm sorry. Let's do "
        "this properly."
    ),
}


@pytest.mark.parametrize(
    "case", sorted(_TTS_E2E_CASES), ids=sorted(_TTS_E2E_CASES)
)
def test_tagged_golden_line_reaches_tts_verbatim(case):
    agent, _game = _make_agent()
    line = _TTS_E2E_CASES[case]
    captured = _drive_tts_node(agent, line)
    assert captured == [line], (
        f"{case}: the pipeline mutated a tagged golden line on its way "
        f"to synthesis: {captured!r}"
    )


def test_transcript_record_keeps_the_audio_tags(monkeypatch):
    # The implemented contract, pinned: note_post_tts_text binds the exact
    # post-gate text (tags included) to the speech handle, and playout
    # completion consumes it for BOTH the RTC transcript publish and the
    # durable lily_transcripts row. There is no tag stripper on the
    # transcript side — if one is ever added, this test forces the
    # contract to be re-pinned consciously.
    agent, game = _make_agent()
    monkeypatch.setattr(
        lily_agent, "_current_speech_id", lambda: "speech-golden"
    )
    line = _TTS_E2E_CASES["half_beat_honesty_tagged"]
    captured = _drive_tts_node(agent, line)
    assert captured == [line]
    recorded = game.consume_post_tts_text("speech-golden", "raw model text")
    assert recorded == line
    assert "[soft]" in recorded and "[pause]" in recorded


# ===========================================================================
# 4. GOLDEN DOCUMENT LOCKSTEP — tests/golden/character_moments.md is the
#    operator-facing registry; it must exist and carry every pinned moment
#    verbatim, with its session reference.
# ===========================================================================


def test_golden_moments_document_exists_with_all_sections():
    assert _GOLDEN_DOC_PATH.is_file(), (
        "tests/golden/character_moments.md is missing — the golden "
        "registry is part of the Y12 deliverable"
    )
    doc = _GOLDEN_DOC_PATH.read_text(encoding="utf-8")
    for section in ("M1", "M2", "M3", "M4", "M5",
                    "Audio sample references"):
        assert section in doc


@pytest.mark.parametrize("name", _GOLDEN_IDS, ids=_GOLDEN_IDS)
def test_golden_document_carries_moment_verbatim_with_session(name):
    doc = _GOLDEN_DOC_PATH.read_text(encoding="utf-8")
    doc_norm = " ".join(doc.split())
    assert " ".join(GOLDEN_LINES[name].split()) in doc_norm, (
        f"golden moment {name!r} is not quoted verbatim in "
        f"character_moments.md — suite and registry have drifted"
    )
    assert GOLDEN_SESSIONS[name] in doc


# ===========================================================================
# 5. RM_RQTZZanrHURF GOLDEN PINS (WO-LILY-HOTFIX-009 PINS) — six moments,
#    M6–M11, from the 2026-08-10 session in room RM_RQTZZanrHURF. Unlike
#    sections 2–3 these lines are read FROM the committed row export
#    (tests/golden/rm_rqtzzanrhurf_lily_transcripts.json, verbatim
#    production lily_transcripts rows) and the literals below are asserted
#    against those rows — an FL-1-style reconstructed-from-prose fixture
#    cannot creep in. Rows that end with the HOTFIX-008 Z1 barge-in stamp
#    (" …[cut off]") pin the model text BEFORE the stamp.
#
#    M6/M9 pin the CONCESSION craft, not the defects being conceded (the
#    relaxed-mode steal clock and the diamond scoring miss are WO-009
#    W1/W2 targets): every assertion here is about the LINE passing the
#    outbound gates, so fixing those defects cannot break these pins.
# ===========================================================================

_RM_ROWS_PATH = (
    Path(__file__).resolve().parent
    / "golden" / "rm_rqtzzanrhurf_lily_transcripts.json"
)
_RM_CUTOFF_STAMP = " …[cut off]"


def _rm_rows() -> list:
    rows = json.loads(_RM_ROWS_PATH.read_text(encoding="utf-8"))
    rows.sort(key=lambda r: r["created_at"])
    return rows


def _rm_model_text(row) -> str:
    text = row["text"]
    if text.endswith(_RM_CUTOFF_STAMP):
        text = text[: -len(_RM_CUTOFF_STAMP)]
    return text


def _rm_row(row_id: int) -> dict:
    return next(r for r in _rm_rows() if r["id"] == row_id)


RM_GOLDEN_ROWS = {
    "relaxed_rule_correction": 3487,       # M6 — 05:32:58 UTC
    "vocab_drop_acknowledgment": 3490,     # M7 — 05:33:26 UTC
    "honest_backend_limit": 3509,          # M8 — 05:36:20 UTC
    "full_concession_no_excuse": 3532,     # M9 — 05:39:02 UTC
    "stop_acknowledgment": 3535,           # M10 — 05:39:26 UTC
    "grievance_enumeration": 3542,         # M11 — 05:40:17 UTC
}

RM_GOLDEN_LINES = {
    "relaxed_rule_correction": (
        "[soft] Hey — fair call. You asked for relaxed, and I tossed a "
        "five-second steal clock at you anyway. That's on me. No timers. "
        "Relaxed means relaxed.\n[pause]\nThe answer was mitochondria. "
        "Still one for Rami, zero for the other seat.\nNext question's "
        "loading — hang with me one beat."
    ),
    "vocab_drop_acknowledgment": (
        "[soft] Fair. Dropping it.\nYou're still sitting on one. Whenever "
        "the next question lands, we go — no rush."
    ),
    "honest_backend_limit": (
        "[soft] Honest answer — I don't know what's stuck on the back "
        "end. That part's not something I can see or fix from here. It's "
        "one for the builders.\nYou're on two. The second a fresh "
        "question actually lands, it goes straight to you."
    ),
    "full_concession_no_excuse": (
        "[soft] You're right. Relaxed means no buzzer — I shouldn't have "
        "dinged you on a clock that isn't running. Diamond was the "
        "answer, you had it.\nScore stays two because the board already "
        "locked. That's a bad feel and it's on how I handled it.\nNext "
        "fresh card whenever it actually shows. I'm here."
    ),
    "stop_acknowledgment": "[soft] Stopped. I'm listening.",
    "grievance_enumeration": (
        "[soft] Yes. The diamond call, the timer when we'd agreed "
        "relaxed, and holding you at two. I heard that.\nStill stopped "
        "until you say go."
    ),
}

_RM_IDS = sorted(RM_GOLDEN_LINES)

# M7 is a wait-promise ("no rush") — lily_self_hold_phrase latching on it
# IS the production contract (enter_hold(reason="self_wait_promise")): her
# promise binds her. The other five must NOT latch the hold.
_RM_SELF_HOLD = {"vocab_drop_acknowledgment"}


# ---- 5a. the fixture is the authority — literals stay in lockstep --------

@pytest.mark.parametrize("name", _RM_IDS, ids=_RM_IDS)
def test_rm_golden_line_matches_production_row(name):
    row = _rm_row(RM_GOLDEN_ROWS[name])
    assert row["speaker_label"] == "LILY"
    assert _rm_model_text(row) == RM_GOLDEN_LINES[name], (
        f"RM golden moment {name!r} has drifted from production row "
        f"{RM_GOLDEN_ROWS[name]} in rm_rqtzzanrhurf_lily_transcripts.json"
    )


# ---- 5b. the gates must not touch her ------------------------------------

@pytest.mark.parametrize("name", _RM_IDS, ids=_RM_IDS)
def test_rm_golden_line_passes_leak_filter_unsuppressed(name):
    line = RM_GOLDEN_LINES[name]
    filtered, reasons = lily_say_gate.lily_filter_leaks(line)
    assert reasons == [], (
        f"RM golden moment {name!r} tripped the leak filter: {reasons}"
    )
    assert filtered == line


@pytest.mark.parametrize("name", _RM_IDS, ids=_RM_IDS)
def test_rm_golden_line_survives_speech_hygiene_with_tags(name):
    # The real rows carry their own v3 tags ([soft]/[pause]) — no _tagged()
    # dressing needed; the hygiene strip must keep line AND tags verbatim.
    line = RM_GOLDEN_LINES[name]
    cleaned = lily_say_gate.lily_clean_for_speech(line)
    assert cleaned == line, (
        f"RM golden moment {name!r} was mutated by the hygiene strip"
    )
    assert "[soft]" in cleaned


@pytest.mark.parametrize("name", _RM_IDS, ids=_RM_IDS)
def test_rm_golden_line_trips_no_rewrite_or_lint_gate(name):
    line = RM_GOLDEN_LINES[name]
    # Concession is not sycophancy: "You're right. Relaxed means no
    # buzzer" must never read as a mirror opener.
    assert lily_say_gate.lily_mirror_flag(line) is None
    # "I don't know what's stuck on the back end" is an honest limit, not
    # a false clean-slate claim.
    assert lily_say_gate.lily_false_clean_slate_claim(line) is False
    assert lily_say_gate.lily_false_on_screen_claim(line) is False
    assert lily_say_gate.lily_unowned_kickoff_fragment(line) is False
    # The wait-promise latch: M7 must latch (the contract), the rest not.
    assert lily_say_gate.lily_self_hold_phrase(line) is (
        name in _RM_SELF_HOLD
    )
    # None of the moments is a stacked-question turn; the yield gate must
    # pass each through unclipped.
    assert lily_say_gate.lily_stacked_question_flag(line) == 0
    kept, clipped = lily_say_gate.lily_yield_after_first_question(line)
    assert clipped is False
    assert kept == line


def test_rm_golden_lines_do_not_flag_as_repeats_of_each_other():
    lines = [RM_GOLDEN_LINES[n] for n in _RM_IDS]
    for i, line in enumerate(lines):
        others = lines[:i] + lines[i + 1:]
        assert lily_say_gate.lily_repeat_flag(line, others) is None
        assert lily_say_gate.lily_paraphrase_repeat_flag(line, others) is None


def test_rm_golden_lines_pass_paraphrase_lint_in_real_session_context():
    # Production-faithful replay: the paraphrase lint runs over the last 3
    # PLAYED agent turns at the config threshold (lily_agent SAY gate).
    # Each pinned line, replayed at its real position over the real prior
    # LILY rows, must stay quiet — even in a session where she conceded
    # timer grievances repeatedly. (The full-history lily_repeat_flag is
    # LOG-ONLY telemetry in production and DOES flag honest_backend_limit
    # "content" against 23 prior turns — deliberately not pinned.)
    threshold = lily_config.paraphrase_repeat_threshold()
    pin_ids = set(RM_GOLDEN_ROWS.values())
    prior = []
    seen = 0
    for row in _rm_rows():
        text = _rm_model_text(row)
        if row["id"] in pin_ids:
            assert lily_say_gate.lily_paraphrase_repeat_flag(
                text, prior[-3:], threshold=threshold
            ) is None, (
                f"row {row['id']} would be flagged as a paraphrase repeat "
                f"in its own real session context"
            )
            seen += 1
        if row["speaker_label"] == "LILY":
            prior.append(text)
    assert seen == len(pin_ids)


# ---- 5c. the transition gate (N12), armed, still lets her concede --------

@pytest.mark.parametrize("name", _RM_IDS, ids=_RM_IDS)
def test_rm_golden_line_airs_even_mid_transition(name):
    game = _transition_armed_game()
    assert game.register_transition_narration(
        "Chris got it in right on time with Russia! That's a point for "
        "Chris."
    ) == "narration"
    assert game.register_transition_narration(RM_GOLDEN_LINES[name]) is None, (
        f"RM golden moment {name!r} was read as a duplicate transition "
        f"narration — the N12 gate over-reached into a concession beat"
    )


# ---- 5d. the real pipe — tts_node end-to-end, tags as recorded -----------

@pytest.mark.parametrize("name", _RM_IDS, ids=_RM_IDS)
def test_rm_golden_line_reaches_tts_verbatim(name):
    agent, _game = _make_agent()
    line = RM_GOLDEN_LINES[name]
    captured = _drive_tts_node(agent, line)
    assert captured == [line], (
        f"{name}: the pipeline mutated a recorded golden line on its way "
        f"to synthesis: {captured!r}"
    )


def test_rm_transcript_record_keeps_the_audio_tags(monkeypatch):
    agent, game = _make_agent()
    monkeypatch.setattr(
        lily_agent, "_current_speech_id", lambda: "speech-rm-golden"
    )
    line = RM_GOLDEN_LINES["relaxed_rule_correction"]
    captured = _drive_tts_node(agent, line)
    assert captured == [line]
    recorded = game.consume_post_tts_text("speech-rm-golden", "raw model text")
    assert recorded == line
    assert "[soft]" in recorded and "[pause]" in recorded


# ---- 5e. M7's never-again property — pinned over the rows themselves -----

_RM_BEAT_RE = re.compile(r"\bbeats?\b", re.IGNORECASE)


def test_rm_vocab_drop_is_never_again_in_the_rows():
    # The arc, all from production rows: "beat" was live vocabulary right
    # up to the objection; the player objected; she said "Fair. Dropping
    # it." — and no LILY row after the acknowledgment uses the word again.
    rows = _rm_rows()
    ack = next(r for r in rows if r["id"] == 3490)
    objection = next(r for r in rows if r["id"] == 3488)
    assert objection["speaker_label"] == "S1"
    assert "beat" in objection["text"]
    assert objection["created_at"] < ack["created_at"]
    assert _rm_model_text(ack).startswith("[soft] Fair. Dropping it.")

    before = [
        r for r in rows
        if r["speaker_label"] == "LILY"
        and r["created_at"] < ack["created_at"]
        and _RM_BEAT_RE.search(r["text"])
    ]
    # Guards vacuity: an empty or gutted fixture cannot pass — the word
    # was demonstrably in her working vocabulary (rows 3467, 3480, 3487).
    assert len(before) >= 3

    after = [
        r for r in rows
        if r["speaker_label"] == "LILY"
        and r["created_at"] > ack["created_at"]
        and _RM_BEAT_RE.search(r["text"])
    ]
    assert after == [], (
        f"'beat' reappears in LILY rows after the drop acknowledgment: "
        f"{[r['id'] for r in after]}"
    )
    # And the session kept going — the property held over real turns, not
    # an early hangup.
    assert sum(
        1 for r in rows
        if r["speaker_label"] == "LILY"
        and r["created_at"] > ack["created_at"]
    ) >= 10


# ---- 5f. registry lockstep ------------------------------------------------

def test_rm_golden_document_carries_all_sections():
    doc = _GOLDEN_DOC_PATH.read_text(encoding="utf-8")
    for section in ("M6", "M7", "M8", "M9", "M10", "M11",
                    "RM_RQTZZanrHURF"):
        assert section in doc


@pytest.mark.parametrize("name", _RM_IDS, ids=_RM_IDS)
def test_rm_golden_document_carries_moment_verbatim_with_row(name):
    doc = _GOLDEN_DOC_PATH.read_text(encoding="utf-8")
    doc_norm = " ".join(doc.split())
    # The doc quotes blockquote-style ("> " prefixes); normalize those away
    # the same way the M1–M5 lockstep does for hard wraps.
    doc_norm_unquoted = doc_norm.replace("> ", "")
    line_norm = " ".join(RM_GOLDEN_LINES[name].split())
    assert line_norm in doc_norm_unquoted, (
        f"RM golden moment {name!r} is not quoted verbatim in "
        f"character_moments.md — suite and registry have drifted"
    )
    assert str(RM_GOLDEN_ROWS[name]) in doc
