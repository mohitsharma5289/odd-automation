from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db.deps import get_db
from app.models.notification import Notification
from app.models.app_settings import AppSettings
from app.api.deps.auth import get_current_user, require_admin

notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])


# ── Notifications ─────────────────────────────────────────────────────────────

@notifications_router.get("/")
async def list_notifications(
    unread_only: bool = False,
    page: int = 1,
    page_size: int = 30,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    # Construct base executable query vectors
    query = select(Notification).order_by(Notification.created_at.desc())
    count_query = select(func.count(Notification.id))
    
    # Dynamically apply conditional filters safely
    if unread_only:
        query = query.where(Notification.is_read == False)
        count_query = count_query.where(Notification.is_read == False)

    query = query.offset((page - 1) * page_size).limit(page_size)

    items = (await db.execute(query)).scalars().all()
    total = (await db.execute(count_query)).scalar()

    return {
        "total": total,
        "items": [
            {
                "id": n.id,
                "type": n.type,
                "message": n.message,
                "is_read": n.is_read,
                "related_request_id": n.related_request_id,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in items
        ],
    }


@notifications_router.patch("/{notif_id}/read")
async def mark_read(
    notif_id: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    n = (await db.execute(
        select(Notification).where(Notification.id == notif_id)
    )).scalars().first()
    if not n:
        raise HTTPException(404, "Not found")
    n.is_read = True
    return {"id": notif_id, "is_read": True}


@notifications_router.post("/mark-all-read")
async def mark_all_read(db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    from sqlalchemy import update
    await db.execute(
        update(Notification).where(Notification.is_read == False).values(is_read=True)
    )
    return {"ok": True}


# ── App Settings ──────────────────────────────────────────────────────────────

@settings_router.get("/")
async def get_settings(db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    s = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalars().first()

    # Engine scope configuration is env-driven (config.py / .env) and
    # read-only from the UI — always reflect the CURRENT effective values.
    from app.core.config import settings as env
    from app.modules.ild.helpers import rbar_scope_rules

    scope_config = {
        "prr_scope_prefixes": env.PRR_SCOPE_PREFIXES,
        "prr_scope_suffixes": env.PRR_SCOPE_SUFFIXES,
        "rbar_scope_rules": rbar_scope_rules(),   # parsed per-category rules
        "prr_name_max_len": env.PRR_NAME_MAX_LEN,
    }

    if not s:
        return {"scope_config": scope_config}
    return {
        "download_base_name": s.download_base_name,
        "download_version": s.download_version,
        "recon_cron_time": s.recon_cron_time,
        "dump_ingest_times": s.dump_ingest_times,
        "scope_config": scope_config,
    }


@settings_router.patch("/")
async def update_settings(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    s = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalars().first()
    if not s:
        s = AppSettings(id=1)
        db.add(s)

    for key in ("download_base_name", "download_version", "recon_cron_time", "dump_ingest_times"):
        if key in payload:
            setattr(s, key, payload[key])

    return {"ok": True}
