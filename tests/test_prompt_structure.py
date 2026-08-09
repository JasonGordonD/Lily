"""Y1a (WO-LILY-HOTFIX-007) — the system prompt is XML-sectioned and the
assembly is static, pinned.

Y1's verify clause: "prompt is fully XML-sectioned; static block proven
byte-identical across consecutive turns." The assembly reads two static
files plus one import-time constant (the audeering rubric), so consecutive
turns are byte-identical BY CONSTRUCTION — these tests pin that
construction so a future dynamic append (a clock, a session id, an f-string)
breaks loudly instead of silently killing the cacheable prefix (Y1b).

Interior text was byte-preserved during tagging; the 83 character
regression pins (test_character_regression.py) prove no directive changed.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
import lily_audeering_consumers

_PROMPTS_DIR = Path(lily_agent.__file__).resolve().parent / "prompts"

# The canonical section order of lily_system.txt. A new section is a
# deliberate edit to this list, not a drive-by.
_SYSTEM_SECTIONS = [
    "identity",
    "voice",
    "game_rules",
    "state",
    "tools",
    "memory",
    "self_knowledge",
    "tts_guidelines",
    "voice_output",
]


def _top_level_tags(text: str) -> list[str]:
    """Tags that occupy a whole line — the sectioning convention."""
    return re.findall(r"^</?([a-z_]+)>$", text, flags=re.MULTILINE)


def test_system_prompt_sections_present_balanced_and_ordered():
    text = (_PROMPTS_DIR / "lily_system.txt").read_text(encoding="utf-8")
    tags = _top_level_tags(text)
    expected: list[str] = []
    for name in _SYSTEM_SECTIONS:
        expected.extend([name, name])  # open immediately followed by close
    assert tags == expected, (
        "lily_system.txt sections changed — update _SYSTEM_SECTIONS "
        f"deliberately. Found: {tags}"
    )
    # Balanced as actual open/close pairs, not just name counts.
    for name in _SYSTEM_SECTIONS:
        assert text.index(f"<{name}>") < text.index(f"</{name}>"), name
        assert text.count(f"<{name}>") == 1, name
        assert text.count(f"</{name}>") == 1, name


def test_adult_layer_is_tagged_and_marker_preserved():
    text = (_PROMPTS_DIR / "layer_lily_adult.md").read_text(encoding="utf-8")
    assert text.startswith("<adult_layer>\n")
    assert text.rstrip("\n").endswith("</adult_layer>")
    # The dedup logic keys on this marker — the tag wrap must not move it.
    assert lily_agent._ADULT_LAYER_MARKER in text


def test_assembled_prompt_is_file_text_plus_tagged_rubric():
    """The assembly is exactly: file text + tagged rubric constant. No
    dynamic content — this equality IS the byte-stability proof, since
    every input is a static file or import-time constant."""
    file_text = (_PROMPTS_DIR / "lily_system.txt").read_text(encoding="utf-8")
    rubric = lily_audeering_consumers.lily_audeering_rubric_block().strip("\n")
    assert lily_agent.LILY_SYSTEM_PROMPT == (
        file_text + "\n<room_read>\n" + rubric + "\n</room_read>\n"
    )


def test_assembled_prompt_has_room_read_section_once():
    assert lily_agent.LILY_SYSTEM_PROMPT.count("<room_read>") == 1
    assert lily_agent.LILY_SYSTEM_PROMPT.count("</room_read>") == 1


def test_assembly_is_byte_identical_across_reads():
    """Consecutive-turn stability: re-running the assembly expression
    yields the same bytes as the module constant built at import."""
    rebuilt = (
        (_PROMPTS_DIR / "lily_system.txt").read_text(encoding="utf-8")
        + "\n<room_read>\n"
        + lily_audeering_consumers.lily_audeering_rubric_block().strip("\n")
        + "\n</room_read>\n"
    )
    assert rebuilt == lily_agent.LILY_SYSTEM_PROMPT
    assert (
        lily_audeering_consumers.lily_audeering_rubric_block()
        == lily_audeering_consumers.lily_audeering_rubric_block()
    )
