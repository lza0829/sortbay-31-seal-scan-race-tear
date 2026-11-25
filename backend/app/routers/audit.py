from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit():
    # 审计只回放真实落账事件：成功事件必有落账、被拒事件必无落账。
    # 禁止插入任何表面宣称事件，争用结局只能由审计与落账同命运重放。
    with db() as conn:
        return rows_to_list(conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT 200"
        ).fetchall())
