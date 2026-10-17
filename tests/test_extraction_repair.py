import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.assessment import extract_opportunity_fields, validate_extraction_evidence
from app.models import OpportunityFields, SourceDocument
from app.providers.common import ProviderError
from app.providers.gemini import GeminiAdapter


def drain(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("Mock transport must not await live network")


@pytest.mark.parametrize("first_response,repair_valid", [("evidence", True), ("evidence", False), ("schema", True), ("quota", True)])
def test_extraction_repair_is_bounded_and_preserves_evidence_checks(first_response, repair_valid):
    fixture = json.loads((Path(__file__).parent / "fixtures/sample_opportunity.json").read_text(encoding="utf-8"))
    data = {key: value for key, value in fixture.items() if key in OpportunityFields.model_fields}
    source_id = fixture["source_id"]
    data["field_evidence"].extend([
        {"field": field, "evidence": {"source_id": source_id, "quote": quote}}
        for field, quote in [("employer", fixture["employer"]), ("opportunity_type", "Internship"), ("work_arrangement", "Hybrid")]
    ])
    text = ". ".join([ref["evidence"]["quote"] for ref in data["field_evidence"]] + [item["evidence"]["quote"] for item in data["requirements"]] + [ref["quote"] for ref in data["salary_details"]["evidence_refs"]])
    valid = {"page_type": "individual_job", "opportunity": data}
    invalid = copy.deepcopy(valid)
    invalid["opportunity"]["field_evidence"][0]["evidence"]["quote"] = "Fabricated quote absent from the saved page"
    document = SourceDocument(id=source_id, user_id=UUID(int=1), run_id=UUID(int=2), url=fixture["canonical_url"],
        title=fixture["title"], source_kind="extracted_page", body_text=text, truncated=False,
        retrieved_at=datetime.now(timezone.utc), published_at=None, extraction_status="retrieved", provider_request_id=None)
    requests = []
    reservations = []

    async def handler(request):
        requests.append(json.loads(request.content))
        if first_response == "quota":
            return httpx.Response(429, json={})
        value = ({} if first_response == "schema" else invalid) if len(requests) == 1 else (valid if repair_valid else invalid)
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(value)}]}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GeminiAdapter("fake", "mock", client=client, reserve_attempt=lambda: reservations.append(1) or True)
    if first_response == "quota" or not repair_valid:
        with pytest.raises(ProviderError) as error:
            drain(extract_opportunity_fields(adapter, document))
        assert error.value.code == ("PROVIDER_QUOTA" if first_response == "quota" else "EVIDENCE_INVALID")
    else:
        output = drain(extract_opportunity_fields(adapter, document))
        validate_extraction_evidence(output, document.id, text)
    expected = 1 if first_response == "quota" else 2
    assert len(requests) == len(reservations) == expected
    if expected == 2:
        payload = json.loads(requests[1]["contents"][0]["parts"][0]["text"])
        assert payload["VALIDATION_FEEDBACK"]
        assert payload["SOURCE_TEXT"] == text and payload["SOURCE_ID"] == source_id
    drain(client.aclose())
