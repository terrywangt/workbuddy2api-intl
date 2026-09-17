"""轻量 cron 调度器（无第三方依赖）：在指定时区按 cron 表达式触发 async 任务。

支持标准 5 字段：分 时 日 月 周（0-6，0=周日），* 与 */n。
"""
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("scheduler")


def parse_cron(expr: str):
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron 表达式必须是 5 段：{expr}")

    def _parse_field(field, lo, hi):
        out = set()
        for part in field.split(","):
            part = part.strip()
            if part == "*":
                out.update(range(lo, hi + 1))
                continue
            step = 1
            if "/" in part:
                part, _, s = part.partition("/")
                step = int(s)
            if part == "*":
                out.update(range(lo, hi + 1, step))
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                out.update(range(int(a), int(b) + 1, step))
            else:
                out.add(int(part))
        return out

    return (
        _parse_field(fields[0], 0, 59),
        _parse_field(fields[1], 0, 23),
        _parse_field(fields[2], 1, 31),
        _parse_field(fields[3], 1, 12),
        _parse_field(fields[4], 0, 6),
    )


def next_run(expr: str, after: datetime, tz: ZoneInfo) -> datetime:
    minutes, hours, doms, months, dows = parse_cron(expr)
    t = after.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # 最多扫描 8 天（每日/每小时间隔的表达式必然命中）
    horizon = t + timedelta(days=8)
    while t < horizon:
        if t.month in months and t.day in doms and t.weekday() in dows \
                and t.hour in hours and t.minute in minutes:
            return t
        t += timedelta(minutes=1)
    raise RuntimeError(f"cron 表达式在 8 天内无匹配：{expr}")


class CronLoop:
    """按多个 cron 表达式循环触发；同一时刻只允许一个任务体运行。"""

    def __init__(self, jobs: dict[str, str], tz_name: str, runner):
        """
        jobs: {名称: cron 表达式}
        runner: async (name) -> None
        """
        self.jobs = jobs
        self.tz = ZoneInfo(tz_name)
        self.runner = runner
        self._lock = asyncio.Lock()
        self._task = None
        self._stop = False

    def start(self):
        self._task = asyncio.create_task(self._loop())
        return self._task

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self):
        while not self._stop:
            now = datetime.now(self.tz)
            schedule = sorted(
                (next_run(expr, now, self.tz), name) for name, expr in self.jobs.items()
            )
            target, name = schedule[0]
            delay = (target - datetime.now(self.tz)).total_seconds()
            log.info("下次定时任务 %s 于 %s（%.0f 秒后）", name, target, delay)
            try:
                await asyncio.wait_for(self._sleep(delay), timeout=delay + 5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("等待定时任务中断：%s", e)
            if self._stop:
                break
            try:
                if self._lock.locked():
                    log.info("定时任务 %s 触发时上一轮仍在运行，跳过本次", name)
                else:
                    async with self._lock:
                        await self.runner(name)
            except Exception as e:
                log.error("定时任务 %s 执行异常：%s", name, e)

    async def _sleep(self, delay):
        if delay > 0:
            await asyncio.sleep(delay)