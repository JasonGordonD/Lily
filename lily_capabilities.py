"""
lily_capabilities.py — the capabilities manifest (WO-LILY-SELFKNOWLEDGE-
INTAKE-001 Task 3).

THE single source of truth for what Lily can do, player-facing. The
architect probe (2026-07-15 fixtures) showed the question "how does a
returning user learn about new features?" had NO true answer in the
build — so under pressure she fabricated one ("the system relies on my
system instructions to flag when a new feature rollout has occurred").
This module makes the honest answer exist: her table card (the rematch
delta note) tells her what's new since a group last played.

Design decisions, on the record:
  - The manifest lives in the CODEBASE, not a database row. The WO names
    the codebase "the only complete record" — a DB copy would be a second
    source of truth that can disagree with the code, which is exactly the
    options-block/manifest divergence class this WO bans. Per-group state
    (the `last_seen_feature_version` stamp) IS persisted — as a key in
    the opaque `lily_group_prefs.prefs` jsonb (migration 013's documented
    extension path), so it joins the forget cascade and the group-id
    re-key like everything group-keyed, with zero schema change.
  - Two layers per feature (the 12:08 fixture's availability dimension):
    CAPABILITY (what she can do — always true of the build) and SESSION
    AVAILABILITY (what is switched on right now — EXA-gated real-entity
    picture sourcing, sensor-gated adult deck). `availability_key` names
    the runtime flag; features without one are always available.
    Generated imagery is NOT availability-bound (analyst correction #3:
    the render path is not phase-gated and lily_imagegen needs no EXA
    key) — only REAL-entity image sourcing rides the EXA key.
  - `prompt_marker` pins each askable feature to the WHAT THE TABLE CAN
    ASK FOR block in prompts/lily_system.txt; tests/test_selfknowledge.py
    CI-checks that the two can never disagree (the 12:08 user caught her
    rundown omitting pictures — enumeration-from-manifest makes omission
    impossible).

STANDING RULE (README WO checklist): any WO shipping a player-facing
feature appends its entry here and bumps LILY_FEATURE_VERSION — and
direct changes outside the WO trail (Rami's voice presets are the
precedent) get added at commit time.

Stdlib-only; pure functions; no I/O.
"""

from typing import Optional


# Bumped whenever a player-facing feature lands. The delta a returning
# table hears is every feature with `since` greater than their stamp.
LILY_FEATURE_VERSION: int = 3

# The backfill (version 1) is the audited launch set; voice presets are
# version 2 (Rami's direct change, 2026-07-31 — added at commit time per
# the standing rule; her fixture claim about them was TRUE).
LILY_CAPABILITIES: list = [
    {
        "key": "freeform",
        "code_ref": "lily_evaluation",
        "tools": [],
        "since": 1,
        "description": "freeform play — shout the answer, first clear answer wins",
        "prompt_marker": "Freeform play is the default",
        "askable": True,
    },
    {
        "key": "multiple_choice",
        "code_ref": "lily_evaluation:lily_tier1_evaluate_mc",
        "tools": ["lily_set_round_format"],
        "since": 1,
        "description": "multiple choice on request (round two runs it by default)",
        "prompt_marker": "Multiple choice on request",
        "askable": True,
    },
    {
        "key": "fifty_fifty",
        "code_ref": "lily_agent:LilyAgent.lily_use_fifty_fifty",
        "tools": ["lily_use_fifty_fifty"],
        "since": 1,
        "description": "one 50/50 lifeline per player per game",
        "prompt_marker": "50/50 lifeline",
        "askable": True,
    },
    {
        "key": "skip",
        "code_ref": "lily_agent:LilyGame.skip_question",
        "tools": [],
        "since": 1,
        "description": "any player can skip a question, no spotlight",
        "prompt_marker": '"Skip"',
        "askable": True,
    },
    {
        "key": "steal",
        "code_ref": "lily_agent:LilyGame.open_window",
        "tools": [],
        "since": 1,
        "description": "a missed question opens a five-second steal window",
        "prompt_marker": "steal window",
        "askable": True,
    },
    {
        "key": "bonus_points",
        "code_ref": "lily_agent:LilyAgent.lily_award_bonus",
        "tools": ["lily_award_bonus"],
        "since": 1,
        "description": "point values climb each round; the wager round risks it all",
        # Gameplay structure, not an askable option — described in the
        # ROUNDS/FINAL prose, not the options block.
        "prompt_marker": None,
        "askable": False,
    },
    {
        "key": "adult_deck",
        "code_ref": "lily_agent:LilyAgent.lily_enter_adult_mode",
        "tools": ["lily_enter_adult_mode"],
        "since": 1,
        "description": "the grown-up deck, every player 18+ and opted in aloud",
        "prompt_marker": "grown-up deck",
        "askable": True,
        # The child-signal sensor and the adult deck deploy as one unit:
        # no sensor watching the room, no adult deck tonight.
        "availability_key": "adult_deck",
    },
    {
        "key": "pacing",
        "code_ref": "lily_agent:LilyGame.set_pacing",
        "tools": ["lily_set_pacing"],
        "since": 1,
        "description": "timed or relaxed pacing, remembered as the table's usual",
        "prompt_marker": "Pacing, the table's call",
        "askable": True,
    },
    {
        "key": "pictures",
        "code_ref": "lily_imagegen",
        "tools": ["lily_show_demo_picture"],
        "since": 1,
        "description": "picture rounds on the screen, on request ('pictures on')",
        "prompt_marker": "Pictures, offered once and lightly",
        "askable": True,
        # ONLY the real-entity sourcing rail is EXA-gated; generated
        # imagery and the render path need no key (analyst correction #3).
        "availability_key": "pictures_real_sourcing",
        "availability_partial": (
            "real-photo questions need the image search switched on; "
            "generated pictures work regardless"
        ),
    },
    {
        "key": "forget_me",
        "code_ref": "lily_forget",
        "tools": ["lily_forget_group", "lily_explain_memory"],
        "since": 1,
        "description": "'Lily, forget me' — deletes everything kept for the table",
        "prompt_marker": '"Lily, forget me"',
        "askable": True,
    },
    {
        "key": "group_memory",
        "code_ref": "lily_memory",
        "tools": ["lily_note_fact"],
        "since": 1,
        "description": "remembers returning tables — names, wins, facts, the usual",
        # Lives in MEMORY, HONESTY, AND FORGETTING, not the options block.
        "prompt_marker": None,
        "askable": False,
    },
    {
        "key": "voice_presets",
        "code_ref": "lily_voice_switch",
        "tools": ["lily_list_voices", "lily_switch_voice"],
        "since": 2,
        "description": "two voices — ask her to switch and she switches",
        "prompt_marker": "switch my voice",
        "askable": True,
    },
    {
        "key": "image_ingestion",
        "code_ref": "lily_vision",
        "tools": ["lily_analyze_image"],
        "since": 3,
        "description": (
            "share a photo through the chat's image button — she looks at "
            "it and reacts"
        ),
        "prompt_marker": "Show her things",
        "askable": True,
        # Grok vision rides XAI_API_KEY — fleet-standard; unset = she can
        # receive the photo but must honestly say she can't look tonight.
        "availability_key": "vision",
    },
]


# WO-LILY-CAPABILITY-LINT-001 — tools that are deliberately INVISIBLE to
# Lily's self-knowledge: plumbing, not player-facing features. Invisible
# by DECLARATION, not by omission — the bidirectional CI lint
# (tests/test_capability_lint.py) fails any registered tool that is
# neither mapped by a manifest entry's `tools` list nor named here.
LILY_INTERNAL_TOOLS: frozenset = frozenset({
    # Game orchestration — the LLM's control surface, not a table option.
    "lily_begin_round",
    # Intake/diarization plumbing (name binding is mechanics; the
    # player-facing surface is the intake protocol in the prompt).
    "lily_bind_speaker",
    # Adjudication bookkeeping (clarify-moment logging).
    "lily_log_clarify",
})


def lily_feature_version() -> int:
    """Current manifest version — max(since) so a forgotten bump of the
    constant can never under-report a shipped feature."""
    return max(
        [LILY_FEATURE_VERSION]
        + [int(entry.get("since", 1)) for entry in LILY_CAPABILITIES]
    )


def lily_whats_new(last_seen: Optional[int]) -> list:
    """Delta descriptions for a returning table stamped at `last_seen`.

    None (a returning table from before stamping existed) returns [] —
    we cannot know what they actually saw, and claiming "new since you
    last played" about features they may know would be its own small
    fabrication. Their stamp moves forward silently; real deltas start
    with their next lagged rematch.
    """
    if last_seen is None:
        return []
    try:
        seen = int(last_seen)
    except (TypeError, ValueError):
        return []
    return [
        entry["description"]
        for entry in LILY_CAPABILITIES
        if int(entry.get("since", 1)) > seen
    ]


def lily_availability_lines(flags: dict) -> list:
    """Plain-language availability notes for the state block.

    `flags` maps availability_key -> bool (True = switched on tonight).
    Only availability-bound features produce lines, and only honest ones:
    a feature she has but can't demonstrate right now is named as exactly
    that. An unknown key is treated as OFF — never overclaim.
    """
    lines = []
    for entry in LILY_CAPABILITIES:
        key = entry.get("availability_key")
        if not key:
            continue
        on = bool(flags.get(key, False))
        if on:
            continue
        partial = entry.get("availability_partial")
        if partial:
            lines.append(f"{entry['key']}: {partial} — tonight: OFF")
        else:
            lines.append(
                f"{entry['key']}: one of yours, but not switched on tonight"
            )
    return lines


def lily_askable_markers() -> list:
    """(key, prompt_marker) for every askable feature — the CI check that
    the prompt's options block and this manifest can never disagree."""
    return [
        (entry["key"], entry["prompt_marker"])
        for entry in LILY_CAPABILITIES
        if entry.get("askable") and entry.get("prompt_marker")
    ]


__all__ = [
    "LILY_CAPABILITIES",
    "LILY_FEATURE_VERSION",
    "lily_feature_version",
    "lily_whats_new",
    "lily_availability_lines",
    "lily_askable_markers",
]
