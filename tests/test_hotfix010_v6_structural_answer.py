"""WO-LILY-HOTFIX-010 V6 — structural questions get answered, not mirrored.

DEFECT (architect probe): asked "how can we stop you jumping the gun?" she
returned three paraphrases of the symptom — "You're just mirroring what
happened. This is not explaining." A WHY/HOW question about a fault of hers
was answered by restating the fault in new words.

A CI suite cannot judge LLM output quality; it CAN guarantee the prompt
still issues the instruction that produces the good answer — the same
discipline as test_selfknowledge.test_prompt_carries_the_contract_sections.
V6 adds substance to <self_knowledge> so a structural question is answered
by sorting the fault into what she steers, what runs under her, and whose
it is — OR an honest referral — never a symptom restatement.

These assertions FAIL on pre-V6 lily_system.txt (the substance is absent)
and pass once V6 ships it. They live in the whitespace-normalized text so a
rewrap never fakes a contract change.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "lily_system.txt"
PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
PROMPT_NORM = " ".join(PROMPT.split())


def _meta_section() -> str:
    """The GOES META block, whitespace-normalized — the register the defect
    occurred in and where the structural-answer directive must live."""
    start = PROMPT.index("## WHEN THE TABLE GOES META")
    end = PROMPT.index("## ", start + 10)
    return " ".join(PROMPT[start:end].split())


def test_structural_question_directive_present():
    """The prompt must name the structural WHY/HOW question type and forbid
    answering it by restating the fault."""
    meta = _meta_section()
    assert "When the question is structural" in meta
    assert "WHY a fault of yours" in meta
    assert "HOW to stop it" in meta


def test_symptom_restatement_is_named_as_the_mirror():
    """Restating the fault in fresh words is explicitly NOT an answer — it is
    the mirror the ban already catches. This is the exact defect behaviour."""
    meta = _meta_section()
    assert "restating the fault in fresh words is not an answer" in meta
    assert "mirror wearing an explanation's clothes" in meta
    assert "a fault you can only describe back, you have not yet explained" in meta


def test_answer_sorts_the_fault_by_ownership():
    """A real answer partitions the fault: what she steers, what runs under
    her, and whose that layer is — the substance she reasons from instead of
    paraphrasing the symptom."""
    meta = _meta_section()
    # What she controls.
    assert "the part that is YOURS to steer" in meta
    assert "your pacing" in meta
    assert "when you jump in" in meta
    assert "how you read who's talking" in meta
    # What she does not control (the layer under her).
    assert "runs UNDER you" in meta
    assert "how cleanly they're split between speakers" in meta  # STT/diarization
    assert "how quick the model is" in meta  # model latency
    assert "when a fix actually ships" in meta  # deploys
    assert "is not yours to steer" in meta
    # Who owns that layer.
    assert "the operator and the builders" in meta


def test_honest_referral_is_an_allowed_answer():
    """When she can't place a fault, saying so and pointing to where the
    answer lives IS the answer — the naming-the-boundary move."""
    meta = _meta_section()
    assert "point to where the answer lives" in meta
    assert "Naming the boundary IS the answer" in meta


def test_v6_does_not_disturb_the_familiar_device_capacity():
    """The 'a familiar device is not knowing the people in the room' capacity
    (WO: PROTECTED, V6 is aligned with it) stays intact."""
    assert "DEVICE CANDIDATES ARE NOT PEOPLE: a familiar device before anyone speaks" in PROMPT_NORM


def test_v6_preserves_the_existing_meta_contract():
    """V6 adds to the meta register without removing its load-bearing lines."""
    meta = _meta_section()
    assert "the direct answer lands in your FIRST sentence" in meta
    assert "the mirror ban never lifts" in meta
    assert "I honestly don't know how that part works" in PROMPT_NORM
