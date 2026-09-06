"""WO-LILY-LLM-USAGE-ALL-PATHS-001 — lily_config.effective_snapshot().

The operator needs the DEPLOYED values (LiveKit Cloud env is unreadable
from outside), never the repo defaults, and never a secret. The snapshot
is persisted as `config_snapshot` in lily_sessions.metadata by the
integrator; these tests pin its contract: reflects env overrides, carries
build identity when present (omits it when absent), and leaks nothing on
the denylist.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config


_SECRET_VALUES = {
    "XAI_API_KEY": "xai-SECRET-1",
    "GOOGLE_API_KEY": "goog-SECRET-2",
    "ELEVEN_API_KEY": "el-SECRET-3",
    "SUPABASE_SERVICE_ROLE_KEY": "sb-SECRET-4",
    "SUPABASE_URL": "https://SECRET-5.supabase.co",
    "LIVEKIT_API_SECRET": "lk-SECRET-6",
    "LIVEKIT_API_KEY": "lk-SECRET-7",
    "LIVEKIT_URL": "wss://SECRET-8.livekit.cloud",
    "LILY_VOICE_1": "VOICE-SECRET-9",
    "LILY_VOICE_2": "VOICE-SECRET-10",
    "EXA_API_KEY": "exa-SECRET-11",
    "TAVILY_API_KEY": "tv-SECRET-12",
    "AUDEERING_API_KEY": "aud-SECRET-13",
    "LILY_XAI_BASE_URL": "https://SECRET-14.example",
}


def _set_secrets(monkeypatch):
    for k, v in _SECRET_VALUES.items():
        monkeypatch.setenv(k, v)


def test_snapshot_contains_no_denylisted_key_or_secret_value(monkeypatch):
    _set_secrets(monkeypatch)
    snap = lily_config.effective_snapshot()
    values = snap["values"]
    assert values, "snapshot must not be empty"
    for name in values:
        up = name.upper()
        assert not any(d in up for d in lily_config.SNAPSHOT_DENYLIST) or (
            name in lily_config._SNAPSHOT_PRESENCE_ALLOW
        ), f"denylisted accessor leaked into snapshot: {name}"
    for env_name in snap["env_overrides"]:
        assert not any(d in env_name for d in lily_config.SNAPSHOT_DENYLIST), (
            f"secret env name leaked into env_overrides: {env_name}"
        )
    blob = repr(snap)
    for v in _SECRET_VALUES.values():
        assert v not in blob, f"secret value leaked: {v}"
    assert "SECRET" not in blob
    # Presence bools are allowed — they carry no secret.
    assert values.get("google_api_key_present") is True


def test_snapshot_reflects_env_override_and_default(monkeypatch):
    monkeypatch.delenv("LILY_VOCAL_MODEL", raising=False)
    monkeypatch.delenv("LILY_ADULT_VOCAL_EFFORT", raising=False)
    base = lily_config.effective_snapshot()
    assert base["values"]["vocal_model"] == "grok-4.5"
    assert "LILY_VOCAL_MODEL" not in base["env_overrides"]

    monkeypatch.setenv("LILY_VOCAL_MODEL", "grok-4-fast-non-reasoning")
    monkeypatch.setenv("LILY_ADULT_VOCAL_EFFORT", "high")
    snap = lily_config.effective_snapshot()
    assert snap["values"]["vocal_model"] == "grok-4-fast-non-reasoning"
    # Derived accessors resolve through the override too.
    assert snap["values"]["adult_vocal_model"] == "grok-4-fast-non-reasoning"
    assert snap["values"]["adult_vocal_effort"] == "high"
    assert "LILY_VOCAL_MODEL" in snap["env_overrides"]
    assert "LILY_ADULT_VOCAL_EFFORT" in snap["env_overrides"]


def test_snapshot_values_are_json_scalars(monkeypatch):
    snap = lily_config.effective_snapshot()
    for name, v in snap["values"].items():
        assert v is None or isinstance(v, (str, int, float, bool)), (name, v)


def test_snapshot_skips_accessors_that_need_arguments_and_constants():
    snap = lily_config.effective_snapshot()
    # Pure constants (no env read) are not "deployed config".
    assert "vocal_effort" not in snap["values"]
    assert "judge_model" not in snap["values"]
    # Accessors with optional args are callable and included when they
    # read env; constants with optional args stay out.
    assert "adult_reasoning_effort" not in snap["values"]  # constant too
    assert "arsenal_target_depth" in snap["values"]


def test_snapshot_carries_build_identity_when_set(monkeypatch):
    monkeypatch.setenv("LILY_GIT_SHA", "a380531deadbeef")
    monkeypatch.setenv("GITHUB_RUN_ID", "1234567890")
    snap = lily_config.effective_snapshot()
    assert snap["git_sha"] == "a380531deadbeef"
    assert snap["build_run_id"] == "1234567890"
    assert lily_config.git_sha() == "a380531deadbeef"
    assert lily_config.build_run_id() == "1234567890"


def test_snapshot_omits_build_identity_when_absent(monkeypatch):
    monkeypatch.delenv("LILY_GIT_SHA", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    snap = lily_config.effective_snapshot()   # must not raise
    assert "git_sha" not in snap
    assert "build_run_id" not in snap
    assert lily_config.git_sha() is None
    assert lily_config.build_run_id() is None


def test_snapshot_never_leaves_trace_armed(monkeypatch):
    lily_config.effective_snapshot()
    assert lily_config._snapshot_trace is None
    # A later ordinary accessor call must not be recorded anywhere.
    lily_config.vocal_model()
    assert lily_config._snapshot_trace is None
