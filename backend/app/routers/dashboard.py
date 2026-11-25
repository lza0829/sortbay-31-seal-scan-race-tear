from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list, row_to_dict

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    # 架构A 同命运口径：总览只读已提交落账，不接受任何表面态覆盖。
    # 封班灯、分布、在口件数全部来自同一库内查询，与扫描册/对账/审计同命运。
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
