import logging
import shutil
from pathlib import Path
import asyncio

from app.workers.celery_app import celery_app
from app.db.session import SyncSessionLocal
from app.core.config import settings

from sqlalchemy import select, and_
from sqlalchemy.exc import DBAPIError, OperationalError, TimeoutError, IntegrityError

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.tasks.task_process_request", 
    bind=True, 
    max_retries=3,
    queue="processing"
)
def task_process_request(self, request_id: str, csv_path: str, selected_instances: list):
    """Run the async ILD processor inside a sync Celery task environment safely."""
    from app.modules.ild.processor import process_ild_request

    logger.info("Asynchronous task worker invocation started for request processing: %s", request_id)

    async def _run():
        from sqlalchemy import update
        from sqlalchemy.pool import NullPool
        from sqlalchemy.ext.asyncio import (
            create_async_engine, async_sessionmaker, AsyncSession,
        )
        from app.models.change_request import ChangeRequest
        from app.models.enums import RequestStatus

        # IMPORTANT: build a fresh engine per task run.
        # asyncio.run() creates a NEW event loop for every task invocation;
        # a module-level engine would pool asyncpg connections bound to the
        # PREVIOUS (already-closed) loop, causing "Event loop is closed" /
        # "Future attached to a different loop" on the next run.
        # NullPool ensures no connection outlives this loop.
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

        try:
            return await _run_with_session(session_factory, update, ChangeRequest, RequestStatus)
        finally:
            await engine.dispose()

    async def _run_with_session(session_factory, update, ChangeRequest, RequestStatus):
        async with session_factory() as db:
            req = (await db.execute(
                select(ChangeRequest).where(ChangeRequest.id == request_id)
            )).scalars().first()

            if not req:
                # Minor Suggestion 3 Fix: Raise explicit validation error so Celery marks this as FAILED
                raise ValueError(f"Task Process Request aborted: ChangeRequest ID {request_id} not found in database.")

            try:
                result = await process_ild_request(db, req, csv_path, selected_instances)
                await db.commit()
                return result
                
            except (DBAPIError, OperationalError, TimeoutError) as infra_ex:
                await db.rollback()
                # Minor Suggestion 4 Fix: Swapped to true exponential backoff scaling (30s, 60s, 120s)
                backoff_countdown = 30 * (2 ** self.request.retries)
                logger.warning(
                    "Database infrastructure error on request %s. Scheduling exponential backoff retry in %ds. Error: %s",
                    request_id, backoff_countdown, infra_ex
                )
                raise self.retry(exc=infra_ex, countdown=backoff_countdown)
                
            except Exception:
                await db.rollback()
                logger.exception("Fatal processing exception encountered on request %s (Bypassing retry pipelines)", request_id)
                
                await db.execute(
                    update(ChangeRequest)
                    .where(ChangeRequest.id == request_id)
                    .values(status=RequestStatus.FAILED.value)
                )
                await db.commit()
                raise 

    return asyncio.run(_run())


@celery_app.task(
    name="app.workers.tasks.task_ingest_dump",
    queue="processing"
)
def task_ingest_dump(
    file_path: str,
    dra_type: str,
    instance_label: str,
    original_filename: str,
):
    """Parse a router dump sheet file and store payload rows safely with race-condition proof guards."""
    from app.modules.ild.parser import _detect_type, parse_prr_dump, parse_rbar_dump, _extract_timestamp
    from app.models.dump_snapshot import DumpSnapshot
    from app.models.prr_dump_row import PrrDumpRow
    from app.models.rbar_dump_row import RbarDumpRow
    from app.models.dra_instance import DRAInstance

    logger.info("Dump ingestion task tracking pipeline initialized for file: %s", original_filename)

    dump_type = _detect_type(original_filename)
    source_ts = _extract_timestamp(original_filename)

    rows_parsed_count = 0
    rows_inserted_count = 0

    with SyncSessionLocal() as db:
        # The RBAR scope rule depends on the DRA instance category
        # (Core/IoT → orcl…vdea, Policy → jio…pcrf; see RBAR_SCOPE_RULES).
        instance = db.execute(
            select(DRAInstance).where(
                and_(
                    DRAInstance.dra_type == dra_type,
                    DRAInstance.instance_label == instance_label,
                )
            )
        ).scalars().first()
        instance_category = instance.category if instance else None
        if not instance:
            logger.warning(
                "Dump ingest: no DRA instance found for %s/%s — RBAR scope "
                "falls back to the 'default' rule.", dra_type, instance_label,
            )
        duplicate_check = db.execute(
            select(DumpSnapshot).where(
                and_(
                    DumpSnapshot.file_name == original_filename,
                    DumpSnapshot.dra_type == dra_type,
                    DumpSnapshot.instance_label == instance_label,
                    DumpSnapshot.source_timestamp == source_ts
                )
            )
        ).scalars().first()

        if duplicate_check:
            logger.info("Deduplication Guard: Snapshot record entry already registered for file %s. Skipping ingestion.", original_filename)
            return {"snapshot_id": duplicate_check.id, "rows": 0, "status": "SKIPPED_DUPLICATE"}

        try:
            snapshot = DumpSnapshot(
                dra_type=dra_type,
                instance_label=instance_label,
                object_type=dump_type,
                file_name=original_filename,
                source_timestamp=source_ts,
            )
            db.add(snapshot)
            db.flush()
            snapshot_id = snapshot.id
            
            logger.info("Dump snapshot record successfully initialized and staged. Assigned Snapshot ID: %s", snapshot_id)
            
        except IntegrityError:
            db.rollback()
            
            existing_id = db.execute(
                select(DumpSnapshot.id).where(
                    and_(
                        DumpSnapshot.file_name == original_filename,
                        DumpSnapshot.dra_type == dra_type,
                        DumpSnapshot.instance_label == instance_label,
                        DumpSnapshot.source_timestamp == source_ts
                    )
                )
            ).scalars().first()
            
            # Minor Suggestion 2 Fix: Enhanced support logging by including the conflicting execution ID
            logger.warning(
                "Concurrency Guard: Structural integrity clash intercepted for file %s. "
                "Aborting duplicate thread task. Matching active tracking Snapshot ID: %s", 
                original_filename, existing_id
            )
            return {"snapshot_id": existing_id, "rows": 0, "status": "SKIPPED_DUPLICATE"}

        try:
            if dump_type == "PRR":
                rows = parse_prr_dump(file_path)
                rows_parsed_count = len(rows)
                for r in rows:
                    db.add(PrrDumpRow(
                        snapshot_id=snapshot_id,
                        name=r.get("name"),
                        realm=r.get("realm"),
                        route_list_name=r.get("route_list_name"),
                        peer_route_table=r.get("peer_route_table"),
                        raw_payload=r.get("raw_payload"),
                    ))
                    rows_inserted_count += 1
            else:
                rows = parse_rbar_dump(file_path, instance_category)
                rows_parsed_count = len(rows)
                for r in rows:
                    try:
                        if r.get("start_addr") is None or r.get("end_addr") is None:
                            raise ValueError("Snapshot record payload is missing boundary address coordinates values.")

                        raw_pl = r.get("raw_payload") or {}
                        # CRITICAL: int(float()) tolerates Excel scientific notation
                        # (e.g. 4.0584E+14); values are stored as int / BigInteger only.
                        start_val = int(float(r["start_addr"]))
                        end_val = int(float(r["end_addr"]))
                        raw_old_start = raw_pl.get("oldStartAddr")
                        old_start = (
                            int(float(raw_old_start))
                            if raw_old_start not in (None, "")
                            else None
                        )
                    except (ValueError, TypeError, OverflowError) as parse_ex:
                        logger.warning(
                            "Data Corruption Skipped: Dropping malformed RBAR range entry row inside file %s. Reason: %s",
                            original_filename, parse_ex
                        )
                        continue

                    db.add(RbarDumpRow(
                        snapshot_id=snapshot_id,
                        table_name=r.get("table_name") or raw_pl.get("tableName"),
                        start_addr=start_val,
                        end_addr=end_val,
                        destination=r.get("destination") or raw_pl.get("destination"),
                        pfx_length=raw_pl.get("pfxLength"),
                        old_table_name=raw_pl.get("oldTableName"),
                        old_start_addr=old_start,
                        old_pfx_length=raw_pl.get("oldPfxLength"),
                        raw_payload=raw_pl,
                    ))
                    rows_inserted_count += 1

            db.commit()
            
            # Critical Issue 11 Fix: Accurate metrics logging pattern combining targets and raw parse yields
            logger.info(
                "Ingestion successfully committed. Type: %s | Parsed from File: %d | Successfully Inserted: %d | "
                "Snapshot ID Target: %s for %s/%s",
                dump_type, rows_parsed_count, rows_inserted_count, snapshot_id, dra_type, instance_label
            )

        except Exception:
            db.rollback()
            # Critical Issue 10 Fix: Replaced standard error text with proper exception stack dump mappings
            logger.exception("Ingestion failed due to unhandled database exception trace. Invalidating staged payload.")
            raise

    try:
        source_file = Path(file_path)
        if source_file.exists():
            dest = Path(settings.DUMP_PROCESSED_PATH) / original_filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_file), str(dest))
    except Exception as file_ex:
        logger.exception(
            "IO Warning: Successfully committed snapshot rows to database, but failed to migrate source dump file %s "
            "to archival storage paths. Error context: %s", original_filename, file_ex
        )

    return {"snapshot_id": snapshot_id, "rows": rows_inserted_count, "status": "SUCCESS"}


@celery_app.task(
    name="app.workers.tasks.task_run_reconciliation",
    queue="scheduler"
)
def task_run_reconciliation(dra_type: str | None = None, instance_label: str | None = None):
    """Automated Scheduled Reconciliation Execution Vector Loop."""
    from app.modules.ild.reconciler import run_reconciliation

    logger.info("Automated batch reconciliation cron engine triggered.")
    with SyncSessionLocal() as db:
        result = run_reconciliation(db, dra_type, instance_label)

    logger.info("Scheduled batch reconciliation completed successfully. Results: %s", result)
    return result


@celery_app.task(
    name="app.workers.tasks.task_scan_incoming_dumps",
    queue="processing"
)
def task_scan_incoming_dumps():
    """
    Scheduled scan of the configured dump source (folder on the VM today,
    swappable via DUMP_SOURCE_TYPE). Every discovered file is dispatched to
    task_ingest_dump; the snapshot dedup guard makes re-scans idempotent.
    """
    from app.modules.ild.dump_source import get_dump_source

    source = get_dump_source()
    dispatched = 0
    for df in source.iter_files():
        logger.info(
            "Dump scan: dispatching %s for %s/%s",
            df.filename, df.dra_type, df.instance_label,
        )
        task_ingest_dump.delay(df.path, df.dra_type, df.instance_label, df.filename)
        dispatched += 1

    logger.info("Dump source scan complete — %d file(s) dispatched for ingestion.", dispatched)
    return {"dispatched": dispatched}