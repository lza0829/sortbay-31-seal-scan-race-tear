"""测试基座：每用例一个独立临时库，HTTP 层走 TestClient。

环境变量必须在任何 app 模块导入前设定（config 在导入期读 SORTBAY_DB）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

_BOOT = tempfile.mkdtemp(prefix="sortbay-boot-")
os.environ.setdefault("SORTBAY_DB", str(Path(_BOOT) / "boot.db"))

import app.db as db_mod  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """全新种子库 + HTTP 客户端。db_mod.DB_PATH 被换到本用例临时库。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    seed_if_empty()
    client = TestClient(app, raise_server_exceptions=False)
    client.db_path = db_file
    return client
