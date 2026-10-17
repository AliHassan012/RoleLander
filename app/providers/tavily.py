from __future__ import annotations

import asyncio
import ipaddress
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.providers.common import AttemptBudgetExceeded, ProviderError

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
TRACKING_KEYS = {"gclid", "fbclid", "dclid", "msclkid", "mc_cid", "mc_eid"}


@dataclass(frozen=True)
class SearchResult:
    id: str
    title: str
    url: str
    content: str
    provider_rank: int


@dataclass(frozen=True)
class SearchOutcome:
    results: list[SearchResult]
    request_id: str | None
    credits: int | None


@dataclass(frozen=True)
class ExtractedPage:
    result_id: str
    url: str
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class ExtractFailure:
    result_id: str
    url: str
    code: str


@dataclass(frozen=True)
class ExtractOutcome:
    successes: list[ExtractedPage]
    failures: list[ExtractFailure]
    request_id: str | None


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query, doseq=True), ""))


def _safe_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.port not in {None, 443}:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or ".local" in host or host.endswith(".internal"):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return address.is_global
    except (ValueError, UnicodeError):
        return False


class TavilyAdapter:
    def __init__(
        self,
        api_key: str | None,
        *,
        client: httpx.AsyncClient | None = None,
        reserve_search_attempt: Callable[[], bool] | None = None,
        reserve_extract_attempt: Callable[[], bool] | None = None,
        timeout_seconds: float = 20.0,
    ):
        self._api_key = api_key
        self._client = client
        self._reserve_search_attempt = reserve_search_attempt
        self._reserve_extract_attempt = reserve_extract_attempt
        self.timeout_seconds = timeout_seconds

    async def _post(self, endpoint: str, payload: dict, reserve: Callable[[], bool] | None) -> dict:
        if not self._api_key:
            raise ProviderError("PROVIDER_NOT_CONFIGURED", "Tavily is not configured.")
        if self._client is None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds)) as client:
                return await self._send(client, endpoint, payload, reserve)
        return await self._send(self._client, endpoint, payload, reserve)

    async def _send(self, client: httpx.AsyncClient, endpoint: str, payload: dict, reserve: Callable[[], bool] | None) -> dict:
        for attempt in range(2):
            if reserve is not None and not reserve():
                raise AttemptBudgetExceeded("Tavily")
            try:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                raise ProviderError("PROVIDER_UNAVAILABLE", "Tavily could not be reached.", retryable=True) from exc
            if response.status_code in {401, 403}:
                raise ProviderError("PROVIDER_AUTH_FAILED", "Tavily credentials were rejected.", status_code=response.status_code)
            if response.status_code in {429, 432, 433}:
                raise ProviderError("PROVIDER_QUOTA", "Tavily request quota is unavailable.", retryable=True, status_code=response.status_code)
            if response.status_code >= 500:
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                raise ProviderError("PROVIDER_UNAVAILABLE", "Tavily returned a temporary service error.", retryable=True, status_code=response.status_code)
            if response.status_code >= 400:
                raise ProviderError("PROVIDER_REQUEST_REJECTED", "Tavily rejected the request.", status_code=response.status_code)
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError("PROVIDER_RESPONSE_INVALID", "Tavily returned an invalid response.") from exc
            if not isinstance(data, dict):
                raise ProviderError("PROVIDER_RESPONSE_INVALID", "Tavily returned an invalid response.")
            return data
        raise ProviderError("PROVIDER_UNAVAILABLE", "Tavily could not be reached.", retryable=True)

    async def search(self, query: str, *, max_results: int = 8) -> SearchOutcome:
        if not query.strip() or len(query) > 500:
            raise ValueError("Search query must contain 1 to 500 characters")
        if not 1 <= max_results <= 8:
            raise ValueError("Search result cap is 8")
        payload = {
            "query": query,
            "search_depth": "basic",
            "auto_parameters": False,
            "topic": "general",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_usage": True,
        }
        data = await self._post(TAVILY_SEARCH_URL, payload, self._reserve_search_attempt)
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise ProviderError("PROVIDER_RESPONSE_INVALID", "Tavily returned no result list.")
        results: list[SearchResult] = []
        for index, raw in enumerate(raw_results[:max_results]):
            if not isinstance(raw, dict):
                continue
            url = raw.get("url")
            title = raw.get("title")
            content = raw.get("content")
            if not isinstance(url, str) or not isinstance(title, str) or not isinstance(content, str) or not _safe_https_url(url):
                continue
            results.append(SearchResult(str(uuid.uuid4()), title[:500], url, content[:5000], index))
        usage = data.get("usage") or {}
        credits = usage.get("credits") if isinstance(usage, dict) else None
        return SearchOutcome(results, data.get("request_id") if isinstance(data.get("request_id"), str) else None,
                             credits if isinstance(credits, int) else None)

    async def extract_saved(
        self,
        result_ids: Sequence[str],
        saved_results: Mapping[str, SearchResult],
    ) -> ExtractOutcome:
        if not result_ids or len(result_ids) > 5:
            raise ValueError("Extract accepts 1 to 5 saved search result IDs")
        if len(set(result_ids)) != len(result_ids):
            raise ValueError("Extract result IDs must be unique")
        selected: list[SearchResult] = []
        for result_id in result_ids:
            result = saved_results.get(result_id)
            if result is None:
                raise ValueError("Extract may use only search results saved for this run")
            if not _safe_https_url(result.url):
                raise ValueError("Saved result is not an allowed public HTTPS URL")
            selected.append(result)
        urls = [result.url for result in selected]
        payload = {
            "urls": urls,
            "extract_depth": "basic",
            "format": "text",
            "include_images": False,
            "include_usage": True,
            "timeout": 20,
        }
        data = await self._post(TAVILY_EXTRACT_URL, payload, self._reserve_extract_attempt)
        by_url = {canonical_url(result.url): result for result in selected}
        successes: list[ExtractedPage] = []
        failures: list[ExtractFailure] = []
        success_urls: set[str] = set()
        raw_successes = data.get("results") if isinstance(data.get("results"), list) else []
        raw_failures = data.get("failed_results") if isinstance(data.get("failed_results"), list) else []
        for raw in raw_successes:
            if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or not isinstance(raw.get("raw_content"), str):
                continue
            key = canonical_url(raw["url"])
            result = by_url.get(key)
            if result is None:
                continue
            success_urls.add(key)
            raw_text = raw["raw_content"]
            successes.append(ExtractedPage(result.id, result.url, raw_text[:20000], len(raw_text) > 20000))
        for raw in raw_failures:
            if not isinstance(raw, dict) or not isinstance(raw.get("url"), str):
                continue
            key = canonical_url(raw["url"])
            result = by_url.get(key)
            if result is not None:
                failures.append(ExtractFailure(result.id, result.url, "EXTRACTION_UNAVAILABLE"))
        reported_failures = {item.result_id for item in failures}
        for result in selected:
            key = canonical_url(result.url)
            if key not in success_urls and result.id not in reported_failures:
                failures.append(ExtractFailure(result.id, result.url, "EXTRACTION_UNAVAILABLE"))
        request_id = data.get("request_id") if isinstance(data.get("request_id"), str) else None
        return ExtractOutcome(successes, failures, request_id)

