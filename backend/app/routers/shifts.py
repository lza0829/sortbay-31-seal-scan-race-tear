from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db, rows_to_list, row_to_dict
from app.services.common import audit, audit_standalone, now_str

router = APIRouter(tags=["班次"])


class ShiftIn(BaseModel):
    营业日: str = Field(min_length=8)
    班次名: str = Field(min_length=1)


@router.get("/班次")
def list_shifts():
    with db() as conn:
        return rows_to_list(conn.execute("SELECT * FROM shifts ORDER BY id DESC").fetchall())


@router.get("/班次/对账")
def reconcile(shift_id: int | None = None):
    with db() as conn:
        if shift_id is None:
            shift = conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone()
        else:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")
        sid = shift["id"]
        scan_n = conn.execute("SELECT COUNT(*) c FROM scans WHERE shift_id=?", (sid,)).fetchone()["c"]
        routed = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('已路由','已入袋','已封袋')",
            (sid,),
        ).fetchone()["c"]
        bag_n = conn.execute("SELECT COUNT(*) c FROM bags WHERE shift_id=?", (sid,)).fetchone()["c"]
        sealed_bag = conn.execute(
            "SELECT COUNT(*) c FROM bags WHERE shift_id=? AND status='已封袋'", (sid,)
        ).fetchone()["c"]
        divert_n = conn.execute(
            "SELECT COUNT(*) c FROM divert_batches WHERE shift_id=?", (sid,)
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('待路由','错分回流')",
            (sid,),
        ).fetchone()["c"]
        return {
            "班次": row_to_dict(shift),
            "扫描总数": scan_n,
            "已路由及以上": routed,
            "袋数": bag_n,
            "已封袋数": sealed_bag,
            "回流批次数": divert_n,
            "未清件数": pending,
        }


@router.post("/班次")
def create_shift(body: ShiftIn):
    # 与封班/装载同一序列化闸：开班检查与落账原子，杜绝两个未封班班次并存
    with db(immediate=True) as conn:
        open_one = conn.execute("SELECT id FROM shifts WHERE sealed=0").fetchone()
        if open_one:
            raise HTTPException(400, "仍有未封班班次，先封班再开新班")
        try:
            conn.execute(
                "INSERT INTO shifts(business_day, name, sealed) VALUES (?, ?, 0)",
                (body.营业日, body.班次名),
            )
        except Exception as e:
            raise HTTPException(400, f"开班失败: {e}")
        row = conn.execute(
            "SELECT * FROM shifts WHERE business_day=? AND name=?",
            (body.营业日, body.班次名),
        ).fetchone()
        audit(conn, "班次", row["id"], "开班", body.班次名)
        return row_to_dict(row)


@router.post("/班次/{shift_id}/封班")
def seal_shift(shift_id: int):
    # 架构A（封班优先）：封班与过机装载在同一 BEGIN IMMEDIATE 闸上序列化。
    # 闸字段 sealed 的检查与落锁同事务：要么看到在途装载已落账的未清件而拒封，
    # 要么先落 sealed=1 使并发装载整笔拒绝回滚；不存在“已封仍增待路由”。
    try:
        with db(immediate=True) as conn:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
            if not shift:
                raise HTTPException(404, "班次不存在")
            if shift["sealed"]:
                raise HTTPException(400, "已封班")
            pending = conn.execute(
                "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('待路由','错分回流','已路由','已入袋')",
                (shift_id,),
            ).fetchone()["c"]
            if pending:
                raise HTTPException(409, f"仍有未封袋/未路由件 {pending}，禁止封班")
            cur = conn.execute(
                "UPDATE shifts SET sealed=1, sealed_at=? WHERE id=? AND sealed=0",
                (now_str(), shift_id),
            )
            if cur.rowcount != 1:
                raise HTTPException(409, "封班闸未落下：班次状态已变化，请重试")
            audit(conn, "班次", shift_id, "封班", "未清件数0")
            return row_to_dict(conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone())
    except HTTPException as e:
        if e.status_code == 409:
            # 争用败方：主事务已回滚，独立补记被拒事件，争用结局可从审计重放
            audit_standalone("班次", shift_id, "封班被拒", e.detail)
        raise

