"""SZYYW_SSO 未设置：行为与接入 SSO 前一致（本地密码登录 + 自助注册），身份头一律不信。"""
from conftest import ADMIN_PASSWORD, gate


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def test_seed_matches_prod_shape(db):
    accounts = {a["username"]: a for a in db.list_accounts()}
    assert accounts["legacy-default"]["id"] == 1 and not accounts["legacy-default"]["login_enabled"]
    assert accounts["admin"]["id"] == 2 and accounts["admin"]["role"] == "admin"
    assert accounts["admin"]["portal_sub"] is None


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_unauthenticated_redirects_to_local_login(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_forged_headers_ignored_without_sso(client):
    resp = client.get("/", headers=gate("admin", "admin"))
    assert resp.status_code == 302 and resp.headers["Location"].startswith("/login")
    assert client.get("/api/me", headers=gate("admin", "admin")).status_code == 401


def test_password_login_and_logout(client):
    assert login(client, "admin", "wrong").status_code == 200
    resp = login(client, "admin", ADMIN_PASSWORD)
    assert resp.status_code == 302
    page = client.get("/")
    assert page.status_code == 200
    assert b"Admin Control Room" in page.data
    assert b"data-sso" not in page.data and b"meNewPassword" in page.data
    resp = client.post("/logout")
    assert resp.headers["Location"] == "/login"


def test_login_page_shows_register_link(client):
    page = client.get("/login")
    assert page.status_code == 200 and "去注册".encode() in page.data


def test_self_register_still_works(client, db):
    assert client.get("/register").status_code == 200
    resp = client.post(
        "/register",
        data={"register_username": "alice", "register_display_name": "Alice", "register_password": "pw-alice"},
    )
    assert resp.status_code == 302
    me = client.get("/api/me").get_json()["user"]
    assert me["username"] == "alice" and me["role"] == "user"
    portal = client.get("/")
    assert b'name="new_password"' in portal.data


def test_user_can_change_password(client, db):
    db.create_account({"username": "bob", "password": "old-pass", "role": "user"})
    login(client, "bob", "old-pass")
    client.put("/api/me", json={"new_password": "new-pass"})
    client.post("/logout")
    assert login(client, "bob", "old-pass").status_code == 200  # 失败回到表单
    assert login(client, "bob", "new-pass").status_code == 302
