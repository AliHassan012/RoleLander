from __future__ import annotations

import json
import httpx
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.providers.common import ProviderError
from app.providers.gemini import GeminiAdapter
from app.providers.tavily import SearchResult, TavilyAdapter, canonical_url


class SmallOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1)


class FakeRequest:
    def __init__(self, url, headers, json_body):
        self.url = urlsplit(url)
        self.headers = headers
        self.content = json.dumps(json_body).encode()


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def post(self, url, *, headers, json, timeout):
        self.requests.append(FakeRequest(url, headers, json))
        status_code, body = self.responses.pop(0)
        return FakeResponse(status_code, body)


def run_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("Fake provider operation unexpectedly yielded for network or sleep")


def gemini_response(text, finish="STOP"):
    return {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": text}]}}]}


def test_gemini_structured_output_validates_and_sends_server_key_only():
    client = FakeClient([(200, gemini_response('{"value":"ok"}'))])
    adapter = GeminiAdapter("local-test-secret", "test-model", client=client)
    result = run_immediate(adapter.generate_structured(system_prompt="Extract", user_payload={"text": "synthetic"}, output_model=SmallOutput))
    assert result.value == "ok"
    request = client.requests[0]
    assert request.url.geturl() == "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent"
    assert request.headers["x-goog-api-key"] == "local-test-secret"
    sent = json.loads(request.content)
    assert sent["generationConfig"]["responseMimeType"] == "application/json"
    assert sent["generationConfig"]["responseJsonSchema"]["additionalProperties"] is False


def test_assessment_payload_decimal_years_pass_through_real_gemini_adapter_mock_transport():
    from app.models import AssessOpportunityOutput, Opportunity, Preferences, Profile
    from app.runner import _assessment_payload

    fixture_dir = Path(__file__).parent / "fixtures"
    opportunity_data = json.loads((fixture_dir / "sample_opportunity.json").read_text(encoding="utf-8"))
    profile_data = json.loads((fixture_dir / "sample_profile.json").read_text(encoding="utf-8"))
    opportunity_data.pop("sample_label", None)
    profile_data.pop("sample_label", None)
    opportunity_data["minimum_years"] = "1.5"
    profile_data["professional_years"] = "2.5"
    opportunity = Opportunity.model_validate(opportunity_data)
    profile = Profile.model_validate(profile_data)
    preferences = Preferences.model_construct(role_queries=["data analyst"])
    captured = {}

    async def handler(request):
        payload = json.loads(request.content)
        captured["user_text"] = payload["contents"][0]["parts"][0]["text"]
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"requirement_matches":[],"role_alignment":"unknown","role_explanation":"No role context.","experience_alignment":"unknown"}'}]}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GeminiAdapter("mock-key", "mock-model", client=client)
    result = run_immediate(adapter.generate_structured(
        system_prompt="Assess", user_payload=_assessment_payload(opportunity, profile, preferences),
        output_model=AssessOpportunityOutput,
    ))
    assert result.experience_alignment == "unknown"
    payload_sent = json.loads(captured["user_text"])
    assert payload_sent["PROFILE_PROFESSIONAL_YEARS"] == 2.5
    assert payload_sent["JOB"]["minimum_years"] == 1.5
    run_immediate(client.aclose())


def test_gemini_repairs_schema_once_and_counts_each_attempt():
    client = FakeClient([
        (200, gemini_response('{"value": 3}')),
        (200, gemini_response('{"value":"fixed"}')),
    ])
    reservations = []
    adapter = GeminiAdapter("test-key", "test-model", client=client, reserve_attempt=lambda: reservations.append(1) is None)
    result = run_immediate(adapter.generate_structured(system_prompt="Extract", user_payload={"text": "sample"}, output_model=SmallOutput))
    assert result.value == "fixed"
    assert len(reservations) == len(client.requests) == 2
    assert "Validation issue" in json.loads(client.requests[1].content)["contents"][0]["parts"][0]["text"]


@pytest.mark.parametrize("status,code", [(401, "PROVIDER_AUTH_FAILED"), (429, "PROVIDER_QUOTA")])
def test_gemini_auth_and_quota_fail_without_retry(status, code):
    client = FakeClient([(status, {"message": "redacted"})])
    adapter = GeminiAdapter("test-key", "test-model", client=client)
    with pytest.raises(ProviderError) as error:
        run_immediate(adapter.generate_structured(system_prompt="x", user_payload={}, output_model=SmallOutput))
    assert error.value.code == code
    assert len(client.requests) == 1
    assert "test-key" not in str(error.value)


def test_gemini_malformed_output_gets_one_repair_then_fails_closed():
    client = FakeClient([(200, gemini_response("not json")), (200, gemini_response('{"value": 5}'))])
    adapter = GeminiAdapter("test-key", "test-model", client=client)
    with pytest.raises(ProviderError) as error:
        run_immediate(adapter.generate_structured(system_prompt="x", user_payload={}, output_model=SmallOutput))
    assert error.value.code == "MODEL_SCHEMA_INVALID"
    assert len(client.requests) == 2


def test_gemini_retries_one_transient_service_error_only():
    client = FakeClient([(503, {}), (200, gemini_response('{"value":"ok"}'))])
    reservations = []
    adapter = GeminiAdapter("test-key", "test-model", client=client, reserve_attempt=lambda: reservations.append(1) is None)

    async def no_delay(_seconds):
        return None

    with patch("app.providers.gemini.asyncio.sleep", new=no_delay):
        result = run_immediate(adapter.generate_structured(system_prompt="x", user_payload={}, output_model=SmallOutput))
    assert result.value == "ok"
    assert len(client.requests) == len(reservations) == 2


def test_gemini_rejects_incomplete_and_blocked_responses():
    for response, expected in [
        (gemini_response('{"value":"partial"}', finish="MAX_TOKENS"), "MODEL_RESPONSE_INCOMPLETE"),
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "MODEL_BLOCKED"),
    ]:
        adapter = GeminiAdapter("test-key", "test-model", client=FakeClient([(200, response)]))
        with pytest.raises(ProviderError) as error:
            run_immediate(adapter.generate_structured(system_prompt="x", user_payload={}, output_model=SmallOutput))
        assert error.value.code == expected


def test_tavily_search_and_mixed_extract_are_correlated_by_saved_result_id():
    client = FakeClient([
        (200, {
            "request_id": "search-1", "usage": {"credits": 1},
            "results": [
                {"title": "Sample A", "url": "https://jobs.example.invalid/a?job_id=42&utm_source=fixture", "content": "A preview"},
                {"title": "Sample B", "url": "https://jobs.example.invalid/b", "content": "B preview"},
            ],
        }),
        (200, {
            "request_id": "extract-1",
            "results": [{"url": "https://jobs.example.invalid/b", "raw_content": "Full vacancy B"}],
            "failed_results": [{"url": "https://jobs.example.invalid/a?job_id=42&utm_source=fixture", "error": "provider private error"}],
        }),
    ])
    adapter = TavilyAdapter("test-key", client=client)
    search = run_immediate(adapter.search("synthetic analyst roles PK"))
    saved = {result.id: result for result in search.results}
    extracted = run_immediate(adapter.extract_saved(list(saved), saved))
    assert len(search.results) == 2 and search.credits == 1
    assert len(extracted.successes) == 1 and extracted.successes[0].text == "Full vacancy B"
    assert len(extracted.failures) == 1 and extracted.failures[0].result_id == search.results[0].id
    assert extracted.failures[0].code == "EXTRACTION_UNAVAILABLE"
    search_payload = json.loads(client.requests[0].content)
    extract_payload = json.loads(client.requests[1].content)
    assert search_payload["max_results"] <= 8 and search_payload["include_raw_content"] is False
    assert extract_payload["urls"] == [item.url for item in search.results]
    assert len(extract_payload["urls"]) <= 5


def test_tavily_rejects_unpersisted_ids_and_private_or_arbitrary_urls():
    result = SearchResult("saved-1", "title", "https://127.0.0.1/private", "preview", 0)
    adapter = TavilyAdapter("test-key", client=FakeClient([]))
    with pytest.raises(ValueError, match="saved for this run"):
        run_immediate(adapter.extract_saved(["not-saved"], {result.id: result}))
    with pytest.raises(ValueError, match="public HTTPS"):
        run_immediate(adapter.extract_saved([result.id], {result.id: result}))
    with pytest.raises(ValueError):
        run_immediate(adapter.extract_saved(["saved-1"], {"saved-1": SearchResult("saved-1", "x", "http://public.example/job", "", 0)}))
    assert "job_id=42" in canonical_url("https://jobs.example/job?job_id=42&utm_source=sample")


def test_tavily_caps_extracted_text_and_drops_nonpublic_search_links():
    long_text = "x" * 20001
    client = FakeClient([
        (200, {"results": [
            {"title":"Public role", "url":"https://jobs.example.invalid/open", "content":"preview"},
            {"title":"Private target", "url":"https://127.0.0.1/admin", "content":"do not keep"},
        ]}),
        (200, {"results":[{"url":"https://jobs.example.invalid/open", "raw_content":long_text}]}),
    ])
    adapter = TavilyAdapter("test-key", client=client)
    search = run_immediate(adapter.search("synthetic public job"))
    assert len(search.results) == 1
    saved = {item.id:item for item in search.results}
    extracted = run_immediate(adapter.extract_saved(list(saved), saved))
    assert len(extracted.successes[0].text) == 20000
    assert extracted.successes[0].truncated is True


def test_tavily_auth_failure_is_not_retried():
    client = FakeClient([(401, {"detail": "no"})])
    adapter = TavilyAdapter("test-key", client=client)
    with pytest.raises(ProviderError) as error:
        run_immediate(adapter.search("nonpersonal synthetic job query"))
    assert error.value.code == "PROVIDER_AUTH_FAILED"
    assert len(client.requests) == 1

