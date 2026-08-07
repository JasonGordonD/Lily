"""
lily_arsenal_content.py — the three partition CONTENT BRIEFS and the
spread planner (WO-LILY-ARSENAL-SEED-001 A3).

A picture bank fails in a way a text bank does not. Text questions can be
about anything and nobody notices the seams; pictures are LOOKED at, side
by side, ten in a night, and a bank with no authored spread announces
itself immediately. Six landmarks in a row and the table stops guessing
and starts pattern-matching the bank. Every image lit the same way and
cropped the same way and the round stops feeling made and starts feeling
generated. Every question a gimme and there is nothing to win.

So the spread is authored, not emergent. This module holds three things
the seeding job cannot invent for itself:

  SUBJECT AREAS  — what each partition is ABOUT, wide enough that fifty
                   entries do not repeat. The seeding plan walks these
                   round-robin, so the list is the anti-repetition axis.
  HOUSE STYLE    — how each partition LOOKS, appended to every generation
                   prompt so the rail reads as one deck rather than a
                   stock-photo grab bag.
  DIFFICULTY     — the tier weights, so not every question is a gimme and
                   not every question is a wall.

THREE PARTITIONS, THREE DIFFERENT JOBS. `general` plays at any table
including one with somebody's mother at it. `adult_suggestive` and
`adult_explicit` are gated adult content for consenting adults who chose
it at the table, and they are written with the confidence that implies —
an adult deck that flinches at its own subject matter is worse than no
adult deck, because it wastes the opt-in and embarrasses everyone.

THE FLOOR IS NOT A STYLE SETTING. Nothing involving minors. Nothing
non-consensual. Nothing outside legal hard limits. That holds at every
heat, it is not configurable, and it is enforced in three places
independently — here at authoring time, in
lily_arsenal_gen.lily_classifier_brief at gate time, and in the render
addenda at the provider. Every subject area below sits comfortably inside
it with room to spare; none of them are near it.

Stdlib only, no I/O, no supabase, no import of lily_arsenal_formats at
module level (the planner takes its formats as an argument, and resolves
binding direction through a lazy import) — the seeding job, the tests and
the operator's review script all import this with nothing configured.
"""

import logging
import math
from typing import Final, Optional

logger = logging.getLogger("lily_arsenal_content")

# Mirrors lily_arsenal.PARTITIONS deliberately — see the same note in
# lily_arsenal_formats. Importing lily_arsenal here would drag a supabase
# client into a module that is pure content data.
PARTITIONS: Final[tuple[str, ...]] = (
    "general", "adult_suggestive", "adult_explicit",
)

BINDING_IMAGE_FIRST: Final[str] = "image_first"
BINDING_QUESTION_FIRST: Final[str] = "question_first"


# ---------------------------------------------------------------------------
# Subject areas — the anti-repetition axis
# ---------------------------------------------------------------------------
#
# Eighteen per partition, and eighteen is a considered number rather than a
# round one. The seeding plan walks these round-robin, so the list length
# IS the repeat interval: at ten entries per partition (the standing depth
# in lily_arsenal.ARSENAL_TARGET_DEPTH) an eighteen-area list cannot repeat
# a subject at all, and a full night plus two replenishment cycles still
# will not come back to the same area twice.
#
# Areas are written as PROMPTABLE NOUNS — a phrase you could hand an image
# model directly. "tools" produces a tool; "the human condition" produces
# a mess. Each one also has to be answerable FROM A PICTURE, which quietly
# rules out a lot of otherwise good trivia territory: you cannot photograph
# a date, a law, or an idea.

SUBJECT_AREAS: dict[str, tuple[str, ...]] = {
    "general": (
        "objects",
        "tools",
        "places",
        "landmarks",
        "architecture",
        "animals",
        "nature",
        "weather",
        "food and drink",
        "vehicles",
        "art",
        "sport",
        "music and instruments",
        "everyday scenes",
        "visual puzzles",
        "flags and maps",
        "extreme close-ups",
        "signs and symbols",
    ),
    # Innuendo, romance, scandal, double-entendre, tasteful sensuality.
    # The heat here is in IMPLICATION — what the picture is clearly about
    # without showing it. History is doing a lot of the work on purpose:
    # the past is filthier than people expect, and "they really did that"
    # is a better laugh than anything invented.
    "adult_suggestive": (
        "innuendo",
        "double-entendre",
        "romance",
        "flirtation",
        "courtship customs",
        "historical scandal",
        "famous love affairs",
        "burlesque",
        "pin-up history",
        "risqué literature",
        "scandalous dances",
        "lingerie and foundation garments",
        "seduction on film",
        "cocktails and nightlife",
        "love letters and coded messages",
        "bedroom etiquette through history",
        "suggestive objects and euphemisms",
        "tasteful sensuality",
    ),
    # The top of the configured heat ladder, inside the structural floor.
    # Note what this list is and is not: it is a list of SUBJECTS a trivia
    # question can be asked about — named things, dated things, invented
    # things, argued-over things. Explicit does not mean formless. Every
    # area here still has to produce a question with one right answer, or
    # it is not trivia, it is a slideshow.
    "adult_explicit": (
        "adult toys",
        "kink implements",
        "kink vocabulary",
        "rope and restraint",
        "positions",
        "anatomy as trivia",
        "sex education facts",
        "aphrodisiacs and myths",
        "erotica classics",
        "ancient and classical erotica",
        "banned and censored works",
        "adult publishing history",
        "adult cinema history",
        "adult-industry history",
        "fetish subcultures",
        "safe words and negotiation",
        "contraception history",
        "slang through the centuries",
    ),
}


# ---------------------------------------------------------------------------
# House style — how each deck LOOKS
# ---------------------------------------------------------------------------
#
# Appended to every generation prompt by
# lily_arsenal_gen.lily_build_image_prompt, after the subject and the
# format composition. The point is coherence: ten images in a night that
# were clearly made for the same show.
#
# ===========================================================================
# OPERATOR DECISION — the general deck's look is NOT settled.
# ===========================================================================
# The 2026-08-07 session produced general-deck imagery the operator
# described as "cartoon", and nobody had ever written down what the general
# deck was supposed to look like — so that was not a bug, it was an unmade
# decision being made by a default. Two coherent answers exist and this
# module ships ONE of them:
#
#   A. CLEAN PHOTOGRAPHIC  (shipped below). Reads as a real object in a
#      real room. Best for identify / era_or_origin, where texture and
#      material ARE the evidence, and mandatory for real_or_imagined,
#      which is unanswerable if the deck does not look photographic.
#      Costs: harder for a model to keep clean, and a mediocre render
#      looks like a mistake rather than a style.
#
#   B. BOLD FLAT ILLUSTRATION — heavy outline, limited palette, flat
#      colour, poster-like. The named alternative, and the thing the
#      2026-08-07 output was drifting toward. It is more legible across a
#      room than photography, it is far more forgiving of a bad render,
#      and it visually distinguishes the general deck from the adult decks
#      (which are comic-illustrated upstream — though that cuts both ways,
#      since it also makes the two decks look like siblings). It CANNOT
#      support real_or_imagined at all.
#
# Recommendation: A. The general deck is the only one that can host
# real_or_imagined, and B forfeits that format permanently for a legibility
# gain the composition directives already deliver. Switching is one string.

HOUSE_STYLE: dict[str, str] = {
    "general": (
        "Clean editorial photograph: one subject, honest colour, natural "
        "directional light, real materials and real surface texture, with a "
        "plain or softly blurred background so the subject separates "
        "cleanly. Documentary realism — not stylised, not illustrated, not "
        "a 3D render, no cartoon outlines, no heavy filter, no vignette, no "
        "decorative border. It should look like a good photograph somebody "
        "took, and it has to read from the far side of a lit room."
    ),
    # ADULT DECKS: composition and framing ONLY.
    #
    # The art direction for both adult partitions is owned upstream by
    # lily_imagegen.LILY_ADULT_IMAGE_STYLE ("realistic comic-book
    # illustration ... never photorealistic"), which is applied at the wire
    # inside lily_generate_image_bytes for mode='adult'. Restating it here
    # would give the arsenal a second, drifting copy of the house look —
    # exactly the divergence the chokepoint exists to prevent — and
    # contradicting it here would produce a prompt arguing with itself.
    # So these strings do the job the chokepoint does NOT do: framing,
    # legibility, and the floor stated in the prompt itself.
    "adult_suggestive": (
        "Composition: one clear subject, centred, filling most of the "
        "frame, on an uncluttered background with nothing competing for the "
        "eye. Strong subject-to-background separation, no busy patterns, no "
        "small detail that has to be squinted at — this is read on a "
        "television from the far side of a room, not on a phone. Everyone "
        "depicted is an adult over twenty-five and a plainly willing, "
        "comfortable participant."
    ),
    "adult_explicit": (
        "Composition: one clear subject, centred, filling most of the "
        "frame, on an uncluttered background. The explicit element is the "
        "SUBJECT of the frame — lit, centred and unambiguous, with no coy "
        "cropping and no strategic obstruction; a picture question whose "
        "answer is hidden behind a conveniently placed object is not a "
        "question. Strong subject-to-background separation and no fine "
        "detail that has to be squinted at, because this is read on a "
        "television from across a room. Everyone depicted is an adult over "
        "twenty-five and a plainly willing, comfortable participant."
    ),
}


# ---------------------------------------------------------------------------
# Difficulty spread
# ---------------------------------------------------------------------------
#
# (tier, weight) pairs per partition, summing to 1.0. Tier 1 = easy warm-up,
# 2 = medium, 3 = hard.
#
# The shape is deliberate: tier 2 is the plurality everywhere, because a
# round is carried by questions that MOST of the table can nearly get. Tier
# 1s exist so the quiet player at the end of the sofa answers something out
# loud in the first five minutes, which is the entire reason they stay in
# the game. Tier 3s exist so winning means something — but they are the
# minority, because a picture question the table cannot crack produces
# silence, and silence on a picture round is worse than on a text round:
# the evidence is right there on the wall, so failing feels like a personal
# failing rather than a gap in knowledge.
#
# The adult partitions skew a notch easier than general, for two honest
# reasons. They are played later, with drink taken. And their subject
# matter is one most adults know by DOING rather than by studying — the
# pleasure in an adult question is the reveal and the laugh, not the
# difficulty, so a hard adult question spends the room's goodwill to buy
# something the deck was not selling. adult_explicit skews easiest of the
# three on the same logic, one step further.
#
# Weights are authored to sum to 1.0 and are normalised defensively by the
# planner anyway, so an operator editing them cannot silently unbalance the
# spread with binary float dust.

DIFFICULTY_SPREAD: dict[str, tuple[tuple[int, float], ...]] = {
    "general":          ((1, 0.30), (2, 0.45), (3, 0.25)),
    "adult_suggestive": ((1, 0.35), (2, 0.45), (3, 0.20)),
    "adult_explicit":   ((1, 0.40), (2, 0.45), (3, 0.15)),
}

# Length of the repeating tier pattern the planner walks. Twenty divides
# every weight above into a whole number of slots (6/9/5, 7/9/4, 8/9/3), so
# each full cycle realises the spread EXACTLY rather than approximately —
# which matters at these run sizes, where a ten-entry partition is half a
# cycle and rounding drift would be visible as a whole extra hard question.
TIER_CYCLE_LENGTH: Final[int] = 20


# ---------------------------------------------------------------------------
# The briefs
# ---------------------------------------------------------------------------
#
# `avoid` is the part that earns its keep. Every entry in these tuples is
# either a rule that keeps the partition inside its register, or a failure
# this stack has already had. They are handed to the authoring model as
# hard constraints.

PARTITION_BRIEFS: dict[str, dict] = {
    "general": {
        "summary": (
            "The deck that has to work at any table, including one with "
            "somebody's mother and somebody's teenager at it. Its job is "
            "range: an object, then a scene, then a building, then a "
            "decade, so nobody at the table is out of their depth for long "
            "and nobody is bored. The bar for a general question is that a "
            "player who knows nothing about the subject can still SEE the "
            "answer coming once it's said — that recognition is the whole "
            "pleasure of a picture round, and it is what separates this "
            "deck from a pub quiz with photographs stapled on."
        ),
        "subject_areas": SUBJECT_AREAS["general"],
        "difficulty_spread": DIFFICULTY_SPREAD["general"],
        "house_style": HOUSE_STYLE["general"],
        "avoid": (
            "No innuendo of any kind — a general table contains people who "
            "did not opt in, and the adult decks exist precisely so this "
            "one never has to guess.",
            "No gore, no injury, no medical trauma, no accident or disaster "
            "photography.",
            "No real, nameable people depicted. The generator invents "
            "faces; a plausible-but-wrong Churchill is a lie standing on "
            "the screen while Lily talks over it.",
            "No brand logos, packaging or trade dress that pins the image "
            "to a real company.",
            "No text, captions, labels or watermarks in the image — the "
            "answer must not be readable off the rail.",
            "No collages, grids or multi-panel layouts while odd_one_out is "
            "out of scope; one image, one subject.",
            "No question that needs a caption to make sense, and none whose "
            "answer depends on detail too small to see from the sofa.",
            "No question answerable without the picture — if the image is "
            "decoration, this is a text question wearing a costume.",
        ),
    },
    "adult_suggestive": {
        "summary": (
            "Gated adult content at the lower of two heats, and the heat is "
            "in the IMPLICATION — what the picture is unmistakably about "
            "without showing it. This is the register of the raised "
            "eyebrow: innuendo, romance, scandal, double-entendre, "
            "burlesque, and the reliable discovery that the past was far "
            "filthier than anyone was taught. It is written warm and "
            "confident, never sniggering, and never at the expense of "
            "anyone — the joke is always the world, and the world has "
            "supplied a great deal of material."
        ),
        "subject_areas": SUBJECT_AREAS["adult_suggestive"],
        "difficulty_spread": DIFFICULTY_SPREAD["adult_suggestive"],
        "house_style": HOUSE_STYLE["adult_suggestive"],
        "avoid": (
            "Nothing graphically explicit — that is the next rung up, and "
            "this table chose this one. Overshooting the chosen heat is the "
            "same failure as undershooting it.",
            "No anatomical detail as the subject; the suggestion is the "
            "content.",
            "Never about anyone in the room. The subject is always the "
            "world — no question that invites the table to speculate about "
            "each other.",
            "No real, nameable people DEPICTED. Historical figures may be "
            "named in the question; their faces are never generated.",
            "No art-style or medium direction in the generation prompt — "
            "lily_imagegen.LILY_ADULT_IMAGE_STYLE owns the look, and a "
            "prompt that argues with it produces a muddle.",
            "No shame-based jokes, no punching down, nothing that treats "
            "desire itself as the punchline.",
            "No text or captions in the image where they could carry the "
            "answer.",
            "No question answerable without the picture.",
        ),
    },
    "adult_explicit": {
        "summary": (
            "The top of the configured heat ladder: a gated adult deck for "
            "a table that explicitly opted up, and it is written like it. "
            "No euphemism, no coyness, no flinching at its own subject — a "
            "deck that hedges here has wasted the opt-in and made the "
            "moment awkward, which is the one thing it was hired not to do. "
            "What keeps it TRIVIA rather than a slideshow is that every "
            "entry still has to be a real question with one right answer: "
            "toys with inventors, acts with names, positions with "
            "etymologies, an industry with dates, and an enormous amount of "
            "genuinely surprising history."
        ),
        "subject_areas": SUBJECT_AREAS["adult_explicit"],
        "difficulty_spread": DIFFICULTY_SPREAD["adult_explicit"],
        "house_style": HOUSE_STYLE["adult_explicit"],
        "avoid": (
            "No euphemism and no coy phrasing. If the answer is a word, the "
            "question says the word.",
            "Never about anyone in the room, at any heat.",
            "No real, nameable people depicted; named in the question only.",
            "No art-style or medium direction in the generation prompt — "
            "the upstream chokepoint owns the look.",
            "No era_or_origin question whose period cue is the ART MEDIUM. "
            "The chokepoint fixes the medium globally, so an entry that "
            "needs to look like a woodblock print or a daguerreotype will "
            "render as a comic panel and lose its own answer. Period lives "
            "in dress, props, materials and setting.",
            "No shock content for its own sake — if there is no question "
            "with an answer, it does not belong in a bank.",
            "No text or captions in the image where they could carry the "
            "answer.",
            "No question answerable without the picture.",
        ),
    },
}


def lily_brief(partition: str) -> Optional[dict]:
    """The full authored brief for one partition, or None if unknown.

    None rather than a general-deck fallback: a partition name that did not
    resolve must not quietly seed an adult shelf with general content, or a
    general shelf with adult content. Both directions of that mistake are
    only discovered at a table."""
    if not partition:
        return None
    return PARTITION_BRIEFS.get(str(partition).strip().lower())


def lily_house_style(partition: str) -> str:
    """The visual style directive appended to this partition's generation
    prompts. Empty string for an unknown partition — the caller
    (lily_arsenal_gen.lily_build_image_prompt) drops empty parts, so an
    unknown partition loses its styling rather than gaining the wrong
    deck's."""
    if not partition:
        return ""
    return HOUSE_STYLE.get(str(partition).strip().lower(), "")


# ---------------------------------------------------------------------------
# The spread planner — what the seeding job walks
# ---------------------------------------------------------------------------
#
# DETERMINISM IS A FLEET RULE, not a preference: no Math.random, no
# time-seeded choice, anywhere. Everything below is index arithmetic on the
# absolute entry index, which buys three things at once —
#
#   a run is REPRODUCIBLE (the same start_index and count produce the same
#   plan, so a failed run can be re-run and a plan can be reviewed before
#   any money is spent on generation);
#   a top-up CONTINUES the spread instead of restarting it (pass
#   start_index = the count already banked, and entry 11 follows entry 10
#   rather than landing on subject area 1 again — re-running a seeding job
#   is the single most likely way to end up with a shelf of near-duplicates,
#   and this is what prevents it);
#   and the spread is INSPECTABLE — you can read the arithmetic and know
#   what the run will produce.


def _tier_cycle(partition: str) -> tuple[int, ...]:
    """The repeating tier pattern for one partition: TIER_CYCLE_LENGTH
    slots realising DIFFICULTY_SPREAD exactly, INTERLEAVED.

    Two steps, and the second one is the one that matters at a table.

    Counts come from largest-remainder apportionment (the same method used
    to allocate seats to vote shares), so the weights survive being turned
    into whole slots however the operator edits them.

    Then the slots are interleaved by fractional position rather than
    listed in blocks. A cycle of 1,1,1,1,1,1,2,2,2... is arithmetically
    perfect and a disaster to play: six warm-ups in a row while the table
    gets bored, then nine mediums in a row while they get tired. Placing
    each tier's k-th slot at (k + 0.5) / count and sorting spreads them
    evenly, so the run alternates the way a hand-built round does."""
    spread = DIFFICULTY_SPREAD.get((partition or "").strip().lower()) or ()
    total = sum(float(w) for _, w in spread if float(w) > 0)
    if not spread or total <= 0:
        # No spread configured — everything medium, which is the least
        # opinionated failure available.
        return (2,) * TIER_CYCLE_LENGTH

    exact = [(int(tier), TIER_CYCLE_LENGTH * float(w) / total) for tier, w in spread]
    counts = [[tier, int(share)] for tier, share in exact]
    # Largest remainder: hand the leftover slots to the biggest fractions
    # first, breaking ties by lower tier so the result is stable.
    leftover = TIER_CYCLE_LENGTH - sum(c for _, c in counts)
    order = sorted(
        range(len(exact)),
        key=lambda i: (-(exact[i][1] - int(exact[i][1])), exact[i][0]),
    )
    for k in range(max(0, leftover)):
        counts[order[k % len(order)]][1] += 1

    slots: list = []
    for tier, count in counts:
        if count <= 0:
            continue
        for k in range(count):
            slots.append(((k + 0.5) / count, tier))
    if not slots:
        return (2,) * TIER_CYCLE_LENGTH
    slots.sort(key=lambda s: (s[0], s[1]))
    return tuple(tier for _, tier in slots)


def _tier_rotation(n_areas: int, n_cycle: int) -> int:
    """How far to rotate the tier pattern after each full pass through the
    subject list.

    Without a rotation the tier index aliases against the subject list
    exactly the way the format index does: with 18 subject areas and a
    20-slot tier cycle they share a factor of 2, so a given subject area
    can only ever land on half the pattern — and half a pattern is not
    guaranteed to contain all three tiers. Measured, before this existed:
    every adult_suggestive question about lingerie was tier 1 or 2, and no
    subject in that partition ever saw all three tiers.

    Rotating by ROT each pass makes a fixed subject area walk the pattern
    in steps of (n_areas + ROT), so the condition for that subject to
    visit EVERY slot is gcd(n_areas + ROT, n_cycle) == 1. ROT starts at 2
    rather than 1 because ROT == 1 makes the tier index a copy of the
    format index's own offset expression, which would lock difficulty to
    format instead — the same bug wearing a different hat.

    Derived rather than hardcoded so the constant survives an operator
    adding a nineteenth subject area."""
    if n_areas <= 0 or n_cycle <= 1:
        return 1
    for rot in range(2, n_cycle + 2):
        if math.gcd(n_areas + rot, n_cycle) == 1 and math.gcd(rot, n_cycle) == 1:
            return rot
    return 1


def _binding_for(fmt: str) -> str:
    """Binding direction for a format, resolved through a LAZY import.

    lily_arsenal_formats is imported inside the function rather than at
    module scope so the two content modules stay independently importable
    in either order — the format taxonomy is free to grow a reference back
    to the briefs without this becoming a circular import. If the import
    fails for any reason the fallback is image_first, which is the
    direction that structurally cannot lie about correspondence: the safe
    default is the one that verifies itself."""
    try:
        import lily_arsenal_formats
        return lily_arsenal_formats.lily_binding_direction(fmt)
    except Exception as e:  # pragma: no cover — import-time breakage only
        logger.warning(
            "LILY_ARSENAL_CONTENT | FORMAT_SPEC_UNAVAILABLE | fmt=%r: %s "
            "— defaulting to %s", fmt, e, BINDING_IMAGE_FIRST,
        )
        return BINDING_IMAGE_FIRST


def lily_plan_entries(
    partition: str,
    count: int,
    *,
    formats: tuple,
    start_index: int = 0,
) -> list:
    """Plan `count` entries for one partition: the spread, before a penny
    of generation is spent.

    Returns a list of {subject_area, format, difficulty_tier,
    binding_direction} — one slot per entry, which
    lily_arsenal_gen.lily_generate_entry turns into an entry or an honest
    counted rejection.

    `formats` is passed in (from
    lily_arsenal_formats.lily_formats_for_partition, which has already
    applied the in-scope, real-image and per-partition filters) rather than
    read here, so this module never has to know which formats are switched
    on this week.

    `start_index` is the absolute index to resume from. Pass the number of
    entries the partition already holds and a top-up run CONTINUES the
    spread instead of restarting it — without this, every replenishment
    re-clusters on the same first few subject areas and the shelf slowly
    fills up with variations of the same six questions.

    THE ARITHMETIC. For absolute index n:

        subject = areas[n mod A]
        format  = formats[(n + n div A) mod F]
        tier    = cycle[(n + ROT * (n div A)) mod T]

    The `n div A` terms are the load-bearing part. Plain `n mod A` and
    `n mod F` walk in lockstep whenever A and F share a factor: with 18
    subject areas and 6 formats, subject area 0 would draw format 0
    forever and you would ship a bank where every question about tools is
    an identify. Advancing by one extra step after each full pass through
    the subjects breaks that alias for ANY A and F, so every subject
    eventually meets every format; ROT (see _tier_rotation) does the same
    job for difficulty, with a different stride so tier does not simply
    inherit format's aliasing instead.

    Note what is NOT done here: the tier index is never allowed to SKIP
    slots. An earlier version offset it by `n div F`, which advanced the
    index by two on every F-th entry and quietly dropped one slot in
    every five — the authored 35/45/20 spread came out of the planner as
    31/44/25. Rotating preserves every slot and therefore the weights;
    skipping does not. That is why this is a rotation and not an offset.

    Never raises. An unknown partition or an empty format list returns []
    loudly — a seeding job that plans nothing is visible in a run summary,
    while one that quietly plans the wrong deck is not."""
    key = (partition or "").strip().lower()
    if key not in PARTITIONS:
        logger.warning(
            "LILY_ARSENAL_CONTENT | UNKNOWN_PARTITION | partition=%r — "
            "planned 0 entries", partition,
        )
        return []
    try:
        n_count = int(count)
    except (TypeError, ValueError):
        n_count = 0
    if n_count <= 0:
        return []
    try:
        base = max(0, int(start_index))
    except (TypeError, ValueError):
        base = 0

    areas = SUBJECT_AREAS.get(key) or ()
    if not areas:
        logger.warning(
            "LILY_ARSENAL_CONTENT | NO_SUBJECT_AREAS | partition=%s — "
            "planned 0 entries", key,
        )
        return []

    fmts = tuple(str(f).strip() for f in (formats or ()) if str(f).strip())
    if not fmts:
        # No substitute is offered on purpose: silently swapping in a
        # default format would seed a shelf the operator did not ask for
        # and did not review.
        logger.warning(
            "LILY_ARSENAL_CONTENT | NO_FORMATS | partition=%s — planned 0 "
            "entries (caller passed no in-scope formats)", key,
        )
        return []

    cycle = _tier_cycle(key)
    n_areas, n_formats, n_cycle = len(areas), len(fmts), len(cycle)
    rot = _tier_rotation(n_areas, n_cycle)

    # One binding lookup per distinct format, not one per entry.
    bindings = {fmt: _binding_for(fmt) for fmt in set(fmts)}

    plan: list = []
    for i in range(n_count):
        n = base + i
        passes = n // n_areas  # completed laps of the subject list
        fmt = fmts[(n + passes) % n_formats]
        plan.append({
            "subject_area": areas[n % n_areas],
            "format": fmt,
            "difficulty_tier": cycle[(n + rot * passes) % n_cycle],
            "binding_direction": bindings[fmt],
        })

    logger.info(
        "LILY_ARSENAL_CONTENT | PLANNED | partition=%s count=%d "
        "start_index=%d subjects=%d formats=%d",
        key, len(plan), base, len({p["subject_area"] for p in plan}),
        len({p["format"] for p in plan}),
    )
    return plan
