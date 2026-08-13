"""Suite-wide baseline (WS-14): room-discharge pacing pinned OFF.

The suite's window-open assertions (desync fixture, stall recovery,
recognition variety, ...) pin delivery-REGISTRATION semantics — the window
opening synchronously at the delivery turn's playout completion. The
WS-14 discharge gap (LILY_ROOM_DISCHARGE_SECONDS, default 0.5s) is a
pacing layer on top of that: gap=0 is documented as exactly the
pre-WS-14 behavior, so the suite runs with it pinned to 0 and the pacing
behavior itself is covered explicitly in test_interruption_layer.py with
nonzero gaps.
"""

import os


def pytest_configure(config) -> None:
    os.environ["LILY_ROOM_DISCHARGE_SECONDS"] = "0"
    # PACING-001 between-beat breath is a live pacing layer on top of the
    # deterministic next-question dispatch. The suite's post-reveal seam
    # assertions pin the inline dispatch (breath=0 = pre-PACING-001 behavior);
    # the breath itself is covered explicitly in test_interruption_layer.py
    # with a nonzero value — same discipline as the discharge gap above.
    os.environ["LILY_INTER_QUESTION_BREATH_SECONDS"] = "0"
