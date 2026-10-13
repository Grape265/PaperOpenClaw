# PaperOpenClaw API 契约

版本：v1
前缀：`/api/v1`
内容类型：`application/json; charset=utf-8`（文件流除外）

本契约是前后端对齐的唯一基准。后端 Pydantic 模型与前端 `web/src/api/` 调用层都必须
与本文件一致。任何一方新增接口，须先改本文件。

---

## 0. 通用约定

### 0.1 响应包裹

成功：

```json
{ "ok": true, "data": <payload>, "error": null, "meta": { "request_id": "..." } }
```

失败：

```json
{ "ok": false, "data": null, "error": { "code": "PAPER_NOT_FOUND", "message": "论文不存在" }, "meta": { "request_id": "..." } }
```

### 0.2 错误码

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `BAD_REQUEST` | 400 | 参数不合法 |
| `UNAUTHORIZED` | 401 | 缺少或错误的访问令牌 |
| `PAPER_NOT_FOUND` | 404 | 论文不存在 |
| `JOB_NOT_FOUND` | 404 | 任务不存在 |
| `SESSION_NOT_FOUND` | 404 | 会话不存在 |
| `SKILL_NOT_FOUND` | 404 | 技能不存在 |
| `ASSET_NOT_FOUND` | 404 | 图表不存在 |
| `CONCEPT_NOT_FOUND` | 404 | OKF 概念不存在 |
| `PATH_OUT_OF_SCOPE` | 400 | 请求路径越出数据目录 |
| `DUPLICATE_PAPER` | 409 | 论文已存在（哈希命中） |
| `PROVIDER_NOT_CONFIGURED` | 412 | 大模型提供方未配置 |
| `PROVIDER_ERROR` | 502 | 大模型调用失败 |
| `PARSE_FAILED` | 422 | 解析结果校验失败 |
| `INTERNAL_ERROR` | 500 | 未预期错误 |

### 0.3 分页

请求：`page`（默认 1）、`page_size`（默认 20，最大 200）。
响应 `data`：

```json
{ "items": [], "total": 0, "page": 1, "page_size": 20 }
```

### 0.4 论文引用对象（贯穿全站）

```json
{
  "hash": "3f2a…",
  "hash_short": "3f2a1b9c",
  "title": "Attention Is All You Need",
  "journal": "NeurIPS",
  "published_at": "2017-06-12",
  "year": 2017,
  "layer": "C",
  "section": "method",
  "unit_id": "c:3f2a…",
  "snippet": "……",
  "score": 0.87,
  "asset_refs": ["fig1", "tab2"]
}
```

---

## 1. 系统

### GET `/api/v1/health`
健康检查与能力探测。

```json
{ "status": "ok", "version": "1.0.0", "data_dir": "D:\\PaperOpenClaw\\data",
  "providers": { "parse": true, "chat": true, "embedding": "local" },
  "counts": { "papers": 12, "rag_units": 340, "kg_nodes": 512, "skills": 3 } }
```

### GET `/api/v1/settings`
返回当前设置，API Key 以掩码回显（`sk-…abcd`）。

### PUT `/api/v1/settings`
Body（全部可选，仅提交的字段被更新）：

```json
{
  "data_dir": "D:\\PaperOpenClaw\\data",
  "providers": [
    { "id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
      "api_key": "sk-…", "models": ["deepseek-chat"] }
  ],
  "routing": { "parse": { "provider": "deepseek", "model": "deepseek-chat" },
               "chat":  { "provider": "deepseek", "model": "deepseek-chat" },
               "embedding": { "provider": "local", "model": "bge-small-zh-v1.5" } },
  "ingest": { "vision_batch_pages": 20, "image_dpi": 200, "max_retry": 2 },
  "retrieval": { "top_k_c": 5, "top_k_b": 8, "top_k_a": 8, "min_score": 0.25 },
  "agent": { "max_steps": 8, "temperature": 0.3, "brainstorm_rounds": 2 }
}
```

### POST `/api/v1/settings/test`
测试提供方连通性。
Body：`{ "provider": "deepseek", "model": "deepseek-chat", "kind": "chat|parse|embedding" }`
返回：`{ "ok_detail": { "latency_ms": 820, "sample": "pong" } }`

### GET `/api/v1/doctor`
诊断：数据目录可写、SQLite 可写、Chroma 可用、嵌入模型是否存在、依赖版本。

### GET `/api/v1/provider-presets`
返回内置的提供方预设，供设置页一键添加。

```json
{ "items": [{ "id": "deepseek", "name": "DeepSeek",
  "base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat"],
  "vision": false, "notes": "文本能力强、价格低……" }] }
```

### GET `/api/v1/providers/{provider_id}/models`
向提供方查询其模型列表（尽力而为，不支持时返回空数组）。
返回：`{ "provider": "deepseek", "models": ["deepseek-chat"] }`

---

## 2. 论文

### GET `/api/v1/papers/facets`
论文库筛选面板所需的取值集合。

```json
{ "journals": ["NeurIPS", "CVPR"], "year_from": 2016, "year_to": 2024,
  "statuses": [{ "value": "ready", "count": 12 }], "total": 12 }
```

### GET `/api/v1/papers`
查询参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `q` | string | 模糊匹配论文名 / 作者 / 期刊 / 哈希 |
| `hash` | string | 精确或前缀匹配哈希 |
| `hashes` | csv | 批量取（用于「已选项刷新」） |
| `author` | string | 作者名 |
| `journal` | string | 期刊 |
| `year_from` / `year_to` | int | 年份范围 |
| `status` | string | `pending\|parsing\|indexing\|ready\|failed` |
| `sort` | string | `created_at\|title\|year\|published_at`，前缀 `-` 为倒序 |
| `page` / `page_size` | int | 分页 |

`data.items[]` 为论文摘要对象：

```json
{
  "hash": "3f2a…", "hash_short": "3f2a1b9c",
  "title": "…", "journal": "…", "published_at": "2017-06-12", "year": 2017,
  "authors": [{ "name": "…", "affiliations": ["…"] }],
  "language": "en", "page_count": 11, "status": "ready",
  "counts": { "figures": 5, "tables": 3, "rag_a": 12, "rag_b": 7, "kg_nodes": 68 },
  "created_at": "2026-01-01T10:00:00Z"
}
```

### GET `/api/v1/papers/{hash}`
论文完整详情：`paper`（含 authors/affiliations/keywords）、`assets`、`okf`（概念树）、
`pyramid`（A/B/C 计数与单元）、`kg_summary`、`ingest`（阶段耗时与模型用量）。

### DELETE `/api/v1/papers/{hash}`
删除论文及其全部关联数据（关系库级联、向量库、图谱、文件目录）。
返回：`{ "deleted": true, "hash": "…" }`

### GET `/api/v1/papers/{hash}/status`
轻量状态查询，供导入页轮询单个哈希。
返回：`{ "hash": "…", "status": "ready", "error": "", "counts": { }, "updated_at": "…" }`

### POST `/api/v1/papers/{hash}/reparse`
重新解析。Body：`{ "from_stage": "layout|vision|okf|pyramid|kg", "force": false }`
返回：`{ "job_id": "…" }`

### GET `/api/v1/papers/{hash}/pdf`
返回 `application/pdf` 流，支持 `Range` 分段请求。

### GET `/api/v1/papers/{hash}/meta.json`
返回解析快照原始 JSON。

### GET `/api/v1/papers/{hash}/export`
打包导出（zip）：PDF + 图表 + OKF Bundle。返回 `application/zip`。

---

## 3. 导入与任务

### POST `/api/v1/ingest/upload`
`multipart/form-data`，字段 `files`（可多个）。
返回：`{ "job_ids": ["…"], "accepted": 3, "duplicated": 1,
        "items": [{ "filename": "a.pdf", "hash": "…", "job_id": "…", "duplicated": false }] }`

### POST `/api/v1/ingest/paths`
从服务器本地路径导入（桌面版选择目录后使用）。
Body：`{ "paths": ["D:\\papers\\a.pdf"], "recursive": true }`
返回同上传。

### GET `/api/v1/jobs`
查询参数：`status`、`page`、`page_size`。
任务对象：

```json
{
  "id": "job_…", "kind": "ingest", "hash": "3f2a…", "filename": "a.pdf",
  "stage": "vision|layout|okf|pyramid|kg", "stage_index": 4, "stage_total": 7,
  "status": "running|done|failed|cancelled", "progress": 0.57,
  "message": "正在解析第 2/3 批", "error": null,
  "started_at": "…", "finished_at": null
}
```

### GET `/api/v1/jobs/{job_id}`
单任务详情，含分阶段耗时 `stages[]` 与模型用量 `usage`。

### GET `/api/v1/jobs/{job_id}/events`  (SSE)
事件流：

```
event: stage
data: {"stage":"layout","stage_index":3,"progress":0.3,"message":"图表裁剪完成：图 5 表 3"}

event: log
data: {"level":"info","message":"…"}

event: done
data: {"status":"done","hash":"3f2a…"}
```

### POST `/api/v1/jobs/{job_id}/cancel`
取消任务。

---

## 4. 图表

### GET `/api/v1/papers/{hash}/assets`
返回：`{ "items": [{ "label": "fig1", "kind": "figure", "page": 3, "caption": "…",
"file_path": "papers/3f2a…/fig/fig1.png", "width": 1420, "height": 860,
"url": "/api/v1/papers/3f2a…/assets/fig1" }] }`

### GET `/api/v1/papers/{hash}/assets/{label}`
返回图片二进制（`image/png`）。

---

## 5. OKF

### GET `/api/v1/papers/{hash}/okf`
返回 OKF Bundle 概念树：

```json
{ "okf_version": "0.2", "root": "papers/3f2a…/okf",
  "tree": [{ "concept_id": "paper", "type": "Research Paper", "title": "…",
             "description": "…", "children": [
               { "concept_id": "sections/method", "type": "Section", "title": "方法", "children": [] } ] }] }
```

### GET `/api/v1/papers/{hash}/okf/{concept_id}`
`concept_id` 为 Bundle 内相对路径去掉 `.md`，如 `sections/method`。
返回：`{ "concept_id": "…", "frontmatter": { }, "body": "…", "raw": "…", "links": ["…"] }`

### POST `/api/v1/papers/{hash}/okf/verify`
OKF 一致性校验（规范 §11）。
返回：`{ "conformant": true, "files": 24, "errors": [], "warnings": [] }`

---

## 6. RAG

### POST `/api/v1/rag/search`
Body：

```json
{ "query": "自注意力机制的复杂度",
  "hashes": ["3f2a…", "9b1c…"],
  "layers": ["C", "B", "A"],
  "top_k": { "c": 5, "b": 8, "a": 8 },
  "min_score": 0.25,
  "cascade": true }
```

返回：

```json
{ "query": "…",
  "candidates": [{ "hash": "…", "hash_short": "…", "title": "…", "score": 0.81 }],
  "evidences": [
    { "unit_id": "b:3f2a…:method", "layer": "B", "hash": "…", "title": "…",
      "section": "method", "text": "…", "score": 0.77,
      "trace": ["c:3f2a…"], "asset_refs": ["fig2"] },
    { "unit_id": "a:3f2a…:3", "layer": "A", "section": "method",
      "text": "…", "score": 0.71, "trace": ["c:3f2a…", "b:3f2a…:method"], "asset_refs": [] }
  ],
  "layers_used": ["C", "B", "A"],
  "empty": false }
```

### GET `/api/v1/rag/pyramid/{hash}`
某篇论文的金字塔结构（含层间关联）：

```json
{ "hash": "…",
  "C": { "unit_id": "c:…", "text": "…", "cross_links": [{ "hash": "9b1c…", "score": 0.66 }] },
  "B": [{ "unit_id": "b:…:method", "section": "method", "title": "方法", "text": "…",
          "parents": ["c:…"], "children": ["a:…:3", "a:…:4"] }],
  "A": [{ "unit_id": "a:…:3", "section": "method", "ordinal": 3, "text": "…",
          "parents": ["b:…:method"] }] }
```

### GET `/api/v1/rag/units/{unit_id}`
单个语义单元及其邻居（上下层、同层相邻、跨论文关联）。

### GET `/api/v1/rag/graph?hashes=…&layers=C,B,A`
返回金字塔关系图（节点 + 边），供前端可视化。

### GET `/api/v1/rag/overview`
语料级金字塔统计，用于图谱页头部。

```json
{ "units": { "A": 120, "B": 84, "C": 12 }, "vectors": { "A": 120, "B": 84, "C": 12 },
  "cross_links": 36, "papers": 12,
  "embedding": { "provider": "local", "model": "bge-small-zh-v1.5", "dimension": 512 },
  "sections": { "background": "背景", "…": "…" } }
```

### POST `/api/v1/rag/rebuild`
重建向量库。Body：`{ "hashes": [], "layers": ["A","B","C"] }` → `{ "job_id": "…" }`

---

## 7. 知识图谱

### GET `/api/v1/kg/sections`
返回固定的七维骨架定义：

```json
{ "items": [{ "slug": "background", "label": "背景", "order": 1 },
            { "slug": "data", "label": "数据", "order": 2 },
            { "slug": "method", "label": "方法", "order": 3 },
            { "slug": "experiment", "label": "实验", "order": 4 },
            { "slug": "conclusion", "label": "结论", "order": 5 },
            { "slug": "discussion", "label": "讨论", "order": 6 },
            { "slug": "limitation", "label": "不足与展望", "order": 7 }] }
```

### GET `/api/v1/kg/fishbone/{hash}`
单篇论文鱼骨图：

```json
{ "hash": "…", "title": "…",
  "spine": [
    { "slug": "background", "label": "背景", "order": 1,
      "nodes": [{ "key": "content:3f2a…:background:1", "label": "…", "detail": "…",
                  "ordinal": 1, "assets": [{ "label": "fig1", "url": "/api/v1/papers/3f2a…/assets/fig1" }] }] }
  ] }
```

### GET `/api/v1/kg/graph`
参数：`hashes`（csv，空为全部）、`sections`（csv）、`depth`（默认 2）、`limit`（默认 2000）。
返回 `{ "nodes": [{ "key": "…", "kind": "spine|content|asset|paper", "label": "…",
"section": "…", "hash": "…", "meta": {} }], "edges": [{ "source": "…", "target": "…",
"relation": "…", "weight": 1.0 }] }`

### GET `/api/v1/kg/stats`
图谱结构统计：`{ "nodes": 0, "edges": 0, "components": 0, "density": 0.0,
"top_degree": [{ "key": "…", "degree": 8, "label": "…", "kind": "spine" }] }`

### GET `/api/v1/kg/path`
参数：`source`、`target`（节点键）。返回两点之间的最短无向路径。

```json
{ "source": "paper:…", "target": "paper:…", "found": true,
  "path": [{ "key": "…", "label": "…", "kind": "spine", "hash": "" }] }
```

### GET `/api/v1/kg/export`
参数：`hashes`、`format`（`json|graphml`）。返回文件流。

### GET `/api/v1/kg/search`
参数：`q`、`hashes`、`sections`、`limit`。返回匹配的内容节点及其所在鱼骨位置。

---

## 8. 智能体

### GET `/api/v1/agent/profiles`
可用的智能体角色预设：

```json
{ "items": [{ "id": "methodologist", "name": "方法学家", "persona": "…",
              "tools": ["rag_search", "kg_query", "okf_read"], "default": false }] }
```

### GET `/api/v1/agent/tools`
已注册工具清单（含来源：内置 / 技能）：

```json
{ "items": [{ "name": "rag_search", "description": "…", "source": "builtin",
              "params_schema": { } }] }
```

### POST `/api/v1/agent/qa`
Body：

```json
{ "question": "…", "hashes": ["…"], "top_k": { "c": 5, "b": 8, "a": 8 },
  "session_id": null, "agent_id": "qa" }
```

返回：`{ "session_id": "…", "answer": "…", "citations": [ <论文引用对象> ],
"layers_used": ["C","B","A"], "usage": { "prompt_tokens": 0, "completion_tokens": 0 } }`

### POST `/api/v1/agent/qa/stream`  (SSE)
Body 同上。事件：

```
event: citations
data: {"citations": [ … ]}

event: delta
data: {"text": "自注意力"}

event: done
data: {"session_id": "…", "usage": { }}

event: error
data: {"code": "PROVIDER_ERROR", "message": "…"}
```

### POST `/api/v1/agent/run`
运行单个智能体的完整工具调用循环（非流式），用于论文详情页的即时分析。

Body：

```json
{ "task": "这篇论文的方法有什么前提假设？", "profile": "qa",
  "hashes": ["…"], "tools": ["rag_search", "okf_read"], "session_id": null }
```

返回：`{ "session_id": "…", "agent_id": "qa", "answer": "…", "steps": 3,
"tool_calls": [{ "step": 1, "name": "rag_search", "arguments": {}, "ok": true, "error": "" }],
"usage": { "total_tokens": 1200 }, "elapsed_ms": 4200, "error": "" }`

### POST `/api/v1/agent/brainstorm`
Body：

```json
{ "topic": "…", "hashes": ["…"],
  "agents": [{ "id": "methodologist", "name": "方法学家", "persona": "…",
               "provider": "deepseek", "model": "deepseek-chat",
               "tools": ["rag_search"], "hashes": ["…"] }],
  "rounds": 2, "moderator": { "name": "主持人", "model": "deepseek-chat" } }
```

返回：`{ "session_id": "…" }`；随后通过 SSE 观察进度。

### GET `/api/v1/agent/sessions`
参数：`kind`（`qa|brainstorm`）、`page`、`page_size`。

### GET `/api/v1/agent/sessions/{session_id}`
返回会话详情：`session`、`agents`、`messages`（按 `agent_id` 分组）。

### GET `/api/v1/agent/sessions/{session_id}/events`  (SSE)
事件：

```
event: round
data: {"round": 1, "phase": "independent", "agents": ["methodologist", "skeptic"]}

event: message
data: {"agent_id": "methodologist", "agent_name": "方法学家", "round": 1, "text": "…",
       "citations": [ … ]}

event: blackboard
data: {"round": 1, "summary": "…"}

event: synthesis
data: {"text": "…", "consensus": [ … ], "disagreements": [ … ], "open_questions": [ … ]}

event: done
data: {"session_id": "…"}
```

### GET `/api/v1/agent/sessions/{session_id}/blackboard`
返回会话的黑板记录（唯一的跨智能体信息通道）。

```json
{ "items": [{ "id": 1, "round": 1, "author_agent": "moderator",
  "entry_type": "summary", "content": "…", "citations": [] }] }
```

### POST `/api/v1/agent/sessions/{session_id}/message`
在已有会话中追加一轮用户发言。Body：`{ "content": "…" }` → `{ "ok_detail": {} }`

### DELETE `/api/v1/agent/sessions/{session_id}`
删除会话。

---

## 9. 技能

### GET `/api/v1/skills`
返回：`{ "items": [{ "id": "…", "name": "…", "description": "…", "version": "1.0.0",
"enabled": true, "path": "…", "tools": ["…"], "installed_at": "…" }] }`

### GET `/api/v1/skills/{skill_id}`
技能详情：frontmatter、正文指令、声明的工具、脚本清单。

### POST `/api/v1/skills/install`
Body：`{ "path": "D:\\skill.zip", "enable": true }` 或上传 `multipart/form-data` 的
`file` 字段（zip）。返回安装后的技能对象。

### POST `/api/v1/skills/upload`
`multipart/form-data` 上传 `.zip` 安装。查询参数：`enable`、`overwrite`。
返回安装后的技能对象。

### PATCH `/api/v1/skills/{skill_id}`
Body：`{ "enabled": false }`

### DELETE `/api/v1/skills/{skill_id}`
卸载（删除目录与登记）。

### POST `/api/v1/skills/{skill_id}/reload`
重新读取 `SKILL.md`。

---

## 10. 文件与路径

### GET `/api/v1/files/download?path=<rel>`
下载数据目录内的文件。路径必须位于数据目录内，否则 `PATH_OUT_OF_SCOPE`。

### GET `/api/v1/files/raw?path=<rel>`
内联返回（用于图片、PDF 预览）。

### GET `/api/v1/files/list?path=<rel>`
列出数据目录（或指定子目录）的内容，供 OKF 浏览器的兜底视图使用。

```json
{ "path": "papers/3f2a…", "entries": [
  { "name": "fig", "is_dir": true, "size": 0, "path": "papers/3f2a…/fig" },
  { "name": "paper.pdf", "is_dir": false, "size": 524288, "path": "papers/3f2a…/paper.pdf" }] }
```

---

## 11. SSE 通用约定

- 所有 SSE 端点使用 `text/event-stream`，禁用缓冲（`X-Accel-Buffering: no`）。
- 每条消息形如 `event: <name>\ndata: <json>\n\n`。
- 心跳：每 15 秒发送注释行 `: ping`。
- 客户端断开时服务端须停止生成（监听 `Request.is_disconnected()`）。
- 结束一律发送 `event: done`；异常发送 `event: error` 后关闭。
