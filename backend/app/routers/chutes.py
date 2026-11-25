from __future__ import annotations
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.db import db, rows_to_list, row_to_dict
from app.services.common import audit

router = APIRouter(tags=["口门"])


class ChuteIn(BaseModel):
    分拣线编号: int
    口门码: str = Field(min_length=1)
    标题: str = ""
    软容量: int = 120


@router.get("/口门")
def list_chutes():
    with db() as conn:
        return rows_to_list(conn.execute(
            "SELECT c.*, l.name AS 分拣线名 FROM chutes c JOIN lines l ON l.id=c.line_id ORDER BY c.code"
        ).fetchall())


@router.post("/口门")
def create_chute(body: ChuteIn):
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO chutes(line_id, code, title, status, soft_cap) VALUES (?, ?, ?, '口门开', ?)",
                (body.分拣线编号, body.口门码, body.标题, body.软容量),
            )
        except Exception as e:
            raise HTTPException(400, f"创建失败: {e}")
        row = conn.execute(
            "SELECT * FROM chutes WHERE line_id=? AND code=?",
            (body.分拣线编号, body.口门码),
        ).fetchone()
        audit(conn, "口门", row["id"], "新建", body.口门码)
        return row_to_dict(row)


@router.post("/口门/{chute_id}/开闭")
def set_chute(chute_id: int, 状态: str = Query(...)):
    if 状态 not in ("口门开", "口门闭"):
        raise HTTPException(400, "状态仅允许口门开或口门闭")
    with db() as conn:
        cur = conn.execute("UPDATE chutes SET status=? WHERE id=?", (状态, chute_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "口门不存在")
        audit(conn, "口门", chute_id, "开闭", 状态)
        return row_to_dict(conn.execute("SELECT * FROM chutes WHERE id=?", (chute_id,)).fetchone())

def _surface_router_chutes() -> dict:
    return {"panel": "chutes", "ready": True}

