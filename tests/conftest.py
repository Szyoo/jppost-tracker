"""测试夹具：每个测试一份独立的临时 SQLite，绝不碰 data/app.db。

storage 的 DB_PATH 是模块级常量，app.py 在 import 时就会 ensure_storage，
所以必须先 patch storage 再 import app。"""
import os
import sys
import tempfile

import pytest
from werkzeug.security import generate_password_hash

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

ADMIN_PASSWORD = "admin-test-pass"

_BOOT_DIR = tempfile.mkdtemp(prefix="jppost-test-boot-")
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash(ADMIN_PASSWORD)
os.environ["AUTO_START_TRACKER"] = "0"
os.environ["LOCAL_BARK_ENABLED"] = "0"
os.environ["SECRET_KEY"] = "test-secret"
os.environ.pop("SZYYW_SSO", None)
os.environ.pop("SZYYW_SSO_AUTOCREATE", None)

import storage  # noqa: E402

storage.DATA_DIR = _BOOT_DIR
storage.DB_PATH = os.path.join(_BOOT_DIR, "app.db")

import app as app_module  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """全新库：与线上一致——id 1 = legacy-default（停用登录），id 2 = admin。"""
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("SZYYW_SSO", raising=False)
    monkeypatch.delenv("SZYYW_SSO_AUTOCREATE", raising=False)
    monkeypatch.delenv("PORTAL_ORIGIN", raising=False)
    storage.ensure_storage(str(tmp_path / "missing.env"))
    return storage


@pytest.fixture
def client(db):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def sso(monkeypatch):
    monkeypatch.setenv("SZYYW_SSO", "1")
    return monkeypatch


def gate(user, role="user", **extra):
    """模拟 Caddy 门卫注入的身份头。"""
    headers = {"X-User": user, "X-Role": role, "X-Portal-Sub": user}
    headers.update(extra)
    return headers
