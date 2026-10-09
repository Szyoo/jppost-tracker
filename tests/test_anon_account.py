"""SZYYW_SSO=1 且门卫标记匿名（X-Portal-Anon: 1）：首页给公开落地页，其余数据接口照旧 401/拒绝。"""
import app as app_module
from conftest import gate

ANON = {"X-Portal-Anon": "1", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "jppost.szyyw.xyz"}


def seed_task(db):
    """给 admin 建一个任务，用来确认匿名页面里不会漏出任何任务数据。"""
    db.create_task(2, {"tracking_number": "ZZ987654321JP", "label": "secret-label", "enabled": True})


def test_anon_index_is_public_landing(client, db, sso):
    seed_task(db)
    resp = client.get("/", headers=ANON)
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    html = resp.data.decode()
    # 页面壳 + 账户菜单挂载条件（boot.js 在 data-sso=1 时挂 mountAccountMenu）
    assert 'data-sso="1" data-portal="https://szyyw.xyz"' in html
    assert '<div class="bg-layer"></div>' in html and "/static/boot.js" in html
    assert "日本邮政快递追踪" in html and 'action="/login"' in html and "登录" in html
    # 没有任何任务 / 设置 / 管理 / 退出
    for leaked in ("ZZ987654321JP", "secret-label", "My Delivery Space", "Admin Control Room",
                   "/me/tasks", "/me/update", "/logout", "initial-user-state", "Bark Key", "系统设置"):
        assert leaked not in html, leaked


def test_anon_head_index(client, db, sso):
    assert client.head("/", headers=ANON).status_code == 200


def test_boot_mounts_account_menu(client, db):
    boot = client.get("/static/boot.js").data.decode()
    # mountChrome 在 portal 非空时挂切换器 + 账户菜单；SSO 关时传 null 不挂
    assert "mountChrome(" in boot and "root.dataset.sso === '1'" in boot and ": null" in boot
    # 设计包走 CDN：boot.js 用 import map 的裸说明符，不写死版本，也不再有本地副本
    assert "from '@szyyw/design/chrome.js'" in boot and "/static/vendor/" not in boot
    assert client.get("/static/vendor/szyyw-design/chrome.js").status_code == 404


def test_design_package_from_cdn(client, db, monkeypatch):
    # 页面里设计包 CSS 与 import map 都指向 CDN 的同一钉死版本（不得用 /latest/、不得留本地副本路径）
    base = f"https://design.szyyw.xyz/{app_module.DESIGN_VERSION}"
    html = client.get("/login").data.decode()
    assert f'href="{base}/tokens.css"' in html and f'href="{base}/components.css"' in html
    assert '<script type="importmap">{"imports": {"@szyyw/design/": "' + base + '/"}}</script>' in html
    assert html.index('type="importmap"') < html.index('type="module"')
    assert "/latest/" not in html and "/static/vendor/" not in html
    # DESIGN_BASE 环境变量可覆盖（本地离线开发）
    monkeypatch.setenv("DESIGN_BASE", "http://127.0.0.1:8137/")
    html = client.get("/login").data.decode()
    assert 'href="http://127.0.0.1:8137/tokens.css"' in html and '"@szyyw/design/": "http://127.0.0.1:8137/"' in html


def test_anon_data_endpoints_stay_401(client, db, sso):
    seed_task(db)
    task_id = db.list_tasks(2)[0]["id"]
    calls = [
        ("get", "/api/me", None),
        ("put", "/api/me", {"display_name": "x"}),
        ("get", "/api/users", None),
        ("post", "/api/users", {"username": "evil"}),
        ("get", "/api/users/2", None),
        ("put", "/api/users/2", {"role": "user"}),
        ("post", "/api/users/2/tasks", {"tracking_number": "AA1JP"}),
        ("post", "/api/users/2/test_push", {}),
        ("put", f"/api/tasks/{task_id}", {"label": "x"}),
        ("post", f"/api/tasks/{task_id}/archive", {}),
        ("delete", f"/api/tasks/{task_id}", None),
        ("post", "/api/admin/accounts/2/portal-user", {"portal_sub": "x"}),
        ("post", "/update_env", {"X": "1"}),
        ("get", "/remote_bark_status", None),
    ]
    for method, url, body in calls:
        resp = getattr(client, method)(url, json=body, headers=ANON)
        assert resp.status_code == 401, (method, url, resp.status_code)
    assert db.list_tasks(2)[0]["label"] == "secret-label"
    assert db.get_account_by_username("evil") is None


def test_anon_form_posts_go_to_portal_login(client, db, sso):
    seed_task(db)
    task_id = db.list_tasks(2)[0]["id"]
    for url in ("/me/tasks", f"/me/tasks/{task_id}/update", f"/me/tasks/{task_id}/delete",
                f"/me/tasks/{task_id}/archive", "/me/update", "/me/test-push"):
        resp = client.post(url, data={"tracking_number": "AB123456789JP"}, headers=ANON)
        assert resp.status_code == 302, url
        assert resp.headers["Location"].startswith("https://szyyw.xyz/login?rd="), url
    assert [t["tracking_number"] for t in db.list_tasks(2)] == ["ZZ987654321JP"]


def test_anon_login_route_goes_to_portal(client, db, sso):
    resp = client.get("/login", headers=ANON)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://szyyw.xyz/login?rd=https%3A%2F%2Fjppost.szyyw.xyz%2F"


def test_anon_socketio_rejected(db, sso):
    anon = app_module.socketio.test_client(app_module.app, headers=ANON)
    assert not anon.is_connected()


def test_identity_wins_over_anon_header(client, db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    headers = {**gate("szyoo", "admin"), "X-Portal-Anon": "1"}
    resp = client.get("/", headers=headers)
    assert resp.status_code == 200 and b"Admin Control Room" in resp.data
    assert client.get("/api/me", headers=headers).status_code == 200
    ok = app_module.socketio.test_client(app_module.app, headers=headers)
    assert ok.is_connected()
    ok.disconnect()


def test_anon_header_must_be_exactly_1(client, db, sso):
    resp = client.get("/", headers={**ANON, "X-Portal-Anon": "true"})
    assert resp.status_code == 302 and resp.headers["Location"].startswith("https://szyyw.xyz/login?rd=")


def test_no_headers_under_sso_still_redirects(client, db, sso):
    resp = client.get("/", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "jppost.szyyw.xyz"})
    assert resp.status_code == 302 and resp.headers["Location"].startswith("https://szyyw.xyz/login?rd=")


def test_forged_anon_ignored_without_sso(client, db):
    resp = client.get("/", headers={"X-Portal-Anon": "1"})
    assert resp.status_code == 302 and resp.headers["Location"] == "/login?next=/"
    assert client.get("/api/me", headers={"X-Portal-Anon": "1"}).status_code == 401
    sock = app_module.socketio.test_client(app_module.app, headers={"X-Portal-Anon": "1"})
    assert not sock.is_connected()


def test_logout_button_only_without_sso(client, db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    hank = db.create_account({"username": "hank", "password": "pw-hank", "role": "user"})
    db.set_account_portal_sub(hank["id"], "sub-hank")
    退出 = "退出</button>".encode()
    # SSO 下登出归右上角账户菜单（portal 登出），页内按钮去掉
    assert 退出 not in client.get("/", headers=gate("szyoo", "admin")).data
    assert 退出 not in client.get("/", headers=gate("hank")).data
    # /logout 路由仍可用
    assert client.post("/logout", headers=gate("hank")).headers["Location"] == "https://szyyw.xyz"
    # SSO 关闭：按钮照旧
    sso.delenv("SZYYW_SSO")
    client.post("/login", data={"username": "hank", "password": "pw-hank"})
    assert 退出 in client.get("/").data
