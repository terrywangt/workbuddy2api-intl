"""账号池：多账号存储、轮询调度、refreshToken 自动刷新（复用 converter 的刷新端点）。"""
import json
import threading
import time

import httpx

from . import config, db

# 提前 60s 判定 accessToken 过期
_EXPIRE_MARGIN_MS = 60_000


class NoAvailableAccount(Exception):
    pass


class AccountPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._rr = 0  # round-robin 指针

    # ------------------------------------------------------------------ CRUD
    def add(self, auth_json: str) -> dict:
        return db.add_account(auth_json)

    def delete(self, account_id: int) -> None:
        db.delete_account(account_id)

    def set_enabled(self, account_id: int, enabled: bool) -> None:
        db.set_enabled(account_id, enabled)

    def list(self) -> list[dict]:
        return db.list_accounts()

    def get(self, account_id: int) -> dict | None:
        return db.get_account(account_id)

    # ------------------------------------------------------------- 解析/头
    def session(self, account: dict) -> dict:
        """解析账号授权的完整 session（workbuddy 官方结构）。"""
        try:
            return json.loads(account["auth_json"])
        except (json.JSONDecodeError, KeyError) as e:
            raise ValueError(f"账号 {account.get('uid','?')} 授权数据损坏：{e}")

    def build_headers(self, account: dict, auth: dict | None = None) -> dict:
        s = self.session(account)
        auth = auth if auth is not None else (s.get("auth") or {})
        acct = s.get("account") or {}
        domain = auth.get("domain") or config.DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": acct.get("uid", ""),
            "X-Domain": domain,
            "User-Agent": config.USER_AGENT,
        }
        if acct.get("enterpriseId"):
            h["X-Enterprise-Id"] = acct["enterpriseId"]
            h["X-Tenant-Id"] = acct["enterpriseId"]
        if auth.get("endpoint"):
            h["X-Endpoint"] = auth["endpoint"]
        return h

    def endpoint(self, account: dict) -> str:
        s = self.session(account)
        return ((s.get("auth") or {}).get("endpoint") or config.BACKEND).rstrip("/")

    def _is_token_expired(self, auth: dict) -> bool:
        exp = int(auth.get("expiresAt") or 0)
        return bool(exp) and time.time() * 1000 >= exp - _EXPIRE_MARGIN_MS

    # ------------------------------------------------------------- 刷新
    def refresh(self, account: dict) -> dict:
        """用 refreshToken 刷新 accessToken，写回数据库。失败抛 RuntimeError。"""
        s = self.session(account)
        auth = s.get("auth") or {}
        if not auth.get("refreshToken"):
            raise RuntimeError("该账号没有 refreshToken，无法自动刷新，请重新上传授权")
        if not auth.get("refreshExpiresAt") or int(auth.get("refreshExpiresAt") or 0) < time.time() * 1000:
            raise RuntimeError("refreshToken 已过期，请重新上传授权")
        headers = self.build_headers(account, auth)
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{self.endpoint(account)}/v2/plugin/auth/token/refresh"
        try:
            r = httpx.post(url, headers=headers, json={}, timeout=15)
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + int(new_auth["expiresIn"]) * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + int(new_auth["refreshExpiresIn"]) * 1000
        s["auth"] = new_auth
        db.save_account_json(
            account["id"], json.dumps(s, ensure_ascii=False),
            (s.get("account") or {}).get("uid", account["uid"]),
            (s.get("account") or {}).get("nickname", account["nickname"]),
            int(new_auth.get("expiresAt") or 0),
            int(new_auth.get("refreshExpiresAt") or 0),
        )
        return new_auth

    def current_headers(self, account: dict) -> dict:
        """返回可用 headers；accessToken 过期时先自动刷新。"""
        s = self.session(account)
        auth = s.get("auth") or {}
        if self._is_token_expired(auth):
            with self._lock:
                s = self.session(account)  # 重读，防并发刷新
                auth = s.get("auth") or {}
                if self._is_token_expired(auth):
                    auth = self.refresh(account)
        return self.build_headers(account, auth)

    # ------------------------------------------------------------- 轮询
    def acquire(self) -> dict:
        """轮询选下一个可用账号，返回 {id, uid, nickname, account}；无可用账号抛 NoAvailableAccount。"""
        with self._lock:
            accounts = [a for a in db.list_accounts() if a["enabled"]]
            if not accounts:
                raise NoAvailableAccount("没有已启用的授权账号")
            n = len(accounts)
            for i in range(n):
                idx = (self._rr + i) % n
                acc = accounts[idx]
                s = self.session(acc)
                auth = s.get("auth") or {}
                rexp = int(auth.get("refreshExpiresAt") or 0)
                if rexp and rexp < time.time() * 1000:
                    continue  # refreshToken 过期，跳号
                self._rr = (idx + 1) % n
                try:
                    headers = self.current_headers(acc)
                except RuntimeError:
                    continue  # 刷新失败，换下一个
                return {"id": acc["id"], "uid": acc["uid"], "nickname": acc["nickname"],
                        "account": acc, "headers": headers}
            raise NoAvailableAccount("所有启用账号均不可用（refreshToken 过期或刷新失败）")

    def acquire_by_id(self, account_id: int) -> dict:
        acc = db.get_account(account_id)
        if not acc:
            raise NoAvailableAccount("账号不存在")
        if not acc["enabled"]:
            raise NoAvailableAccount("账号已停用")
        headers = self.current_headers(acc)
        return {"id": acc["id"], "uid": acc["uid"], "nickname": acc["nickname"],
                "account": acc, "headers": headers}

    def build_signin_provider(self, account_id: int):
        """供签到引擎使用：返回 (headers_provider, endpoint)。"""
        acc = db.get_account(account_id)
        if not acc:
            raise NoAvailableAccount("账号不存在")
        endpoint = self.endpoint(acc)

        def headers_provider():
            return self.current_headers(acc)

        return headers_provider, endpoint


pool = AccountPool()