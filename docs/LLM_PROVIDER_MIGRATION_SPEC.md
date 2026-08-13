# MD Reader 直接 LLM 接入迁移执行规范

> 文档状态：✅ **迁移已完成**（2026-08-13，commit `222501b`，已推送 GitHub main）。
> 本文件保留为决策记录与验收门槛；第 2 章的"当前状态"描述的是迁移前基线。
>
> 制定日期：2026-08-12
>
> 适用仓库：MD Reader
>
> 执行对象：迁移的执行记录；后续改动以实际代码与架构文档为准

## 0. 文档权威性与使用方式

本文件定义 MD Reader 从 **OpenCode 子进程 + 免费模型** 迁移到
**用户填写 API 基础地址与 API Key 的直接 LLM 接入** 的目标状态、实施顺序、
安全边界、测试要求和最终验收门槛。

执行者必须先完整阅读：

1. `AGENTS.md`
2. `.ai/WORKING.md`（实时状态，gitignore 本地文件）
3. `docs/ARCHITECTURE.md`
4. `docs/DESIGN_SPEC.md`
5. **本文件**
6. `git status --short` 与最新提交

如果上述文档中的 OpenCode 设计与本文件冲突：

- **当前代码行为**仍以仓库实际代码和原有架构文档为准；
- **本轮迁移目标**以本文件为准；
- 执行第一阶段必须同步更新架构、设计、README 和实施计划，不能让“代码已改、
  文档仍宣称使用 OpenCode”的状态进入收尾。

本文件使用以下规范词：

- **MUST / 必须**：不满足即不得宣布完成。
- **MUST NOT / 严禁**：触犯即视为安全或产品回归。
- **SHOULD / 应当**：除非有明确、记录在案的技术证据，否则必须执行。
- **MAY / 可以**：可选增强，不得阻塞核心交付。

---

## 1. 决策摘要

### 1.1 新的产品决策

MD Reader 的 AI 助手不再依赖 OpenCode，也不再限定免费模型。目标是：

1. 用户在应用内填写 **API 基础地址** 和 **API Key**；
2. 应用通过 OpenAI 兼容协议直接请求 LLM；
3. 用户点击“获取模型”后，应用使用当前未保存的地址和密钥请求模型列表；
4. 用户可从可搜索的模型列表中选择模型，也可在模型列表接口不可用时手动填写模型 ID；
5. AI 问答、流式显示、选区上下文、修改提案、diff 审批、原子写入和撤销继续工作；
6. 未配置 AI、无网络、系统密钥环不可用或服务端故障时，Markdown 阅读功能必须完整可用。

### 1.2 第一版协议边界

第一版 **只承诺 OpenAI-compatible API**：

- 模型列表：`GET {api_base_url}/models`
- 对话：`POST {api_base_url}/chat/completions`
- 鉴权：`Authorization: Bearer <API Key>`
- 流式协议：OpenAI 风格 Server-Sent Events（SSE）
- 非流式兼容：若服务端在 `stream=true` 时直接返回标准 JSON completion，应用可直接解析，
  但不得为了兼容而自动重发一次可能产生重复计费的请求。

`api_base_url` 是**版本根地址**，例如：

```text
https://api.openai.com/v1
https://openrouter.ai/api/v1
https://example-provider.invalid/v1
http://127.0.0.1:8000/v1
```

第一版不承诺原生 Anthropic Messages、Google Gemini、AWS Bedrock、Azure 特殊部署路径、
OAuth、厂商私有签名协议或任意“只要有 URL 就能用”的协议。未来扩展必须通过 provider
adapter 增加，不能把厂商判断散落在窗口或 widget 中。

### 1.3 CC Switch 参考边界

本方案参考了 CC Switch 在 2026-08-12、提交
`58d92e5616a203f180b2fd63232a3c95106265f9` 中的“填写地址和密钥后获取模型”交互与
OpenAI-compatible `GET /models` 思路。

只参考产品能力和错误分类，**不照搬其多供应商表单、OAuth、代理、厂商预设或兼容后缀猜测**。
MD Reader 第一版必须保持范围小、行为确定、易测试：

- 默认模型地址严格由版本根地址追加 `/models`；
- 高级设置允许填写一个同源的“模型列表地址”作为精确覆盖；
- 不维护按域名或厂商名称匹配的隐藏规则表；
- 不静默把 API Key 发送到不同 origin 的候选地址。

---

## 2. 当前状态与迁移差距

> **本节为迁移前基线（历史记录）**：迁移已完成，`services/opencode.py` 与
> `tests/test_opencode.py` 已删除，目标列（下方"迁移后的主要变化"表格的
> 右侧）即当前实现。

当前实现（迁移前）主要集中在：

- `src/mdreader/services/opencode.py`
- `src/mdreader/window.py`
- `src/mdreader/widgets/ai_panel.py`
- `tests/test_opencode.py`
- `data/io.github.pang.mdreader.gschema.xml` 中的 `opencode-model`
- README、架构、设计和 Flatpak 文档中的 OpenCode 描述

当前行为包括：

- 通过 `opencode run --format json` 启动隔离子进程；
- 通过 OpenCode session ID 保持多轮对话；
- 通过 `opencode models` 获取并过滤免费模型；
- UI 直接根据 `shutil.which("opencode")` 判断 AI 是否可用；
- 凭据由 OpenCode 管理，MD Reader 不接触 API Key；
- 编辑响应仍由 `PatchService` 校验、预览、确认、原子应用和撤销。

迁移后的主要变化是：

| 领域 | 当前 | 目标 |
|---|---|---|
| 传输 | OpenCode 子进程 JSON 事件 | libsoup 直接 HTTPS/HTTP 请求 |
| 鉴权 | OpenCode 自己管理 | MD Reader 通过 Secret Service 保存密钥 |
| 模型列表 | OpenCode CLI 免费模型 | 用户配置服务的 `GET /models` |
| 会话 | OpenCode session ID | 应用内存中的有界消息历史 |
| 可用性 | 检测 `opencode` executable | 检测有效配置、密钥与运行时依赖 |
| 模型限制 | 只允许免费模型 | 接受服务端返回或用户手填的合法模型 ID |
| 安全隔离 | deny-all agent + 临时目录 | 无工具的纯 HTTP 请求 + 原有 PatchService 写入边界 |

**必须保留的既有能力：**

- 选区、标题、相对文件路径和行号上下文；
- 原始 Markdown 内容被视为不可信引用，不得覆盖系统指令；
- Ask 与 Edit 两种模式；
- Edit 只允许精确替换当前选中行；
- diff 预览、显式接受、冲突检测、原子写入和撤销；
- AI 回答的本地安全 Markdown 渲染；
- 中文输入法稳定性；
- 640/960/1280/1920 Niri 自适应布局；
- AI 不可用时阅读器不降级。

---

## 3. 范围与非目标

### 3.1 本轮必须完成

- 移除运行时 OpenCode 依赖；
- 新增 OpenAI-compatible 直接 HTTP transport；
- 新增 API 连接设置 UI；
- 新增 Secret Service 密钥存储；
- 新增模型列表获取、解析、搜索、选择和手动模型 ID；
- 新增流式 SSE 与标准 JSON completion 解析；
- 新增应用内存中的有界多轮对话；
- 迁移现有 Ask/Edit 流程；
- 更新错误状态、测试、安装依赖和用户文档；
- 删除或废弃所有会误导用户的 OpenCode 文案和运行时代码。

### 3.2 本轮明确不做

- 多供应商账号管理器或 CC Switch 克隆；
- 同时保存多个可切换连接配置；
- OAuth 登录；
- Anthropic/Gemini/Bedrock/Azure 私有协议适配；
- 工具调用、函数调用、MCP、联网搜索或仓库自动检索；
- 后台自动探测供应商；
- 启动应用时自动联网；
- 自动测速、价格查询、余额查询或模型能力数据库；
- 远程模型对工作区的直接文件访问；
- 绕过现有 diff 审批边界的任何写入；
- 在本轮把 Flatpak 设为首发交付方式。

---

## 4. 不可违反的硬规范

### 4.1 凭据规范

> **产品决策注记（2026-08-13，项目负责人）**：放宽第 2 条——"能连上 API 即可，不
> 搞守护进程"。运行时以非激活探测（D-Bus `NameHasOwner`，不触发自动激活）判断
> `org.freedesktop.secrets` 是否有持有者；无持有者时应用回退到**会话内存密钥存储**
> （InMemorySecretStore），密钥不落盘、重启后要求重新输入。第 1、3 条不变：任何
> 情况下严禁明文持久化密钥，禁用 Secret Service 时也绝不静默明文保存。

1. API Key **严禁**写入 GSettings、普通文件、缓存、日志、异常消息、命令行参数、
   环境变量、测试快照或截图。
2. 持久化密钥必须使用 `libsecret` / Secret Service；无 Secret Service 持有者时按
   上方注记回退到会话内存存储（不持久化）。
3. Secret Service 不可用时，严禁静默回退到明文保存。
4. 已保存的密钥不得重新显示在 UI 中；编辑设置时密码框保持空白。只有草稿仍与已保存
   profile 同 origin 且选择 `bearer` 时，才显示“留空则继续使用已保存密钥”。origin 改变后，
   `bearer` 模式必须显示“请重新输入 API Key”；若规范化地址为 loopback，用户也可显式选择
   “无需鉴权”。不得留下误导性复用提示，也不得由空密码框隐式推断 `none`。
5. **留空复用旧 key 只允许发生在同一 origin。** 当前草稿规范化后的 scheme、host、
   effective port 必须与已保存 profile 完全一致；一旦用户修改 scheme、host 或 port，
   “获取模型”和“保存连接”都不得复用旧 key：新的 bearer 连接必须重新输入 key，新的 loopback
   连接只可由用户明确采用 `auth-mode=none`。严禁把旧 provider 的 key 静默发送到新 origin。
   仅修改同源 path 或模型 ID 时可以继续复用，但仍须通过完整 URL policy 校验。
6. URL 中严禁包含用户名或密码；发现 `https://user:pass@host/...` 必须拒绝。
7. API Key 只放在 HTTP `Authorization` header；不得拼入 URL、query 或请求正文。
8. 默认日志不得记录完整 request/response body 或 headers。诊断层必须按当前 key、
   `Authorization: Bearer ...` 形态和 URL userinfo 进行统一脱敏；服务端错误正文即使回显
   当前 key，也必须在进入异常、日志和 UI 之前脱敏。
9. 测试必须使用明显的假密钥，并验证该假密钥没有出现在日志或持久化元数据中。
10. Python 字符串无法可靠做内存清零；实现不得虚假承诺“密钥已从内存彻底擦除”。要求是
    缩短引用生命周期、完成后释放引用，并避免不必要复制和持久化。

### 4.2 网络规范

1. 非 loopback 地址必须使用 HTTPS。
2. HTTP 只允许主机精确为 `localhost`（ASCII 大小写不敏感），或经 `ipaddress` 等标准库
   **按规范字面量解析**后确认属于 loopback 的 IPv4/IPv6 地址；拒绝模糊数字 IPv4、IPv6
   zone ID、反斜杠和非法端口。普通域名即使 DNS 恰好解析到 loopback，也不得因此允许 HTTP。
3. loopback HTTP 请求必须绕过环境/系统代理，避免本地请求与 Authorization 被代理转发；
   proxy bypass 是 transport 集成责任，不得依赖用户恰好配置了 `NO_PROXY`。远程 HTTPS 是否遵循
   系统代理保持桌面默认行为，但首版不提供应用内自定义代理或代理凭据 UI。
4. 严禁提供“忽略 TLS 证书错误”开关。
5. 模型列表覆盖地址必须与 API 基础地址同 scheme、host、effective port；默认端口与显式默认端口
   视为相同 origin，否则拒绝，避免密钥泄漏。
6. 必须给每个 Soup message 设置 `Soup.MessageFlags.NO_REDIRECT` 或等价 no-redirect 行为，
   **禁止 libsoup 自动携带 Authorization 跟随重定向**。客户端只可手动读取 `Location`，每一跳
   重新解析并执行完整 URL、安全协议和同源校验；只允许原始 origin 内最多 3 跳。跨 origin、
   HTTPS 降级为 HTTP、缺失/非法 `Location` 或超过跳数时立即拒绝，Authorization 永不跨 origin。
7. body 上限按**解压后的实际读取字节**执行，不能只相信 `Content-Length`。
8. 模型列表响应体上限 2 MiB；chat 的 pre-parser response bytes、单个 SSE event、单个
   `data:` line 和 Ask/Edit UTF-8 text 必须分别设限。Ask 上限 2 MiB，Edit 上限 256 KiB；
   具体流式上限见第 8.4 节。
9. 模型获取总超时 20 秒；对话连接超时 15 秒；流式空闲超时 90 秒；单次对话硬上限
   10 分钟。超时必须可取消并给出明确状态。
10. 首版不做隐藏自动重试。尤其对话请求一旦发出，严禁自动重发，以免重复响应或重复计费。
11. 不得在 GTK 主线程执行阻塞 DNS、网络、Secret Service 或文件 I/O。

### 4.3 AI 与文件安全规范

1. 直接 LLM transport 没有工具接口，也不获得工作区根目录。
2. 请求只能包含相对文件路径、受限 excerpt、标题、选区和用户问题。
3. 严禁发送绝对工作区路径。
4. Edit 请求只能返回当前选区的 replacement JSON；不得让模型指定任意文件路径。
5. `PatchService` 仍是唯一写入入口；widget 和 LLM service 严禁直接写文件。
6. 原有 canonical workspace、symlink escape、source hash、stale proposal、atomic replace、
   line-ending preservation 和 Undo 规则全部保留。
7. 模型输出永远是不可信输入。Markdown、JSON、链接和错误正文都必须经过现有或新增的
   安全解析与长度限制。

### 4.4 架构与线程规范

1. GTK 调用只发生在主线程。
2. widget 不发网络请求、不访问 Secret Service、不读取 GSettings 原始细节。
3. `window.py` 只协调服务和 UI 状态，不实现 URL 拼接、SSE 解析或密钥存储。
4. 网络请求必须支持 `Gio.Cancellable` 或等价的明确取消机制。
5. 每个异步操作必须携带 generation/request token，旧请求回调不得覆盖新配置或新文档状态。
6. “查询失败”和“成功但返回空列表”必须是两个不同结果，严禁把失败报告为“没有模型”。
7. 不能通过吞异常、放宽测试或把错误改成空结果来制造通过状态。

---

## 5. 目标架构

### 5.1 总体数据流

```text
AiPanel
   │ 用户发送 Ask/Edit
   ▼
MdReaderWindow coordinator
   ├── ContextBuilder ──> 受限上下文 envelope
   ├── ConversationState ──> 有界内存历史（Ask only）
   ├── AiSecretStore ──> Secret Service 中的 API Key
   ├── OpenAICompatibleGateway
   │      ├── EndpointPolicy
   │      ├── ModelCatalogClient ──> GET /models
   │      └── ChatCompletionsClient ──> POST /chat/completions
   └── PatchService ──> diff 审批 ──> 原子写入 / Undo
```

### 5.2 建议模块边界

文件名可以在实现时微调，但职责必须保持分离：

```text
src/mdreader/
├── models/
│   ├── conversation.py        现有上下文与消息模型
│   └── ai.py                  AiProfile、AiModel、AiErrorCode、AiRequest
├── services/
│   ├── ai_endpoints.py        URL 验证、规范化、端点构造、同源判断
│   ├── ai_secrets.py          libsecret 保存/读取/清除
│   ├── ai_models.py           /models 请求与响应解析
│   ├── ai_stream.py           增量 UTF-8、SSE、标准 JSON completion 解析
│   ├── llm.py                 provider-neutral gateway 接口与 OpenAI-compatible 实现
│   ├── context.py             保持现有受限上下文构造
│   ├── patches.py             保持唯一写入边界
│   └── settings.py            非秘密配置的 typed facade 与迁移
├── widgets/
│   ├── ai_panel.py            对话、上下文 rail、状态与 composer
│   └── ai_connection_dialog.py 连接配置、获取模型、搜索与选择
└── window.py                  生命周期、状态协调、取消与 generation
```

不得为了“少建文件”把所有网络、解析、密钥和 UI 逻辑塞进 `window.py` 或
`ai_panel.py`。

### 5.3 核心数据模型

建议使用不可变 dataclass：

```python
from dataclasses import dataclass, field

@dataclass(frozen=True, slots=True)
class AiProfile:
    profile_id: str
    provider_kind: str          # 首版固定 "openai-compatible"
    api_base_url: str           # 已规范化，不含结尾斜杠
    models_url: str             # 空串表示默认 {base}/models
    model_id: str
    auth_mode: str              # "bearer" 或仅 loopback 可用的 "none"

@dataclass(frozen=True, slots=True)
class AiModel:
    model_id: str
    owned_by: str = ""

@dataclass(frozen=True, slots=True)
class AiConnectionDraft:
    api_base_url: str
    models_url: str
    api_key: str = field(repr=False, compare=False)
    auth_mode: str = "bearer"  # "none" 仅允许用户对 loopback 明确选择
    model_id: str = ""
    keep_existing_secret: bool = False  # coordinator 必须从 origin policy 推导，不能盲信 UI
```

`keep_existing_secret` 不是权限凭据，只是派生意图；service 必须重新比较规范化 origin 后才允许
lookup 旧 secret，不能因为调用者传入 `True` 就复用。由于 `api_key` 使用 `compare=False`，禁止用
`AiConnectionDraft` 相等性判断 key 是否变化；password entry 的每次变更必须推进独立、单调的
key revision。`AiProfile` **不得**包含 API Key。
`AiConnectionDraft`、request wrapper、exception 及其嵌套对象的
`repr`/`str` 不得暴露 key；不得把 draft 交给 `dataclasses.asdict()` 后记录，也不得记录完整
headers 或请求正文。任何诊断字段必须使用显式 allowlist 构造，而不是事后假设通用 logger 会脱敏。

### 5.4 错误分类

必须提供稳定错误码，UI 文案不能依赖解析英文异常字符串：

```text
NOT_CONFIGURED
INVALID_URL
INSECURE_REMOTE_URL
CROSS_ORIGIN_MODELS_URL
REDIRECT_REJECTED
AI_RUNTIME_UNAVAILABLE
SECRET_SERVICE_UNAVAILABLE
SECRET_NOT_FOUND
AUTHENTICATION_FAILED
PERMISSION_DENIED
ENDPOINT_NOT_FOUND
RATE_LIMITED
TIMEOUT
TLS_FAILED
NETWORK_FAILED
PROVIDER_UNAVAILABLE
REQUEST_REJECTED
INVALID_RESPONSE
RESPONSE_TOO_LARGE
STREAM_ENDED_EARLY
CANCELLED
MODEL_NOT_SELECTED
BILLING_OR_QUOTA_REQUIRED
SETTINGS_WRITE_FAILED
CLEANUP_INCOMPLETE
BUSY
```

底层异常可保留调试原因，但用户可见消息必须经过安全映射，且不得包含 API Key 或整页 HTML。

---

## 6. URL、鉴权与端点规范

### 6.1 API 基础地址

规范化步骤必须是纯函数并有单元测试：

1. 去除首尾空白；
2. 长度必须为 1–2048 个字符；
3. 拒绝控制字符、换行和 NUL；
4. 使用严格 URI parser 解析；解析前拒绝反斜杠，解析后拒绝非法/超范围端口、IPv6 zone ID、
   模糊数字 IPv4 和任何 parser 不能往返保持语义的 authority；
5. scheme 仅允许 `https`，或第 4.2 节定义的精确 loopback 主机上的 `http`；
6. host 必须存在；DNS 解析结果不得参与“是否允许 HTTP”的判断；
7. 拒绝 userinfo、query 和 fragment；
8. 保留合法路径，删除结尾 `/`；
9. 如果路径已经以 `/models`、`/chat/completions` 或 `/responses` 结束，拒绝并提示用户填写
   “版本根地址”，不要静默猜测；
10. host 按 URI 规则规范化，origin 使用 `(scheme, normalized_host, effective_port)` 比较；
    `https:443`/省略 443 与 `http:80`/省略 80 各自视为同一 origin；
11. 规范化后的地址用于显示、持久化和同源比较。

高级“模型列表地址”使用独立校验入口：它表示一个**精确 `/models` endpoint**，允许自身包含
最终 endpoint path，但仍拒绝 userinfo、query、fragment、不安全协议及跨 origin。不得把 API base
和 explicit models URL 混用同一条“版本根地址”规则，否则会误拒绝合法覆盖或接受完整 chat URL。

API Key 与 auth mode 校验同样必须是纯逻辑：远程地址强制 `bearer` 且要求 1–8192 个字符；
loopback 只有在用户明确选择 `none` 时才允许空 key，`bearer` 仍要求新 key 或同 origin 的已保存
secret。拒绝 key 首尾空白、控制字符、换行和 NUL，不得静默修剪或改写用户输入。
用户问题最多 8,000 个 Unicode 字符，完整序列化请求正文上限 128 KiB；超过时在本地拒绝，
不得先发出再等待服务端拒绝。

### 6.2 端点构造

```text
chat_url   = append_path_segments(parsed_api_base, ("chat", "completions"))
models_url = validated_explicit_models_url or append_path_segments(parsed_api_base, ("models",))
```

上述伪代码表达的是**基于解析后 URI 组件的确定性构造**，不是对未解析用户字符串执行
`urljoin()`、正则替换或供应商后缀猜测。构造后必须再次确认 scheme、host、effective port、
userinfo、query 与 fragment 不变量；路径只能在规范化 base path 后追加固定段。

示例：

| 输入 | 对话地址 | 模型地址 |
|---|---|---|
| `https://api.example/v1` | `https://api.example/v1/chat/completions` | `https://api.example/v1/models` |
| `https://router.example/api/v1` | `https://router.example/api/v1/chat/completions` | `https://router.example/api/v1/models` |
| `http://127.0.0.1:8000/v1` | `http://127.0.0.1:8000/v1/chat/completions` | `http://127.0.0.1:8000/v1/models` |

首版不得自动追加或删除厂商专用的 `/anthropic`、`/coding`、`/claude` 等路径。
如果模型接口与对话接口路径不同，用户通过高级设置填写精确的同源模型列表地址。

### 6.3 请求 header

模型列表：

```http
GET /v1/models
Accept: application/json
Authorization: Bearer <key>
User-Agent: MDReader/<version>
```

对话：

```http
POST /v1/chat/completions
Accept: text/event-stream, application/json
Content-Type: application/json
Authorization: Bearer <key>
User-Agent: MDReader/<version>
```

只有规范化地址为 loopback 且用户显式选择 `auth-mode=none` 时，API Key 才可为空，且不得发送
Authorization header。loopback 若选择 `bearer`，仍必须使用本次输入的新 key 或同 origin 的已保存
secret；不得根据密码框为空自动切换为 `none`。非 loopback 配置必须使用 `bearer`，并在保存和发送前
解析出有效密钥。

### 6.4 重定向请求算法

模型列表和 chat completion 共用同一手动重定向器：

1. 每次创建 message 都设置 no-redirect，且只在当前跳即将发送时附加 Authorization；loopback
   HTTP 必须使用明确禁用 proxy resolver 的 Session/transport，不能仅依赖 `NO_PROXY` 环境变量；
2. 只有 301、302、303、307、308 进入 redirect 分支；300、304、305、306 及未知 3xx 直接
   `REDIRECT_REJECTED`。收到允许处理的状态后不复用已发送的 message；读取并限制 `Location`
   长度，再相对当前 URI 解析；
3. 对解析结果重新执行本节全部 URL policy，并与**最初请求 origin**比较；
4. 只有同 origin 且没有 HTTPS→HTTP 降级时才创建下一条新 message，最多跟随 3 跳；
5. 跨 origin、非法/缺失 Location、协议降级或第 4 个 redirect 返回安全错误，不发送下一跳；
6. 对 chat 的 301/302/303 不得把 POST 静默改成 GET；首版应拒绝会改变方法语义的 redirect，
   只有能保持 method/body 的 307/308 才可按上述规则继续；重放仍计入同一个用户请求，不得触发
   transport 层之外的重试。

测试必须捕获每一跳实际收到的 headers，证明跨源 listener 从未收到 Authorization，而不是只断言
最终 UI 显示了错误。

---

## 7. 模型列表功能规范

### 7.1 用户流程

1. 用户打开“AI 连接设置”；
2. 输入 API 基础地址；
3. 选择鉴权模式并输入 API Key；仅当选择 `bearer` 且当前草稿与已保存 profile **同 origin** 时，
   留空才表示继续使用已保存密钥。origin 改变后，`bearer` 必须输入新 key；仅对 loopback 可显式
   选择“无需鉴权”；
4. 点击“获取模型”；
5. UI 立即进入 loading 状态，按钮变为“停止获取”，显示原生 spinner；
6. service 使用**当前表单草稿**，而不是旧的已保存配置；
7. 成功后显示模型数量并打开/启用可搜索模型选择器；
8. 用户选择模型或手动输入模型 ID；
9. 点击“保存连接”；
10. 密钥与非秘密元数据完成事务式保存后，AI 面板进入可用状态。

“获取模型”只做读取，不保存设置，也不发送任何推理/聊天请求。应用不得承诺第三方服务
一定不会对管理 API 计费；它只能保证自己调用的是模型列表接口。

若用户改动 API base、API Key、auth mode 或 explicit models URL，dialog 必须立即增加 draft revision，
取消底层请求；若取消无法及时完成，则至少废弃其结果。旧模型结果必须标记为 stale，不得作为新草稿的
已验证目录继续保存。若旧列表中选中的 ID 仍留在输入框，只能降级为“未验证的手动 ID”，不能继续
显示为新 endpoint 的已获取结果。
回调只有在 `dialog_generation` 与草稿 fingerprint 同时匹配时才能应用。fingerprint 基于规范化 URL、
models URL、auth mode、key source 枚举和独立单调 key revision 计算；它只能包含非秘密字段与
revision，**不得包含、散列、序列化或记录 key**。

当草稿 origin 不再等于已保存 origin 时，留空密码框必须变为“需要重新输入 API Key”；若目标是
loopback，可由用户明确选择“无需鉴权”。两种情况下获取模型和保存连接都不得查取旧 secret。

### 7.2 `/models` 响应格式

首版支持标准 OpenAI-compatible 形态：

```json
{
  "object": "list",
  "data": [
    {"id": "model-a", "owned_by": "provider"},
    {"id": "model-b"}
  ]
}
```

解析规则：

- 根必须是 JSON object；
- `data` 必须是数组；
- 每个有效条目必须是 object 且 `id` 为 string；
- model ID 去除首尾空白后长度 1–256；
- 拒绝含 Unicode `Cc`/`Cf` 类字符、任意空白、换行或 NUL 的 ID，避免不可见字符与
  双向文本欺骗；
- `owned_by` 可缺失，非 string 时按空值处理；
- 无效条目跳过，但如果所有条目都无效，返回 `INVALID_RESPONSE`，不是空成功；
- 精确 ID 去重；
- 最多接收 2000 个模型，超过即返回 `INVALID_RESPONSE` 或明确截断错误，不得静默吞掉；
- 展示按 Unicode casefold 后稳定排序，实际请求仍使用原始 ID；
- `data: []` 是**成功但空列表**，UI 提示“服务返回了 0 个模型，可手动填写模型 ID”；
- HTTP、超时或 JSON 失败绝不能转换为空列表。

### 7.3 模型选择规则

- 获取模型不是发送聊天的强制前置条件；服务不支持 `/models` 时允许手动输入模型 ID。
- 手动 ID 使用与远程模型相同的长度和控制字符校验。
- 已保存模型不在新列表中时：保留原值、显示“此模型未出现在本次结果中”，不得静默切换到第一项。
- 响应生成期间禁止切换模型或保存新的连接配置。
- 模型切换成功后清空应用内存对话历史，并在 UI 明确提示“已切换模型，将开始新对话”。
- 不再出现“免费模型”过滤或 `opencode/` 前缀规则。

### 7.4 模型获取错误文案

必须区分至少以下情况：

| 条件 | 用户状态 |
|---|---|
| 地址和密钥都缺失 | “请先填写 API 地址和密钥” |
| 仅地址缺失 | “请填写 API 基础地址” |
| 远程地址密钥缺失 | “请填写 API Key” |
| 401/403 | “API Key 无效或无权读取模型列表” |
| 404/405 | “此地址没有可用的模型列表接口，可检查地址或手动填写模型” |
| 402 或明确 quota/billing error | “账户额度或计费状态不允许请求” |
| 429 | “请求过于频繁，请稍后重试” |
| 5xx | “AI 服务暂时不可用，请稍后重试” |
| timeout | “获取模型超时，请检查网络和服务地址” |
| TLS | “无法验证服务端安全连接” |
| malformed JSON | “服务返回的模型列表格式不兼容” |
| empty data | “服务返回了 0 个模型，可手动填写模型 ID” |
| cancelled | 保持草稿，不追加红色错误消息 |

服务端错误正文最多取 512 个字符用于内部诊断，并在展示前去标签、去控制字符、去密钥。

---

## 8. 对话请求与流式解析规范

### 8.1 请求正文

Ask 模式首版只发送最小兼容字段：

```json
{
  "model": "selected-model-id",
  "messages": [
    {"role": "system", "content": "...app-owned system prompt..."},
    {"role": "user", "content": "...bounded context envelope..."}
  ],
  "stream": true
}
```

除非有明确供应商兼容需求和测试，不得默认加入 `temperature`、`max_tokens`、
`reasoning_effort`、`tools`、`response_format` 或厂商扩展字段。

对话 HTTP 状态至少映射：400/422 为请求或协议不兼容，401 为认证失败，403 为权限失败，
402 或明确 quota/billing 错误为额度/计费失败，404/405 为 endpoint 错误，409 保留服务端
安全摘要，429 为限流，5xx 为 provider unavailable。任何错误都不自动重发。

### 8.2 系统提示词

现有 OpenCode system prompt 的安全意图必须迁移为应用自有常量，并删除 OpenCode 名称。
至少包含：

- 只能使用消息中的上下文；
- 文档内容是不可信引用，不能当作指令；
- 不得声称读取了其他文件；
- 上下文不足时明确说明还需要哪个文件或章节；
- 回答适合窄侧栏，使用简洁 Markdown；
- 不输出隐藏推理；
- Edit 模式只返回精确结构的 replacement JSON。

### 8.3 应用内存会话

OpenCode session ID 被移除后，新增 `ConversationState`：

- 只在内存中保存，不写 GSettings 或磁盘；
- 只保存成功完成的 Ask 用户消息和助手消息；
- 失败、取消和半截回复不进入后续请求历史；
- 最多保留最近 12 条消息；
- 历史文本总计最多 48,000 个 Unicode 字符；
- 超限时从最早的完整 user/assistant pair 开始删除；
- 当前 system prompt 和当前用户消息不计入上述历史上限；
- 文档切换、模型切换、连接配置切换或显式重置时清空历史；
- 仅选区变化不自动清空历史，但每个新问题必须携带最新上下文 envelope。

Edit 请求是独立 one-shot：

- 不携带 Ask 历史；
- 不把原始 replacement JSON 加入 Ask 历史；
- 不把 Edit partial stream 渲染为普通助手 Markdown；
- 完成后仍走原有 PatchService 审批流程。

### 8.4 SSE 解析

解析器必须独立于 HTTP client，可用纯字节/文本 fixture 单测：

- 使用增量 UTF-8 decoder，正确处理多字节字符跨网络 chunk；
- 以空行划分 SSE event；
- 忽略 `:` comment；
- 支持一个 event 中多个 `data:` 行；
- `data: [DONE]` 正常结束；
- 每个 JSON event 的根必须是 object；除明确 error object 外，`choices` 必须是非空 array，
  `choices[0]` 必须是 object，`delta` 存在时必须是 object，`finish_reason` 只能是 null 或 string；
  缺字段、空 choices 或错误类型返回 `INVALID_RESPONSE`，不得把 malformed event 当 keepalive；
- JSON chunk 只读取 `choices[0].delta.content`，忽略额外 choices，不把多个 choice 拼接；
- `content` 为 `null` 时忽略；首版只接受 string content，数组或 object content 返回
  `INVALID_RESPONSE`；
- 忽略 role-only chunk；
- 不显示 `reasoning_content`、隐藏思维或未知扩展字段；
- 遇到明确 error object 立即失败；
- `finish_reason=stop` 表示完整完成；Ask 可把结果加入历史，Edit 才能继续生成 diff；
- 一旦收到任何非 `null` finish reason，本次完成判定即锁定；后续互相矛盾的 finish reason、
  finish 后新增正文或多个 choice 混淆均返回 `INVALID_RESPONSE`，不得用稍后的 `stop` 覆盖 partial；
- `finish_reason=length|content_filter|tool_calls|function_call` 等非 `stop` 值保留 Ask partial
  文本并显示明确未完成状态，但不加入历史；Edit 直接失败且不得生成 diff；
- 收到 `[DONE]` 但未收到 finish reason 时，为兼容部分服务，仅在正文非空且此前没有错误或
  partial finish reason 时视为明确兼容成功；
- 未收到 `[DONE]`、任何 `finish_reason` 或合法 JSON completion 就 EOF，返回
  `STREAM_ENDED_EARLY`；
- 第一个非空、非 `[DONE]` 且无法解析为 JSON 的 `data` event 立即返回
  `INVALID_RESPONSE`；不得跳过坏 event 并最终伪装成功；
- 空响应、只有 `[DONE]`、只有 role/reasoning/未知字段或从未产生正文的响应均不得算成功；
- 增量 parser 必须分别执行以下上限，任一超限立即取消底层请求并返回
  `RESPONSE_TOO_LARGE`：Content-Encoding 解码后、SSE/JSON parser 前的 pre-parser response bytes
  8 MiB；单个尚未以空行结束的 SSE event 256 KiB；单个 `data:` line 256 KiB；Ask 展示正文
  UTF-8 text 2 MiB；Edit 收集正文 UTF-8 text 256 KiB；
- comment、reasoning 字段、未知 JSON 字段和 malformed event 的字节也计入 pre-parser response、
  SSE event 与 `data:` line 对应上限，不能通过“不展示”绕过限制；增量 UTF-8 decoder、行缓冲和
  event 缓冲不得无限增长；
- 到达正文上限时不得截断后伪装成功；Ask 可保留已显示 partial 并标记失败，Edit 不得进入 diff。

### 8.5 标准 JSON completion 兼容

若同一个 `stream=true` 请求返回 `application/json`，可解析：

```json
{
  "choices": [
    {"message": {"content": "complete answer"}, "finish_reason": "stop"}
  ]
}
```

JSON completion 与 SSE 共用 8 MiB pre-parser response bytes 上限，并在增量读取时执行，禁止先无限缓冲
再校验。JSON completion 必须严格验证：根为 object；`choices` 为非空 array；`choices[0]` 为 object；
`message` 为 object；`message.content` 为 string 且正文非空；`finish_reason` 为 `stop` 时才是完整
成功。缺字段、错误类型、空正文或空 choices 均返回 `INVALID_RESPONSE`；非 `stop` finish reason
按 partial/失败处理，Ask 不入历史，Edit 不生成 diff。兼容实现不得把 `null`、array 或 object content
隐式字符串化。

不得因为响应不是 SSE 自动再发一次请求。`Content-Type` 缺失或错误时，只允许在不读取超过
4 KiB 前缀的前提下识别明显的 `data:` SSE 或 JSON object；无法可靠识别就返回
`INVALID_RESPONSE`，不得进行第二次网络请求。sniff 前缀计入 pre-parser response bytes 上限；
Content-Type 不符本身绝不是重发理由。

### 8.6 取消与陈旧回调

- 同一时间只允许一个对话请求；
- “停止回答”必须取消底层网络操作；
- 取消后保留已经显示的 partial 文本，并标记为已停止，或按现有产品行为显示明确取消状态；
- 取消结果不进入后续会话历史；
- 关闭窗口、切换文档、替换配置时取消活动请求；
- 旧 generation 的 chunk、done、error 回调全部丢弃；
- 取消不是红色网络错误，不得显示为“服务异常”。

---

## 9. 密钥与设置持久化规范

### 9.1 GSettings 只保存非秘密信息

首版只支持一个活动连接。为了避免多个独立键出现半更新状态，必须用**一个**
`ai-profile` GSettings 值保存完整非秘密 metadata；推荐类型为 `a{ss}`：

```text
profile-id       UUID
provider-kind    openai-compatible
api-base-url     已规范化 URL
models-url       空串或同源精确覆盖
model-id         当前模型
Auth-mode        bearer | none
```

实际 key 名统一使用小写 `auth-mode`。上述表格中的所有值都不是 API Key。
一次 `set_value()` 更新整个 profile，并检查其布尔返回值；不要用多个 `set_string()`
模拟事务。`ai-configured` 不应作为冗余布尔值持久化：

- `bearer` 由完整 profile、model 和可读取 secret 共同决定；
- `none` 只允许 loopback URL，由完整 profile 和 model 决定。

模型目录只保存在内存中，启动时不自动联网，也不把整份模型列表写入 GSettings。

旧 `opencode-model`：

- schema 中保留至少一个兼容版本，标记 deprecated；
- 新代码不再写入；
- 不把旧 OpenCode model ID 自动迁移为新 provider model；
- 不猜测 API 地址或密钥；
- 首次升级后 AI 显示“尚未配置连接”，阅读器正常工作。

### 9.2 Secret Service schema

使用独立 schema，例如：

```text
schema: io.github.pang.mdreader.ai
flags: Secret.SchemaFlags.NONE
attributes:
  application = Secret.SchemaAttributeType.STRING
  profile-id  = Secret.SchemaAttributeType.STRING
stored values:
  application = io.github.pang.mdreader
  profile-id  = <UUID>
label:
  MD Reader AI API Key
```

Secret attribute 中不得包含 API Key。是否包含服务 host 不是必需条件，首版只用 profile ID。

### 9.3 保存事务

保存连接必须按下列顺序执行：

保存不是数据库事务，但必须实现可恢复的两阶段顺序：

1. 校验全部草稿字段；
2. **输入了新 key**：生成新 profile UUID，以新 UUID 先保存 secret；
3. **留空继续使用旧 key**：仅在草稿 origin 与旧 profile origin 完全一致时，才可异步读取并
   确认旧 secret 存在、保留旧 profile UUID；origin 不同则禁止 lookup 和复用，bearer 模式必须要求
   新 key，只有 loopback 可转入第 4 步的显式 `auth-mode=none`；
4. **loopback 显式无需鉴权**：只有用户明确选择 `auth-mode=none` 才进入此分支；生成或保留
   profile UUID，不创建空 secret，也不得把空密码框本身解释为选择了 `none`；
5. 用一次 `Gio.Settings.set_value("ai-profile", ...)` 写入完整 metadata，并检查返回值；
6. metadata 写入成功后激活新 profile、重建 gateway、清空会话；
7. 如果换了 UUID 或切换为 `auth-mode=none`，最后删除旧 profile 的 secret；
8. 第 5 步失败时，删除本次刚写入的新 secret，并保留旧 profile；若回滚删除也失败，必须报告
   “连接未保存，临时密钥清理失败”，不得激活新 profile，并保留该非秘密 UUID 供当前会话重试清理；
9. 旧 secret 删除失败时，报告“新连接已保存，但旧密钥清理失败”，不得称清理成功；后续用户显式
   打开设置时可按 `application` attribute 枚举非活动 profile secret，展示并重试清理，但不得在启动时
   自动弹出 keyring 解锁，也不得删除当前活动 profile 的 secret。

编辑已有 profile 且密码框留空时不得创建空 secret。如果旧 secret 已丢失，保存按钮不得
假装成功，必须要求用户重新输入密钥。远程 URL 不能选择 `auth-mode=none`。同源判定必须使用
规范化 origin；scheme、host 或 effective port 任一变化都会使旧 key 复用失效。

Secret 的 lookup/store/clear 全部必须异步且可取消。不得在模块顶层无条件
`gi.require_version("Soup", ...)` / `gi.require_version("Secret", ...)` 并 import 后让整个阅读器因
可选 AI runtime 缺失而启动失败；依赖探测和 adapter 构造须位于可捕获边界。Soup/Secret typelibs、
Secret Service/keyring 或网络不可用时，只降级 AI 状态，文档阅读、搜索、目录和缩放必须完整工作。
可能弹出 keyring 解锁的操作只允许由用户显式打开 AI 设置、点击获取模型、保存/清除连接或发送消息
触发，应用启动和纯阅读流程不得触发。

### 9.4 清除连接

“清除 AI 连接”是不可撤销安全操作，必须明确确认：

1. 取消活动请求；
2. 异步清除 Secret Service 项；
3. secret 清除成功后，再清除 GSettings profile，并检查写入结果；
4. 两者都成功后清空内存会话和模型列表，AI 面板回到“尚未配置”状态；
5. 如果 secret 删除失败，必须显示失败并保留 profile 供重试，不能说“已删除”；
6. 如果 secret 已删除但 GSettings 清除失败，必须显示明确的 partial-failure：旧 metadata 仍存在、
   secret 已不存在、AI 不可发送，并提供重试清除 metadata 的操作；不得恢复或伪造旧 key，也不得
   把该状态显示为“连接仍健康”。

---

## 10. UI 与交互规范

### 10.1 入口

至少提供两个入口：

- AI 未配置状态页中的“配置 AI 连接”；
- 主菜单中的“AI 连接设置…”或 AI header 的设置 action。

不得要求用户先安装 OpenCode。所有“OpenCode 不可用”“选择 OpenCode 模型”等文案必须迁移。

### 10.2 连接设置对话框

使用 `Adw.PreferencesDialog` / `Adw.Dialog` 的原生模式，不构建网页式卡片。
建议结构：

```text
AI 连接设置

[连接]
API 基础地址      https://…/v1
API Key           ••••••••
                  同一服务地址下留空可继续使用；更换服务不会复用旧密钥
无需鉴权          [开关；只在 loopback 地址可用]

[模型]
模型列表地址      高级设置；默认使用 {base}/models
[获取模型]
当前模型          model-id                 >
                  或“手动填写模型 ID”

[隐私]
发送问题时，当前文档的受限摘录、选区、相对路径和行号会发送到上述服务。

[清除连接]                         [取消] [保存连接]
```

要求：

- API Key 使用原生 password entry；
- 不预填已保存密钥；
- `auth-mode=none` 必须由用户通过“无需鉴权”开关明确选择，只在规范化后确认的 loopback URL
  可用；远程 URL 强制 bearer 并关闭该开关。同 origin 的已保存 bearer profile 中，单纯留空仍表示
  复用旧 key，不得被解释为切换到 none；
- icon-only 控件必须有 tooltip 和 accessible name；
- loading 使用 `AdwSpinner`；
- Save 是唯一 suggested action；
- 清除连接使用 destructive 样式并确认；
- 错误靠近对应 field，网络级错误使用 banner 或持久状态，不只闪一个瞬时 toast；
- 关闭 dialog 时取消模型获取并清空未保存 key；
- 640px 宽度、200% 文本缩放和高对比模式无裁切。

### 10.3 模型选择器

模型可能从 0 到 2000 个，严禁继续使用无法搜索的超长 `Gio.Menu`。

必须使用可搜索列表：

- `Gtk.SearchEntry`；
- `Gtk.FilterListModel` + `Gtk.ListView` 或等价原生列表；
- 每行主标题为完整 model ID；
- 可选副标题显示 `owned_by`；
- 键盘可搜索、上下移动、Enter 选择、Escape 返回；
- 长 ID 可省略显示但 tooltip/accessibility 提供完整值；
- 提供“手动填写模型 ID”入口；
- 已选 model 清晰标记。

AI header 只显示当前模型的紧凑名称或完整 ID tooltip；点击后打开相同模型选择器或连接设置，
不得复制两套独立模型状态。

### 10.4 AI 面板状态

至少覆盖：

```text
UNCONFIGURED      尚未配置 AI 服务 + 配置按钮
READY_NO_DOCUMENT 已配置，但尚未打开文档
READY             可提问
FETCHING_MODELS   仅设置 dialog 显示，不冻结 reader
RUNNING           Thinking + 停止按钮
AUTH_ERROR        密钥失效 + 打开设置
NETWORK_ERROR     保留对话与问题，可重试
SECRET_ERROR      密钥环不可用/密钥丢失 + 打开设置
```

`AiPanel` 不再自行调用 `shutil.which()` 或读取 OpenCode 测试环境变量来决定可用性。
可用性必须由 coordinator 注入 typed state。

### 10.5 隐私提示

设置页必须明确说明：

> 发送问题时，MD Reader 会把当前文档的受限摘录、选区、相对路径、行号和你的问题发送到
> 你配置的 AI 服务。应用不会把完整工作区自动发送给模型。

不得用模糊的“可能使用数据”措辞，也不得声称第三方服务不会保留数据；数据保留由用户选择的
服务商决定。

---

## 11. 详细工作流程

### 11.1 应用启动

1. 加载非秘密 profile 元数据；
2. 不自动发网络请求；
3. 不自动弹出系统密钥环解锁；
4. 如果 profile 不完整，AI 显示未配置；
5. 如果 profile 完整，先显示已配置状态；
6. 只有用户显式打开连接设置并执行需要密钥的操作，或点击发送消息时，才异步读取 secret；
   仅展开/查看 AI panel 不得触发 keyring 解锁；
7. secret 不存在时转为 `SECRET_NOT_FOUND`，阅读器继续可用；
8. 启动过程不得因 Soup、Secret、网络或 provider 故障崩溃；Soup/Secret 可选依赖不得在模块
   顶层无条件导入，缺失时 AI 降级为带明确原因的不可用状态。

### 11.2 获取模型

```text
validate draft
  ├─ invalid -> field error, no request
  └─ valid
      ├─ resolve draft key；existing saved key 仅限同 origin
      ├─ create cancellable + dialog generation + draft fingerprint
      ├─ GET models endpoint
      ├─ status/error classification
      ├─ bounded JSON parse
      ├─ validate/dedupe/sort models
      └─ apply only if dialog generation and draft fingerprint are still current
```

API Key 只在此次请求生命周期内持有。完成或取消后，draft 仍只存在于 dialog 内；关闭 dialog 后
释放引用，不能复制到全局状态。

### 11.3 保存连接

按第 9.3 节事务执行。保存成功后：

- 更新 AI panel 状态；
- 当前对话重置；
- 选区 context rail 不必清除；
- 显示“AI 连接已保存”；
- 不自动发送测试聊天；
- 不自动重新获取模型。

### 11.4 Ask 请求

1. 校验 workspace、document、rendered source、profile、model、secret；
2. 构造最新 `DocumentContext`；
3. 构造 authoritative user question + untrusted document envelope；
4. 读取**既有的**有界成功历史；当前 user message 此时不得写入 `ConversationState`；
5. 用既有历史、本次最新上下文和用户问题组装 request；UI 追加 user transcript 并显示 Thinking；
6. 发出一个 `stream=true` 请求；
7. 增量正文回到 GTK 主线程并节流 Markdown 重建；
8. 仅在 `finish_reason=stop`，或第 8.4 节定义的 `[DONE]` 明确兼容成功条件成立且正文非空时，才把本次
   user/assistant pair 原子加入 history；
9. 失败保留 transcript、context quote 和输入问题；
10. 取消、失败或 partial response 不进入 history。

### 11.5 Edit 请求

1. 必须存在有效 source-line selection；
2. 绑定 canonical target、expected line range、source hash；
3. 使用独立 Edit system prompt 和 one-shot request；
4. Edit 请求不得携带 Ask 历史，也不得自动重试；
5. 收集完整响应，不把 partial JSON 显示为聊天正文；
6. 响应上限 256 KiB；
7. `PatchService.parse_replacement()` 校验 exact keys、exact range 和字段类型；
8. 生成 app-owned unified diff；
9. 用户显式接受后原子写入；
10. 保持 conflict detection 与 Undo；
11. 所有旧的“OpenCode 生成建议期间”错误文案改为中性的“AI 生成建议期间”。

### 11.6 模型或配置切换

- 活动回答期间禁用；
- 切换成功后清空 `ConversationState`；
- 取消所有旧 generation；
- UI transcript 重置为“正在使用 <model>，使用此模型开始新对话”；
- 不清除当前文档和选区；
- 不复用旧 provider 的 response 或错误。

---

## 12. 分阶段推进计划

每阶段完成后必须：

- 运行该阶段目标测试；
- 更新 `.ai/WORKING.md` 阶段状态（实时状态文件，gitignore 本地）；
- 记录实际验证与未验证项；
- 保持工作树可审查；
- 不跨阶段隐藏红测。

### Phase 0 — 基线与文档契约

- [ ] 运行当前完整单测和 Meson 测试，记录基线；
- [ ] 搜索所有 `OpenCode|opencode` 引用并分类：运行时代码、测试、用户文档、历史文档；
- [ ] 更新 `ARCHITECTURE.md` 的目标数据流和安全边界；
- [ ] 更新 `DESIGN_SPEC.md` 的连接设置、模型选择和状态文案；
- [ ] 把本迁移拆成实施计划 checkbox；
- [ ] 明确保留现有 PatchService 边界；
- [ ] 新增第一条会失败的 URL policy 或 model parser 测试。

**完成门槛：** 基线结果已记录；第一条迁移测试在旧代码上确实失败；没有业务实现混入文档提交。

### Phase 1 — 纯领域模型、URL policy 与解析器

- [ ] 新增 `AiProfile`、`AiModel`、错误码；
- [ ] 实现 URL 规范化、严格 loopback HTTP、同源、组件化端点构造和手动 redirect policy；
- [ ] 实现 `/models` JSON parser；
- [ ] 实现增量 SSE parser 与 JSON completion parser；
- [ ] 实现 `ConversationState` 有界历史；
- [ ] 所有内容保持无 GTK、无网络、可纯单测。

**完成门槛：** 纯逻辑边界测试全绿，旧 OpenCode path 尚未删除，应用仍可启动。

### Phase 2 — Secret Service 与非秘密设置

- [ ] 引入可选 `Soup`/`Secret` GI runtime 检查，禁止模块顶层硬依赖导致 reader 崩溃；
- [ ] 实现异步 `AiSecretStore` 接口；
- [ ] 让测试可注入 fake secret store；
- [ ] 新增 GSettings profile keys；
- [ ] 实现事务保存、轮换、清除和旧键兼容；
- [ ] 验证 API Key 不进入 dconf、日志和异常。

**完成门槛：** fake store 覆盖成功/失败/缺失；真实 Secret Service 有条件 smoke 明确报告通过或不可用，
不可用不能算通过。

### Phase 3 — 模型列表 HTTP client

- [ ] 使用 libsoup 3 异步 API；
- [ ] 实现 header、timeout、size limit、redirect policy、cancellation；
- [ ] 使用本地 loopback stub server 测试；
- [ ] 正确区分 empty、auth、404/405、429、timeout、TLS/network、malformed JSON；
- [ ] 实现 dialog generation + draft fingerprint，字段变化立即作废 stale result；
- [ ] 不把 key 写入诊断。

**完成门槛：** 不访问公网的集成测试覆盖成功与至少六类失败；GTK 主线程无阻塞等待。

### Phase 4 — Chat completions gateway

- [ ] 实现单请求 streaming gateway；
- [ ] 接入 SSE/JSON parser；
- [ ] 接入 bounded conversation history；
- [ ] 实现取消、超时、pre-parser response bytes / SSE event / `data:` line / UTF-8 text
  分层上限与严格 finish 判定；
- [ ] Ask 与 Edit 使用不同会话策略；
- [ ] 使用本地 stub server 验证 Unicode chunk、partial、DONE、EOF、HTTP error；
- [ ] 保证请求中无绝对工作区路径。

**完成门槛：** headless integration test 可完成 Ask stream、cancel、JSON fallback 和 Edit payload 收集。

### Phase 5 — GTK 设置与模型选择 UI

- [ ] 新增连接设置 dialog；
- [ ] 新增 password row、URL row、advanced models URL、隐私说明；
- [ ] 新增获取/取消模型流程；
- [ ] 新增可搜索模型列表与手动 ID；
- [ ] 新增保存事务、清除连接与错误 banner；
- [ ] `AiPanel` 改为接收 typed availability；
- [ ] AI header 模型入口复用同一状态；
- [ ] 响应期间禁用配置和模型切换。

**完成门槛：** process-level GTK smoke 覆盖未配置、配置成功、模型获取成功/空/失败、保存后可发送。

### Phase 6 — 替换 OpenCode 运行时并清理

- [ ] `window.py` 切换到新 gateway；
- [ ] 删除 OpenCode executable 检测和子进程路径；
- [ ] 删除免费模型过滤；
- [ ] 迁移所有用户文案；
- [ ] 删除或重命名 `services/opencode.py` 和 `tests/test_opencode.py`；
- [ ] 更新 PatchService 中的 OpenCode 文案；
- [ ] 更新 README、安装脚本、AppStream、架构、设计和 Flatpak 约束；
- [ ] 保留旧 GSettings key 的兼容说明，但运行时不依赖。

**完成门槛：** 运行时代码和用户文档中没有 OpenCode 依赖；历史/迁移文档中的引用有明确语境。

### Phase 7 — 全量验证与收尾

- [ ] 完整 unit/integration/Meson/GTK smoke；
- [ ] 真实应用启动，不使用 mockup；
- [ ] 640/960/1280/1920 Niri 截图；
- [ ] 高对比、200% 文本、长 URL、长 model ID、中文输入法；
- [ ] AI 未配置、Secret Service 不可用、无网络、401、429、stream cancel；
- [ ] Edit diff/apply/conflict/undo 回归；
- [ ] 安装脚本和隔离 DESTDIR/user install；
- [ ] 凭据泄漏检查；
- [ ] 一次有边界的独立只读 review；
- [ ] 更新实施计划最终 handoff。

**完成门槛：** 第 15 节所有验收项通过，失败或环境不可用检查被如实记录。

---

## 13. 测试规范与测试矩阵

### 13.1 测试执行纪律

1. 新行为先写在旧实现上会失败的测试；
2. 先证明失败原因与需求一致；
3. 再实现；
4. 同一测试从红变绿；
5. 至少补一个相邻边界；
6. 网络只用本地 deterministic stub，不依赖真实供应商；
7. Secret Service 单测用 injected fake，不污染开发者真实密钥环；
8. 真实系统 smoke 只能使用测试 profile 和假 key；
9. 不得为通过测试删除有效断言、扩大 skip 或捕获所有异常。

### 13.2 URL policy 单测

至少覆盖：

- 合法 HTTPS `/v1`；
- 合法 HTTPS 带自定义 path；
- 合法 loopback HTTP：精确 localhost、规范 IPv4 loopback、IPv6 `::1`；
- 拒绝远程 HTTP、普通域名解析到 loopback、模糊数字 IPv4、IPv6 zone ID；
- 拒绝反斜杠与非法/超范围端口；
- loopback HTTP transport 绕过系统代理；
- 拒绝空 URL；
- 拒绝无 host；
- 拒绝 userinfo；
- 拒绝 query、fragment、换行、NUL；
- 拒绝完整 `/models`、`/chat/completions`、`/responses` 地址；
- 正确删除尾斜杠；
- explicit models URL 作为精确 endpoint 通过独立 policy；
- models URL 同源通过、跨源失败；
- default ports 与显式 default ports 的同源比较；
- 2048 长度边界。

### 13.3 模型 parser 单测

至少覆盖：

- 标准 `data`；
- 缺失 `owned_by`；
- `data: []` 空成功；
- 缺失 `data`；
- `data` 非数组；
- entry 非 object；
- id 非 string/空/过长/控制字符；
- 部分坏条目 + 部分好条目；
- 全部坏条目；
- exact duplicate；
- 大小写不同 ID 不误合并；
- 2000 与 2001 数量边界；
- 2 MiB body 边界。

### 13.4 Model HTTP 集成测试

本地 `ThreadingHTTPServer` 或等价 fixture 至少模拟：

- 200 + Bearer header 正确；
- loopback 显式选择 `auth-mode=none` 时可空 key 且不发送 Authorization；
- loopback `bearer` 空 key 且无同 origin saved secret 时在本地拒绝；
- 401、403；
- 404、405；
- 429；
- 500；
- 慢响应 timeout；
- 用户取消；
- malformed JSON；
- oversized body；
- GET 模型请求的 same-origin 301/302/303/307/308，以及 chat 的 307/308，最多 3 跳；
- 300/304/305/306 与未知 3xx 被拒绝；
- 301/302/303 不得把 chat POST 改为 GET；
- cross-origin、HTTPS→HTTP、缺失/非法 Location、redirect loop/超限均拒绝；
- 跨源接收端实际未收到 Authorization；
- URL/key/auth mode/models URL 改动后旧结果立即 stale，旧选择最多保留为未验证手动 ID；
- dialog 关闭、generation 或 draft fingerprint 变化后的 callback 不更新 UI state。

### 13.5 SSE parser 单测

至少覆盖：

- 一个完整 event；
- 一个 JSON 被拆成多个 TCP chunk；
- 中文/emoji UTF-8 字节被拆开；
- 多个 `data:` 行；
- comment 与空行，海量 comment 仍受 pre-parser response bytes 与 SSE event 上限；
- role-only chunk；
- `content: null`；
- 多段正文；
- `[DONE]`；
- `finish_reason=stop` 后 EOF；
- partial finish 后再出现 `stop`、finish 后新增正文、互相矛盾 finish reason 均失败；
- 无终止标记 EOF；
- error object；
- malformed JSON event；
- reasoning-only 字段不显示；
- non-string content 被拒绝；
- finish reason `stop`、`length`、`content_filter`、`tool_calls`；
- 缺失/错误 Content-Type 的 4 KiB sniff 成功与失败；
- 空响应、仅 `[DONE]`、仅 reasoning/role 不成功；
- Content-Encoding 解码后的 pre-parser response bytes 8 MiB、单个 SSE event 256 KiB、单个
  `data:` line 256 KiB、Ask UTF-8 text 2 MiB、Edit UTF-8 text 256 KiB 的分层边界；
- 未终止单行/event 不得无限缓存；
- SSE 与 JSON completion 的 root/choices/delta/message/content/finish 类型、empty choices/正文及
  非 `stop` finish；
- 取消后不再发 chunk callback。

### 13.6 ConversationState 单测

- 成功 user/assistant pair 入历史；
- failed/cancelled/partial 不入历史；
- 12 条上限；
- 48,000 字符上限；
- 只删除完整 pair；
- model/profile/document change reset；
- selection change 不 reset；
- Edit 不携带 Ask history；
- prompt 只含相对路径，不含 canonical root。

### 13.7 Secret 与设置测试

- store、lookup、clear；
- Secret Service unavailable；
- missing item；
- 新 secret 成功、settings 失败时回滚；回滚 clear 失败时报告并可重试清理临时 UUID；
- settings 成功、旧 secret 删除失败时明确 partial success；
- 同 origin 且选择 `bearer` 时，密码框留空保持旧 key；
- scheme/host/effective port 改变后不得读取或发送旧 key；`bearer` 要求新 key，loopback 可显式
  选择 `none`；
- 旧 key 缺失时拒绝假成功；
- loopback 只有显式 `auth-mode=none` 才可保存无 key profile；
- `opencode-model` 不自动迁移；
- GSettings dump 不含 sentinel key；
- captured logs 不含 sentinel key；
- draft/request/exception repr 与服务端回显错误不含 sentinel key；
- 禁止通过 `dataclasses.asdict()` 或结构化 logger 泄漏 draft key；
- clear secret 成功但 GSettings 清除失败时进入明确 partial-failure；
- lookup/store/clear 异步，启动和纯阅读不触发 keyring 解锁。

测试假密钥统一可使用：

```text
sk-mdreader-test-secret-never-log-7d9f
```

测试结束后必须搜索工作树、build logs 和测试输出，确认此串没有被持久化；测试 fixture 中的
源代码常量本身可加入明确 allowlist。

### 13.8 GTK/process smoke

至少覆盖：

- 无 AI 配置启动；
- Soup/Secret typelib 缺失时 reader 仍启动，AI 明确降级；
- Soup/Secret 依赖存在但 keyring service 不可用；
- 打开设置 dialog；
- 密钥字段为 password 且不回显旧 key；
- 获取模型 loading/cancel/success/empty/auth error；
- 搜索并选择模型；
- 手动输入模型；
- 保存后 panel ready；
- 本地 stub SSE 回答出现在真实 GTK panel；
- 停止回答；
- 切换模型重置对话；
- OpenCode 完全缺失时仍可使用新 AI；
- 未配置/网络失败不影响文档打开、搜索、缩放、目录和 outline。

### 13.9 Edit 安全回归

现有 PatchService 测试必须继续通过，并增加/保留：

- exact selected range；
- bool 不能冒充 int 行号；
- 模型扩大范围被拒绝；
- stale source hash；
- workspace 外 target；
- symlink escape；
- CRLF；
- external change before apply；
- external change before undo；
- AI response oversized/malformed；
- direct LLM request 无 target path 字段。

---

## 14. 视觉、可访问性与 Niri 验收

必须用真实应用验证，不接受静态 mockup。

### 14.1 需要截图的状态

每个 640、960、1280、1920 logical-pixel width 至少检查：

- AI 未配置状态；
- AI 连接设置 dialog；
- 模型 loading；
- 模型结果列表；
- 超长 model ID；
- AI 正在回答；
- 网络/auth error；
- Edit selection context 与 diff 入口。

至少额外检查：

- 高对比主题；
- 200% 文本缩放；
- 简体中文输入法；
- 键盘-only：打开设置、填写、获取、搜索、选择、保存、发送、取消；
- reduced motion 下 spinner/状态仍可理解。

### 14.2 UI 验收标准

- 640px 不出现横向裁切；
- 960px 不因设置 dialog 或空 AI pane 挤压正文；
- 1280/1920 保持三栏和阅读宽度；
- 密钥字段、URL、长模型不撑破布局；
- 主操作始终可见；
- focus 顺序符合视觉顺序；
- 所有 icon-only 按钮有 tooltip 和 accessible name；
- 错误不是只有颜色差异；
- 模型列表 2000 项时滚动与搜索不冻结 GTK。

---

## 15. 最终完成定义（Definition of Done）

只有下列全部满足，才能宣布迁移完成。

### 15.1 功能

- [ ] 用户可填写并保存 API 基础地址与 API Key；
- [ ] API Key 保存于 Secret Service；
- [ ] 用户可点击获取模型并看到可搜索列表；
- [ ] 模型接口失败时可手动填写模型；
- [ ] 用户可选择模型并进行流式 Ask；
- [ ] Edit 仍生成受限 diff，不能直接写文件；
- [ ] 切换模型/配置会重置会话；
- [ ] 取消、超时、401、429、空列表和 malformed response 有正确状态；
- [ ] 没有 OpenCode 也能使用新 AI；
- [ ] 没有 AI 配置也能完整阅读 Markdown。

### 15.2 安全

- [ ] GSettings、文件、日志、截图和异常不含 key；
- [ ] remote HTTP 被拒绝；
- [ ] cross-origin models URL/redirect 被拒绝，接收端证明确实未收到 Authorization；
- [ ] 旧 key 永不复用于不同 scheme/host/effective port；
- [ ] redirect 由客户端手动校验，HTTPS 降级和非法 Location 被拒绝；
- [ ] TLS 验证不能关闭；
- [ ] 请求不含绝对 workspace path；
- [ ] response 的 pre-parser bytes、SSE event、`data:` line、Ask/Edit UTF-8 text、模型列表和
  错误正文均有独立上限；
- [ ] model output 仍经过 Markdown/JSON/PatchService 安全边界；
- [ ] Secret Service 失败不回退明文；
- [ ] Soup/Secret 缺失或 keyring 不可用时 reader 仍完整启动和阅读。

### 15.3 架构

- [ ] widget 不含网络和 secret 逻辑；
- [ ] URL、模型 parser、SSE parser 可纯单测；
- [ ] 所有慢操作异步且可取消；
- [ ] stale dialog generation 或 draft fingerprint 不污染新状态；
- [ ] `OpenCodeGateway` 运行时路径已移除；
- [ ] 旧 `opencode-model` 只作为明确的兼容遗留存在。

### 15.4 测试与构建

至少实际运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
node --check src/resources/reader/bridge.js
git diff --check
meson compile -C builddir
meson test -C builddir --print-errorlogs
```

如 `builddir` 不存在：

```bash
meson setup builddir
```

此外必须：

- [ ] 本地 stub provider 的模型与 chat 集成测试通过；
- [ ] 真实 GTK smoke 通过；
- [ ] Niri 640/960/1280/1920 截图已人工检查；
- [ ] 高对比和 200% 文本通过；
- [ ] 用户级安装或 DESTDIR 安装验证通过；
- [ ] 独立只读 review 已完成，或明确记录为不可用检查；
- [ ] 所有失败/skip/环境不可用项在 handoff 中逐项写明。

### 15.5 文档与清理

- [ ] `README.md` 不再要求安装 OpenCode；
- [ ] `ARCHITECTURE.md` 描述直接 LLM、Secret Service 与 HTTP 边界；
- [ ] `DESIGN_SPEC.md` 描述配置 dialog 和模型搜索；
- [ ] `FLATPAK_CONSTRAINTS.md` 重新评估网络和秘密存储，不沿用 OpenCode host bridge 结论；
- [ ] `scripts/install.sh` 检查 Soup/Secret runtime；
- [ ] AppStream/desktop 文案与真实行为一致；
- [ ] `rg -n -i 'opencode' src tests data README.md docs` 的剩余结果全部是有意保留且有解释的历史/迁移文本；
- [ ] 没有生成目录、schema 编译产物、密钥或测试日志进入仓库。

---

## 16. 建议提交边界

如执行者被授权提交，建议保持以下独立提交，不要把全部迁移压成一个巨型提交：

1. `docs: define direct LLM provider migration contract`
2. `test: specify endpoint model and stream behavior`
3. `feat: add AI profile and secret storage`
4. `feat: fetch OpenAI-compatible model catalogs`
5. `feat: stream direct LLM chat completions`
6. `feat: add AI connection and model selection UI`
7. `refactor: replace OpenCode runtime integration`
8. `docs: update installation security and acceptance`

未获得用户明确要求时，不自行 push、tag 或发布。

---

## 17. 执行者交接模板

每个阶段结束后在 `.ai/WORKING.md`（实时状态文件，gitignore 本地）追加：

```text
### YYYY-MM-DD direct LLM migration — Phase N

已完成：
- ...

改动文件：
- path: purpose

红转绿证据：
- 迁移前失败测试：...
- 实现后通过命令：...

安全检查：
- secret persistence: pass/fail/unavailable
- no absolute workspace path: pass/fail
- redirect/origin policy: pass/fail

UI/真实环境：
- launched real app: yes/no
- widths checked: 640/960/1280/1920
- screenshots: ...

未完成或环境依赖：
- ...

精确下一步：
- ...
```

不得写“全部正常”替代具体命令、数量和环境条件。查询失败必须写失败，检查不可用必须写不可用，
不能把失败或超时写成“没有发现问题”。

---

## 18. 最后红线

以下任一情况发生，必须停止收尾并修复：

- API Key 出现在 GSettings、文件、日志、URL 或异常；
- 为兼容第三方接口关闭 TLS 校验；
- 跨 origin 携带 Authorization；
- GTK 主线程被网络或 secret lookup 阻塞；
- 模型查询失败被显示为“0 个模型”；
- 模型输出绕过 PatchService 直接写文件；
- 直接请求包含 canonical workspace path；
- 取消后的旧请求继续写入新对话；
- 切换配置后仍复用旧 provider history；
- 640px 或 200% 文本下配置主操作不可达；
- 只验证 mockup，没有启动真实应用；
- 为了绿色结果删除测试、放宽安全断言或静默 skip；
- 文档仍告诉用户安装 OpenCode，而代码已经要求 URL 和 Key；
- 未记录失败/不可用检查却宣称完成。
