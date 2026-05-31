"""Manual company-name -> domain overrides for Hunter lookups.

Hunter's `company=<name>` query relies on their internal name-to-domain
resolver, which is biased toward US/EU companies and frequently returns
nonsense for Indian / smaller firms. Observed bad resolutions:
    Uplers      -> uplers.nl       (real: uplers.com)
    Persistent  -> persistentit.com (real: persistent.com)
    Emerson     -> emersonre.com    (real: emerson.com)

When we know the right domain, we pass `domain=<domain>` to Hunter
directly, bypassing the resolver entirely.

Keys are matched **case-insensitively** against `Job.company`. Substring
matching is intentional so common suffixes don't break overrides:
    "Persistent Systems Limited" matches the "persistent" key.

Order of precedence:
    1. Exact case-insensitive match against full `Job.company`
    2. First entry whose key appears as a whole-word substring (longest first)
    3. Hunter's own name-to-domain resolver (fallback)

Adding more overrides:
    - For permanent additions, edit `_BUILTINS` below.
    - For per-deployment additions without a code change, set env var
      HUNTER_DOMAIN_OVERRIDES as a JSON object, e.g.:
          HUNTER_DOMAIN_OVERRIDES='{"Acme Corp": "acme.com"}'
      Env entries are merged with (and override) the builtins.
"""
from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache

log = logging.getLogger(__name__)

# Conservative starter set. Only entries I'm confident about — add more as
# you observe Hunter mis-resolving companies in the dry_run output.
_BUILTINS: dict[str, str] = {
    "Uplers": "uplers.com",
    "Persistent": "persistent.com",
    "Persistent Systems": "persistent.com",
    "Emerson": "emerson.com",
    # Hunter resolves to UAE site (publicissapient.ae); force global domain.
    "Publicis Sapient": "publicissapient.com",
    "Publicis.Sapient": "publicissapient.com",
    # Hunter can't resolve "Talentica Software India Private Limited" at all.
    "Talentica": "talentica.com",
    "Talentica Software": "talentica.com",
    # KPMG India member firm — Hunter resolves to a Dutch paint co (paint.nl).
    "BSR & Co": "bsraffiliates.com",
    "BSR and Co": "bsraffiliates.com",
    # AutoApply originals, kept for parity
    "HARMAN": "harman.com",
    "HARMAN India": "harman.com",
    "Thales": "thalesgroup.com",
    "Thales India": "thalesgroup.com",
}


@lru_cache(maxsize=1)
def _load_overrides() -> dict[str, str]:
    """Merge builtins with env-supplied additions. Cached for process lifetime."""
    merged = {k.lower(): v for k, v in _BUILTINS.items()}
    raw = os.environ.get("HUNTER_DOMAIN_OVERRIDES")
    if raw:
        try:
            extra = json.loads(raw)
            if not isinstance(extra, dict):
                raise ValueError("must be a JSON object")
            for k, v in extra.items():
                merged[str(k).lower()] = str(v)
            log.info("hunter overrides: loaded %d entries from HUNTER_DOMAIN_OVERRIDES",
                     len(extra))
        except (json.JSONDecodeError, ValueError) as e:
            log.error("hunter overrides: HUNTER_DOMAIN_OVERRIDES env is malformed (%s); ignoring", e)
    return merged


def resolve_domain(company: str) -> str | None:
    """Return a known-good domain for `company`, or None to defer to Hunter.

    Matching strategy:
      1. Exact case-insensitive match  (e.g. "Uplers" -> "uplers.com")
      2. Whole-word substring match, longest key first
         (so "Persistent Systems Limited" hits the "Persistent Systems" key
          before the shorter "Persistent" key)
    """
    if not company:
        return None
    overrides = _load_overrides()
    needle = company.lower().strip()

    # 1. Exact match
    if needle in overrides:
        return overrides[needle]

    # 2. Whole-word substring \u2014 walk keys longest-first so the most
    #    specific entry wins. Whole-word avoids matching "Uplers" inside
    #    a hypothetical "Multiplersolutions Ltd".
    for key in sorted(overrides.keys(), key=len, reverse=True):
        pattern = r"\b" + re.escape(key) + r"\b"
        if re.search(pattern, needle):
            return overrides[key]

    return None
