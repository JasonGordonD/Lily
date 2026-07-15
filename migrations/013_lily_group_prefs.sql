-- LILY migration 013 — persistent per-group preferences (group prefs WO).
--
-- One row per group: the OPAQUE prefs dict. Keys are feature-owned — this
-- WO writes {"pacing": "timed" | "relaxed"}; round_format / media_mode land
-- with their own features post-merge and slot into the SAME jsonb without a
-- schema change. Persisted as a whole-dict upsert (on_conflict=group_id) on
-- every preference change.
--
-- CRITICAL INTERLOCK: a forgotten table's preferences are recognition data
-- — this table is HARD-DELETED by the forget cascade
-- (lily_forget.HARD_DELETE_GROUP_TABLES) and re-keyed on a mid-session
-- group-id upgrade (lily_persistence.lily_rekey_group).
CREATE TABLE IF NOT EXISTS lily_group_prefs (
    group_id    text PRIMARY KEY,
    prefs       jsonb NOT NULL DEFAULT '{}',
    updated_at  timestamptz DEFAULT now()
);
