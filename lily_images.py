"""
lily_images.py — LILY question-image storage (WO-LILY-OMNIBUS-002 sub-agent H).

Supabase Storage plumbing for picture rounds: raw bytes (or a fetched web
image) land in the public `lily-images` bucket under the content-addressed
path `{source}/{sha1}.{ext}` and come back as a public URL for the question
payload / room metadata. Content-addressing makes uploads idempotent — the
same image bytes always land on the same path, so a re-upload is a no-op,
never a duplicate.

CACHE-FIRST RULE: any image need checks the bank row's image_url BEFORE
generating or fetching — the lily_questions row is the cache (migration
012). `lily_cached_bank_image` is the check; `lily_save_bank_image` is the
write-back after a successful source/generate so the next session cache-hits.

VISIBLE ATTEMPT ROWS (no-silent-crash, sub-agent J's port of the JRVS
media_gen_attempts rule — the writer lives here with the rest of the
storage foundation so both the Exa-sourcing side and the generation side
can use it without an import cycle): every image generation/fetch attempt
— success OR failure — writes one lily_image_attempts row; rejection and
error rows carry the actual provider message in failure_reason.
"""

import asyncio
import hashlib
import logging
from typing import Optional

import httpx

logger = logging.getLogger("lily_images")

# Existing infra: public Supabase Storage bucket (pre-provisioned).
LILY_IMAGES_BUCKET = "lily-images"

# Provenance values for stored images (question-schema enum minus 'none').
IMAGE_SOURCES = ("generated", "web", "player")

# Attempt-row statuses (JRVS media_gen_attempts spellings).
ATTEMPT_SUCCESS = "success"
ATTEMPT_REJECTED = "rejected"
ATTEMPT_ERROR = "error"

FETCH_TIMEOUT_SECONDS = 15.0
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB cap on fetched/uploaded images

_EXT_FOR_CONTENT_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def lily_image_ext(content_type: Optional[str]) -> Optional[str]:
    """File extension for a Content-Type, or None for non-image types."""
    if not content_type:
        return None
    base = content_type.split(";", 1)[0].strip().lower()
    return _EXT_FOR_CONTENT_TYPE.get(base)


def lily_image_storage_path(source: str, data: bytes, ext: str) -> str:
    """Content-addressed bucket path: {source}/{sha1}.{ext}."""
    return f"{source}/{hashlib.sha1(data).hexdigest()}.{ext}"


async def lily_upload_image_bytes(
    supabase,
    data: bytes,
    *,
    source: str,
    content_type: str = "image/png",
) -> Optional[str]:
    """Upload image bytes to the `lily-images` bucket; return the public
    URL, or None on any failure (logged — never raises)."""
    if supabase is None:
        logger.warning("LILY_IMAGES | UPLOAD_SKIPPED | reason=no_supabase_client")
        return None
    if source not in IMAGE_SOURCES:
        logger.warning("LILY_IMAGES | UPLOAD_SKIPPED | reason=bad_source source=%r", source)
        return None
    if not data:
        logger.warning("LILY_IMAGES | UPLOAD_SKIPPED | reason=empty_bytes source=%s", source)
        return None
    if len(data) > MAX_IMAGE_BYTES:
        logger.warning(
            "LILY_IMAGES | UPLOAD_SKIPPED | reason=too_large bytes=%d cap=%d",
            len(data), MAX_IMAGE_BYTES,
        )
        return None
    ext = lily_image_ext(content_type) or "png"
    path = lily_image_storage_path(source, data, ext)
    try:
        storage = supabase.storage.from_(LILY_IMAGES_BUCKET)
        try:
            await asyncio.to_thread(
                lambda: storage.upload(
                    path, data,
                    {"content-type": content_type, "upsert": "true"},
                )
            )
        except Exception as e:
            # Content-addressed path: an already-exists conflict IS a cache
            # hit — the exact bytes are already in the bucket.
            if "exist" not in str(e).lower() and "duplicate" not in str(e).lower():
                raise
            logger.info("LILY_IMAGES | UPLOAD_DEDUP | path=%s already stored", path)
        url = await asyncio.to_thread(lambda: storage.get_public_url(path))
        url = str(url or "").strip().rstrip("?")
        if not url:
            logger.error("LILY_IMAGES | UPLOAD_FAILED | reason=empty_public_url path=%s", path)
            return None
        logger.info("LILY_IMAGES | UPLOADED | path=%s bytes=%d", path, len(data))
        return url
    except Exception as e:
        logger.error(
            "LILY_IMAGES | UPLOAD_FAILED | path=%s error_class=%s error=%s",
            path, type(e).__name__, e,
        )
        return None


async def lily_fetch_image_bytes(
    url: str,
    *,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> Optional[tuple[bytes, str]]:
    """Fetch an image URL -> (bytes, content_type), or None on any failure
    (logged — never raises). Split out of lily_fetch_url_to_bucket so the
    content gate (OR amendment W1) can inspect the bytes BEFORE anything
    is cached — one bad cached image serves forever."""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        logger.warning("LILY_IMAGES | FETCH_SKIPPED | reason=bad_url url=%r", str(url)[:120])
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(
                "LILY_IMAGES | FETCH_FAILED | status=%d url=%s",
                resp.status_code, url[:200],
            )
            return None
        content_type = resp.headers.get("content-type", "")
        if not content_type.split(";", 1)[0].strip().lower().startswith("image/"):
            logger.warning(
                "LILY_IMAGES | FETCH_REJECTED | reason=not_an_image "
                "content_type=%r url=%s", content_type, url[:200],
            )
            return None
        return resp.content, content_type
    except Exception as e:
        logger.warning(
            "LILY_IMAGES | FETCH_FAILED | url=%s error_class=%s error=%s",
            url[:200], type(e).__name__, e,
        )
        return None


async def lily_fetch_url_to_bucket(
    supabase,
    url: str,
    *,
    source: str = "web",
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Fetch an image URL -> bytes -> `lily-images` bucket; return the
    public URL, or None on any failure (logged — never raises)."""
    fetched = await lily_fetch_image_bytes(url, timeout=timeout)
    if fetched is None:
        return None
    data, content_type = fetched
    return await lily_upload_image_bytes(
        supabase, data, source=source, content_type=content_type
    )


# ---------------------------------------------------------------------------
# Bank-row cache (CACHE-FIRST rule)
# ---------------------------------------------------------------------------

def lily_bank_row_id(question_id) -> Optional[int]:
    """kb_<row id> -> row id (mirrors lily_persistence.lily_burn_question).
    Only bank questions have a DB row to cache against."""
    qid = str(question_id or "")
    if not qid.startswith("kb_"):
        return None
    try:
        return int(qid[3:])
    except ValueError:
        return None


async def lily_cached_bank_image(supabase, question_id) -> Optional[dict]:
    """CACHE-FIRST check: the bank row's stored image, if any. Returns
    {image_url, image_source, image_license_note} or None."""
    row_id = lily_bank_row_id(question_id)
    if supabase is None or row_id is None:
        return None
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_questions")
            .select("image_url, image_source, image_license_note")
            .eq("id", row_id)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        if row.get("image_url"):
            logger.info(
                "LILY_IMAGES | CACHE_HIT | question_id=%s source=%s",
                question_id, row.get("image_source"),
            )
            return {
                "image_url": row["image_url"],
                "image_source": row.get("image_source") or "web",
                "image_license_note": row.get("image_license_note"),
            }
        return None
    except Exception as e:
        logger.warning("lily_cached_bank_image error for %s: %s", question_id, e)
        return None


async def lily_save_bank_image(
    supabase,
    question_id,
    *,
    image_url: str,
    image_source: str,
    image_license_note: Optional[str] = None,
) -> bool:
    """Write a sourced/generated image back onto the bank row so the next
    session cache-hits. No-op (False) for non-bank ids — generated
    questions have no DB row. Never raises."""
    row_id = lily_bank_row_id(question_id)
    if supabase is None or row_id is None or not image_url:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_questions")
            .update({
                "image_url": image_url,
                "image_source": image_source,
                "image_license_note": image_license_note,
            })
            .eq("id", row_id)
            .execute()
        )
        logger.info(
            "LILY_IMAGES | BANK_WRITE_BACK | question_id=%s source=%s",
            question_id, image_source,
        )
        return True
    except Exception as e:
        logger.warning("lily_save_bank_image error for %s: %s", question_id, e)
        return False


# ---------------------------------------------------------------------------
# Visible attempt rows (no-silent-crash — JRVS media_gen_attempts port)
# ---------------------------------------------------------------------------

async def lily_record_image_attempt(
    supabase,
    *,
    session_id: Optional[str],
    question_id: Optional[str],
    source: str,
    prompt: str,
    status: str,
    failure_reason: Optional[str] = None,
    model: Optional[str] = None,
    image_url: Optional[str] = None,
) -> None:
    """Persist one lily_image_attempts row. Fail-soft, but LOUD: a skipped
    or failed insert logs a warning — the attempt row is the visibility
    surface for the no-silent-crash rule, so its own failure must at least
    be visible in the logs."""
    logger.log(
        logging.INFO if status == ATTEMPT_SUCCESS else logging.WARNING,
        "LILY_IMAGE_ATTEMPT | status=%s source=%s question_id=%s reason=%s",
        status, source, question_id, (failure_reason or "-")[:200],
    )
    if supabase is None:
        logger.warning(
            "LILY_IMAGE_ATTEMPT | ROW_SKIPPED | reason=no_supabase_client status=%s",
            status,
        )
        return
    payload = {
        "session_id": session_id or "",
        "question_id": str(question_id or ""),
        "source": source,
        # Schema makes prompt NOT NULL — sentinel so the attempt is still
        # recorded instead of dropped (JRVS donor behavior).
        "prompt": (prompt or "").strip() or "(prompt unavailable)",
        "status": status,
    }
    if failure_reason is not None:
        payload["failure_reason"] = str(failure_reason)[:4000]
    if model:
        payload["model"] = model
    if image_url:
        payload["image_url"] = image_url
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_image_attempts").insert(payload).execute()
        )
    except Exception as e:
        logger.warning(
            "LILY_IMAGE_ATTEMPT | ROW_INSERT_FAILED | status=%s error_class=%s error=%s",
            status, type(e).__name__, e,
        )
