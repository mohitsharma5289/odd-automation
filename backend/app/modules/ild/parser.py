"""
ILD dump file parser.

PRR dump filename pattern: …_Diameter_PeerRouteRule.csv
RBAR dump filename pattern: …_Rbar_AddressRange.csv

Both use TAB separators.  All lines beginning with # are comments;
the header row also begins with # – strip the # before parsing.
"""
import csv
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.core.config import settings

logger = logging.getLogger(__name__)


def _detect_type(filename: str) -> str:
    """Return 'PRR' or 'RBAR' based on filename."""
    fn = filename.upper()
    if "PEERROUTERULE" in fn or "DIAMETER" in fn:
        return "PRR"
    if "ADDRESSRANGE" in fn or "RBAR" in fn:
        return "RBAR"
    raise ValueError(f"Cannot determine dump type from filename: {filename}")


def _extract_timestamp(filename: str) -> datetime | None:
    """
    Extract source timestamp from dump filename.
    Pattern: …_120425-1351-IST_…  → 2025-04-12 13:51
    """
    m = re.search(r"(\d{6})-(\d{4})-IST", filename)
    if m:
        try:
            date_part = m.group(1)   # DDMMYY
            time_part = m.group(2)   # HHMM
            day, mon, yr = int(date_part[:2]), int(date_part[2:4]), int(date_part[4:]) + 2000
            hh, mm = int(time_part[:2]), int(time_part[2:])
            from zoneinfo import ZoneInfo
            return datetime(yr, mon, day, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata"))
        except Exception:
            pass
    return None


def _sniff_delimiter(header_line: str) -> str:
    """
    DSR exports are TAB-separated per spec, but files that transited
    through Excel or copy-paste sometimes arrive comma- or
    semicolon-separated. Pick whichever splits the header into the
    most columns (TAB wins ties).
    """
    candidates = ["\t", ",", ";"]
    best = max(candidates, key=lambda d: len(header_line.split(d)))
    if len(header_line.split(best)) <= 1:
        return "\t"
    return best


def _iter_dump_rows(file_path: str) -> tuple[list[str], Iterator[dict]]:
    """
    Yield (headers, row_dicts) from a DRA dump CSV/TSV file.

    Robustness (fixes "0 rows in scope" on real DSR exports):
    * utf-8-sig encoding: a UTF-8 BOM would otherwise prefix the first
      line with \\ufeff so '#...' no longer starts with '#' and the
      header/comment logic silently skips the whole file.
    * Delimiter sniffing: TAB per spec, falls back to ','/';' for files
      that were re-saved through Excel.
    * Every line is also BOM/whitespace-tolerant before the '#' check.
    """
    header: list[str] | None = None
    delimiter = "\t"
    data_rows: list[dict] = []

    # utf-8-sig strips a leading BOM if present; harmless otherwise
    with open(file_path, encoding="utf-8-sig", errors="replace") as fh:
        lines = fh.readlines()

    for line in lines:
        stripped = line.rstrip("\n\r").lstrip('\ufeff')
        if not stripped.strip():
            continue

        if stripped.lstrip().startswith("#"):
            # Potential header line: starts with "#Application Name" (after the #)
            candidate = stripped.lstrip().lstrip("#").strip()
            if candidate.lower().startswith("application name"):
                delimiter = _sniff_delimiter(candidate)
                header = [h.strip() for h in candidate.split(delimiter)]
                logger.info(
                    "Dump header detected: %d columns, delimiter=%r",
                    len(header), delimiter,
                )
            # else: pure comment – skip
            continue

        if header is None:
            continue  # data before header – skip

        parts = [p.strip() for p in stripped.split(delimiter)]
        # Pad / truncate to match header length
        while len(parts) < len(header):
            parts.append("")
        row = dict(zip(header, parts))
        data_rows.append(row)

    if header is None:
        preview = lines[0][:120].rstrip() if lines else "<empty file>"
        logger.warning(
            "No '#Application Name' header found in dump file %s — 0 rows will "
            "be parsed. First line preview: %r (check for missing header line, "
            "wrong file, or unexpected encoding)",
            file_path, preview,
        )
    elif not data_rows:
        logger.warning(
            "Dump header found but no data rows in %s — file may contain only "
            "comments or use an unexpected line format.",
            file_path,
        )

    return header or [], data_rows


# Scope filters are shared, config-driven helpers (PRR = contains, RBAR = endswith)
from app.modules.ild.helpers import in_prr_scope as _in_prr_scope
from app.modules.ild.helpers import in_rbar_scope as _in_rbar_scope


# ── Public parse functions ─────────────────────────────────────────────────────

def parse_prr_dump(file_path: str) -> list[dict]:
    """
    Parse a PRR (PeerRouteRule) dump file.
    Returns list of dicts with keys: name, realm (value_1), route_list_name, peer_route_table, raw_payload.
    Only in-scope rows (matching PRR_SCOPE_SUFFIXES) are returned.
    """
    _, rows = _iter_dump_rows(file_path)
    results = []
    skipped = 0
    for row in rows:
        name = row.get("name", "").strip()
        if not name:
            continue
        if not _in_prr_scope(name):
            skipped += 1
            continue
        results.append({
            "name": name,
            "realm": row.get("value_1", "").strip() or None,
            "route_list_name": row.get("routeListName", "").strip() or None,
            "peer_route_table": row.get("peerRouteTable", "").strip() or None,
            "raw_payload": row,
        })

    logger.info("PRR dump: %d in-scope rows parsed, %d skipped (out-of-scope)", len(results), skipped)
    return results


def parse_rbar_dump(file_path: str, category: str | None = None) -> list[dict]:
    """
    Parse an RBAR (AddressRange) dump file.
    Returns list of dicts with keys: table_name, start_addr, end_addr, destination, raw_payload.
    startAddr / endAddr are stored as Python int (handles scientific notation from Excel).
    Only in-scope rows are returned — the prefix+suffix rule depends on the
    owning DRA instance's ``category`` (RBAR_SCOPE_RULES; e.g. Core/IoT →
    orcl…vdea, Policy → jio…pcrf).
    """
    _, rows = _iter_dump_rows(file_path)
    results = []
    skipped = 0
    for row in rows:
        destination = row.get("destination", "").strip()
        if not _in_rbar_scope(destination, category):
            skipped += 1
            continue

        raw_start = row.get("startAddr", "").strip()
        raw_end = row.get("endAddr", "").strip()

        if not raw_start or not raw_end:
            logger.warning("RBAR row skipped (missing startAddr/endAddr): %s", row)
            continue

        try:
            # CRITICAL: use int(float()) to handle scientific notation (e.g. 4.0584E+14)
            start_addr = int(float(raw_start))
            end_addr = int(float(raw_end))
        except (ValueError, OverflowError) as e:
            logger.warning("RBAR parse error for row %s: %s", row, e)
            continue

        results.append({
            "table_name": row.get("tableName", "").strip() or None,
            "start_addr": start_addr,
            "end_addr": end_addr,
            "destination": destination or None,
            "raw_payload": row,
        })

    logger.info("RBAR dump: %d in-scope rows parsed, %d skipped (out-of-scope)", len(results), skipped)
    return results


async def parse_dump(file_path: str, category: str | None = None) -> dict:
    """Generic entry point (category drives the RBAR scope rule)."""
    dump_type = _detect_type(Path(file_path).name)
    if dump_type == "PRR":
        rows = parse_prr_dump(file_path)
    else:
        rows = parse_rbar_dump(file_path, category)
    return {"type": dump_type, "rows": rows}
