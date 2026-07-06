# AI Generation Service API

本文档描述独立 AI 生成服务对外提供的接口。服务目录为 `services/ai_generation_service`，Docker 镜像名为 `ai-generation-service:latest`。

## 服务边界

AI Generation Service 负责：

- 接收平台提交的 AI 生成、优化、修复请求。
- 调用 Hermes Agent `/v1/runs` 执行真实生成。
- 保存 AI run 状态、事件、产物索引。
- 管理 skill，并导出 Hermes 可读取的 `SKILL.md`。
- 对生成产物做轻量质量检查。
- 可选代理 OpenAI-compatible 模型接口。
- 可选调用 RAGFlow 知识库检索。

AI Generation Service 不负责：

- 用户、租户、权限、登录。
- 工单管理页面。
- 前端聊天 UI。
- 预览 iframe 页面。
- 长期部署注册表。

## 基础信息

本地默认地址：

```text
http://localhost:8091
```

Docker 健康检查：

```text
GET /health
```

如果配置了 `AI_GENERATION_API_KEY`，除 `/health` 和 `/api/ai/health` 外，其余接口需要：

```http
Authorization: Bearer <AI_GENERATION_API_KEY>
```

如果 `AI_GENERATION_API_KEY` 为空，则不启用该服务自身的 Bearer 校验。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AI_GENERATION_API_KEY` | 空 | AI 服务自身 API key。为空时不校验。 |
| `AI_GENERATION_DATA_DIR` | `/data` | run、event、skill 的持久化目录。 |
| `AI_GENERATION_TIMEOUT_SECONDS` | `1800` | AI 生成超时时间，当前按 30 分钟配置。 |
| `HERMES_RUNS_BASE_URL` | `http://hermes-agent:8642` | Hermes Agent runs API 地址。 |
| `HERMES_AGENT_API_KEY` | `hermes-local-dev-key` | 调用 Hermes Agent 的 Bearer key。 |
| `HERMES_AGENT_MODEL` | `gpt-5.5` | 提交给 Hermes 的模型名。 |
| `PLATFORM_BASE_URL` | `http://backend:8001` | 平台后端地址，用于状态和产物回调。 |
| `PLATFORM_API_KEY` | 空 | 调用平台回调接口时使用的 Bearer key。 |
| `OPENAI_BASE_URL` | 空 | OpenAI-compatible 模型服务地址。 |
| `OPENAI_API_KEY` | 空 | OpenAI-compatible 模型服务 key。 |
| `RAGFLOW_BASE_URL` | `http://host.docker.internal:9380` | RAGFlow 服务地址。 |
| `RAGFLOW_API_KEY` | 空 | RAGFlow API key。 |

## 接口总览

### 健康与运行时

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | Docker 健康检查。 |
| `GET` | `/api/ai/health` | 服务健康检查。 |
| `GET` | `/api/ai/runtime/status` | 查看 Hermes、模型代理、RAGFlow 配置状态。 |

### AI 生成任务

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/ai/runs` | 创建 AI 生成任务，提交到 Hermes `/v1/runs`。 |
| `GET` | `/api/ai/runs/{run_id}` | 查询 AI 生成任务状态，并同步 Hermes 状态。 |
| `GET` | `/api/ai/runs/{run_id}/events` | 查询任务事件。 |
| `POST` | `/api/ai/runs/{run_id}/retry` | 基于原始请求重试任务。 |
| `POST` | `/api/ai/runs/{run_id}/cancel` | 取消本地 run 状态。 |
| `POST` | `/api/ai/tasks/{task_id}/resume` | 按平台 task_id 继续生成。 |
| `GET` | `/api/ai/tasks/{task_id}/artifacts` | 查询某个 task 的生成产物。 |
| `POST` | `/api/ai/tasks/{task_id}/quality` | 对某个 task 执行质量检查。 |

### Skill 管理

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/ai/skills` | 查询 skill 列表，支持 `category`、`keyword` 过滤。 |
| `POST` | `/api/ai/skills` | 创建 skill。 |
| `GET` | `/api/ai/skills/{skill_id}` | 查询单个 skill。 |
| `PATCH` | `/api/ai/skills/{skill_id}` | 修改 skill。 |
| `DELETE` | `/api/ai/skills/{skill_id}` | 删除 skill。 |
| `POST` | `/api/ai/skills/match` | 根据需求文本匹配 skill。 |
| `POST` | `/api/ai/skills/export/hermes` | 导出 skill 为 Hermes 可用的 `SKILL.md`。 |
| `POST` | `/api/ai/skills/reload` | 记录或触发 skill reload 意图。 |

### 质量检查

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/ai/quality/check` | 对生成产物做轻量质量检查。 |

### 模型代理

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/ai/model/v1/chat/completions` | OpenAI-compatible chat completions 代理。 |
| `POST` | `/api/ai/model/v1/responses` | OpenAI-compatible responses 代理。 |

### 知识库检索

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/ai/knowledge/search` | 调用 RAGFlow 搜索；未配置或失败时可降级。 |

## 核心接口详情

### `GET /api/ai/runtime/status`

返回示例：

```json
{
  "runtime": "hermes",
  "model": "gpt-5.5",
  "hermes": {
    "healthy": true,
    "status_code": 200
  },
  "openai_proxy_configured": true,
  "ragflow_configured": true
}
```

### `POST /api/ai/runs`

用于创建 AI 生成任务。服务收到请求后会：

1. 生成本地 `airun_*`。
2. 组装 Hermes work-loop prompt 和工作目录要求。
3. 调用 Hermes Agent `/v1/runs`。
4. 持久化本地 run 状态。
5. 尝试回调平台任务状态接口。

请求体：

```json
{
  "task_id": "T-demo",
  "tenant_id": "tenant-demo",
  "user_id": "user-demo",
  "title": "公开 API 文章小看板",
  "markdown": "开发一个可部署预览的公开 API 小看板，使用 JSONPlaceholder posts API。",
  "mode": "create",
  "base_task_id": null,
  "runtime_secrets": {},
  "workspace": {
    "container_runtime_root": "/opt/data/task-runtime",
    "task_work_order_dir": "/opt/data/task-runtime/work-orders/T-demo",
    "task_staging_dir": "/opt/data/task-runtime/staging/T-demo",
    "note": null
  },
  "skill_context": null,
  "repair_instruction": null,
  "constraints": {
    "requires_backend": false
  },
  "payload": {},
  "callback": {}
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `task_id` | 是 | 平台任务 ID。优化、修复时应沿用原任务 ID。 |
| `tenant_id` | 否 | 租户 ID。 |
| `user_id` | 否 | 用户 ID。 |
| `title` | 否 | 任务标题。 |
| `markdown` | 否 | 用户自然语言需求。 |
| `mode` | 否 | `create`、`refine`、`repair`，默认 `create`。 |
| `base_task_id` | 否 | 被继承或参考的原任务 ID。 |
| `runtime_secrets` | 否 | 运行期密钥，如用户提供的数据 API token。 |
| `workspace.task_work_order_dir` | 是 | Hermes 生成产物工作目录。 |
| `workspace.task_staging_dir` | 否 | 平台部署前暂存目录。 |
| `skill_context` | 否 | 平台预匹配或注入的 skill 内容。 |
| `repair_instruction` | 否 | 修复场景的结构化说明。 |
| `constraints` | 否 | 生成约束，如是否需要后端。 |
| `payload` | 否 | 扩展上下文。 |
| `callback` | 否 | 回调扩展配置。 |

成功响应示例：

```json
{
  "run_id": "airun_a975df4b5b2e43b183fc811dcbebc398",
  "hermes_run_id": "run_3381d63c5d0f47788d825c466a7d3602",
  "task_id": "T-demo",
  "status": "running",
  "current_node": "CODING",
  "summary": "Hermes run submitted.",
  "artifacts": [],
  "next_action": "wait",
  "error": "",
  "created_at": 1783178827.0081248,
  "updated_at": 1783178827.0081248,
  "metadata": {}
}
```

Hermes 不可用时，接口不会直接崩溃，会返回一个失败 run：

```json
{
  "run_id": "airun_2798818a033642b3aef61408ed0196ad",
  "hermes_run_id": "",
  "task_id": "T-demo",
  "status": "failed",
  "current_node": "FAILED",
  "summary": "Failed to submit Hermes run: [Errno 111] Connection refused",
  "artifacts": [],
  "next_action": "manual_input",
  "error": "[Errno 111] Connection refused"
}
```

### `GET /api/ai/runs/{run_id}`

查询本地 run，并尝试同步 Hermes 状态。

如果 Hermes 仍显示 running，但工作目录中已经存在完整 `artifact.json` 和 `source`，服务会尝试恢复产物状态并标记完成。

### `GET /api/ai/runs/{run_id}/events`

查询本地事件。

可选参数：

| 参数 | 说明 |
|---|---|
| `stream` | 当前接口参数保留，默认为 `false`。 |

### `POST /api/ai/runs/{run_id}/retry`

基于原 run 的 `metadata.request` 重新提交 Hermes 任务。

注意：当前实现会创建新的 `airun_*`，而不是复用旧 `run_id`。

### `POST /api/ai/runs/{run_id}/cancel`

取消本地 run 状态。

注意：当前服务已能把本地状态置为 `cancelled`。Hermes 侧是否真实停止取决于 Hermes Agent 当前是否提供对应取消能力。

### `POST /api/ai/tasks/{task_id}/resume`

请求体与 `POST /api/ai/runs` 相同，但路径中的 `task_id` 会覆盖请求体里的 `task_id`。

适用场景：

- 多轮优化。
- 失败后继续。
- 平台希望明确沿用某个任务编号。

### `GET /api/ai/tasks/{task_id}/artifacts`

返回指定 task 下所有本地 run 中记录的 artifacts。

响应示例：

```json
{
  "task_id": "T-demo",
  "artifacts": [
    {
      "type": "web_app",
      "path": "/opt/data/task-runtime/work-orders/T-demo/source",
      "entry": "index.html"
    }
  ]
}
```

## Skill 接口详情

### `POST /api/ai/skills`

创建 skill。

请求体：

```json
{
  "skill_id": "api-dashboard",
  "name": "API Dashboard",
  "category": "frontend",
  "description": "生成公开 API 小看板",
  "triggers": ["public api", "dashboard", "posts"],
  "instructions": "Use vanilla HTML/CSS/JS and call real APIs.",
  "prompt": "",
  "metadata": {}
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `skill_id` | 是 | Skill 唯一 ID。 |
| `name` | 是 | Skill 名称。 |
| `category` | 否 | 分类，例如 `frontend`、`backend`、`data`。 |
| `description` | 否 | 描述。 |
| `triggers` | 否 | 触发词列表。 |
| `instructions` | 否 | Skill 具体执行要求。 |
| `prompt` | 否 | 可追加给 Hermes 的 prompt 片段。 |
| `metadata` | 否 | 扩展字段。 |

### `GET /api/ai/skills`

查询 skill 列表。

可选 query 参数：

| 参数 | 说明 |
|---|---|
| `category` | 按分类过滤。 |
| `keyword` | 按名称、描述、触发词过滤。 |

### `PATCH /api/ai/skills/{skill_id}`

更新 skill。请求体字段与创建接口一致，所有字段都可选。

### `POST /api/ai/skills/match`

按需求文本匹配 skill。

请求体：

```json
{
  "query": "开发一个公开 API 文章小看板，支持搜索和排序",
  "limit": 3
}
```

响应示例：

```json
{
  "matches": [
    {
      "score": 3,
      "skill_id": "api-dashboard",
      "name": "API Dashboard",
      "category": "frontend",
      "description": "生成公开 API 小看板",
      "prompt": ""
    }
  ]
}
```

### `POST /api/ai/skills/export/hermes`

将当前 skill 导出为 Hermes skill 目录。

响应示例：

```json
{
  "export_dir": "/data/hermes-skills",
  "written": [
    "/data/hermes-skills/api-dashboard/SKILL.md"
  ],
  "count": 1
}
```

## 质量检查接口详情

### `POST /api/ai/quality/check`

请求体：

```json
{
  "task_id": "T-demo",
  "project_root": "/opt/data/task-runtime/work-orders/T-demo/source",
  "files": [],
  "requirements": {
    "requires_backend": false
  }
}
```

响应示例：

```json
{
  "task_id": "T-demo",
  "passed": true,
  "issues": [],
  "checked_file_count": 1,
  "checks": [
    {
      "name": "html_entry",
      "status": "passed"
    },
    {
      "name": "dependency_free_node",
      "status": "passed"
    }
  ]
}
```

当前检查重点：

- 是否存在 HTML 入口。
- Node 服务是否尽量保持 dependency-free。
- 是否存在 mock、sample、fake 等明显测试替身信号。
- 需要后端时是否存在合理后端入口。

## 模型代理接口详情

### `POST /api/ai/model/v1/chat/completions`

透传 OpenAI-compatible chat completions 请求到：

```text
{OPENAI_BASE_URL}/v1/chat/completions
```

### `POST /api/ai/model/v1/responses`

透传 OpenAI-compatible responses 请求到：

```text
{OPENAI_BASE_URL}/v1/responses
```

调用要求：

- 必须配置 `OPENAI_BASE_URL`。
- 如上游需要鉴权，必须配置 `OPENAI_API_KEY`。

## 知识库接口详情

### `POST /api/ai/knowledge/search`

请求体：

```json
{
  "query": "需要检索的知识",
  "top_k": 5
}
```

行为：

- 配置 `RAGFLOW_BASE_URL` 后会调用 RAGFlow。
- 配置为空时返回可降级结果。
- RAGFlow 不可达或鉴权失败时，调用方应允许生成链路继续执行。

## Docker 使用

构建镜像：

```powershell
docker compose -f compose.backend-only.yml build ai-generation-service
```

启动服务：

```powershell
docker compose -f compose.backend-only.yml up -d ai-generation-service
```

单独启动服务：

```powershell
docker run -d --name ai-generation-standalone `
  -p 8091:8091 `
  -e HERMES_RUNS_BASE_URL=http://host.docker.internal:18642 `
  -e AI_GENERATION_DATA_DIR=/data `
  ai-generation-service:latest
```

接入当前 compose 网络中的 Hermes：

```powershell
docker run -d --name ai-generation-standalone-hermes `
  --network ff-ai_default `
  -p 8091:8091 `
  -e HERMES_RUNS_BASE_URL=http://hermes-agent:8642 `
  -e HERMES_AGENT_API_KEY=hermes-local-dev-key `
  -e AI_GENERATION_DATA_DIR=/data `
  ai-generation-service:latest
```

## 已验证结果

本地已验证：

- `ai-generation-service:latest` 镜像可构建。
- 容器可独立启动。
- `/health` 和 `/api/ai/health` 正常。
- Hermes 不可达时服务自身不崩溃，生成任务返回可控失败 run。
- 接入 `ff-ai_default` 网络后可识别 Hermes 健康。
- `/api/ai/runs` 可成功提交 Hermes run。
- Skill 创建、查询、匹配、导出可用。
- 质量检查接口可用。

## Docker 镜像实际生成测试记录

测试时间：2026-07-05

测试目标：

- 使用新拆出的 `ai-generation-service:latest` Docker 镜像发起真实 AI 生成。
- 通过 AI Generation Service 调用 Hermes `/v1/runs`。
- 让 Hermes 在共享工作目录中生成可运行产物。
- 对生成结果执行质量检查和 HTTP smoke test。

测试环境：

| 项目 | 值 |
|---|---|
| AI 服务地址 | `http://localhost:8091` |
| AI 服务容器 | `ff-ai-generation-service` |
| Hermes 容器 | `ff-ai-hermes-agent` |
| Hermes 地址 | `http://hermes-agent:8642` |
| 共享工作卷 | `ff-ai_task-runtime-data` |
| 工作目录挂载点 | `/opt/data/task-runtime` |
| 最终测试镜像 | `ai-generation-service:latest` |
| 最终镜像 ID | `987c7b55d9b5` |

### 测试用例

生成需求：

```text
Build a deployable public API posts dashboard.
Use the real JSONPlaceholder posts API to fetch the first 10 posts.
Support keyword filtering by title, sorting by id ascending and descending,
card display for id title body, and a detail panel when a card is clicked.
Show a clear Chinese error message when the API request fails.
Use only vanilla HTML CSS JS and a dependency-free native Node http server.
Do not use mock data and do not use a build tool.
```

最终测试任务：

| 字段 | 值 |
|---|---|
| `task_id` | `T-ai-service-docker-smoke-20260705-r3` |
| `run_id` | `airun_4e8717ed899d49f0a200dd61f79576cd` |
| `hermes_run_id` | `run_e11ad35d2ea846d6bd6944397f73891d` |
| `mode` | `create` |
| `requires_backend` | `true` |
| `external_api` | `https://jsonplaceholder.typicode.com/posts` |

请求摘要：

```json
{
  "task_id": "T-ai-service-docker-smoke-20260705-r3",
  "title": "Docker AI Service real generation permission retest",
  "mode": "create",
  "workspace": {
    "container_runtime_root": "/opt/data/task-runtime",
    "task_work_order_dir": "/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3",
    "task_staging_dir": "/opt/data/task-runtime/staging/T-ai-service-docker-smoke-20260705-r3"
  },
  "constraints": {
    "requires_backend": true,
    "vanilla_only": true,
    "external_api": "https://jsonplaceholder.typicode.com/posts"
  }
}
```

### 第一轮测试结果

任务：

| 字段 | 值 |
|---|---|
| `task_id` | `T-ai-service-docker-smoke-20260705` |
| `run_id` | `airun_f7b67f0a94b54fc8b84e518bfd4f84e0` |
| `hermes_run_id` | `run_ca325563b0d145b8af949d23d1bae3da` |

结果：

- AI 服务成功提交 Hermes run。
- Hermes 返回 `completed`，但结果为 `pending_approval`。
- 未生成实际文件。

失败原因：

```text
The required work-order directory ... does not exist,
and the available file-writing tool failed to create nested parent directories.
```

修复：

- AI 服务在提交 Hermes 前预创建工作目录。
- 新增预创建目录：
  - `task_work_order_dir`
  - `task_work_order_dir/source`
  - `task_work_order_dir/source/public`
  - `task_staging_dir`
- 产物恢复逻辑补充识别 `source/public/index.html`。

相关代码：

```text
services/ai_generation_service/app/main.py
```

### 第二轮测试结果

任务：

| 字段 | 值 |
|---|---|
| `task_id` | `T-ai-service-docker-smoke-20260705-r2` |
| `run_id` | `airun_ca98e787cb6c438298b8fd6debfb6f43` |
| `hermes_run_id` | `run_50062ae8cc644c7eaea31314d8b82169` |

结果：

- AI 服务成功提交 Hermes run。
- 工作目录已创建。
- Hermes 仍无法写入产物。

失败原因：

```text
write_file failed with Permission denied for /source/.keep and /source/public/index.html;
writing /source/server/index.js failed because /source/server did not exist.
```

修复：

- AI 服务预创建 `source/server`。
- 对共享工作目录执行 `chmod 0777`，解决 AI 服务容器与 Hermes 容器 UID/GID 不一致导致的写权限问题。

修复后的预创建目录：

```text
/opt/data/task-runtime/work-orders/{task_id}
/opt/data/task-runtime/work-orders/{task_id}/source
/opt/data/task-runtime/work-orders/{task_id}/source/public
/opt/data/task-runtime/work-orders/{task_id}/source/server
/opt/data/task-runtime/staging/{task_id}
```

权限验证：

```text
drwxrwxrwx /opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3
drwxrwxrwx /opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source
drwxrwxrwx /opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/public
drwxrwxrwx /opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/server
```

### 第三轮测试结果

任务：

| 字段 | 值 |
|---|---|
| `task_id` | `T-ai-service-docker-smoke-20260705-r3` |
| `run_id` | `airun_4e8717ed899d49f0a200dd61f79576cd` |
| `hermes_run_id` | `run_e11ad35d2ea846d6bd6944397f73891d` |

AI 服务最终状态：

```json
{
  "status": "completed",
  "current_node": "TESTING",
  "summary": "Hermes is still running, but a complete staged artifact was recovered.",
  "artifacts": [
    {
      "type": "project_root",
      "path": "/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source"
    },
    {
      "type": "html_page",
      "path": "/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/public/index.html"
    },
    {
      "type": "backend_entry",
      "path": "/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/server.js"
    }
  ]
}
```

生成文件：

```text
/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/server.js
/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/public/index.html
/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/public/app.js
/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/public/styles.css
/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/README.md
/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/test_plan.md
/opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source/tests/smoke-test.js
```

质量检查：

```json
{
  "task_id": "T-ai-service-docker-smoke-20260705-r3",
  "passed": true,
  "issues": [],
  "checked_file_count": 7,
  "checks": [
    {
      "name": "html_entry",
      "status": "passed"
    },
    {
      "name": "dependency_free_node",
      "status": "passed"
    }
  ]
}
```

实际 HTTP smoke test：

启动命令：

```sh
cd /opt/data/task-runtime/work-orders/T-ai-service-docker-smoke-20260705-r3/source
PORT=3210 node server.js
```

测试结果：

| URL | HTTP 状态 | 结果 |
|---|---:|---|
| `http://127.0.0.1:3210/` | `200` | 返回生成的中文 HTML 页面。 |
| `http://127.0.0.1:3210/api/posts` | `200` | 返回真实 JSONPlaceholder 数据。 |

`/api/posts` 响应摘要：

```json
{
  "source": "https://jsonplaceholder.typicode.com/posts",
  "count": 10,
  "posts": [
    {
      "userId": 1,
      "id": 1,
      "title": "sunt aut facere repellat provident occaecati excepturi optio reprehenderit"
    }
  ]
}
```

### 测试结论

结论：`ai-generation-service:latest` 可以作为独立 Docker 服务完成真实生成链路。

已验证能力：

- AI 服务 Docker 镜像可构建、可启动、健康检查通过。
- AI 服务可调用 Hermes `/v1/runs`。
- AI 服务可预创建共享工作目录。
- Hermes 可在共享工作目录内生成文件。
- AI 服务可从工作目录恢复 artifact。
- 生成产物通过质量检查。
- 生成的 Node 服务可启动。
- 页面入口 `/` 返回 `200`。
- 后端代理接口 `/api/posts` 返回 `200`，并成功获取真实 JSONPlaceholder 数据。

仍需对齐的问题：

- 第三轮中 AI 服务已经通过产物恢复标记 `completed`，但 Hermes run 查询仍显示 `running` / `tool.completed`。说明 Hermes Agent run 收尾状态与平台产物恢复之间还需要进一步对齐。
- 当前共享卷权限采用 `0777` 解决跨容器 UID/GID 写入问题。生产环境建议改为统一运行 UID/GID，或在 init container 中集中设置目录所有权。

## 待对齐事项

- `POST /api/ai/runs/{run_id}/cancel` 当前主要取消本地 run 状态；Hermes 侧真实取消能力需要与 Hermes Agent 接口继续对齐。
- 平台回调接口当前按独立服务拆分后的契约预留，平台侧需要实现或适配对应状态、产物接收接口。
- `skills/export/hermes` 当前导出到 `/data/hermes-skills`，Hermes 是否自动加载该目录需要部署层配置挂载或 reload 机制。
