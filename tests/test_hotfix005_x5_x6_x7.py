"""WO-LILY-HOTFIX-005 X5/X6/X7 — delivery & display integrity.

X5: a torn chunk that pushed ZERO bytes is re-fetched once in-place (no
audio duplication) so the tail stays off the cut-recovery routine path.
X6: published metadata stamps the question number so the glass can detect a
stale render (displayed id ≠ active id).
X7: a provider content/moderation rejection is a first-class REJECTED
outcome, not an ATTEMPT_ERROR, and falls back pictureless.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_tts
import lily_imagegen


# -- X5: bounded chunk retry constant -----------------------------------------

def test_chunk_retry_is_bounded():
    assert lily_tts._MAX_CHUNK_RETRIES == 1


# -- X7: content-rejection classifier -----------------------------------------

def test_moderation_400_is_a_content_rejection():
    # the live 18:47:48 line
    assert lily_imagegen.lily_is_content_rejection(
        "xAI image HTTP 400: Generated image rejected by content moderation"
    )


def test_safety_and_prohibited_are_rejections():
    assert lily_imagegen.lily_is_content_rejection("blocked by safety filters")
    assert lily_imagegen.lily_is_content_rejection("FinishReason.PROHIBITED_CONTENT")
    assert lily_imagegen.lily_is_content_rejection(
        RuntimeError("no image in response")
    )


def test_transport_errors_are_not_rejections():
    assert not lily_imagegen.lily_is_content_rejection(
        "xAI image HTTP 500: internal error"
    )
    assert not lily_imagegen.lily_is_content_rejection("connection timeout")
    assert not lily_imagegen.lily_is_content_rejection("bucket store failed")
