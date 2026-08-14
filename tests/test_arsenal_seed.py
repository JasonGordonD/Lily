"""WO-LILY-ARSENAL-SEED-001 — stocking the picture bank.

These tests exist because the previous round of this work shipped code that
passed its own tests over an empty bank. So they check the things that
actually determine whether a shelf gets stocked and stays honest:

  - the watermark scales with configured depth (a hardcoded "fire at 4"
    silently becomes "fire at 80% empty" when depth drops to 5);
  - a re-run creates NOTHING (idempotency), including under a review gate
    where entries are pending rather than ready;
  - two concurrent runs cannot double-fill;
  - an interrupted run resumes instead of starting over, and a LIVE run is
    not mistaken for a dead one;
  - moderation rejection is counted and REPORTED, not swallowed;
  - a group can never be served the same entry twice;
  - the gate holds adult entries off the rail until an operator passes
    them.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_arsenal
import lily_arsenal_gen
import lily_config
from fake_supabase import FakeSupabase, seed_entry


def _run(coro):
    return asyncio.run(coro)


# -- A8: the watermark scales with depth --------------------------------------


def test_watermark_scales_with_configured_depth():
    """40% consumed, whatever the depth. The count is derived, never
    hardcoded — this is the bug that would bite the moment the operator
    takes depth from 10 to 5."""
    assert lily_arsenal.lily_replenish_threshold(10, 0.40) == 4
    assert lily_arsenal.lily_replenish_threshold(5, 0.40) == 2
    assert lily_arsenal.lily_replenish_threshold(7, 0.40) == 3
    # Never zero: a zero threshold would fire replenishment forever.
    assert lily_arsenal.lily_replenish_threshold(1, 0.40) == 1


def test_watermark_preserves_patch003_behaviour_at_depth_ten():
    """The PATCH-003 contract at the shipped default must not move."""
    assert lily_arsenal.lily_should_replenish(4, 6, target=10) is True
    assert lily_arsenal.lily_should_replenish(3, 6, target=10) is False
    assert lily_arsenal.lily_should_replenish(9, 10, target=10) is False


def test_watermark_fires_earlier_at_shallower_depth():
    """At depth 5 the second serve is 40% — a hardcoded 4 would have waited
    until the shelf was 80% gone."""
    assert lily_arsenal.lily_should_replenish(2, 3, target=5) is True
    assert lily_arsenal.lily_should_replenish(1, 4, target=5) is False


# -- A5: the quality gate ------------------------------------------------------


def test_gate_holds_adult_entries_for_operator_review_by_default():
    """The first adult batch cannot serve on a classifier's say-so."""
    assert lily_arsenal.lily_gate_status("general", classifier_ok=True) == "ready"
    assert (
        lily_arsenal.lily_gate_status("adult_suggestive", classifier_ok=True)
        == "generating"
    )
    assert (
        lily_arsenal.lily_gate_status("adult_explicit", classifier_ok=True)
        == "generating"
    )


def test_classifier_refusal_is_never_promotable():
    """No gate mode promotes something the classifier refused."""
    for partition in lily_arsenal.PARTITIONS:
        assert (
            lily_arsenal.lily_gate_status(partition, classifier_ok=False)
            == "rejected"
        )


def test_gate_mode_is_configurable_without_a_deploy(monkeypatch=None):
    import os

    os.environ["LILY_ARSENAL_GATE_MODE_ADULT_EXPLICIT"] = "auto"
    try:
        assert lily_config.arsenal_gate_mode("adult_explicit") == "auto"
        assert (
            lily_arsenal.lily_gate_status("adult_explicit", classifier_ok=True)
            == "ready"
        )
    finally:
        del os.environ["LILY_ARSENAL_GATE_MODE_ADULT_EXPLICIT"]
    assert lily_config.arsenal_gate_mode("adult_explicit") == "review"


def test_pending_entries_cannot_be_drawn():
    """A 'generating' entry is invisible to the draw — that IS the gate."""
    db = FakeSupabase()
    seed_entry(db, "adult_explicit", status="generating")
    drawn = _run(lily_arsenal.lily_arsenal_draw(
        db, partition="adult_explicit", group_id="g1", session_id="s1"
    ))
    assert drawn is None


def test_operator_promotion_makes_an_entry_servable():
    db = FakeSupabase()
    row = seed_entry(db, "adult_explicit", status="generating")
    assert _run(lily_arsenal.lily_arsenal_promote(db, arsenal_id=row["id"])) is True
    drawn = _run(lily_arsenal.lily_arsenal_draw(
        db, partition="adult_explicit", group_id="g1", session_id="s1"
    ))
    assert drawn is not None
    assert db.tables["lily_picture_arsenal"][0]["reviewed_by"] == "operator"


# -- A5: dedup -----------------------------------------------------------------


def test_near_duplicate_is_caught_not_just_exact_text():
    """The hash alone lets a bank fill with the same question reworded."""
    db = FakeSupabase()
    seed_entry(db, "general", question_text="which landmark is shown here")
    hit = _run(lily_arsenal.lily_arsenal_is_duplicate(
        db, partition="general",
        question_text="which landmark is shown here?", check_kb=False,
    ))
    assert hit is not None


def test_genuinely_different_question_is_not_a_duplicate():
    db = FakeSupabase()
    seed_entry(db, "general", question_text="which landmark is shown here")
    hit = _run(lily_arsenal.lily_arsenal_is_duplicate(
        db, partition="general",
        question_text="what breed of dog is this", check_kb=False,
    ))
    assert hit is None


def test_duplicate_entry_is_not_banked_twice():
    db = FakeSupabase()
    q = {
        "prompt": "what object is this",
        "canonical_answer": "a teapot",
        "image_storage_path": "general/a.jpg",
    }
    assert _run(lily_arsenal.lily_arsenal_insert(
        db, partition="general", question=q)) is True
    assert _run(lily_arsenal.lily_arsenal_insert(
        db, partition="general", question=q)) is False
    assert len(db.tables["lily_picture_arsenal"]) == 1


def test_entry_without_an_image_is_never_banked():
    """The arsenal's whole promise is a ready image."""
    db = FakeSupabase()
    assert _run(lily_arsenal.lily_arsenal_insert(
        db, partition="general",
        question={"prompt": "what is this", "canonical_answer": "x"},
    )) is False


# -- A1: entry anatomy round-trips ---------------------------------------------


def test_stored_entry_reconstructs_a_servable_question():
    """A1 verify: the stored fields alone rebuild a servable question with
    its image — nothing re-derived, nothing regenerated."""
    db = FakeSupabase()
    _run(lily_arsenal.lily_arsenal_insert(
        db, partition="general",
        question={
            "prompt": "what is happening in this scene",
            "canonical_answer": "a wedding",
            "acceptable_answers": ["a wedding", "wedding", "marriage"],
            "options": ["a wedding", "a funeral", "a graduation", "a parade"],
            "reveal_color": "A wedding — look at the confetti.",
            "image_storage_path": "general/scene.jpg",
            "format": "whats_happening",
            "binding_direction": "image_first",
            "subject_area": "everyday scenes",
            "difficulty_tier": 3,
            "generation_prompt": "a wedding scene",
        },
    ))
    row = db.tables["lily_picture_arsenal"][0]
    question = lily_arsenal._row_to_question(row)
    assert question["prompt"] == "what is happening in this scene"
    assert question["canonical_answer"] == "a wedding"
    assert question["difficulty_tier"] == 3
    assert question["format"] == "whats_happening"
    assert question["image_storage_path"] == "general/scene.jpg"
    assert question["options"][0] == "a wedding"
    assert question["reveal_color"].startswith("A wedding")
    # The columns _row_to_question reads must actually be persisted — they
    # were read through dict.get() against columns that did not exist.
    assert row["difficulty_tier"] == 3
    assert row["reveal_color"]
    assert row["binding_direction"] == "image_first"
    assert row["subject_area"] == "everyday scenes"


# -- A7: draw and burn ---------------------------------------------------------


def test_a_group_never_sees_the_same_entry_twice():
    db = FakeSupabase()
    seed_entry(db, "general", question_text="q1")
    first = _run(lily_arsenal.lily_arsenal_draw(
        db, partition="general", group_id="g1", session_id="s1"))
    second = _run(lily_arsenal.lily_arsenal_draw(
        db, partition="general", group_id="g1", session_id="s2"))
    assert first is not None
    assert second is None  # only entry already burned for this group


def test_a_different_group_can_be_served_the_same_entry():
    db = FakeSupabase()
    seed_entry(db, "general", question_text="q1")
    assert _run(lily_arsenal.lily_arsenal_draw(
        db, partition="general", group_id="g1", session_id="s1")) is not None
    assert _run(lily_arsenal.lily_arsenal_draw(
        db, partition="general", group_id="g2", session_id="s2")) is not None


def test_forced_duplicate_usage_insert_is_rejected_by_the_constraint():
    """The no-repeat rule is DB-enforced, not merely intended."""
    db = FakeSupabase()
    row = seed_entry(db, "general")
    db.table("lily_picture_arsenal_usage").insert(
        {"arsenal_id": row["id"], "group_id": "g1", "partition": "general"}
    ).execute()
    raised = False
    try:
        db.table("lily_picture_arsenal_usage").insert(
            {"arsenal_id": row["id"], "group_id": "g1", "partition": "general"}
        ).execute()
    except Exception:
        raised = True
    assert raised, "UNIQUE(arsenal_id, group_id) must reject a second serve"


def test_retire_never_deletes():
    db = FakeSupabase()
    row = seed_entry(db, "general")
    assert _run(lily_arsenal.lily_arsenal_retire(
        db, arsenal_id=row["id"], reason="stale")) is True
    stored = db.tables["lily_picture_arsenal"][0]
    assert stored["status"] == "retired"
    assert len(db.tables["lily_picture_arsenal"]) == 1  # provenance survives


def test_mix_heat_draws_from_both_adult_partitions():
    assert lily_arsenal.lily_partitions_for("mix") == [
        "adult_suggestive", "adult_explicit",
    ]
    assert lily_arsenal.lily_partitions_for("explicit") == ["adult_explicit"]
    assert lily_arsenal.lily_partitions_for("suggestive") == ["adult_suggestive"]


# -- A6: idempotency, concurrency, resumability -------------------------------


def test_bank_depth_counts_pending_so_review_gated_reruns_are_idempotent():
    """Under a review gate entries sit at 'generating'. A job sizing off
    'ready' alone would regenerate a full batch on every re-run."""
    db = FakeSupabase()
    seed_entry(db, "adult_explicit", status="ready", question_text="a")
    seed_entry(db, "adult_explicit", status="generating", question_text="b")
    seed_entry(db, "adult_explicit", status="rejected", question_text="c")
    depth = _run(lily_arsenal.lily_arsenal_bank_depth(
        db, partition="adult_explicit"))
    assert depth["ready"] == 1
    assert depth["pending"] == 1
    assert depth["depth"] == 2  # rejected does not count toward the shelf


def test_a_second_concurrent_run_cannot_claim_the_same_partition():
    db = FakeSupabase()
    first = _run(lily_arsenal.lily_run_start(db, partition="general", target=10))
    second = _run(lily_arsenal.lily_run_start(db, partition="general", target=10))
    assert first is not None
    assert second is None, "the partial unique index must block a second run"


def test_a_live_run_is_not_reclaimed_as_stale():
    """Reclaiming every 'running' row on sight would hand the concurrency
    guard back its own key."""
    db = FakeSupabase()
    run_id = _run(lily_arsenal.lily_run_start(db, partition="general", target=10))
    db.tables["lily_picture_arsenal_runs"][0]["heartbeat_at"] = (
        _now_iso_offset(seconds_ago=5)
    )
    reclaimed = _run(lily_arsenal.lily_run_reclaim_stale(db, partition="general"))
    assert reclaimed == 0
    assert run_id is not None
    assert db.tables["lily_picture_arsenal_runs"][0]["status"] == "running"


def test_an_interrupted_run_is_reclaimed_so_a_rerun_can_proceed():
    db = FakeSupabase()
    _run(lily_arsenal.lily_run_start(db, partition="general", target=10))
    db.tables["lily_picture_arsenal_runs"][0]["heartbeat_at"] = (
        _now_iso_offset(seconds_ago=4000)
    )
    reclaimed = _run(lily_arsenal.lily_run_reclaim_stale(db, partition="general"))
    assert reclaimed == 1
    assert db.tables["lily_picture_arsenal_runs"][0]["status"] == "failed"
    # And the partition can be claimed again.
    assert _run(lily_arsenal.lily_run_start(
        db, partition="general", target=10)) is not None


def _now_iso_offset(*, seconds_ago: int) -> str:
    import datetime

    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_ago
    )
    return stamp.isoformat()


# -- A9: moderation is an expected, counted outcome ---------------------------


def test_provider_moderation_refusal_is_recognised():
    """The exact on-record error must classify as a refusal, not an error:
    `xAI image HTTP 400: Generated image rejected by content moderation`"""
    assert lily_arsenal_gen.lily_is_moderation_rejection(
        "xAI image HTTP 400: Generated image rejected by content moderation"
    ) is True
    assert lily_arsenal_gen.lily_is_moderation_rejection(
        "no image in response (possible safety rejection)"
    ) is True


def test_transport_failure_is_not_mistaken_for_moderation():
    """Reworking the prompt does not fix a dead socket or a missing key."""
    assert lily_arsenal_gen.lily_is_moderation_rejection(
        "adult image provider unconfigured (XAI_API_KEY)"
    ) is False
    assert lily_arsenal_gen.lily_is_unavailable(
        "adult image provider unconfigured (XAI_API_KEY)"
    ) is True
    assert lily_arsenal_gen.lily_is_unavailable("xAI image HTTP 429: slow down") is True


def test_rework_keeps_the_subject_and_steps_the_heat_down():
    base = "A clear image of a burlesque dressing room"
    first = lily_arsenal_gen.lily_rework_prompt(base, 1)
    second = lily_arsenal_gen.lily_rework_prompt(base, 2)
    assert base in first and base in second
    assert first != second
    assert "suggestively" in first
    assert "still-life" in second or "setting" in second


# -- A4: answer sets ----------------------------------------------------------


def test_answer_set_covers_grammatical_manglings():
    got = lily_arsenal_gen.lily_answer_set(
        "The Colosseum", ["colloseum", "coliseum"]
    )
    assert "the colosseum" in got
    assert "colosseum" in got      # article stripped
    assert "colloseum" in got      # author-supplied near-miss
    assert len(got) == len(set(got)), "no duplicates in the answer set"


def test_answer_set_normalises_punctuation_and_case():
    got = lily_arsenal_gen.lily_answer_set("St. Basil's Cathedral")
    assert all(g == g.lower() for g in got)
    assert any("basil" in g for g in got)


# -- A10: observability --------------------------------------------------------


def test_health_readout_reports_counts_rejection_rate_and_cost():
    db = FakeSupabase()
    for i in range(3):
        seed_entry(db, "general", question_text=f"q{i}",
                   generation_cost_usd=0.02)
    seed_entry(db, "general", question_text="bad", status="rejected",
               generation_cost_usd=0.02)
    seed_entry(db, "adult_explicit", question_text="p", status="generating")
    health = _run(lily_arsenal.lily_arsenal_health(db))
    general = health["partitions"]["general"]
    assert general["ready"] == 3
    assert general["rejected"] == 1
    assert general["rejection_rate"] == 0.25
    assert general["cost_usd"] == 0.08
    assert health["partitions"]["adult_explicit"]["pending_review"] == 1
    assert health["total_cost_usd"] == 0.08


def test_below_watermark_raises_an_arsenal_low_warning():
    db = FakeSupabase()
    seed_entry(db, "general", question_text="only one")
    health = _run(lily_arsenal.lily_arsenal_health(db))
    assert health["partitions"]["general"]["below_watermark"] is True
    assert health["healthy"] is False
    warnings = lily_arsenal.lily_arsenal_low_warnings(health)
    assert any("ARSENAL_LOW" in w and "general" in w for w in warnings)


def test_a_stocked_partition_raises_no_warning():
    db = FakeSupabase()
    for i in range(10):
        seed_entry(db, "general", question_text=f"stocked {i}")
    health = _run(lily_arsenal.lily_arsenal_health(db))
    assert health["partitions"]["general"]["stocked"] is True
    assert health["partitions"]["general"]["below_watermark"] is False


def test_readout_renders_for_an_empty_bank():
    """The readout an operator checks BEFORE a game night must be readable
    when the bank is empty — that is exactly when it matters."""
    db = FakeSupabase()
    health = _run(lily_arsenal.lily_arsenal_health(db))
    text = lily_arsenal.lily_format_health_readout(health)
    assert "PICTURE ARSENAL" in text
    for partition in lily_arsenal.PARTITIONS:
        assert partition in text
    assert "LOW" in text
