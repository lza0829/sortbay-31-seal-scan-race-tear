"""封班与过机装载争用夹具。

架构A（封班优先，全局唯一口径）：
  封班与装载在同一 BEGIN IMMEDIATE 闸上序列化；闸字段 shifts.sealed 同事务检查。
  - 封班先提交 ⇒ 并发装载整笔失败：不留扫描行、不进待路由、不落成功审计；
  - 装载先落账 ⇒ 封班被未清件检查拒绝，班次保持未封；
  - 半截（事务中断）⇒ 整笔回滚到争用前快照；
  - 败方「被拒」独立补记审计，争用结局可从审计重放。

定位面：封班入口、装载入口、扫描册、待路由桶、班次页、裁决入口、总览、审计。
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.db as db_mod
import app.routers.scans as scans_mod
import app.routers.shifts as shifts_mod
from app.main import app

DESTS = ["110", "210", "510", "999"]


# ---------------------------------------------------------------- 表面探针

def latest_shift(client):
    return client.get("/api/班次").json()[0]


def shift_by_id(client, sid):
    return next(s for s in client.get("/api/班次").json() if s["id"] == sid)


def scans_all(client):
    return client.get("/api/扫描").json()


def pending_bucket(client):
    """待路由桶"""
    return client.get("/api/扫描", params={"状态": "待路由"}).json()


def reconcile(client):
    return client.get("/api/班次/对账").json()


def overview(client):
    return client.get("/api/总览").json()


def audit_events(client):
    return client.get("/api/审计").json()


def snapshot(client):
    """争用前/后全表面快照：扫描册、待路由桶、班次页、对账、总览、审计。"""
    return {
        "scans": scans_all(client),
        "bucket": pending_bucket(client),
        "shifts": client.get("/api/班次").json(),
        "reconcile": reconcile(client),
        "overview": overview(client),
        "audit": audit_events(client),
    }


# ---------------------------------------------------------------- 全程收口工具

def find_or_open_bag(client, chute_id):
    for b in client.get("/api/封袋").json():
        if b["chute_id"] == chute_id and b["status"] == "集包中":
            return b["id"]
    r = None
    for _ in range(3):
        r = client.post("/api/封袋/开袋", json={"口门编号": chute_id})
        if r.status_code == 200:
            return r.json()["id"]
        time.sleep(1.1)  # 袋码按秒生成，同秒撞码等下一秒（既有限缺陷，另立需求）
    raise AssertionError(f"开袋失败: {r.status_code} {r.text}")


def walk_scan_to_sealed_bag(client, scan_id):
    """路由裁决 → 开袋 → 入袋 → 封口，把一件扫描走到已封袋。"""
    r = client.post(f"/api/扫描/{scan_id}/路由")
    assert r.status_code == 200, r.text
    chute_id = r.json()["chute_id"]
    bag_id = find_or_open_bag(client, chute_id)
    r = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/封袋/{bag_id}/封口")
    assert r.status_code == 200, r.text


def make_sealable(client):
    """把当前班所有未清件（待路由/错分回流/已路由/已入袋）收口到已封袋。"""
    for _ in range(30):
        shift = latest_shift(client)
        mine = [s for s in scans_all(client) if s["shift_id"] == shift["id"]]
        todo = [s for s in mine if s["status"] in ("待路由", "错分回流", "已路由", "已入袋")]
        if not todo:
            return shift
        for s in todo:
            if s["status"] in ("待路由", "错分回流"):
                r = client.post(f"/api/扫描/{s['id']}/路由")
                assert r.status_code == 200, r.text
            elif s["status"] == "已路由":
                bag_id = find_or_open_bag(client, s["chute_id"])
                r = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": s["id"]})
                assert r.status_code == 200, r.text
            elif s["status"] == "已入袋":
                r = client.post(f"/api/封袋/{s['bag_id']}/封口")
                assert r.status_code == 200, r.text
    raise AssertionError("无法收口到可封班状态")


# ---------------------------------------------------------------- 同命运闭包断言

def assert_surfaces_consistent(client):
    """七个定位面在任何时刻都必须同命运（不变式）。"""
    scans = scans_all(client)
    bucket = pending_bucket(client)
    ov = overview(client)
    rec = reconcile(client)
    shifts = client.get("/api/班次").json()
    latest = shifts[0]
    # 待路由桶三面一致：扫描册过滤 == 桶 == 总览分布
    assert len([s for s in scans if s["status"] == "待路由"]) == len(bucket)
    assert ov["分布"]["待路由"] == len(bucket)
    # 对账未清件 == 该班 待路由+错分回流
    pend = len([s for s in scans
                if s["shift_id"] == rec["班次"]["id"] and s["status"] in ("待路由", "错分回流")])
    assert rec["未清件数"] == pend
    # 封班灯三面一致：班次页 == 对账 == 总览
    assert latest["sealed"] == rec["班次"]["sealed"] == ov["班次"]["sealed"]
    # 已封班 ⇒ 该班绝无未清件、待路由桶绝无该班行（不得已封仍增待路由）
    if latest["sealed"]:
        assert pend == 0
        assert not [s for s in bucket if s["shift_id"] == latest["id"]]


def assert_stable(client, rounds=3):
    """长期不拧花：反复重读，任何表面不得漂移。"""
    snaps = [snapshot(client) for _ in range(rounds)]
    for s in snaps[1:]:
        assert s == snaps[0]


# ---------------------------------------------------------------- 夹具：确定性方向一

def test_seal_wins_load_rejected_all_surfaces(env):
    """封班先提交 ⇒ 并发装载整笔失败：无行、无桶、无成功审计，各面同刷。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)
    snap = snapshot(client)

    r = client.post(f"/api/班次/{shift['id']}/封班")
    assert r.status_code == 200 and r.json()["sealed"] == 1

    # 封班已生效 ⇒ 装载整笔拒绝（409），回执不得假装成功
    r = client.post("/api/扫描", json={"条码": "AFTER-SEAL", "目的地码": "110"})
    assert r.status_code == 409
    assert "封班" in r.json()["detail"]

    # 扫描册/待路由桶：无此行、桶不增（已封仍增待路由 ⇒ 未闭合）
    assert not [s for s in scans_all(client) if s["barcode"] == "AFTER-SEAL"]
    assert len(pending_bucket(client)) == len(snap["bucket"])

    # 班次页/对账/总览：封班灯一致，待路由未增
    assert_surfaces_consistent(client)
    assert reconcile(client)["未清件数"] == 0
    assert overview(client)["分布"]["待路由"] == snap["overview"]["分布"]["待路由"]

    # 裁决入口：已封 ⇒ 写口关闭，与封班灯同命运
    r = client.post("/api/扫描/1/路由")
    assert r.status_code == 400 and "封班" in r.json()["detail"]

    # 审计：封班事件 + 装载被拒事件；绝无“过机装载 AFTER-SEAL”成功事件
    events = audit_events(client)
    assert any(e["kind"] == "班次" and e["action"] == "封班" and e["ref_id"] == shift["id"]
               for e in events)
    assert any(e["action"] == "装载被拒" and "AFTER-SEAL" in e["note"] for e in events)
    assert not any(e["action"] == "过机装载" and e["note"] == "AFTER-SEAL" for e in events)

    assert_stable(client)


# ---------------------------------------------------------------- 夹具：确定性方向二

def test_load_lands_first_seal_rejected_all_surfaces(env):
    """装载先落账 ⇒ 封班被拒：行在、桶+1、班未封、裁决入口可用，收口后封班可成。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)
    snap = snapshot(client)

    r = client.post("/api/扫描", json={"条码": "BEFORE-SEAL", "目的地码": "210"})
    assert r.status_code == 200
    scan_id = r.json()["id"]

    # 封班入口：在途件已落账 ⇒ 拒封，回执不得假装已封
    r = client.post(f"/api/班次/{shift['id']}/封班")
    assert r.status_code == 409 and "禁止封班" in r.json()["detail"]

    # 落账一致：行在（待路由）、桶+1、班未封、总览+1
    row = next(s for s in scans_all(client) if s["barcode"] == "BEFORE-SEAL")
    assert row["status"] == "待路由" and row["shift_id"] == shift["id"]
    assert len(pending_bucket(client)) == len(snap["bucket"]) + 1
    assert not latest_shift(client)["sealed"]
    assert overview(client)["分布"]["待路由"] == snap["overview"]["分布"]["待路由"] + 1
    assert reconcile(client)["未清件数"] == 1
    assert_surfaces_consistent(client)

    # 裁决入口：未封 ⇒ 可用，与落账同命运
    r = client.post(f"/api/扫描/{scan_id}/路由")
    assert r.status_code == 200, r.text

    # 审计：过机装载 + 封班被拒；绝无该班封班事件
    events = audit_events(client)
    assert any(e["action"] == "过机装载" and e["note"] == "BEFORE-SEAL" for e in events)
    assert any(e["action"] == "封班被拒" and e["ref_id"] == shift["id"] for e in events)
    assert not any(e["action"] == "封班" and e["ref_id"] == shift["id"] for e in events)

    # 收口：走完全程 → 封班成功 → 待路由归零，各面仍同命运
    make_sealable(client)
    r = client.post(f"/api/班次/{shift['id']}/封班")
    assert r.status_code == 200 and r.json()["sealed"] == 1
    assert overview(client)["分布"]["待路由"] == snap["overview"]["分布"]["待路由"]
    assert_surfaces_consistent(client)
    assert_stable(client)


# ---------------------------------------------------------------- 夹具：并发对撞

def test_concurrent_collision_closure(env):
    """封班与装载并发对撞多轮：恰一胜一负，败方整笔回滚，七面同命运。"""
    client = env
    make_sealable(client)

    for round_no in range(6):
        shift = latest_shift(client)
        assert not shift["sealed"]
        barcode = f"RACE-{round_no}"
        snap = snapshot(client)
        audit_watermark = snap["audit"][0]["id"] if snap["audit"] else 0

        barrier = threading.Barrier(2)
        results = {}

        def do_scan():
            c = TestClient(app, raise_server_exceptions=False)
            barrier.wait()
            r = c.post("/api/扫描", json={"条码": barcode, "目的地码": DESTS[round_no % len(DESTS)]})
            results["scan"] = (r.status_code, r.json())

        def do_seal():
            c = TestClient(app, raise_server_exceptions=False)
            barrier.wait()
            r = c.post(f"/api/班次/{shift['id']}/封班")
            results["seal"] = (r.status_code, r.json())

        t1 = threading.Thread(target=do_scan)
        t2 = threading.Thread(target=do_seal)
        t1.start(); t2.start(); t1.join(); t2.join()

        scan_code, scan_body = results["scan"]
        seal_code, seal_body = results["seal"]
        # 争用结局：恰一胜一负；禁止双胜（已封仍增待路由）与双负（在途无着落）
        assert {scan_code, seal_code} == {200, 409}, (
            f"第{round_no}轮对撞结局异常: scan={scan_code} {scan_body} seal={seal_code} {seal_body}")

        new_events = [e for e in audit_events(client) if e["id"] > audit_watermark]
        row = next((s for s in scans_all(client) if s["barcode"] == barcode), None)

        if seal_code == 200:
            # 封班胜：装载整笔失败 —— 无行、无桶、无成功审计
            assert scan_code == 409 and "封班" in scan_body["detail"]
            assert row is None, "封班已生效仍多出扫描行"
            assert len(pending_bucket(client)) == len(snap["bucket"])
            assert shift_by_id(client, shift["id"])["sealed"] == 1
            assert any(e["action"] == "封班" and e["ref_id"] == shift["id"] for e in new_events)
            assert any(e["action"] == "装载被拒" and barcode in e["note"] for e in new_events)
            assert not any(e["action"] == "过机装载" and e["note"] == barcode for e in new_events)
            # 裁决入口随封班关闭
            r = client.post("/api/扫描/1/路由")
            assert r.status_code == 400 and "封班" in r.json()["detail"]
            # 开新班进入下一轮
            r = client.post("/api/班次", json={"营业日": "2026-11-25", "班次名": f"对撞{round_no}班"})
            assert r.status_code == 200, r.text
        else:
            # 装载胜：封班被拒 —— 行在、桶+1、班未封、裁决入口可用
            assert scan_code == 200
            assert seal_code == 409 and "禁止封班" in seal_body["detail"]
            assert row is not None and row["status"] == "待路由", "装载成功却无行"
            assert len(pending_bucket(client)) == len(snap["bucket"]) + 1
            assert not shift_by_id(client, shift["id"])["sealed"]
            assert any(e["action"] == "过机装载" and e["note"] == barcode for e in new_events)
            assert any(e["action"] == "封班被拒" and e["ref_id"] == shift["id"] for e in new_events)
            assert not any(e["action"] == "封班" and e["ref_id"] == shift["id"] for e in new_events)
            r = client.post(f"/api/扫描/{row['id']}/路由")
            assert r.status_code == 200, r.text  # 裁决入口与未封同命运
            make_sealable(client)  # 收口，恢复可封班

        assert_surfaces_consistent(client)
        assert_stable(client)


# ---------------------------------------------------------------- 夹具：半截回滚

def test_half_cut_scan_rolls_back_to_pre_contention_snapshot(env):
    """装载半截（落行后审计前断电）⇒ 整笔回滚到争用前快照，随后封班不多出待路由。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)
    snap = snapshot(client)

    def boom(*a, **k):
        raise RuntimeError("模拟装载半截：落行后审计前断电")

    orig = scans_mod.audit
    scans_mod.audit = boom
    try:
        r = client.post("/api/扫描", json={"条码": "HALF-CUT", "目的地码": "110"})
        assert r.status_code == 500
    finally:
        scans_mod.audit = orig

    # 争用前快照逐面恢复：扫描册、待路由桶、审计（无半截残留）、班未封
    now = snapshot(client)
    assert now["scans"] == snap["scans"]
    assert now["bucket"] == snap["bucket"]
    assert now["audit"] == snap["audit"]
    assert not latest_shift(client)["sealed"]
    assert_surfaces_consistent(client)

    # 随后封班可成，且已封后待路由仍为零：半截无残留、已封未增待路由
    r = client.post(f"/api/班次/{shift['id']}/封班")
    assert r.status_code == 200
    assert overview(client)["分布"]["待路由"] == 0
    assert_surfaces_consistent(client)
    assert_stable(client)


def test_half_cut_seal_rolls_back_to_pre_contention_snapshot(env):
    """封班半截（落锁后审计前断电）⇒ 回滚：班未封、无封班事件、装载闸仍开。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)

    def boom(*a, **k):
        raise RuntimeError("模拟封班半截：落锁后审计前断电")

    orig = shifts_mod.audit
    shifts_mod.audit = boom
    try:
        r = client.post(f"/api/班次/{shift['id']}/封班")
        assert r.status_code == 500
    finally:
        shifts_mod.audit = orig

    # 回滚：班未封、无封班审计事件；装载入口仍开（闸状态一致）
    assert not latest_shift(client)["sealed"]
    assert not any(e["action"] == "封班" and e["ref_id"] == shift["id"] for e in audit_events(client))
    r = client.post("/api/扫描", json={"条码": "AFTER-HALF-SEAL", "目的地码": "110"})
    assert r.status_code == 200
    assert_surfaces_consistent(client)

    # 收口后封班可成
    make_sealable(client)
    r = client.post(f"/api/班次/{shift['id']}/封班")
    assert r.status_code == 200
    assert_surfaces_consistent(client)
    assert_stable(client)


# ---------------------------------------------------------------- 夹具：争用结局可重放（闸层）

def test_gate_level_contention_replay(env):
    """在闸层确定性回放两个方向的争用结局：写口互斥 + 闸字段同事务裁决。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)

    # 方向一：封班事务在途未提交 ⇒ 装载进不了闸；封班提交后装载重进读到 sealed=1 ⇒ 整笔拒绝
    conn_a = db_mod.connect()
    conn_a.execute("BEGIN IMMEDIATE")
    conn_a.execute(
        "UPDATE shifts SET sealed=1, sealed_at='2026-11-25 09:00:00' WHERE id=? AND sealed=0",
        (shift["id"],),
    )
    conn_b = db_mod.connect()
    conn_b.execute("PRAGMA busy_timeout=200")
    with pytest.raises(sqlite3.OperationalError):
        conn_b.execute("BEGIN IMMEDIATE")  # 写口互斥：在途封班未收口，装载进不了闸
    conn_a.commit()
    conn_b.execute("BEGIN IMMEDIATE")  # 重进：读到已提交封班态
    assert conn_b.execute("SELECT sealed FROM shifts WHERE id=?", (shift["id"],)).fetchone()["sealed"] == 1
    conn_b.rollback()  # 域裁决：已封 ⇒ 装载整笔拒绝，不落行
    conn_b.close(); conn_a.close()
    assert not [s for s in scans_all(client) if s["barcode"] == "REPLAY-1"]
    assert shift_by_id(client, shift["id"])["sealed"] == 1
    r = client.post("/api/扫描", json={"条码": "REPLAY-1", "目的地码": "110"})
    assert r.status_code == 409  # HTTP 面重放同一结局

    # 方向二：装载事务在途未提交 ⇒ 封班进不了闸；装载提交后封班重进点到未清件 ⇒ 拒封
    r = client.post("/api/班次", json={"营业日": "2026-11-25", "班次名": "回放班"})
    assert r.status_code == 200
    shift2 = r.json()
    conn_a = db_mod.connect()
    conn_a.execute("BEGIN IMMEDIATE")
    conn_a.execute(
        "INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note) "
        "VALUES ('REPLAY-2', '110', ?, 1, '待路由', '2026-11-25 09:30:00', '')",
        (shift2["id"],),
    )
    conn_b = db_mod.connect()
    conn_b.execute("PRAGMA busy_timeout=200")
    with pytest.raises(sqlite3.OperationalError):
        conn_b.execute("BEGIN IMMEDIATE")  # 写口互斥：在途装载未收口，封班进不了闸
    conn_a.commit()
    conn_b.execute("BEGIN IMMEDIATE")  # 重进：点到已落账未清件
    pending = conn_b.execute(
        "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('待路由','错分回流','已路由','已入袋')",
        (shift2["id"],),
    ).fetchone()["c"]
    assert pending == 1  # 域裁决：有未清件 ⇒ 拒封
    conn_b.rollback()
    conn_b.close(); conn_a.close()
    assert not shift_by_id(client, shift2["id"])["sealed"]
    r = client.post(f"/api/班次/{shift2['id']}/封班")
    assert r.status_code == 409  # HTTP 面重放同一结局
    assert [s for s in pending_bucket(client) if s["barcode"] == "REPLAY-2"]

    # 回放完毕收口：两个方向终态都与审计/落账一致
    make_sealable(client)
    r = client.post(f"/api/班次/{shift2['id']}/封班")
    assert r.status_code == 200
    assert_surfaces_consistent(client)
    assert_stable(client)


# ---------------------------------------------------------------- 夹具：审计重放

def test_audit_replay_matches_ledger(env):
    """审计重放与落账同命运：成功事件必有落账，被拒事件必无落账，事件序定胜负。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)

    r = client.post("/api/扫描", json={"条码": "AUD-OK", "目的地码": "210"})
    assert r.status_code == 200
    walk_scan_to_sealed_bag(client, r.json()["id"])
    r = client.post(f"/api/班次/{shift['id']}/封班")
    assert r.status_code == 200
    r = client.post("/api/扫描", json={"条码": "AUD-REJ", "目的地码": "210"})
    assert r.status_code == 409

    events = audit_events(client)
    by_barcode = {s["barcode"]: s for s in scans_all(client)}
    for e in events:
        if e["kind"] == "扫描" and e["action"] == "过机装载":
            assert e["note"] in by_barcode, f"过机装载事件无落账: {e}"
        if e["kind"] == "扫描" and e["action"] == "装载被拒":
            barcode = e["note"].split("条码")[1].split(":")[0]
            assert barcode not in by_barcode, f"装载被拒却有落账: {e}"
        if e["kind"] == "班次" and e["action"] == "封班":
            assert shift_by_id(client, e["ref_id"])["sealed"] == 1, f"封班事件未落账: {e}"

    # 事件序定胜负：封班（胜）先于装载被拒（负）落审计
    seal_id = next(e["id"] for e in events if e["action"] == "封班" and e["ref_id"] == shift["id"])
    rej_id = next(e["id"] for e in events if e["action"] == "装载被拒" and "AUD-REJ" in e["note"])
    assert seal_id < rej_id
    # 同班事件序：所有封班被拒必先于该班封班（被拒不得出现在已封之后）
    for sid in {e["ref_id"] for e in events if e["kind"] == "班次"}:
        seq = sorted(e["id"] for e in events
                     if e["kind"] == "班次" and e["ref_id"] == sid and e["action"] in ("封班", "封班被拒"))
        acts = {e["id"]: e["action"] for e in events if e["kind"] == "班次" and e["ref_id"] == sid}
        if any(acts[i] == "封班" for i in seq):
            seal_eid = min(i for i in seq if acts[i] == "封班")
            assert all(acts[i] == "封班被拒" for i in seq if i < seal_eid)
            assert not any(acts[i] == "封班被拒" for i in seq if i > seal_eid)

    assert_surfaces_consistent(client)
    assert_stable(client)


# ---------------------------------------------------------------- 夹具：总览与封班灯长期一致

def test_overview_and_seal_lamp_no_longterm_divergence(env):
    """封班灯与总览不得长期拧花：封班后多轮重读，灯、桶、对账、审计恒一致。"""
    client = env
    make_sealable(client)
    shift = latest_shift(client)
    r = client.post(f"/api/班次/{shift['id']}/封班")
    assert r.status_code == 200
    r = client.post("/api/扫描", json={"条码": "LAMP-1", "目的地码": "110"})
    assert r.status_code == 409

    for _ in range(3):
        ov = overview(client)
        rec = reconcile(client)
        shifts = client.get("/api/班次").json()
        assert ov["班次"]["sealed"] == rec["班次"]["sealed"] == shifts[0]["sealed"] == 1
        assert ov["分布"]["待路由"] == 0
        assert rec["未清件数"] == 0
        assert not [s for s in pending_bucket(client) if s["shift_id"] == shift["id"]]
        time.sleep(0.05)
    assert_stable(client)
