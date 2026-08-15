"""WO-LILY-ARSENAL-SEED-001 A6/A9 — the seeding job.

The work order's acceptance is explicit that working code over an empty
bank is not acceptance. These tests therefore drive the JOB, not its
parts: run it from empty and check the shelf filled; run it twice and check
the second run created nothing; interrupt it and check the resume was
cheap; refuse everything at the provider and check the run REPORTED how far
it got and why instead of quietly coming up short.

The provider is faked, because api.x.ai is unreachable from anywhere but
the deployment — but the orchestration under test is the same code the
operator's run executes.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_arsenal
import lily_arsenal_gen
import lily_arsenal_seed
from fake_supabase import FakeSupabase


def _run(coro):
    return asyncio.run(coro)


# -- fake provider surface ----------------------------------------------------


class FakeProvider:
    """Stands in for author / imagegen / upload / classify."""

    def __init__(self, *, refuse_after=None, refuse_all=False,
                 unavailable=False, classifier_ok=True):
        self.calls = 0
        self.refuse_after = refuse_after
        self.refuse_all = refuse_all
        self.unavailable = unavailable
        self.classifier_ok = classifier_ok
        self.authored = 0

    # Genuinely distinct stems. A fake that emitted "question 1", "question
    # 2", ... would be caught by the A5 near-duplicate rule (ratio 0.82) and
    # the job would correctly bank exactly one of them — which would make
    # every count below measure the fake rather than the job.
    _STEMS = (
        ("Brass, hinged, with a small mirror on a swing arm. What is it?",
         "a sextant"),
        ("That road is bone dry, so what is the puddle up ahead?", "a mirage"),
        ("Which stone arms hold the cathedral wall up from outside?",
         "flying buttresses"),
        ("Name the breed of dog sitting in the doorway.", "a beagle"),
        ("Which planet wears the rings you can see here?", "saturn"),
        ("What instrument is propped against the chair?", "a cello"),
        ("Which sport is being played on that pitch?", "cricket"),
        ("Name the fruit split open on the board.", "a pomegranate"),
        ("What weather event is forming over the plain?", "a tornado"),
        ("Which tool is clamped to the workbench edge?", "a vice"),
        ("What kind of bridge spans the gorge?", "a suspension bridge"),
        ("Name the bird standing in the shallows.", "a heron"),
        ("Which spice fills the bowl on the left?", "saffron"),
        ("What is growing along the terraced hillside?", "rice"),
    )

    async def author(self, partition, plan, image_description):
        stem, answer = self._STEMS[self.authored % len(self._STEMS)]
        self.authored += 1
        return {
            "question_text": stem,
            "canonical_answer": answer,
            "acceptable_answers": [answer],
            "options": None,
            "reveal_color": f"It was {answer}.",
        }

    async def imagegen(self, prompt, partition, intensity):
        self.calls += 1
        if self.unavailable:
            raise RuntimeError(
                "adult image provider unconfigured (XAI_API_KEY)"
            )
        if self.refuse_all or (
            self.refuse_after is not None and self.calls > self.refuse_after
        ):
            raise RuntimeError(
                "xAI image HTTP 400: Generated image rejected by content "
                "moderation"
            )
        return b"\x89PNG-fake-bytes", "image/jpeg", "grok-imagine-image-2.0"

    async def upload(self, data, mime, partition):
        return f"{partition}/{self.calls:04d}.jpg"

    async def classify(self, data, mime, claim, brief):
        return self.classifier_ok, "ok" if self.classifier_ok else "refused"


def _seed(db, partition="general", provider=None, **kw):
    # A shared provider across successive runs keeps the authored stems
    # advancing, so a top-up run writes NEW questions rather than re-emitting
    # the first run's and being (correctly) rejected as duplicates.
    p = provider or FakeProvider(**kw.pop("provider_kw", {}))
    kw.pop("provider_kw", None)
    result = _run(lily_arsenal_seed.lily_seed_partition(
        db, partition=partition, author=p.author, imagegen=p.imagegen,
        upload=p.upload, classify=p.classify, **kw,
    ))
    return result, p


# -- A6: from empty to stocked ------------------------------------------------


def test_run_from_empty_fills_the_partition_to_target():
    db = FakeSupabase()
    result, provider = _seed(db, target=10)
    assert result["created"] == 10
    assert result["final_ready"] == 10
    assert result["status"] == "completed"
    assert provider.calls == 10
    rows = db.tables["lily_picture_arsenal"]
    assert all(r["status"] == "ready" for r in rows)
    assert all(r["image_storage_path"] for r in rows), "every entry has an image"


def test_every_banked_entry_carries_the_full_anatomy():
    """A1: the bank is a designed corpus, not a pile of pictures."""
    db = FakeSupabase()
    _seed(db, target=5)
    for row in db.tables["lily_picture_arsenal"]:
        for field in (
            "format", "binding_direction", "subject_area", "difficulty_tier",
            "question_text_hash", "generation_prompt", "generation_model",
            "image_storage_path", "acceptable_answers", "reveal_color",
            "classifier_verdict", "gate_mode", "run_id",
        ):
            assert row.get(field) not in (None, ""), f"missing {field}"
        assert row["classifier_verdict"] == "pass"


def test_entries_distribute_across_subjects_formats_and_difficulty():
    """A3 verify: generated entries spread rather than clustering."""
    db = FakeSupabase()
    _seed(db, target=10)
    rows = db.tables["lily_picture_arsenal"]
    assert len({r["subject_area"] for r in rows}) >= 8
    assert len({r["format"] for r in rows}) >= 3
    assert len({r["difficulty_tier"] for r in rows}) >= 2


def test_an_immediate_rerun_creates_nothing():
    """Idempotency: re-running is always safe."""
    db = FakeSupabase()
    _seed(db, target=10)
    second, provider = _seed(db, target=10)
    assert second["created"] == 0
    assert provider.calls == 0, "a topped-up shelf must not pay for generation"
    assert len(db.tables["lily_picture_arsenal"]) == 10


def test_a_partial_bank_is_topped_up_not_refilled():
    db = FakeSupabase()
    shared = FakeProvider()
    _seed(db, target=4, provider=shared)
    before = shared.calls
    result, _ = _seed(db, target=10, provider=shared)
    assert result["started_ready"] == 4
    assert result["created"] == 6, "only the shortfall"
    assert shared.calls - before == 6
    assert result["final_ready"] == 10


def test_dry_run_generates_nothing_and_holds_no_lock():
    db = FakeSupabase()
    result, provider = _seed(db, target=10, dry_run=True)
    assert result["status"] == "dry_run"
    assert provider.calls == 0
    assert len(result["plan"]) == 10
    assert db.tables.get("lily_picture_arsenal_runs") in (None, [])
    # And a real run afterwards is unobstructed.
    real, _ = _seed(db, target=10)
    assert real["created"] == 10


# -- A6: concurrency ----------------------------------------------------------


def test_a_second_concurrent_run_stands_down_rather_than_double_filling():
    db = FakeSupabase()
    # Simulate a live run holding the partition.
    _run(lily_arsenal.lily_run_start(db, partition="general", target=10))
    result, provider = _seed(db, target=10)
    assert result["status"] == "skipped_concurrent"
    assert result["created"] == 0
    assert provider.calls == 0
    assert len(db.tables.get("lily_picture_arsenal", [])) == 0


# -- A6: resumability ---------------------------------------------------------

def _now_iso_offset(seconds_ago):
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=seconds_ago)
    ).isoformat()


def test_an_interrupted_run_resumes_and_only_pays_for_the_shortfall():
    db = FakeSupabase()
    shared = FakeProvider()
    # A run that banked 6 then died: entries present, run row left 'running'
    # with a heartbeat that has since gone cold.
    first, _ = _seed(db, target=6, provider=shared)
    assert first["created"] == 6
    db.tables["lily_picture_arsenal_runs"][0]["status"] = "running"
    db.tables["lily_picture_arsenal_runs"][0]["heartbeat_at"] = (
        _now_iso_offset(4000)
    )
    before = shared.calls

    resumed, _ = _seed(db, target=10, provider=shared)
    assert resumed["status"] == "completed"
    assert resumed["created"] == 4, "only the shortfall, not a fresh batch"
    assert shared.calls - before == 4, "the dead run's 6 entries were not re-paid for"
    assert resumed["final_ready"] == 10


# -- A9: moderation ------------------------------------------------------------


def test_moderation_refusals_are_counted_and_the_run_still_reports():
    db = FakeSupabase()
    result, _ = _seed(db, target=10, provider_kw={"refuse_after": 4})
    assert result["created"] > 0
    assert result["rejected_moderation"] > 0
    assert result["status"] == "short_of_target"
    assert result["final_ready"] < 10
    # The findings must SAY how far it got and why.
    joined = " ".join(result["findings"])
    assert "short by" in joined
    assert any("moderation" in r for r in result["skipped_reasons"])


def test_a_partition_the_provider_will_not_paint_reports_the_rate_as_a_finding():
    """A9: 'that is a finding to report, not a failure to hide'."""
    db = FakeSupabase()
    result, _ = _seed(
        db, partition="adult_explicit", target=5,
        provider_kw={"refuse_all": True},
    )
    assert result["created"] == 0
    assert result["rejected_moderation"] == 5
    assert result["moderation_rejection_rate"] == 1.0
    joined = " ".join(result["findings"])
    assert "exceeds what the provider will paint" in joined


def test_each_refusal_is_reworked_a_bounded_number_of_times():
    """Bounded: an unbounded retry against a prompt the provider will never
    paint just burns the account."""
    db = FakeSupabase()
    result, provider = _seed(
        db, target=2, provider_kw={"refuse_all": True},
    )
    # 2 slots x (1 initial + 2 reworks) = 6 attempts, and no more.
    assert provider.calls == 6
    assert result["attempts"] == 6


def test_moderation_cost_counts_attempts_not_entries():
    """A refused generation still bills."""
    db = FakeSupabase()
    result, _ = _seed(db, target=2, provider_kw={"refuse_all": True})
    assert result["created"] == 0
    assert result["cost_usd"] > 0, "refusals are not free"


def test_generation_being_down_halts_early_instead_of_burning_the_plan():
    db = FakeSupabase()
    result, provider = _seed(db, target=10, provider_kw={"unavailable": True})
    assert result["status"] == "halted_generation_unavailable"
    assert provider.calls == 1, "one probe, not ten"
    assert "generation unavailable" in " ".join(result["findings"])


# -- A5: the gate in the job --------------------------------------------------


def test_adult_entries_land_pending_review_and_cannot_serve():
    """The first adult batch cannot reach a table without the operator."""
    db = FakeSupabase()
    result, _ = _seed(db, partition="adult_explicit", target=5)
    assert result["gate_mode"] == "review"
    assert result["created"] == 5
    assert result["final_ready"] == 0, "nothing servable yet"
    assert result["final_pending"] == 5
    rows = db.tables["lily_picture_arsenal"]
    assert all(r["status"] == "generating" for r in rows)
    drawn = _run(lily_arsenal.lily_arsenal_draw(
        db, partition="adult_explicit", group_id="g1", session_id="s1"))
    assert drawn is None
    assert any("awaiting operator review" in f for f in result["findings"])


def test_a_classifier_refusal_never_reaches_the_bank():
    db = FakeSupabase()
    result, _ = _seed(db, target=5, provider_kw={"classifier_ok": False})
    assert result["created"] == 0
    assert result["rejected_gate"] == 5
    assert len(db.tables.get("lily_picture_arsenal", [])) == 0


def test_a_missing_classifier_fails_closed():
    """A run with no classifier must not quietly fill the bank."""
    db = FakeSupabase()
    provider = FakeProvider()
    result = _run(lily_arsenal_seed.lily_seed_partition(
        db, partition="general", author=provider.author,
        imagegen=provider.imagegen, upload=provider.upload,
        classify=None, target=3,
    ))
    assert result["created"] == 0
    assert result["rejected_gate"] == 3


# -- A6: the run summary -------------------------------------------------------


def test_the_run_summary_is_persisted():
    db = FakeSupabase()
    result, _ = _seed(db, target=5)
    runs = db.tables["lily_picture_arsenal_runs"]
    assert len(runs) == 1
    row = runs[0]
    assert row["status"] == "completed"
    assert row["created_count"] == 5
    assert row["target_depth"] == 5
    assert row["finished_at"]
    assert float(row["cost_usd"]) > 0


def test_the_printed_report_names_what_was_skipped_and_why():
    """'created 6 of 10' without the four reasons is the report that sends
    somebody to the logs."""
    db = FakeSupabase()
    result, _ = _seed(db, target=6, provider_kw={"refuse_after": 3})
    report = lily_arsenal_seed.lily_format_run_report([result])
    assert "seeding run summary" in report
    assert "general" in report
    assert "moderation" in report
    assert "skipped:" in report
    assert "FINDING:" in report


# -- A7/A8: the whole point ----------------------------------------------------


def test_a_seeded_bank_serves_instantly_with_zero_generation():
    """Acceptance, in miniature: from a cold start the first picture
    question comes off the shelf and nothing is generated on the way."""
    db = FakeSupabase()
    _seed(db, target=10)
    provider = FakeProvider()
    served = _run(lily_arsenal.lily_arsenal_draw(
        db, partition="general", group_id="new-table", session_id="s1"))
    assert served is not None
    assert served["image_storage_path"]
    assert served["prompt"]
    assert provider.calls == 0, "zero generation on the delivery path"


def test_sustained_play_never_repeats_an_entry_for_one_group():
    db = FakeSupabase()
    _seed(db, target=10)
    seen = []
    for i in range(10):
        q = _run(lily_arsenal.lily_arsenal_draw(
            db, partition="general", group_id="g1", session_id=f"s{i}"))
        assert q is not None, f"pool exhausted early at {i}"
        seen.append(q["id"])
    assert len(set(seen)) == 10
    # Eleventh draw finds nothing left for this group — and says so.
    assert _run(lily_arsenal.lily_arsenal_draw(
        db, partition="general", group_id="g1", session_id="s11")) is None


def test_crossing_the_watermark_mid_play_triggers_replenishment_for_that_partition():
    """A8: the refill fires for the crossed partition alone."""
    db = FakeSupabase()
    _seed(db, target=10)
    _seed(db, partition="adult_suggestive", target=10)
    for i in range(4):
        _run(lily_arsenal.lily_arsenal_draw(
            db, partition="general", group_id="g1", session_id="sess"))
    served = _run(lily_arsenal.lily_arsenal_served_count(
        db, session_id="sess", partition="general"))
    ready = _run(lily_arsenal.lily_arsenal_ready_count(db, partition="general"))
    available = _run(lily_arsenal.lily_arsenal_available_to_group(
        db, partition="general", group_id="g1"))
    assert served == 4
    # The global ready count does NOT drop when a group plays — entries stay
    # ready because they are still new to every other group. Availability to
    # THIS group is what actually ran down, and it is what fires the refill.
    assert ready == 10
    assert available == 6
    assert lily_arsenal.lily_should_replenish(
        served, ready, target=10, available_to_group=available) is True
    # Without the availability signal the old rule would have held at 10/10
    # while this table ran out — that gap is the reason the signal exists.
    assert lily_arsenal.lily_should_replenish(served, ready, target=10) is False
    # The untouched partition is independent and does not fire.
    other_served = _run(lily_arsenal.lily_arsenal_served_count(
        db, session_id="sess", partition="adult_suggestive"))
    other_available = _run(lily_arsenal.lily_arsenal_available_to_group(
        db, partition="adult_suggestive", group_id="g1"))
    assert other_served == 0
    # Zero available, but for a different reason entirely: adult partitions
    # are review-gated, so those ten entries are sitting at 'generating'
    # waiting for the operator and are not servable to anybody yet.
    assert other_available == 0
    assert _run(lily_arsenal.lily_arsenal_bank_depth(
        db, partition="adult_suggestive"))["pending"] == 10
    # It still does not fire: this session has served nothing from it. The
    # partitions are evaluated independently, so a general pool running
    # down never drags the adult pool's refill along with it.
    assert lily_arsenal.lily_should_replenish(
        other_served, 0, target=10, available_to_group=other_available
    ) is False


# -- --promote-all: bulk operator pass ----------------------------------------


def test_promote_all_promotes_every_pending_entry_in_a_partition():
    """--promote-all <partition> transitions ALL 'generating' rows in that
    partition to 'ready' — the same state change --promote <id> makes, once
    per entry, without the operator copying UUIDs."""
    from fake_supabase import seed_entry

    db = FakeSupabase()
    for _ in range(4):
        seed_entry(db, "adult_suggestive", status="generating")
    # An already-ready row and a rejected one must be left untouched.
    seed_entry(db, "adult_suggestive", status="ready")
    seed_entry(db, "adult_suggestive", status="rejected")

    summary = _run(
        lily_arsenal_seed.lily_promote_all(db, partition="adult_suggestive")
    )
    assert summary["promoted"] == 4
    assert summary["failed"] == 0
    assert summary["per_partition"]["adult_suggestive"] == 4

    rows = db.tables["lily_picture_arsenal"]
    assert sum(1 for r in rows if r["status"] == "generating") == 0
    assert sum(1 for r in rows if r["status"] == "ready") == 5  # 4 promoted + 1
    assert sum(1 for r in rows if r["status"] == "rejected") == 1
    # Promotion stamps the review columns exactly as the single-entry path does.
    promoted = [r for r in rows if r.get("reviewed_by") == "operator"]
    assert len(promoted) == 4


def test_promote_all_no_arg_sweeps_every_partition():
    """A bare --promote-all promotes pending entries across all partitions."""
    from fake_supabase import seed_entry

    db = FakeSupabase()
    seed_entry(db, "adult_suggestive", status="generating")
    seed_entry(db, "adult_suggestive", status="generating")
    seed_entry(db, "adult_explicit", status="generating")

    summary = _run(lily_arsenal_seed.lily_promote_all(db, partition=None))
    assert summary["promoted"] == 3
    assert summary["per_partition"]["adult_suggestive"] == 2
    assert summary["per_partition"]["adult_explicit"] == 1
    # 'general' has none pending -> 0, not an error.
    assert summary["per_partition"]["general"] == 0
    assert all(
        r["status"] == "ready" for r in db.tables["lily_picture_arsenal"]
    )


def test_promote_all_on_empty_partition_is_a_noop():
    """Nothing pending -> promotes nothing, reports zero, does not fail."""
    db = FakeSupabase()
    summary = _run(
        lily_arsenal_seed.lily_promote_all(db, partition="adult_suggestive")
    )
    assert summary == {
        "promoted": 0, "failed": 0, "per_partition": {"adult_suggestive": 0},
    }
