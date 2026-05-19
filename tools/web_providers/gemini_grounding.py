"""Gemini Grounding Search web search provider.

Uses Google Gemini's ``generateContent`` REST endpoint with the ``google_search``
tool to perform LLM-grounded web search.  Each call is one full LLM inference
that returns:

  - a synthesized answer text (Gemini's prose summary of the findings)
  - grounding chunks (URL + title pairs) that back up the answer

This provider implements ``WebSearchProvider`` only — no extract/crawl.  Pair
with Firecrawl/Tavily/Exa/Parallel when ``web_extract`` is also needed.

Configuration::

    # ~/.hermes/.env  (required)
    GEMINI_GROUNDING_API_KEY=AIza...

    # ~/.hermes/.env  (optional overrides)
    # GEMINI_GROUNDING_MODEL=gemini-3.1-flash-lite
    GEMINI_GROUNDING_BASE_URL=...                # escape hatch for proxies

    # ~/.hermes/config.yaml
    web:
      search_backend: "gemini-grounding"
      extract_backend: "firecrawl"   # or any other extract provider

The key is intentionally separate from ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``
to keep grounding billing and quota isolated from regular Gemini API usage.

Cost / latency notes:
  - Each ``web_search`` call is a full LLM inference (typical 2~8s, peak 15s+).
    Plus 1~6s for Vertex redirect URL resolution.  Roughly 10~100x the cost of
    a plain search API.  Limit aggressive use; for high-volume, switch backends.
  - The Gemini ``google_search`` tool does NOT accept a results-count parameter.
    The model decides chunk count (typically 3~10).  ``limit`` is enforced via
    post-hoc truncation only — it does not reduce inference cost.

Security notes:
  - Auth uses the ``x-goog-api-key`` header (not a query parameter), so the key
    does not appear in URLs that may be logged by HTTP middleware.
  - The ``answer`` field is content synthesized from external web pages and may
    contain prompt-injection attempts.  Callers/agents must treat it as data,
    not as instructions.  The schema description for ``web_search`` includes
    this caveat.
  - Gemini's ``groundingChunks[].web.uri`` returns ``vertexaisearch.cloud
    .google.com/grounding-api-redirect/...`` redirect URLs, not direct source
    URLs.  Each is resolved via a HEAD request (5s timeout) before being
    returned; resolution failures fall back to the original redirect URL.
    Redirect resolution refuses to follow URLs whose initial host is not a
    Google vertex-redirect host, and the final resolved URL is checked against
    ``is_safe_url`` to refuse internal/private targets.

Google Terms of Service notes:
  - Per the `Gemini API Additional Terms`_, Grounding with Google Search
    responses include a ``searchEntryPoint`` (rendered HTML/CSS chip) that
    apps with a user-visible UI are required to display.  This provider
    preserves the raw ``groundingMetadata`` block under ``data.grounding_metadata``
    so downstream UIs can render Search Suggestions and inline citations.
  - The grounded URLs returned in ``data.web`` are search results from
    Google's grounding service.  Mechanically auto-chaining ``web_extract`` /
    ``web_crawl`` on every grounded URL to bypass the synthesized ``answer``
    may run afoul of the Grounding terms (which restrict caching and
    redistribution of search results).  The schema description tells the
    agent to treat the ``answer`` as the primary signal and only fetch
    individual URLs when truly necessary.

.. _`Gemini API Additional Terms`: https://ai.google.dev/gemini-api/terms
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

import httpx

from tools.web_providers.base import WebSearchProvider

logger = logging.getLogger(__name__)


_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_DEFAULT_MODEL = "gemini-3.1-flash-lite"
_REQUEST_TIMEOUT = 60.0
_REDIRECT_TIMEOUT = 5.0

# Strip API keys from any text that gets surfaced as an error message.
# Gemini occasionally echoes parts of the request (including header values)
# back in 4xx response bodies; we never want a key to leak there.
_KEY_REDACT_RE = re.compile(
    r"(key=|x-goog-api-key[\"':\s]*)[^\s&\"',]+",
    re.IGNORECASE,
)


def _redact_key(text: str) -> str:
    """Replace any leaked API key in error text with ``***``."""
    return _KEY_REDACT_RE.sub(r"\1***", text)


def _resolve_citation_redirect_url(url: str, client: httpx.Client) -> str:
    """Resolve a Vertex grounding redirect URL to its real destination.

    Gemini grounding chunks contain redirect URLs of the form
    ``https://vertexaisearch.cloud.google.com/grounding-api-redirect/...``.
    We send a HEAD request (with redirects followed) and read the final URL.

    Defense in depth (defensive, not classical SSRF — the URL comes from
    Google):
      1. Only follow redirects when the *initial* host is a Google
         vertex-redirect host.  If Gemini ever returned a non-redirect URL
         (compromise, API change, error), we will not fetch it.
      2. Run the *final* resolved URL through ``is_safe_url`` to refuse
         private / internal targets.  Hermes's other web tools already do
         this on every URL; resolved citations should hold the same bar.

    On any failure (timeout, network error, non-2xx, malformed Location,
    safety check failure) fall back to the original URL — better to surface
    a redirect than fail the whole search.
    """
    from urllib.parse import urlparse
    from tools.url_safety import is_safe_url

    initial_host = (urlparse(url).hostname or "").lower()
    if not (initial_host == "vertexaisearch.cloud.google.com"
            or initial_host.endswith(".vertexaisearch.cloud.google.com")):
        # Not a Gemini redirect host — return as-is, do not fetch.
        return url

    try:
        resp = client.head(
            url,
            follow_redirects=True,
            timeout=_REDIRECT_TIMEOUT,
        )
        final = str(resp.url) if resp.url else ""
        if not final:
            return url
        if not is_safe_url(final):
            logger.warning(
                "Gemini grounding redirect resolved to unsafe URL host=%s; "
                "falling back to original redirect",
                (urlparse(final).hostname or ""),
            )
            return url
        return final
    except Exception:
        return url


class GeminiGroundingSearchProvider(WebSearchProvider):
    """Search via Gemini's ``google_search`` grounding tool.

    Requires ``GEMINI_GROUNDING_API_KEY``.  No extract capability — pair with
    Firecrawl/Tavily/Exa/Parallel when also using ``web_extract``.

    Returns the standard ``{success, data: {web: [...]}}`` shape extended with
    a ``data.answer`` string containing Gemini's synthesized response.  Other
    backends do not populate ``answer``; consumers using ``data.get("answer", "")``
    remain forwards-compatible.
    """

    def provider_name(self) -> str:
        return "gemini-grounding"

    def is_configured(self) -> bool:
        """Return True when ``GEMINI_GROUNDING_API_KEY`` is set to a non-empty value."""
        return bool(os.getenv("GEMINI_GROUNDING_API_KEY", "").strip())

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a grounded search via Gemini.

        Returns::

            {
              "success": True,
              "data": {
                "answer": "<synthesized text>",
                "web": [
                  {"title": str, "url": str, "description": "", "position": int},
                  ...
                ]
              }
            }

        On failure returns ``{"success": False, "error": str}`` with API keys
        redacted from the error string.

        Note: ``limit`` is enforced via post-hoc truncation since Gemini does
        not accept a results-count parameter.  ``description`` is intentionally
        empty — the synthesized ``answer`` carries the content load.
        """
        api_key = os.getenv("GEMINI_GROUNDING_API_KEY", "").strip()
        if not api_key:
            return {"success": False, "error": "GEMINI_GROUNDING_API_KEY is not set"}

        model = os.getenv("GEMINI_GROUNDING_MODEL", "").strip() or _DEFAULT_MODEL
        base_url = (
            os.getenv("GEMINI_GROUNDING_BASE_URL", "").strip().rstrip("/")
            or _DEFAULT_BASE_URL
        )
        try:
            safe_limit = max(1, int(limit))
        except (TypeError, ValueError):
            safe_limit = 5

        endpoint = f"{base_url}/models/{model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
        }

        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            try:
                resp = client.post(
                    endpoint,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": api_key,
                    },
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    logger.warning("Gemini grounding rate-limited (429)")
                    return {
                        "success": False,
                        "error": "Gemini grounding rate-limited (HTTP 429). Wait and retry, or use a different backend.",
                    }
                detail = ""
                try:
                    body = exc.response.json()
                    if isinstance(body, dict):
                        err = body.get("error") or {}
                        detail = str(err.get("message") or err.get("status") or "")
                except Exception:
                    detail = (exc.response.text or "")[:200]
                msg = f"Gemini grounding returned HTTP {status}"
                if detail:
                    msg += f": {_redact_key(detail[:200])}"
                logger.warning("Gemini grounding HTTP error: %s", msg)
                return {"success": False, "error": msg}
            except httpx.RequestError as exc:
                err_text = _redact_key(f"Could not reach Gemini grounding API: {exc}")
                logger.warning("Gemini grounding request error: %s", err_text)
                return {"success": False, "error": err_text}

            try:
                data = resp.json()
            except Exception as exc:
                logger.warning("Gemini grounding response parse error: %s", exc)
                return {
                    "success": False,
                    "error": "Could not parse Gemini grounding response as JSON",
                }

            if isinstance(data, dict) and data.get("error"):
                err = data.get("error") or {}
                msg = str(err.get("message") or err.get("status") or "unknown")
                return {
                    "success": False,
                    "error": f"Gemini grounding API error: {_redact_key(msg[:300])}",
                }

            candidates = (data.get("candidates") if isinstance(data, dict) else None) or []
            if not candidates:
                block_reason = (
                    (data.get("promptFeedback") or {}).get("blockReason")
                    if isinstance(data, dict)
                    else None
                )
                if block_reason:
                    return {
                        "success": False,
                        "error": f"Gemini grounding blocked the query: {block_reason}",
                    }
                return {
                    "success": False,
                    "error": "Gemini grounding returned no candidates",
                }

            first = candidates[0] if isinstance(candidates[0], dict) else {}
            content_obj = first.get("content") or {}
            parts = content_obj.get("parts") or []
            answer_text = "\n".join(
                str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text")
            ).strip()

            gm = first.get("groundingMetadata") or {}
            chunks = gm.get("groundingChunks") or []

            web: List[Dict[str, Any]] = []
            for chunk in chunks[:safe_limit]:
                if not isinstance(chunk, dict):
                    continue
                web_meta = chunk.get("web") or {}
                raw_uri = str(web_meta.get("uri", "") or "").strip()
                if not raw_uri:
                    continue
                resolved = _resolve_citation_redirect_url(raw_uri, client)
                web.append({
                    "title": str(web_meta.get("title", "") or ""),
                    "url": resolved,
                    "description": "",
                    "position": len(web) + 1,
                })

            # Preserve raw grounding metadata so downstream UIs can render the
            # Search Suggestions chip and inline citations that Google's
            # Grounding Terms require apps with a user-visible UI to display.
            # Fields preserved verbatim:
            #   - search_entry_point (rendered HTML + sdk_blob for the chip)
            #   - grounding_supports (segment-to-chunk index map for inline cites)
            #   - web_search_queries (the actual Google Search queries Gemini ran)
            # We omit ``groundingChunks`` here because the same chunks (after
            # redirect resolution) are already in ``data.web``.
            grounding_metadata: Dict[str, Any] = {}
            for src_key, dst_key in (
                ("searchEntryPoint", "search_entry_point"),
                ("groundingSupports", "grounding_supports"),
                ("webSearchQueries", "web_search_queries"),
            ):
                if src_key in gm:
                    grounding_metadata[dst_key] = gm[src_key]

        logger.info(
            "Gemini grounding '%s': %d chunks (limit %d), answer %d chars",
            query,
            len(web),
            safe_limit,
            len(answer_text),
        )

        result_data: Dict[str, Any] = {
            "answer": answer_text,
            "web": web,
        }
        if grounding_metadata:
            result_data["grounding_metadata"] = grounding_metadata

        return {
            "success": True,
            "data": result_data,
        }


# Re-export private helpers for unit testing.
__all__ = [
    "GeminiGroundingSearchProvider",
]
