"""管理 API：单管理员密码登录（HMAC 签名会话），账号 CRUD、立即签到、日志查看。

安全：
- 所有接口拒绝返回明文 token；手机号/uid 一律脱敏
- 会话 cookie 签名防伪造，24h 过期
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import APIRouter, Cookie, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import config, db
from .accounts import pool

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 会话签名密钥：持久化在数据目录（重启后旧会话仍有效）；否则启动时随机生成
_SECRET_FILE = config.DATA_DIR / ".session_secret"

def _load_session_secret() -> str:
    try:
        if _SECRET_FILE.exists():
            v = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if len(v) >= 32:
                return v
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        v = secrets.token_hex(32)
        _SECRET_FILE.write_text(v, encoding="utf-8")
        try:
            os.chmod(_SECRET_FILE, 0o600)
        except OSError:
            pass
        return v
    except OSError:
        return secrets.token_hex(32)  # 数据目录不可写时退化为进程级 secret

_SESSION_SECRET = _load_session_secret()
_SESSION_TTL = 24 * 3600


def _mask_phone(p: str) -> str:
    p = str(p or "")
    if len(p) >= 7:
        return p[:3] + "*" * (len(p) - 7) + p[-4:]
    return p


# ---------------------------------------------------------------------------
# 管理员密码：DB 优先，env ADMIN_PASSWORD 仅作首次种子（登录成功后写入 DB）
# ---------------------------------------------------------------------------

_PW_SALT = "wb-admin-pw-v1"


def _pw_hash(password: str) -> str:
    return hashlib.sha256((_PW_SALT + password).encode()).hexdigest()


def _password_is_set() -> bool:
    return bool(db.get_setting("admin_password_hash")) or bool(config.ADMIN_PASSWORD)


def _check_password(password: str) -> bool:
    """校验密码。DB 有哈希则只验 DB；否则验 env（首次）并回写 DB。"""
    h = db.get_setting("admin_password_hash")
    if h:
        return hmac.compare_digest(_pw_hash(password), h)
    if config.ADMIN_PASSWORD and hmac.compare_digest(
            password.encode(), config.ADMIN_PASSWORD.encode()):
        try:
            db.set_setting("admin_password_hash", _pw_hash(password))
        except Exception:
            pass
        return True
    return False


def _set_password(new_password: str) -> None:
    db.set_setting("admin_password_hash", _pw_hash(new_password))


def _sign(payload: str) -> str:
    return hmac.new(_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _issue_cookie() -> str:
    exp = int(time.time()) + _SESSION_TTL
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return f"{body}.{_sign(body)}"


def _verify_cookie(value: str | None) -> bool:
    if not value:
        return False
    try:
        body, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(_sign(body), sig):
            return False
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
        return int(payload.get("exp", 0)) > int(time.time())
    except Exception:
        return False


def _require_admin(session: str | None = Cookie(default=None, alias="wb_admin")):
    if not _password_is_set():
        raise HTTPException(503, "管理员密码未配置（环境变量 ADMIN_PASSWORD）")
    if not _verify_cookie(session):
        raise HTTPException(401, "未登录或会话已过期")


def _masked(acc: dict) -> dict:
    payload = json.loads(acc["auth_json"])
    account = payload.get("account") or {}
    auth = payload.get("auth") or {}
    exp = int(auth.get("expiresAt") or 0)
    rexp = int(auth.get("refreshExpiresAt") or 0)
    now = int(time.time() * 1000)
    if rexp and rexp < now:
        auth_state = "dead"           # refresh 也过期，必须重新上传
    elif exp and exp < now:
        auth_state = "expired"        # access 过期，但可自动刷新
    else:
        auth_state = "ok"
    return {
        "id": acc["id"],
        "uid": str(acc["uid"])[:8] + ("…" if len(str(acc["uid"])) > 8 else ""),
        "nickname": acc["nickname"] or account.get("nickname"),
        "phone_mask": _mask_phone(account.get("phoneNumber")),
        "phone": None,
        "auth_state": auth_state,
        "enabled": bool(acc["enabled"]),
        "expires_at": exp,
        "refresh_expires_at": rexp,
        "last_signin_at": acc["last_signin_at"],
        "last_report": acc["last_signin_report"],
    }


# --------------------------------------------------------------------------- 会话
@router.post("/login")
def login(body: dict, response: Response):
    password = body.get("password") or ""
    if not _password_is_set():
        raise HTTPException(503, "管理员密码未配置（环境变量 ADMIN_PASSWORD）")
    if not _check_password(password):
        raise HTTPException(401, "密码错误")
    response.set_cookie("wb_admin", _issue_cookie(), httponly=True, samesite="lax",
                        max_age=_SESSION_TTL, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(body: dict, _: None = Depends(_require_admin)):
    """修改管理员密码：需验证旧密码，新密码长度 >= 8。改完立即生效（写 DB）。"""
    old = body.get("old_password") or ""
    new = body.get("new_password") or ""
    if not _check_password(old):
        raise HTTPException(401, "旧密码错误")
    if len(new) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    _set_password(new)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, _: None = Depends(_require_admin)):
    response.delete_cookie("wb_admin", path="/")
    return {"ok": True}


@router.get("/me")
def me(_: None = Depends(_require_admin)):
    return {"ok": True, "admin": True}


# --------------------------------------------------------------------------- 账号
@router.get("/accounts")
def list_accounts(_: None = Depends(_require_admin)):
    accs = pool.list()
    return {"accounts": [_masked(a) for a in accs]}


@router.post("/accounts")
async def add_account(request: Request, _: None = Depends(_require_admin)):
    """支持三种入口：
    1. multipart 文件上传（file 字段，workbuddy 授权 JSON）
    2. JSON body: {"json_text": "..."} 粘贴完整授权
    3. JSON body: {"access_token":..., "refresh_token":..., "uid":..., "nickname":...} 手动填写
    """
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload: UploadFile | None = form.get("file")
        if upload is None:
            raise HTTPException(400, "缺少 file 字段")
        raw = (await upload.read()).decode("utf-8", "replace")
        auth_json = raw
    else:
        body = await request.json()
        if body.get("json_text"):
            auth_json = body["json_text"]
        elif body.get("access_token") and body.get("uid"):
            now_ms = int(time.time() * 1000)
            auth = {
                "accessToken": body["access_token"],
                "refreshToken": body.get("refresh_token") or "",
                "tokenType": "Bearer",
                "expiresIn": int(body.get("expires_in") or 60 * 60 * 24),
                "domain": body.get("domain") or config.DEFAULT_DOMAIN,
                "expiresAt": now_ms + int(body.get("expires_in") or 60 * 60 * 24) * 1000,
            }
            if body.get("refresh_token"):
                ri = int(body.get("refresh_expires_in") or 90 * 24 * 3600)
                auth["refreshExpiresIn"] = ri
                auth["refreshExpiresAt"] = now_ms + ri * 1000
            account = {"uid": body["uid"], "nickname": body.get("nickname") or body["uid"][:8]}
            s = {"account": account, "auth": auth}
            auth_json = json.dumps(s, ensure_ascii=False)
        else:
            raise HTTPException(400, "无法识别的添加方式：需要 file / json_text / access_token+uid")
    try:
        fields = pool.add(auth_json)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "uid": fields["uid"]}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, _: None = Depends(_require_admin)):
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    pool.delete(account_id)
    return {"ok": True}


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, body: dict, _: None = Depends(_require_admin)):
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    if "enabled" in body:
        pool.set_enabled(account_id, bool(body["enabled"]))
    return {"ok": True}


# --------------------------------------------------------------------------- 签到
@router.post("/accounts/{account_id}/signin")
async def signin_now(account_id: int, _: None = Depends(_require_admin)):
    """立即手动签到（签到 + 成长中心），等待结果返回。"""
    from .signin import run_signin_for_account
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    try:
        out = await run_signin_for_account(account_id, kind="manual")
    except Exception as e:
        out = {"result": "ERROR", "report": f"签到执行异常：{e}"}
    return {"ok": True, "result": out}


# --------------------------------------------------------------------------- 日志
@router.get("/logs")
def all_logs(limit: int = 100, _: None = Depends(_require_admin)):
    return {"logs": db.list_logs(None, min(max(limit, 1), 500))}


@router.get("/accounts/{account_id}/logs")
def account_logs(account_id: int, limit: int = 100, _: None = Depends(_require_admin)):
    return {"logs": db.list_logs(account_id, min(max(limit, 1), 500))}


# --------------------------------------------------------------------------- 状态
@router.get("/status")
def status(_: None = Depends(_require_admin)):
    return {"stats": db.stats(), "cron": {"daily": config.SIGNIN_CRON, "poll": config.SIGNIN_POLL_CRON}}


# --------------------------------------------------------------------------- API 鉴权 / 设置
@router.get("/settings")
def settings_get(_: None = Depends(_require_admin)):
    return {
        "api_auth_enabled": db.api_auth_enabled(),
        "api_keys": db.list_api_keys(),
        "has_env_password": bool(config.ADMIN_PASSWORD),
    }


@router.put("/settings")
def settings_put(body: dict, _: None = Depends(_require_admin)):
    if "api_auth_enabled" in body:
        db.set_api_auth_enabled(bool(body["api_auth_enabled"]))
    return {"ok": True, "api_auth_enabled": db.api_auth_enabled()}


@router.post("/api-keys")
def api_key_create(body: dict, _: None = Depends(_require_admin)):
    """新增 API key：body.name 必填；body.key 可选（缺省自动生成）。仅此响应返回明文 key。"""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "名称必填")
    key = (body.get("key") or "").strip()
    if not key:
        key = secrets.token_urlsafe(32)
    if len(key) < 16:
        raise HTTPException(400, "API key 至少 16 位")
    kid = db.add_api_key(name, key)
    return {"ok": True, "id": kid, "name": name, "key": key}  # key 仅此一次返回


@router.patch("/api-keys/{key_id}")
def api_key_patch(key_id: int, body: dict, _: None = Depends(_require_admin)):
    if "enabled" in body:
        db.set_api_key_enabled(key_id, bool(body["enabled"]))
    return {"ok": True}


@router.delete("/api-keys/{key_id}")
def api_key_delete(key_id: int, _: None = Depends(_require_admin)):
    db.delete_api_key(key_id)
    return {"ok": True}