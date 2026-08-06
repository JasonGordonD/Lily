"""WO-LILY-ENV-DEPLOY-LINT-001 — bidirectional env-var ↔ deploy.yml lint.

The incident this guards (2026-07-31): deploy.yml correctly forwarded
XAI_API_KEY, but a wiring gap anywhere on this axis greens the job and
lets a key resolve to empty at runtime (grok 401s silently). A build-time
lint reads red on the wiring instead of waiting for a live 401.

Runs on every merge (it's a pytest under tests/, so the deploy job's
`needs: test` gate makes it build-breaking):

  Direction 1 (missing-forward): every env var the runtime reads via the
    lily_config accessors either reaches the deploy container OR is
    declared in LILY_LOCAL_ONLY_ENV (default-backed, ships on its
    in-code default and is intentionally not wired as a deploy override).
  Direction 2 (orphan-forward): every var the workflow delivers into the
    container maps to a lily_config accessor OR is declared in
    LILY_EXTERNAL_FORWARD_ENV (consumed directly by a plugin/the deploy
    container, no accessor by design).
  Consistency: a var plumbed via one deploy.yml form but not the other
    (job `env:` mapping vs docker `-e NAME="${NAME}"`) is a latent drop —
    it expands to empty in the container. Flagged either direction.

SCOPE / LIMITATION (WO §6): this lint checks deploy.yml *wiring* only —
that a required var is forwarded and that a forward has a consumer. It
CANNOT see whether the GitHub secret actually exists at repo scope; an
absent-secret-at-scope (the XAI_API_KEY root cause) resolves to empty
even with correct wiring and needs a runtime/secret-scope check, out of
this WO's scope. `lily_config` is the single env surface (its module
docstring: "no raw os.environ reads scattered through the tree"), so the
accessor literals are the authoritative required set.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CONFIG = _REPO / "lily_config.py"
_DEPLOY = _REPO / ".github" / "workflows" / "deploy.yml"


# ---------------------------------------------------------------------------
# Allowlists — the two legal escapes from the bidirectional check. Both are
# explicit, reviewed declarations: the whole point of the lint is that
# adding a new required accessor (or a new forward) forces a conscious
# choice here rather than a silent drift.
# ---------------------------------------------------------------------------

# Required accessor vars that intentionally ship on their in-code default
# and are NOT wired as deploy overrides. Each has a safe default in
# lily_config (tunables / thresholds / optional paths); forwarding one is a
# deliberate ops decision (move it into deploy.yml's `vars.` block if you
# want to override it in production). None of these is a `_require` boot key.
LILY_LOCAL_ONLY_ENV = {
    # WS-16 dereverb node — DEFAULT OFF, enabling is gated on the decision
    # memo + operator sign-off; move to deploy.yml only at enablement.
    "LILY_DEREVERB_NODE",
    # Addressee confidence fusion tunables (WO-ADDRESSEE-H1).
    "LILY_ADDRESSEE_ACOUSTIC_MAX_FUTURE_SECONDS",
    "LILY_ADDRESSEE_ACOUSTIC_MAX_STALENESS_SECONDS",
    "LILY_ADDRESSEE_CONFIDENCE_NEUTRAL",
    "LILY_ADDRESSEE_CONFIDENCE_PENALTY_MAX",
    "LILY_ADDRESSEE_FUSION_ACOUSTIC_WEIGHT",
    "LILY_ADDRESSEE_FUSION_DIARIZATION_WEIGHT",
    # Overlap (crosstalk) fusion tunables.
    "LILY_OVERLAP_EPSILON_SECONDS",
    "LILY_OVERLAP_FUSION_DIARIZATION_WEIGHT",
    "LILY_OVERLAP_FUSION_MIN_CONFIDENCE",
    "LILY_OVERLAP_FUSION_NEUTRAL_CONFIDENCE",
    # Tier-1 acceptance thresholds (state-prior machine).
    "LILY_TIER1_CLARIFY_MARGIN",
    "LILY_TIER1_THRESHOLD_HOST_SPEAKING",
    "LILY_TIER1_THRESHOLD_IDLE",
    "LILY_TIER1_THRESHOLD_OPEN_WINDOW",
    "LILY_TIER1_THRESHOLD_OVERLAP",
    "LILY_TIER1_THRESHOLD_SCORING",
    # Lobby / memory / pacing tunables.
    "LILY_AUTO_START_LOBBY_GRACE_SECONDS",
    "LILY_AUTO_START_MIN_PLAYERS",
    "LILY_CLARIFY_MAX_PER_SESSION",
    "LILY_GREETING_MEMORY_BUDGET_SECONDS",
    "LILY_INTAKE_SETTLE_SECONDS",
    "LILY_MEMORY_MIN_QUESTIONS",
    "LILY_RELAXED_WINDOW_MULTIPLIER",
    "LILY_SUPPLY_FALLBACK_SECONDS",
    "LILY_UNDELIVERED_RECONCILE_SECONDS",
    # WS-8 identity reconciliation tunables (default-backed).
    "LILY_ENROLL_RETRY_COOLDOWN_SECONDS",
    "LILY_GHOST_FOLD_WINDOW_SECONDS",
    # n-best ASR recovery tunables (default OFF — see lily_config.stt_*).
    "LILY_NBEST_DISPERSION_THRESHOLD",
    "LILY_STT_MAX_ALTERNATIVES",
    # Segment sanity gate S/L (WO-LILY-OMNIBUS-003 WS-10) — provisional
    # defaults in lily_config; WS-13's segmentation audit binds tuned
    # values (edit the defaults, or promote to deploy.yml vars).
    "LILY_SEGMENT_MAX_SPAN_SECONDS",
    "LILY_SEGMENT_MAX_FINALIZATION_LAG_SECONDS",
    # WS-11 garble clarify gate threshold.
    "LILY_GARBLE_CLARIFY_MIN_CONFIDENCE",
    # Misc: image model id (prefetch-only) and voice preset 1 (hardcoded
    # default ID, same pattern as Zuna's VOICE_NADIA).
    "LILY_IMAGEGEN_MODEL",
    "LILY_VOICE_1",
}

# Vars the workflow forwards into the container that have NO lily_config
# accessor because something OTHER than lily_config consumes them directly.
LILY_EXTERNAL_FORWARD_ENV = {
    # Read straight from the environment by livekit-plugins-speechmatics —
    # SpeechmaticsSTT(...) in lily_agent.py takes no api_key= argument, so
    # there is (correctly) no lily_config.speechmatics_api_key() accessor.
    "SPEECHMATICS_API_KEY",
}


# ---------------------------------------------------------------------------
# Parsers — read the real tree (like test_capability_lint introspects real
# code), so the lint reflects what would actually deploy.
# ---------------------------------------------------------------------------

def _required_env_vars() -> set:
    """Every env var name read through a lily_config accessor:
    _get / _get_int / _get_float / _get_bool / _require ."""
    src = _CONFIG.read_text()
    return set(
        re.findall(
            r'_(?:get|get_int|get_float|get_bool|require)\(\s*"([A-Z][A-Z0-9_]*)"',
            src,
        )
    )


def _deploy_env_forms() -> tuple:
    """Return (env_block, docker_shell) name sets parsed from deploy.yml.

    env_block  — job-step `NAME: ${{ secrets.NAME }}` / `${{ vars.NAME }}`
                 mappings (what the workflow plumbs into the step env).
    docker_shell — `-e NAME="${NAME}"` docker args that pass a same-named
                 shell var through to the container. Deliberately excludes
                 literal (`-e INPUT_OP="deploy"`) and `${{ github.* }}`
                 plugin-control args — those never carry a lily env value.
    """
    text = _DEPLOY.read_text()
    env_block = set(
        re.findall(
            r'^\s+([A-Z][A-Z0-9_]*):\s*\$\{\{\s*(?:secrets|vars)\.',
            text,
            re.M,
        )
    )
    docker_shell = {
        name
        for name, ref in re.findall(
            r'-e\s+([A-Z][A-Z0-9_]*)="\$\{([A-Z][A-Z0-9_]*)\}"', text
        )
        if name == ref
    }
    return env_block, docker_shell


def _forwarded_env_vars() -> set:
    """Vars that actually reach the running agent container: present in
    BOTH deploy.yml forms (a var in only one form expands to empty)."""
    env_block, docker_shell = _deploy_env_forms()
    return env_block & docker_shell


# ---------------------------------------------------------------------------
# Pure lint cores — exercised synthetically below with injected sets so the
# failure directions are proven without depending on the live tree.
# ---------------------------------------------------------------------------

def _missing_forwards(required, forwarded, local_only) -> list:
    return sorted(required - forwarded - local_only)


def _orphan_forwards(forwarded, required, external) -> list:
    return sorted(forwarded - required - external)


def _forwarding_inconsistencies(env_block, docker_shell) -> tuple:
    """(env-only, docker-only): plumbed one way but not the other."""
    return sorted(env_block - docker_shell), sorted(docker_shell - env_block)


# ---------------------------------------------------------------------------
# Real-tree tests.
# ---------------------------------------------------------------------------

def test_every_required_var_is_forwarded_or_local_only():
    offenders = _missing_forwards(
        _required_env_vars(), _forwarded_env_vars(), LILY_LOCAL_ONLY_ENV
    )
    assert not offenders, (
        f"lily_config reads these env vars but deploy.yml does not forward "
        f"them: {offenders}. Two legal fixes: (1) production needs it → add "
        "the var to deploy.yml (both the job `env:` mapping and the docker "
        "`-e NAME=\"${NAME}\"` arg); (2) it ships on its in-code default → "
        "add it to LILY_LOCAL_ONLY_ENV with a reason."
    )


def test_every_forward_has_a_consumer():
    offenders = _orphan_forwards(
        _forwarded_env_vars(), _required_env_vars(), LILY_EXTERNAL_FORWARD_ENV
    )
    assert not offenders, (
        f"deploy.yml forwards these vars into the container but nothing "
        f"consumes them: {offenders}. Two legal fixes: (1) it should be "
        "read → add a lily_config accessor; (2) a plugin/the deploy "
        "container reads it directly → add it to LILY_EXTERNAL_FORWARD_ENV "
        "with a reason."
    )


def test_deploy_forms_agree():
    env_block, docker_shell = _deploy_env_forms()
    env_only, docker_only = _forwarding_inconsistencies(env_block, docker_shell)
    assert not env_only, (
        f"vars mapped in deploy.yml's job `env:` but never passed to the "
        f"container via `-e NAME=\"${{NAME}}\"`: {env_only} (silently dropped)."
    )
    assert not docker_only, (
        f"vars passed via docker `-e NAME=\"${{NAME}}\"` but never mapped "
        f"from secrets/vars in the job `env:` block: {docker_only} (expands "
        "to empty in the container)."
    )


def test_local_only_and_forwarded_are_disjoint():
    # A var can't both be intentionally-not-forwarded and forwarded.
    both = sorted(LILY_LOCAL_ONLY_ENV & _forwarded_env_vars())
    assert not both, f"declared LOCAL_ONLY yet forwarded in deploy.yml: {both}"


def test_external_forward_has_no_accessor():
    # An external-forward entry must genuinely lack a lily_config accessor —
    # otherwise it belongs to the normal required set, not the escape hatch.
    leaked = sorted(LILY_EXTERNAL_FORWARD_ENV & _required_env_vars())
    assert not leaked, (
        f"declared EXTERNAL_FORWARD but lily_config DOES read it: {leaked} — "
        "drop it from LILY_EXTERNAL_FORWARD_ENV."
    )


# ---------------------------------------------------------------------------
# The WO's verify criteria, exercised synthetically (no vacuous needles):
# both failure directions fail BY NAME on a dummy, and clear when excused.
# ---------------------------------------------------------------------------

def test_lint_names_a_missing_required_var():
    offenders = _missing_forwards(
        _required_env_vars() | {"LILY_DUMMY_UNFORWARDED"},
        _forwarded_env_vars(),
        LILY_LOCAL_ONLY_ENV,
    )
    assert offenders == ["LILY_DUMMY_UNFORWARDED"]


def test_lint_clears_the_missing_var_once_local_only():
    offenders = _missing_forwards(
        _required_env_vars() | {"LILY_DUMMY_UNFORWARDED"},
        _forwarded_env_vars(),
        LILY_LOCAL_ONLY_ENV | {"LILY_DUMMY_UNFORWARDED"},
    )
    assert offenders == []


def test_lint_names_an_orphan_forward():
    offenders = _orphan_forwards(
        _forwarded_env_vars() | {"LILY_DUMMY_ORPHAN"},
        _required_env_vars(),
        LILY_EXTERNAL_FORWARD_ENV,
    )
    assert offenders == ["LILY_DUMMY_ORPHAN"]


def test_lint_clears_the_orphan_once_external():
    offenders = _orphan_forwards(
        _forwarded_env_vars() | {"LILY_DUMMY_ORPHAN"},
        _required_env_vars(),
        LILY_EXTERNAL_FORWARD_ENV | {"LILY_DUMMY_ORPHAN"},
    )
    assert offenders == []


def test_xai_api_key_is_forwarded_and_required():
    # The concrete incident var: it IS a required accessor AND it IS wired
    # in deploy.yml, so the lint passes it clean (the lint proves wiring,
    # not GitHub-secret existence — see module docstring SCOPE).
    assert "XAI_API_KEY" in _required_env_vars()
    assert "XAI_API_KEY" in _forwarded_env_vars()
