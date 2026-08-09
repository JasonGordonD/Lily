"""M7: the model-visible prompt agrees with deterministic runtime truth."""

import inspect
from pathlib import Path

from lily_agent import LilyAgent


ROOT = Path(__file__).resolve().parent.parent
PROMPT = (ROOT / "prompts" / "lily_system.txt").read_text(encoding="utf-8")
PROMPT_NORM = " ".join(PROMPT.split())
INVENTORY = (ROOT / "docs" / "voice_inventory.md").read_text(
    encoding="utf-8"
)


def test_speaker_labels_are_not_claimed_as_certain_identity():
    assert "You always know who is speaking" not in PROMPT
    assert "Speaker labels are best-effort, not identity" in PROMPT_NORM
    assert "use a name only after it is confirmed and bound" in PROMPT_NORM


def test_prompt_and_tool_require_identity_plus_explicit_start():
    assert "at least one confirmed, bound player" in PROMPT_NORM
    assert "Only clear start language" in PROMPT_NORM
    assert "first genuine group laugh" not in PROMPT_NORM
    tool_contract = inspect.getdoc(LilyAgent.lily_begin_round) or ""
    assert "clear start language" in tool_contract
    assert "confirmed bound name" in tool_contract
    assert "bare yes" in tool_contract


def test_custom_round_claims_only_registered_work():
    assert "BUILT AND REGISTERED" in PROMPT
    assert "registers zero, refuse plainly" in PROMPT_NORM
    assert "say you're putting their round together" not in PROMPT_NORM


def test_state_prompt_matches_need_to_know_and_temporal_tail():
    assert "current UTC/session elapsed time" in PROMPT_NORM
    assert "canonical answer is deliberately absent" in PROMPT_NORM
    assert "current question with its pinned answer" not in PROMPT_NORM


def test_render_and_stop_claims_match_runtime_gates():
    assert "only image_shown confirms that it rendered" in PROMPT_NORM
    assert "STOP is sticky" in PROMPT
    assert "explicitly says resume or continue" in PROMPT_NORM
    assert "the board's behind me" not in PROMPT_NORM


def test_voice_inventory_records_model_visible_authorities():
    for marker in (
        "Function-tool docstrings are model-visible instructions",
        "current UTC and session elapsed time",
        "zero registered questions means a refusal",
        "`image_shown` is the only on-screen confirmation",
        "Sticky STOP permits one acknowledgment",
    ):
        assert marker in INVENTORY
