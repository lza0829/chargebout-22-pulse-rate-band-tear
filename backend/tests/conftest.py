from __future__ import annotations

import os
import tempfile
from pathlib import Path

# 必须在导入 app.* 之前固定独立测试库，避免污染 data/chargebout.db。
_TMP = Path(tempfile.mkdtemp(prefix="chargebout-test-"))
os.environ["CHARGEBOUT_DB"] = str(_TMP / "test.db")

import pytest  # noqa: E402

from app.db import db  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    path = Path(os.environ["CHARGEBOUT_DB"])
    if path.exists():
        path.unlink()
    seed_if_empty()
    yield
    if path.exists():
        path.unlink()


@pytest.fixture()
def conn():
    with db() as c:
        yield c
