"""WS-15 streaming-diarization bake-off harness (WO-LILY-OMNIBUS-003, AMENDMENT-002).

Incumbent = Speechmatics ENHANCED with the WS-13 tuned config (STT + built-in
diarization in one stream). Challenger = pyannoteAI Live-1 (diarization-only,
word-aligned against the SAME Speechmatics STT words). The bake-off isolates
the diarization difference: identical words, different speaker labels.

Program-wide rule: machine metrics only (WER/DER vs fixture ground truth, plus
the game-level metrics that decide adjudication) — never perceptual.

The pure-python pieces (game_metrics, word_align) ship as tested library code.
The live arms (diar_providers.SpeechmaticsIncumbent, PyannoteLive1Challenger)
and the fixture/score entry scripts are eval-only and lazy-import their vendor
SDKs so nothing here is on the agent boot path.
"""
