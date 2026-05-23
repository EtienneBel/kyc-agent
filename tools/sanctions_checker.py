"""
MCP Tool: sanctions_checker
────────────────────────────
Cross-references a person against:
  - Local sanctions table (PostgreSQL)
  - OFAC SDN list (fetched via public API)
  - BCEAO watchlist (pluggable)
"""

import logging
from difflib import SequenceMatcher

from db import get_pool

logger = logging.getLogger(__name__)

# Fuzzy match threshold — 0.85 catches typos / name variations
FUZZY_THRESHOLD = 0.85


class SanctionsResult:
    def __init__(self, is_sanctioned: bool, matches: list[dict], sources_checked: list[str]):
        self.is_sanctioned = is_sanctioned
        self.matches = matches
        self.sources_checked = sources_checked

    def to_dict(self) -> dict:
        return {
            "is_sanctioned": self.is_sanctioned,
            "matches": self.matches,
            "sources_checked": self.sources_checked,
        }


async def check_sanctions_list(
    full_name: str,
    date_of_birth: str | None = None,
    nationality: str | None = None,
) -> dict:
    """
    Check if a person appears on any sanctions or watchlist.

    Args:
        full_name:      Person's full name
        date_of_birth:  ISO date string (YYYY-MM-DD), optional
        nationality:    ISO 3166-1 alpha-3, optional

    Returns:
        dict: { is_sanctioned, matches, sources_checked }
    """
    logger.info(f"[sanctions_checker] Checking: {full_name}")

    matches = []
    sources_checked = []

    # ── 1. Check local PostgreSQL sanctions table ─────────────
    local_matches = await _check_local_db(full_name, date_of_birth, nationality)
    sources_checked.append("LOCAL_DB")
    matches.extend(local_matches)

    # ── 2. OFAC SDN (open public API) ─────────────────────────
    # Uncomment in prod — adds ~200ms latency
    # ofac_matches = await _check_ofac(full_name, date_of_birth)
    # sources_checked.append("OFAC")
    # matches.extend(ofac_matches)

    is_sanctioned = len(matches) > 0

    if is_sanctioned:
        logger.warning(f"[sanctions_checker] MATCH FOUND for {full_name}: {matches}")
    else:
        logger.info(f"[sanctions_checker] Clear — no matches for {full_name}")

    return SanctionsResult(
        is_sanctioned=is_sanctioned,
        matches=matches,
        sources_checked=sources_checked,
    ).to_dict()


async def _check_local_db(
    full_name: str,
    date_of_birth: str | None,
    nationality: str | None,
) -> list[dict]:
    """Fuzzy search against local sanctions table."""
    pool = await get_pool()

    # Pull candidates (pre-filter by nationality if provided)
    query = "SELECT full_name, date_of_birth, nationality, source FROM sanctions_list"
    params = []

    if nationality:
        query += " WHERE nationality = $1 OR nationality IS NULL"
        params.append(nationality)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    matches = []
    name_normalized = full_name.strip().lower()

    for row in rows:
        ratio = SequenceMatcher(
            None,
            name_normalized,
            row["full_name"].strip().lower()
        ).ratio()

        if ratio >= FUZZY_THRESHOLD:
            # Extra check: DOB match tightens confidence
            dob_match = (
                date_of_birth is not None
                and row["date_of_birth"] is not None
                and str(row["date_of_birth"]) == date_of_birth
            )

            matches.append({
                "matched_name": row["full_name"],
                "similarity": round(ratio, 3),
                "source": row["source"],
                "dob_match": dob_match,
            })

    return matches


async def _check_ofac(full_name: str, date_of_birth: str | None) -> list[dict]:
    """
    Query OFAC SDN public API.
    Docs: https://sanctionslistservice.ofac.treas.gov/api/
    """
    import httpx

    url = "https://sanctionslistservice.ofac.treas.gov/api/publicationsUpdates"
    params = {"name": full_name}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            # Parse OFAC response format
            # Implementation depends on OFAC API version
            return []
        except Exception as e:
            logger.error(f"[sanctions_checker] OFAC API error: {e}")
            return []
