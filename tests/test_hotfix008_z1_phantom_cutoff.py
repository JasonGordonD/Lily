"""WO-LILY-HOTFIX-008 Z1 — the phantom-`[cut off]` double-record.

Fixture: session lily-938EFF-2260354c (2026-08-10 03:41–03:45, room log
job AJ_DkF4Au8gitLr). 20 of the session's 36 LILY rows end in
"…[cut off]" against `PREEMPTIVE_INVALIDATED total=15` in its logs, and
NONE of the 20 is a verbatim duplicate of a prior recorded row — because
the phantom REPLACED the real row:

  1. a generation commits: `conversation_item_added` writes her text into
     the last-assistant buffer BEFORE that turn's playout record exists;
  2. the user speaks; the framework's speculative reply is invalidated at
     turn commit and its SpeechHandle reaches the playout watcher
     ITEMLESS with interrupted=True;
  3. the watcher's itemless fallback fabricated `spoken` from the buffer
     (the still-airing previous turn) and the `interrupted` branch
     recorded it as "…[cut off]" — a record-only phantom, no TTS;
  4. when the REAL turn's playout then finished, its own record died on
     the verbatim-dup guard against the phantom already in
     `sk.agent_turns` — so the table shows the phantom marked cut off and
     the genuinely-aired turn is gone, and the phantom rides her own
     context/repeat-lint window.

The fix is a DELETION at the source (the fallback), not a new guard —
the HOTFIX-007 mandate forbids stacking another suppressor over a root
cause (the fallback itself was already one narrowing, 541dcff). Plus the
buffer is now STAMPED with the chat-item id it belongs to, so no reader
keyed to any other generation can ever borrow a neighboring turn's text.

Pinned here:
  - an invalidated-preemptive/itemless-interrupted handle records and
    publishes NOTHING;
  - the lily-938EFF replay: exactly one recorded agent turn per
    generation, zero `[cut off]` rows without genuine truncation, and
    user speech during/after an agent turn never reproduces prior text;
  - the stale-read race is closed at the buffer: under every
    interleaving of the turn-N+1 buffer write and a generation-keyed
    read, the read can never return turn N's text;
  - a genuine barge-in (real partial aired, had_items=True) still
    records its real partial marked `[cut off]` — nothing else does;
  - the HOTFIX-002 tool-call-only shape stays an empty record.
"""

import ast
import asyncio
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
from test_desync_fixture import _make_game

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CUT_MARK = " …[cut off]"


def _load(name):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


class _FakeItem:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class _FakeHandle:
    """The three shapes the watcher sees: aired (assistant items),
    tool-call-only (items, no assistant text), invalidated-preemptive
    (no items at all, interrupted=True)."""

    def __init__(self, id, items, interrupted):
        self.id = id
        self.chat_items = items
        self.interrupted = interrupted


class _FakeBatcher:
    def __init__(self):
        self.rows = []

    def add(self, text, speaker_label=None, speaker_name=None,
            segment_start=None, segment_end=None):
        self.rows.append({"text": text, "label": speaker_label})


class _FakeWriter:
    def __init__(self, sink):
        self.sink = sink

    async def write(self, value):
        self.sink.append(value)

    async def aclose(self):
        pass


class _FakePublication:
    def __init__(self):
        self.kind = lily_agent.rtc.TrackKind.KIND_AUDIO
        self.sid = "TR_fake_audio"


class _FakeLocalParticipant:
    def __init__(self):
        self.identity = "lily"
        self.track_publications = {"a": _FakePublication()}
        self.published = []
        self.streamed = []

    async def publish_transcription(self, transcription):
        self.published.append(transcription)

    async def stream_text(self, topic=None, attributes=None):
        return _FakeWriter(self.streamed)


class _FakeRoom:
    def __init__(self, participant):
        self.local_participant = participant


class _FakeCtx:
    def __init__(self, participant):
        self.room = _FakeRoom(participant)


def _game():
    game = _make_game()
    game.transcripts = _FakeBatcher()
    game.participant = _FakeLocalParticipant()
    game.ctx = _FakeCtx(game.participant)
    return game


def _finish(game, handle):
    """The watcher tail, exactly as wired at speech_created: collect what
    the handle actually carried, then hand it to the finish callback."""
    spoken, _had_items = lily_agent._handle_spoken_text(handle)
    suppressed_ids = getattr(game, "_suppressed_speech_ids", set())
    game.on_agent_speech_finished(
        spoken,
        speech_id=handle.id,
        interrupted=handle.interrupted,
        suppressed=handle.id in suppressed_ids,
        failed=False,
    )


def _run(coro):
    """Run one async scenario; any task the finish path armed (the
    cut-recovery watchdog) is cancelled inside the loop."""

    async def _wrapped():
        result = await coro
        current = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not current:
                task.cancel()
        await asyncio.sleep(0)
        return result

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()


# -- the evidence, pinned ------------------------------------------------------


def test_fixture_is_the_live_defect_shape():
    rows = _load("lily-938EFF-2260354c.transcripts.json")
    events = _load("lily-938EFF-2260354c.preemptive_log.json")
    lily_rows = [r for r in rows if r["speaker_label"] == "LILY"]
    cut_rows = [r for r in lily_rows if r["text"].endswith(CUT_MARK)]
    assert len(rows) == 59
    assert len(cut_rows) == 20
    assert len(events) == 15
    assert all("PREEMPTIVE_INVALIDATED" in e["message"] for e in events)
    # The defect signature that rules out the AAC431 dup class: no phantom
    # is a verbatim copy of a PRIOR recorded row — each phantom row IS the
    # only record of the turn it stole its text from.
    bases = [r["text"][: -len(CUT_MARK)] for r in cut_rows]
    assert len(set(bases)) == len(bases)


# -- the watcher: what a handle actually carried -------------------------------


def test_itemless_handle_is_empty_never_borrowed():
    """The invalidated-preemptive shape: no items -> ("", False). The old
    fallback turned exactly this shape into the previous turn's text."""
    spoken, had_items = lily_agent._handle_spoken_text(
        _FakeHandle("speech_phantom", [], interrupted=True)
    )
    assert (spoken, had_items) == ("", False)


def test_tool_call_only_handle_stays_empty():
    """HOTFIX-002 Defect 1 preserved: items but no assistant text aired no
    new words — empty record, no fabrication."""
    spoken, had_items = lily_agent._handle_spoken_text(
        _FakeHandle("speech_tool", [_FakeItem("tool", "lookup()")], False)
    )
    assert (spoken, had_items) == ("", True)


def test_aired_handle_carries_its_own_text():
    spoken, had_items = lily_agent._handle_spoken_text(
        _FakeHandle(
            "speech_real",
            [_FakeItem("assistant", "Round one, question one!")],
            False,
        )
    )
    assert (spoken, had_items) == ("Round one, question one!", True)


# -- record/publish: the phantom path is dark ---------------------------------


def test_invalidated_preemptive_records_and_publishes_nothing():
    game = _game()
    # Turn N committed to chat ctx (buffer written) and still airing —
    # exactly the live state when the invalidation lands.
    game._last_assistant_turn = (
        "item_N", "[soft] Yeah. A little glitchy on the feed right now."
    )

    async def scenario():
        _finish(game, _FakeHandle("speech_phantom", [], interrupted=True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(scenario())
    assert game.transcripts.rows == []
    assert game.sk.agent_turns == []
    assert game.participant.published == []
    assert game.participant.streamed == []


def test_replay_lily_938EFF_one_record_per_generation_no_phantoms():
    """The live interleaving replayed with the fix: buffer write at
    generation commit -> invalidated-preemptive handle finishes ->
    the real turn's playout record. Every generation lands exactly once,
    unmarked; the phantoms land nowhere; her context window
    (sk.agent_turns) holds only what actually aired."""
    rows = _load("lily-938EFF-2260354c.transcripts.json")
    # The 20 [cut off] bases ARE the session's real committed turns — the
    # phantom row was the only record each of those turns ever got.
    aired = [
        r["text"][: -len(CUT_MARK)]
        for r in rows
        if r["speaker_label"] == "LILY" and r["text"].endswith(CUT_MARK)
    ]
    events = _load("lily-938EFF-2260354c.preemptive_log.json")
    game = _game()

    invalidations = iter(range(len(events)))
    for n, text in enumerate(aired):
        # 1. generation commit: conversation_item_added stamps the buffer
        #    (the prod write shape at _on_item_added).
        game._last_assistant_turn = (f"item_{n}", text)
        # 2. a speculative reply is invalidated mid-air (15 events across
        #    20 turns live; the exact pairing doesn't matter — every
        #    itemless finish must be dark regardless of position).
        if next(invalidations, None) is not None:
            _finish(
                game, _FakeHandle(f"speech_phantom_{n}", [], interrupted=True)
            )
        # 3. the real turn's playout completes.
        _finish(
            game,
            _FakeHandle(
                f"speech_real_{n}", [_FakeItem("assistant", text)], False
            ),
        )

    recorded = [r["text"] for r in game.transcripts.rows]
    assert recorded == aired  # one row per generation, none stolen
    assert not any(CUT_MARK.strip() in t for t in recorded)
    assert game.sk.agent_turns == aired
    # User speech during/after an agent turn never reproduces prior text:
    # nothing recorded twice, nothing recorded that did not air.
    assert len(set(recorded)) == len(recorded)


# -- the stamp: stale-read race closed at the buffer ---------------------------


def test_stale_read_race_closed_under_every_interleaving():
    """conversation_item_added for turn N+1 racing a generation-keyed
    read: wherever the read lands relative to the writes, it can never
    return turn N's text."""
    write_n = lambda g: setattr(g, "_last_assistant_turn", ("item_N", "text N"))
    write_n1 = lambda g: setattr(
        g, "_last_assistant_turn", ("item_N1", "text N+1")
    )
    for read_position in (0, 1, 2):
        game = _make_game()
        observed = []
        steps = [write_n, write_n1]
        steps.insert(
            read_position,
            lambda g: observed.append(g.last_assistant_text_for({"item_N1"})),
        )
        for step in steps:
            step(game)
        assert observed[0] in ("", "text N+1")
        assert observed[0] != "text N"


def test_stamp_scopes_every_generation_keyed_read():
    game = _make_game()
    game._last_assistant_turn = ("item_N", "text N")
    # The phantom shape: a reader holding NO items of its own.
    assert game.last_assistant_text_for(set()) == ""
    # A reader keyed to a different generation.
    assert game.last_assistant_text_for({"item_other"}) == ""
    # The owning generation reads its own text.
    assert game.last_assistant_text_for({"item_N"}) == "text N"
    # Once N+1 commits, N's reader can no longer see the buffer either.
    game._last_assistant_turn = ("item_N1", "text N+1")
    assert game.last_assistant_text_for({"item_N"}) == ""


def test_manual_buffer_write_is_unstamped_and_unborrowable():
    """Direct `_last_assistant_text = ...` writes (tests, any legacy
    caller) keep working through the property — readable as her last
    turn, never attributable to any generation."""
    game = _make_game()
    game._last_assistant_text = "manual text"
    assert game._last_assistant_text == "manual text"
    assert game.last_assistant_text_for({"item_N"}) == ""


# -- [cut off] semantics: genuine truncation only ------------------------------


def test_genuine_bargein_still_records_its_real_partial_marked():
    game = _game()
    partial = "Round two, question three! Which river runs through"
    game._last_assistant_turn = ("item_prev", "The previous full turn text.")

    async def scenario():
        # A phantom first — must stay dark even back-to-back with a cut.
        _finish(game, _FakeHandle("speech_phantom", [], interrupted=True))
        # Then the genuine barge-in: real partial aired, had_items=True.
        _finish(
            game,
            _FakeHandle(
                "speech_cut", [_FakeItem("assistant", partial)], True
            ),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(scenario())
    assert [r["text"] for r in game.transcripts.rows] == [partial + CUT_MARK]
    assert game.sk.agent_turns == [partial]
    # The durable records carry the marked real partial — and the phantom
    # published nothing.
    published = [s.segments[0].text for s in game.participant.published]
    assert published == [partial + CUT_MARK]
    # WO-LILY-UI-SYNC-TYPEWRITER-001 (default ON): the manual lk.transcription
    # stream mirror is suppressed — the framework's own final chunk carries
    # the truncation on the wire. The legacy publish + transcript rows above
    # remain the durable cut record. (The flag-off stream mirror is covered
    # in test_transcript_forwarding.)
    assert game.participant.streamed == []


def test_no_cut_off_row_without_genuine_truncation():
    """The shapes that must never mint a marker: itemless interrupt,
    tool-call-only turn, suppressed (never-aired) turn. (The
    suppressed-AND-interrupted cancel_speech shape is the pre-existing §7
    residual, out of Z1's scope — not pinned here.)"""
    game = _game()
    game._last_assistant_turn = ("item_prev", "The previous full turn text.")
    game._suppressed_speech_ids = {"speech_suppressed"}

    async def scenario():
        _finish(game, _FakeHandle("speech_phantom", [], interrupted=True))
        _finish(
            game,
            _FakeHandle("speech_tool", [_FakeItem("tool", "lookup()")], True),
        )
        _finish(
            game,
            _FakeHandle(
                "speech_suppressed",
                [_FakeItem("assistant", "Never aired, suppressed at tts_node.")],
                False,
            ),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(scenario())
    marked = [
        r["text"] for r in game.transcripts.rows
        if r["text"].endswith(CUT_MARK)
    ]
    assert marked == []


# --- Z1 review Note A: needle the LIVE `_watch` closure body ---------------
#
# Every functional test above drives the EXTRACTED _handle_spoken_text; a
# regression that re-adds a buffer fallback INSIDE the `_watch` closure —
# between the _handle_spoken_text call and on_agent_speech_finished —
# would slip past all of them. So the closure body itself is needled at
# the source (the test_post_tts_transcript_truth idiom), on UNPARSED AST
# rather than raw text: the Z1 deletion note in `_watch` quotes the old
# fallback verbatim in a comment, and needles must hit code, not prose.


def _watch_body_violations(source: str) -> list:
    """Needle checker for a module source containing the playout watcher
    `_watch`. Returns human-readable violations; [] means the Z1 shape
    holds. Source-agnostic on purpose so the negative fixture below can
    prove the needle bites on the pre-Z1 shape."""
    watches = [
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_watch"
    ]
    if len(watches) != 1:
        return [f"expected exactly one `_watch` closure, found {len(watches)}"]
    watch = watches[0]
    violations = []

    code_only = ast.unparse(watch)
    for needle in ("_last_assistant_text", "last_assistant_text_for"):
        if needle in code_only:
            violations.append(f"buffer reference inside _watch: {needle}")

    def _called_name(call):
        fn = call.func
        return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)

    spoken_var = None
    for node in ast.walk(watch):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _called_name(node.value) == "_handle_spoken_text"
        ):
            target = node.targets[0]
            first = target.elts[0] if isinstance(target, ast.Tuple) else target
            if isinstance(first, ast.Name):
                spoken_var = first.id
    if spoken_var is None:
        violations.append(
            "_watch no longer binds the result of _handle_spoken_text"
        )
        return violations

    # Any OTHER write to the spoken variable is a reintroduced rewrite.
    for node in ast.walk(watch):
        if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _called_name(node.value) == "_handle_spoken_text"
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            for leaf in ast.walk(t):
                if isinstance(leaf, ast.Name) and leaf.id == spoken_var:
                    violations.append(
                        f"`{spoken_var}` reassigned inside _watch after "
                        "_handle_spoken_text"
                    )

    # And it must reach on_agent_speech_finished as the first argument,
    # unmodified (a bare Name, not an expression wrapping it).
    finish_calls = [
        n for n in ast.walk(watch)
        if isinstance(n, ast.Call)
        and _called_name(n) == "on_agent_speech_finished"
    ]
    if not finish_calls:
        violations.append("_watch no longer calls on_agent_speech_finished")
    for call in finish_calls:
        first_arg = call.args[0] if call.args else None
        if not (isinstance(first_arg, ast.Name) and first_arg.id == spoken_var):
            violations.append(
                "on_agent_speech_finished's first argument is not the "
                f"unmodified `{spoken_var}` from _handle_spoken_text"
            )
    return violations


# The pre-Z1 regression shape: the two-line itemless fallback re-added
# between _handle_spoken_text and on_agent_speech_finished (541dcff's
# narrowing, deleted by Z1). Embedded as text — never checked out.
_PRE_Z1_WATCH_SHAPE = '''
async def _watch() -> None:
    await handle.wait_for_playout()
    spoken, had_items = _handle_spoken_text(handle)
    if not spoken and not had_items:
        spoken = game._last_assistant_text
    game.on_agent_speech_finished(
        spoken,
        speech_id=handle.id,
        interrupted=handle.interrupted,
    )
'''


def test_watch_body_has_no_buffer_fallback():
    """The live `_watch` closure keeps the Z1 shape: no reference to the
    last-assistant buffer, and _handle_spoken_text's result flows to
    on_agent_speech_finished untouched."""
    assert _watch_body_violations(inspect.getsource(lily_agent)) == []


def test_watch_needle_bites_on_the_pre_z1_shape():
    """Non-vacuity self-check: run the same needle against the pre-Z1
    fallback shape and require it to FAIL — on both the buffer reference
    and the `spoken` rewrite."""
    violations = _watch_body_violations(_PRE_Z1_WATCH_SHAPE)
    assert any("_last_assistant_text" in v for v in violations)
    assert any("reassigned" in v for v in violations)
