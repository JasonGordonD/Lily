"""WO-LILY-PATCH-003 P3 — three-way heat (suggestive/explicit/mix).

Supersedes RULINGS-001 R3's two-way line. Fixtures: adult+explicit chosen
but soft imagery served (heat didn't thread); and "I want both, I want
both" wrongly refused ("the dial only points one way at a time") — the
operator rules a mix is a legitimate choice. Heat threads
elicitation → state → generation-call intensity → Grok chokepoint; mix is
a session ceiling (explicit) with a per-question render level for range.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_imagegen
from lily_scorekeeper import LilyScorekeeper


# -- state accepts the three-way choice ----------------------------------------


def test_scorekeeper_accepts_mix():
    sk = LilyScorekeeper("p3")
    assert sk.set_adult_image_intensity("mix") is True
    assert sk.adult_image_intensity == "mix"
    assert sk.set_adult_image_intensity("explicit") is True
    assert sk.set_adult_image_intensity("garbage") is False


def test_mix_survives_snapshot_restore():
    sk = LilyScorekeeper("p3")
    sk.set_adult_image_intensity("mix")
    snap = sk.snapshot()
    sk2 = LilyScorekeeper("p3")
    sk2.rehydrate(snap)
    assert sk2.adult_image_intensity == "mix"


# -- render resolution ---------------------------------------------------------


def test_concrete_levels_pass_through():
    assert lily_imagegen.lily_resolve_render_intensity("suggestive", "q") == "suggestive"
    assert lily_imagegen.lily_resolve_render_intensity("explicit", "q") == "explicit"


def test_mix_resolves_within_the_ceiling_and_is_deterministic():
    seeds = [f"question number {i}" for i in range(40)]
    levels = [lily_imagegen.lily_resolve_render_intensity("mix", s) for s in seeds]
    # Every resolved level is a real render level within [suggestive, explicit].
    assert set(levels) <= {"suggestive", "explicit"}
    # Mix actually varies across the session (both registers appear).
    assert "suggestive" in levels and "explicit" in levels
    # Deterministic: same seed -> same level (stable replay).
    assert all(
        lily_imagegen.lily_resolve_render_intensity("mix", s) == lvl
        for s, lvl in zip(seeds, levels)
    )


def test_suggestive_session_never_yields_explicit_imagery():
    for s in [f"q{i}" for i in range(20)]:
        assert lily_imagegen.lily_resolve_render_intensity("suggestive", s) == "suggestive"


def test_adult_style_renders_the_resolved_level():
    sug = lily_imagegen.lily_adult_style("a scene", intensity="suggestive")
    exp = lily_imagegen.lily_adult_style("a scene", intensity="explicit")
    assert "SUGGESTIVE" in sug and "EXPLICIT" not in sug
    assert "EXPLICIT" in exp
