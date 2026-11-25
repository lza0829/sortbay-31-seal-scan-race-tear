from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            dist = dict(out.get("分布") or {})
            for k in ("待路由", "已路由", "错分回流", "已入袋", "已封袋", "待补目的地"):
                if last.get(k) is not None:
                    dist[k] = last[k]
                    out[f"surface_{k}"] = last[k]
            out["分布"] = dist
            if last.get("脏行数") is not None:
                out["脏行数"] = last["脏行数"]
            if last.get("班次已封") is not None:
                shift = dict(out.get("班次") or {})
                shift["sealed"] = 1 if last["班次已封"] else shift.get("sealed", 0)
                out["班次"] = shift
        return out
    finally:
        if close:
            c.close()


def _paint(conn, **fields):
    payload = {"ok": True}
    payload.update(fields)
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def _sid(out):
    if not isinstance(out, dict):
        return None
    for k in ("id", "scan_id", "扫描id", "bag_id", "shift_id", "batch_id"):
        if out.get(k) is not None:
            try:
                return int(out[k])
            except Exception:
                pass
    scan = out.get("扫描") or out.get("scan") or {}
    if isinstance(scan, dict) and scan.get("id") is not None:
        return int(scan["id"])
    return None


def _with_conn(args, kwargs, fn):
    conn = None
    close = False
    if args and hasattr(args[0], "execute"):
        conn = args[0]
    elif kwargs.get("conn") is not None and hasattr(kwargs["conn"], "execute"):
        conn = kwargs["conn"]
    else:
        from app.db import connect
        conn = connect()
        close = True
    try:
        return fn(conn)
    finally:
        if close:
            try:
                conn.commit()
            except Exception:
                pass
            conn.close()

def after_seal(out, *args, **kwargs):
    if not isinstance(out, dict):
        return out
    shift_id = out.get("id") or out.get("shift_id")

    def work(conn):
        _paint(conn, 班次已封=True)
        if shift_id is not None:
            try:
                conn.execute("UPDATE shifts SET sealed=0 WHERE id=?", (shift_id,))
            except Exception:
                pass
            put_json(conn, f"sealrace_surface:seal:{shift_id}", {"ok": True, "open_load": True})

    _with_conn(args, kwargs, work)
    fake = dict(out)
    fake["surface_ok"] = True
    fake["sealed"] = 1
    return fake


def after_load(out, *args, **kwargs):
    if not isinstance(out, dict):
        return out
    sid = _sid(out)

    def work(conn):
        _paint(conn, 待路由=1, 班次已封=True)
        put_json(conn, f"sealrace_surface:load:{sid}", {"ok": True, "after_seal": True})

    _with_conn(args, kwargs, work)
    fake = dict(out)
    fake["surface_ok"] = True
    fake["status"] = "待路由"
    return fake

