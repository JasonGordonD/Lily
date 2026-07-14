-- LILY schema — Part II §5. Six tables, prefix lily_, lowercase, underscores.
-- No pgvector tables in v1.

-- Session state, phase, mode flag, scorekeeper snapshots, final standings.
CREATE TABLE IF NOT EXISTS lily_sessions (
    session_id         text PRIMARY KEY,
    group_id           text,
    phase              text DEFAULT 'lobby',
    mode               text DEFAULT 'general',
    round              integer DEFAULT 0,
    question_number    integer DEFAULT 0,
    scorekeeper_state  jsonb DEFAULT '{}'::jsonb,
    final_standings    jsonb,
    metadata           jsonb DEFAULT '{}'::jsonb,
    created_at         timestamptz DEFAULT now(),
    updated_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lily_sessions_group
    ON lily_sessions (group_id);

-- Every utterance, timestamped, speaker-tagged (lbs_transcripts shape).
CREATE TABLE IF NOT EXISTS lily_transcripts (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id     text NOT NULL,
    speaker_label  text,
    speaker_name   text,
    text           text NOT NULL,
    segment_start  double precision,
    segment_end    double precision,
    created_at     timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lily_transcripts_session
    ON lily_transcripts (session_id);

-- Passive voiceprint enrollment. group_id is TEXT, not UUID.
CREATE TABLE IF NOT EXISTS lily_speaker_voiceprints (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id             text NOT NULL,
    speaker_label        text NOT NULL,
    player_name          text,
    speaker_identifiers  jsonb,
    created_at           timestamptz DEFAULT now(),
    updated_at           timestamptz DEFAULT now(),
    UNIQUE (group_id, speaker_label)
);

CREATE INDEX IF NOT EXISTS idx_lily_speaker_voiceprints_group
    ON lily_speaker_voiceprints (group_id);

-- Optional curated question bank.
CREATE TABLE IF NOT EXISTS lily_questions (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category         text,
    question         text NOT NULL,
    answer           text NOT NULL,
    difficulty_tier  integer,
    source           text,
    created_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lily_questions_category
    ON lily_questions (category);

-- Audit log: every adjudicated attempt.
CREATE TABLE IF NOT EXISTS lily_answers (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      text NOT NULL,
    player_name     text,
    question_id     text,
    question_index  integer,
    transcript      text,
    verdict         text,
    eval_tier       integer,
    awarded_points  integer DEFAULT 0,
    ts              timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lily_answers_session
    ON lily_answers (session_id);

-- Per-player lobby facts + running-bit material, persisted for rematches.
CREATE TABLE IF NOT EXISTS lily_group_facts (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id           text NOT NULL,
    player_name        text,
    fact               text NOT NULL,
    source_session_id  text,
    created_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lily_group_facts_group
    ON lily_group_facts (group_id);
