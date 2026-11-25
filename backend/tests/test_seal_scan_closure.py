"""封班/过机争用收口的第二组夹具（架构A 全局唯一口径）。

第一组 test_seal_scan_contention.py 钉方向对撞、半截回滚、闸层重放与审计重放；
本组钉旁路拆除后必须长期成立的同命运面，防止“假封班/双影子”从任何口复活：

  1. 封班灯持久：封班提交后任何重读、任何写口都不得把 shifts.sealed 改回 0
     （直接回归 after_seal 另开连接回拨占用钉的旧缺陷）；
  2. 封后全写口关闭：装载 409，路由/回流/开袋/入袋/封口一律 400，且零副作用；
  3. 条码目的地/分拣线班次审计：封后拒写不留半行、目的地与分拣线随拒绝整笔回滚；
  4. 启动毒键清理：旧表面态 settings 键在启动时被清除，总览/扫描册/审计只认真账；
  5. 口门在口件数与扫描册同命运，总览/对账/班次列/审计无任何表面态字段；
  6. 并发半截重放：装载事务在途异常与封班并发，结局唯一且无半截残留。
"""
from __future__ import annotations

import threading
import time

import app.db as db_mod
import app.routers.scans as scans_mod
from app.seed import seed_if_empty
from fastapi.testclient import TestClient

from app.main import app


def open_bag_retry(client, chute_id):
    """袋码按秒生成，同秒同口门撞码时等下一秒重试（既有限缺陷，另立需求）。"""
    r = None
    for _ in range(4):
        r = client.post("/api/封袋/开袋", json={"口门编号": chute_id})
        if r.status_code == 200:
            return r.json()["id"]
        time.sleep(1.1)
    raise AssertionError(f"开袋失败: {r.status_code} {r.text}")


def latest_shift(client):
    return client.get("/api/班次").json()[0]


def scans_all(client):
    return client.get("/api/扫描").json()


def audit_events(client):
    return client.get("/api/审计").json()


def make_sealable(client):
    """把当前班所有未清件收口到已封袋（与第一组夹具同口径的最小实现）。"""
    shift = latest_shift(client)
    for s in [r for r in scans_all(client)
              if r["shift_id"] == shift["id"]
              and r["status"] in ("待路由", "错分回流")]:
        r = client.post(f"/api/扫描/{s['id']}/路由")
        assert r.status_code == 200, r.text
    for s in [r for r in scans_all(client)
              if r["shift_id"] == shift["id"] and r["status"] == "已路由"]:
        bag_id = open_bag_retry(client, s["chute_id"])
        r = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": s["id"]})
        assert r.status_code == 200, r.text
    for s in [r for r in scans_all(client)
              if r["shift_id"] == shift["id"] and r["status"] == "已入袋"]:
        r = client.post(f"/api/封袋/{s['bag_id']}/封口")
        assert r.status_code == 200, r.text
    return shift


# ----------------------------------------------------------- 1. 封班灯持久

def test_sealed_lamp_persists_and_cannot_be_reopened_by_any_surface(env):
    """封班提交后 sealed=1 必须落库持久：重读、装载尝试、总览刷新都不得回拨。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)

    r = client.post(f"/api/班次/{shift['id']}/封班")
    assert r.status_code == 200 and r.json()["sealed"] == 1

    # 直接读库钉死占用钉：旁路曾在这里把 sealed 改回 0
    with db_mod.db() as conn:
        sealed = conn.execute(
            "SELECT sealed FROM shifts WHERE id=?", (shift["id"],)
        ).fetchone()["sealed"]
    assert sealed == 1

    # 封后装载被拒，拒绝本身不得改变封班态
    r = client.post("/api/扫描", json={"条码": "LAMP-PERSIST", "目的地码": "110"})
    assert r.status_code == 409
    assert latest_shift(client)["sealed"] == 1
    assert client.get("/api/班次/对账").json()["班次"]["sealed"] == 1
    assert client.get("/api/总览").json()["班次"]["sealed"] == 1

    # 多轮重读无漂移（封班灯与总览长期一致）
    for _ in range(3):
        ov = client.get("/api/总览").json()
        assert ov["班次"]["sealed"] == 1 and ov["分布"]["待路由"] == 0


def test_no_surface_side_channel_keys_after_seal_cycle(env):
    """完整 装载→收口→封班→拒装载 循环后，settings 不得出现任何表面态毒键。"""
    client = env
    r = client.post("/api/扫描", json={"条码": "NO-POISON", "目的地码": "210"})
    assert r.status_code == 200
    make_sealable(client)
    shift = latest_shift(client)
    assert client.post(f"/api/班次/{shift['id']}/封班").status_code == 200
    assert client.post(
        "/api/扫描", json={"条码": "NO-POISON-2", "目的地码": "210"}
    ).status_code == 409

    with db_mod.db() as conn:
        keys = [r["key"] for r in conn.execute("SELECT key FROM settings").fetchall()]
    assert not [k for k in keys if k.startswith("surface_")
                or k.startswith("sealrace_surface:")]
    # 各响应面不得携带表面态字段
    ov = client.get("/api/总览").json()
    assert "surface_ok" not in ov and "surface_probe" not in ov
    assert all(not k.startswith("surface_") for k in ov)
    for row in scans_all(client):
        assert "surface_ok" not in row and "surface_status" not in row
    for e in audit_events(client):
        assert e["kind"] != "表面" and e["action"] != "成功宣称"


# ----------------------------------------------------------- 2. 封后全写口关闭

def test_all_write_ports_close_after_seal_with_zero_side_effects(env):
    """封班后裁决回流封袋口门各写口同命运关闭，拒绝即整笔无副作用。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)
    assert client.post(f"/api/班次/{shift['id']}/封班").status_code == 200

    # 封后无未封班班次：路由/回流/开袋/入袋/封口都必须 400，且提示含封班
    r = client.post("/api/扫描/1/路由")
    assert r.status_code == 400 and "封班" in r.json()["detail"]

    r = client.post("/api/回流批次", json={"扫描编号": 1})
    assert r.status_code == 400 and "封班" in r.json()["detail"]

    r = client.post("/api/封袋/开袋", json={"口门编号": 1})
    assert r.status_code == 400 and "封班" in r.json()["detail"]

    r = client.post("/api/封袋/1/入袋", json={"扫描编号": 1})
    assert r.status_code == 400 and "封班" in r.json()["detail"]

    r = client.post("/api/封袋/1/封口")
    assert r.status_code == 400 and "封班" in r.json()["detail"]

    # 零副作用：袋、回流批次、路由事件均未增
    with db_mod.db() as conn:
        bags_before = conn.execute("SELECT COUNT(*) c FROM bags").fetchone()["c"]
        batches_before = conn.execute("SELECT COUNT(*) c FROM divert_batches").fetchone()["c"]
        events_before = conn.execute("SELECT COUNT(*) c FROM route_events").fetchone()["c"]
    # 再打一轮确认计数稳定
    client.post("/api/封袋/开袋", json={"口门编号": 1})
    client.post("/api/回流批次", json={"扫描编号": 1})
    client.post("/api/封袋/1/封口")
    with db_mod.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM bags").fetchone()["c"] == bags_before
        assert conn.execute("SELECT COUNT(*) c FROM divert_batches").fetchone()["c"] == batches_before
        assert conn.execute("SELECT COUNT(*) c FROM route_events").fetchone()["c"] == events_before

    # 班次列：封班灯只认真账，没有第二个未封班班次被悄悄造出
    shifts = client.get("/api/班次").json()
    assert all(s["sealed"] == 1 for s in shifts)


# ----------------------------------------------------------- 3. 条码/目的地/分拣线随拒绝整笔回滚

def test_rejected_load_leaves_no_barcode_dest_line_trace(env):
    """封后拒写：条码、目的地码、分拣线归属一个都不得落半行。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)
    line = client.get("/api/分拣线").json()[0]
    assert client.post(f"/api/班次/{shift['id']}/封班").status_code == 200

    payload = {"条码": "DEAD-BARCODE", "目的地码": "999", "分拣线编号": line["id"]}
    r = client.post("/api/扫描", json=payload)
    assert r.status_code == 409

    with db_mod.db() as conn:
        trace = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE barcode=?", ("DEAD-BARCODE",)
        ).fetchone()["c"]
    assert trace == 0
    # 扫描册、待路由桶同样查无此码
    assert not [s for s in scans_all(client) if s["barcode"] == "DEAD-BARCODE"]
    assert not client.get("/api/扫描", params={"状态": "待路由"}).json()
    # 审计只有被拒事件，绝无过机装载成功事件
    events = audit_events(client)
    assert any(e["action"] == "装载被拒" and "DEAD-BARCODE" in e["note"] for e in events)
    assert not any(e["action"] == "过机装载" and e["note"] == "DEAD-BARCODE" for e in events)

    # 总览分布、对账未清件维持封班态零值
    ov = client.get("/api/总览").json()
    assert sum(ov["分布"].values()) == 0 or ov["分布"]["待路由"] == 0
    assert client.get("/api/班次/对账").json()["未清件数"] == 0


# ----------------------------------------------------------- 4. 启动毒键清理

def test_boot_purges_legacy_surface_poison(tmp_path, monkeypatch):
    """旧库若残留 surface_*/sealrace_surface:* 毒键，启动 seed 必须清干净。"""
    db_file = tmp_path / "poison.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    seed_if_empty()

    # 模拟旧版本旁路落库的表面态
    with db_mod.db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('surface_ok', '1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('surface_last', '{\"ok\": true}') "
            "ON CONFLICT(key) DO UPDATE SET value='{\"ok\": true}'"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('sealrace_surface:seal:1', '{}') "
            "ON CONFLICT(key) DO UPDATE SET value='{}'"
        )
        poison_n = conn.execute("SELECT COUNT(*) c FROM settings").fetchone()["c"]
    assert poison_n >= 3

    # 重新启动（main 导入时即调用 seed_if_empty；这里直接调用同一入口）
    seed_if_empty()
    with db_mod.db() as conn:
        keys = [r["key"] for r in conn.execute("SELECT key FROM settings").fetchall()]
    assert "surface_ok" not in keys and "surface_last" not in keys
    assert not [k for k in keys if k.startswith("sealrace_surface:")]
    # 业务设置键保留
    assert "口门软容量提示" in keys


# ----------------------------------------------------------- 5. 口门在口同命运

def test_chute_at_port_counts_match_scan_ledger(env):
    """总览口门「在口件数」必须与扫描册按口门统计同命运，路由后两面同增。"""
    client = env
    make_sealable(client)
    r = client.post("/api/扫描", json={"条码": "CHUTE-1", "目的地码": "110"})
    assert r.status_code == 200
    scan_id = r.json()["id"]
    r = client.post(f"/api/扫描/{scan_id}/路由")
    assert r.status_code == 200
    chute_id = r.json()["chute_id"]

    ov = client.get("/api/总览").json()
    at_port = next(c for c in ov["口门"] if c["id"] == chute_id)["在口件数"]
    ledger_n = len([s for s in scans_all(client)
                    if s["chute_id"] == chute_id and s["status"] in ("已路由", "已入袋")])
    assert at_port == ledger_n == 1

    # 入袋后状态变已入袋，仍计入在口，两面依旧同数
    bag_id = open_bag_retry(client, chute_id)
    assert client.post(
        f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id}
    ).status_code == 200
    ov = client.get("/api/总览").json()
    at_port = next(c for c in ov["口门"] if c["id"] == chute_id)["在口件数"]
    ledger_n = len([s for s in scans_all(client)
                    if s["chute_id"] == chute_id and s["status"] in ("已路由", "已入袋")])
    assert at_port == ledger_n == 1


# ----------------------------------------------------------- 7. 写口模块名不得被旁路重绑定

def test_module_write_ports_are_the_registered_handlers(env):
    """模块名必须就是 HTTP 注册的处理函数本体。

    旧撕裂以「先定义真函数、再 def 同名包装器重绑定模块名」的方式植入：
    HTTP 装饰器注册的是原函数，包装器成为只在内部直接调用时引爆的地雷
    （封班回执 sealed=1 而库内 sealed=0）。钉死写口模块名与注册处理函数同一，
    旁路不得再以重绑定方式复活。
    """
    client = env
    endpoint = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
    import app.routers.scans as scans_mod
    import app.routers.shifts as shifts_mod
    import app.routers.dashboard as dashboard_mod
    import app.routers.audit as audit_mod

    assert scans_mod.create_scan is endpoint["/api/扫描"]
    assert scans_mod.list_scans in [
        r.endpoint for r in app.routes
        if getattr(r, "path", "") == "/api/扫描" and "GET" in r.methods
    ]
    assert shifts_mod.seal_shift is endpoint["/api/班次/{shift_id}/封班"]
    assert dashboard_mod.overview in [
        r.endpoint for r in app.routes
        if getattr(r, "path", "") == "/api/总览" and "GET" in r.methods
    ]
    assert audit_mod.list_audit in [
        r.endpoint for r in app.routes
        if getattr(r, "path", "") == "/api/审计" and "GET" in r.methods
    ]

    # 写口模块里不得残留任何表面态装配符号
    import inspect
    for mod in (scans_mod, shifts_mod, dashboard_mod, audit_mod):
        src = inspect.getsource(mod)
        assert "surface" not in src and "skew" not in src and "sealrace" not in src


# ----------------------------------------------------------- 6. 并发半截重放

def test_concurrent_half_cut_load_vs_seal_leaves_no_residue(env):
    """装载在途半截（审计前异常）与封班并发：结局唯一，无半截行、无双影子。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)
    barrier = threading.Barrier(2)
    results = {}

    def half_load():
        c = TestClient(app, raise_server_exceptions=False)
        orig = scans_mod.audit
        def boom(*a, **k):
            raise RuntimeError("并发半截：装载落行后审计前断电")
        scans_mod.audit = boom
        try:
            barrier.wait()
            r = c.post("/api/扫描", json={"条码": "RACE-HALF", "目的地码": "110"})
            results["load"] = r.status_code
        finally:
            scans_mod.audit = orig

    def do_seal():
        c = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        r = c.post(f"/api/班次/{shift['id']}/封班")
        results["seal"] = (r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else {})

    t1 = threading.Thread(target=half_load)
    t2 = threading.Thread(target=do_seal)
    t1.start(); t2.start(); t1.join(); t2.join()

    # 装载两种合法结局，都必须零残留：
    #  - 装载先拿锁：落行后审计前异常 ⇒ 500 整笔回滚（半截路径）；
    #  - 封班先拿锁：装载等锁后重读 sealed=1，插入前即 409 拒入（走不到半截点）。
    assert results["load"] in (409, 500)
    # 装载不会提交任何未清件，封班终必成功；503 为锁等待未决，重试即可
    if results["seal"][0] != 200:
        for _ in range(5):
            r = client.post(f"/api/班次/{shift['id']}/封班")
            if r.status_code == 200:
                break
            assert r.status_code == 503, r.text
        else:
            raise AssertionError("封班重试后仍未成功")
    assert latest_shift(client)["sealed"] == 1
    assert client.get("/api/总览").json()["分布"]["待路由"] == 0

    # 半截无残留：扫描册查无此码，无成功装载审计
    assert not [s for s in scans_all(client) if s["barcode"] == "RACE-HALF"]
    events = audit_events(client)
    assert not any(e["action"] == "过机装载" and e["note"] == "RACE-HALF" for e in events)

    # 封班已在对撞中落账：终态直接核对，不得再封一次
    ov = client.get("/api/总览").json()
    rec = client.get("/api/班次/对账").json()
    assert ov["班次"]["sealed"] == rec["班次"]["sealed"] == 1
    assert rec["未清件数"] == 0 and ov["分布"]["待路由"] == 0
