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
_rates_cache: dict[str, float] = {}   # key = "model:region", value = credit_rate
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


def get_rate(model: str, region: str) -> float:
    """获取模型在指定地区的积分倍率。若无缓存返回 1.0。"""
    return _rates_cache.get(f"{model}:{region}", 1.0)


def update_rate(model: str, region: str, rate: float):
    """更新指定模型的积分倍率。"""
    _rates_cache[f"{model}:{region}"] = rate
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


def build_model_display(upstream: str, region: str, rate: float) -> str:
    """构建展示用模型名（含地区标签 + 倍率）。"""
    if not region:
        return upstream
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
        rate = get_rate(m, region) if region else 1.0
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
async def sync_model_rates(backend: str, get_account_token_fn, region: str,
                           models: list[str], user_agent: str, domain: str):
    """
    对每个模型发送标准 prompt，从 usage.credit 推算积分倍率，更新缓存。
    由 scheduler 每日定时触发。
    """
    import httpx

    token = get_account_token_fn()
    if not token:
        sys.stderr.write(f"[model_rates] {region}: 无可用 token，跳过同步\n")
        return

    std_prompt = "Reply with exactly one word: test"
    updated = 0
    import asyncio as _asyncio
    async with httpx.AsyncClient(timeout=60) as client:
        for i, model in enumerate(models):
            # 跳过非通用模型（default/fast 等别名）
            if model in ("default-model", "fast-model", "balanced-model",
                         "primary-model", "deep-model"):
                continue
            # 避免触发限流：每 3 个请求后暂停 5 秒
            if i > 0 and i % 3 == 0:
                sys.stderr.write(f"[model_rates] {region}: 暂停 5s 避免限流...\n")
                await _asyncio.sleep(5)
            try:
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
                r = await client.post(
                    f"{backend}/v2/chat/completions",
                    json=body, headers=headers,
                )
                if r.status_code != 200:
                    err_body = r.text[:300]
                    sys.stderr.write(
                        f"[model_rates] {region} {model}: HTTP {r.status_code} → {err_body}\n")
                    continue
                # 流式响应：解析 SSE 提取 usage
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
                    updated += 1
                    sys.stderr.write(
                        f"[model_rates] {region} {model}: credit={credit}, "
                        f"tokens={tokens}, rate={estimated_rate}\n")
                else:
                    sys.stderr.write(
                        f"[model_rates] {region} {model}: 无 credit 数据 (credit={credit}, tokens={tokens})\n")
            except Exception as e:
                sys.stderr.write(
                    f"[model_rates] {region} {model}: 异常 {e}\n")

    set_last_sync(time.time())
    sys.stderr.write(
        f"[model_rates] {region}: 同步完成，更新 {updated}/{len(models)} 个模型\n")
