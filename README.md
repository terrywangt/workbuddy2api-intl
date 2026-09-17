# WorkBuddy2API-Intl

把 **WorkBuddy 国际版（WorkBuddy AI）** 账号变成一套自托管服务：OpenAI / Anthropic 兼容代理 + 多账号管理台。

> **基于 [terrywangt/workbuddy-allinone](https://github.com/terrywangt/workbuddy-allinone)（MIT）改造**
> 仅供个人学习研究，非腾讯官方项目，请合法使用自有订阅。

## 功能

| 模块 | 说明 |
|---|---|
| 🚀 代理 API | `POST /v1/chat/completions`、`/v1/responses`（Codex）、`/v1/messages`（Claude Code）、`/v1/models`、`/health` |
| 🔁 自动续期 | accessToken 过期自动用 refreshToken 刷新，无需手工干预 |
| 👥 多账号 | 多个授权账号共存；代理请求轮询切换；账号可单独启用/停用 |
| 🛠 管理台 | `/admin` 网页：查看账号、上传授权文件 / 粘贴 JSON / 手动填 token 添加账号、查看日志 |
| 🌐 国际版 | 对接 `www.workbuddy.ai`（国际版），支持 Claude / GPT-5 / Gemini 等模型 |

## 与国内版的区别

| 维度 | 国内版（workbuddy-allinone） | 本项目（workbuddy2api-intl） |
|---|---|---|
| 域名 | www.codebuddy.cn / copilot.tencent.com | **www.workbuddy.ai** |
| 登录 | 国内 OAuth | **Google / GitHub OAuth** |
| 模型 | 国内 18 个（GLM/Kimi/DeepSeek 等） | **国际 21 个**（Claude/GPT-5/Gemini/GLM/Kimi/Hunyuan） |
| 凭证文件 | `workbuddy-desktop.info` | `workbuddy-desktop-ai.info` |
| 端口 | 8787 | **8789** |

## 授权文件格式

即 WorkBuddy **国际版** 桌面端登录后写出的 `workbuddy-desktop-ai.info`（含 `account` + `auth` 两个对象）：

```json
{
  "account": { "uid": "...", "nickname": "...", ... },
  "auth": {
    "accessToken": "...",
    "refreshToken": "...",
    "expiresIn": 31525472,
    "expiresAt": 1821158338417,
    "refreshExpiresAt": 1821158338417,
    "domain": "www.workbuddy.ai"
  }
}
```

> ⚠️ **国际版与国内版凭证不互通**：国内版凭证（domain: www.codebuddy.cn）会被国际版网关拒绝（JWT issuer 校验）。

## 获取账号凭证

### 方式一：OAuth 设备授权登录（推荐）

1. 部署好网关后，打开管理台「登录新账号」
2. 浏览器打开授权链接，用 **GitHub 或 Google** 账号登录
3. 网关自动获取 accessToken/refreshToken → 落库

### 方式二：导入国际版桌面客户端凭证

1. 下载 [WorkBuddy 国际版客户端](https://www.workbuddy.ai/)（Windows/macOS/Linux）
2. 登录（GitHub 或 Google 授权）
3. 找到凭证文件：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`
4. 管理台「上传授权文件」导入

### 方式三：CLI 官方引导

```bash
npm install -g @tencent-ai/codebuddy-code
codebuddy   # 选择 "Log in via International Site" → 完成授权
```

## 快速开始（Docker）

```bash
cp .env.example .env
vim .env          # 至少设置 ADMIN_PASSWORD
docker compose up -d
```

- 管理台：`http://<host>:8789/admin`
- 代理：`http://<host>:8789/v1`（任意 OpenAI 兼容客户端）

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.proxy --host 0.0.0.0 --port 8789
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | （必填） | 管理台密码 |
| `API_KEY` | 空 | API 鉴权 key；留空则不鉴权 |
| `DATA_DIR` | `./data` | 数据卷：SQLite 账号库 + 日志 |
| `SIGNIN_CRON` | `5 0 * * *` | 每日签到 cron（Asia/Shanghai） |
| `HOST` / `PORT` | `0.0.0.0` / `8789` | 监听地址 |

## 可用模型（21 个）

```
deepseek-v4.1-flash   gpt-6-astra      gpt-5.6-sol     gpt-5.6-terra
gpt-5.6-luna          gpt-5.5          gpt-5.4         gpt-5.3-codex
gemini-3.5-flash      glm-5.3          glm-5.2         kimi-k3
kimi-k2.6             hy4-preview      hy4-preview-f   hy3
default-model         fast-model       balanced-model  primary-model
deep-model
```

## 免责声明

本项目仅供个人学习与研究使用，与腾讯、WorkBuddy 无官方关联。请仅在你合法拥有订阅的前提下使用，接口可能随时变动，风险自负。

## License

[MIT](./LICENSE)
