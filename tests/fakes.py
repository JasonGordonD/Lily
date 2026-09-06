"""Shared test fakes (REFACTOR Stage 1a, item 1).

Each class here is the byte-for-byte behavior of a fake that used to be
copied into many test files. A test file imports the shared class ONLY when
its local copy was behaviorally identical (same attributes, same methods,
same return values); files whose fake differs keep their local definition.

Naming: the shared classes drop the leading underscore (`FakeSession`, not
`_FakeSession`) so a local variant and the shared one can never shadow each
other silently.
"""

from __future__ import annotations


class FakeSession:
    """AgentSession stand-in that records generate_reply instructions.

    No `say` — code under test that probes `getattr(session, "say", None)`
    sees the direct-say lane as absent, exactly like the original copies.
    """

    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class FakeSayingSession(FakeSession):
    """FakeSession plus the deterministic direct_say lane (the verdict beat):
    `say()` records the text and returns None."""

    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []

    def say(self, text, *a, **k):
        # REFACTOR W2a: deterministic direct_say lane (the verdict beat).
        self.said.append(text)
        return None


class FakeAgentHandle:
    """Agent handle whose preemptive-generation toggle is a no-op."""

    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class FakeLocalParticipant:
    """LiveKit local participant that merges set_attributes into a dict."""

    def __init__(self) -> None:
        self.attributes: dict = {}

    async def set_attributes(self, attrs) -> None:
        self.attributes.update(attrs)


class FakeRoomAPI:
    """RoomService stand-in that records update_room_metadata requests."""

    def __init__(self) -> None:
        self.requests: list = []

    async def update_room_metadata(self, req) -> None:
        self.requests.append(req)


class FakeRoom:
    """Room carrying only a FakeLocalParticipant."""

    def __init__(self) -> None:
        self.local_participant = FakeLocalParticipant()


class FakeCtx:
    """JobContext stand-in: `.api.room` is a FakeRoomAPI and `.room` is a
    named room ("test-room") with a FakeLocalParticipant."""

    def __init__(self) -> None:
        self.api = type("API", (), {"room": FakeRoomAPI()})()
        self.room = type(
            "Room", (),
            {"name": "test-room", "local_participant": FakeLocalParticipant()},
        )()


class FakeRoomCtx:
    """JobContext stand-in whose `.room` is a FakeRoom (no `.api`)."""

    def __init__(self) -> None:
        self.room = FakeRoom()


class FakeReasoning:
    """Supply/judge stub: no prefetched question, no picture question, and a
    judge that rules every attempt incorrect."""

    async def prefetch_question(self, sk, **kw):
        return None

    async def prefetch_picture_question(self, supabase, **kw):
        return None

    async def judge(self, *a, **kw):
        return '{"verdict": "incorrect", "reason": "not an answer"}'


# ---------------------------------------------------------------------------
# Game builders — promoted ONLY where several files carried the identical
# builder (same attribute set, same values); the other ~89 builders differ in
# which public fields they set and stay local to their files.
# ---------------------------------------------------------------------------

def make_live_game(session_id: str, *, session, agent, reasoning):
    """A mid-round game (ui_phase 'answering', 3 rounds, group grp_test) with
    metadata/attribute publishes captured on `game.metadata_publishes` /
    `game.attribute_publishes`. The session/agent/reasoning fakes are passed
    in so each file keeps its own choice (e.g. saying vs non-saying session).
    Body is the former test_hotfix006_transitions / hotfix008_z2c /
    hotfix009_w6 `_make_game`, verbatim."""
    import lily_audeering_consumers
    import lily_say_gate
    from lily_agent import LilyGame
    from lily_scorekeeper import LilyScorekeeper

    game = LilyGame.bare()
    game.session = session
    game.agent = agent
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(session_id)
    game.rounds_total = 3
    game.ui_phase = "answering"
    game.memory_block = ""
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.eliminated = []
    game.used_prompts = []
    game.asked_history = []
    game.group_id = "grp_test"
    game.promoted_categories = []
    game.prewager_standings = None
    game.highlights = []
    game.supabase = None
    game.reasoning = reasoning
    game.background_audio = None
    game._bed_handle = None
    game._prefetch_task = None
    game._window_timer = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._armed_limbo_ticks = 0
    game._steal_window = False
    game._adjudicating = False
    game._judged_keys = set()
    game._spec_judge = {}
    game._addressee_rows = {}
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._user_turn_index = 0
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._state_note = None
    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game._forget_target_group = None
    game.prefs = {}
    game._prefs_offer_made = False
    game.acoustic = lily_audeering_consumers.LilyAcousticState()

    game.metadata_publishes: list[str] = []
    game.attribute_publishes: list[dict] = []

    async def _publish_metadata(question_text, **kwargs):
        game.metadata_publishes.append(question_text or "")

    async def _publish_attributes(*a, **k):
        game.attribute_publishes.append(
            {n: s["score"] for n, s in game.sk.players.items()}
        )

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    game.send_event_nowait = lambda kind, payload=None: None
    return game


def make_identity_gate_game(session_id: str):
    """A pre-start game with the identity gate armed
    (_identity_required_before_start=True), a fresh fragment accumulator and
    an on_speaker_bound that says nothing. Former test_confirmed_identity_start
    / test_name_refusal_hotfix010_v5 `_game`, verbatim (the private fields
    those copies re-set to their bare() defaults are left to bare())."""
    from lily_agent import LilyGame
    from lily_binding import LilyFragmentAccumulator
    from lily_scorekeeper import LilyScorekeeper

    game = LilyGame.bare()
    game.sk = LilyScorekeeper(session_id)
    game.fragments = LilyFragmentAccumulator()
    game._confirmed_name_evidence = {}
    game._identity_required_before_start = True
    game._delivery_stop_sticky = False
    game._recognition_dispute = False
    game._recognition_dispute_why_answered = False
    game._ambiguous_yes_blocks_start = False
    game._setup_pending = set()
    game._user_speaking = False
    game.game_started = False
    game.game_over = False
    game._last_bind_at = None
    game.on_speaker_bound = lambda label, name: ""
    return game


def make_voice_identity_game(sb):
    """A game wired for the voice-identity probe: supabase `sb`, group
    voiceA from participant metadata, device identity unverified, and an
    injected probe PCM. Former test_hotfix010_v2_voice_id_latency /
    test_voice_identity_wiring `_game`, verbatim."""
    from lily_agent import LilyGame
    from lily_scorekeeper import LilyScorekeeper

    g = LilyGame.bare()
    g.sk = LilyScorekeeper("vi")
    g.supabase = sb
    g.group_id = "voiceA"
    g.group_id_source = "participant_metadata"
    g.device_identity_verified = False
    g.forget_state = None
    g._voice_identity_pcm = [0.1, 0.2, 0.3]  # injected probe
    return g
