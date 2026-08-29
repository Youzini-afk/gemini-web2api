# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[English](README.md)

将 Google Gemini 网页端转换为 OpenAI 兼容 API，并集成管理后台与代理节点池。零成本、跨平台。

## 特性

- **可选密钥**: `api_keys` 为空时免密, 填入密钥后按 OpenAI Bearer Key 校验
- **OpenAI 兼容**: 直接替换 `/v1/chat/completions` 和 `/v1/models`
- **工具调用**: 完整的 Function Calling 支持 (OpenAI 格式)
- **多模型**: Flash (3.7/3.6), 扩展思考 (2万字+输出), Pro, Auto, Lite
- **思考深度**: 通过 `@think=N` 后缀调节 (0=最深, 4=最浅)
- **联网搜索**: 内置互联网访问 (Gemini 原生搜索能力)
- **跨平台**: Python 服务；Docker 镜像内置可选的 Mihomo 节点引擎和 Go TLS helper
- **流式输出**: 基于 `httpx` 的 SSE Streaming 支持
- **Codex CLI**: Responses API (`/v1/responses`) 兼容 OpenAI Codex
- **Gemini CLI**: Google 原生 API (`/v1beta/models`) 兼容 Gemini CLI

## 快速开始

```bash
pip install -r requirements.txt
python gemini_web2api.py
```

服务启动在 `http://localhost:8081/v1`.

## 客户端配置

### Cherry Studio / ChatBox / 任何 OpenAI 兼容客户端

| 字段 | 值 |
|------|-----|
| Base URL | `http://localhost:8081/v1` |
| API Key | 未启用鉴权时随便填；启用后填你的密钥 |
| Model | `gemini-3.5-flash-thinking` |

### curl

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"你好!"}]}'
```

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "解释量子计算"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

支持 Google 原生 API 端点:
- `GET /v1beta/models` — 模型列表
- `POST /v1beta/models/{model}:generateContent` — 非流式生成
- `POST /v1beta/models/{model}:streamGenerateContent` — 流式生成 (SSE)

## 可用模型

| 模型 | 说明 | 输出量 |
|------|------|--------|
| `gemini-3.7-flash` | 最新全能模型 | ~1.2万字 |
| `gemini-3.6-flash` | 全能模型 | ~1.2万字 |
| `gemini-3.5-flash` | gemini-3.6-flash 别名 | ~1.2万字 |
| `gemini-3.5-flash-thinking` | 扩展思考, 最长输出 | **~2万字** |
| `gemini-3.5-flash-thinking-lite` | 自适应思考深度 | ~1.5万字 |
| `gemini-3.1-pro` | 高级数学与代码 (需 cookie) | ~1.2万字 |
| `gemini-auto` | 自动选择模型 | 不定 |
| `gemini-flash-lite` | 最快响应, 轻量 | ~1万字 |

### 思考深度

在模型名后追加 `@think=N`:

```
gemini-3.5-flash-thinking@think=0   # 最深 (默认)
gemini-3.5-flash-thinking@think=2   # 中等
gemini-3.5-flash-thinking@think=4   # 最浅
```

## 可选: Cookie 配置 (Pro 模型)

匿名访问对所有模型有效, 但 `gemini-3.1-pro` 在无认证时会路由到 Flash. 要获得真正的 Pro 路由, 需要 **Gemini Advanced (付费订阅)** 账号的 cookie:

```bash
python gemini_web2api.py --cookie-file cookie.txt
```

### 如何获取 Cookie

1. 打开 Chrome, 访问 [gemini.google.com](https://gemini.google.com) 并登录 **Gemini Advanced** 付费账号
2. 打开开发者工具 (F12) → Application → Cookies → `https://gemini.google.com`
3. 复制以下 cookie 值: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`
4. 创建 `cookie.txt`, 格式如下:

```
SID=你的SID值; HSID=你的HSID值; SSID=你的SSID值; APISID=你的APISID值; SAPISID=你的SAPISID值; __Secure-1PSID=你的1PSID值
```

或使用 JSON 格式:
```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx", "sapisid": "你的SAPISID值"}
```

**替代方案 (浏览器扩展)**: 使用任意 "Export Cookies" 扩展导出 `gemini.google.com` 的 cookie, 然后转换为上述单行格式.

### 登录账号路径与 XSRF Token

如果已登录的 Gemini 页面 URL 带账号序号, 例如:

```
https://gemini.google.com/u/1/app/...
```

请把 `auth_user` 设置为该序号。登录态的 Gemini Web 请求还可能需要页面里的 XSRF token。该 token 在渲染后的 Gemini 页面源码中名为 `SNlM0e`; 在 `config.json` 中填入 `xsrf_token` 后, 服务会把它作为 `at` 表单字段提交。

示例:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

如果登录态请求返回 HTTP 400 且错误中包含 `xsrf`, 请刷新 Gemini Web 后更新 `xsrf_token`, 并确认 `auth_user` 与浏览器 URL 中的 `/u/<序号>/` 一致.

Pro 路由需要 **Gemini Advanced** (付费订阅). 免费 Google 账号的 cookie 可以登录认证, 但会静默回退到 Flash.

## 配置文件

在同目录创建 `config.json`:

```json
{
  "port": 8081,
  "host": "0.0.0.0",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "api_key": null,
  "api_keys": [],
  "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
  "default_model": "gemini-3.6-flash",
  "auth_user": null,
  "xsrf_token": null,
  "admin_password": null,
  "cookie_file": null,
  "proxy": null,
  "log_requests": true,
  "temporary_chats": false
}
```

## API Key 鉴权

默认不启用鉴权，兼容旧行为。配置密钥后，`/v1/*` 和 `/v1beta/*` 均需要鉴权；支持 `Authorization: Bearer <key>`、`x-api-key`、`x-goog-api-key`，以及 Gemini CLI 使用的 `?key=<key>` 查询参数。

```http
Authorization: Bearer 你的密钥
```

**方式 1: 环境变量**

```bash
export GEMINI_WEB2API_API_KEY=你的密钥
python gemini_web2api.py
```

也兼容：

```bash
export API_KEY=你的密钥
```

**方式 2: config.json**

```json
{"api_key": "你的密钥"}
```

也支持多个密钥：

```json
{"api_keys": ["密钥1", "密钥2"]}
```

客户端 API Key 填你的密钥即可。

将 `temporary_chats` 设置为 `true` 后，请求会使用 Gemini 网页版的临时聊天，
不会将对话保存在账号历史记录中。

当 `api_keys` 为空数组、`api_key` 为 `null`，且管理后台密钥库也为空时，不校验密钥。

## 管理后台与节点池

访问 `/admin/`，使用 `admin_password` 登录。若未配置，服务首次启动会自动生成密码并打印一次；环境变量 `GEMINI_WEB2API_ADMIN_PASSWORD` 可覆盖保存值。

本 fork 的管理后台可管理 API 密钥、运行参数、Clash 兼容订阅、节点健康状态和每节点 Mihomo worker。Docker 镜像内置 Mihomo 与 Go `tls-client` helper；导入的节点会按健康状态选择和重试，同时让代理访问 Gemini 时保持浏览器风格的 TLS 行为。

## Docker 部署

```bash
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8080:8080 -e GEMINI_WEB2API_API_KEY=sk-your-key gemini-web2api
```

或使用 Docker Compose:

```bash
mkdir -p data
cp config.example.json data/config.json
docker compose -f docker-compose.local.yml up -d
```

如需挂载 Cookie 文件:

```bash
mkdir -p data
cp config.example.json data/config.json
docker run -d --name gemini-web2api -p 8080:8080 \
  -v "$PWD/data:/app/config" \
  -v "$PWD/cookie.txt:/app/config/cookie.txt:ro" \
  gemini-web2api
```

此时在 `data/config.json` 中设置 `"cookie_file": "/app/config/cookie.txt"`。

Zeabur Docker 部署时暴露容器端口 `8080`，将持久卷挂载到 `/app/config`，再设置需要的环境变量，例如 `GEMINI_WEB2API_API_KEY`。

> **注意**: 如果 Docker 默认 bridge 网络下出现空回复 (`content: null`), 请切换到 host 网络: `docker run --network host ...` 或在 compose 文件中添加 `network_mode: host`. 这是 Gemini 上游拒绝来自 Docker NAT IP 段的请求导致的.

## 代理配置

如果无法直接访问 `gemini.google.com` (连接超时), 需要配置代理:

**方式 1: 命令行参数**
```bash
python gemini_web2api.py --proxy http://127.0.0.1:7890
```

**方式 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**方式 3: 环境变量** (自动检测)
```bash
set HTTPS_PROXY=http://127.0.0.1:7890
python gemini_web2api.py
```

支持 Clash, V2Ray, Shadowsocks 等任何 HTTP 代理.

## 图片输入

Chat Completions 和 Responses API 支持 OpenAI 风格的多模态消息。图片可以使用
HTTP(S) URL 或 base64 data URL:

```python
resp = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        ]
    }]
)
```

## 已知限制

- **图片上传可能需要 Cookie**: 多模态输入使用 Gemini 网页端图片上传接口。匿名上传失败时, 请配置 Gemini cookie。
- **Pro/Ultra 非真实路由**: 无付费订阅 cookie 时, `gemini-3.1-pro` 实际路由到 Flash 模型. "Pro" 只是 UI 偏好标签.
- **单轮对话**: 每次请求是独立对话, 多轮上下文通过在 prompt 中包含历史消息模拟.
- **频率限制**: Google 可能限制高频请求, server 会自动重试但持续高负载可能被封.

## 系统要求

- Python 3.8+
- `httpx` (`pip install httpx`) — 用于流式请求
- `PyYAML` — 用于 Clash 订阅及 Mihomo 节点配置
- 仅使用托管节点池时需要 Mihomo；Docker 镜像已内置
- 需要能访问 `gemini.google.com` (部分地区需代理)

## 工作原理

逆向 Google Gemini 网页端的 StreamGenerate 协议, 将 OpenAI API 格式与 Gemini 内部 protobuf-like 格式互转. 模型选择通过请求 payload 的 `[79]` 字段控制, 映射自 Gemini 前端 JS 源码中的 `MODE_CATEGORY` 枚举.

## 致谢

- [linux.do](https://linux.do) 社区
- 开源 API 代理生态

## License

MIT
