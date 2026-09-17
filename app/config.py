"""配置：全部来自环境变量，无硬编码凭据。"""
import os
from pathlib import Path

BACKEND = os.environ.get("BACKEND", "https://www.workbuddy.ai")
APP_PORT = int(os.environ.get("PORT", "8789"))
APP_HOST = os.environ.get("HOST", "0.0.0.0")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
API_KEY = os.environ.get("API_KEY", "") or os.environ.get("CODEBUDDY2OPENAI_KEY", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "workbuddy.db"
SIGNIN_CRON = os.environ.get("SIGNIN_CRON", "5 0 * * *")
SIGNIN_POLL_CRON = os.environ.get("SIGNIN_POLL_CRON", "0 */6 * * *")
TIMEZONE = os.environ.get("TZ", "Asia/Shanghai")
USER_AGENT = "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"
DEFAULT_DOMAIN = "www.workbuddy.ai"