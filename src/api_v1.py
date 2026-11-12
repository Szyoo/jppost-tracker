"""机器接口 /api/v1：给外部调用方用的令牌鉴权接口。

鉴权只认 `Authorization: Bearer <令牌>`，令牌由 `manage.py issue-caller` 签发并绑定一个本地账号。
完全不看门户身份头（X-User / X-Role / X-Portal-Sub / X-Portal-Anon）和会话 cookie：
反代对带 Authorization 的请求不做门户校验，同网调用方也可能直连应用端口，
所以这里的令牌校验是唯一防线。所有响应（含错误）都是 JSON，并带 Cache-Control: no-store。
"""
import json

from flask import Blueprint, g, jsonify, request
from werkzeug.exceptions import HTTPException

from storage import (
    EVENT_PAGE_MAX,
    ApiConflict,
    api_archive_tracking,
    api_get_tracking,
    api_put_tracking,
    authenticate_api_caller,
    list_task_events,
    normalize_api_tracking_number,
    normalize_milestones,
    task_forecast,
)
from forecast import CHANNEL_MAX_LENGTH

API_V1_PREFIX = "/api/v1"
BLUEPRINT_NAME = "api_v1"

LABEL_MAX_LENGTH = 100
EXTERNAL_REF_MAX_LENGTH = 200
BODY_MAX_BYTES = 8 * 1024
EVENTS_DEFAULT = 20

# 错误码穷举：调用方按 code 分支，message 只给人看
ERROR_CODES = {
    "unauthorized": 401,
    "invalid_tracking_number": 400,
    "invalid_body": 400,
    "invalid_query": 400,
    "not_found": 404,
    "user_deleted": 409,
    "user_archived": 409,
    "method_not_allowed": 405,
    "internal_error": 500,
}

bp = Blueprint(BLUEPRINT_NAME, __name__, url_prefix=API_V1_PREFIX)


def is_api_v1_path(path: str) -> bool:
    return path == API_V1_PREFIX or path.startswith(API_V1_PREFIX + "/")


def api_error(code: str, message: str, status: int | None = None):
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status or ERROR_CODES[code]
    if response.status_code == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="api"'
    return response


def _bearer_token():
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def authenticate_request():
    """app 级 before_request：凡是 /api/v1 路径都在这里校验令牌（含未匹配到路由的路径）。
    通过后把调用方放进 g.api_caller；视图只用它，不碰 session 与身份头。"""
    if not is_api_v1_path(request.path):
        return None
    g.api_caller = None
    # 防御：前缀下若匹配到了别的蓝图的视图，一律当不存在
    if request.endpoint is not None and request.blueprint != BLUEPRINT_NAME:
        return api_error("not_found", "接口不存在。")
    caller = authenticate_api_caller(_bearer_token())
    if caller is None:
        return api_error("unauthorized", "缺少令牌、令牌无效或已吊销。")
    g.api_caller = caller
    return None


def add_no_store(response):
    if is_api_v1_path(request.path):
        response.headers["Cache-Control"] = "no-store"
    return response


def handle_http_error(exc: HTTPException):
    """/api/v1 下的 404 / 405 等也返回统一 JSON；其他路径保持原样。"""
    if not is_api_v1_path(request.path):
        return exc
    if exc.code == 404:
        return api_error("not_found", "接口不存在。")
    if exc.code == 405:
        return api_error("method_not_allowed", "不支持该请求方法。")
    if exc.code is not None and exc.code < 500:
        return api_error("invalid_body", exc.description or "请求无效。", exc.code)
    return api_error("internal_error", "服务器内部错误。")


def handle_unexpected_error(exc: Exception):
    """只挂在蓝图上：v1 视图里的意外异常也回 JSON，不泄露堆栈。"""
    if isinstance(exc, HTTPException):
        return handle_http_error(exc)
    return api_error("internal_error", "服务器内部错误。")


bp.register_error_handler(Exception, handle_unexpected_error)


def register(app):
    """挂到 app 上。app 的 require_auth 对 /api/v1 放行，令牌校验在这里注册的 before_request 里做。"""
    app.register_blueprint(bp)
    app.before_request(authenticate_request)
    app.after_request(add_no_store)
    app.register_error_handler(HTTPException, handle_http_error)


# --- 请求解析 ---

class BadRequest(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _tracking_number(raw):
    try:
        return normalize_api_tracking_number(raw)
    except ValueError as exc:
        raise BadRequest("invalid_tracking_number", str(exc)) from None


def _json_body(allowed: dict) -> dict:
    """请求体可省略；给了就必须是 JSON 对象，字段只能是 allowed 里的，类型要对。null 视为没给。"""
    raw = request.get_data(cache=True)
    if len(raw) > BODY_MAX_BYTES:
        raise BadRequest("invalid_body", f"请求体不能超过 {BODY_MAX_BYTES} 字节。")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("invalid_body", "请求体必须是 JSON。") from None
    if not isinstance(data, dict):
        raise BadRequest("invalid_body", "请求体必须是 JSON 对象。")
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise BadRequest("invalid_body", f"不支持的字段：{', '.join(unknown)}")
    result = {}
    for key, (kind, max_length) in allowed.items():
        value = data.get(key)
        if value is None:
            continue
        if kind is bool:
            if not isinstance(value, bool):
                raise BadRequest("invalid_body", f"{key} 必须是布尔值。")
        elif kind is dict:
            if not isinstance(value, dict):
                raise BadRequest("invalid_body", f"{key} 必须是 JSON 对象。")
        elif kind is str:
            if not isinstance(value, str):
                raise BadRequest("invalid_body", f"{key} 必须是字符串。")
            value = value.strip()
            if len(value) > max_length:
                raise BadRequest("invalid_body", f"{key} 不能超过 {max_length} 个字符。")
        result[key] = value
    return result


def _milestones(value):
    """milestones：key 只能是 ordered / packed / handed_over，值是时间字符串（YYYY-MM-DD[ HH:MM[:SS]]
    或带时区的 ISO 8601，换算成日本时间）。不允许空值——调用方不能删除节点。"""
    if value is None:
        return None
    try:
        return normalize_milestones(value, allow_tz=True, allow_empty=False)
    except ValueError as exc:
        raise BadRequest("invalid_body", f"milestones：{exc}") from None


def _events_limit() -> int:
    raw = request.args.get("events")
    if raw is None or raw.strip() == "":
        return EVENTS_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        raise BadRequest("invalid_query", "events 必须是整数。") from None
    if value < 0 or value > EVENT_PAGE_MAX:
        raise BadRequest("invalid_query", f"events 需在 0–{EVENT_PAGE_MAX} 之间。")
    return value


@bp.errorhandler(BadRequest)
def _bad_request(exc: BadRequest):
    return api_error(exc.code, exc.message)


@bp.errorhandler(ApiConflict)
def _conflict(exc: ApiConflict):
    return api_error(exc.code, exc.message)


# --- 响应表示 ---

def _or_none(value):
    return value if value not in ("", None) else None


def tracking_view(task: dict) -> dict:
    ref = task.get("api_ref")
    return {
        "tracking_number": task["tracking_number"],
        "label": task["label"],
        "channel": _or_none(task.get("channel")),
        "external_ref": _or_none(ref["external_ref"]) if ref else None,
        "source_state": ref["state"] if ref else None,
        "enabled": task["enabled"],
        "archived": task["archived"],
        "last_tracking_info": _or_none(task["last_tracking_info"]),
        "last_checked_at": _or_none(task["last_checked_at"]),
        "last_error": _or_none(task["last_error"]),
        "last_push_at": _or_none(task["last_push_at"]),
        "tracking_url": task["tracking_url"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }


def event_view(event: dict) -> dict:
    return {
        "id": event["id"],
        "kind": event["kind"],
        "status": event["status"],
        "detail": event["detail"],
        "tracking_number": event["tracking_number"],
        "created_at": event["created_at"],
    }


def _not_found(tombstone):
    if tombstone == "user_deleted":
        return api_error("user_deleted", "该单号的任务已被用户在网页里删除。", 404)
    return api_error("not_found", "该账号下没有这个单号。")


# --- 接口 ---

@bp.route("/ping", methods=["GET"])
def ping():
    caller = g.api_caller
    return jsonify({"caller": caller["name"], "account": caller["account_username"]})


@bp.route("/tracking/<number>", methods=["PUT"])
def put_tracking(number):
    caller = g.api_caller
    number = _tracking_number(number)
    body = _json_body({
        "label": (str, LABEL_MAX_LENGTH),
        "external_ref": (str, EXTERNAL_REF_MAX_LENGTH),
        "revive": (bool, None),
        "channel": (str, CHANNEL_MAX_LENGTH),
        "milestones": (dict, None),
    })
    result, task = api_put_tracking(
        caller["id"],
        caller["account_id"],
        number,
        label=body.get("label"),
        external_ref=body.get("external_ref"),
        revive=bool(body.get("revive")),
        channel=body.get("channel") or None,
        milestones=_milestones(body.get("milestones")),
    )
    status = 201 if result == "created" else 200
    return jsonify({"result": result, "tracking": tracking_view(task)}), status


@bp.route("/tracking/<number>", methods=["GET"])
def get_tracking(number):
    caller = g.api_caller
    number = _tracking_number(number)
    limit = _events_limit()
    task, tombstone = api_get_tracking(caller["id"], caller["account_id"], number)
    if task is None:
        return _not_found(tombstone)
    events = list_task_events(task["id"], limit=limit)[0] if limit > 0 else []
    return jsonify({"tracking": tracking_view(task), "events": [event_view(e) for e in events]})


@bp.route("/tracking/<number>/archive", methods=["POST"])
def archive_tracking(number):
    caller = g.api_caller
    number = _tracking_number(number)
    result, task = api_archive_tracking(caller["id"], caller["account_id"], number)
    if result is None:
        return _not_found(task)
    return jsonify({"result": result, "tracking": tracking_view(task)})


@bp.route("/tracking/<number>/forecast", methods=["GET"])
def get_tracking_forecast(number):
    """到达预测，结构与网页的 GET /api/tasks/<id>/forecast 相同（见 forecast.build_forecast）。"""
    caller = g.api_caller
    number = _tracking_number(number)
    task, tombstone = api_get_tracking(caller["id"], caller["account_id"], number)
    if task is None:
        return _not_found(tombstone)
    return jsonify({"tracking_number": task["tracking_number"], "forecast": task_forecast(task["id"])})
