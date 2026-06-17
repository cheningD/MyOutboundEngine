"""Load leads from a CSV into the store as prospects.

Handles the messiness of real exports: column names vary by source, so headers are matched
against a small alias table; emails are validated and normalized; duplicates are dropped both
within the file and against what's already stored; suppressed addresses are skipped; and
re-ingesting an existing prospect *merges* (filling blanks, preserving any prior scoring) rather
than overwriting. Apollo auto-pull, later, feeds the same code path.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Lead, Prospect, stable_prospect_id
from ..store import Store

# Canonical field -> header names we'll recognize for it (compared after normalization).
FIELD_ALIASES: dict[str, list[str]] = {
    "email": ["email", "email address", "e mail", "work email", "emailaddress", "mail"],
    "first_name": ["first name", "firstname", "first", "given name"],
    "last_name": ["last name", "lastname", "last", "surname", "family name"],
    "title": ["title", "job title", "position", "role"],
    "company": ["company", "company name", "organization", "organisation", "account", "employer"],
    "domain": ["domain", "company domain", "company website", "website", "url"],
    "industry": ["industry", "sector", "vertical"],
    "location": ["location", "city", "state", "country", "region", "geo"],
}

_LEAD_FIELDS = ["first_name", "last_name", "title", "company", "domain", "industry", "location"]


@dataclass
class LoadResult:
    total_rows: int = 0
    created: int = 0
    merged: int = 0
    skipped_invalid: int = 0
    skipped_suppressed: int = 0
    skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.total_rows} rows -> {self.created} new, {self.merged} merged | "
            f"skipped: {self.skipped_invalid} invalid, {self.skipped_suppressed} suppressed, "
            f"{self.skipped_duplicate} in-file duplicate"
        )


def _normalize_header(header: str) -> str:
    """Collapse to lowercase alphanumerics so 'E-mail Address' and 'Email Address' both match."""
    return re.sub(r"[^a-z0-9]+", "", header.lower())


def build_header_map(fieldnames: list[str]) -> dict[str, str]:
    """Map each canonical field to the actual CSV header that best matches it."""
    normalized = {_normalize_header(h): h for h in fieldnames if h}
    mapping: dict[str, str] = {}
    for canonical, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            key = _normalize_header(alias)
            if key in normalized:
                mapping[canonical] = normalized[key]
                break
    return mapping


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def row_to_lead(row: dict[str, str], header_map: dict[str, str], source: str) -> Lead:
    """Build a validated Lead from a CSV row. Raises if the email is missing or invalid."""

    def get(field_name: str) -> str | None:
        col = header_map.get(field_name)
        return _clean(row.get(col)) if col else None

    email = get("email")
    if not email:
        raise ValueError("missing email")

    mapped_cols = set(header_map.values())
    raw = {k: v.strip() for k, v in row.items() if k and k not in mapped_cols and _clean(v)}

    return Lead(
        email=email.lower(),
        first_name=get("first_name"),
        last_name=get("last_name"),
        title=get("title"),
        company=get("company"),
        domain=get("domain"),
        industry=get("industry"),
        location=get("location"),
        source=source,
        raw=raw,
    )


def merge_into_existing(existing: Prospect, new_lead: Lead) -> Prospect:
    """Fill blank lead fields from the new row and merge raw columns, keeping prior scoring."""
    current = existing.lead
    for field_name in _LEAD_FIELDS:
        if not getattr(current, field_name) and getattr(new_lead, field_name):
            setattr(current, field_name, getattr(new_lead, field_name))
    # Existing raw values win on key collisions; new keys are added.
    current.raw = {**new_lead.raw, **current.raw}
    return existing


def load_leads(store: Store, csv_path: str | Path, *, source: str = "csv", max_errors: int = 25) -> LoadResult:
    """Load a CSV into the store, returning a summary of what happened."""
    result = LoadResult()
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return result
        header_map = build_header_map(reader.fieldnames)
        if "email" not in header_map:
            raise ValueError(
                "CSV has no recognizable email column (looked for: "
                + ", ".join(FIELD_ALIASES["email"])
                + ")."
            )

        seen_in_file: set[str] = set()
        for line_no, row in enumerate(reader, start=2):  # line 1 is the header
            result.total_rows += 1
            try:
                lead = row_to_lead(row, header_map, source)
            except Exception as exc:
                result.skipped_invalid += 1
                if len(result.errors) < max_errors:
                    result.errors.append(f"line {line_no}: {exc}")
                continue

            email = lead.email
            if email in seen_in_file:
                result.skipped_duplicate += 1
                continue
            seen_in_file.add(email)

            if store.is_suppressed(email):
                result.skipped_suppressed += 1
                continue

            existing = store.get_prospect(stable_prospect_id(email))
            if existing is not None:
                store.upsert_prospect(merge_into_existing(existing, lead))
                result.merged += 1
            else:
                store.upsert_prospect(Prospect.from_lead(lead))
                result.created += 1

    return result
