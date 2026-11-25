from __future__ import annotations
import sqlite3
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.config import PROJECT_NAME
from app.routers import audit, bags, chutes, dashboard, diverts, lines, routes, scans, settings, shifts
from app.seed import seed_if_empty

seed_if_empty()

app = FastAPI(title=PROJECT_NAME)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
for mod in (dashboard, lines, chutes, routes, scans, diverts, bags, shifts, settings, audit):
    app.include_router(mod.router, prefix="/api")


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_busy_handler(request: Request, exc: sqlite3.OperationalError):
    # 写口争用超过等待上限：事务已整笔回滚，回执必须是明确失败而非含糊 500
    return JSONResponse(
        status_code=503,
        content={"detail": "写口争用未决，本笔已整笔回滚，请重试"},
    )


@app.get("/api/健康")
def health():
    return {"产品": PROJECT_NAME, "状态": "正常"}
