"""The XML tool-call leak (live 2026-08-12 15:26 ET): the vocal model
emitted lily_bind_speaker as literal <tool_call> XML in the content
stream and Lily READ THE BLOCK ALOUD ("Why are you reading this stuff
out loud?"). The JSON-form leak filter never matched it. The XML form —
whole block, truncated block, and stray fragments — now excises on the
same "tool_call" reason; a pure tool-call turn collapses to nothing
speakable and the empty-candidate path regenerates instead of airing it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate

LIVE_BLOCK = """<tool_call>
<tool_name>lily_bind_speaker</tool_name>
<parameter_name>speaker_label</parameter_name>
<parameter_value>S1</parameter_value>
<parameter_name>player_name</parameter_name>
<parameter_value>Rami</parameter_value>
</tool_call>."""


def _filtered(text):
    out = lily_say_gate.lily_filter_leaks(text)
    return out if isinstance(out, tuple) else (out, [])


def test_the_live_block_never_reaches_speech():
    clean, reasons = _filtered(LIVE_BLOCK)
    assert "tool_call" in reasons
    assert "tool_name" not in clean and "lily_bind_speaker" not in clean
    # After the hygiene pass the turn is unspeakable — the caller's
    # empty-candidate retry owns it, nothing airs.
    assert lily_say_gate.lily_clean_for_speech(clean).strip(" .") == ""


def test_prose_around_a_block_survives():
    clean, reasons = _filtered(
        "Rami — locked in.\n" + LIVE_BLOCK + "\nRight, first question!"
    )
    assert "tool_call" in reasons
    assert "locked in" in clean.lower()
    assert "first question" in clean.lower()
    assert "tool_call" not in clean.lower()


def test_truncated_block_strips_to_end():
    clean, reasons = _filtered(
        "One sec.\n<tool_call>\n<tool_name>lily_bind_speaker</tool_name>"
    )
    assert "tool_call" in reasons
    assert "tool_name" not in clean


def test_stray_fragment_lines_strip():
    clean, reasons = _filtered("<parameter_value>Rami</parameter_value> done")
    assert "tool_call" in reasons
    assert "parameter" not in clean


def test_json_form_still_caught():
    clean, reasons = _filtered(
        'Sure! {"name": "lily_bind_speaker", "arguments": {"x": 1}} ok'
    )
    assert "tool_call" in reasons
