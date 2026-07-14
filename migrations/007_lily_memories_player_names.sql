-- 007: lily_memories.player_names — the normalized sorted player-name set
-- ("carly","kali","rami") stored alongside each session memory for
-- group-identity audit (WO-LILY-MEMORY-CLOSEOUT-001 Task 3). Written by
-- lily_memory.lily_write_session_memory; the same set feeds the name-set
-- group-id hash (grp_<sha1 of "carly|kali|rami">), so this column is the
-- audit trail for how a session's group id was derived.

ALTER TABLE lily_memories
    ADD COLUMN IF NOT EXISTS player_names text[];

CREATE INDEX IF NOT EXISTS idx_lily_memories_player_names
    ON lily_memories USING gin (player_names);
