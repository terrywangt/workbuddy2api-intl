"""
模型命名规范 + 积分倍率自动同步模块。

命名格式：
    {upstream_model}:{region}:{credit_rate}
    例：deepseek-v4.1-flash:cn:1.0  /  gpt-5.5:intl:1.2

region: cn = 国内版, intl = 国际版
credit_rate: 每 token 积分消耗倍率（1.0 = 基准，<1.0 = 便宜，>1.0 = 贵）

行为：
    - 客户端请求带后缀 → 剥离后缀映射回上游真实模型，响应中附带 rate 元数据
    - /v1/models 返回带后缀的模型名（供客户端发现价格信息）
    - 倍率缓存持久化到 data/model_rates.json，每日定时任务刷新
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 倍率缓存（内存 + 持久化）
# ---------------------------------------------------------------------------
_CACHE_FILE: Path | None = None
_rates_cache: dict[str, float | None] = {}   # key = "model:region", value = credit_rate 或 None
_last_sync: float = 0.0

# 基准积分（gpt-5.5 约 0.07 credit / 47 tokens 作为 1.0 基准）
_BASE_CREDIT = 0.07
_BASE_TOKENS = 47.0


def _cache_path() -> Path:
    global _CACHE_FILE
    if _CACHE_FILE is None:
        data_dir = os.environ.get("DATA_DIR", "/data")
        _CACHE_FILE = Path(data_dir) / "model_rates.json"
        _load_cache()
    return _CACHE_FILE


def _load_cache():
    global _rates_cache, _last_sync
    p = _cache_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            _rates_cache = data.get("rates", {})
            _last_sync = data.get("last_sync", 0.0)
        except Exception:
            pass


def _save_cache():
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "rates": _rates_cache,
        "last_sync": _last_sync,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def get_rate(model: str, region: str) -> float | None:
    """获取模型在指定地区的积分倍率。未同步过（无数据）返回 None。"""
    return _rates_cache.get(f"{model}:{region}")


def update_rate(model: str, region: str, rate: float | None):
    """更新指定模型的积分倍率（None = 无数据/未同步）。"""
    _rates_cache[f"{model}:{region}"] = rate
    _save_cache()


def delete_rate(model: str, region: str):
    """删除指定模型的倍率记录。"""
    _rates_cache.pop(f"{model}:{region}", None)
    _save_cache()


def set_last_sync(ts: float):
    global _last_sync
    _last_sync = ts
    _save_cache()


def get_last_sync() -> float:
    return _last_sync


# ---------------------------------------------------------------------------
# 模型名解析
# ---------------------------------------------------------------------------
_REGIONS = {"cn", "intl"}


def parse_model_name(raw_model: str) -> tuple[str, str, str]:
    """
    解析带后缀的模型名 → (upstream_model, region, raw_rate_str)
    不带后缀 → (raw_model, "", "")  # 透传，不修改
    例：
        'deepseek-v4.1-flash:cn:1.0'  → ('deepseek-v4.1-flash', 'cn', '1.0')
        'gpt-5.5:intl:1.2'            → ('gpt-5.5', 'intl', '1.2')
        'gpt-5.5'                     → ('gpt-5.5', '', '')
    """
    parts = raw_model.split(":")
    if len(parts) >= 3 and parts[-2] in _REGIONS:
        upstream = ":".join(parts[:-2])
        region = parts[-2]
        rate_str = parts[-1]
        return upstream, region, rate_str
    return raw_model, "", ""


def build_model_display(upstream: str, region: str, rate: float | None) -> str:
    """构建展示用模型名（含地区标签 + 倍率）。倍率未知时显示 none。"""
    if not region:
        return upstream
    if rate is None:
        return f"{upstream}:{region}:none"
    return f"{upstream}:{region}:{rate:.2f}"


def strip_model_suffix(model: str) -> str:
    """
    剥离 :region:rate 后缀，返回纯上游模型名。
    """
    upstream, _, _ = parse_model_name(model)
    return upstream


def decorate_model_name(model: str, region: str) -> str:
    """
    在响应中装饰模型名：裸模型名 → 带 :region:rate 后缀。
    已是带后缀的模型名原样返回。
    """
    if not model or model == "unknown":
        return model
    upstream, r, _ = parse_model_name(model)
    if r:
        return model  # 已带后缀
    return build_model_display(upstream, region, get_rate(upstream, region))


# ---------------------------------------------------------------------------
# /v1/models 模型列表生成
# ---------------------------------------------------------------------------
def build_model_list(base_models: list[str], region: str) -> list[dict]:
    """
    为 /v1/models 构建完整模型元数据列表。
    每个模型同时提供两个变体：带后缀（显示价格）和不带后缀（向后兼容）。
    """
    result = []
    seen = set()
    for m in base_models:
        rate = get_rate(m, region) if region else None
        # 带后缀版本（推荐使用）
        display = build_model_display(m, region, rate)
        if display not in seen:
            seen.add(display)
            result.append({
                "id": display,
                "object": "model",
                "owned_by": f"workbuddy-{region or 'unknown'}",
                "meta": {
                    "upstream_model": m,
                    "region": region,
                    "credit_rate": rate,
                },
            })
        # 裸模型名（向后兼容，也加入列表）
        if m not in seen:
            seen.add(m)
            result.append({
                "id": m,
                "object": "model",
                "owned_by": f"workbuddy-{region or 'unknown'}",
                "meta": {
                    "upstream_model": m,
                    "region": region,
                    "credit_rate": rate,
                },
            })
    return result


# ---------------------------------------------------------------------------
# 倍率自动同步（每日定时任务调用）
# ---------------------------------------------------------------------------
async def sync_one_model(backend: str, token: str, region: str, model: str,
                         user_agent: str, domain: str) -> dict:
    """
    对单个模型发送标准 prompt，从 usage.credit 推算积分倍率，更新缓存。
    不返回 credit 的模型倍率置为 None（显示 none）。

    Returns:
        {"model": ..., "rate": float|None, "credit": float, "tokens": int, "ok": bool}
    """
    import httpx

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a test bot. Reply with exactly one word: test"},
            {"role": "user", "content": "test"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "X-Domain": domain,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{backend}/v2/chat/completions", json=body, headers=headers)
        if r.status_code != 200:
            return {"model": model, "rate": None, "credit": 0.0, "tokens": 0,
                    "ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        credit = 0.0
        tokens = 0
        for line in r.text.splitlines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                usage = chunk.get("usage") or {}
                if usage.get("credit", 0) > 0:
                    credit = usage["credit"]
                if usage.get("total_tokens", 0) > 0:
                    tokens = usage["total_tokens"]
            except json.JSONDecodeError:
                continue
        if credit > 0 and tokens > 0:
            estimated_rate = round(credit / _BASE_CREDIT * (_BASE_TOKENS / tokens), 2)
            estimated_rate = max(0.1, min(10.0, estimated_rate))
            update_rate(model, region, estimated_rate)
            return {"model": model, "rate": estimated_rate, "credit": credit,
                    "tokens": tokens, "ok": True}
        else:
            update_rate(model, region, None)
            return {"model": model, "rate": None, "credit": credit,
                    "tokens": tokens, "ok": True}  # ok=True 表示已尝试（结果可能是 none）
    except Exception as e:
        return {"model": model, "rate": None, "credit": 0.0, "tokens": 0,
                "ok": False, "error": str(e)[:300]}


async def sync_model_rates(backend: str, get_account_token_fn, region: str,
                           models: list[str], user_agent: str, domain: str) -> dict:
    """
    对每个模型逐个同步倍率（首个账号导入/初始化时自动触发一次）。
    不做每日自动同步；后台可对单个模型手动触发。

    Returns:
        {"updated": n, "none": n, "failed": n, "results": [...]}
    """
    import asyncio as _asyncio

    token = get_account_token_fn()
    if not token:
        sys.stderr.write(f"[model_rates] {region}: 无可用 token，跳过同步\n")
        return {"updated": 0, "none": 0, "failed": 0, "results": [], "error": "无可用 token"}

    results = []
    updated = none_count = failed = 0
    for i, model in enumerate(models):
        if model in ("default-model", "fast-model", "balanced-model",
                     "primary-model", "deep-model"):
            continue
        if i > 0 and i % 3 == 0:
            sys.stderr.write(f"[model_rates] {region}: 暂停 5s 避免限流...\n")
            await _asyncio.sleep(5)
        res = await sync_one_model(backend, token, region, model, user_agent, domain)
        results.append(res)
        if not res.get("ok"):
            failed += 1
        elif res.get("rate") is not None:
            updated += 1
        else:
            none_count += 1
        if res.get("rate") is not None:
            sys.stderr.write(f"[model_rates] {region} {model}: rate={res['rate']} (credit={res.get('credit')}, tokens={res.get('tokens')})\n")
        else:
            sys.stderr.write(f"[model_rates] {region} {model}: 无 credit 数据 → rate=none\n")

    set_last_sync(time.time())
    summary = {"updated": updated, "none": none_count, "failed": failed, "results": results}
    sys.stderr.write(
        f"[model_rates] {region}: 同步完成 (updated={updated}, none={none_count}, failed={failed})\n")
    return summary
