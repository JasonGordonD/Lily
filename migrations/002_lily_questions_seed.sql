-- 002_lily_questions_seed.sql — curated demo insurance bank (runbook: seed 30
-- questions BEFORE the demo; flip supply with LILY_KB_ONLY=1).
-- Adds the two bank columns the structured question shape (§4.2) carries.

ALTER TABLE lily_questions ADD COLUMN IF NOT EXISTS acceptable_answers jsonb;
ALTER TABLE lily_questions ADD COLUMN IF NOT EXISTS reveal_color text;

-- lily_questions curated seed · curated_v1 · 30 questions
-- Target table (Part II §5): lily_questions
--   (category text, question text, answer text, acceptable_answers jsonb,
--    difficulty_tier int, reveal_color text, source text default 'curated_v1')
-- Plain column list below: if the final schema differs slightly, adjust the
-- column list once — every VALUES row follows the same order.
-- Tiers: 1 ≈ 65% table success (8), 2 ≈ 50-55% (14), 3 ≈ 40-45% (8).
-- CLOSEST-NUMBER questions (nearest-guess-wins semantics, see per-row comments):
--   the piano-keys and skeleton-bones rows. Their acceptable_answers hold exact
--   forms of the canonical number only; adjudication is nearest guess, not match.
-- TWO-STEP questions (obscure clue first, broad clue second):
--   Tanzania, Breaking Bad, Freddie Mercury rows.

INSERT INTO lily_questions (category, question, answer, acceptable_answers, difficulty_tier, reveal_color, source) VALUES
-- ── Tier 1 ──────────────────────────────────────────────────────────────
('academic',    'The Great Pyramids of Giza stand just outside Cairo, in this North African country.',
 'Egypt', '["egypt"]', 1,
 'Egypt — the Nile''s been photobombing postcards for five thousand years.', 'curated_v1'),

('pop_culture', 'He yelled ''I''m the king of the world'' from the bow of the Titanic — name this Oscar-winning American actor.',
 'Leonardo DiCaprio', '["dicaprio", "leonardo dicaprio", "leo dicaprio", "leonardo di caprio", "leo"]', 1,
 'DiCaprio — the ship sank, the career didn''t.', 'curated_v1'),

('wordplay',    'It can hold your money or hold back a river — name this double-duty little word.',
 'bank', '["bank", "a bank", "the bank"]', 1,
 'Bank — where your money and the river both sit.', 'curated_v1'),

('lifestyle',   'Margherita, pepperoni, and four-cheese are all classic versions of this baked Italian dish.',
 'pizza', '["pizza", "a pizza"]', 1,
 'Pizza — the only pie that counts as dinner.', 'curated_v1'),

('academic',    'His face is on the one-dollar bill, and the job was invented for him — name this very first American president.',
 'George Washington', '["washington", "george washington"]', 1,
 'Washington — first in war, first in peace, first on the single.', 'curated_v1'),

('pop_culture', '''Hello,'' ''Rolling in the Deep,'' ''Someone Like You'' — name this one-named British powerhouse singer.',
 'Adele', '["adele", "adel"]', 1,
 'Adele — one name, several million broken hearts.', 'curated_v1'),

('wordplay',    'It follows butter, dragon, and fire to make three different insects — name this tiny airborne word.',
 'fly', '["fly", "a fly"]', 1,
 'Fly — butter, dragon, fire: same wings, different outfits.', 'curated_v1'),

('lifestyle',   'Birdies, bogeys, and the occasional hole-in-one — name this quiet club-and-ball sport.',
 'golf', '["golf"]', 1,
 'Golf — the only sport where silence gets a scoreboard.', 'curated_v1'),

-- ── Tier 2 ──────────────────────────────────────────────────────────────
('academic',    'In 79 AD it buried the Roman city of Pompeii in ash — name this infamous Italian volcano.',
 'Mount Vesuvius', '["vesuvius", "mount vesuvius", "mt vesuvius"]', 2,
 'Vesuvius — still looming over Naples, still not sorry.', 'curated_v1'),

('pop_culture', 'Keanu Reeves'' hacker takes the red pill and drops ''Mr. Anderson'' for this short chosen name.',
 'Neo', '["neo"]', 2,
 'Neo — an anagram of ''one,'' and he was the One.', 'curated_v1'),

('wordplay',    'It can come before board, walk, and show — name this directional little word.',
 'side', '["side"]', 2,
 'Side — board it, walk it, or run away with the show.', 'curated_v1'),

('lifestyle',   'Sushi rolls come wrapped in sheets of dried seaweed known by this short Japanese name.',
 'nori', '["nori", "norry", "noori"]', 2,
 'Nori — the ocean''s answer to gift wrap.', 'curated_v1'),

-- two-step: obscure clue (Dodoma) first, broad clue (Kilimanjaro) second
('academic',    'Its capital is Dodoma, not Dar es Salaam — and Kilimanjaro rises inside it — name this East African country.',
 'Tanzania', '["tanzania"]', 2,
 'Tanzania — you can admire Kilimanjaro from Kenya, but it lives in Tanzania.', 'curated_v1'),

('pop_culture', 'Before going solo, Beyoncé rose to fame fronting this chart-topping R&B girl group.',
 'Destiny''s Child', '["destiny''s child", "destinys child", "destiny child", "destinies child"]', 2,
 'Destiny''s Child — the group survived, the solo career ascended.', 'curated_v1'),

('wordplay',    'It''s a small paddle-powered boat spelled the same in both directions — name this palindromic watercraft.',
 'kayak', '["kayak", "a kayak", "kyack"]', 2,
 'Kayak — the same boat coming and going.', 'curated_v1'),

('lifestyle',   'A perfect game in ten-pin bowling — twelve straight strikes — scores exactly this round number.',
 '300', '["300", "three hundred"]', 2,
 'Three hundred — twelve strikes, zero mercy.', 'curated_v1'),

('academic',    'Covering some twenty square feet on an adult, it''s the human body''s largest — name this often-overlooked organ.',
 'skin', '["skin", "the skin", "your skin"]', 2,
 'Skin — the liver only wins on the inside.', 'curated_v1'),

-- two-step: obscure clue (Heisenberg alias) first, broad clue (Albuquerque chemistry teacher) second
('pop_culture', 'Its hero steals his alias from the physicist Heisenberg — a chemistry teacher goes criminal in Albuquerque — name this acclaimed AMC drama.',
 'Breaking Bad', '["breaking bad"]', 2,
 'Breaking Bad — say my name. The show''s, not the teacher''s.', 'curated_v1'),

('wordplay',    'It''s the fine white powder in a baker''s pantry that sounds exactly like a garden bloom — name this kitchen homophone.',
 'flour', '["flour", "flower"]', 2,
 'Flour — and if you meant the garden kind, same sound, same point.', 'curated_v1'),

('lifestyle',   'Hummus is made from tahini, lemon, garlic, and this mashed pale legume.',
 'chickpeas', '["chickpeas", "chick peas", "chickpea", "garbanzo", "garbanzos", "garbanzo beans"]', 2,
 'Chickpeas — garbanzo if you''re feeling fancy. Same bean.', 'curated_v1'),

('academic',    'It circles the sun tipped fully onto its side — name this blue-green seventh planet.',
 'Uranus', '["uranus", "your anus"]', 2,
 'Uranus — yes, laugh, it''s tradition.', 'curated_v1'),

-- two-step: obscure clue (Farrokh Bulsara, Zanzibar) first, broad clue (fronted Queen) second
('pop_culture', 'He was born Farrokh Bulsara on the island of Zanzibar — and fronted Queen as this legendary showman.',
 'Freddie Mercury', '["freddie mercury", "freddy mercury", "mercury", "freddie", "fred mercury"]', 2,
 'Freddie Mercury — Zanzibar''s greatest export, no contest.', 'curated_v1'),

-- ── Tier 3 (final-round mean) ───────────────────────────────────────────
('academic',    'He served as both the twenty-second and the twenty-fourth president of the United States — name this mustachioed nineteenth-century Democrat.',
 'Grover Cleveland', '["cleveland", "grover cleveland"]', 3,
 'Grover Cleveland — the only man counted twice in the 1800s.', 'curated_v1'),

('pop_culture', 'A dying tycoon whispers ''Rosebud'' in the opening scene — name this 1941 Orson Welles masterpiece.',
 'Citizen Kane', '["citizen kane", "citizen cane", "kane"]', 3,
 'Citizen Kane — the sled did it. Sorry, spoilers.', 'curated_v1'),

('wordplay',    'It sits above every lowercase i and j you''ve ever written — name this tiny typographical dot.',
 'tittle', '["tittle", "a tittle", "the tittle", "title"]', 3,
 'The tittle — proof that even the dot has a name.', 'curated_v1'),

-- CLOSEST-NUMBER: nearest guess wins; acceptable_answers are exact forms only
('lifestyle',   'Closest guess wins — counting black and white together, the number of keys on a full-size concert piano.',
 '88', '["88", "eighty eight", "eighty-eight"]', 3,
 'Eighty-eight — fifty-two white, thirty-six black, zero excuses.', 'curated_v1'),

('academic',    'William the Conqueror seized England at the Battle of Hastings in this famous eleventh-century year.',
 '1066', '["1066", "ten sixty six", "ten sixty-six", "one thousand sixty six"]', 3,
 'Ten sixty-six — the year England changed management.', 'curated_v1'),

('pop_culture', 'Fleetwood Mac released it in 1977 while the band''s couples were splitting up — name this Grammy-winning one-word album.',
 'Rumours', '["rumours", "rumors"]', 3,
 'Rumours — the couples broke up and the record went diamond.', 'curated_v1'),

('wordplay',    'Add two letters to it, and the word itself becomes shorter — name this paradoxical little word.',
 'short', '["short", "the word short"]', 3,
 'Short — the only word that shrinks by growing.', 'curated_v1'),

-- CLOSEST-NUMBER: nearest guess wins; acceptable_answers are exact forms only
('lifestyle',   'Closest guess wins — the total number of bones in a full-grown adult human skeleton.',
 '206', '["206", "two hundred six", "two hundred and six"]', 3,
 'Two hundred six — babies start with around three hundred and merge the spares.', 'curated_v1');
