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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    assert _PROMPT.lstrip().startswith("# LILY")


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
    game = LilyGame.__new__(LilyGame)
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
    game = LilyGame.__new__(LilyGame)
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
