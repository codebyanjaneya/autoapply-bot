"""Groq-backed job scorer.

Takes a Job + the user's preferences, returns (score, reason).
- score: 0-100, higher = better fit
- reason: short string shown to the user in the bot

The model evaluates: title/description vs role_keywords + skills, location
match vs preferences.locations, recency. We instruct it to return strict JSON
so parsing is deterministic.
"""
from __future__ import annotations

import json
import logging
import os
import re

from groq import AsyncGroq
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from core.models import Job, UserPreferences

log = logging.getLogger(__name__)

_MODEL = "llama-3.3-70b-versatile"
_SYSTEM = (
    "You score job postings for fit against a candidate's preferences. "
    "Return STRICT JSON only: {\"score\": <int 0-100>, \"reason\": <short string, max 120 chars>}. "
    "No markdown, no commentary. Higher score = better fit."
)


class GroqScorer:
    def __init__(self, api_key: str | None = None, client: AsyncGroq | None = None):
        self._client = client or AsyncGroq(api_key=api_key or os.environ["GROQ_API_KEY"])

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=8),
        reraise=True,
    )
    async def score(self, job: Job, prefs: UserPreferences) -> tuple[float, str]:
        prompt = _build_prompt(job, prefs)
        resp = await self._client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        return _parse(raw)


def _build_prompt(job: Job, prefs: UserPreferences) -> str:
    return (
        f"Candidate preferences:\n"
        f"  Roles: {', '.join(prefs.role_keywords) or '(none)'}\n"
        f"  Locations: {', '.join(prefs.locations) or '(any)'}\n"
        f"  Skills: {', '.join(prefs.skills) or '(none)'}\n"
        f"\n"
        f"Job posting:\n"
        f"  Title: {job.title}\n"
        f"  Company: {job.company}\n"
        f"  Location: {job.location}\n"
        f"  Description: {job.description[:2000]}\n"
        f"\n"
        f"Score this job for fit. Penalize location mismatch heavily unless "
        f"the description says 'remote'. Penalize seniority mismatch (e.g. "
        f"a candidate seeking 'developer' should score 'staff engineer' low)."
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(raw: str) -> tuple[float, str]:
    """Tolerant JSON extraction \u2014 the model occasionally wraps in markdown
    despite the system prompt. Falls back to regex extraction."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_RE.search(raw)
        if not match:
            log.warning("groq: unparseable scorer output: %r", raw[:200])
            return (0.0, "scorer-parse-error")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return (0.0, "scorer-parse-error")

    score = float(data.get("score", 0))
    score = max(0.0, min(100.0, score))
    reason = str(data.get("reason", ""))[:120]
    return (score, reason)
