"""Adjudication vocabulary is never a player name (lily-A070E8).

Live 2026-08-09, 11:03 UTC: a name-fix exchange bound the player as
"Correct" (his one-word confirmation) and then "Supposed" (a fragment of
"it's supposed to be Robin") — the screen cycled Robin → Correct →
Supposed while he spelled R-A-M-I in NATO. Verdict words, spelling-fix
words and screen-complaint words pass the shape check (capitalized STT
tokens) but are never names.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_binding import lily_extract_name, lily_is_valid_name


def test_verdict_and_correction_words_are_not_names():
    for word in (
        "Correct", "Incorrect", "Supposed", "Wrong", "Screen",
        "Spelled", "Fixing", "Instead", "Latency", "Exactly",
    ):
        assert lily_is_valid_name(word) is False, word


def test_real_names_containing_stopwords_stay_bindable():
    # Exact lowercase match only: Wright is not "right", Newman is not "new".
    for word in ("Rami", "Robin", "Wright", "Newman", "Paige"):
        assert lily_is_valid_name(word) is True, word


def test_the_live_utterances_no_longer_extract_garbage():
    # "Correct." — the one-word confirmation that became the bound name.
    assert lily_extract_name("Correct.") is None
    # "it's supposed to be Rami" — the introducer regex captures
    # "supposed"; the stoplist now rejects it and the fallback finds the
    # actual name.
    assert lily_extract_name("It's supposed to be Rami") == "Rami"
    # A screen complaint mid-fix binds nobody.
    assert lily_extract_name(
        "You put. Correct. The word correct."
    ) is None
