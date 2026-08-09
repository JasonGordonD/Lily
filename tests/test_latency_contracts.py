"""Latency contracts that were quietly false.

Measured live, session `lily-2C489B-a61fb6d9` (2026-08-08, adult, 421s):

    llm_ttft_ms   p50   1954.4   p95   7301.4
    tts_ttfb_ms   p50    412.2   p95    532.9
    e2e_latency   p50   4374.4   p95  13610.7
    llm_input_tokens 134357, llm_input_cached_tokens 42368

TTS is not the problem. Two of the things that are, both pinned here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SRC = (Path(__file__).resolve().parent.parent / "lily_agent.py").read_text(
    encoding="utf-8"
)


def _apply_context_blocks_source() -> str:
    start = SRC.index("    def _apply_context_blocks(")
    return SRC[start:SRC.index("\n    def ", start + 10)]


# -- the prompt-cache prefix --------------------------------------------------


def test_the_injected_blocks_sit_behind_the_system_prompt():
    """THE fixture. A provider's prompt cache keys on the PREFIX. Both
    blocks used to `insert(0, ...)`, landing them in FRONT of the ~8,000
    token system prompt — so every injection invalidated the largest stable
    thing in the context. 134k input tokens against 42k cached."""
    body = _apply_context_blocks_source()
    assert "items.insert(\n                0," not in body, (
        "an injected block is back in front of the system prompt; the "
        "prompt cache prefix is invalidated on every turn"
    )
    assert body.count("items.insert(\n                anchor,") == 2, (
        "both the adult layer and the memory block must anchor behind the "
        "agent's own instructions"
    )


def test_the_anchor_degrades_to_the_front_when_there_is_no_system_prompt():
    """A context whose first item is NOT a system message (unit fixtures,
    a future framework change) must still get its blocks — at 0, exactly
    as before."""
    assert (
        'anchor = 1 if items and getattr(items[0], "role", None) == "system" '
        "else 0" in _apply_context_blocks_source()
    )


def test_the_state_block_still_appends_at_the_tail():
    """PROTECTED. The volatile per-turn block belongs LAST — it is the one
    thing that must not sit in the cached prefix. It already did; this
    keeps it that way."""
    body = _apply_context_blocks_source()
    assert "items.append(" in body


# -- the read timeout that actually applies -----------------------------------


def test_the_llm_read_budget_is_set_where_the_framework_reads_it():
    """0f31b71 raised the adult lane's CLIENT read budget to 30s. Correct
    diagnosis, ineffective fix: the openai plugin's LLMStream inherits from
    livekit.agents.inference.llm.LLMStream, which passes
    `timeout=httpx.Timeout(self._conn_options.timeout)` on every create() —
    and in openai-python a per-request timeout REPLACES the client's. The
    real wall was DEFAULT_API_CONNECT_OPTIONS' 10s on both lanes."""
    assert "conn_options=SessionConnectOptions(" in SRC
    assert "llm_conn_options=APIConnectOptions(" in SRC


def test_a_wedged_first_token_is_not_four_walls_of_dead_air():
    """max_retry defaults to 3, so one wedged first token became ~40s of
    silence in a voice-first game."""
    start = SRC.index("conn_options=SessionConnectOptions(")
    block = SRC[start:start + 600]
    assert "max_retry=1" in block, (
        "the framework's 3 retries are back; a wedged first token is 4x "
        "the read wall of dead air"
    )


def test_the_plugin_really_does_inherit_the_per_request_timeout():
    """The load-bearing external fact, asserted against the INSTALLED
    package rather than trusted. If a version bump changes this, the fix
    above is either unnecessary or insufficient — either way, know."""
    from livekit.plugins.openai import llm as plugin_llm

    src = Path(plugin_llm.__file__).read_text(encoding="utf-8")
    assert "from livekit.agents.inference.llm import LLMStream as _LLMStream" in src
    inference = Path(
        __import__(
            "livekit.agents.inference.llm", fromlist=["llm"]
        ).__file__
    ).read_text(encoding="utf-8")
    assert "timeout=httpx.Timeout(self._conn_options.timeout)" in inference


def test_barge_in_responsiveness_is_untouched():
    """PROTECTED. Every latency change here is on the LLM/context path.
    Interruption runs off Silero plus the adaptive detector and must keep
    its live-tuned floor — this is a game built on shouting over the host
    and nothing in a latency pass may dull that."""
    assert "min_words=1," in SRC
    assert "min_duration=lily_config.interruption_min_duration()" in SRC
