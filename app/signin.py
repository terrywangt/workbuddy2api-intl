"""签到引擎：移植自 workbuddy-auto-signin（MIT），改造为 httpx 异步 + 多账号。

行为与上游一致：
- 幂等：先查状态，未签才领；重复运行不重复领取
- 成长中心 7 段全量（旅行礼物/任务/补登/连登兑换/盲盒/Buddy 盲盒/能量连签展示）
- 时间预算 + 网络/5xx 退避重试；写操作不重试（防重复提交）
- 任一请求遇 401/403：自动 refreshToken 刷新后重试一次；仍失败报 NO_SESSION
"""
import asyncio
import math
import time
import uuid

import httpx

CODE_NO_NETWORK = -1
CODE_BUDGET_OUT = -2
REQUEST_TIMEOUT = 30
NETWORK_RETRY_DELAYS = (5, 15, 30, 60, 90)
SERVER_RETRY_DELAYS = (3, 10)
MAKEUP_MAX_PER_RUN = 1


def dig(obj, key):
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if k in obj and isinstance(obj[k], dict):
                r = dig(obj[k], key)
                if r is not None:
                    return r
    return None


def as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def fmt_credit(v):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return v


def _client_token(prefix="u"):
    return "%s-%s" % (prefix, uuid.uuid4())


def _is_already_checked_in(cbody):
    if cbody is None:
        return True
    if isinstance(cbody, dict):
        msg = cbody.get("msg") or ""
        if cbody.get("code") == 10001 or "已签" in msg:
            return True
    return False


def _is_no_chance(msg):
    m = str(msg or "").lower()
    if not m:
        return False
    if "insufficient" in m or "not enough" in m:
        return "chance" in m or "balance" in m
    return "no chance" in m


def _is_unknown_tier(code, body):
    if code != 400:
        return False
    m = str(dig(body, "msg") or "").lower()
    return "tier" in m and ("unknown" in m or "invalid" in m or "unsupported" in m)


def _is_hard_failure(code):
    return code >= 500 or code in (CODE_NO_NETWORK, CODE_BUDGET_OUT)


def _http_label(code):
    if code == CODE_NO_NETWORK:
        return "网络不可达"
    if code == CODE_BUDGET_OUT:
        return "时间预算耗尽"
    return "HTTP %s" % code


def _fmt_eta(arrive_at, server_now):
    try:
        left = float(arrive_at) - float(server_now)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(left):
        return ""
    if left <= 0:
        return "，已到达待领取"
    minutes = int(round(left / 60.0))
    if minutes < 60:
        return "，约 %d 分钟后回" % max(1, minutes)
    return "，约 %.1f 小时后回" % (left / 3600.0)


_REDEEM_REWARDS = {
    "starter": "+2 能量 +1 补登卡 +1 次抽奖",
    "advanced": "+50 积分 +3 能量 +1 补登卡 +1 次抽奖",
    "legendary": "+150 积分 +5 能量 +1 补登卡 +1 次抽奖",
}


def _redeem_reward_desc(body, tier):
    bits = []
    credit = as_int(dig(body, "credit"), 0)
    energy = as_int(dig(body, "energy"), 0)
    if credit:
        bits.append("+%s 积分" % fmt_credit(credit))
    if energy:
        bits.append("+%s 能量" % fmt_credit(energy))
    if bits:
        return "（%s）" % " ".join(bits)
    return "（%s）" % _REDEEM_REWARDS.get(tier, "奖励已到账")


def _already_report(status, via=None):
    today_credit = dig(status, "today_credit") or dig(status, "daily_credit")
    streak_days = dig(status, "streak_days")
    total_credits = dig(status, "total_credits")
    is_streak_day = dig(status, "is_streak_day")
    next_streak_day = dig(status, "next_streak_day")
    inner = []
    if today_credit is not None:
        inner.append("今日 +%s" % fmt_credit(today_credit))
    if streak_days is not None:
        inner.append("连续 %s 天" % streak_days)
    if total_credits is not None:
        inner.append("累计 %s 积分" % fmt_credit(total_credits))
    prefix = via or "今日已签过"
    report = "%s（%s）" % (prefix, "，".join(inner)) if inner else prefix
    return {
        "result": "ALREADY", "report": report,
        "today_credit": today_credit, "streak_days": streak_days,
        "total_credits": total_credits, "is_streak_day": is_streak_day,
        "next_streak_day": next_streak_day,
    }


class SigninEngine:
    """单个账号的一轮签到。headers_provider 每次请求前调用（内含自动刷新）。"""

    def __init__(self, headers_provider, endpoint, budget_seconds=420.0,
                 refresh_cb=None):
        self._hp = headers_provider
        self._endpoint = endpoint
        self._budget = budget_seconds
        self._refresh_cb = refresh_cb or (lambda: None)
        self._started = None

    # ------------------------------------------------------------ 预算
    def _budget_left(self):
        if self._started is None:
            return self._budget
        return self._budget - (time.monotonic() - self._started)

    # ------------------------------------------------------------ 请求层
    def _retry_delays(self, code):
        if code == CODE_NO_NETWORK:
            return NETWORK_RETRY_DELAYS
        if code >= 500:
            return SERVER_RETRY_DELAYS
        return ()

    async def _request_once(self, client, url, method, payload=None, timeout=None):
        headers = dict(self._hp())
        headers["User-Agent"] = "WorkBuddy"
        body = None
        if payload is not None:
            body = json_dumps(payload)
        try:
            r = await client.request(method, url, headers=headers, content=body,
                                     timeout=timeout or min(REQUEST_TIMEOUT, max(1, self._budget_left())))
            raw = r.content.decode("utf-8", "replace")
            try:
                return r.status_code, _json_loads(raw)
            except Exception:
                return r.status_code, {"raw": raw[:500]}
        except httpx.HTTPError as e:
            return CODE_NO_NETWORK, {"error": str(e)}
        except Exception as e:
            return CODE_NO_NETWORK, {"error": str(e)}

    async def _request(self, client, url, method="GET", payload=None, retry=True):
        """带预算 + 退避 + 401 自动刷新重试的请求。"""
        if self._budget_left() <= 1:
            return CODE_BUDGET_OUT, {"error": "已达本次运行时间预算，跳过剩余请求"}
        code, body = await self._request_once(client, url, method, payload)
        # 401/403：先刷新 token 再重试一次，仍失败如实返回（上层判 NO_SESSION）
        if code in (401, 403):
            try:
                self._refresh_cb()
            except Exception:
                pass
            code2, body2 = await self._request_once(client, url, method, payload)
            if code2 not in (401, 403):
                code, body = code2, body2
        if not retry:
            return code, body
        attempts = {}
        while True:
            delays = self._retry_delays(code)
            if not delays:
                return code, body
            used = attempts.get(delays, 0)
            if used >= len(delays):
                return code, body
            delay = delays[used]
            attempts[delays] = used + 1
            if self._budget_left() <= delay + REQUEST_TIMEOUT:
                return code, body
            await asyncio.sleep(delay)
            code2, body2 = await self._request_once(client, url, method, payload)
            if code2 in (401, 403):
                try:
                    self._refresh_cb()
                except Exception:
                    pass
                code2, body2 = await self._request_once(client, url, method, payload)
            code, body = code2, body2

    async def get(self, client, url):
        return await self._request(client, url, "GET", retry=True)

    async def post(self, client, url, payload=None, retry=False):
        return await self._request(client, url, "POST", payload, retry=retry)

    # ------------------------------------------------------------ 签到
    def _check_auth(self, code):
        return code in (401, 403)

    async def run_auto(self, client):
        code_str = {"code": 0}
        scode, sbody = await self.post(client, self._endpoint + "/v2/billing/meter/checkin-activity-status", retry=True)
        if scode == CODE_BUDGET_OUT:
            return 1, {"result": "TIMEOUT", "report": "已达本次运行时间预算，签到跳过，下次自动重试"}
        if scode == CODE_NO_NETWORK:
            return 1, {"result": "NETWORK", "report": "网络不可达，签到跳过，下次自动重试（%s）" % (sbody.get("error") or ""), "error": sbody.get("error")}
        if self._check_auth(scode):
            return 1, {"result": "NO_SESSION", "report": "登录态已失效（HTTP %s），请重新上传授权" % scode, "http": scode}
        if not (200 <= scode < 300):
            return 1, {"result": "ERROR", "report": "签到接口返回异常（HTTP %s）" % scode, "http": scode, "status_body": sbody}

        status = sbody if isinstance(sbody, dict) else {}
        active = dig(status, "active")
        activity_name = dig(status, "activity_name")
        if active is False:
            report = "签到活动未开启" + ("（%s）" % activity_name if activity_name else "")
            return 0, {"result": "INACTIVE", "report": report, "active": False}
        if dig(status, "today_checked_in") in (True, 1):
            return 0, _already_report(status)

        ccode, cbody = await self.post(client, self._endpoint + "/v2/billing/meter/daily-checkin", retry=True)
        if ccode in (CODE_NO_NETWORK, CODE_BUDGET_OUT):
            return 1, {"result": "NETWORK" if ccode == CODE_NO_NETWORK else "TIMEOUT",
                       "report": "领取请求未能送达，下次自动重试（%s）" % ((cbody.get("error") or "") if isinstance(cbody, dict) else "")}
        if self._check_auth(ccode):
            return 1, {"result": "NO_SESSION", "report": "登录态已失效（HTTP %s），请重新上传授权" % ccode, "http": ccode}
        if _is_already_checked_in(cbody):
            scode2, sbody2 = await self.post(client, self._endpoint + "/v2/billing/meter/checkin-activity-status", retry=True)
            fresh = sbody2 if (200 <= scode2 < 300 and isinstance(sbody2, dict)) else status
            return 0, _already_report(fresh, via="今日已签过（服务端判定已领取）")

        credit = dig(cbody, "credit")
        if credit is not None:
            scode2, sbody2 = await self.post(client, self._endpoint + "/v2/billing/meter/checkin-activity-status", retry=True)
            fresh = sbody2 if (200 <= scode2 < 300 and isinstance(sbody2, dict)) else status
            streak_days = dig(fresh, "streak_days") or dig(status, "streak_days")
            total_credits = dig(fresh, "total_credits")
            is_streak_day = dig(fresh, "is_streak_day")
            next_streak_day = dig(fresh, "next_streak_day")
            bonus = "，且为连签奖励日" if is_streak_day else ""
            cum = "，累计 %s 积分" % fmt_credit(total_credits) if total_credits is not None else ""
            streak = "（连续 %s 天%s）" % (streak_days, cum) if streak_days is not None else (
                "（%s）" % cum.lstrip("，") if cum else "")
            report = "成功领取 %s 积分%s%s" % (fmt_credit(credit), bonus, streak)
            return 0, {"result": "CLAIMED", "report": report, "credit": credit,
                       "streak_days": streak_days, "total_credits": total_credits,
                       "is_streak_day": is_streak_day, "next_streak_day": next_streak_day}

        if isinstance(cbody, dict) and ("code" in cbody or "msg" in cbody):
            msg = cbody.get("msg") or ("code %s" % cbody.get("code"))
            return 1, {"result": "ERROR", "report": "领取失败：%s（HTTP %s）" % (msg, ccode), "http": ccode, "claim_body": cbody}
        return 1, {"result": "UNKNOWN", "report": "未识别的领取返回，请检查接口：%s" % str(cbody)[:200], "http": ccode, "claim_body": cbody}

    # ------------------------------------------------------------ 成长中心
    def _note_http(self, parts, failures, hard_failures, code, body, label):
        if 200 <= code < 300:
            return False
        reason = _http_label(code)
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error") or body.get("msg") or "")
        parts.append("%s失败：%s" % (label, "%s（%s）" % (reason, detail)
                                     if detail and detail != reason else (detail or reason)))
        if _is_hard_failure(code):
            return True
        return True

    async def run_growth(self, client):
        base = self._endpoint + "/v2/activity/growth"
        parts = []
        credits_gained = 0
        failures = 0
        hard_failures = 0
        successes = 0

        def _check_auth(code):
            return code in (401, 403)

        def _note(code, body, label_):
            nonlocal failures, hard_failures
            if 200 <= code < 300:
                return False
            reason = _http_label(code)
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("error") or body.get("msg") or "")
            parts.append("%s失败：%s" % (label_, "%s（%s）" % (reason, detail)
                                         if detail and detail != reason else (detail or reason)))
            if _is_hard_failure(code):
                failures += 1
                hard_failures += 1
            return True

        # --- 1. Buddy 旅行 ---
        try:
            scode, sbody = await self.get(client, base + "/buddy/travel/status")
            if scode == CODE_BUDGET_OUT:
                return 1, {"result": "TIMEOUT", "report": "时间预算耗尽，成长中心跳过，下次自动重试"}
            if scode == CODE_NO_NETWORK:
                return 1, {"result": "NETWORK", "report": "网络不可达，成长中心跳过（%s）" % (sbody.get("error") or "")}
            if _check_auth(scode):
                return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
            travel = dig(sbody, "state") if (200 <= scode < 300) else None
            daily_limit = bool(dig(sbody, "daily_limit_reached")) if (200 <= scode < 300) else False
            _note(scode, sbody, "查旅行状态")
            claimed_travel = False
            if travel == "arrived":
                record_id = dig(sbody, "record_id")
                ccode, cbody = await self.post(client, base + "/buddy/travel/claim", {"record_id": record_id})
                if _check_auth(ccode):
                    return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                if 200 <= ccode < 300 and dig(cbody, "reward_credit") is not None:
                    got = as_int(dig(cbody, "reward_credit"))
                    credits_gained += got
                    parts.append("领旅行礼物 +%s 积分" % fmt_credit(got))
                    successes += 1
                    claimed_travel = True
                else:
                    msg = (dig(cbody, "msg") or "") if isinstance(cbody, dict) else ""
                    parts.append("领旅行礼物失败：%s" % (msg or "HTTP %s" % ccode))
                    failures += 1
                    hard_failures += _is_hard_failure(ccode)
                if claimed_travel:
                    travel = "idle"
            if travel == "idle" and daily_limit:
                parts.append("今日旅行名额已用完")
            elif travel == "idle":
                ccode, cbody = await self.get(client, base + "/buddy/travel/config")
                if _check_auth(ccode):
                    return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                locs = dig(cbody, "locations") if (200 <= ccode < 300) else None
                if locs and isinstance(locs[0], dict):
                    loc = locs[0]
                    dcode, dbody = await self.post(client, base + "/buddy/travel/depart", {"location_id": loc.get("id")})
                    if _check_auth(dcode):
                        return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                    if 200 <= dcode < 300:
                        loc_name = (dig(dbody, "location") or {}).get("name", "?")
                        dur = dig(dbody, "duration_hours") or (dig(dbody, "location") or {}).get("duration_hours", "?")
                        parts.append("派 Buddy 去%s（%s 小时后回）" % (loc_name, dur))
                        successes += 1
                    else:
                        msg = dig(dbody, "msg") or ""
                        parts.append("派 Buddy 失败：%s" % (msg or "HTTP %s" % dcode))
                        failures += 1
                        hard_failures += _is_hard_failure(dcode)
            elif travel == "traveling":
                loc_name = (dig(sbody, "location") or {}).get("name", "?")
                parts.append("Buddy 旅行中（%s%s）" % (loc_name, _fmt_eta(dig(sbody, "arrive_at"), dig(sbody, "server_now"))))
        except Exception as e:
            parts.append("旅行模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

        # --- 2. 任务领奖 ---
        if self._budget_left() <= 0:
            parts.append("时间预算耗尽，任务领奖跳过")
        else:
            try:
                tcode, tbody = await self.get(client, base + "/tasks")
                if _check_auth(tcode):
                    return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                if not _note(tcode, tbody, "查任务列表"):
                    tasks = dig(tbody, "tasks") or []
                    for t in tasks:
                        if self._budget_left() <= 0:
                            parts.append("时间预算耗尽，剩余任务下次再领")
                            break
                        try:
                            status = t.get("accept_status")
                            if status == "claimed" or t.get("locked"):
                                continue
                            prog = t.get("progress") or {}
                            target = as_int(prog.get("target"), 1) or 1
                            done = as_int(prog.get("current")) >= target
                            if not done and status:
                                continue
                            claiming = bool(done and status)
                            acode, abody = await self.post(client, base + "/tasks/accept", {"task_code": t.get("task_code")})
                            if _check_auth(acode):
                                return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                            title = t.get("title", t.get("task_code"))
                            if 200 <= acode < 300:
                                if claiming:
                                    rc = as_int(dig(abody, "credit"), t.get("reward_credit"))
                                    re_ = as_int(dig(abody, "energy"), t.get("reward_energy"))
                                    credits_gained += rc
                                    parts.append("领任务奖「%s」+credit%s+energy%s" % (title, rc, re_))
                                else:
                                    parts.append("领取任务「%s」（进度开始计）" % title)
                                successes += 1
                            else:
                                msg = dig(abody, "msg") or ""
                                parts.append("%s「%s」失败：%s" % ("领任务奖" if claiming else "领取任务", title, msg or "HTTP %s" % acode))
                                failures += 1
                                hard_failures += _is_hard_failure(acode)
                        except Exception as e:
                            parts.append("任务「%s」异常（%s: %s）" % (t.get("task_code", "?"), type(e).__name__, e))
                            failures += 1
                            hard_failures += 1
            except Exception as e:
                parts.append("任务模块异常（%s: %s）" % (type(e).__name__, e))
                failures += 1
                hard_failures += 1

        # --- 3. 补登 ---
        streak_body = None
        streak_stale = False
        if self._budget_left() <= 0:
            parts.append("时间预算耗尽，补登跳过")
        else:
            try:
                mcode, mbody = await self.get(client, base + "/streak")
                if _check_auth(mcode):
                    return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                if not _note(mcode, mbody, "查连登状态"):
                    streak_body = mbody
                    cards_obj = dig(mbody, "makeup_cards")
                    cards = as_int(cards_obj.get("balance")) if isinstance(cards_obj, dict) else as_int(cards_obj)
                    streak_obj = dig(mbody, "streak") or {}
                    dates = (streak_obj.get("makeup_dates") if isinstance(streak_obj, dict) else None) \
                        or dig(mbody, "makeup_dates") or []
                    if cards > 0 and isinstance(dates, list) and dates:
                        for d in dates[:min(cards, MAKEUP_MAX_PER_RUN)]:
                            if self._budget_left() <= 0:
                                parts.append("时间预算耗尽，剩余补登下次再做")
                                break
                            ucode, ubody = await self.post(client, base + "/makeup-cards/use",
                                                           {"target_date": d, "client_token": _client_token()})
                            if _check_auth(ucode):
                                return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                            if 200 <= ucode < 300:
                                cards -= 1
                                streak_stale = True
                                left_obj = dig(ubody, "makeup_cards")
                                left_cards = as_int(left_obj.get("balance"), cards) if isinstance(left_obj, dict) else as_int(left_obj, cards)
                                parts.append("补登 %s（剩 %s 张卡）" % (d, left_cards))
                                successes += 1
                            else:
                                msg = dig(ubody, "msg") or ""
                                parts.append("补登 %s 失败：%s" % (d, msg or "HTTP %s" % ucode))
                                failures += 1
                                hard_failures += _is_hard_failure(ucode)
                        if len(dates) > MAKEUP_MAX_PER_RUN and cards > 0:
                            parts.append("另有 %s 天可补、剩 %s 张卡，下轮继续" % (len(dates) - MAKEUP_MAX_PER_RUN, cards))
            except Exception as e:
                parts.append("补登模块异常（%s: %s）" % (type(e).__name__, e))
                failures += 1
                hard_failures += 1

        # --- 4. 连登兑换 ---
        if self._budget_left() <= 0:
            parts.append("时间预算耗尽，连登兑换跳过")
        else:
            try:
                rcode, rbody = await self.get(client, base + "/redeem/summary")
                if _check_auth(rcode):
                    return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                if not _note(rcode, rbody, "查连登兑换"):
                    for tier, label, days in (("starter", "入门", 7), ("advanced", "进阶", 14), ("legendary", "巅峰", 28)):
                        if self._budget_left() <= 0:
                            parts.append("时间预算耗尽，剩余连登兑换下次再领")
                            break
                        status = dig(rbody, tier + "_status")
                        if not status or status in ("claimed", "locked"):
                            continue
                        c2code, c2body = await self.post(client, base + "/redeem", {"tier": days, "client_token": _client_token()})
                        if _is_unknown_tier(c2code, c2body):
                            c2code, c2body = await self.post(client, base + "/redeem", {"tier": tier, "client_token": _client_token()})
                        if _check_auth(c2code):
                            return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                        if 200 <= c2code < 300:
                            credits_gained += as_int(dig(c2body, "credit"))
                            parts.append("连登兑换「%s」%s" % (label, _redeem_reward_desc(c2body, tier)))
                            successes += 1
                        else:
                            msg = dig(c2body, "msg") or ""
                            parts.append("连登兑换「%s」失败：%s" % (label, msg or "HTTP %s" % c2code))
                            failures += 1
                            hard_failures += _is_hard_failure(c2code)
            except Exception as e:
                parts.append("连登兑换模块异常（%s: %s）" % (type(e).__name__, e))
                failures += 1
                hard_failures += 1

        # --- 5. 盲盒/抽奖 ---
        if self._budget_left() <= 0:
            parts.append("时间预算耗尽，盲盒跳过")
        else:
            try:
                lcode, lbody = await self.get(client, base + "/lottery/chances")
                if _check_auth(lcode):
                    return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                chances = 0 if _note(lcode, lbody, "查抽奖机会") else as_int(dig(lbody, "balance"))
                if chances > 0:
                    dcode, dbody = await self.post(client, base + "/lottery/draw", {"client_token": _client_token()})
                    if _check_auth(dcode):
                        return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                    if 200 <= dcode < 300:
                        prize = dig(dbody, "prize_name") or dig(dbody, "prize") or "未知"
                        if not isinstance(prize, str):
                            prize = str(prize)
                        if dig(dbody, "need_address") or dig(dbody, "require_address"):
                            prize += "（实物奖，需到成长中心填写收件信息）"
                        parts.append("开盲盒获得：%s" % prize)
                        successes += 1
                        if chances > 1:
                            parts.append("还剩 %s 次抽奖机会，下轮继续" % (chances - 1))
                    else:
                        msg = dig(dbody, "msg") or ""
                        if _is_no_chance(msg):
                            parts.append("开盲盒：%s" % (msg or "无抽奖机会"))
                        else:
                            parts.append("开盲盒失败：%s" % (msg or "HTTP %s" % dcode))
                            failures += 1
                            hard_failures += _is_hard_failure(dcode)
            except Exception as e:
                parts.append("盲盒模块异常（%s: %s）" % (type(e).__name__, e))
                failures += 1
                hard_failures += 1

        # --- 6. Buddy 盲盒 ---
        if self._budget_left() <= 0:
            parts.append("时间预算耗尽，Buddy 盲盒跳过")
        else:
            try:
                qcode, qbody = await self.get(client, base + "/buddy/quota")
                if _check_auth(qcode):
                    return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                if not _note(qcode, qbody, "查 Buddy 能量"):
                    affordable = as_int(dig(qbody, "affordable"))
                    max_open = as_int(dig(qbody, "max_open_count"), 1) or 1
                    if affordable > 0:
                        count = min(affordable, max_open)
                        ocode, obody = await self.post(client, base + "/buddy/open", {"count": count, "client_token": _client_token()})
                        if _check_auth(ocode):
                            return 1, {"result": "NO_SESSION", "report": "登录态已失效，请重新上传授权"}
                        if 200 <= ocode < 300:
                            name = dig(obody, "buddy") or dig(obody, "name") or dig(obody, "buddies")
                            if not isinstance(name, str):
                                name = "新 Buddy"
                            parts.append("开 Buddy 盲盒 ×%s（%s）" % (count, name))
                            successes += 1
                        else:
                            msg = dig(obody, "msg") or ""
                            parts.append("开 Buddy 盲盒失败：%s" % (msg or "HTTP %s" % ocode))
                            failures += 1
                            hard_failures += _is_hard_failure(ocode)
            except Exception as e:
                parts.append("Buddy 盲盒模块异常（%s: %s）" % (type(e).__name__, e))
                failures += 1
                hard_failures += 1

        # --- 7. 能量 & 连签 ---
        energy = None
        streak_days = None
        if self._budget_left() > 0:
            try:
                ecode, ebody = await self.get(client, base + "/energy")
                if not _check_auth(ecode):
                    energy = dig(ebody, "balance") if (200 <= ecode < 300) else None
            except Exception:
                pass
        try:
            if streak_body is not None and not streak_stale:
                streak_obj = dig(streak_body, "streak") or {}
                streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
            elif self._budget_left() > 0:
                scode2, sbody2 = await self.get(client, base + "/streak")
                if not _check_auth(scode2):
                    streak_obj = dig(sbody2, "streak") or {}
                    streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
        except Exception:
            pass

        tail = []
        if energy is not None:
            tail.append("能量 %s" % energy)
        if streak_days is not None:
            tail.append("连签 %s 天" % streak_days)
        if credits_gained:
            tail.append("本次 +共 %s 积分" % credits_gained)

        if parts:
            report = "；".join(parts)
        elif failures:
            report = "成长中心各步骤均失败"
        else:
            report = "成长中心无可领取项"
        if tail:
            report += "（%s）" % "，".join(tail)

        result_code = 1 if (hard_failures and not successes) else 0
        idle = (successes == 0 and failures == 0)
        return result_code, {"result": "GROWTH", "report": report, "credits_gained": credits_gained,
                             "energy": energy, "streak_days": streak_days, "idle": idle,
                             **({"failures": failures} if failures else {})}

    # ------------------------------------------------------------ 每日组合
    async def run_daily(self, client):
        """查状态→未签才领→成长中心。返回 (code, out, quiet)。"""
        self._started = time.monotonic()
        code, out = await self.run_auto(client)
        if out.get("result") in ("NETWORK", "TIMEOUT"):
            out["growth"] = "网络不可达或时间预算耗尽，成长中心跳过"
            out["growth_result"] = out["result"]
            return code, out, False
        if out.get("result") == "NO_SESSION":
            out["growth"] = "登录态已失效，成长中心跳过"
            out["growth_result"] = out["result"]
            return code, out, False
        try:
            gcode, gout = await self.run_growth(client)
        except Exception as e:
            gcode, gout = 1, {"result": "ERROR", "report": "成长中心异常（%s: %s）" % (type(e).__name__, e)}
        out["growth"] = gout.get("report")
        out["growth_result"] = gout.get("result")
        if gout.get("credits_gained"):
            out["report"] += "；" + gout["report"]
        if gcode != 0 and code == 0:
            code = gcode
        quiet = out.get("result") in ("ALREADY", "INACTIVE") and bool(gout.get("idle"))
        return code, out, quiet


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _json_loads(raw):
    import json
    return json.loads(raw)


async def run_signin_for_account(account_id: int, kind: str = "auto", budget: float = 420.0) -> dict:
    """为指定账号跑一轮完整签到（签到+成长中心），写库并返回结果。"""
    from .accounts import pool
    from . import db
    acc = db.get_account(account_id)
    if not acc:
        return {"result": "ERROR", "report": "账号不存在"}
    provider, endpoint = pool.build_signin_provider(account_id)
    engine = SigninEngine(provider, endpoint, budget_seconds=budget,
                          refresh_cb=lambda: pool.refresh(acc))
    async with httpx.AsyncClient() as client:
        code, out, quiet = await engine.run_daily(client)
    out["exit_code"] = code
    out["trigger"] = kind
    db.add_log(account_id, kind, {k: v for k, v in out.items() if k != "exit_code"})
    db.touch_signin(account_id, out)
    return out