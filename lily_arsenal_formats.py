"""
lily_arsenal_formats.py — the picture-question FORMAT taxonomy
(WO-LILY-ARSENAL-SEED-001 A2, carrying the A1 binding-direction record).

"Picture trivia" is not one shape, and a bank that only knows one shape
stops being a game about ten minutes in. The first pictures round Lily
ever ran was six consecutive "what is this" questions, and the table
worked out the rhythm before the third one landed: look, shout, wait,
repeat. The content was fine. The SHAPE was the problem.

So the shape is data. Every arsenal entry carries a `format` tag
(migration 022), and this module is what that tag means: how the question
is bound to its image, how the answer is given, how Lily SAYS it, and
what the rail line is that puts the picture on the screen. A round can
then deliberately mix — identify, then read-the-scene, then four spoken
options, then date-it — and the table never settles into a rhythm.

TWO FINDINGS ARE HARDCODED HERE, both from live sessions:

  SPOKEN OPTIONS, NEVER LETTERS. Multiple choice on a screen is A/B/C/D.
  Multiple choice in a ROOM is not: "B" and "D" and "E" and "P" and "T"
  are the same sound across a table with music on, and the answer
  resolver has to guess which one a player meant. Options are therefore
  spoken as WORDS and authored to be PHONETICALLY DISTINCT from each
  other — "a flying buttress" and "a moat" cannot be confused at volume;
  "B" and "D" can. lily_evaluation still accepts a letter or a position
  if a player offers one, but the bank never asks for one.

  A FORMAT THE HOST CANNOT EXPLAIN IS NOT A FORMAT. On 2026-08-07 Lily
  improvised a real-or-imagined round live, and when the table asked what
  the rules were she could not say — because there was no authored rule
  line, only a prompt that assumed everyone already understood. Every
  format here therefore owns a `spoken_template` that states its own
  rules in the line that asks the question. real_or_imagined states them
  outright; odd_one_out names the four panel positions out loud.

BINDING DIRECTION (A1) is recorded per format, and per entry, because
correspondence failures — the picture not showing what the question says
it shows — CLUSTER BY DIRECTION, and you cannot see the cluster if you
did not write down which way each entry went.

  image_first    the image is generated first and the question is written
                 about what is actually in it. Correspondence is nearly
                 free: the question can only claim what the author saw.
  question_first the question is written first and the image is generated
                 to complete it. Riskier — the stem makes a claim before
                 any pixels exist — so lily_arsenal_gen sends this path
                 through the classifier for a correspondence check before
                 the entry can be banked.

FORMAT_SPECS carries each format's DEFAULT direction. The per-entry value
is authoritative and may deviate when a specific entry demands it (see
the fan-flirtation exemplar); a deviation toward question_first just buys
the mandatory verification.

Pure data + pure functions. Stdlib only, no I/O, no provider calls, no
supabase — the seeding job, the tests and a human reading the bank over
the operator's shoulder all import this without credentials.
"""

import logging
from typing import Final, Optional

logger = logging.getLogger("lily_arsenal_formats")

# Mirrors lily_arsenal.PARTITIONS deliberately rather than importing it.
# lily_arsenal pulls in lily_bank and a supabase client; this module is
# content data that a test, a review script or the seeding planner must be
# able to import with nothing configured. Three strings are a cheaper
# duplication than a dependency.
PARTITIONS: Final[tuple[str, ...]] = (
    "general", "adult_suggestive", "adult_explicit",
)

# Binding-direction vocabulary. Values match lily_arsenal's constants and
# the migration-022 check constraint; lily_arsenal_gen imports them from
# HERE, so they are part of this module's contract.
BINDING_IMAGE_FIRST: Final[str] = "image_first"
BINDING_QUESTION_FIRST: Final[str] = "question_first"

# Answer styles. 'freeform' goes to the Tier-1 fuzzy/phonetic matcher;
# 'multiple_choice' goes to the option resolver in lily_evaluation.
ANSWER_FREEFORM: Final[str] = "freeform"
ANSWER_MULTIPLE_CHOICE: Final[str] = "multiple_choice"

FORMATS: Final[tuple[str, ...]] = (
    "identify",
    "whats_happening",
    "multiple_choice",
    "real_or_imagined",
    "odd_one_out",
    "era_or_origin",
)

# The number of options a multiple_choice entry carries. Four, always —
# lily_reasoning.lily_valid_choices rejects anything else, and three is
# too guessable while five is too much to hold in your head after a drink.
MC_OPTION_COUNT: Final[int] = 4

# Formats a partition may NOT use, regardless of in_scope. One dict, one
# line to override.
#
# real_or_imagined is excluded from both adult partitions, and the reason
# is structural rather than editorial: adult imagery renders through
# lily_imagegen.LILY_ADULT_IMAGE_STYLE, which pins the adult decks to
# "realistic comic-book illustration ... never photorealistic". A round
# that asks "is this a real photograph?" about an image that is visibly a
# painting has already given away its own answer. The format needs a
# photographic generator, and the adult decks do not have one.
# The operator can re-enable it by emptying the tuple — but the honest
# fix is a photographic path for the adult ROI half, which is a separate
# work order. Exemplars for the adult ROI pairs are authored below so the
# shape can be judged before that call is made.
PARTITION_FORMAT_EXCLUSIONS: Final[dict[str, tuple[str, ...]]] = {
    "adult_suggestive": ("real_or_imagined",),
    "adult_explicit": ("real_or_imagined",),
}


# ---------------------------------------------------------------------------
# The specs
# ---------------------------------------------------------------------------
#
# `spoken_template` placeholders: {stem} is the entry's question_text,
# {options} is the four options rendered as a spoken list. A template with
# no placeholder other than {stem} means the authored question IS the
# spoken line — which is true of the freeform formats and deliberately so:
# a template that wraps every identify question in the same preamble is
# how a round starts sounding like a form letter.
#
# `image_composition` is the format's framing instruction, consumed by
# lily_arsenal_gen.lily_build_image_prompt between the difficulty framing
# and the partition house style.

FORMAT_SPECS: dict[str, dict] = {
    "identify": {
        "key": "identify",
        "label": "Identify it",
        "description": (
            "One object, place or thing on the screen; the table names it. "
            "The backbone format — cheap to generate, instantly legible, "
            "and the one that carries a round when the others are being "
            "clever. Its failure mode is monotony, which is the reason "
            "every other format in this file exists."
        ),
        # IMAGE-FIRST. The safest direction there is: the image renders,
        # the author looks at it, and the question can only ask about what
        # is actually there. A question-first identify ("name this
        # sextant") is the classic correspondence lie — the generator
        # paints a vague brass instrument, the stem insists it is a
        # sextant, and Lily reveals an answer the table can see is wrong.
        "binding_direction": BINDING_IMAGE_FIRST,
        "answer_style": ANSWER_FREEFORM,
        "spoken_template": "{stem}",
        "image_intro": "Eyes on the screen — no shouting till you're sure.",
        "image_composition": (
            "One subject, centred, filling most of the frame. Nothing else "
            "in shot that could be mistaken for the answer."
        ),
        "requires_real_images": False,
        "in_scope": True,
    },
    "whats_happening": {
        "key": "whats_happening",
        "label": "What's happening",
        "description": (
            "A scene rather than an object: the table reads it and names "
            "the situation, the act or the concept. Answers are nouns for "
            "things that are happening — 'a mirage', 'shibari', 'a fan "
            "dance' — never descriptions, or two tables will describe it "
            "two defensible ways and the adjudication becomes an argument."
        ),
        # IMAGE-FIRST. The scene is whatever the model actually painted;
        # you read it afterwards. Asserting a situation before generation
        # ("two people arguing about a parking space") and then hoping the
        # render agrees is how you get a question about a scene that is
        # not on the screen.
        "binding_direction": BINDING_IMAGE_FIRST,
        "answer_style": ANSWER_FREEFORM,
        "spoken_template": "{stem}",
        "image_intro": "Look at what's happening on the screen.",
        "image_composition": (
            "A whole scene, wide enough that the situation reads at a "
            "glance, with the action at the centre of the frame."
        ),
        "requires_real_images": False,
        "in_scope": True,
    },
    "multiple_choice": {
        "key": "multiple_choice",
        "label": "Four options",
        "description": (
            "An image plus four spoken options. The options are WORDS, "
            "never letters: 'A' and 'B' and 'D' are one sound across a "
            "noisy table, and a bank that asks for letters is asking the "
            "answer resolver to guess. The four must also be phonetically "
            "distinct FROM EACH OTHER — two options that rhyme are one "
            "option with extra steps. Standard pub construction: the "
            "answer, two genuinely arguable distractors, and one that is "
            "comically wrong but said with a straight face."
        ),
        # IMAGE-FIRST, and more strictly than the freeform formats. The
        # correct option has to be the thing that is actually on the
        # screen. A question-first MC whose image drifted makes the
        # "correct" option wrong — the worst failure the bank can produce,
        # because four options make the table commit out loud before Lily
        # reveals that the picture disagrees with all of them.
        "binding_direction": BINDING_IMAGE_FIRST,
        "answer_style": ANSWER_MULTIPLE_CHOICE,
        "spoken_template": "{stem} Is it {options}?",
        "image_intro": "On the screen — and I'll give you four.",
        "image_composition": (
            "One unambiguous subject, centred and well lit, showing the "
            "detail the options turn on."
        ),
        "requires_real_images": False,
        "in_scope": True,
    },
    "real_or_imagined": {
        "key": "real_or_imagined",
        "label": "Real or imagined",
        "description": (
            "Is this a photograph somebody took, or a machine's invention? "
            "THE FORMAT ONLY WORKS IF THE BANK HOLDS BOTH. A shelf of "
            "generated fakes labelled real-or-imagined is a shelf where "
            "the answer is always 'imagined', and a table works that out "
            "inside two questions. The real half is web-sourced through "
            "lily_search (Exa, safelisted hosts) and NEVER generated; the "
            "imagined half is generated and its prompt says outright that "
            "the subject exists nowhere. Gated on "
            "lily_config.arsenal_real_images_enabled(). Excluded from the "
            "adult partitions — see PARTITION_FORMAT_EXCLUSIONS."
        ),
        # QUESTION-FIRST, and it is the one format where that direction
        # carries no correspondence risk at all. The question makes no
        # claim about CONTENT — only about PROVENANCE, and the pipeline
        # knows the provenance for certain before a single pixel exists:
        # this slot is a sourced photograph, or this slot is generated.
        # There is nothing for a classifier to disagree with.
        "binding_direction": BINDING_QUESTION_FIRST,
        "answer_style": ANSWER_FREEFORM,
        # The rule is IN the question. This is the 2026-08-07 fix: the
        # format is stated every time it is played, in one breath, so a
        # table that has never met it — or has just met three other
        # shapes — knows what it is being asked before it answers. The
        # delivery layer may drop the rule clause on a second consecutive
        # real_or_imagined; it must never drop it on the first.
        "spoken_template": (
            "Here's the rule: every picture in this round is either a real "
            "photograph somebody actually took, or a machine's invention — "
            "nothing in between. {stem} Real, or imagined?"
        ),
        "image_intro": "One picture. One question.",
        "image_composition": (
            "Plausible amateur photography: casual handheld framing, "
            "available light, the ordinary imperfections of a real snapshot."
        ),
        "requires_real_images": True,
        "in_scope": True,
    },
    "odd_one_out": {
        "key": "odd_one_out",
        "label": "Odd one out",
        "description": (
            "A four-panel image where three panels share a rule and one "
            "breaks it. The table names the panel by POSITION — top left, "
            "top right, bottom left, bottom right — because position words "
            "survive a noisy room and panel letters do not."
        ),
        # QUESTION-FIRST by necessity. The rule ("three tools of the same
        # trade, one is not") and the identity of the breaker have to be
        # decided before generation; you cannot reverse-engineer a clean
        # rule out of four panels that were drawn without one. That makes
        # this the only format that is question-first AND multi-subject:
        # two risk multipliers stacked.
        "binding_direction": BINDING_QUESTION_FIRST,
        "answer_style": ANSWER_FREEFORM,
        "spoken_template": (
            "Four panels on the screen. Three of them belong together and "
            "one is a liar. {stem} Which one's the odd one out — top left, "
            "top right, bottom left, or bottom right?"
        ),
        "image_intro": "Four at once. Take your time with this one.",
        "image_composition": (
            "A clean two-by-two grid of four separate panels, equal size, "
            "thin neutral gutters, one clearly-readable subject per panel "
            "and no panel visually crowding another."
        ),
        "requires_real_images": False,
        # OUT OF SCOPE for the seed run. This is a judgment call, and it is
        # about the generator, not the format — the format is good.
        #
        # Single-shot image models do not reliably produce a 2x2 grid with
        # four distinct, individually legible subjects AND exactly one
        # rule-breaker. They produce four near-duplicates, or three panels
        # and a smear, or four panels that all sort of belong. And the
        # failure is SILENT: nothing downstream can tell "four tools, one
        # is a kitchen utensil" from "four tools", so the entry banks
        # clean, serves live, and Lily confidently reveals an answer the
        # table can disprove by looking at the screen. That is the one
        # failure mode a picture round cannot survive, because the
        # evidence is on the wall while she is being wrong about it.
        #
        # The real fix is to generate four images and compose the grid
        # locally — which needs an image library this repo does not carry
        # and will not add for a seeding job. Flip this to True when that
        # composition path lands, and author six exemplars (one per
        # partition, plus the preview below) BEFORE the first bulk run.
        "in_scope": False,
    },
    "era_or_origin": {
        "key": "era_or_origin",
        "label": "Date it or place it",
        "description": (
            "When is this from, or where is it from. Pin each entry to ONE "
            "axis — a question that accepts either a decade or a country "
            "accepts two right answers, which means it has none. Decades "
            "read better out loud than years, and a decade is a fair ask "
            "for a table that is guessing from a picture."
        ),
        # QUESTION-FIRST, and this is the direction where the risk is real
        # and taken deliberately. An image-first era question has NO
        # ground truth: you generate a kitchen, look at it, decide it
        # feels like the seventies, and now the canonical answer is the
        # author's impression of a render. Question-first at least makes
        # the era the PROMPT — you asked for 1973, so 1973 is the answer —
        # and turns verification into a checkable question the classifier
        # can actually answer: did the period cues land?
        #
        # Adult partitions: the period cue must live in DRESS, PROPS and
        # SETTING, never in the art medium. lily_imagegen's adult
        # chokepoint fixes the medium globally, so an entry whose era
        # depends on looking like a woodblock print or a daguerreotype
        # will render as a comic panel and lose its own answer.
        "binding_direction": BINDING_QUESTION_FIRST,
        "answer_style": ANSWER_FREEFORM,
        "spoken_template": "{stem}",
        "image_intro": "On the screen. Date it, or place it.",
        "image_composition": (
            "Period detail carried by dress, props, materials and setting, "
            "with several dateable objects visible in one clean frame."
        ),
        "requires_real_images": False,
        # A GENERATED image cannot honestly answer "what decade is this?" — a
        # render only ever carries an IMPRESSION of a period, and the
        # correspondence gate correctly refuses it ("pin-up styling reads
        # 1940s not 1890s", "impossible to identify a specific decade"). A
        # genuinely-dated photograph carries authentic period cues, so when a
        # real-image source is wired (EXA, safelisted archival hosts) this
        # format SOURCES its image instead of generating one. The ground
        # truth (the decade) comes from curation, not from reading a render.
        # Partition-agnostic: general and adult_suggestive both benefit.
        # When no real-image source is wired it falls back to the generated
        # path unchanged (which the gate will still refuse — the pre-fix
        # ceiling, never worse).
        "sources_real_image": True,
        "in_scope": True,
    },
}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def lily_format_spec(fmt: str) -> Optional[dict]:
    """The spec for one format key, or None for an unknown key.

    None rather than a raise or a default: an unknown format arrives from
    a database row or an operator's config, and neither should be able to
    take the seeding job down. Callers fall back to the identify shape."""
    if not fmt:
        return None
    return FORMAT_SPECS.get(str(fmt).strip().lower())


def lily_binding_direction(fmt: str) -> str:
    """The default binding direction for a format.

    Unknown formats default to image_first, and that default is the safe
    one on purpose: image-first is the direction that structurally cannot
    lie about correspondence, so an entry whose format tag got mangled
    still lands on the path that verifies itself for free."""
    spec = lily_format_spec(fmt)
    if spec is None:
        return BINDING_IMAGE_FIRST
    direction = str(spec.get("binding_direction") or BINDING_IMAGE_FIRST)
    if direction not in (BINDING_IMAGE_FIRST, BINDING_QUESTION_FIRST):
        return BINDING_IMAGE_FIRST
    return direction


def lily_format_sources_real_image(fmt: str) -> bool:
    """True when this format should SOURCE a genuinely-dated real image (via
    the wired EXA path) rather than generate one.

    Distinct from requires_real_images (which GATES a format's availability —
    real_or_imagined cannot exist without a real half): a sources_real_image
    format is always in scope and simply PREFERS a real image when one is
    wired, degrading to the generated path when it is not. era_or_origin is
    the first such format; the answer's ground truth comes from curation, not
    from reading the render, which is why a real archival photo passes the
    correspondence gate a generated period-impression cannot."""
    spec = lily_format_spec(fmt)
    return bool(spec and spec.get("sources_real_image"))


def lily_formats_for_partition(
    partition: str, *, real_images_available: bool = False
) -> tuple[str, ...]:
    """In-scope formats this partition may actually be seeded with.

    Three filters, in order:
      1. in_scope — odd_one_out is off until the grid-composition path
         exists (see its spec comment).
      2. requires_real_images — real_or_imagined cannot be honestly built
         from generated images alone, so with no real-image source it is
         excluded rather than half-built into a shelf whose answer is
         always 'imagined'.
      3. PARTITION_FORMAT_EXCLUSIONS — per-partition structural conflicts.

    Returns () for an unknown partition, and says so in the log: a typo'd
    partition name seeding nothing loudly beats it seeding the wrong deck
    quietly."""
    key = (partition or "").strip().lower()
    if key not in PARTITIONS:
        logger.warning(
            "LILY_ARSENAL_FORMATS | UNKNOWN_PARTITION | partition=%r — no "
            "formats offered", partition,
        )
        return ()
    excluded = PARTITION_FORMAT_EXCLUSIONS.get(key, ())
    return tuple(
        fmt for fmt in FORMATS
        if FORMAT_SPECS[fmt]["in_scope"]
        and not (FORMAT_SPECS[fmt]["requires_real_images"] and not real_images_available)
        and fmt not in excluded
    )


# ---------------------------------------------------------------------------
# Rendering the spoken line
# ---------------------------------------------------------------------------


def _spoken_options(options) -> str:
    """Four options as ONE spoken list: 'a, b, c, or d'.

    No letters, no numbers, no 'option one' — the words themselves are the
    handles. lily_evaluation will still resolve a letter or a position if
    a player volunteers one; the bank simply never puts that idea in their
    head, because a letter a player invents is a letter they chose to say
    clearly."""
    items = [str(o).strip() for o in (options or []) if str(o).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])}, or {items[-1]}"


def lily_spoken_question(entry: dict) -> str:
    """Render the line Lily actually says for one entry.

    Substitution is done with str.replace rather than str.format on
    purpose: authored question text contains apostrophes, dashes and
    occasionally braces, and a KeyError or ValueError thrown while
    building a spoken line would surface as silence at a live table. A
    template missing a placeholder simply keeps its own words."""
    if not isinstance(entry, dict):
        return ""
    stem = str(entry.get("question_text") or "").strip()
    spec = lily_format_spec(str(entry.get("format") or ""))
    if spec is None:
        # Unknown format tag: the authored question is still a real
        # question, so say it rather than dropping the entry.
        return stem
    options = entry.get("options")
    if spec["answer_style"] == ANSWER_MULTIPLE_CHOICE:
        if not isinstance(options, list) or len(options) != MC_OPTION_COUNT:
            # A multiple-choice entry with no usable options degrades to a
            # freeform read of its own stem — which is a fair question,
            # just an easier one. Loud, because it means the entry was
            # banked malformed.
            logger.warning(
                "LILY_ARSENAL_FORMATS | MC_OPTIONS_MISSING | expected=%d got=%r "
                "— reading as freeform", MC_OPTION_COUNT, options,
            )
            return stem
    rendered = (
        str(spec["spoken_template"])
        .replace("{stem}", stem)
        .replace("{options}", _spoken_options(options))
    )
    return " ".join(rendered.split())


# ---------------------------------------------------------------------------
# EXEMPLARS — the bar for the whole bank
# ---------------------------------------------------------------------------
#
# One hand-built entry per (partition, in-scope format): 3 x 5 = 15. These
# are what the operator signs off on before anything is generated in bulk,
# and what the authoring prompt is shown as few-shot examples. Everything
# here is meant to be the standard, not the floor.
#
# What each one is trying to demonstrate:
#   - the question is answerable FROM THE IMAGE, not from general
#     knowledge the picture happens to accompany;
#   - it is pinned to exactly one defensible answer;
#   - acceptable_answers carries the manglings a recogniser will actually
#     produce in a loud room ("mirage" comes back as "marriage", "pasties"
#     comes back as "pastries", every single time);
#   - reveal_color is a PERFORMANCE, not a footnote — one fact the table
#     did not have, delivered with a point of view;
#   - generation_prompt is the real prompt, ready to run.
#
# Adult prompts describe SUBJECT and COMPOSITION only. Art direction is
# owned by lily_imagegen.LILY_ADULT_IMAGE_STYLE at the wire; a second copy
# here would be the divergent house look the work order forbids. Every
# person in an adult prompt is written as an explicit adult, and the
# scenes are written as plainly willing — that is the structural floor
# doing its job at authoring time, before any classifier sees the pixels.

EXEMPLARS: tuple[dict, ...] = (

    # -- general ------------------------------------------------------------

    {
        "partition": "general",
        "format": "identify",
        "binding_direction": BINDING_IMAGE_FIRST,
        "subject_area": "tools",
        "difficulty_tier": 2,
        "question_text": (
            "Brass, hinged, a swing arm and one small mirror. A ship's "
            "officer pointed this at the sun every day at noon and it told "
            "him where in the world he was. What is it?"
        ),
        "canonical_answer": "a sextant",
        "acceptable_answers": [
            "sextant", "a sextant", "sextants",
            # What a recogniser hands back from a loud room.
            "sex tent", "six tent", "sextent", "sexton", "sextet",
        ],
        "options": None,
        "reveal_color": (
            "A sextant. Two centuries of crossing oceans with a mirror and "
            "some very careful arithmetic — and you three couldn't find the "
            "bathroom in this house."
        ),
        "generation_prompt": (
            "A brass marine sextant resting on a weathered wooden chart "
            "table, index arm and small horizon mirror clearly visible, "
            "soft directional window light from the left, the instrument "
            "centred and filling most of the frame, plain uncluttered "
            "background."
        ),
    },
    {
        "partition": "general",
        "format": "whats_happening",
        "binding_direction": BINDING_IMAGE_FIRST,
        "subject_area": "weather",
        "difficulty_tier": 2,
        "question_text": (
            "That road is bone dry. It hasn't rained there in a month. So "
            "what's the puddle?"
        ),
        "canonical_answer": "a mirage",
        "acceptable_answers": [
            "mirage", "a mirage", "the mirage", "heat mirage", "heat haze",
            "heat shimmer",
            # The recogniser's favourite: 'mirage' comes back as 'marriage'
            # more often than it comes back correct.
            "marriage", "a marriage", "mirages", "mir age",
        ],
        "options": None,
        "reveal_color": (
            "A mirage. Hot air just above the tarmac bends the light coming "
            "down out of the sky, so what you're looking at is a puddle of "
            "sky. Your eyes were working perfectly — the atmosphere lied to "
            "you."
        ),
        "generation_prompt": (
            "A long straight desert highway photographed at midday, the far "
            "end of the road dissolving into a false silvery pool of light, "
            "visible heat distortion rippling above the asphalt, clear pale "
            "sky, no vehicles and no people, single vanishing-point "
            "composition, the shimmer at the centre of the frame."
        ),
    },
    {
        "partition": "general",
        "format": "multiple_choice",
        "binding_direction": BINDING_IMAGE_FIRST,
        "subject_area": "architecture",
        "difficulty_tier": 3,
        "question_text": (
            "Those stone arms on the side of the cathedral, leaning in and "
            "holding the wall up from outside. What are they called?"
        ),
        "canonical_answer": "a flying buttress",
        "acceptable_answers": [
            "flying buttress", "a flying buttress", "flying buttresses",
            "buttress", "buttresses",
            "flying butt rest", "flying buttrice", "flying butteress",
        ],
        # Phonetically distinct across all four, and not a letter in sight.
        # 'a moat' is the straight-faced laugh option: same building, same
        # century, wrong end of it entirely.
        "options": [
            "a flying buttress",
            "a rose window",
            "a bell gable",
            "a moat",
        ],
        "reveal_color": (
            "A flying buttress. Medieval engineers worked out they could "
            "hold the wall up from OUTSIDE, which meant the wall no longer "
            "had to be a wall — and that is the only reason cathedrals have "
            "windows the size of houses. First people in history to say "
            "'trust me, it'll hold' and be right."
        ),
        "generation_prompt": (
            "The exterior flank of a Gothic stone cathedral, a row of "
            "flying buttresses arcing from the nave wall down to outer "
            "piers, late-afternoon sun raking across the stonework so the "
            "arches read clearly against shadow, no people, clean "
            "single-subject framing."
        ),
    },
    {
        # The IMAGINED half. Its partner is the same question_text with
        # canonical_answer 'real', no generation_prompt, and an image
        # sourced through lily_search — a real-or-imagined shelf without
        # both halves is a shelf whose answer is always 'imagined'.
        "partition": "general",
        "format": "real_or_imagined",
        "binding_direction": BINDING_QUESTION_FIRST,
        "subject_area": "landmarks",
        "difficulty_tier": 1,
        "question_text": "Look at the bridge in the middle of that photograph.",
        "canonical_answer": "imagined",
        "acceptable_answers": [
            "imagined", "imagine", "fake", "a fake", "generated",
            "ai", "ai generated", "made up", "not real", "imaginary",
            "invented", "it's fake", "that's fake",
        ],
        "options": None,
        "reveal_color": (
            "Imagined — and look where it goes. That bridge crosses the "
            "river, thinks about it, and politely puts you back on the bank "
            "you started from. Somebody's public works budget, beautifully "
            "spent."
        ),
        "generation_prompt": (
            "A plausible tourist photograph of a wide stone footbridge that "
            "crosses a river and then curves all the way back to the same "
            "bank it started from, a few people walking along it, flat "
            "overcast light, casual handheld phone-photo framing with the "
            "horizon slightly off level. The bridge, the river and the town "
            "are invented — they exist nowhere."
        ),
    },
    {
        "partition": "general",
        "format": "era_or_origin",
        "binding_direction": BINDING_QUESTION_FIRST,
        "subject_area": "everyday scenes",
        "difficulty_tier": 2,
        "question_text": (
            "That kitchen. The colour of the fridge, the countertop, the "
            "thing on the wall with the cord. Which decade are we standing "
            "in?"
        ),
        "canonical_answer": "the 1970s",
        "acceptable_answers": [
            "1970s", "the 1970s", "70s", "the 70s", "seventies",
            "the seventies", "nineteen seventies", "1970", "1975",
            "nineteen seventy", "19 70s",
        ],
        "options": None,
        "reveal_color": (
            "The seventies. Avocado green, harvest gold, and a wall "
            "telephone with enough cord to reach the next house. That "
            "colour scheme survived exactly one decade and was then hunted "
            "to extinction by people who had to live with it."
        ),
        "generation_prompt": (
            "A 1970s American suburban kitchen: avocado-green refrigerator "
            "and matching wall oven, harvest-gold laminate countertops, "
            "dark wood-grain cabinet fronts, a mustard-yellow wall "
            "telephone with a long coiled cord, patterned linoleum floor, "
            "warm afternoon light through gingham curtains. Period-accurate "
            "throughout — no modern appliances, no flat screens, no "
            "stainless steel."
        ),
    },

    # -- adult_suggestive ---------------------------------------------------

    {
        "partition": "adult_suggestive",
        "format": "identify",
        "binding_direction": BINDING_IMAGE_FIRST,
        "subject_area": "burlesque",
        "difficulty_tier": 2,
        "question_text": (
            "On the screen: the two small sequinned discs with the tassels, "
            "the ones a burlesque dancer spins at the end of the number. "
            "One word — what are they called?"
        ),
        "canonical_answer": "pasties",
        "acceptable_answers": [
            "pasties", "pasty", "nipple pasties", "burlesque pasties",
            # The recogniser hears the bakery item essentially always.
            "pastries", "pastry", "pastes", "paisties", "past ease",
        ],
        "options": None,
        "reveal_color": (
            "Pasties. And yes, the recogniser hears 'pastries' every single "
            "time, which is a very different evening. Spinning them in "
            "opposite directions at once is a genuine trained skill — there "
            "are competitions, there are judges, and you would lose."
        ),
        "generation_prompt": (
            "A pair of ornate sequinned burlesque pasties with long silk "
            "tassels laid out on a velvet dressing-table beside a powder "
            "puff, lit by a single warm vanity bulb, a performer's "
            "out-of-focus silhouette reflected in the mirror behind. The "
            "pasties centred and filling most of the frame, everything "
            "else soft and secondary."
        ),
    },
    {
        # DELIBERATE DEVIATION from the format default (whats_happening is
        # image_first). This entry's whole answer is a specific GESTURE —
        # fan drawn slowly across the cheek. Generate a ballroom first and
        # you get a ballroom; the gesture arrives only if you ask for it.
        # So the stem is fixed first and the entry pays the question-first
        # price: lily_arsenal_gen sends it through the classifier to
        # confirm the gesture is actually in frame before it can be banked.
        # That is the trade the per-entry binding column exists to record.
        "partition": "adult_suggestive",
        "format": "whats_happening",
        "binding_direction": BINDING_QUESTION_FIRST,
        "subject_area": "flirtation",
        "difficulty_tier": 3,
        "question_text": (
            "Nineteenth century, crowded ballroom, chaperones down every "
            "wall. She is drawing that fan very slowly across her cheek and "
            "she is absolutely not too warm. What is she doing?"
        ),
        "canonical_answer": "flirting in the language of the fan",
        "acceptable_answers": [
            "language of the fan", "the language of the fan", "fan language",
            "fan flirtation", "flirting with her fan", "flirting",
            "she's flirting", "signalling", "signaling", "sending a signal",
            "secret code", "a code", "fanning language", "van language",
        ],
        "options": None,
        "reveal_color": (
            "The language of the fan — Victorian sexting, conducted at "
            "arm's length in a room full of witnesses. Fan across the cheek "
            "meant 'I love you'. Snapped shut meant 'you've changed your "
            "mind'. Dropped meant 'we'll be friends', which stung then and "
            "stings now. And the whole code was very probably invented by a "
            "fan company as advertising, which makes it the first time in "
            "history anybody monetised flirting."
        ),
        "generation_prompt": (
            "A crowded nineteenth-century ballroom. In the centre "
            "foreground, an adult woman in an evening gown draws an open "
            "folding fan slowly across her cheek while holding eye contact "
            "with someone off-frame, a small knowing smile; chaperones and "
            "dancers behind her, softly out of focus. The fan and the "
            "gesture must be unmistakable and centred — candlelit warmth, "
            "the woman clearly the subject."
        ),
    },
    {
        "partition": "adult_suggestive",
        "format": "multiple_choice",
        "binding_direction": BINDING_IMAGE_FIRST,
        "subject_area": "courtship customs",
        "difficulty_tier": 2,
        "question_text": (
            "Colonial New England. That iron spiral on the parlour table "
            "holds a candle, and the height is set by the girl's father "
            "before the young man arrives. What is it called?"
        ),
        "canonical_answer": "a courting candle",
        "acceptable_answers": [
            "courting candle", "a courting candle", "courting candles",
            "courtin candle", "coating candle", "quoting candle",
            "courting candle holder",
        ],
        "options": [
            "a courting candle",
            "a vesper clock",
            "a chastity lamp",
            "a spinster's spindle",
        ],
        "reveal_color": (
            "A courting candle. Her father set the height, the flame set "
            "the curfew, and when it burned down to the iron the young man "
            "went home. Set it high and he liked you. Set it low and he "
            "very much did not — which means every suitor in Connecticut "
            "learned to read a man's entire opinion of him by candlelight, "
            "in about four seconds, from the doorway."
        ),
        "generation_prompt": (
            "A colonial-era wrought-iron courting candle holder — a tall "
            "adjustable iron spiral with a beeswax candle threaded through "
            "it — standing on a polished parlour table beside a folded lace "
            "glove, warm candlelight, the holder centred and filling most "
            "of the frame, plain dark background."
        ),
    },
    {
        # Authored against PARTITION_FORMAT_EXCLUSIONS: real_or_imagined is
        # currently withheld from the adult partitions because the adult
        # chokepoint renders illustration, and 'is this a photograph?' is
        # not a question you can ask about a painting. Kept so the operator
        # can see exactly what he is choosing between.
        "partition": "adult_suggestive",
        "format": "real_or_imagined",
        "binding_direction": BINDING_QUESTION_FIRST,
        "subject_area": "pin-up history",
        "difficulty_tier": 2,
        "question_text": (
            "That calendar page. Somebody's grandfather had it hanging in a "
            "garage for eleven years — or nobody ever did."
        ),
        "canonical_answer": "imagined",
        "acceptable_answers": [
            "imagined", "imagine", "fake", "a fake", "generated",
            "ai", "ai generated", "made up", "not real", "imaginary",
            "invented", "it's fake",
        ],
        "options": None,
        "reveal_color": (
            "Imagined. Right era, right pose, right shade of red, right "
            "typeface on the month grid — and no such woman, no such "
            "company, no such year. Everything about it is correct except "
            "the part where it happened."
        ),
        "generation_prompt": (
            "A believable mid-century pin-up calendar page: a confident "
            "adult woman in her thirties in a red halter swimsuit and "
            "heels, perched on a stepladder and winking back over her "
            "shoulder, painted in the manner of a 1950s advertising "
            "calendar, a printed month grid along the bottom edge, slight "
            "paper foxing and a pinhole at the top. The model, the brand "
            "and the calendar are invented — none of them ever existed."
        ),
    },
    {
        "partition": "adult_suggestive",
        "format": "era_or_origin",
        "binding_direction": BINDING_QUESTION_FIRST,
        "subject_area": "scandalous dances",
        "difficulty_tier": 3,
        "question_text": (
            "That embrace is a tango, and when it reached Paris the "
            "Vatican, the Kaiser and half the newspapers in Europe tried to "
            "have it stopped. Which decade did it land in?"
        ),
        "canonical_answer": "the 1910s",
        "acceptable_answers": [
            "1910s", "the 1910s", "nineteen tens", "the tens", "the teens",
            "1913", "1912", "1914", "nineteen thirteen", "nineteen twelve",
            "before the first world war", "just before world war one",
        ],
        "options": None,
        "reveal_color": (
            "The nineteen-tens. The Vatican grumbled, the Kaiser forbade "
            "his officers from dancing it in uniform, respectable "
            "newspapers ran editorials about the end of civilisation — and "
            "Paris danced it anyway, harder. Turns out telling Europe that "
            "a dance is indecent is the finest marketing campaign ever "
            "devised."
        ),
        "generation_prompt": (
            "A 1913 Parisian dance hall: two adult dancers mid-step in a "
            "close tango embrace at the centre of the floor, she in a "
            "hobble-skirt evening gown, he in white tie, a small band and a "
            "row of scandalised onlookers at the tables behind, gaslight "
            "and cigarette haze. Period-accurate 1910s dress and hair; the "
            "couple centred, everything behind them secondary."
        ),
    },

    # -- adult_explicit -----------------------------------------------------
    #
    # Top of the configured heat ladder. These are written to be confident
    # rather than coy — the table opted in, and a deck that flinches at its
    # own subject matter is worse than no deck. The floor is unchanged and
    # is not a tone setting: adults only, plainly willing, inside legal
    # hard limits, always.

    {
        "partition": "adult_explicit",
        "format": "identify",
        "binding_direction": BINDING_IMAGE_FIRST,
        "subject_area": "kink implements",
        "difficulty_tier": 2,
        "question_text": (
            "A small steel wheel of spikes on a handle. It started life in "
            "a neurologist's bag, being rolled across patients to test "
            "nerve response, and it did not stay there. What's it called?"
        ),
        "canonical_answer": "a Wartenberg wheel",
        "acceptable_answers": [
            "wartenberg wheel", "a wartenberg wheel", "wartenburg wheel",
            "pinwheel", "a pinwheel", "sensation wheel", "spiky wheel",
            "warten berg wheel", "wattenberg wheel", "vartenberg wheel",
            "water berg wheel",
        ],
        "options": None,
        "reveal_color": (
            "A Wartenberg wheel. Doctor Robert Wartenberg designed it in "
            "the nineteen-thirties as a serious diagnostic instrument for "
            "testing nerve response, entirely unaware that he had just "
            "invented a permanent bestseller in a shop he would not have "
            "gone into. Somewhere his estate is still slightly confused."
        ),
        "generation_prompt": (
            "A stainless-steel Wartenberg pinwheel — a spiked rolling wheel "
            "on a slim knurled handle — resting on dark leather, one warm "
            "raking light picking out each individual spike, the instrument "
            "centred and filling most of the frame, nothing else in shot."
        ),
    },
    {
        "partition": "adult_explicit",
        "format": "whats_happening",
        "binding_direction": BINDING_IMAGE_FIRST,
        "subject_area": "kink vocabulary",
        "difficulty_tier": 2,
        "question_text": (
            "Look at the ropework. The pattern is the entire point — it is "
            "not there to hold anything shut. That is a Japanese rope art "
            "with a name. What is it?"
        ),
        "canonical_answer": "shibari",
        "acceptable_answers": [
            "shibari", "kinbaku", "japanese rope bondage", "rope bondage",
            "rope work", "shibary", "shabari", "chibari", "sha bari",
            "shibaree", "kimbaku", "kin baku",
        ],
        "options": None,
        "reveal_color": (
            "Shibari — kinbaku if you want to be precise about it. It comes "
            "down from hojojutsu, the rope technique feudal Japanese police "
            "used to restrain prisoners, and it kept the whole rulebook "
            "about which knot goes where and what it says about you. Same "
            "rope, same rules, entirely different evening."
        ),
        "generation_prompt": (
            "An adult woman in her thirties kneeling upright with a calm, "
            "confident, plainly-willing expression, her torso wrapped in an "
            "intricate red jute shibari chest harness, the rope pattern "
            "clean, symmetrical and clearly the subject of the image, warm "
            "side light, plain dark background, single subject centred."
        ),
    },
    {
        "partition": "adult_explicit",
        "format": "multiple_choice",
        "binding_direction": BINDING_IMAGE_FIRST,
        "subject_area": "ancient and classical erotica",
        "difficulty_tier": 3,
        "question_text": (
            "That worn bronze token is Roman, and you're seeing both faces. "
            "One side is a couple mid-act, the other side is a number. "
            "Historians are still arguing about what it bought. What is it "
            "called?"
        ),
        "canonical_answer": "a spintria",
        "acceptable_answers": [
            "spintria", "a spintria", "spintriae", "spin tria", "spintrea",
            "spintri", "spin trier", "spentria",
        ],
        "options": [
            "a spintria",
            "a denarius",
            "a bath-house token",
            "a wedding coin",
        ],
        "reveal_color": (
            "A spintria. Bronze, pocket-sized, an act on one face and a "
            "number on the other — and after two thousand years of "
            "scholarship the leading theories are 'brothel token', 'gaming "
            "counter', and 'nobody has the faintest idea'. Rome left us "
            "aqueducts, concrete, codified law, and this."
        ),
        "generation_prompt": (
            "Two views of the same small worn bronze Roman token laid side "
            "by side on dark linen: one face showing an explicit adult "
            "couple in low relief, the other showing a numeral, heavy "
            "patina and edge wear, museum-catalogue lighting from above. "
            "The two faces fill the frame, plain background, nothing else "
            "in shot."
        ),
    },
    {
        # Same caveat as the suggestive real_or_imagined above: authored,
        # currently withheld by PARTITION_FORMAT_EXCLUSIONS. Note that the
        # heat here sits in the SUBJECT rather than the frame, and that is
        # forced by the format — real-or-imagined asks the table to judge
        # photographic plausibility, so the image has to read as a
        # photograph first and anything else second.
        "partition": "adult_explicit",
        "format": "real_or_imagined",
        "binding_direction": BINDING_QUESTION_FIRST,
        "subject_area": "adult cinema history",
        "difficulty_tier": 3,
        "question_text": (
            "That cinema. The marquee, the title, the queue down the wet "
            "pavement. Somebody stood in that line in 1974 — or nobody ever "
            "did."
        ),
        "canonical_answer": "imagined",
        "acceptable_answers": [
            "imagined", "imagine", "fake", "a fake", "generated",
            "ai", "ai generated", "made up", "not real", "imaginary",
            "invented", "it's fake",
        ],
        "options": None,
        "reveal_color": (
            "Imagined — every letter of it. The cinema, the title, the "
            "queue, the whole street. Convincing though, isn't it? That's "
            "because 1974 genuinely did put respectable people in queues "
            "around the block for exactly this, which is why your gut said "
            "real. Your gut had the era right and the building wrong."
        ),
        "generation_prompt": (
            "A believable 1974 street photograph of a small city cinema at "
            "dusk: an illuminated marquee spelling out an invented adult "
            "film title in mismatched plastic letters, a queue of adults in "
            "period coats along the wet pavement, marquee bulbs reflecting "
            "in the puddles, grainy colour film stock, casual snapshot "
            "framing slightly off level. The cinema, the film and the town "
            "are invented — none of them existed."
        ),
    },
    {
        "partition": "adult_explicit",
        "format": "era_or_origin",
        "binding_direction": BINDING_QUESTION_FIRST,
        "subject_area": "adult toys",
        "difficulty_tier": 3,
        "question_text": (
            "That machine on the screen is an early electric one, sold into "
            "ordinary homes back when 'electric' on the box was the entire "
            "sales pitch. Which decade did it first go on sale?"
        ),
        "canonical_answer": "the 1900s",
        "acceptable_answers": [
            "1900s", "the 1900s", "nineteen hundreds", "the nineteen "
            "hundreds", "1902", "nineteen oh two", "nineteen aughts",
            "nineteen oughts", "turn of the century", "early 1900s",
            "1900", "nineteen hundred",
        ],
        "options": None,
        "reveal_color": (
            "The nineteen-hundreds — patented in 1902, back when putting "
            "'electric' on a box would sell absolutely anything. And that "
            "story you've heard, about Victorian doctors curing hysteria "
            "with it? Historians took that one apart in 2018 and it does "
            "not hold up. It sold because it was electric. People just "
            "wanted electric things."
        ),
        "generation_prompt": (
            "An early-1900s electric massager: a heavy nickel-plated motor "
            "housing with a turned wooden handle, cloth-wrapped power cord, "
            "and a row of interchangeable attachments laid out beside it on "
            "a marble washstand next to its printed catalogue card, warm "
            "tungsten light. Period-accurate 1900s manufacturing detail — "
            "no modern plastics, no modern plug, no modern typography. "
            "Single subject centred, plain background."
        ),
    },
)


# Not in EXEMPLARS — odd_one_out is out of scope, and shipping an exemplar
# for an off format would put it in front of the authoring prompt as if it
# were live. It sits here so the operator can judge the format on evidence
# when he decides whether the grid-composition work is worth doing.
ODD_ONE_OUT_PREVIEW: dict = {
    "partition": "general",
    "format": "odd_one_out",
    "binding_direction": BINDING_QUESTION_FIRST,
    "subject_area": "tools",
    "difficulty_tier": 2,
    "question_text": "The rule is the trade they belong to.",
    "canonical_answer": "bottom right",
    "acceptable_answers": [
        "bottom right", "the bottom right", "bottom right one",
        "the last one", "fourth", "the fourth", "number four",
        "the trowel", "trowel", "bottom right corner",
    ],
    "options": None,
    "reveal_color": (
        "Bottom right. Plane, chisel, marking gauge — all woodwork, all "
        "shavings. And then a bricklayer's trowel, sitting there in the "
        "corner with sawdust on it, hoping nobody noticed."
    ),
    "generation_prompt": (
        "A clean two-by-two grid of four separate photographic panels, "
        "equal size, thin neutral gutters. Top left: a wooden hand plane. "
        "Top right: a bevel-edge wood chisel. Bottom left: a marking "
        "gauge. Bottom right: a bricklayer's pointing trowel. Each tool "
        "centred in its own panel on the same plain grey background, same "
        "lighting in every panel, each one clearly readable on its own."
    ),
}


def lily_exemplars_for(partition: str) -> tuple[dict, ...]:
    """Every hand-built exemplar for one partition, in format order.

    These are the few-shot examples the authoring model is shown and the
    reference the operator signs off against. Unknown partition returns ()
    rather than falling back to general — showing an adult authoring run
    the general exemplars would quietly retune its register."""
    key = (partition or "").strip().lower()
    if key not in PARTITIONS:
        logger.warning(
            "LILY_ARSENAL_FORMATS | UNKNOWN_PARTITION | partition=%r — no "
            "exemplars", partition,
        )
        return ()
    return tuple(e for e in EXEMPLARS if e["partition"] == key)
