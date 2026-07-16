-- 017: answer-level no-repeat (live 2026-07-15 20:42: "you asked me the
-- gold question every time I played" — generated questions dodge the
-- text-hash dedup by rewording, so the same ANSWER recurs across
-- sessions). The served-question ledger now records the canonical
-- answer; supply excludes recent answers per group.

alter table lily_asked_history
  add column if not exists canonical_answer text;
