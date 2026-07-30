import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.models.prr_dump_row import PrrDumpRow
from app.models.rbar_dump_row import RbarDumpRow
from app.models.entry_instance_status import EntryInstanceStatus
from app.models.entry_instance_detail import EntryInstanceDetail
from app.models.unknown_entry import UnknownEntry
from app.models.enums import DecisionType, ImplStatus
from app.core.config import settings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

ADD_DECISIONS = {DecisionType.ADD.value, DecisionType.DEPENDENCY_ADD.value}
DEL_DECISIONS = {DecisionType.DELETE.value, DecisionType.DEPENDENCY_DELETE.value}
SUPERSEDE_DECISIONS = {DecisionType.SUPERSEDE.value, DecisionType.SUPERSEDE_PENDING.value}

# Shared config-driven scope filters (PRR = contains, RBAR = endswith),
# evaluated at call time so env changes don't require module reloads.
from app.modules.ild.helpers import in_prr_scope as _in_prr_scope
from app.modules.ild.helpers import in_rbar_scope as _in_rbar_scope


def _normalize_string(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _load_effective_prr(db: Session, dra_type: str, instance_label: str) -> set[tuple[str, str]]:
    effective_state: dict[str, str] = {}
    statuses = db.execute(
        select(EntryInstanceStatus, EntryInstanceDetail)
        .join(EntryInstanceDetail, EntryInstanceDetail.instance_status_id == EntryInstanceStatus.id)
        .where(
            and_(
                EntryInstanceStatus.dra_type == dra_type,
                EntryInstanceStatus.instance_label == instance_label,
                EntryInstanceStatus.entry_type == "PRR",
                EntryInstanceStatus.impl_status == ImplStatus.IMPLEMENTED.value
            )
        ).order_by(EntryInstanceStatus.created_at.asc(), EntryInstanceStatus.id.asc())
    ).all()

    for s, d in statuses:
        realm = _normalize_string(d.realm)
        rule = _normalize_string(d.final_prt_rule)
        if not realm:
            continue
        if s.decision in ADD_DECISIONS or s.decision in SUPERSEDE_DECISIONS:
            effective_state[realm] = rule
        elif s.decision in DEL_DECISIONS:
            effective_state.pop(realm, None)

    return {(realm, rule) for realm, rule in effective_state.items()}


def _load_effective_rbar(db: Session, dra_type: str, instance_label: str) -> set[tuple[int, int, str]]:
    effective_state: dict[tuple[int, int], str] = {}
    statuses = db.execute(
        select(EntryInstanceStatus, EntryInstanceDetail)
        .join(EntryInstanceDetail, EntryInstanceDetail.instance_status_id == EntryInstanceStatus.id)
        .where(
            and_(
                EntryInstanceStatus.dra_type == dra_type,
                EntryInstanceStatus.instance_label == instance_label,
                EntryInstanceStatus.entry_type == "RBAR",
                EntryInstanceStatus.impl_status == ImplStatus.IMPLEMENTED.value
            )
        ).order_by(EntryInstanceStatus.created_at.asc(), EntryInstanceStatus.id.asc())
    ).all()

    for s, d in statuses:
        if d.start_addr is None or d.end_addr is None:
            continue
        new_start = int(d.start_addr)
        new_end = int(d.end_addr)
        new_rk = (new_start, new_end)
        dest = _normalize_string(d.destination)

        if s.decision in ADD_DECISIONS or s.decision in SUPERSEDE_DECISIONS:
            overridden_keys = [old_rk for old_rk in effective_state.keys() if (old_rk[0] >= new_start and old_rk[1] <= new_end)]
            for old_rk in overridden_keys:
                effective_state.pop(old_rk, None)
            effective_state[new_rk] = dest
        elif s.decision in DEL_DECISIONS:
            effective_state.pop(new_rk, None)

    return {(rk[0], rk[1], dest) for rk, dest in effective_state.items()}


# ── Core Stateful Detectors ───────────────────────────────────────────

def sync_unknown_prr(db: Session, dra_type: str, instance_label: str, snapshot) -> int:
    """
    Scans PRR dump rows, synchronizing against the permanent inventory state model.
    Returns the total number of newly registered rogue entries.
    """
    now = datetime.now(IST)
    new_detections = 0
    
    dump_rows = db.execute(select(PrrDumpRow).where(PrrDumpRow.snapshot_id == snapshot.id)).scalars().all()
    effective_prr = _load_effective_prr(db, dra_type, instance_label)

    # FIXED: Querying without snapshot_id or realm schema mismatch attributes
    existing_records = db.execute(
        select(UnknownEntry).where(
            and_(
                UnknownEntry.dra_type == dra_type,
                UnknownEntry.instance_label == instance_label,
                UnknownEntry.entry_type == "PRR"
            )
        )
    ).scalars().all()
    inventory_map = {r.identifier: r for r in existing_records}

    for row in dump_rows:
        name = (row.name or "").strip()
        if not name or not _in_prr_scope(name):
            continue
            
        realm = _normalize_string(row.realm)
        rule_name = _normalize_string(name)
        
        config_key = (realm, rule_name)
        lookup_identifier = f"{realm}:{rule_name}"

        if config_key not in effective_prr:
            if lookup_identifier in inventory_map:
                # Rogue entry already tracked -> simply bump lifecycle timestamp
                inventory_map[lookup_identifier].last_seen_at = now
            else:
                # Brand new rogue configuration discovered
                logger.warning("Unknown PRR entry discovered: identifier=%s", lookup_identifier)
                new_entry = UnknownEntry(
                    dra_type=dra_type,
                    instance_label=instance_label,
                    entry_type="PRR",
                    identifier=lookup_identifier,
                    raw_payload=row.raw_payload,
                    first_seen_at=now,
                    last_seen_at=now
                )
                db.add(new_entry)
                new_detections += 1
                
    return new_detections


def sync_unknown_rbar(db: Session, dra_type: str, instance_label: str, snapshot) -> int:
    """
    Scans RBAR dump rows, synchronizing against the permanent inventory state model.
    Returns the total number of newly registered rogue entries.
    """
    now = datetime.now(IST)
    new_detections = 0
    
    dump_rows = db.execute(select(RbarDumpRow).where(RbarDumpRow.snapshot_id == snapshot.id)).scalars().all()
    effective_rbar = _load_effective_rbar(db, dra_type, instance_label)

    # RBAR scope depends on the instance category (RBAR_SCOPE_RULES)
    from app.models.dra_instance import DRAInstance
    inst = db.execute(
        select(DRAInstance).where(
            and_(DRAInstance.dra_type == dra_type, DRAInstance.instance_label == instance_label)
        )
    ).scalars().first()
    category = inst.category if inst else None

    # FIXED: Querying using structural inventory boundaries
    existing_records = db.execute(
        select(UnknownEntry).where(
            and_(
                UnknownEntry.dra_type == dra_type,
                UnknownEntry.instance_label == instance_label,
                UnknownEntry.entry_type == "RBAR"
            )
        )
    ).scalars().all()
    inventory_map = {r.identifier: r for r in existing_records}

    for row in dump_rows:
        if row.start_addr is None or row.end_addr is None:
            continue
            
        dest = (row.destination or "").strip()
        if not _in_rbar_scope(dest, category):
            continue
            
        try:
            start_i = int(row.start_addr)
            end_i = int(row.end_addr)
        except (TypeError, ValueError) as ex:
            logger.warning("Malformed RBAR row parsed in snapshot %s: %s", snapshot.id, ex)
            continue
            
        dest_norm = _normalize_string(dest)
        config_key = (start_i, end_i, dest_norm)
        lookup_identifier = f"{start_i}-{end_i}:{dest_norm}"

        if config_key not in effective_rbar:
            if lookup_identifier in inventory_map:
                inventory_map[lookup_identifier].last_seen_at = now
            else:
                logger.warning("Unknown RBAR entry discovered: identifier=%s", lookup_identifier)
                new_entry = UnknownEntry(
                    dra_type=dra_type,
                    instance_label=instance_label,
                    entry_type="RBAR",
                    identifier=lookup_identifier,
                    raw_payload=row.raw_payload,
                    first_seen_at=now,
                    last_seen_at=now
                )
                db.add(new_entry)
                new_detections += 1
                
    return new_detections