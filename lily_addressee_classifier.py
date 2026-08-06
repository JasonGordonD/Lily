"""
lily_addressee_classifier.py — per-utterance ADDRESSEE CLASSIFIER
(WO-LILY-FLOOR-001 FL-1).

Pure stdlib module (lily_addressee-style): fuses three signal families
into one host-directed score per finalized utterance, BEFORE any reply is
generated, so the reply is conditioned on the judgment instead of
implying it afterward:

  1. Deterministic priors (game state) — open answer window +
     expectation-primed match on the active registered question is
     host-directed BY DEFINITION, no name needed. Idle/vamp flips the
     default toward side chatter unless named or command-shaped.
     Adjacency to a Lily prompt biases host-directed. Rapid
     player-to-player alternation with content cohering BETWEEN the
     players locks a SIDE-CLUSTER: utterances inside it classify as a
     cluster, not one by one, until the cluster breaks.
  2. Name evidence (not a wake word) — "Lily" anywhere spikes
     host-directed probability; there is no gating phrase and answers
     never require the name. Vocative ("Carry on, Lily") is address;
     referential ("Lily is a joke") is third-person talk ABOUT her and
     mild side-chatter evidence.
  3. Acoustic register (Omnibus-003 WS-11/WS-13 feature surface) —
     device-directed speech runs louder, slower, more articulated,
     mic-oriented; side chatter quieter and faster. The
     LilyAcousticRegister dataclass is the narrow feature interface: the
     WS-11/WS-13 surfaces feed it when present, fixture-recorded features
     drive it in replay tests, and every field is optional so an absent
     pipeline degrades to priors + name.

Downstream contract (FL-2 floor-state machine, FL-4 answer-register
handling): consume LilyAddresseeJudgment — classification, fused score,
per-family components, side-cluster lock/extend/break events and cluster
id. lily_agent.py owns the wiring (game state in, judgment stored on the
game and written to lily_addressee_log.agent_classification per
utterance); this module never imports livekit or supabase.
"""

import json
import re as _re
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# -- classification vocabulary ------------------------------------------------
# Every judgment lands on exactly one of these — never null.

CLASS_HOST_DIRECTED = "host_directed"
CLASS_SIDE_CHATTER = "side_chatter"
CLASS_SIDE_CLUSTER = "side_cluster"

# -- name evidence vocabulary -------------------------------------------------

NAME_VOCATIVE = "vocative"       # "Carry on, Lily." — address
NAME_REFERENTIAL = "referential" # "Lily is a joke" — talk ABOUT her
NAME_MENTION = "mention"         # name present, position inconclusive
NAME_NONE = "none"

# -- cluster events -----------------------------------------------------------

CLUSTER_LOCK = "lock"
CLUSTER_EXTEND = "extend"
CLUSTER_BREAK = "break"


# -----------------------------------------------------------------------------
# Name evidence — vocative vs referential at the language layer
# -----------------------------------------------------------------------------

_DIAR_TAG_RE = _re.compile(r"^\s*\[S\d+\]\s*")

# Name-as-subject / name-as-object: third-person talk about her.
_REFERENTIAL_RE = _re.compile(
    r"\blily(?:'s)?\s+"
    r"(?:is|was|isn't|wasn't|has|had|says|said|thinks|thought|got|gets|"
    r"does|doesn't|did|didn't|will|would|won't|can|can't|could|keeps|kept|"
    r"sounds|sounded|looks|looked|seems|seemed|just|always|never|butted|"
    r"talks|talked|interrupted|jumped)\b"
    r"|\b(?:about|of)\s+lily\b"
)

# Address: name at a clause boundary (comma/period-adjacent, start, end,
# or after a greeting/imperative lead-in).
_VOCATIVE_RE = _re.compile(
    r"(?:^|[,.;!?]\s*)lily\b(?=\s*(?:[,.;:!?]|$))"
    r"|\blily\s*[,!?]"
    r"|\b(?:hey|hi|yo|ok|okay|thanks|thank you|please|stop|quiet|hush|"
    r"go ahead|carry on|come on)\s*[,.]?\s*lily\b"
    r"|^lily\b"
)

_NAME_RE = _re.compile(r"\blily\b")

# Floor-hold declaration: an utterance ADDRESSED to Lily whose content
# asserts the table holds the floor. Both 81BCB0 derailment corrections
# are this shape ("Okay. Carry on. Lily. We're not talking to you." /
# "Carry on. Lily. We're having a conversation."): host-directed speech
# that DECLARES a side conversation — it locks/sustains the cluster
# instead of breaking it. "Carry on" alone is NOT a hold (Rami also uses
# it as plain "proceed"); the hold is carried by the we-clause.
_FLOOR_HOLD_RE = _re.compile(
    r"\b(?:we'?re?|we are)\s+not\s+talking\s+to\s+you\b"
    r"|\bnot\s+talking\s+to\s+you\b"
    r"|\b(?:we'?re?|we are)\s+having\s+a\s+conversation\b"
    r"|\b(?:we'?re?|we are)\s+talking\s+to\s+each\s+other\b"
    r"|\b(?:we'?re?|we are)\s+in\s+the\s+middle\s+of\s+something\b"
    r"|\bgive\s+us\s+a\s+(?:minute|moment|second|sec)\b"
)

# Table-address: speech aimed at the OTHER PLAYERS as a group ("Have you
# guys seen Loki?") — player-to-player evidence at the language layer,
# and the solo-run cluster anchor (diarization only captures the audible
# side of a conversation; a solo-attributed run addressing the table is
# one side of a multi-person exchange).
_TABLE_ADDRESS_RE = _re.compile(
    r"\byou guys\b|\by'?all\b|\byou two\b|\byou both\b|\bguys\b|\beverybody\b"
)


def lily_floor_hold(text: str) -> bool:
    """Whether the utterance declares the table holds the floor."""
    return _FLOOR_HOLD_RE.search(_normalize_for_name(text)) is not None


def lily_table_address(text: str) -> bool:
    """Whether the utterance addresses the other players as a group."""
    return _TABLE_ADDRESS_RE.search(_normalize_for_name(text)) is not None


def _normalize_for_name(text: str) -> str:
    return _DIAR_TAG_RE.sub("", text or "").strip().lower()


def lily_name_evidence(text: str) -> str:
    """Classify how the utterance uses the host's name.

    Referential wins over vocative when both patterns fire ("Lily, Lily
    is a joke" is still talk about her); a bare hit with inconclusive
    position is a MENTION (still host evidence — the WO spikes
    host-directed probability on the name anywhere).
    """
    normalized = _normalize_for_name(text)
    if not _NAME_RE.search(normalized):
        return NAME_NONE
    if _REFERENTIAL_RE.search(normalized):
        return NAME_REFERENTIAL
    if _VOCATIVE_RE.search(normalized):
        return NAME_VOCATIVE
    return NAME_MENTION


# -----------------------------------------------------------------------------
# Acoustic register — the WS-11/WS-13 feature interface
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class LilyAcousticRegister:
    """Narrow per-utterance acoustic feature interface (all fields 0..1,
    all optional). WS-11 supplies arousal/energy, WS-13 per-word volume;
    until those surfaces land, fixture-recorded features drive this in
    replay tests and a missing pipeline yields None throughout."""
    arousal: Optional[float] = None          # table heat — high reads side
    energy: Optional[float] = None           # loudness — device-directed runs louder
    speech_rate: Optional[float] = None      # normalized rate — side runs faster
    articulation: Optional[float] = None     # device-directed more articulated
    mic_orientation: Optional[float] = None  # toward-mic score
    per_word_volume: Optional[tuple] = None  # WS-13 per-word volume series

    @classmethod
    def from_snapshot(cls, snapshot) -> Optional["LilyAcousticRegister"]:
        """Best-effort adapter from the live audEERING addressee snapshot
        (lily_audeering_consumers.addressee_snapshot() shape). Fields the
        pipeline does not deliver yet stay None; when WS-11/WS-13 write
        their features into the snapshot they flow through unchanged."""
        if not isinstance(snapshot, dict):
            return None
        dimension = snapshot.get("dimension") or {}
        prosody = snapshot.get("prosody") or {}
        features = snapshot.get("features") or {}

        def _f(*candidates) -> Optional[float]:
            for source, key in candidates:
                try:
                    v = float(source.get(key))
                except (TypeError, ValueError, AttributeError):
                    continue
                if 0.0 <= v <= 1.0:
                    return v
            return None

        volumes = None
        raw_volumes = (
            features.get("per_word_volume")
            or features.get("word_volumes")
            or prosody.get("per_word_volume")
        )
        if isinstance(raw_volumes, (list, tuple)) and raw_volumes:
            try:
                volumes = tuple(
                    max(0.0, min(1.0, float(v))) for v in raw_volumes
                )
            except (TypeError, ValueError):
                volumes = None
        register = cls(
            arousal=_f((dimension, "arousal")),
            energy=_f(
                (prosody, "loudness"), (prosody, "energy"),
                (features, "energy"),
            ),
            speech_rate=_f(
                (prosody, "speech_rate"), (prosody, "speaking_rate"),
                (prosody, "articulation_rate"),
            ),
            articulation=_f(
                (prosody, "articulation"), (features, "articulation"),
            ),
            mic_orientation=_f(
                (prosody, "mic_orientation"), (features, "mic_orientation"),
            ),
            per_word_volume=volumes,
        )
        if all(
            getattr(register, f) is None
            for f in (
                "arousal", "energy", "speech_rate", "articulation",
                "mic_orientation", "per_word_volume",
            )
        ):
            return None
        return register


def lily_register_score(
    register: Optional[LilyAcousticRegister],
) -> Optional[float]:
    """Host-directedness read of the acoustic register, 0..1 (0.5 =
    neutral). Louder / slower / more articulated / mic-oriented cues pull
    up; quiet-and-fast (side chatter) and high arousal pull down. None
    when no features are available — fusion then runs on priors + name."""
    if register is None:
        return None
    cues = []
    if register.energy is not None:
        cues.append(register.energy)
    if register.per_word_volume:
        cues.append(sum(register.per_word_volume) / len(register.per_word_volume))
    if register.speech_rate is not None:
        cues.append(1.0 - register.speech_rate)
    if register.articulation is not None:
        cues.append(register.articulation)
    if register.mic_orientation is not None:
        cues.append(register.mic_orientation)
    if register.arousal is not None:
        cues.append(1.0 - register.arousal)
    if not cues:
        return None
    return round(sum(cues) / len(cues), 3)


# -----------------------------------------------------------------------------
# Content cohesion — does this utterance cohere with the previous one?
# -----------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "the a an and but or so is was are were be been being to of in on at "
    "for with that this these those it its it's i you we they he she him "
    "her them my your our their me us do did does not no yes have has had "
    "what who when where why how".split()
)

_REPLY_OPENER_RE = _re.compile(
    r"^(?:yeah|yes|yep|no|nah|nope|right|exactly|true|same|honestly|"
    r"i mean|me too|but|well|okay|ok|so|and|totally|for real|seriously|"
    r"wait|oh)\b"
)


def _content_tokens(text: str) -> set:
    lowered = _re.sub(r"[^a-z0-9\s']+", " ", (text or "").lower())
    return {
        tok for tok in lowered.split()
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def lily_content_cohesion(prev_text: Optional[str], text: str) -> bool:
    """Player-to-player content cohesion: a shared content token with the
    previous utterance, or a reply-shaped opener. Deterministic — this is
    the 'content cohering BETWEEN the players' signal for cluster lock."""
    if not prev_text:
        return False
    normalized = _normalize_for_name(text)
    if _REPLY_OPENER_RE.match(normalized):
        return True
    return bool(_content_tokens(prev_text) & _content_tokens(text))


# -----------------------------------------------------------------------------
# Structured types — the FL-2 / FL-4 consumption surface
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class LilyUtteranceSignals:
    """Per-utterance classifier inputs. lily_agent.py assembles these from
    live game state; fixture tests build them from the recorded session."""
    text: str
    speaker_label: Optional[str]
    ts: float
    window_open: bool = False
    # Tier-1 expectation-primed match against the ACTIVE registered
    # question (any fuzzy hit — the definitional host rule wants the raw
    # match, not the dispersion-gated verdict).
    expectation_match: bool = False
    # Scorekeeper phase surface: "idle" | "vamp" | "lobby" | "question" ...
    phase: str = "idle"
    # Scorekeeper control-command detection (skip / next / repeat ...).
    command_shaped: bool = False
    # Seconds since Lily's last finished prompt; None = use the
    # classifier's own note_agent_prompt anchor.
    seconds_since_agent_prompt: Optional[float] = None
    register: Optional[LilyAcousticRegister] = None


@dataclass(frozen=True)
class LilyAddresseeJudgment:
    """One per finalized utterance, emitted BEFORE reply generation.
    FL-2 (floor-state machine) keys on classification + cluster events;
    FL-4 (answer-register handling) additionally reads score/components."""
    classification: str            # CLASS_* — never null
    score: float                   # fused host-directed probability 0..1
    components: dict               # {"prior","name","acoustic"} contributions
    name_evidence: str             # NAME_*
    cluster_id: Optional[int]      # active side-cluster id, when inside one
    cluster_event: Optional[str]   # CLUSTER_LOCK / _EXTEND / _BREAK / None
    reason: str                    # deterministic short tag for telemetry
    speaker_label: Optional[str]
    ts: float

    def row_fields(self) -> dict:
        """lily_addressee_log column payload for this judgment."""
        return {
            "agent_classification": self.classification,
            "addressee_score": self.score,
            "addressee_score_components": {
                **self.components,
                "name_evidence": self.name_evidence,
                "reason": self.reason,
            },
            "side_cluster_id": self.cluster_id,
            "side_cluster_event": self.cluster_event,
        }

    def log_json(self) -> str:
        """Structured emission payload for the pre-reply telemetry line."""
        return json.dumps(
            {
                "classification": self.classification,
                "score": self.score,
                "components": self.components,
                "name_evidence": self.name_evidence,
                "cluster_id": self.cluster_id,
                "cluster_event": self.cluster_event,
                "reason": self.reason,
                "speaker": self.speaker_label,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass
class _RecentUtterance:
    ts: float
    speaker: Optional[str]
    text: str
    side_leaning: bool
    cohesive_with_prev: bool


# -----------------------------------------------------------------------------
# The classifier
# -----------------------------------------------------------------------------

def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class LilyAddresseeClassifier:
    """Per-session addressee classifier. Stateful for adjacency (time
    since Lily's last prompt) and the side-cluster machine; classify()
    itself is deterministic given state + signals."""

    def __init__(
        self,
        *,
        host_threshold: float = 0.60,
        adjacency_seconds: float = 4.0,
        # Idle default (0.35) + adjacency lands AT the host threshold:
        # speech immediately following a Lily prompt defaults
        # host-directed, and the acoustic register can still pull a
        # quiet-and-fast aside back below it.
        adjacency_bonus: float = 0.25,
        cluster_min_utterances: int = 3,
        cluster_min_speakers: int = 2,
        # 81BCB0 ground truth: the feedback-beat run carries a 12.5s
        # intra-run gap (one 11-second utterance); the break gap must sit
        # clearly above the intra-run bound.
        cluster_max_gap_seconds: float = 15.0,
        cluster_break_gap_seconds: float = 25.0,
        acoustic_weight: float = 0.3,
    ) -> None:
        self.host_threshold = host_threshold
        self.adjacency_seconds = adjacency_seconds
        self.adjacency_bonus = adjacency_bonus
        self.cluster_min_utterances = cluster_min_utterances
        self.cluster_min_speakers = cluster_min_speakers
        self.cluster_max_gap_seconds = cluster_max_gap_seconds
        self.cluster_break_gap_seconds = cluster_break_gap_seconds
        self.acoustic_weight = acoustic_weight
        self._last_agent_prompt_ts: Optional[float] = None
        self._recent: deque = deque(maxlen=8)
        self._cluster_counter: int = 0
        self._active_cluster: Optional[dict] = None
        self.last_judgment: Optional[LilyAddresseeJudgment] = None

    # -- adjacency + cluster anchors -----------------------------------------

    def note_agent_prompt(self, ts: float) -> None:
        """Lily finished a prompt/turn at ts: adjacency anchor for
        speech-follows-prompt bias. A Lily turn also supersedes any live
        side-cluster — the floor context it locked on is gone."""
        self._last_agent_prompt_ts = ts
        self._active_cluster = None
        self._recent.clear()

    # -- scoring -------------------------------------------------------------

    def _prior_score(self, signals: LilyUtteranceSignals) -> float:
        if signals.window_open and signals.expectation_match:
            return 0.95  # host-directed by definition
        if signals.window_open:
            base = 0.65
        elif signals.phase in ("idle", "vamp", "lobby", "wrapup"):
            base = 0.35   # default flips toward side chatter
        else:
            base = 0.45
        if signals.command_shaped:
            base += 0.25
        since = signals.seconds_since_agent_prompt
        if since is None and self._last_agent_prompt_ts is not None:
            since = signals.ts - self._last_agent_prompt_ts
        if since is not None and 0.0 <= since <= self.adjacency_seconds:
            base += self.adjacency_bonus
        return _clamp01(base)

    @staticmethod
    def _name_delta(name_evidence: str) -> float:
        if name_evidence == NAME_VOCATIVE:
            return 0.35
        if name_evidence == NAME_MENTION:
            return 0.20
        if name_evidence == NAME_REFERENTIAL:
            return -0.15
        return 0.0

    def _acoustic_delta(self, register_score: Optional[float]) -> float:
        if register_score is None:
            return 0.0
        return (register_score - 0.5) * 2.0 * self.acoustic_weight

    # -- cluster machine -----------------------------------------------------

    def _cluster_expired(self, ts: float) -> bool:
        return (
            self._active_cluster is not None
            and ts - self._active_cluster["last_ts"]
            > self.cluster_break_gap_seconds
        )

    def _trailing_run(self) -> list:
        """Trailing gap-bounded run of side-leaning utterances, newest
        first."""
        run: list = []
        prev_ts: Optional[float] = None
        for entry in reversed(self._recent):
            if not entry.side_leaning:
                break
            if prev_ts is not None and prev_ts - entry.ts > self.cluster_max_gap_seconds:
                break
            run.append(entry)
            prev_ts = entry.ts
        return run

    def _lock_cluster(self, ts: float, speakers: set, started_ts: float) -> int:
        self._cluster_counter += 1
        self._active_cluster = {
            "id": self._cluster_counter,
            "speakers": set(speakers),
            "last_ts": ts,
            "started_ts": started_ts,
        }
        return self._cluster_counter

    def _try_lock(self, ts: float) -> Optional[int]:
        """Lock a side-cluster off the trailing run of side-leaning,
        gap-bounded utterances. Two routes:

        - alternation: enough utterances from >=2 distinct speakers with
          content cohering between them;
        - solo run (81BCB0 reality — diarization captures only the
          audible side of a conversation): enough utterances from one
          voice, anchored by a table-address ("Have you guys seen
          Loki?") or fully-cohering links.
        """
        run = self._trailing_run()
        if len(run) < self.cluster_min_utterances:
            return None
        speakers = {e.speaker for e in run}
        links = len(run) - 1
        cohesive = sum(1 for e in run[:-1] if e.cohesive_with_prev)
        if len(speakers) >= self.cluster_min_speakers:
            if cohesive < max(1, (links + 1) // 2):
                return None
        else:
            table_address = any(lily_table_address(e.text) for e in run)
            if not table_address and cohesive < links:
                return None
        return self._lock_cluster(ts, speakers, run[-1].ts)

    def _declare_cluster(self, ts: float) -> Optional[int]:
        """A floor-hold declaration locks the cluster the declarer is
        protecting: the trailing side run (any length) becomes the
        cluster. None when there is no RECENT side speech to protect —
        the run must reach into the declaration's own gap window."""
        run = self._trailing_run()
        if not run or ts - run[0].ts > self.cluster_max_gap_seconds:
            return None
        speakers = {e.speaker for e in run}
        return self._lock_cluster(ts, speakers, run[-1].ts)

    # -- the judgment --------------------------------------------------------

    def classify(self, signals: LilyUtteranceSignals) -> LilyAddresseeJudgment:
        name_evidence = lily_name_evidence(signals.text)
        register_score = lily_register_score(signals.register)
        prior = self._prior_score(signals)
        name_delta = self._name_delta(name_evidence)
        acoustic_delta = self._acoustic_delta(register_score)
        score = _clamp01(prior + name_delta + acoustic_delta)
        components = {
            "prior": round(prior, 3),
            "name": round(name_delta, 3),
            "acoustic": (
                round(acoustic_delta, 3) if register_score is not None else None
            ),
        }

        if self._cluster_expired(signals.ts):
            self._active_cluster = None

        cluster_id: Optional[int] = None
        cluster_event: Optional[str] = None
        floor_hold = lily_floor_hold(signals.text)

        # Hard host rules. Definitional and plain vocative/command break a
        # live side-cluster; a FLOOR-HOLD declaration ("Carry on, Lily.
        # We're not talking to you.") is host-directed speech that ASSERTS
        # the side conversation — it locks/sustains the cluster instead.
        definitional = signals.window_open and signals.expectation_match
        if definitional:
            if self._active_cluster is not None:
                cluster_id = self._active_cluster["id"]
                cluster_event = CLUSTER_BREAK
                self._active_cluster = None
            score = max(score, 0.95)
            reason = "window+match"
            classification = CLASS_HOST_DIRECTED
        elif floor_hold:
            score = max(score, self.host_threshold)
            reason = "floor-hold"
            classification = CLASS_HOST_DIRECTED
            if self._active_cluster is not None:
                cluster = self._active_cluster
                cluster["last_ts"] = signals.ts
                cluster["speakers"].add(signals.speaker_label)
                cluster_id = cluster["id"]
                cluster_event = CLUSTER_EXTEND
            else:
                declared = self._declare_cluster(signals.ts)
                if declared is not None:
                    self._active_cluster["speakers"].add(
                        signals.speaker_label
                    )
                    cluster_id = declared
                    cluster_event = CLUSTER_LOCK
        elif name_evidence == NAME_VOCATIVE or signals.command_shaped:
            if self._active_cluster is not None:
                cluster_id = self._active_cluster["id"]
                cluster_event = CLUSTER_BREAK
                self._active_cluster = None
            score = max(score, self.host_threshold)
            reason = (
                "vocative" if name_evidence == NAME_VOCATIVE else "command"
            )
            classification = CLASS_HOST_DIRECTED
        elif self._active_cluster is not None:
            # Inside a locked cluster: utterances classify as a cluster,
            # not one by one, until the cluster breaks.
            cluster = self._active_cluster
            cluster["last_ts"] = signals.ts
            cluster["speakers"].add(signals.speaker_label)
            cluster_id = cluster["id"]
            cluster_event = CLUSTER_EXTEND
            classification = CLASS_SIDE_CLUSTER
            reason = "cluster-extend"
        elif score >= self.host_threshold:
            classification = CLASS_HOST_DIRECTED
            reason = "score"
        else:
            classification = CLASS_SIDE_CHATTER
            reason = "score"

        prev = self._recent[-1] if self._recent else None
        self._recent.append(
            _RecentUtterance(
                ts=signals.ts,
                speaker=signals.speaker_label,
                text=signals.text,
                # A floor-hold is host-classified but belongs to the
                # table's conversation by its own claim — it must not
                # reset the side-run bookkeeping.
                side_leaning=(
                    classification != CLASS_HOST_DIRECTED or floor_hold
                ),
                cohesive_with_prev=lily_content_cohesion(
                    prev.text if prev else None, signals.text
                ),
            )
        )

        if classification == CLASS_SIDE_CHATTER and self._active_cluster is None:
            locked = self._try_lock(signals.ts)
            if locked is not None:
                cluster_id = locked
                cluster_event = CLUSTER_LOCK
                classification = CLASS_SIDE_CLUSTER
                reason = "cluster-lock"

        judgment = LilyAddresseeJudgment(
            classification=classification,
            score=round(score, 3),
            components=components,
            name_evidence=name_evidence,
            cluster_id=cluster_id,
            cluster_event=cluster_event,
            reason=reason,
            speaker_label=signals.speaker_label,
            ts=signals.ts,
        )
        self.last_judgment = judgment
        return judgment
