"""Lightweight role-keyword expansion for the Adzuna scraper.

The user enters a single role like ``"python developer"`` but recruiters
post the same job under many synonymous titles (``"python engineer"``,
``"backend python developer"``). Expanding before we hit Adzuna gives us
materially more unique listings without asking the user to type variants.

Kept intentionally tiny + hardcoded — no NLP, no LLM call. Each entry
caps at 2 extra synonyms so we don't blow the per-user daily scan slot
budget. The original query is always first and never dropped.
"""
from __future__ import annotations

# Lowercase substring -> list of extra queries to also run.
# Keep additions narrow & high-signal: a true synonym, not a sibling role.
_SYNONYMS: dict[str, list[str]] = {
    "python developer":     ["python engineer", "backend python developer"],
    "python engineer":      ["python developer", "backend python developer"],
    "java developer":       ["java engineer", "backend java developer"],
    "full stack developer": ["full stack engineer", "software developer"],
    "full stack engineer":  ["full stack developer", "software engineer"],
    "frontend developer":   ["front end developer", "react developer"],
    "front end developer":  ["frontend developer", "react developer"],
    "backend developer":    ["back end developer", "backend engineer"],
    "back end developer":   ["backend developer", "backend engineer"],
    "data engineer":        ["data platform engineer", "etl developer"],
    "data scientist":       ["machine learning engineer", "ml engineer"],
    "ml engineer":          ["machine learning engineer", "data scientist"],
    "machine learning engineer": ["ml engineer", "data scientist"],
    "devops engineer":      ["site reliability engineer", "sre"],
    "sre":                  ["site reliability engineer", "devops engineer"],
    "android developer":    ["android engineer", "mobile developer"],
    "ios developer":        ["ios engineer", "mobile developer"],
    "qa engineer":          ["sdet", "test engineer"],
    "software engineer":    ["software developer"],
    "software developer":   ["software engineer"],
}

_MAX_EXTRA = 2


def expand_role(role: str) -> list[str]:
    """Return ``[role, ...up to 2 synonyms]`` with case-insensitive
    matching and de-duplication. Unknown roles return ``[role]`` only.
    """
    if not role or not role.strip():
        return []
    base = role.strip()
    key = base.lower()
    extras = _SYNONYMS.get(key, [])
    seen = {key}
    out: list[str] = [base]
    for syn in extras[:_MAX_EXTRA]:
        if syn.lower() in seen:
            continue
        seen.add(syn.lower())
        out.append(syn)
    return out
