"""Adzuna India job search client.

Endpoint: GET http://api.adzuna.com/v1/api/jobs/in/search/{page}
Docs:     https://developer.adzuna.com/docs/search
Free tier: 1000 calls/month (sufficient for ~50 active users at MVP scan rates)

Response shape (relevant fields only):
{
  "count": 1700,
  "mean": 1234567.0,
  "results": [
    {
      "id": "5092891234",
      "title": "Python Developer",
      "company": {"display_name": "TCS"},
      "location": {
        "area": ["IN", "Karnataka", "Bengaluru"],
        "display_name": "Bengaluru, Karnataka"
      },
      "description": "...",                  -- snippet, ~250 chars; OK for v1
      "salary_min": 1200000.0,
      "salary_max": 1800000.0,
      "salary_is_predicted": "0",            -- string "0"/"1", coerce to bool
      "created": "2026-05-28T11:30:00Z",
      "redirect_url": "https://www.adzuna.in/land/ad/...",
      "contract_type": "permanent",
      "contract_time": "full_time",
      "category": {"label": "IT Jobs", "tag": "it-jobs"}
    }
  ]
}

Pagination: `/search/1`, `/search/2`, ... up to 50 results per page.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

from core.models import JobSource
from core.scrapers.base import BaseScraper, ScrapedJob

log = logging.getLogger(__name__)

# HTTPS is mandatory — Adzuna returns 301 for plain http://
_BASE_URL = "https://api.adzuna.com/v1/api/jobs/in/search/{page}"
_MAX_PER_PAGE = 50


class AdzunaScraper(BaseScraper):
    source = JobSource.adzuna

    def __init__(
        self,
        app_id: str | None = None,
        app_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.app_id = app_id or os.environ["ADZUNA_APP_ID"]
        self.app_key = app_key or os.environ["ADZUNA_APP_KEY"]
        # Caller may inject a shared httpx.AsyncClient (recommended for
        # connection pooling across many users in one worker tick).
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "AdzunaScraper":
        if self._client is None:
            # follow_redirects=True is belt-and-braces in case Adzuna changes
            # the URL scheme again; httpx defaults to False, unlike requests.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self,
        query: str,
        location: str,
        *,
        limit: int = 20,
        max_days_old: int = 7,
    ) -> list[ScrapedJob]:
        if self._client is None:
            # Allow use without `async with` \u2014 build a one-shot client.
            async with self:
                return await self.search(query, location, limit=limit, max_days_old=max_days_old)

        results: list[ScrapedJob] = []
        page = 1
        while len(results) < limit:
            per_page = min(_MAX_PER_PAGE, limit - len(results))
            params = {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "results_per_page": per_page,
                "what": query,
                "where": location,
                "max_days_old": max_days_old,
                "content-type": "application/json",
            }
            resp = await self._client.get(_BASE_URL.format(page=page), params=params)
            resp.raise_for_status()
            data = resp.json()
            raw_jobs = data.get("results", [])
            if not raw_jobs:
                break
            for raw in raw_jobs:
                try:
                    results.append(_map(raw))
                except Exception as e:
                    # Bad row from upstream shouldn't kill the whole batch.
                    log.warning("adzuna: skipping malformed result id=%s: %s", raw.get("id"), e)
            if len(raw_jobs) < per_page:
                break  # last page
            page += 1
            if page > 10:  # hard ceiling \u2014 free tier doesn't allow more
                break
        return results[:limit]


def _map(raw: dict) -> ScrapedJob:
    company = (raw.get("company") or {}).get("display_name") or "Unknown"
    location = (raw.get("location") or {}).get("display_name") or ""
    posted_at: datetime | None = None
    created = raw.get("created")
    if created:
        try:
            posted_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
        except ValueError:
            posted_at = None
    sip_raw = raw.get("salary_is_predicted")
    salary_is_predicted = str(sip_raw) == "1"
    return ScrapedJob(
        external_id=str(raw["id"]),
        url=raw.get("redirect_url", ""),
        title=raw.get("title", "")[:512],
        company=company[:256],
        location=location[:256],
        description=raw.get("description", ""),
        posted_at=posted_at,
        salary_min=raw.get("salary_min"),
        salary_max=raw.get("salary_max"),
        salary_currency="INR",  # /in/ endpoint is always INR
        salary_is_predicted=salary_is_predicted,
    )
