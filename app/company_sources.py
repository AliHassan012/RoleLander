from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.models import Company

MAX_POSTINGS = 200
MAX_PAGES = 4
PAGE_SIZE = 50


@dataclass
class FeedOutcome:
    status: str
    postings: list[dict[str, Any]]
    pages_fetched: int
    error: str | None = None


def _identifier(company: Company) -> str:
    value = company.adapter_identifier or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise ValueError("The verified board identifier is invalid")
    return value


def _safe_posting(url: Any, allowed_hosts: set[str]) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        return None
    if parsed.hostname.lower() not in allowed_hosts:
        return None
    return url


async def fetch_company_feed(company: Company, client: httpx.AsyncClient | None = None) -> FeedOutcome:
    """Read a public ATS feed using a fixed provider host and hard page/result limits."""
    if company.verified_at is None or company.adapter not in {"greenhouse", "lever"}:
        return FeedOutcome("failed", [], 0, "This company does not have a verified Greenhouse or Lever board.")
    try:
        identifier = _identifier(company)
    except ValueError:
        return FeedOutcome("failed", [], 0, "The verified board identifier is invalid.")
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)
    postings: list[dict[str, Any]] = []
    pages = 0
    truncated = False
    host = "api.eu.lever.co" if company.adapter == "lever" and company.adapter_region == "eu" else (
        "api.lever.co" if company.adapter == "lever" else "boards-api.greenhouse.io"
    )
    allowed_posting_hosts = {"boards.greenhouse.io", "job-boards.greenhouse.io"} if company.adapter == "greenhouse" else {"jobs.lever.co", "jobs.eu.lever.co"}
    try:
        for page in range(MAX_PAGES):
            if len(postings) >= MAX_POSTINGS:
                truncated = True
                break
            if company.adapter == "greenhouse":
                url = f"https://{host}/v1/boards/{identifier}/jobs"
                params = {"content": "true", "page": page + 1}
            else:
                url = f"https://{host}/v0/postings/{identifier}"
                params = {"mode": "json", "skip": page * PAGE_SIZE, "limit": min(PAGE_SIZE, MAX_POSTINGS - len(postings))}
            response = await client.get(url, params=params, headers={"Accept": "application/json"})
            pages += 1
            if response.status_code != 200:
                return FeedOutcome("partial" if postings else "failed", postings, pages, f"Board returned HTTP {response.status_code}.")
            payload = response.json()
            items = payload.get("jobs", []) if company.adapter == "greenhouse" and isinstance(payload, dict) else payload
            if not isinstance(items, list):
                return FeedOutcome("partial" if postings else "failed", postings, pages, "Board response did not contain a posting list.")
            raw_count = len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                if company.adapter == "greenhouse":
                    posting_url = _safe_posting(item.get("absolute_url"), allowed_posting_hosts)
                    title = item.get("title")
                    location = (item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else None
                    updated = item.get("updated_at")
                    description = item.get("content")
                    stable_id = item.get("id")
                else:
                    posting_url = _safe_posting(item.get("hostedUrl"), allowed_posting_hosts)
                    categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
                    title = item.get("text")
                    location = categories.get("location")
                    updated = item.get("createdAt")
                    description = item.get("descriptionPlain")
                    stable_id = item.get("id")
                if posting_url and isinstance(title, str) and isinstance(stable_id, (str, int)):
                    postings.append({"posting_id":str(stable_id),"title":title[:300],"url":posting_url,"location":location[:300] if isinstance(location,str) else None,
                        "updated_at":str(updated)[:80] if updated is not None else None,"description":description[:20000] if isinstance(description,str) else None})
                    if len(postings) >= MAX_POSTINGS:
                        truncated = raw_count > len(postings) or page < MAX_PAGES - 1
                        break
            if raw_count < PAGE_SIZE:
                break
        status = "partial" if truncated or pages == MAX_PAGES else "complete"
        return FeedOutcome(status, postings[:MAX_POSTINGS], pages)
    except (httpx.TimeoutException, httpx.NetworkError):
        return FeedOutcome("partial" if postings else "failed", postings, pages, "The public company board is temporarily unreachable.")
    except (ValueError, TypeError):
        return FeedOutcome("partial" if postings else "failed", postings, pages, "The public company board returned invalid data.")
    finally:
        if owns_client:
            await client.aclose()
