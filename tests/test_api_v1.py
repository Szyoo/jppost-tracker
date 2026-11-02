"""机器接口 /api/v1：令牌鉴权（含伪造身份头攻击）、账号隔离、PUT 幂等、来源与墓碑语义、manage 子命令、迁移。"""
import sqlite3

import pytest

import manage
import storage
from conftest import ADMIN_PASSWORD, gate

NUM = "AB123456789JP"
NUM2 = "123456789012"


def make_user(db, username):
    return db.create_account({"username": username, "password": "pw-" + username, "role": "user"})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def alice(db):
    return make_user(db, "alice")


@pytest.fixture
def alice_token(db, alice):
    _, token = db.issue_api_caller("caller-a", "alice")
    return token


def put(client, token, number=NUM, body=None, **kwargs):
    return client.put(f"/api/v1/tracking/{number}", json=body, headers=bearer(token), **kwargs)


def find_task(db, account_id, number=NUM):
    return next((t for t in db.list_tasks(account_id, include_archived=True) if t["tracking_number"] == number), None)


# --- 鉴权：攻击面 ---

V1_REQUESTS = [
    ("get", "/api/v1/ping"),
    ("get", f"/api/v1/tracking/{NUM}"),
    ("put", f"/api/v1/tracking/{NUM}"),
    ("post", f"/api/v1/tracking/{NUM}/archive"),
    ("get", "/api/v1/no-such-endpoint"),
]


def assert_all_unauthorized(client, headers):
    for method, path in V1_REQUESTS:
        resp = getattr(client, method)(path, headers=headers)
        assert resp.status_code == 401, (method, path, resp.status_code)
        body = resp.get_json()
        assert body["error"]["code"] == "unauthorized"
        assert resp.headers["Cache-Control"] == "no-store"


ATTACKS = {
    "no_token": {},
    "wrong_token": bearer("jpt_" + "x" * 43),
    "no_prefix": bearer("not-a-token"),
    "basic_scheme": {"Authorization": "Basic YWRtaW46YWRtaW4="},
    "empty_bearer": {"Authorization": "Bearer "},
    "forged_identity_only": gate("admin", role="admin", sub="sub-admin"),
    "forged_identity_plus_bearer": {**gate("admin", role="admin", sub="sub-admin"), **bearer("anything")},
    "forged_anon": {"X-Portal-Anon": "1"},
    "forged_anon_plus_bearer": {"X-Portal-Anon": "1", **bearer("jpt_whatever")},
}


@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_attacks_rejected(client, db, alice_token, attack):
    assert_all_unauthorized(client, ATTACKS[attack])
    assert find_task(db, 1) is None


@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_attacks_rejected_under_sso(client, db, sso, alice_token, attack):
    # 让伪造头在网页侧是「有效身份」：admin 已映射 sub-admin
    admin = db.get_account_by_username("admin")
    db.set_account_portal_sub(admin["id"], "sub-admin")
    assert client.get("/api/me", headers=gate("admin", role="admin", sub="sub-admin")).status_code == 200
    assert_all_unauthorized(client, ATTACKS[attack])


def test_valid_token_works_under_sso_without_identity_headers(client, db, sso, alice_token):
    resp = client.get("/api/v1/ping", headers=bearer(alice_token))
    assert resp.status_code == 200
    assert resp.get_json() == {"caller": "caller-a", "account": "alice"}


def test_session_cookie_is_not_a_token(client, db, alice_token):
    login = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    assert login.status_code == 302
    assert client.get("/api/me").status_code == 200  # 会话本身有效
    assert_all_unauthorized(client, {})
    # 会话 + 伪造 Bearer 也不行
    assert_all_unauthorized(client, bearer("jpt_forged"))


def test_revoked_and_rotated_tokens_rejected(client, db, alice):
    _, token = db.issue_api_caller("caller-a", "alice")
    assert client.get("/api/v1/ping", headers=bearer(token)).status_code == 200
    _, new_token = db.rotate_api_caller("caller-a")
    assert_all_unauthorized(client, bearer(token))
    assert client.get("/api/v1/ping", headers=bearer(new_token)).status_code == 200
    db.revoke_api_caller("caller-a")
    assert_all_unauthorized(client, bearer(new_token))
    with pytest.raises(ValueError):
        db.rotate_api_caller("caller-a")
    # 同名重新签发：新令牌可用，旧的仍无效
    _, again = db.issue_api_caller("caller-a", "alice")
    assert client.get("/api/v1/ping", headers=bearer(again)).status_code == 200
    assert_all_unauthorized(client, bearer(new_token))


def test_token_bypasses_web_auth_only_for_v1(client, db, alice_token):
    """令牌只在 /api/v1 下有效，不能拿去访问网页侧接口。"""
    assert client.get("/api/me", headers=bearer(alice_token)).status_code == 401
    assert client.get("/api/users", headers=bearer(alice_token)).status_code == 401


def test_bearer_scheme_case_insensitive_and_no_store(client, db, alice_token):
    resp = client.get("/api/v1/ping", headers={"Authorization": f"bearer {alice_token}"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in resp.headers


def test_last_used_at_throttled(client, db, alice_token):
    assert db.list_api_callers()[0]["last_used_at"] is None
    client.get("/api/v1/ping", headers=bearer(alice_token))
    first = db.list_api_callers()[0]["last_used_at"]
    assert first
    with sqlite3.connect(storage.DB_PATH) as conn:
        conn.execute("UPDATE api_callers SET last_used_at = '2000-01-01 00:00:00'")
    client.get("/api/v1/ping", headers=bearer(alice_token))
    assert db.list_api_callers()[0]["last_used_at"] != "2000-01-01 00:00:00"
    with sqlite3.connect(storage.DB_PATH) as conn:
        conn.execute("UPDATE api_callers SET last_used_at = '2999-01-01 00:00:00'")
    client.get("/api/v1/ping", headers=bearer(alice_token))
    assert db.list_api_callers()[0]["last_used_at"] == "2999-01-01 00:00:00"  # 节流期内不写


# --- 错误格式 ---

def test_json_errors_for_404_405_and_bad_input(client, db, alice_token):
    h = bearer(alice_token)
    resp = client.get("/api/v1/nope", headers=h)
    assert resp.status_code == 404 and resp.get_json()["error"]["code"] == "not_found"
    resp = client.delete(f"/api/v1/tracking/{NUM}", headers=h)
    assert resp.status_code == 405 and resp.get_json()["error"]["code"] == "method_not_allowed"
    resp = client.get(f"/api/v1/tracking/{NUM}/archive", headers=h)
    assert resp.status_code == 405 and resp.get_json()["error"]["code"] == "method_not_allowed"

    for bad in ("abc", "AB12345678<9JP", "1" * 41, "%20%20"):
        resp = client.get(f"/api/v1/tracking/{bad}", headers=h)
        assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "invalid_tracking_number", bad

    bodies = [
        b"not json",
        b"[1, 2]",
        b'{"label": 5}',
        b'{"revive": "yes"}',
        b'{"external_ref": ["x"]}',
        b'{"unknown": 1}',
        ('{"label": "%s"}' % ("x" * 101)).encode(),
        ('{"external_ref": "%s"}' % ("x" * 201)).encode(),
    ]
    for raw in bodies:
        resp = client.put(f"/api/v1/tracking/{NUM}", data=raw, headers={**h, "Content-Type": "application/json"})
        assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "invalid_body", raw
    assert find_task(db, db.get_account_by_username("alice")["id"]) is None


def test_tracking_number_normalized(client, db, alice, alice_token):
    resp = put(client, alice_token, number="ab123456789jp")
    assert resp.status_code == 201
    assert resp.get_json()["tracking"]["tracking_number"] == NUM
    assert client.get(f"/api/v1/tracking/{NUM.lower()}", headers=bearer(alice_token)).status_code == 200


# --- PUT / GET / archive ---

def test_put_creates_then_idempotent(client, db, alice, alice_token):
    resp = put(client, alice_token, body={"label": "keyboard", "external_ref": "order-1"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["result"] == "created"
    t = body["tracking"]
    assert t["tracking_number"] == NUM and t["label"] == "keyboard" and t["external_ref"] == "order-1"
    assert t["enabled"] is True and t["archived"] is False and t["source_state"] == "active"
    assert t["last_checked_at"] is None and t["last_push_at"] is None

    resp = put(client, alice_token, body={"label": "changed"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "existing"
    assert body["tracking"]["label"] == "keyboard"  # label 只在新建时用
    assert body["tracking"]["external_ref"] == "order-1"  # 没给就保留

    resp = put(client, alice_token, body={"external_ref": "order-2"})
    assert resp.get_json()["tracking"]["external_ref"] == "order-2"
    resp = put(client, alice_token)  # 无请求体
    assert resp.status_code == 200
    assert len(db.list_tasks(alice["id"], include_archived=True)) == 1


def test_put_links_existing_user_task_without_touching_it(client, db, alice, alice_token):
    db.create_task(alice["id"], {"tracking_number": NUM, "label": "mine", "check_interval": 3600, "enabled": 0})
    resp = put(client, alice_token, body={"label": "api-label", "external_ref": "x1"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "linked"
    assert body["tracking"]["label"] == "mine" and body["tracking"]["enabled"] is False
    task = find_task(db, alice["id"])
    assert task["label"] == "mine" and task["check_interval"] == 3600 and not task["enabled"]
    assert put(client, alice_token).get_json()["result"] == "existing"


def test_get_returns_state_and_events(client, db, alice, alice_token):
    put(client, alice_token)
    task = find_task(db, alice["id"])
    for i in range(30):
        db.update_task_state(task["id"], latest_info=f"s{i}", pushed=True, push_status=f"s{i}")
    h = bearer(alice_token)
    body = client.get(f"/api/v1/tracking/{NUM}", headers=h).get_json()
    assert len(body["events"]) == 20
    assert body["events"][0]["status"] == "s29" and body["events"][0]["kind"] == "push_ok"
    t = body["tracking"]
    assert t["last_tracking_info"] == "s29" and t["last_push_at"] and t["last_checked_at"]
    assert len(client.get(f"/api/v1/tracking/{NUM}?events=5", headers=h).get_json()["events"]) == 5
    assert client.get(f"/api/v1/tracking/{NUM}?events=0", headers=h).get_json()["events"] == []
    assert len(client.get(f"/api/v1/tracking/{NUM}?events=100", headers=h).get_json()["events"]) == 30
    for bad in ("101", "-1", "x"):
        resp = client.get(f"/api/v1/tracking/{NUM}?events={bad}", headers=h)
        assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "invalid_query"


def test_get_unknown_number_404(client, db, alice_token):
    resp = client.get(f"/api/v1/tracking/{NUM}", headers=bearer(alice_token))
    assert resp.status_code == 404 and resp.get_json()["error"]["code"] == "not_found"
    resp = client.post(f"/api/v1/tracking/{NUM}/archive", headers=bearer(alice_token))
    assert resp.status_code == 404


def test_cross_account_isolation(client, db, alice, alice_token):
    bob = make_user(db, "bob")
    db.create_task(bob["id"], {"tracking_number": NUM2, "label": "bob's"})
    _, bob_token = db.issue_api_caller("caller-b", "bob")
    put(client, bob_token, number=NUM)
    h = bearer(alice_token)
    for number in (NUM, NUM2):
        assert client.get(f"/api/v1/tracking/{number}", headers=h).status_code == 404
        assert client.post(f"/api/v1/tracking/{number}/archive", headers=h).status_code == 404
    assert not find_task(db, bob["id"], NUM2)["archived"]
    assert not find_task(db, bob["id"], NUM)["archived"]
    # A 用同一单号 PUT：在 A 自己账号下另建，不碰 B 的
    assert put(client, alice_token, number=NUM2).status_code == 201
    assert find_task(db, bob["id"], NUM2)["label"] == "bob's"
    assert find_task(db, alice["id"], NUM2)["account_id"] == alice["id"]


def test_api_archive_then_put_revives(client, db, alice, alice_token):
    put(client, alice_token, body={"label": "kb"})
    h = bearer(alice_token)
    resp = client.post(f"/api/v1/tracking/{NUM}/archive", headers=h)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "archived" and body["tracking"]["archived"] is True
    assert body["tracking"]["enabled"] is False and body["tracking"]["source_state"] == "archived_by_api"
    again = client.post(f"/api/v1/tracking/{NUM}/archive", headers=h)
    assert again.status_code == 200 and again.get_json()["result"] == "already_archived"

    resp = put(client, alice_token)  # 不带 revive 也复活
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "revived"
    assert body["tracking"]["archived"] is False and body["tracking"]["enabled"] is True
    assert body["tracking"]["label"] == "kb" and body["tracking"]["source_state"] == "active"
    assert find_task(db, alice["id"])["seen_count"] == 2


def test_user_delete_leaves_tombstone_and_blocks_put(client, db, alice, alice_token):
    put(client, alice_token, body={"external_ref": "r1"})
    task = find_task(db, alice["id"])
    db.delete_task(task["id"], actor_role="user", actor_id=alice["id"])

    resp = put(client, alice_token)
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "user_deleted"
    assert find_task(db, alice["id"]) is None  # 没有被建回
    resp = client.get(f"/api/v1/tracking/{NUM}", headers=bearer(alice_token))
    assert resp.status_code == 404 and resp.get_json()["error"]["code"] == "user_deleted"
    resp = client.post(f"/api/v1/tracking/{NUM}/archive", headers=bearer(alice_token))
    assert resp.status_code == 404 and resp.get_json()["error"]["code"] == "user_deleted"

    resp = put(client, alice_token, body={"revive": True, "label": "back"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["result"] == "created" and body["tracking"]["label"] == "back"
    assert body["tracking"]["external_ref"] == "r1" and body["tracking"]["source_state"] == "active"
    assert put(client, alice_token).get_json()["result"] == "existing"


def test_user_delete_via_web_route_leaves_tombstone(client, db, alice, alice_token):
    put(client, alice_token)
    task = find_task(db, alice["id"])
    client.post("/login", data={"username": "alice", "password": "pw-alice"})
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
    client.post("/logout")
    assert put(client, alice_token).status_code == 409


def test_user_readds_after_delete_is_linked(client, db, alice, alice_token):
    put(client, alice_token)
    db.delete_task(find_task(db, alice["id"])["id"])
    db.create_task(alice["id"], {"tracking_number": NUM, "label": "again"})
    resp = put(client, alice_token)
    assert resp.status_code == 200 and resp.get_json()["result"] == "linked"
    assert resp.get_json()["tracking"]["label"] == "again"


def test_user_renumber_counts_as_delete(client, db, alice, alice_token):
    put(client, alice_token)
    task = find_task(db, alice["id"])
    db.update_task(task["id"], {"tracking_number": NUM2})
    resp = put(client, alice_token)
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "user_deleted"


def test_user_archive_blocks_put_until_revive(client, db, alice, alice_token):
    put(client, alice_token, body={"label": "kb"})
    task = find_task(db, alice["id"])
    db.update_task(task["id"], {"label": "user-renamed", "check_interval": 600})
    db.archive_task(task["id"], actor_role="user", actor_id=alice["id"])

    resp = put(client, alice_token)
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "user_archived"
    assert find_task(db, alice["id"])["archived"]
    body = client.get(f"/api/v1/tracking/{NUM}", headers=bearer(alice_token)).get_json()
    assert body["tracking"]["archived"] is True and body["tracking"]["source_state"] == "user_archived"
    # 归档接口对已归档任务幂等，且不把用户归档改成 API 归档
    assert client.post(f"/api/v1/tracking/{NUM}/archive", headers=bearer(alice_token)).get_json()["result"] == "already_archived"
    assert put(client, alice_token).status_code == 409

    resp = put(client, alice_token, body={"revive": True, "label": "ignored"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "revived" and body["tracking"]["archived"] is False
    assert body["tracking"]["label"] == "user-renamed"
    assert find_task(db, alice["id"])["check_interval"] == 600


def test_user_archive_via_update_and_unarchive_in_web(client, db, alice, alice_token):
    put(client, alice_token)
    task = find_task(db, alice["id"])
    db.update_task(task["id"], {"archived": 1})
    assert put(client, alice_token).status_code == 409
    # 用户在网页里重新添加（复活归档任务）后，来源记录跟着恢复
    db.create_task(alice["id"], {"tracking_number": NUM})
    resp = put(client, alice_token)
    assert resp.status_code == 200 and resp.get_json()["result"] == "existing"


def test_user_archived_own_task_before_api_saw_it(client, db, alice, alice_token):
    task = db.create_task(alice["id"], {"tracking_number": NUM})
    db.archive_task(task["id"])
    resp = put(client, alice_token)
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "user_archived"
    assert put(client, alice_token, body={"revive": True}).get_json()["result"] == "revived"


def test_web_create_still_rejects_duplicate_and_revives_archived(client, db, alice, alice_token):
    """网页侧 _insert_task 语义不变。"""
    put(client, alice_token)
    with pytest.raises(ValueError):
        db.create_task(alice["id"], {"tracking_number": NUM})
    task = find_task(db, alice["id"])
    db.archive_task(task["id"])
    revived = db.create_task(alice["id"], {"tracking_number": NUM, "label": "web"})
    assert revived["revived"] and revived["label"] == "web"


# --- manage.py ---

def test_manage_caller_commands(db, alice, monkeypatch, capsys):
    monkeypatch.setattr(manage, "DOTENV_PATH", "/nonexistent/.env")
    assert manage.main(["issue-caller", "svc-one", "alice"]) == 0
    out = capsys.readouterr().out
    tokens = [line for line in out.splitlines() if line.startswith("jpt_")]
    assert len(tokens) == 1
    token = tokens[0]
    assert out.count(token) == 1

    with sqlite3.connect(storage.DB_PATH) as conn:
        dump = "\n".join(conn.iterdump())
        stored = conn.execute("SELECT token_sha256 FROM api_callers WHERE name = 'svc-one'").fetchone()[0]
    assert token not in dump
    assert stored == storage.hash_api_token(token)
    assert storage.authenticate_api_caller(token)["name"] == "svc-one"

    assert manage.main(["issue-caller", "svc-one", "alice"]) == 1  # 同名未吊销
    assert manage.main(["issue-caller", "svc-two", "nobody"]) == 1
    assert manage.main(["issue-caller", "Bad Name", "alice"]) == 1
    capsys.readouterr()

    assert manage.main(["list-callers"]) == 0
    listing = capsys.readouterr().out
    assert "svc-one" in listing and "alice" in listing and "active" in listing
    assert "jpt_" not in listing and stored not in listing

    assert manage.main(["rotate-caller", "svc-one"]) == 0
    out = capsys.readouterr().out
    new_tokens = [line for line in out.splitlines() if line.startswith("jpt_")]
    assert len(new_tokens) == 1 and new_tokens[0] != token
    assert storage.authenticate_api_caller(token) is None
    assert storage.authenticate_api_caller(new_tokens[0])["name"] == "svc-one"

    assert manage.main(["revoke-caller", "svc-one"]) == 0
    assert storage.authenticate_api_caller(new_tokens[0]) is None
    assert manage.main(["revoke-caller", "svc-one"]) == 0  # 幂等
    assert manage.main(["rotate-caller", "svc-one"]) == 1
    assert manage.main(["revoke-caller", "missing"]) == 1
    capsys.readouterr()
    manage.main(["list-callers"])
    assert "revoked" in capsys.readouterr().out

    make_user(db, "carol")
    assert manage.main(["issue-caller", "svc-one", "carol"]) == 1  # 已吊销的同名换账号要换名
    assert manage.main(["issue-caller", "svc-one", "alice"]) == 0  # 同账号可重新签发


# --- 迁移 ---

def test_existing_db_upgrade_keeps_data(db, alice):
    """模拟上线前的库：先删掉新表与触发器，放入任务与事件，再跑 ensure_storage。"""
    task = db.create_task(alice["id"], {"tracking_number": NUM, "label": "old"})
    db.update_task_state(task["id"], latest_info="s1", pushed=True, push_status="s1")
    archived = db.create_task(alice["id"], {"tracking_number": NUM2})
    db.archive_task(archived["id"])
    with sqlite3.connect(storage.DB_PATH) as conn:
        for trigger in ("trg_api_refs_task_delete", "trg_api_refs_task_renumber",
                        "trg_api_refs_task_archive", "trg_api_refs_task_unarchive"):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DROP TABLE api_refs")
        conn.execute("DROP TABLE api_callers")
        before_tasks = conn.execute("SELECT * FROM tracking_tasks ORDER BY id").fetchall()
        before_events = conn.execute("SELECT * FROM task_events ORDER BY id").fetchall()
        # 管理员行的 updated_at 每次启动都会被 _sync_admin_account 刷新，不参与比较
        before_accounts = conn.execute("SELECT id, username, password_hash, portal_sub FROM accounts ORDER BY id").fetchall()
    assert before_tasks and before_events

    storage.ensure_storage("/nonexistent/.env")
    storage.ensure_storage("/nonexistent/.env")  # 幂等

    with sqlite3.connect(storage.DB_PATH) as conn:
        assert conn.execute("SELECT * FROM tracking_tasks ORDER BY id").fetchall() == before_tasks
        assert conn.execute("SELECT * FROM task_events ORDER BY id").fetchall() == before_events
        assert conn.execute(
            "SELECT id, username, password_hash, portal_sub FROM accounts ORDER BY id"
        ).fetchall() == before_accounts
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"api_callers", "api_refs"} <= tables
    assert len([t for t in triggers if t.startswith("trg_api_refs_")]) == 4


def test_ref_state_check_constraint(db, alice, alice_token):
    caller = db.list_api_callers()[0]
    with sqlite3.connect(storage.DB_PATH) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO api_refs (caller_id, account_id, tracking_number, state, created_at, updated_at) "
            "VALUES (?, ?, 'X', 'bogus', '', '')",
            (caller["id"], alice["id"]),
        )
