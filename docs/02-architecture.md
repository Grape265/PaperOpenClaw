# PaperOpenClaw 软件架构设计

版本：v1.0
对应需求：`docs/01-requirements.md`

---

## 1. 总体架构：OpenClaw 式分层

PaperOpenClaw 采用 OpenClaw 的架构范式：**网关统一出入口，智能体运行时承担推理循环，
能力通过工具与技能挂载，记忆通过会话与 RAG/图谱沉淀，多智能体通过路由与隔离并行。**

```
┌──────────────────────────────────────────────────────────────────────┐
│                          前端 Web（Vue 3）                            │
│   库 / 导入 / 论文详情 / 问答 / 头脑风暴 / 图谱 / 技能 / 设置           │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ HTTP + SSE（契约见 docs/03-api-contract.md）
┌───────────────────────────▼──────────────────────────────────────────┐
│ Gateway 网关层  (FastAPI)                                             │
│  路由 · 依赖注入 · 参数校验(Pydantic) · SSE 事件流 · 静态资源 · 鉴权占位 │
└───────┬───────────────────────────────┬──────────────────────────────┘
        │                               │
┌───────▼──────────────┐      ┌─────────▼───────────────────────────────┐
│ Agent Runtime 运行时  │      │ Ingest Pipeline 入库管线                 │
│  Agent Loop          │      │  哈希 → 去重 → 落盘 → 图表裁剪 →          │
│  Context Engine      │      │  视觉模型解析 → OKF 落盘 →                │
│  Tool Registry       │      │  金字塔 RAG → 鱼骨图谱                    │
│  Skill Loader        │      └─────────┬───────────────────────────────┘
│  Sub-agent Router    │                │
│  Session Store       │                │
└───────┬──────────────┘                │
        │                               │
┌───────▼───────────────────────────────▼──────────────────────────────┐
│ Memory 记忆层                                                        │
│   RAG Store（A/B/C 金字塔 + 层间关联 + C-C 跨论文关联）                │
│   KG Store（七维鱼骨骨架 + 内容节点 + 图表链接）                       │
│   Paper Registry（论文表/作者/单位/资产/OKF 概念索引）                 │
└───────┬──────────────────────────────────────────────────────────────┘
        │
┌───────▼──────────────────────────────────────────────────────────────┐
│ Storage 存储层                                                       │
│   SQLite（关系数据） · ChromaDB（向量） · 文件系统（PDF/图表/OKF）      │
└──────────────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────────┐
│ LLM Provider 抽象层                                                   │
│   OpenAI 兼容协议 · 视觉/文档输入 · JSON 结构化输出 · 流式 · 重试       │
│   解析模型 / 问答模型 / 嵌入模型 三路可分别配置                         │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.1 分层职责

| 层 | 职责 | 不做什么 |
| --- | --- | --- |
| Gateway | 协议转换、校验、鉴权、流式推送 | 不含业务逻辑 |
| Agent Runtime | 推理循环、上下文装配、工具调度、子智能体编排 | 不直接读写数据库 |
| Ingest Pipeline | 把 PDF 变成四类产物（文件/OKF/向量/图谱） | 不处理 HTTP |
| Memory | 检索、过滤、关联、聚合 | 不调用大模型 |
| Storage | 持久化与事务 | 不含语义逻辑 |
| LLM Provider | 统一各家 API 差异 | 不含提示词策略 |

**依赖方向单向向下**：Gateway → Runtime/Pipeline → Memory → Storage。反向依赖通过
事件与回调实现（入库进度事件）。

---

## 2. 目录结构

### 2.1 代码仓库

```
PaperOpenClaw/
├── README.md
├── docs/
│   ├── 01-requirements.md
│   ├── 02-architecture.md
│   ├── 03-api-contract.md
│   └── 04-operations.md
├── backend/
│   ├── pyproject.toml
│   ├── requirements.txt
│   └── paperopenclaw/
│       ├── __main__.py            # CLI：serve / ingest / doctor / okf-verify
│       ├── config.py              # 配置模型与持久化
│       ├── core/                  # 无依赖的通用能力
│       │   ├── hashing.py         # SHA-256 流式计算
│       │   ├── paths.py           # 数据目录布局（内容寻址）
│       │   ├── events.py          # 进程内事件总线（入库/智能体进度）
│       │   ├── jsonutil.py        # 宽松 JSON 解析（剥离 ```json 围栏等）
│       │   └── logging.py
│       ├── llm/
│       │   ├── base.py            # LLMProvider 协议、Message/ToolSpec/LLMResult
│       │   ├── openai_compat.py   # OpenAI 兼容实现（含视觉与工具调用）
│       │   ├── registry.py        # 提供方注册与「解析/问答/嵌入」路由
│       │   ├── pdf_input.py       # PDF → 视觉消息（分页/分批/降采样）
│       │   └── prompts/           # 提示词模板（解析、压缩、问答、头脑风暴）
│       ├── storage/
│       │   ├── db.py              # SQLite 连接、迁移、事务
│       │   ├── schema.sql         # 全量建表语句
│       │   └── repos/             # 仓储层
│       │       ├── papers.py      # 论文表/作者/单位/资产
│       │       ├── okf.py         # OKF 概念索引
│       │       ├── rag.py         # 语义单元与层间关联
│       │       ├── kg.py          # 图谱节点与边
│       │       ├── sessions.py    # 会话与消息
│       │       └── skills.py      # 技能登记
│       ├── ingest/
│       │   ├── pipeline.py        # 编排七个阶段，发布进度事件
│       │   ├── pdf_layout.py      # PyMuPDF 图表检测与裁剪
│       │   ├── vision_parse.py    # 视觉模型解析（分批+合并+校验重试）
│       │   ├── okf_writer.py      # OKF Bundle 生成与校验
│       │   ├── pyramid.py         # A/B/C 三层构建与关联
│       │   └── kg_builder.py      # 鱼骨图谱构建
│       ├── memory/
│       │   ├── embeddings.py      # 嵌入提供方：local / remote / lexical
│       │   ├── lexical.py         # jieba 分词 + BM25 + 字符 n-gram 哈希向量
│       │   ├── vector_store.py    # ChromaDB 三集合封装
│       │   ├── retriever.py       # C→B→A 级联检索 + 混合重排
│       │   └── okf_fs.py          # OKF Bundle 读取/遍历
│       ├── kg/
│       │   ├── skeleton.py        # 七维骨架定义与章节映射
│       │   └── graph.py           # NetworkX 构图/查询/导出
│       ├── agent/
│       │   ├── runtime.py         # Agent Loop
│       │   ├── context.py         # Context Engine（系统提示装配）
│       │   ├── tools/             # 内置工具
│       │   ├── skill_loader.py    # SKILL.md 发现与装载
│       │   ├── subagent.py        # 子智能体定义与隔离策略
│       │   ├── brainstorm.py      # 头脑风暴编排
│       │   └── qa.py              # RAG 问答编排
│       └── gateway/
│           ├── app.py             # create_app()
│           ├── deps.py            # 依赖注入容器
│           ├── sse.py             # SSE 工具
│           └── routers/           # 按资源划分路由
├── web/                           # Vue 3 前端
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── api/                   # 与后端契约一一对应的调用层
│       ├── views/
│       ├── components/
│       ├── stores/
│       ├── router/
│       └── styles/
├── scripts/                       # 启动/构建脚本（跨平台）
└── tests/
```

### 2.2 运行时数据目录

默认 `./data`，可在设置中修改。整目录可迁移、可备份。

```
data/
├── config.json                    # 运行配置（含 API Key，权限 600）
├── db/
│   ├── paperopenclaw.sqlite3      # 关系库（论文/作者/资产/语义单元/图谱/会话）
│   └── vectors/                   # ChromaDB 持久化目录
│       ├── rag_c/                 # 顶层：abstract
│       ├── rag_b/                 # 中层：鱼骨维度要点
│       └── rag_a/                 # 底层：章节级详细大意
├── papers/
│   └── <sha256>/
│       ├── paper.pdf              # 原始 PDF（FR-8.1）
│       ├── meta.json              # 解析快照与来源信息
│       ├── fig/
│       │   ├── fig1.png
│       │   └── fig2.png
│       ├── tab/
│       │   ├── tab1.png
│       │   └── tab2.png
│       └── okf/                   # OKF v0.2 Bundle（FR-5）
│           ├── index.md
│           ├── log.md
│           ├── paper.md           # type: Research Paper
│           ├── sections/
│           │   ├── index.md
│           │   └── <slug>.md
│           ├── figures/
│           │   ├── index.md
│           │   └── fig1.md
│           └── tables/
│               ├── index.md
│               └── tab1.md
├── skills/
│   └── <skill-id>/
│       ├── SKILL.md
│       └── scripts/
└── agents/                        # 子智能体隔离工作目录
    └── <session-id>/
        └── <agent-id>/
```

**路径安全**：所有对外暴露的文件读取一律经 `core/paths.py` 解析为绝对路径后校验其位于
数据目录内，杜绝路径穿越。

---

## 3. 数据模型

### 3.1 关系库（SQLite）

启用 `PRAGMA foreign_keys=ON`、`journal_mode=WAL`、`busy_timeout=5000`。

核心表与关联（完整 DDL 见 `backend/paperopenclaw/storage/schema.sql`）：

| 表 | 作用 | 关键约束 |
| --- | --- | --- |
| `papers` | 论文主表 | `hash` 主键 + 唯一索引（FR-2.2） |
| `authors` | 作者 | `(paper_hash, order_index)` |
| `affiliations` | 单位 | `(paper_hash, order_index)` |
| `author_affiliations` | 作者-单位多对多 | 联合主键 |
| `assets` | 图表资产 | `(paper_hash, label)` 唯一，`file_path` 指向 fig/tab |
| `okf_concepts` | OKF 概念索引 | `(paper_hash, concept_id)` 唯一 |
| `rag_units` | 语义单元 A/B/C | 主键为语义化 ID：`a:<hash>:<n>` / `b:<hash>:<dim>` / `c:<hash>` |
| `rag_links` | 金字塔层间关联 | `(src_unit, dst_unit, relation)` 唯一 |
| `kg_nodes` / `kg_edges` | 知识图谱 | `node_key` 唯一 |
| `sessions` / `messages` | 会话与消息 | 级联删除 |
| `agents` | 子智能体实例 | 含工具白名单与工作目录 |
| `skills` | 技能登记 | 主键为目录名 |
| `jobs` | 异步任务 | 入库/重建任务的进度载体 |

**级联一致性（FR-13.5）**：`papers` 删除时，`authors`、`affiliations`、`assets`、
`okf_concepts`、`rag_units`、`kg_nodes` 均 `ON DELETE CASCADE`；向量库与文件目录由
服务层在同一事务意图内显式清理，删除操作整体记录到 `jobs` 以便审计。

### 3.2 向量库（ChromaDB）

三个持久化集合，与关系库通过 `rag_units.id` 一一对应：

| 集合 | 内容 | 元数据 |
| --- | --- | --- |
| `rag_c` | 论文 abstract | `hash, title, journal, year, language` |
| `rag_b` | 七维维度要点 | `hash, section, ordinal` |
| `rag_a` | 章节级详细大意 | `hash, section, ordinal, source_ref` |

- 距离度量统一 `cosine`。
- 文档 ID 即 `rag_units.id`，因此向量库与关系库天然对齐，重建时可全量重灌（FR-13.2）。
- 每层的向量维度由当前嵌入提供方决定，维度记录在 `jobs`/配置中；更换嵌入模型时提供
  「重建向量库」操作。

### 3.3 文件系统

- PDF、图表、OKF 均为普通文件，是**可读的真相来源**（FR-13.4）。
- 关系库中 `assets.file_path`、`okf_concepts.file_path` 存相对路径，保证数据目录可整体搬迁。

### 3.4 图谱

- 关系库 `kg_nodes`/`kg_edges` 为持久化真相；NetworkX 图为查询期在内存构建的视图（FR-7.6 导出用）。
- 节点键约定：
  - 骨架：`spine:<section-slug>`（全局共享，只有七个）
  - 论文归属：`paper:<hash>`
  - 内容：`content:<hash>:<section-slug>:<n>`
  - 图表：`asset:<hash>:<label>`
- 边类型：`has_spine`（论文→骨架）、`spine_order`（骨架→骨架，体现行文顺序）、
  `has_content`（论文→内容）、`belongs_to`（内容→骨架）、`has_asset`（内容→图表）、
  `mentions`（跨论文内容关联）。

---

## 4. 入库管线

七个阶段，每阶段发布进度事件（Gateway 转 SSE 推送前端）。

```
①  接收文件
      └─ 流式计算 SHA-256（64KB 分块）
②  去重
      ├─ 命中 → 直接返回既有论文记录
      └─ 未命中 → 建 papers 记录（status=parsing）+ 建 job
③  落盘
      └─ papers/<hash>/paper.pdf，建 fig/ tab/ okf/ 空目录
④  本地版面分析（PyMuPDF，不调用模型）
      ├─ 逐页文本抽取（用于标题/图注定位与降级）
      ├─ 图像与矢量簇检测 → 候选区域
      ├─ 图注正则匹配（Figure/Fig./Table/图/表 + 序号）→ 修正区域
      └─ 200 DPI 裁剪 → fig/figN.png、tab/tabN.png，写 assets 表
⑤  视觉模型解析
      ├─ PDF 按页分批（默认 ≤ 20 页/批）转图像化消息
      ├─ 严格 JSON Schema 提示，要求返回元数据 + 章节 + 图表说明
      ├─ 合并分批结果，冲突以首批元数据为准
      └─ 校验失败 → 降温重试（最多 N 次）→ 仍失败标记 failed 并留原始响应
⑥  OKF 落盘 + 金字塔构建 + 图谱构建
      ├─ okf_writer：写 index.md / log.md / paper.md / sections/* / figures/* / tables/*
      ├─ pyramid：A=章节级详细大意（由章节正文精简）；B=七个维度要点（由 A 再精简）；
      │           C=abstract 原文；建 A→B、B→C、C→C 关联
      ├─ 向量化并写入三个 Chroma 集合
      └─ kg_builder：七维骨架 + 内容节点 + 图表链接
⑦  完成
      └─ papers.status=ready，写 meta.json，job 完成
```

**失败语义**：任一阶段失败即 `status=failed` 并记录 `error`，已产生的文件保留，
支持「重新解析」从④或⑤重跑。

---

## 5. 金字塔 RAG 设计（对应 FR-6）

### 5.1 三层语义

| 层 | 来源 | 粒度 | 数量级 |
| --- | --- | --- | --- |
| C | 论文 abstract | 每篇 1 条 | 论文数 |
| B | 由 A 再精简，按七维归并 | 每篇 ≤ 7 条 | 7 × 论文数 |
| A | 由原文精简的详细大意，按章节切分 | 每篇 5–20 条 | ~10 × 论文数 |

**关键点**：C 不是「从 A 提炼」而是直接取 abstract 原文（FR-6.3），因此 C 层是客观
事实层，不受模型压缩质量影响；B 由 A 提炼而非直接从原文提炼，保证「再精简」语义
（FR-6.2）。

### 5.2 层间关联

```
        ┌─────────┐        ┌─────────┐
        │  C(论文1)│◄──────►│  C(论文2)│   c_to_c：跨论文
        └────┬────┘        └────┬────┘
             │ b_to_c           │
        ┌────▼────┐              │
        │  B(论文1)│  七维要点     │
        └────┬────┘              │
             │ a_to_b           │
        ┌────▼────┐              │
        │  A(论文1)│  章节级大意   │
        └─────────┘              │
```

- `a_to_b`：A 单元按 `section` 归属到对应 B 单元，`weight` 取该章节正文字数占比。
- `b_to_c`：论文的所有 B 单元指向其 C 单元。
- `c_to_c`：跨论文建立，权重为 C 向量余弦相似度；另叠加「标题词重叠」与「同年同刊」信号。
  仅保留每篇 top-k（默认 k=5）且相似度超过阈值的边，避免完全图爆炸。

### 5.3 检索级联（对应 FR-11.2）

```
query
  │
  ├─ ① 嵌入 + 词法混合检索 rag_c（限论文范围）
  │      命中 → 得到候选论文集合 P，按分数取 top_n_papers
  │
  ├─ ② 由 b_to_c 反向扩展到 P 的 B 单元
  │      在 B 单元上重排（向量 + BM25 + RRF 融合）
  │      命中 → 得到候选维度集合 S
  │
  ├─ ③ 由 a_to_b 反向扩展到 S 对应 A 单元
  │      在 A 单元上重排 → 最终证据片段
  │
  └─ ④ 组装上下文（带引用：哈希 / 论文名 / 层 / 章节或维度）
```

**停止条件**：若 C 层最强分数低于阈值，直接判定「库中无相关内容」（FR-11.4）；
若 B 层已足以回答（返回条数达到 `top_k` 且分数高于阈值），可跳过 A 层以节省 token。

**混合检索**：向量相似度解决语义，BM25 解决术语与编号（如 "BLEU"、"表 3"）的精确匹配，
二者用 Reciprocal Rank Fusion 融合，避免量纲不可比问题。

---

## 6. 鱼骨知识图谱设计（对应 FR-7）

### 6.1 七维骨架

固定顺序，构成「鱼骨」主干（FR-7.2）：

| 序号 | 维度 | slug | 章节关键词映射 |
| --- | --- | --- | --- |
| 1 | 背景 | `background` | introduction, background, motivation, related work, 引言, 背景, 相关工作 |
| 2 | 数据 | `data` | dataset, data, corpus, benchmark, 数据, 数据集, 语料 |
| 3 | 方法 | `method` | method, approach, model, architecture, 方法, 模型, 架构 |
| 4 | 实验 | `experiment` | experiment, evaluation, result, ablation, 实验, 评估, 结果 |
| 5 | 结论 | `conclusion` | conclusion, 结论 |
| 6 | 讨论 | `discussion` | discussion, analysis, 讨论, 分析 |
| 7 | 不足与展望 | `limitation` | limitation, future work, 不足, 局限, 展望, 未来工作 |

章节归属由规则匹配 + 模型判定双重确认；无法归属的章节进入最接近的维度，
并在节点 `meta` 中标记 `mapped_by=fallback`。

### 6.2 挂载策略

- 每个维度 B 单元 → 一个或多个 `content` 节点（按句子/要点拆分，保留 `section` 与顺序）。
- 内容节点中若引用了 `figN`/`tabN`（由模型在解析时标注），建立 `has_asset` 边，
  边与节点均只存**文件地址链接**，不复制图片数据（FR-7.4）。
- 跨论文：同名维度下内容节点通过共享骨架节点连通；C 层相似度高的论文之间加 `mentions` 边。

### 6.3 查询与导出

- 单篇鱼骨：返回七个骨架节点及其子树。
- 跨论文：给定多个哈希，返回并集子图，骨架节点合并（体现「横向对比」）。
- 导出：JSON（节点/边数组）与 GraphML（供外部图工具打开）。

---

## 7. 智能体运行时设计（对应 FR-9、FR-10、FR-11）

### 7.1 Agent Loop

```
装配上下文 → 调用模型 → 若有 tool_calls → 执行工具 → 结果回填 → 再调用模型
     ▲                                                            │
     └──────────────── 直到无工具调用或达到 max_steps ◄────────────┘
```

- 工具执行受**智能体工具白名单**限制（隔离要求）。
- 每步记录到 `messages`，前端可回放。
- 单步超时、总步数上限、工具错误均不抛出到网关，而是作为工具结果回填给模型自行修正。

### 7.2 Context Engine

系统提示按固定顺序拼装：

1. 角色与身份（persona）
2. 任务约束（必须基于 RAG 作答、必须给引用、无内容须明说）
3. 可用技能指令（已启用 Skill 的正文）
4. 工具清单（名称 + 描述 + 参数 schema）
5. 记忆上下文（由检索得到的证据片段，带引用编号）
6. 用户输入

### 7.3 工具注册表

内置工具：

| 工具 | 作用 | 归属 |
| --- | --- | --- |
| `rag_search` | C→B→A 级联检索 | 全部智能体 |
| `paper_meta` | 取论文元数据/作者/单位 | 全部 |
| `kg_query` | 查询鱼骨图谱子图 | 全部 |
| `okf_read` | 读取 OKF 概念文档 | 全部 |
| `asset_list` | 列出论文图表 | 全部 |
| `py_eval` | 受控 Python 计算（仅计算，无文件/网络） | 可白名单开启 |
| Skill 工具 | 由 Skill 声明 | 装载后按需授权 |

### 7.4 子智能体隔离（FR-10.2）

隔离体现在四个维度：

| 维度 | 实现 |
| --- | --- |
| 上下文隔离 | 每个子智能体独立 `session` 视图，`messages` 按 `agent_id` 隔离 |
| 工作目录隔离 | `data/agents/<session-id>/<agent-id>/`，工具只能写自己的目录 |
| 工具隔离 | `agents.tools` 白名单，未授权工具调用被拒绝 |
| 论文范围隔离 | 每个智能体可限定可见 `paper_hashes` 子集 |

### 7.5 头脑风暴编排（FR-10.4/10.5/10.6）

```
第 0 轮：主持人 → 拆解议题，产出 N 个子问题（不共享给子智能体彼此）
第 1 轮：N 个子智能体独立分析（隔离，互不可见）
         └─ 各自调用 rag_search 取证据，产出「观点 + 证据 + 置信度」
第 2..k 轮：主持人生成「黑板摘要」（只含观点摘要，不含完整上下文）
         └─ 各子智能体基于黑板摘要补充/反驳，仍相互隔离
末轮：主持人汇总 → 结构化结论（共识 / 分歧 / 证据 / 后续问题）
```

黑板是唯一跨智能体通道，避免上下文互相污染，同时保证信息可追溯。

---

## 8. 前后端通信设计（对应 FR-12、NFR-2）

### 8.1 协议

| 场景 | 协议 | 说明 |
| --- | --- | --- |
| 常规请求 | HTTP JSON | 统一响应包裹（见下） |
| 入库进度 | SSE `/api/v1/jobs/{id}/events` | 阶段事件流 |
| 问答流式 | SSE `/api/v1/agent/qa/stream` | token 增量 + 引用块 |
| 头脑风暴 | SSE `/api/v1/agent/sessions/{id}/events` | 按智能体分发的发言流 |
| 大文件（PDF/图片） | HTTP 静态流（支持 Range） | PDF 阅读器需要分段加载 |

### 8.2 统一响应包裹

```json
{ "ok": true,  "data": { }, "error": null, "meta": { "request_id": "..." } }
{ "ok": false, "data": null, "error": { "code": "PAPER_NOT_FOUND", "message": "..." }, "meta": {} }
```

- 错误码为大写下划线常量，前端按码做提示，不解析 message 文案。
- 列表接口统一 `{ items, total, page, page_size }`。

### 8.3 前端契约对齐措施（FR-12.6）

1. `docs/03-api-contract.md` 为人读契约；后端 Pydantic 模型为机器契约。
2. 前端 `src/api/` 每个函数一一对应一个后端路由，命名与路径同构。
3. 提供 `scripts/check_contract.ps1` / `.sh`：抓取后端 OpenAPI 与前端调用清单做差集校验，
   有「前端调用但后端不存在」的接口即报错。
4. 前端调用层集中定义路径常量，禁止在组件里硬编码 URL。

---

## 9. 前端设计（对应 FR-12）

### 9.1 技术选型

Vue 3 + TypeScript + Vite + Vue Router + Pinia。无 UI 组件库（避免图标与主题耦合），
样式用原生 CSS 变量实现毛玻璃，保证可控且体积小。

### 9.2 页面结构

```
┌───────────────────────────────────────────────────────────────┐
│  侧栏（DeepSeek 式）          主区（多页面路由）                 │
│  ─ 论文库        /library     ┌───────────────────────────┐   │
│  ─ 导入          /ingest      │  页面标题 + 操作区（文字按钮）│   │
│  ─ 问答          /qa          ├───────────────────────────┤   │
│  ─ 头脑风暴      /brainstorm  │                           │   │
│  ─ 知识图谱      /graph       │      内容区（毛玻璃卡片）   │   │
│  ─ 技能          /skills      │                           │   │
│  ─ 设置          /settings    └───────────────────────────┘   │
│                                                               │
│  ─ 论文详情      /paper/:hash                                 │
└───────────────────────────────────────────────────────────────┘
```

### 9.3 视觉规范

| 元素 | 规范 |
| --- | --- |
| 背景 | 深色或浅色渐变底 + 2~3 个模糊光斑（`filter: blur(80px)`） |
| 卡片 | `background: rgba(255,255,255,.06)` + `backdrop-filter: blur(20px)` + 1px 半透明描边 |
| 圆角 | 卡片 16px，按钮 10px，输入框 10px |
| 按钮 | 纯文字，无图标；主按钮为渐变填充，次按钮为描边，危险操作为红色文字描边 |
| 字体 | 系统字体栈，中文优先 `Microsoft YaHei` / `PingFang SC` / `Noto Sans SC` |
| 禁用 | 全部 emoji 与图形符号，分隔与指示一律用文字或 CSS 绘制 |

### 9.4 论文选择器

跨页面复用的核心组件：支持

- 关键词搜索（论文名 / 作者 / 哈希）
- 按年份、期刊筛选
- 多选（勾选框为 CSS 绘制的方形，非 emoji）
- 全选/反选/清空
- 已选数量与哈希短码展示

选中集合存入 Pinia store，问答、头脑风暴、图谱、导出页面共享。

---

## 10. 跨操作系统与部署（对应 NFR-1）

| 关注点 | 方案 |
| --- | --- |
| 路径 | 一律 `pathlib.Path`，数据目录内相对路径入库，绝不硬编码分隔符 |
| 启动 | `python -m paperopenclaw serve`；提供 `scripts/start.ps1`（Windows）与 `scripts/start.sh`（Unix） |
| 前端构建 | Vite 产物由 FastAPI 挂载为静态目录，单进程单端口即可使用 |
| 数据库 | SQLite 文件 + ChromaDB 嵌入式，无外部服务 |
| 依赖 | 固定版本于 `requirements.txt`；国内环境提供镜像安装说明 |
| 编码 | 全链路 UTF-8；Windows 下显式设置控制台编码 |

---

## 11. 关键技术决策记录

| 编号 | 决策 | 理由 | 备选与否决原因 |
| --- | --- | --- | --- |
| ADR-1 | 用视觉大模型直读 PDF，而非本地版面模型 | 满足 FR-3.1，且无需下载大模型、跨平台一致 | MinerU 本地部署体积大、GPU 依赖强、跨平台差 |
| ADR-2 | 图表裁剪仍用 PyMuPDF 本地完成 | 裁剪是确定性几何操作，交给模型既慢又贵且不稳定 | 全交给模型：坐标不可靠 |
| ADR-3 | OKF v0.2 作为可读真相来源，SQLite 作为索引 | 满足 FR-5 与 NFR-8；文件可读可 diff 可迁移 | 纯 DB 存储：不可读、不可迁移 |
| ADR-4 | ChromaDB 嵌入式而非独立向量服务 | 零外部依赖、跨平台、Python 原生 | Milvus/Qdrant：需常驻服务 |
| ADR-5 | 层间关联用关系表而非向量 | 关联是确定性的层级结构，查表比向量召回精确 | 纯向量：会漏掉结构关系 |
| ADR-6 | 混合检索（向量 + BM25 + RRF） | 补偿嵌入模型对术语/编号的弱匹配 | 纯向量：术语检索差 |
| ADR-7 | 嵌入提供方三选一：本地模型 / 远程接口 / 离线词法 | NFR-6 离线降级；用户环境差异大 | 单一方案：总有一种环境不可用 |
| ADR-8 | 黑板模式作为唯一的跨智能体通道 | 满足隔离要求的同时保证信息流通可控 | 共享完整上下文：相互污染、token 爆炸 |
| ADR-9 | SSE 而非 WebSocket | 单向推送足够，HTTP 语义简单，跨平台与代理友好 | WebSocket：需额外心跳与重连管理 |
| ADR-10 | 前端不引入 UI 组件库 | 禁用图标与毛玻璃定制需求，组件库反而增加对抗成本 | Element Plus：图标体系与需求冲突 |

---

## 12. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 视觉模型对长 PDF 输出不稳定 | 解析失败 | 分批送模 + JSON 校验重试 + 原始响应留存 + 可重跑 |
| 大模型返回图表坐标不准 | 裁剪错误 | 坐标不从模型取，只从模型取「图表说明与顺序」，几何由 PyMuPDF 决定 |
| 嵌入模型下载失败 | 无法语义检索 | 三级降级：本地模型 → 远程嵌入接口 → 离线词法向量 |
| 跨论文 C-C 关联爆炸 | 图谱与存储膨胀 | 阈值 + 每篇 top-k + 仅存双向唯一边 |
| API Key 泄漏 | 安全事故 | 仅本地存储、前端掩码回显、日志脱敏 |
| 大 PDF 占用磁盘 | 空间不足 | 图表按需 200 DPI 且 PNG 优化；提供清理与压缩入口 |

---

## 13. 实施顺序

1. 骨架与配置 → 2. 存储层与论文表 → 3. LLM 抽象 → 4. 入库管线（含图表裁剪）
→ 5. OKF 落盘 → 6. 金字塔 RAG → 7. 鱼骨图谱 → 8. 智能体运行时与技能
→ 9. FastAPI 网关 → 10. Vue 前端 → 11. 测试与验收。
