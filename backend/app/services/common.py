from __future__ import annotations
from datetime import datetime
from fastapi import HTTPException
from app.db import db, row_to_dict


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_shift(conn):
    row = conn.execute(
        "SELECT * FROM shifts WHERE sealed=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise HTTPException(400, "无未封班班次")
    return row


def audit(conn, kind: str, ref_id: int | None, action: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_events(kind, ref_id, action, created_at, note) VALUES (?, ?, ?, ?, ?)",
        (kind, ref_id, action, now_str(), note),
    )


def require_unsealed(conn) -> None:
    s = current_shift(conn)
    if s["sealed"]:
        raise HTTPException(400, "班次已封班，禁止写口")


def audit_standalone(kind: str, ref_id: int | None, action: str, note: str = "") -> None:
    """独立事务落一条审计。

    争用败方的主事务已整笔回滚（不留扫描行、不进待路由），
    但「被拒」这一争用结局必须可重放：在回滚之后用新事务补记，
    使审计与落账同命运——成功事件必有落账，被拒事件必无落账。
    """
    with db() as conn:
        audit(conn, kind, ref_id, action, note)
