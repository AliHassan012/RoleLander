from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.providers.common import AttemptBudgetExceeded, ProviderError

OutputModel = TypeVar("OutputModel", bound=BaseModel)
GEMINI_HOST = "https://generativelanguage.googleapis.com"


class GeminiAdapter:
    def __init__(
        self,
        api_key: str | None,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
        reserve_attempt: Callable[[], bool] | None = None,
        timeout_seconds: float = 35.0,
    ):
        self._api_key = api_key
        self.model = model
        self._client = client
        self._reserve_attempt = reserve_attempt
        self.timeout_seconds = timeout_seconds

    def _reserve(self) -> None:
        if self._reserve_attempt is not None and not self._reserve_attempt():
            raise AttemptBudgetExceeded("Gemini")

    async def _post(self, payload: dict) -> dict:
        if not self._api_key:
            raise ProviderError("PROVIDER_NOT_CONFIGURED", "Gemini is not configured.")
        if self._client is None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds)) as client:
                return await self._send(client, payload)
        return await self._send(self._client, payload)

    async def _send(self, client: httpx.AsyncClient, payload: dict) -> dict:
        url = f"{GEMINI_HOST}/v1beta/models/{self.model}:generateContent"
        for attempt in range(2):
            self._reserve()
            try:
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                raise ProviderError("PROVIDER_UNAVAILABLE", "Gemini could not be reached.", retryable=True) from exc
            if response.status_code in {401, 403}:
                raise ProviderError("PROVIDER_AUTH_FAILED", "Gemini credentials were rejected.", status_code=response.status_code)
            if response.status_code in {429, 432, 433}:
                raise ProviderError("PROVIDER_QUOTA", "Gemini request quota is unavailable.", retryable=True, status_code=response.status_code)
            if response.status_code >= 500:
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                raise ProviderError("PROVIDER_UNAVAILABLE", "Gemini returned a temporary service error.", retryable=True, status_code=response.status_code)
            if response.status_code >= 400:
                raise ProviderError("PROVIDER_REQUEST_REJECTED", "Gemini rejected the structured request.", status_code=response.status_code)
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError("PROVIDER_RESPONSE_INVALID", "Gemini returned an invalid response.") from exc
            if not isinstance(data, dict):
                raise ProviderError("PROVIDER_RESPONSE_INVALID", "Gemini returned an invalid response.")
            return data
        raise ProviderError("PROVIDER_UNAVAILABLE", "Gemini could not be reached.", retryable=True)

    @staticmethod
    def _candidate_text(response: dict) -> str:
        feedback = response.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ProviderError("MODEL_BLOCKED", "Gemini blocked this request.")
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ProviderError("MODEL_RESPONSE_MISSING", "Gemini did not return a usable response.")
        candidate = candidates[0]
        if candidate.get("finishReason") != "STOP":
            raise ProviderError("MODEL_RESPONSE_INCOMPLETE", "Gemini did not finish the structured response.")
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        text_parts = [part.get("text", "") for part in parts if isinstance(part, dict) and not part.get("thought")]
        result = "".join(part for part in text_parts if isinstance(part, str)).strip()
        if not result:
            raise ProviderError("MODEL_RESPONSE_MISSING", "Gemini did not return structured text.")
        return result

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_payload: dict,
        output_model: type[OutputModel],
        max_attempts: int = 2,
    ) -> OutputModel:
        if not 1 <= max_attempts <= 2:
            raise ValueError("Structured generation allows one call and at most one repair call")
        schema = output_model.model_json_schema()
        prompt_payload = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        correction: str | None = None
        last_issue = "Invalid JSON"
        for attempt in range(max_attempts):
            user_text = prompt_payload
            if correction:
                user_text += "\n\nThe prior answer failed local validation. Correct it once. Validation issue: " + correction
            request_payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_text}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            }
            response = await self._post(request_payload)
            raw_text = self._candidate_text(response)
            try:
                value = json.loads(raw_text)
                return output_model.model_validate(value)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                if isinstance(exc, ValidationError):
                    last_issue = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['type']}" for error in exc.errors())[:1200]
                else:
                    last_issue = "Response was not valid JSON matching the required schema"
                correction = last_issue
        raise ProviderError("MODEL_SCHEMA_INVALID", "Gemini did not return data matching the required schema.")

