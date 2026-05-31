"""Abstract job-board scraper interface.

Every concrete scraper (Adzuna, JSearch, ...) implements this. The pipeline
calls `scraper.search()` and gets back a list of `ScrapedJob` dataclasses,
which the repository layer then upserts into the `jobs` table.

Why dataclasses instead of returning Job ORM objects directly?
- Decouples HTTP layer from DB layer: scrapers can be tested without a DB.
- Lets the pipeline batch upserts in one transaction.
- ScrapedJob has no `user_id` \u2014 the repository attaches that at insert time.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from core.models import JobSource


@dataclass(slots=True)
class ScrapedJob:
    external_id: str
    url: str
    title: str
    company: str
    location: str
    description: str
    posted_at: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_is_predicted: bool = False


class BaseScraper(ABC):
    source: JobSource

    @abstractmethod
    async def search(
        self,
        query: str,
        location: str,
        *,
        limit: int = 20,
        max_days_old: int = 7,
    ) -> list[ScrapedJob]:
        """Search the job board. Implementations MUST:
        - Return at most `limit` results (paginate internally if needed).
        - Filter to jobs posted in the last `max_days_old` days.
        - Never raise on empty results \u2014 return `[]`.
        - Raise `httpx.HTTPStatusError` on auth failures so the worker can
          mark the user's run as errored without retrying.
        """
        ...
