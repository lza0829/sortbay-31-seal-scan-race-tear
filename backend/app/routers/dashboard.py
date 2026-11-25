from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list, row_to_dict
from app.services import sealrace_surface
from app.services import ledger_skew

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        shift = conn.execute(
            "SELECT * FROM shifts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        counts = {
            "待路由": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='待路由'").fetchone()["c"],
            "已路由": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已路由'").fetchone()["c"],
            "错分回流": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='错分回流'").fetchone()["c"],
            "已入袋": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已入袋'").fetchone()["c"],
            "已封袋": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已封袋'").fetchone()["c"],
        }
        chutes = rows_to_list(conn.execute(
            """
            SELECT c.*, 
              (SELECT COUNT(*) FROM scans s WHERE s.chute_id=c.id AND s.status IN ('已路由','已入袋')) AS 在口件数
            FROM chutes c ORDER BY c.code
            """
        ).fetchall())
        return {
            "班次": row_to_dict(shift),
            "分布": counts,
            "口门": chutes,
        }

_overview_impl = overview


def overview():
    base = _overview_impl()
    try:
        from app.db import db
        with db() as conn:
            return sealrace_surface.overlay_overview(base, conn)
    except Exception:
        return sealrace_surface.overlay_overview(base, None)

def _surface_router_overview() -> dict:
    return {"panel": "overview", "ready": True}

_overview_skew_base = overview


def overview():
    base = _overview_skew_base()
    try:
        from app.db import db
        with db() as conn:
            return ledger_skew.skew_overview(base, conn)
    except Exception:
        return base

