"""测试隔离：锁一个测试库，并在每个测试模块开始前重建夹具。

`app.db.DB_PATH` 在导入时由 `DATA_DIR` 固定；多个测试模块各自设置 `DATA_DIR`
会互相覆盖，导致共享同一库、顺序相关。这里在导入 app 之前锁定测试目录，
再按模块重建，使各测试文件彼此独立。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

os.environ["DATA_DIR"] = str(ROOT / "backend" / "data_test_shared")
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

import pytest  # noqa: E402

from app import db as app_db  # noqa: E402  (导入即锁定 DB_PATH)
from app import seed  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _fresh_db():
    for path in (
        app_db.DB_PATH,
        Path(str(app_db.DB_PATH) + "-wal"),
        Path(str(app_db.DB_PATH) + "-shm"),
    ):
        if path.exists():
            path.unlink()
    seed.init_db()
    yield
