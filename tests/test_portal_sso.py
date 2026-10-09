"""SZYYW_SSO=1：身份来自门卫注入的 X-Portal-Sub / X-User / X-Role，按 portal_sub 映射本地账号。"""
import sqlite3

import pytest

import app as app_module
import manage
from conftest import ADMIN_PASSWORD, gate


def make_user(db, username, **extra):
    data = {"username": username, "password": "pw-" + username, "role": "user"}
    data.update(extra)
    return db.create_account(data)


# --- 账号解析 ---

def test_mapped_user_resolves_to_mapped_row_and_owns_tasks(client, db, sso):
    alice = make_user(db, "alice")
    db.set_account_portal_sub(alice["id"], "sub-Alice")
    headers = gate("Alice")

    resp = client.get("/", headers=headers)
    assert resp.status_code == 200
    assert b"My Delivery Space" in resp.data

    me = client.get("/api/me", headers=headers).get_json()["user"]
    assert me["id"] == alice["id"]

    resp = client.post("/me/tasks", data={"tracking_number": "AB123456789JP", "enabled": "on"}, headers=headers)
    assert resp.status_code == 302
    tasks = db.list_tasks(alice["id"])
    assert [t["tracking_number"] for t in tasks] == ["AB123456789JP"]
    assert tasks[0]["account_id"] == alice["id"]


def test_unmapped_without_autocreate_is_403(client, db, sso):
    before = len(db.list_accounts())
    resp = client.get("/", headers=gate("stranger"))
    assert resp.status_code == 403
    assert "此账号尚未在 JPPost 开通，请联系管理员".encode() in resp.data
    api = client.get("/api/me", headers=gate("stranger"))
    assert api.status_code == 403 and "尚未在 JPPost 开通" in api.get_json()["message"]
    assert len(db.list_accounts()) == before


def test_autocreate_creates_row(client, db, sso):
    sso.setenv("SZYYW_SSO_AUTOCREATE", "1")
    resp = client.get("/api/me", headers=gate("newbie"))
    assert resp.status_code == 200
    me = resp.get_json()["user"]
    assert me["username"] == "newbie" and me["portal_sub"] == "sub-newbie"
    assert me["role"] == "user" and me["login_enabled"] and me["has_password"]
    # 再来一次不重复建号
    again = client.get("/api/me", headers=gate("newbie")).get_json()["user"]
    assert again["id"] == me["id"]


def test_autocreate_role_and_username_derivation(client, db, sso):
    sso.setenv("SZYYW_SSO_AUTOCREATE", "1")
    # portal 允许大写/两位长度，本地用户名规则不允许 → 规整后建号，portal_sub 记 X-Portal-Sub
    me = client.get("/api/me", headers=gate("Xy", "admin")).get_json()["user"]
    assert me["portal_sub"] == "sub-Xy"
    assert me["username"] == "xy-portal"
    assert me["role"] == "admin"


def test_autocreate_avoids_username_held_by_other_mapping(client, db, sso):
    sso.setenv("SZYYW_SSO_AUTOCREATE", "1")
    carol = make_user(db, "carol")
    db.set_account_portal_sub(carol["id"], "sub-someone-else")
    me = client.get("/api/me", headers=gate("carol")).get_json()["user"]
    assert me["id"] != carol["id"]
    assert me["username"] == "carol-2" and me["portal_sub"] == "sub-carol"


def test_username_match_with_null_portal_sub_is_adopted(client, db, sso):
    dave = make_user(db, "dave")
    resp = client.get("/api/me", headers=gate("dave", sub="3f2a-dave-uuid"))
    assert resp.status_code == 200
    assert resp.get_json()["user"]["id"] == dave["id"]
    assert db.get_account(dave["id"])["portal_sub"] == "3f2a-dave-uuid"  # 填的是 sub，不是用户名
    # 认领后只认 sub：portal 里改名也还是这个账号，本地 username 不跟着改
    renamed = client.get("/api/me", headers=gate("david", sub="3f2a-dave-uuid")).get_json()["user"]
    assert renamed["id"] == dave["id"] and renamed["username"] == "dave"


def test_sub_match_wins_over_username_match(client, db, sso):
    alice = make_user(db, "alice")
    bob = make_user(db, "bob")
    db.set_account_portal_sub(alice["id"], "sub-alice")
    # alice 在 portal 改名成 "bob"（恰好是另一个未映射本地账号的用户名）：仍按 sub 进 alice
    resp = client.get("/", headers=gate("bob", sub="sub-alice"))
    assert resp.status_code == 200
    me = client.get("/api/me", headers=gate("bob", sub="sub-alice")).get_json()["user"]
    assert me["id"] == alice["id"]
    assert db.get_account(bob["id"])["portal_sub"] is None  # 没被认领
    # SSO 下页头不再自写用户名 / 角色徽章（右上角账户菜单从 portal 取当前用户名），也没有本地「退出」
    assert b'class="current-user"' not in resp.data and b'action="/logout"' not in resp.data


def test_local_login_header_shows_user(client, db):
    """非 SSO：页头仍显示当前用户与退出按钮（没有账户菜单可替代）。"""
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    html = client.get("/").data
    assert b'class="current-user"' in html and b'action="/logout"' in html


def test_missing_sub_is_logged_out(client, db, sso):
    sso.setenv("SZYYW_SSO_AUTOCREATE", "1")
    before = len(db.list_accounts())
    headers = gate("admin", "admin", sub=None, **{"X-Forwarded-Proto": "https", "X-Forwarded-Host": "jppost.szyyw.xyz"})
    resp = client.get("/", headers=headers)
    assert resp.status_code == 302 and resp.headers["Location"].startswith("https://szyyw.xyz/login?rd=")
    assert client.get("/api/me", headers=headers).status_code == 401
    assert client.get("/api/me", headers=gate("admin", "admin", sub="  ")).status_code == 401
    # 既没认领同名 admin，也没自动建号
    assert db.get_account(2)["portal_sub"] is None and len(db.list_accounts()) == before


def test_username_match_with_other_portal_sub_is_not_adopted(client, db, sso):
    erin = make_user(db, "erin")
    db.set_account_portal_sub(erin["id"], "sub-erin-portal")
    resp = client.get("/api/me", headers=gate("erin", sub="sub-other"))  # 同名但 sub 不同
    assert resp.status_code == 403
    assert db.get_account(erin["id"])["portal_sub"] == "sub-erin-portal"


def test_adoption_is_case_sensitive(client, db, sso):
    make_user(db, "frank")
    assert client.get("/api/me", headers=gate("Frank")).status_code == 403
    assert db.get_account_by_username("frank")["portal_sub"] is None


def test_login_disabled_account_is_403(client, db, sso):
    # 线上的 legacy-default 就是停用登录的账号
    db.set_account_portal_sub(1, "sub-legacy")
    resp = client.get("/", headers=gate("legacy"))
    assert resp.status_code == 403 and "已在 JPPost 停用".encode() in resp.data


# --- 角色优先级 ---

def test_x_role_admin_grants_admin_without_touching_stored_role(client, db, sso):
    gina = make_user(db, "gina")
    db.set_account_portal_sub(gina["id"], "sub-gina")
    resp = client.get("/", headers=gate("gina", "admin"))
    assert b"Admin Control Room" in resp.data
    assert client.get("/api/users", headers=gate("gina", "admin")).status_code == 200
    assert db.get_account(gina["id"])["role"] == "user"


def test_x_role_user_demotes_stored_admin(client, db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    resp = client.get("/", headers=gate("szyoo", "user"))
    assert resp.status_code == 200 and b"My Delivery Space" in resp.data
    assert client.get("/api/users", headers=gate("szyoo", "user")).status_code == 403
    assert db.get_account(2)["role"] == "admin"
    assert client.get("/api/users", headers=gate("szyoo", "admin")).status_code == 200


# --- 路由 ---

def test_login_redirects_to_portal_with_rd(client, db, sso):
    resp = client.get(
        "/login?next=/foo",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "jppost.szyyw.xyz"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://szyyw.xyz/login?rd=https%3A%2F%2Fjppost.szyyw.xyz%2Ffoo"


def test_login_honours_portal_origin(client, db, sso):
    sso.setenv("PORTAL_ORIGIN", "https://portal.example/")
    resp = client.get("/login", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "jppost.szyyw.xyz"})
    assert resp.headers["Location"] == "https://portal.example/login?rd=https%3A%2F%2Fjppost.szyyw.xyz%2F"


def test_login_with_identity_goes_straight_in(client, db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    resp = client.get("/login?next=/", headers=gate("szyoo", "admin"))
    assert resp.status_code == 302 and resp.headers["Location"] == "/"


def test_no_identity_browser_goes_to_portal_and_api_gets_401(client, db, sso):
    resp = client.get("/", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "jppost.szyyw.xyz"})
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://szyyw.xyz/login?rd=https%3A%2F%2Fjppost.szyyw.xyz%2F")
    assert client.get("/api/me").status_code == 401


def test_local_session_cookie_is_ignored_under_sso(client, db, sso):
    with client.session_transaction() as s:
        s["account_id"] = 2
    assert client.get("/api/me").status_code == 401


def test_register_is_404(client, db, sso):
    assert client.get("/register").status_code == 404
    assert client.post("/register", data={"register_username": "x", "register_password": "y"}).status_code == 404
    assert client.get("/register", headers=gate("anyone")).status_code == 404


def test_healthz_under_sso(client, db, sso):
    assert client.get("/healthz").status_code == 200


def test_logout_goes_to_portal(client, db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    resp = client.post("/logout", headers=gate("szyoo", "admin"))
    assert resp.status_code == 302 and resp.headers["Location"] == "https://szyyw.xyz"


# --- 改密入口与 UI 标记 ---

def test_password_ui_hidden_and_switcher_flag(client, db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    admin_page = client.get("/", headers=gate("szyoo", "admin")).data
    assert b'data-sso="1" data-portal="https://szyyw.xyz"' in admin_page
    assert b"meNewPassword" not in admin_page and b"userNewPassword" not in admin_page
    assert b"createPassword" not in admin_page and b"userPortalSub" in admin_page
    hank = make_user(db, "hank")
    db.set_account_portal_sub(hank["id"], "sub-hank")
    user_page = client.get("/", headers=gate("hank")).data
    assert b'name="new_password"' not in user_page


def test_password_fields_ignored_under_sso(client, db, sso):
    ivy = make_user(db, "ivy")
    db.set_account_portal_sub(ivy["id"], "sub-ivy")
    before = db.get_account_by_username("ivy", include_secret=True)["password_hash"]
    client.put("/api/me", json={"new_password": "hijack", "display_name": "Ivy"}, headers=gate("ivy"))
    client.post("/me/update", data={"new_password": "hijack2", "display_name": "Ivy2"}, headers=gate("ivy"))
    after = db.get_account_by_username("ivy", include_secret=True)
    assert after["password_hash"] == before and after["display_name"] == "Ivy2"


def test_user_cannot_escalate_via_update_me(client, db, sso):
    jack = make_user(db, "jack")
    db.set_account_portal_sub(jack["id"], "sub-jack")
    client.put("/api/me", json={"role": "admin"}, headers=gate("jack"))
    assert db.get_account(jack["id"])["role"] == "user"


# --- 管理端映射接口 ---

def test_admin_portal_sub_endpoint(client, db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    admin = gate("szyoo", "admin")
    kim = make_user(db, "kim")
    url = f"/api/admin/accounts/{kim['id']}/portal-user"

    resp = client.post(url, json={"portal_sub": "sub-Kim"}, headers=admin)
    assert resp.status_code == 200 and resp.get_json()["user"]["portal_sub"] == "sub-Kim"
    listed = {u["username"]: u for u in client.get("/api/users", headers=admin).get_json()["users"]}
    assert listed["kim"]["portal_sub"] == "sub-Kim"

    # 同一 portal ID 不能映射到第二个账号
    conflict = client.post("/api/admin/accounts/1/portal-user", json={"portal_sub": "sub-Kim"}, headers=admin)
    assert conflict.status_code == 400

    cleared = client.post(url, json={"portal_sub": None}, headers=admin)
    assert cleared.status_code == 200 and cleared.get_json()["user"]["portal_sub"] is None
    assert client.post("/api/admin/accounts/999/portal-user", json={"portal_sub": "z"}, headers=admin).status_code == 404
    assert client.post(url, json={}, headers=admin).status_code == 400

    # 非 admin（X-Role=user）不能改映射
    db.set_account_portal_sub(kim["id"], "sub-Kim")
    assert client.post(url, json={"portal_sub": "x"}, headers=gate("Kim")).status_code == 403


def test_admin_create_user_under_sso_needs_no_password(client, db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    resp = client.post(
        "/api/users",
        json={"username": "leo", "display_name": "Leo", "role": "user", "login_enabled": True, "portal_sub": "sub-Leo"},
        headers=gate("szyoo", "admin"),
    )
    assert resp.status_code == 200
    user = resp.get_json()["user"]
    assert user["portal_sub"] == "sub-Leo" and user["has_password"]


# --- Socket.IO：握手请求经过门卫，处理器从握手的身份头读身份 ---

def test_socketio_identity_from_headers(db, sso):
    db.set_account_portal_sub(2, "sub-szyoo")
    ok = app_module.socketio.test_client(app_module.app, headers=gate("szyoo", "admin"))
    assert ok.is_connected()
    ok.disconnect()
    denied = app_module.socketio.test_client(app_module.app, headers=gate("szyoo", "user"))
    assert not denied.is_connected()
    anonymous = app_module.socketio.test_client(app_module.app)
    assert not anonymous.is_connected()


def test_socketio_forged_headers_ignored_without_sso(db):
    db.set_account_portal_sub(2, "sub-szyoo")
    forged = app_module.socketio.test_client(app_module.app, headers=gate("szyoo", "admin"))
    assert not forged.is_connected()


# --- 迁移与 CLI ---

def test_migration_adds_column_keeps_ids(tmp_path, monkeypatch):
    import storage
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    storage._ensure_accounts_schema(conn)
    storage._ensure_tasks_schema(conn)
    conn.execute(
        "INSERT INTO accounts (id, username, role, login_enabled, created_at, updated_at) VALUES (7, 'old', 'user', 0, 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO tracking_tasks (account_id, tracking_number, first_seen_at, created_at, updated_at) VALUES (7, 'N1', 'x', 'x', 'x')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))
    storage.ensure_storage(str(tmp_path / "missing.env"))
    storage.ensure_storage(str(tmp_path / "missing.env"))  # 幂等
    old = storage.get_account(7)
    assert old["portal_sub"] is None and old["task_count"] == 1
    with sqlite3.connect(db_path) as c:
        indexes = [r[1] for r in c.execute("PRAGMA index_list(accounts)")]
    assert "idx_accounts_portal_sub" in indexes


def test_cli_map_and_unmap(db, capsys, monkeypatch):
    monkeypatch.setattr(manage, "DOTENV_PATH", "/nonexistent/.env")
    sub = "3f2a9c1e-0000-4000-8000-000000000002"
    assert manage.main(["map-account", "admin", sub]) == 0
    assert db.get_account(2)["portal_sub"] == sub
    assert manage.main(["map-account", "legacy-default", sub]) == 1  # 冲突
    assert manage.main(["map-account", "nobody", "x"]) == 1
    assert manage.main(["list-accounts"]) == 0
    assert sub in capsys.readouterr().out
    assert manage.main(["unmap-account", "admin"]) == 0
    assert db.get_account(2)["portal_sub"] is None
