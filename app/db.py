"""SQLite 存储：授权账号 + 签到日志。

安全：数据库位于数据卷（DATA_DIR），含授权 token，文件权限收紧；代码不打印 token。
"""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_con: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE NOT NULL,
    nickname TEXT DEFAULT '',
    auth_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at INTEGER DEFAULT 0,
    refresh_expires_at INTEGER DEFAULT 0,
    last_signin_at INTEGER DEFAULT 0,
    last_signin_report TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'auto',   -- auto | manual
    result TEXT DEFAULT '',
    report TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_account ON signin_logs(account_id, id);
CREATE INDEX IF NOT EXISTS idx_logs_time ON signin_logs(id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    key_hash TEXT UNIQUE NOT NULL,   -- sha256(明文 key)，不存明文
    prefix TEXT NOT NULL DEFAULT '', -- 展示用（key 前 8 位）
    enabled INTEGER NOT NULL DEFAULT 1,
    last_used_at INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    global _con
    if _con is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _con.executescript(SCHEMA)
        _con.commit()
        try:
            os.chmod(config.DB_PATH, 0o600)
        except OSError:
            pass
    return _con


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        cur = _connect().execute(sql, params)
        out = [dict(r) for r in cur.fetchall()]
    return out


def row(sql: str, params: tuple = ()) -> dict | None:
    with _lock:
        cur = _connect().execute(sql, params)
        r = cur.fetchone()
    return dict(r) if r else None


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        con = _connect()
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

def account_from_json(auth_json: str) -> dict:
    """解析并校验 workbuddy 授权 JSON（account+auth 结构），返回入库字段。

    兼容两种入口：完整官方结构 {"account":..., "auth":...}；或手动填写时
    由调用方构造同构对象。
    """
    try:
        s = json.loads(auth_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"授权 JSON 解析失败：{e}")
    if not isinstance(s, dict):
        raise ValueError("授权 JSON 必须是对象")
    auth = s.get("auth") or {}
    account = s.get("account") or {}
    token = auth.get("accessToken") or ""
    uid = account.get("uid") or ""
    if not token or not uid:
        raise ValueError("授权信息缺少必要字段：account.uid / auth.accessToken")
    now_ms = int(time.time() * 1000)
    expires_at = int(auth.get("expiresAt") or 0)
    if not expires_at and auth.get("expiresIn"):
        expires_at = now_ms + int(auth["expiresIn"]) * 1000
    refresh_expires_at = int(auth.get("refreshExpiresAt") or 0)
    if not refresh_expires_at and auth.get("refreshExpiresIn"):
        refresh_expires_at = now_ms + int(auth["refreshExpiresIn"]) * 1000
    return {
        "uid": uid,
        "nickname": account.get("nickname") or uid[:8],
        "auth_json": json.dumps(s, ensure_ascii=False),
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
    }


def add_account(auth_json: str) -> dict:
    fields = account_from_json(auth_json)
    now = int(time.time())
    execute(
        """INSERT OR REPLACE INTO accounts
           (uid, nickname, auth_json, enabled, expires_at, refresh_expires_at,
            created_at, updated_at)
           VALUES (?,?,?,1,?,?,?,?)""",
        (fields["uid"], fields["nickname"], fields["auth_json"],
         fields["expires_at"], fields["refresh_expires_at"], now, now),
    )
    return fields


def list_accounts() -> list[dict]:
    return rows("SELECT * FROM accounts ORDER BY id")


def get_account(account_id: int) -> dict | None:
    return row("SELECT * FROM accounts WHERE id=?", (account_id,))


def delete_account(account_id: int) -> bool:
    execute("DELETE FROM accounts WHERE id=?", (account_id,))
    return True


def set_enabled(account_id: int, enabled: bool) -> None:
    execute("UPDATE accounts SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, int(time.time()), account_id))


def save_account_json(account_id: int, auth_json: str, uid: str, nickname: str,
                      expires_at: int, refresh_expires_at: int) -> None:
    execute(
        """UPDATE accounts SET auth_json=?, uid=?, nickname=?, expires_at=?,
           refresh_expires_at=?, updated_at=? WHERE id=?""",
        (auth_json, uid, nickname, expires_at, refresh_expires_at,
         int(time.time()), account_id),
    )


def touch_signin(account_id: int, result: dict) -> None:
    report = str(result.get("report") or "")
    execute("UPDATE accounts SET last_signin_at=?, last_signin_report=? WHERE id=?",
            (int(time.time()), report[:500], account_id))


# ---------------------------------------------------------------------------
# signin_logs
# ---------------------------------------------------------------------------

def add_log(account_id: int, kind: str, result: dict) -> None:
    execute(
        "INSERT INTO signin_logs (account_id, kind, result, report, detail, created_at) VALUES (?,?,?,?,?,?)",
        (account_id, kind, str(result.get("result") or ""),
         str(result.get("report") or "")[:1000],
         json.dumps(result, ensure_ascii=False, default=str)[:4000],
         int(time.time())),
    )


def list_logs(account_id: int | None = None, limit: int = 100) -> list[dict]:
    if account_id:
        return rows(
            "SELECT l.*, a.nickname, a.uid FROM signin_logs l LEFT JOIN accounts a ON a.id=l.account_id "
            "WHERE l.account_id=? ORDER BY l.id DESC LIMIT ?", (account_id, limit))
    return rows(
        "SELECT l.*, a.nickname, a.uid FROM signin_logs l LEFT JOIN accounts a ON a.id=l.account_id "
        "ORDER BY l.id DESC LIMIT ?", (limit,))


def stats() -> dict:
    total = row("SELECT COUNT(*) c FROM accounts")["c"]
    enabled = row("SELECT COUNT(*) c FROM accounts WHERE enabled=1")["c"]
    expired = row("SELECT COUNT(*) c FROM accounts WHERE refresh_expires_at>0 AND refresh_expires_at<?",
                  (int(time.time() * 1000),))["c"]
    return {"total": total, "enabled": enabled, "refresh_expired": expired}


# ---------------------------------------------------------------------------
# settings（k-v）：api 鉴权开关、管理员密码哈希
# ---------------------------------------------------------------------------


def set_setting(key: str, value: str) -> None:
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


def get_setting(key: str, default: str = "") -> str:
    r = row("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


API_AUTH_KEY = "api_auth_enabled"


def api_auth_enabled() -> bool:
    """API 鉴权开关，默认开启。"""
    return get_setting(API_AUTH_KEY, "1") == "1"


def set_api_auth_enabled(on: bool) -> None:
    set_setting(API_AUTH_KEY, "1" if on else "0")


def hash_api_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(("wb-api:" + key).encode()).hexdigest()


def add_api_key(name: str, key: str) -> int:
    """新增 API key（只存哈希 + 前缀）。返回记录 id。"""
    prefix = key[:8]
    return execute(
        "INSERT INTO api_keys (name, key_hash, prefix, enabled, created_at) VALUES (?,?,?,1,?)",
        (name, hash_api_key(key), prefix, int(time.time())),
    )


def verify_api_key(key: str) -> bool:
    """校验 API key 是否有效且启用；命中后更新 last_used_at。"""
    if not key:
        return False
    r = row("SELECT id FROM api_keys WHERE key_hash=? AND enabled=1", (hash_api_key(key),))
    if not r:
        return False
    execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (int(time.time()), r["id"]))
    return True


def list_api_keys() -> list[dict]:
    return rows(
        "SELECT id, name, prefix, enabled, last_used_at, created_at FROM api_keys ORDER BY id")


def set_api_key_enabled(key_id: int, enabled: bool) -> None:
    execute("UPDATE api_keys SET enabled=? WHERE id=?", (1 if enabled else 0, key_id))


def delete_api_key(key_id: int) -> None:
    execute("DELETE FROM api_keys WHERE id=?", (key_id,))