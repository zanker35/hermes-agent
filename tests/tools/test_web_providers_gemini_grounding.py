"""Tests for the Gemini Grounding web search provider.

Covers:
- GeminiGroundingSearchProvider.is_configured() env var gating
- GeminiGroundingSearchProvider.search() — happy path with answer + chunks
- Auth via x-goog-api-key header (not URL query string)
- Vertex citation redirect URL resolution + fallback on failure
- description field is empty (Option A — answer carries the load)
- Multi-part text join with "\\n"
- HTTP errors: 429 rate-limit, 4xx with body message, 403, RequestError
- Malformed JSON, empty candidates, safety block
- API key redaction in error messages
- Custom GEMINI_GROUNDING_MODEL / GEMINI_GROUNDING_BASE_URL via env
- limit truncation
- _is_backend_available("gemini-grounding") integration
- _get_backend() recognizes "gemini-grounding" as configured + auto-detect priority
- check_web_api_key() includes gemini-grounding
- web_extract / web_crawl return search-only error when active backend
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import httpx


# ---------------------------------------------------------------------------
# Helpers — sample Gemini grounding response
# ---------------------------------------------------------------------------


_VERTEX_REDIRECT_BASE = "https://vertexaisearch.cloud.google.com/grounding-api-redirect"


def _sample_response(num_chunks: int = 2, answer: str = "Synthesized answer.") -> dict:
    """Build a minimal Gemini grounding response payload."""
    parts = [{"text": answer}] if answer else []
    chunks = [
        {
            "web": {
                "uri": f"{_VERTEX_REDIRECT_BASE}/chunk-{i}",
                "title": f"Source {i}",
            }
        }
        for i in range(num_chunks)
    ]
    return {
        "candidates": [
            {
                "content": {"parts": parts},
                "groundingMetadata": {"groundingChunks": chunks},
            }
        ]
    }


def _make_mock_post_response(status_code: int = 200, json_data: dict | None = None,
                              text: str | None = None, raises: Exception | None = None):
    """Build a MagicMock that mimics httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    if text is not None:
        resp.text = text
    if raises is not None:
        resp.raise_for_status.side_effect = raises
    else:
        resp.raise_for_status.return_value = None
    return resp


def _make_mock_head_response(final_url: str):
    """Build a MagicMock for httpx HEAD with `url` attribute."""
    resp = MagicMock()
    resp.url = final_url
    return resp


def _patched_client(post_resp=None, head_resp=None, post_raises=None, head_raises=None):
    """Context manager that patches httpx.Client to return a mocked client.

    Returns the mock client instance so tests can assert on .post/.head calls.
    """
    mock_client = MagicMock()
    if post_raises is not None:
        mock_client.post.side_effect = post_raises
    elif post_resp is not None:
        mock_client.post.return_value = post_resp

    if head_raises is not None:
        mock_client.head.side_effect = head_raises
    elif head_resp is not None:
        mock_client.head.return_value = head_resp
    else:
        # Default: HEAD returns a no-op resolved URL = original
        mock_client.head.side_effect = lambda url, **_: _make_mock_head_response(url)

    patcher = patch("httpx.Client")
    MockClient = patcher.start()
    MockClient.return_value.__enter__.return_value = mock_client
    MockClient.return_value.__exit__.return_value = False
    return patcher, mock_client


# ---------------------------------------------------------------------------
# is_configured / provider_name / ABC contract
# ---------------------------------------------------------------------------


class TestGeminiGroundingProviderIsConfigured:
    def test_configured_when_key_set(self, monkeypatch):
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "AIzaTest")
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider
        assert GeminiGroundingSearchProvider().is_configured() is True

    def test_not_configured_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("GEMINI_GROUNDING_API_KEY", raising=False)
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider
        assert GeminiGroundingSearchProvider().is_configured() is False

    def test_not_configured_when_key_whitespace(self, monkeypatch):
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "   ")
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider
        assert GeminiGroundingSearchProvider().is_configured() is False

    def test_provider_name(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider
        assert GeminiGroundingSearchProvider().provider_name() == "gemini-grounding"

    def test_implements_web_search_provider(self):
        from tools.web_providers.base import WebSearchProvider
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider
        assert issubclass(GeminiGroundingSearchProvider, WebSearchProvider)

    def test_separate_from_gemini_api_key(self, monkeypatch):
        """Setting GEMINI_API_KEY alone must NOT make grounding available — keys are decoupled."""
        monkeypatch.delenv("GEMINI_GROUNDING_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaOther")
        monkeypatch.setenv("GOOGLE_API_KEY", "AIzaOther")
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider
        assert GeminiGroundingSearchProvider().is_configured() is False


# ---------------------------------------------------------------------------
# search() — happy path, schema, header auth, redirect resolution
# ---------------------------------------------------------------------------


class TestGeminiGroundingProviderSearch:
    def setup_method(self):
        os.environ["GEMINI_GROUNDING_API_KEY"] = "AIzaTest"
        for k in ("GEMINI_GROUNDING_MODEL", "GEMINI_GROUNDING_BASE_URL"):
            os.environ.pop(k, None)

    def teardown_method(self):
        for k in ("GEMINI_GROUNDING_API_KEY", "GEMINI_GROUNDING_MODEL", "GEMINI_GROUNDING_BASE_URL"):
            os.environ.pop(k, None)

    def test_happy_path_returns_answer_and_web(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = _sample_response(num_chunks=2, answer="The synthesized answer.")
        post_resp = _make_mock_post_response(json_data=sample)
        head_resp = _make_mock_head_response("https://real.example.com/article")

        patcher, mock_client = _patched_client(post_resp=post_resp, head_resp=head_resp)
        try:
            result = GeminiGroundingSearchProvider().search("test query", limit=5)
        finally:
            patcher.stop()

        assert result["success"] is True
        assert result["data"]["answer"] == "The synthesized answer."
        assert len(result["data"]["web"]) == 2
        assert result["data"]["web"][0]["position"] == 1
        assert result["data"]["web"][1]["position"] == 2

    def test_auth_uses_header_not_query_string(self):
        """API key must travel in x-goog-api-key header, NOT in URL ?key=."""
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = _sample_response(num_chunks=0)
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, mock_client = _patched_client(post_resp=post_resp)
        try:
            GeminiGroundingSearchProvider().search("test query")
        finally:
            patcher.stop()

        # Inspect the post() call
        assert mock_client.post.called
        call_args = mock_client.post.call_args
        url_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
        kwargs = call_args.kwargs

        assert "key=" not in url_arg, f"API key leaked into URL: {url_arg}"
        assert "params" not in kwargs or "key" not in (kwargs.get("params") or {}), \
            "API key leaked into params"
        assert kwargs["headers"]["x-goog-api-key"] == "AIzaTest"

    def test_redirect_resolution_returns_real_url(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = _sample_response(num_chunks=1)
        post_resp = _make_mock_post_response(json_data=sample)
        # HEAD resolves redirect → real URL
        head_resp = _make_mock_head_response("https://real-source.com/article")
        patcher, mock_client = _patched_client(post_resp=post_resp, head_resp=head_resp)
        try:
            # Bypass DNS-based is_safe_url in tests — the real safety check is
            # exercised in TestRedirectSSRFDefense.  Here we only assert the
            # happy-path resolution wiring.
            with patch("tools.url_safety.is_safe_url", return_value=True):
                result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["data"]["web"][0]["url"] == "https://real-source.com/article"
        # The HEAD must have been called with the vertex redirect URL
        head_call_url = mock_client.head.call_args.args[0]
        assert "vertexaisearch.cloud.google.com" in head_call_url

    def test_redirect_failure_falls_back_to_original_url(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = _sample_response(num_chunks=1)
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, mock_client = _patched_client(
            post_resp=post_resp,
            head_raises=httpx.TimeoutException("HEAD timed out"),
        )
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        # On HEAD failure, return the original vertex redirect URL
        assert result["data"]["web"][0]["url"].startswith(_VERTEX_REDIRECT_BASE)

    def test_description_is_empty(self):
        """Locks Option A — description is intentionally empty for grounding backend."""
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = _sample_response(num_chunks=3)
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, mock_client = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        for entry in result["data"]["web"]:
            assert entry["description"] == ""

    def test_multi_part_text_joined_with_newline(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "Para 1"}, {"text": "Para 2"}]},
                    "groundingMetadata": {"groundingChunks": []},
                }
            ]
        }
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["data"]["answer"] == "Para 1\nPara 2"

    def test_no_grounding_metadata_returns_answer_with_empty_web(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = {
            "candidates": [
                {"content": {"parts": [{"text": "Direct answer from training."}]}}
            ]
        }
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["success"] is True
        assert result["data"]["answer"] == "Direct answer from training."
        assert result["data"]["web"] == []

    def test_empty_grounding_chunks(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = {
            "candidates": [
                {"content": {"parts": [{"text": "Answer"}]},
                 "groundingMetadata": {"groundingChunks": []}}
            ]
        }
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["success"] is True
        assert result["data"]["web"] == []

    def test_limit_truncates_chunks(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = _sample_response(num_chunks=5)
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query", limit=2)
        finally:
            patcher.stop()

        assert len(result["data"]["web"]) == 2

    def test_missing_key_returns_failure(self, monkeypatch):
        monkeypatch.delenv("GEMINI_GROUNDING_API_KEY", raising=False)
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider
        result = GeminiGroundingSearchProvider().search("query")
        assert result["success"] is False
        assert "GEMINI_GROUNDING_API_KEY" in result["error"]

    def test_http_429_returns_rate_limit_message(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        err_resp = MagicMock(spec=httpx.Response)
        err_resp.status_code = 429
        err_resp.text = "Too Many Requests"
        post_resp = _make_mock_post_response(
            status_code=429,
            text="Too Many Requests",
            raises=httpx.HTTPStatusError(
                "429", request=MagicMock(), response=err_resp,
            ),
        )
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["success"] is False
        assert "429" in result["error"] or "rate-limited" in result["error"].lower()

    def test_http_400_surfaces_error_message(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        err_body = {"error": {"code": 400, "message": "Invalid model name"}}
        err_resp = MagicMock(spec=httpx.Response)
        err_resp.status_code = 400
        err_resp.json.return_value = err_body
        err_resp.text = json.dumps(err_body)
        post_resp = _make_mock_post_response(
            status_code=400,
            text=err_resp.text,
            raises=httpx.HTTPStatusError(
                "400", request=MagicMock(), response=err_resp,
            ),
        )
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["success"] is False
        assert "400" in result["error"]
        assert "Invalid model name" in result["error"]

    def test_http_403_returns_failure(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        err_resp = MagicMock(spec=httpx.Response)
        err_resp.status_code = 403
        err_resp.json.return_value = {"error": {"message": "Forbidden"}}
        err_resp.text = "Forbidden"
        post_resp = _make_mock_post_response(
            status_code=403,
            raises=httpx.HTTPStatusError("403", request=MagicMock(), response=err_resp),
        )
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["success"] is False
        assert "403" in result["error"]

    def test_request_error_returns_failure(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        patcher, _ = _patched_client(post_raises=httpx.ConnectError("conn refused"))
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["success"] is False
        assert "Could not reach" in result["error"] or "Gemini" in result["error"]

    def test_malformed_json_returns_failure(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        post_resp = MagicMock(spec=httpx.Response)
        post_resp.status_code = 200
        post_resp.raise_for_status.return_value = None
        post_resp.json.side_effect = ValueError("not JSON")
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["success"] is False
        assert "parse" in result["error"].lower() or "JSON" in result["error"]

    def test_safety_block_returns_failure(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert result["success"] is False
        assert "SAFETY" in result["error"]

    def test_error_text_redacts_leaked_key(self):
        """If Gemini echoes the API key in error body, the returned error string must mask it."""
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        err_body = {"error": {"message": "Bad request with key=AIzaSyXXXXX"}}
        err_resp = MagicMock(spec=httpx.Response)
        err_resp.status_code = 400
        err_resp.json.return_value = err_body
        err_resp.text = json.dumps(err_body)
        post_resp = _make_mock_post_response(
            status_code=400,
            raises=httpx.HTTPStatusError("400", request=MagicMock(), response=err_resp),
        )
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert "AIzaSyXXXXX" not in result["error"]
        assert "key=***" in result["error"]

    def test_model_via_env(self, monkeypatch):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        monkeypatch.setenv("GEMINI_GROUNDING_MODEL", "gemini-3.1-flash-lite")
        sample = _sample_response(num_chunks=0)
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, mock_client = _patched_client(post_resp=post_resp)
        try:
            GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        url_arg = mock_client.post.call_args.args[0]
        assert "gemini-3.1-flash-lite" in url_arg

    def test_default_model_uses_flash_lite(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = _sample_response(num_chunks=0)
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, mock_client = _patched_client(post_resp=post_resp)
        try:
            GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        url_arg = mock_client.post.call_args.args[0]
        assert "/models/gemini-3.1-flash-lite:generateContent" in url_arg

    def test_custom_base_url_via_env(self, monkeypatch):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        monkeypatch.setenv("GEMINI_GROUNDING_BASE_URL", "https://proxy.example/v1beta")
        sample = _sample_response(num_chunks=0)
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, mock_client = _patched_client(post_resp=post_resp)
        try:
            GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        url_arg = mock_client.post.call_args.args[0]
        assert url_arg.startswith("https://proxy.example/v1beta/")


# ---------------------------------------------------------------------------
# Grounding metadata preservation (TOS / inline-citation rendering)
# ---------------------------------------------------------------------------


class TestGroundingMetadataPreservation:
    """Google's Grounding terms require apps with a user-visible UI to display
    Search Suggestions (from ``searchEntryPoint``) and inline citations (from
    ``groundingSupports``).  The provider must preserve these raw fields so
    downstream consumers can render them.
    """

    def setup_method(self):
        os.environ["GEMINI_GROUNDING_API_KEY"] = "AIzaTest"

    def teardown_method(self):
        os.environ.pop("GEMINI_GROUNDING_API_KEY", None)

    def test_search_entry_point_preserved(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "answer"}]},
                    "groundingMetadata": {
                        "groundingChunks": [],
                        "searchEntryPoint": {
                            "renderedContent": "<style>...</style><div>Suggestions chip HTML</div>",
                            "sdkBlob": "Zm9vYmFy",
                        },
                    },
                }
            ]
        }
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        gm = result["data"]["grounding_metadata"]
        assert gm["search_entry_point"]["renderedContent"].startswith("<style>")
        assert gm["search_entry_point"]["sdkBlob"] == "Zm9vYmFy"

    def test_grounding_supports_preserved(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "answer with citations"}]},
                    "groundingMetadata": {
                        "groundingChunks": [],
                        "groundingSupports": [
                            {
                                "segment": {"startIndex": 0, "endIndex": 6, "text": "answer"},
                                "groundingChunkIndices": [0],
                                "confidenceScores": [0.95],
                            }
                        ],
                    },
                }
            ]
        }
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        supports = result["data"]["grounding_metadata"]["grounding_supports"]
        assert len(supports) == 1
        assert supports[0]["segment"]["text"] == "answer"
        assert supports[0]["groundingChunkIndices"] == [0]

    def test_web_search_queries_preserved(self):
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "answer"}]},
                    "groundingMetadata": {
                        "groundingChunks": [],
                        "webSearchQueries": ["actual google query 1", "actual google query 2"],
                    },
                }
            ]
        }
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        queries = result["data"]["grounding_metadata"]["web_search_queries"]
        assert queries == ["actual google query 1", "actual google query 2"]

    def test_no_grounding_metadata_omits_key(self):
        """When response has no metadata fields, ``data.grounding_metadata``
        should not appear (don't emit an empty dict)."""
        from tools.web_providers.gemini_grounding import GeminiGroundingSearchProvider

        sample = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "answer"}]},
                    "groundingMetadata": {"groundingChunks": []},
                }
            ]
        }
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result = GeminiGroundingSearchProvider().search("query")
        finally:
            patcher.stop()

        assert "grounding_metadata" not in result["data"]


# ---------------------------------------------------------------------------
# Redirect resolution — SSRF defense
# ---------------------------------------------------------------------------


class TestRedirectSSRFDefense:
    """``_resolve_citation_redirect_url`` is the only place this provider
    actively dials out to a URL the LLM/Google chose.  It must:
      1. Refuse to follow redirects whose initial host is not a Gemini
         vertex-redirect host (defense against API drift / compromise).
      2. Run the final resolved URL through ``is_safe_url`` so a malicious
         or accidental redirect to an internal target is rejected.
    """

    def setup_method(self):
        os.environ["GEMINI_GROUNDING_API_KEY"] = "AIzaTest"

    def teardown_method(self):
        os.environ.pop("GEMINI_GROUNDING_API_KEY", None)

    def test_non_vertex_initial_host_returns_url_unchanged_without_head(self):
        """If Gemini ever returns a non-redirect URL (compromise, API change,
        error), we must NOT actively fetch it."""
        from tools.web_providers.gemini_grounding import _resolve_citation_redirect_url

        mock_client = MagicMock()
        # Should never be called
        mock_client.head.side_effect = AssertionError("HEAD must not be sent for non-vertex host")

        suspect = "https://attacker.example.com/grounding-api-redirect/xyz"
        assert _resolve_citation_redirect_url(suspect, mock_client) == suspect
        assert mock_client.head.call_count == 0

    def test_vertex_host_with_unsafe_final_url_falls_back_to_original(self):
        """When ``is_safe_url`` rejects the final URL (e.g. it resolved to an
        internal IP), return the original redirect — don't surface a URL we
        wouldn't otherwise fetch."""
        from tools.web_providers import gemini_grounding

        original = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
        mock_client = MagicMock()
        mock_client.head.return_value = _make_mock_head_response("http://10.0.0.1/internal")

        with patch.object(gemini_grounding, "_resolve_citation_redirect_url",
                          wraps=gemini_grounding._resolve_citation_redirect_url):
            with patch("tools.url_safety.is_safe_url", return_value=False):
                result = gemini_grounding._resolve_citation_redirect_url(original, mock_client)
        assert result == original

    def test_vertex_host_with_safe_final_url_returns_resolved(self):
        from tools.web_providers import gemini_grounding

        original = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
        mock_client = MagicMock()
        mock_client.head.return_value = _make_mock_head_response("https://real-source.example.com/article")

        with patch("tools.url_safety.is_safe_url", return_value=True):
            result = gemini_grounding._resolve_citation_redirect_url(original, mock_client)
        assert result == "https://real-source.example.com/article"

    def test_vertex_subdomain_also_accepted(self):
        """Future-proof: ``*.vertexaisearch.cloud.google.com`` subdomains
        should still be accepted, not only the bare apex."""
        from tools.web_providers import gemini_grounding

        url = "https://us-central1.vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
        mock_client = MagicMock()
        mock_client.head.return_value = _make_mock_head_response("https://real.example.com/a")

        with patch("tools.url_safety.is_safe_url", return_value=True):
            result = gemini_grounding._resolve_citation_redirect_url(url, mock_client)
        assert result == "https://real.example.com/a"
        assert mock_client.head.call_count == 1


# ---------------------------------------------------------------------------
# _redact_key helper
# ---------------------------------------------------------------------------


class TestRedactKey:
    def test_redacts_query_string_key(self):
        from tools.web_providers.gemini_grounding import _redact_key
        assert _redact_key("error: key=AIzaSyABC123") == "error: key=***"

    def test_redacts_header_form(self):
        from tools.web_providers.gemini_grounding import _redact_key
        # Header-style mention in error body
        assert "AIzaSyABC123" not in _redact_key("x-goog-api-key: AIzaSyABC123 was invalid")

    def test_idempotent(self):
        from tools.web_providers.gemini_grounding import _redact_key
        once = _redact_key("key=AIzaSyABC123")
        twice = _redact_key(once)
        assert once == twice

    def test_no_key_passthrough(self):
        from tools.web_providers.gemini_grounding import _redact_key
        assert _redact_key("plain error message") == "plain error message"


# ---------------------------------------------------------------------------
# Integration: _is_backend_available
# ---------------------------------------------------------------------------


class TestIsBackendAvailable:
    def test_gemini_grounding_available_when_key_set(self, monkeypatch):
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "AIzaTest")
        from tools.web_tools import _is_backend_available
        assert _is_backend_available("gemini-grounding") is True

    def test_gemini_grounding_unavailable_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("GEMINI_GROUNDING_API_KEY", raising=False)
        from tools.web_tools import _is_backend_available
        assert _is_backend_available("gemini-grounding") is False


# ---------------------------------------------------------------------------
# Integration: _get_backend()
# ---------------------------------------------------------------------------


class TestGetBackendGeminiGrounding:
    def _isolate_env(self, monkeypatch):
        for v in (
            "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "PARALLEL_API_KEY",
            "TAVILY_API_KEY", "EXA_API_KEY", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY",
        ):
            monkeypatch.delenv(v, raising=False)
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)

    def test_configured_gemini_grounding_returns_backend(self, monkeypatch):
        from tools import web_tools
        self._isolate_env(monkeypatch)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "gemini-grounding"},
        )
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "AIzaTest")
        assert web_tools._get_backend() == "gemini-grounding"

    def test_auto_detect_does_not_pick_gemini_grounding_over_free(self, monkeypatch):
        """Lock the decision: with both SEARXNG and grounding keys set, auto-detect picks searxng."""
        from tools import web_tools
        self._isolate_env(monkeypatch)
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "AIzaTest")
        assert web_tools._get_backend() == "searxng"

    def test_auto_detect_picks_gemini_grounding_when_only_key_set(self, monkeypatch):
        """When grounding key is the only credential, fall through to it as last-resort."""
        from tools import web_tools
        self._isolate_env(monkeypatch)
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "AIzaTest")
        assert web_tools._get_backend() == "gemini-grounding"


# ---------------------------------------------------------------------------
# Integration: check_web_api_key
# ---------------------------------------------------------------------------


class TestCheckWebApiKey:
    def test_gemini_grounding_satisfies_check_web_api_key(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "gemini-grounding"},
        )
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "AIzaTest")
        assert web_tools.check_web_api_key() is True


# ---------------------------------------------------------------------------
# search-only: web_extract / web_crawl return clear errors
# ---------------------------------------------------------------------------


class TestGeminiGroundingExtractCrawlErrors:
    def _activate_grounding(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "gemini-grounding"},
        )
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "AIzaTest")
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "check_firecrawl_api_key", lambda: False)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

    def test_web_crawl_returns_search_only_error(self, monkeypatch):
        import asyncio
        from tools import web_tools
        self._activate_grounding(monkeypatch)
        result_str = asyncio.get_event_loop().run_until_complete(
            web_tools.web_crawl_tool("https://example.com")
        )
        result = json.loads(result_str)
        assert result["success"] is False
        assert "search-only" in result["error"].lower() or "Gemini Grounding" in result["error"]

    def test_web_extract_returns_search_only_error(self, monkeypatch):
        import asyncio
        from tools import web_tools
        self._activate_grounding(monkeypatch)
        result_str = asyncio.get_event_loop().run_until_complete(
            web_tools.web_extract_tool(["https://example.com"])
        )
        result = json.loads(result_str)
        assert result["success"] is False
        assert "search-only" in result["error"].lower() or "Gemini Grounding" in result["error"]


# ---------------------------------------------------------------------------
# Integration: web_search_tool dispatch routes JSON with answer + web
# ---------------------------------------------------------------------------


class TestWebSearchToolDispatch:
    def test_dispatches_to_gemini_grounding(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "gemini-grounding"},
        )
        monkeypatch.setenv("GEMINI_GROUNDING_API_KEY", "AIzaTest")
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        sample = _sample_response(num_chunks=2, answer="Combined answer.")
        post_resp = _make_mock_post_response(json_data=sample)
        patcher, _ = _patched_client(post_resp=post_resp)
        try:
            result_str = web_tools.web_search_tool("foo", limit=3)
        finally:
            patcher.stop()

        result = json.loads(result_str)
        assert result["success"] is True
        assert "answer" in result["data"]
        assert "web" in result["data"]
        assert result["data"]["answer"] == "Combined answer."
        assert len(result["data"]["web"]) == 2
