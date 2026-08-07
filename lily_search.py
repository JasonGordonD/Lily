"""
lily_search.py — LILY web tools: Exa + Tavily (WO-LILY-OMNIBUS-002
sub-agents I/K). NATIVE lift of the prmpt_common search modules
(prmpt_common is do-not-import), rewritten on httpx.

===========================================================================
HARD GUARDRAIL — REASONING NODE ONLY. NEVER THE VOCAL PATH. EVER.
===========================================================================
These functions run at PREFETCH TIME on the background reasoning node
(lily_reasoning): question verification, current-events sourcing, and
real-entity image sourcing. They must NEVER be imported by, called from,
or registered as tools on the vocal node (lily_agent):

  - a web round-trip on the vocal path is a multi-second stall in live
    audio (the whole dual-brain architecture exists to prevent this);
  - raw web text reaching the vocal LLM is a prompt-injection surface.

Web results reach Lily ONLY as bank rows or state-block facts prepared by
the reasoning node. The import tripwire below enforces the rule in code;
tests/test_web_guardrails.py enforces it by inspection.

Real-entity image sourcing (sub-agent I) is CONSERVATIVE by design:
candidates must come from a safelisted host, carry every significant
entity token, and look like a direct image — anything less is rejected.
Failure is always a text-only fallback; real entities are NEVER handed to
the image generator (that is sub-agent J's invented-content-only stack).
"""

import inspect
import logging
import re
import urllib.parse
from typing import Optional

import httpx

import lily_config
import lily_images

logger = logging.getLogger("lily_search")

# ---------------------------------------------------------------------------
# Import tripwire — the vocal node may never pull this module in.
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORTERS = ("lily_agent",)


def lily_direct_importer(stack_module_names) -> str:
    """The module DIRECTLY importing lily_search: the first stack frame
    that is neither this module nor import machinery. Pure (takes the
    module-name list) so the tripwire is unit-testable."""
    for name in stack_module_names:
        if not name or name == "lily_search" or "importlib" in name:
            continue
        return name
    return ""


def lily_forbid_vocal_import(stack_module_names) -> None:
    """Raise if the DIRECT importer is the vocal node. The designed seam —
    lily_agent -> lily_reasoning -> lily_search — is legal (the reasoning
    module is the one place web results are turned into question payloads
    and state-block facts); a direct `import lily_search` in lily_agent is
    not, and neither is a lazy in-function import there."""
    importer = lily_direct_importer(stack_module_names)
    if importer in FORBIDDEN_IMPORTERS:
        raise RuntimeError(
            "LILY_SEARCH | GUARDRAIL | lily_search is reasoning-node-only "
            f"and must never be imported from the vocal path ({importer}). "
            "Web results reach Lily only as bank rows or state-block "
            "facts prepared by the reasoning node."
        )


lily_forbid_vocal_import(
    [f.frame.f_globals.get("__name__", "") for f in inspect.stack()]
)

EXA_SEARCH_URL = "https://api.exa.ai/search"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
EXA_TIMEOUT_SECONDS = 10.0
TAVILY_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Exa semantic search (donor: prmpt_common/search/exa.py, aiohttp -> httpx)
# ---------------------------------------------------------------------------

async def lily_exa_search(
    query: str,
    *,
    num_results: int = 5,
    search_type: str = "auto",
    highlight_sentences: int = 3,
    highlights_per_url: int = 2,
    max_text_chars: int = 500,
    timeout: float = EXA_TIMEOUT_SECONDS,
    api_key: Optional[str] = None,
) -> dict:
    """Semantic search via the Exa API.

    Returns {'results': [{title, url, image, highlights, text}]} or
    {'error': ...} on any failure (never raises)."""
    key = api_key or lily_config.exa_api_key()
    if not key:
        return {"error": "Exa API key not configured"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                EXA_SEARCH_URL,
                json={
                    "query": query,
                    "type": search_type,
                    "numResults": max(1, min(10, num_results)),
                    "contents": {
                        "highlights": {
                            "numSentences": highlight_sentences,
                            "highlightsPerUrl": highlights_per_url,
                        },
                        "text": {"maxCharacters": max_text_chars},
                    },
                },
                headers={"x-api-key": key, "Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            logger.error(
                "LILY_SEARCH | EXA | HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return {"error": f"HTTP {resp.status_code}"}
        data = resp.json()
        return {
            "results": [
                {
                    "title": r.get("title", "Untitled"),
                    "url": r.get("url", ""),
                    # og:image of the result page, when Exa has one — the
                    # raw material for real-entity picture sourcing.
                    "image": r.get("image", ""),
                    "highlights": r.get("highlights", []),
                    "text": r.get("text", ""),
                }
                for r in data.get("results", [])
            ],
        }
    except Exception as e:
        logger.error("LILY_SEARCH | EXA | error: %s", e)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tavily search (donor: prmpt_common/search/tavily.py, aiohttp -> httpx)
# ---------------------------------------------------------------------------

async def lily_tavily_search(
    query: str,
    *,
    max_results: int = 3,
    search_depth: str = "basic",
    include_answer: str = "basic",
    timeout: float = TAVILY_TIMEOUT_SECONDS,
    api_key: Optional[str] = None,
) -> dict:
    """Web search via the Tavily API (fast — built for agent latency).

    Returns {'answer': str, 'results': [{title, content, url}]} or
    {'error': ...} on any failure (never raises)."""
    key = api_key or lily_config.tavily_api_key()
    if not key:
        return {"error": "Tavily API key not configured"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                TAVILY_SEARCH_URL,
                json={
                    "query": query,
                    "search_depth": search_depth,
                    "max_results": max_results,
                    "include_answer": include_answer,
                },
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code != 200:
            logger.error(
                "LILY_SEARCH | TAVILY | HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return {"error": f"HTTP {resp.status_code}"}
        data = resp.json()
        return {
            "answer": data.get("answer", ""),
            "results": [
                {
                    "title": r.get("title", ""),
                    "content": r.get("content", ""),
                    "url": r.get("url", ""),
                }
                for r in data.get("results", [])[:max_results]
            ],
        }
    except Exception as e:
        logger.error("LILY_SEARCH | TAVILY | error: %s", e)
        return {"error": str(e)}


def lily_format_tavily_results(data: dict, max_chars: int = 1500) -> str:
    """Format Tavily results into one bounded text block (donor formatter
    minus the prmpt_common paginator — a plain truncation bound)."""
    if "error" in data:
        return f"Search error: {data['error']}"
    parts = []
    if data.get("answer"):
        parts.append(f"Answer: {data['answer']}")
    for r in data.get("results", []):
        parts.append(f"- {r['title']}: {r['content']} ({r['url']})")
    full = "\n".join(parts) if parts else "No results found."
    return full[:max_chars]


# ---------------------------------------------------------------------------
# Reasoning-node consumers (K-b): verification context + current events
# ---------------------------------------------------------------------------

async def lily_web_verification_context(
    question_prompt: str,
    canonical_answer: str,
    *,
    timeout: float = TAVILY_TIMEOUT_SECONDS,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """One bounded web-fact block for question verification at prefetch
    (consumed by lily_reasoning.verify_question). None on any failure —
    verification proceeds on model knowledge alone."""
    query = f"{question_prompt} {canonical_answer}".strip()
    if not query:
        return None
    data = await lily_tavily_search(
        query, max_results=3, include_answer="basic",
        timeout=timeout, api_key=api_key,
    )
    if "error" in data:
        return None
    text = lily_format_tavily_results(data)
    return text if text and text != "No results found." else None


async def lily_current_events_brief(
    topic: str,
    *,
    timeout: float = TAVILY_TIMEOUT_SECONDS,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Fresh-facts brief for current-events question sourcing at prefetch
    (consumed by lily_reasoning.generate_question). None on any failure —
    generation falls back to evergreen knowledge."""
    if not (topic or "").strip():
        return None
    data = await lily_tavily_search(
        f"notable {topic} this week", max_results=5,
        include_answer="basic", timeout=timeout, api_key=api_key,
    )
    if "error" in data:
        return None
    text = lily_format_tavily_results(data)
    return text if text and text != "No results found." else None


# ---------------------------------------------------------------------------
# Google Search grounding (Gemini built-in google_search tool) — an ADDITIONAL
# reasoning-node source ALONGSIDE Exa/Tavily, never a replacement. Gemini 3.x
# runs the whole search->synthesize->cite loop server-side and returns text
# plus grounding_metadata (queries + source chunks). Same reasoning-node-only
# guardrail as the rest of this module. Additive by design: a failure returns
# None and the existing Exa/Tavily paths are untouched.
# ---------------------------------------------------------------------------

GOOGLE_GROUNDING_TIMEOUT_SECONDS = 20.0

_GENAI_GROUNDING_CLIENT = None


def _genai_grounding_client():
    """Lazily build the reasoning-node genai client for grounding. Separate
    from lily_reasoning's vocal/reasoning clients (its own instance, same
    key), so grounding never contends with generation."""
    global _GENAI_GROUNDING_CLIENT
    if _GENAI_GROUNDING_CLIENT is None:
        from google import genai as google_genai
        _GENAI_GROUNDING_CLIENT = google_genai.Client(
            api_key=lily_config.google_api_key()
        )
    return _GENAI_GROUNDING_CLIENT


def lily_parse_google_grounding(resp) -> Optional[dict]:
    """Pull {text, citations, queries, retrieved_urls} out of a grounded
    generate_content response. citations = [{url, title}] from
    grounding_chunks (google_search); queries = executed web_search_queries;
    retrieved_urls = [{url, status}] from url_context_metadata. None when
    there is no usable text. Defensive against shape drift — a missing field
    is skipped, never raised."""
    text = (getattr(resp, "text", None) or "").strip()
    citations: list = []
    queries: list = []
    retrieved_urls: list = []
    try:
        candidates = getattr(resp, "candidates", None) or []
        cand = candidates[0] if candidates else None
        gm = getattr(cand, "grounding_metadata", None) if cand else None
        if gm is not None:
            queries = list(getattr(gm, "web_search_queries", None) or [])
            for chunk in (getattr(gm, "grounding_chunks", None) or []):
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None) if web else None
                if uri:
                    citations.append(
                        {"url": uri, "title": getattr(web, "title", "") or ""}
                    )
        ucm = getattr(cand, "url_context_metadata", None) if cand else None
        if ucm is not None:
            for meta in (getattr(ucm, "url_metadata", None) or []):
                url = getattr(meta, "retrieved_url", None)
                if url:
                    retrieved_urls.append({
                        "url": url,
                        "status": str(getattr(meta, "url_retrieval_status", "") or ""),
                    })
    except Exception as e:
        logger.warning("LILY_SEARCH | GROUNDING | parse warn: %s", e)
    if not text:
        return None
    return {
        "text": text, "citations": citations, "queries": queries,
        "retrieved_urls": retrieved_urls,
    }


async def _lily_grounded_generate(
    prompt: str, *, use_search: bool, use_url_context: bool, timeout: float,
) -> Optional[dict]:
    """Shared grounded generate_content call: enable google_search and/or
    url_context built-in tools (Gemini runs the whole loop server-side).
    Returns the parsed grounding dict or None. Reasoning-node only."""
    lily_forbid_vocal_import(lily_direct_importer(None))
    if not (prompt or "").strip() or not lily_config.google_api_key_present():
        return None
    try:
        import asyncio
        from google.genai import types as gt

        tools = []
        if use_search:
            tools.append(gt.Tool(google_search=gt.GoogleSearch()))
        if use_url_context:
            tools.append(gt.Tool(url_context=gt.UrlContext()))
        if not tools:
            return None
        client = _genai_grounding_client()

        def _call():
            return client.models.generate_content(
                model=lily_config.google_grounding_model(),
                contents=prompt,
                config=gt.GenerateContentConfig(tools=tools),
            )

        resp = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout)
        result = lily_parse_google_grounding(resp)
        if result:
            logger.info(
                "LILY_SEARCH | GROUNDING | search=%s url_context=%s queries=%d "
                "citations=%d urls=%d",
                use_search, use_url_context, len(result["queries"]),
                len(result["citations"]), len(result["retrieved_urls"]),
            )
        return result
    except Exception as e:
        logger.error("LILY_SEARCH | GROUNDING | error: %s", e)
        return None


async def lily_google_grounded_search(
    query: str, *, timeout: float = GOOGLE_GROUNDING_TIMEOUT_SECONDS,
) -> Optional[dict]:
    """Grounded answer for `query` via Gemini's built-in google_search tool —
    an ADDITIONAL source alongside Exa/Tavily. Returns {text, citations,
    queries, retrieved_urls} or None (caller falls back). Reasoning-node
    only; gated on lily_config.google_grounding_enabled()."""
    if not lily_config.google_grounding_enabled():
        return None
    return await _lily_grounded_generate(
        query, use_search=True, use_url_context=False, timeout=timeout,
    )


async def lily_url_context_read(
    prompt: str,
    *,
    with_search: bool = False,
    timeout: float = GOOGLE_GROUNDING_TIMEOUT_SECONDS,
) -> Optional[dict]:
    """Read/compare/extract from the URL(s) embedded in `prompt` via Gemini's
    built-in url_context tool (up to 20 public URLs; the model fetches them
    itself). Optionally combine with google_search (with_search=True) so the
    model can search AND deep-read specific pages. Returns the grounding dict
    (retrieved_urls carries per-URL fetch status) or None. Reasoning-node
    only; gated on lily_config.url_context_enabled()."""
    if not lily_config.url_context_enabled():
        return None
    return await _lily_grounded_generate(
        prompt, use_search=with_search, use_url_context=True, timeout=timeout,
    )


def lily_format_google_grounding(result: Optional[dict], max_chars: int = 1500) -> str:
    """One bounded text block from a grounding result — the answer plus its
    sources, same shape as lily_format_tavily_results so a consumer can use
    either interchangeably."""
    if not result or not result.get("text"):
        return "No results found."
    parts = [result["text"]]
    cites = result.get("citations") or []
    if cites:
        parts.append(
            "Sources: "
            + "; ".join(
                f"{c.get('title') or c.get('url')} ({c.get('url')})"
                for c in cites[:5]
            )
        )
    return "\n".join(parts)[:max_chars]


# ---------------------------------------------------------------------------
# Real-entity image sourcing (sub-agent I) — conservative, reject-on-doubt
# ---------------------------------------------------------------------------

# Hosts whose page images are overwhelmingly direct, captioned photographs
# of the page subject (recognizable + unambiguous) and safe-for-table.
# Anything off this list is rejected — reject on doubt.
SAFE_IMAGE_HOSTS = (
    "wikipedia.org",
    "wikimedia.org",
    "britannica.com",
    "nasa.gov",
    "si.edu",
    "nps.gov",
    "loc.gov",
)

_ENTITY_STOPWORDS = {"the", "a", "an", "of", "in", "at", "de", "la", "le"}


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_is_safelisted(url: str) -> bool:
    host = _host(url)
    return any(
        host == safe or host.endswith("." + safe) for safe in SAFE_IMAGE_HOSTS
    )


def _entity_tokens(entity: str) -> list[str]:
    return [
        t for t in re.sub(r"[^a-z0-9\s]", " ", (entity or "").lower()).split()
        if len(t) >= 3 and t not in _ENTITY_STOPWORDS
    ]


def lily_filter_entity_image_candidate(
    entity: str, result: dict
) -> Optional[dict]:
    """Conservative filter for ONE Exa result as a picture-question image
    for a REAL entity. Accepts only when every check passes — recognizable
    (page is about the entity), unambiguous (all significant entity tokens
    present), safe-for-table (safelisted host, https). Reject on doubt.

    Returns {'image_url', 'page_url', 'title'} or None."""
    image_url = str(result.get("image") or "").strip()
    page_url = str(result.get("url") or "").strip()
    title = str(result.get("title") or "").strip()
    if not image_url or not image_url.lower().startswith("https://"):
        return None
    if not _host_is_safelisted(image_url) or not _host_is_safelisted(page_url):
        return None
    tokens = _entity_tokens(entity)
    if not tokens or not any(t.isalpha() and len(t) >= 4 for t in tokens):
        # Entities with no substantial name token (bare numbers, dates,
        # two-letter answers) are not picture material — reject.
        return None
    haystack = " ".join(
        [title, page_url, str(result.get("text") or "")]
    ).lower()
    if not all(t in haystack for t in tokens):
        return None
    return {"image_url": image_url, "page_url": page_url, "title": title}


async def lily_find_real_entity_image(
    entity: str,
    *,
    timeout: float = EXA_TIMEOUT_SECONDS,
    api_key: Optional[str] = None,
) -> Optional[dict]:
    """Source ONE candidate image for a real entity via Exa. Applies the
    conservative filter to each result in rank order; the first survivor
    wins. None on any failure or when nothing passes — the caller falls
    back to text-only. NEVER generates (real entities are never handed to
    the image generator)."""
    entity = (entity or "").strip()
    if not entity:
        return None
    data = await lily_exa_search(
        f"{entity} photograph", num_results=5,
        max_text_chars=300, timeout=timeout, api_key=api_key,
    )
    if "error" in data:
        logger.info(
            "LILY_SEARCH | REAL_ENTITY_IMAGE | entity=%r search error: %s "
            "-> text-only fallback", entity, data["error"],
        )
        return None
    for result in data.get("results", []):
        candidate = lily_filter_entity_image_candidate(entity, result)
        if candidate is not None:
            logger.info(
                "LILY_SEARCH | REAL_ENTITY_IMAGE | entity=%r accepted image "
                "from %s", entity, _host(candidate["image_url"]),
            )
            return candidate
    logger.info(
        "LILY_SEARCH | REAL_ENTITY_IMAGE | entity=%r no candidate passed the "
        "conservative filter -> text-only fallback", entity,
    )
    return None


# ---------------------------------------------------------------------------
# Real-entity picture questions ("name this landmark") — sub-agent I
# ---------------------------------------------------------------------------
# Purpose-built questions ABOUT the image (the image never decorates a
# standard text question — that would put the answer on the screen).
# Curated, visually unmistakable real subjects; sourcing is web-only.

REAL_ENTITY_SUBJECTS: tuple[dict, ...] = (
    {"entity": "Eiffel Tower", "kind": "landmark",
     "acceptable": ["eiffel tower", "the eiffel tower", "eiffel"]},
    {"entity": "Golden Gate Bridge", "kind": "landmark",
     "acceptable": ["golden gate bridge", "golden gate", "the golden gate bridge"]},
    {"entity": "Colosseum", "kind": "landmark",
     "acceptable": ["colosseum", "the colosseum", "coliseum", "roman colosseum"]},
    {"entity": "Sydney Opera House", "kind": "landmark",
     "acceptable": ["sydney opera house", "the sydney opera house", "opera house"]},
    {"entity": "Taj Mahal", "kind": "landmark",
     "acceptable": ["taj mahal", "the taj mahal"]},
    {"entity": "Mount Fuji", "kind": "landmark",
     "acceptable": ["mount fuji", "fuji", "mt fuji"]},
    {"entity": "Statue of Liberty", "kind": "landmark",
     "acceptable": ["statue of liberty", "the statue of liberty", "lady liberty"]},
    {"entity": "Great Wall of China", "kind": "landmark",
     "acceptable": ["great wall of china", "the great wall", "great wall"]},
)

REAL_ENTITY_PROMPT = (
    "Eyes on the screen — no shouting till you're sure. Name this {kind}."
)


async def lily_build_real_entity_picture_question(
    supabase,
    *,
    index: int,
    session_id: str,
    difficulty_tier: int = 2,
    approve=None,
) -> Optional[dict]:
    """Build one 'name this landmark' picture question from the curated
    real-subject list: Exa-source the image (conservative filter), store
    via lily_images (sub-agent H) with provenance in image_license_note,
    and return the §4.2 question shape. None on ANY failure — the caller
    falls back to the standard text supply. Never raises; every fetch or
    upload failure writes a visible attempt row."""
    subject = REAL_ENTITY_SUBJECTS[index % len(REAL_ENTITY_SUBJECTS)]
    entity = subject["entity"]
    try:
        candidate = await lily_find_real_entity_image(entity)
        if candidate is None:
            # A conservative pass is a decision, not a failure — logged by
            # the finder; no error row for declining to show an image.
            return None
        # Content gate (OR amendment W1): the reasoning node approves
        # image-vs-question BEFORE anything is cached — one bad cached
        # image serves forever. `approve` is an async (bytes, content_type,
        # entity) -> (approved, reason); production always passes it.
        fetched = await lily_images.lily_fetch_image_bytes(
            candidate["image_url"]
        )
        if fetched is None:
            await lily_images.lily_record_image_attempt(
                supabase, session_id=session_id,
                question_id=f"pic_{index:04d}", source="web", prompt=entity,
                status=lily_images.ATTEMPT_ERROR,
                failure_reason=(
                    "fetch failed for accepted candidate "
                    f"{candidate['image_url'][:300]}"
                ),
            )
            return None
        image_bytes, content_type = fetched
        if approve is not None:
            approved, gate_reason = await approve(
                image_bytes, content_type, entity
            )
            if not approved:
                await lily_images.lily_record_image_attempt(
                    supabase, session_id=session_id,
                    question_id=f"pic_{index:04d}", source="web",
                    prompt=entity, status=lily_images.ATTEMPT_REJECTED,
                    failure_reason=f"content gate: {gate_reason}"[:500],
                )
                return None
        else:
            logger.warning(
                "LILY_SEARCH | CONTENT_GATE_SKIPPED | entity=%r — no "
                "approver supplied (test/direct call path)", entity,
            )
        stored_url = await lily_images.lily_upload_image_bytes(
            supabase, image_bytes, source="web", content_type=content_type
        )
        if stored_url is None:
            await lily_images.lily_record_image_attempt(
                supabase, session_id=session_id,
                question_id=f"pic_{index:04d}", source="web", prompt=entity,
                status=lily_images.ATTEMPT_ERROR,
                failure_reason=(
                    "store failed for approved candidate "
                    f"{candidate['image_url'][:300]}"
                ),
            )
            return None
        license_note = (
            f"web image via Exa: page={candidate['page_url']} "
            f"image={candidate['image_url']}"
        )
        await lily_images.lily_record_image_attempt(
            supabase, session_id=session_id, question_id=f"pic_{index:04d}",
            source="web", prompt=entity,
            status=lily_images.ATTEMPT_SUCCESS, image_url=stored_url,
        )
        return {
            "id": f"pic_{index:04d}",
            "category": "picture round",
            "difficulty_tier": difficulty_tier,
            "prompt": REAL_ENTITY_PROMPT.format(kind=subject["kind"]),
            "canonical_answer": entity,
            "acceptable_answers": list(subject["acceptable"]),
            "reveal_color": f"That's the {entity} — the real thing.",
            "image_url": stored_url,
            "image_source": "web",
            "image_license_note": license_note,
        }
    except Exception as e:
        # No-silent-crash: the failure is a visible row + a log line, and
        # the round continues text-only.
        logger.error(
            "LILY_SEARCH | REAL_ENTITY_QUESTION_FAILED | entity=%r "
            "error_class=%s error=%s", entity, type(e).__name__, e,
        )
        await lily_images.lily_record_image_attempt(
            supabase, session_id=session_id, question_id=f"pic_{index:04d}",
            source="web", prompt=entity, status=lily_images.ATTEMPT_ERROR,
            failure_reason=f"{type(e).__name__}: {e}",
        )
        return None
