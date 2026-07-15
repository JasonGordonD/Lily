-- 016: bounded server-side question draw filters.

create index if not exists lily_questions_draw_idx
  on lily_questions (status, adult, category, difficulty_tier, id);
