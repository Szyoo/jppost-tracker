"""主屏 Web App：清单与图标免登录可取、/api/version、页面缓存头与 data-version、每个页面 head 里的必需 meta。"""
import json
import os
import re

import pytest

import app as app_module
from conftest import ADMIN_PASSWORD, gate

TEMPLATE_DIR = os.path.join(os.path.dirname(app_module.__file__), "templates")

PUBLIC_ASSETS = [
    ("/manifest.webmanifest", "application/manifest+json"),
    ("/icon-192.png", "image/png"),
    ("/icon-512.png", "image/png"),
    ("/apple-touch-icon.png", "image/png"),
    ("/favicon.svg", "image/svg+xml"),
    ("/favicon.ico", "image/x-icon"),
]

REQUIRED_HEAD = [
    r'<meta name="viewport" content="[^"]*viewport-fit=cover[^"]*"',
    r'<link rel="manifest" href="/manifest.webmanifest"',
    r'<meta name="apple-mobile-web-app-capable" content="yes"',
    r'<meta name="mobile-web-app-capable" content="yes"',
    r'<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"',
    r'<meta name="apple-mobile-web-app-title" content="JPPost Tracker"',
    r'<meta name="theme-color" content="#0b1020"',
    r'<link rel="apple-touch-icon" href="/apple-touch-icon.png"',
]


def head_of(html: str) -> str:
    match = re.search(r"<head>(.*?)</head>", html, re.S)
    assert match, "页面缺少 <head>"
    return match.group(1)


def assert_webapp_page(resp):
    html = resp.get_data(as_text=True)
    head = head_of(html)
    for pattern in REQUIRED_HEAD:
        assert re.search(pattern, head), f"head 缺少 {pattern}"
    assert f'data-version="{app_module.APP_VERSION}"' in html
    assert resp.headers["Cache-Control"] in {"no-cache", "no-store"}
    assert "/static/boot.js" in html  # autoUpdate 在 boot.js 里挂


# --- 清单与图标 ---

@pytest.mark.parametrize("path,mimetype", PUBLIC_ASSETS)
def test_assets_are_public(client, path, mimetype):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.mimetype == mimetype
    assert resp.data


@pytest.mark.parametrize("path,mimetype", PUBLIC_ASSETS)
def test_assets_public_under_sso(client, sso, path, mimetype):
    # 门卫放行后请求不带任何身份头，应用自己也不能拦
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.mimetype == mimetype


def test_manifest_content_and_cache(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.headers["Cache-Control"] == "no-cache"
    data = json.loads(resp.data)
    assert data["display"] == "standalone"
    assert data["start_url"] == "/"
    assert {icon["src"] for icon in data["icons"]} == {"/icon-192.png", "/icon-512.png"}


def test_png_sizes(client):
    import struct

    for path, size in [("/icon-192.png", 192), ("/icon-512.png", 512), ("/apple-touch-icon.png", 180)]:
        data = client.get(path).data
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (size, size), path


def test_other_paths_still_protected(client, sso):
    # 放行清单 / 图标 / 版本号不能顺带放开别的路径（伪造身份头在 SSO 下由门卫剥掉，这里只验应用侧）
    assert client.get("/api/me", headers={"Authorization": "Bearer anything"}).status_code == 401
    assert client.get("/static/../app.py").status_code == 404
    resp = client.get("/")
    assert resp.status_code in (302, 401)


# --- 版本号 ---

def test_api_version_is_public(client):
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.get_json() == {"version": app_module.APP_VERSION}
    assert resp.headers["Cache-Control"] == "no-store"


def test_api_version_public_under_sso(client, sso):
    assert client.get("/api/version").get_json() == {"version": app_module.APP_VERSION}


def test_source_version_is_stable_and_content_based(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython.pyc").write_bytes(b"junk")
    first = app_module.compute_source_version(str(tmp_path))
    assert first == app_module.compute_source_version(str(tmp_path))
    (tmp_path / "__pycache__" / "b.pyc").write_bytes(b"more junk")
    assert first == app_module.compute_source_version(str(tmp_path))
    (tmp_path / "a.py").write_text("x = 2")
    assert first != app_module.compute_source_version(str(tmp_path))


# --- 静态文件缓存头 ---

@pytest.mark.parametrize("path", ["/static/boot.js", "/static/app.js", "/static/style.css"])
def test_static_no_cache(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-cache"


# --- 每个完整页面 ---

def test_login_and_register_pages(client):
    for path in ("/login", "/register"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert_webapp_page(resp)


def test_admin_console_page(client):
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="app"' in resp.get_data(as_text=True)
    assert_webapp_page(resp)


def test_user_portal_page(client, db, sso):
    alice = db.create_account({"username": "alice", "password": "pw-alice", "role": "user"})
    db.set_account_portal_sub(alice["id"], "sub-alice")
    resp = client.get("/", headers=gate("alice"))
    assert resp.status_code == 200
    assert b"My Delivery Space" in resp.data
    assert_webapp_page(resp)


def test_anon_landing_page(client, sso):
    resp = client.get("/", headers={"X-Portal-Anon": "1"})
    assert resp.status_code == 200
    assert_webapp_page(resp)


def test_sso_denied_page(client, sso):
    resp = client.get("/", headers=gate("stranger"))
    assert resp.status_code == 403
    assert_webapp_page(resp)


def test_every_full_page_template_includes_webapp_head():
    # 新增页面模板时也得带上：凡是完整 HTML 文档的模板都要 include 片段并写 data-version
    pages = [name for name in os.listdir(TEMPLATE_DIR) if name.endswith(".html") and not name.startswith("_")]
    assert pages
    for name in pages:
        with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as fh:
            text = fh.read()
        if "<html" not in text:
            continue
        assert "{% include '_webapp_head.html' %}" in text, name
        assert 'data-version="{{ APP_VERSION }}"' in text, name
        assert "viewport" not in text.replace("{% include '_webapp_head.html' %}", ""), f"{name} 另写了 viewport"
