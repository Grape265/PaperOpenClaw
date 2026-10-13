# PaperOpenClaw

**PaperOpenClaw** 是一个基于 OpenClaw 式智能体架构的**跨平台研究论文知识库**：导入 PDF 后自动完成
版面分析、图表裁剪、OKF 忠实存储、A/B/C 金字塔 RAG、双图库（鱼骨 + 知识图谱），并提供检索问答、
多智能体头脑风暴与技能系统。

> 后端 Python FastAPI + 前端 Vue 3；桌面端用 **Tauri 2**（Rust 外壳 + 系统 WebView）做成原生 GUI，
> 界面渲染不依赖 Python。同时支持 Docker 部署与源码分发包安装。

软件采用 OpenClaw 式分层：**网关（Gateway）统一出入口，智能体运行时（Agent Runtime）承担推理循环，
能力通过工具注册表（Tool Registry）与技能加载器（Skill Loader）挂载，记忆通过会话与 RAG/图谱沉淀，
多智能体通过路由与隔离并行。**

- 本地优先：论文、图表、向量、图谱全部落在本地数据目录，可整目录迁移与备份
- 零外部服务：SQLite + ChromaDB 嵌入式，无数据库/消息队列/图数据库需要常驻
- 跨操作系统：Windows / macOS / Linux 同一套代码，路径一律经 `pathlib` 处理
- 内容寻址：论文以 SHA-256 作为唯一标识与目录名，天然去重、可校验
- 大模型全部通过 API Key 接入；嵌入方案本地模型 → 远程接口 → 离线词法三级降级

---

## 核心特性

- **PDF → 知识库全自动流水线（七阶段）**
  - 版面分析：PyMuPDF 自动裁剪图/表为 `fig1.png` / `tab1.png`（正文里的 "fig1" 字样不会误触发）
  - 解析：优先「PDF→PNG→多模态 LLM」，未配置/失败时回退本地 PyMuPDF 抽取
  - **OKF 忠实存储**：整篇 PDF 完整原文按页原样落盘（不删减），模型只做结构化
- **A/B/C 金字塔 RAG**：`C`=摘要原文 → `B`=七维要点 → `A`=逐节大意；检索 C→B→A，必要时回溯 OKF 原文
- **双图库**（独立存储、互不混淆）
  - **鱼骨图**：七维骨架（背景/数据/方法/实验/结论/讨论/不足与展望），要点从 OKF 用 LLM 提取
  - **知识图谱**：气泡+连线，关键词从 OKF 用 LLM 提取并过滤通用词（「影响」「作者」不入库），
    支持拖拽节点、斥力布局、展开/收起论文节点、悬停显示论文名
- **检索问答**：仿 DeepSeek 布局、流式回答、引用溯源、对话列表可收起
- **多智能体头脑风暴**：微信群聊式界面，逐轮暂停，可发言引导方向或「不发言」继续
- **技能系统**：从目录/zip 安装（SKILL.md + 脚本），隔离执行
- **跨库一致性**：哈希寻址、外键级联、向量/图/文件三处同步清理

## 架构

```
┌─────────────────────────────────────────────────────┐
│  桌面 GUI（Tauri 2 · Rust + 系统 WebView）          │   ← 不依赖 Python 渲染
│  原生窗口加载 Vue 前端，启动时自动拉起后端           │
└───────────────────────┬─────────────────────────────┘
                        │ HTTP /api/v1
┌───────────────────────▼─────────────────────────────┐
│  后端 Python FastAPI（单进程 = API + 前端托管）       │
│  gateway → agent → ingest → memory → storage → llm   │
│  · RAG 金字塔 / 向量库(ChromaDB) / 双图库 / OKF      │
│  · 多智能体 / 技能加载 / 子代理隔离                   │
└─────────────────────────────────────────────────────┘
```

## 目录结构

```
PaperOpenClaw/
├── docs/                     需求、架构、接口契约、运维、打包分发
├── backend/paperopenclaw/    后端（Python 3.9+）
│   ├── core/                 哈希、路径布局、文本、事件总线、日志
│   ├── llm/                  OpenAI 兼容提供方、PDF/图像输入、提示词
│   ├── memory/               嵌入、词法降级、向量库、OKF、级联检索
│   ├── kg/                   七维骨架、NetworkX 视图
│   ├── storage/              SQLite schema 与仓储层（含双图库）
│   ├── ingest/               入库管线：裁剪、解析、OKF、金字塔、双图
│   ├── agent/                运行时、工具、技能、隔离子智能体、问答与头脑风暴
│   ├── gateway/              FastAPI 路由与 SSE
│   └── desktop.py            备用桌面入口（pywebview）
├── web/                      前端（Vue 3 + Vite + TypeScript）
├── desktop/                  Tauri 2 桌面工程（src-tauri + 图标 + 构建脚本）
├── packaging/                PyInstaller 打包规格（备用）
├── scripts/                  启动、测试、契约校验、构建脚本
├── tests/                    158 个测试用例
├── examples/skills/          技能示例
├── Dockerfile / docker-compose.yml
└── README.md
```

## 快速开始（开发）

### 1. 环境要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.9 或更高 | 后端与入库管线 |
| Node.js | 18 或更高 | 构建前端 |

### 2. 一键启动

```powershell
.\scripts\start.ps1          # Windows
./scripts/start.sh           # macOS / Linux
```

脚本自动检查 Python → 必要时装依赖（清华镜像）→ 首次构建前端 → 启动并打开浏览器，
默认 <http://127.0.0.1:8787>。

### 3. 手动启动

```bash
pip install -e backend       # 或 pip install backend
cd web && npm install && npm run build && cd ..
python -m paperopenclaw serve
```

### 4. 首次配置

在「设置」页：① 添加模型提供方（DeepSeek / 百炼 / Moonshot / 智谱 / SiliconFlow / OpenAI / Ollama 预设）；
② 指定模型路由——**解析模型**（需支持 PDF/图像，用于解析）、**问答模型**、**摘要模型**（可留空复用问答）。
嵌入默认 `auto`：本地模型 → 远程接口 → 离线词法三级降级，完全离线也能建索引。

---

## 使用流程

1. **导入**：上传 PDF（可开关「使用 LLM 解析」，关闭则纯本地解析），七阶段实时显示进度。
2. **论文库**：按论文名/作者/期刊/年份/哈希检索，多选共享给问答、头脑风暴、图谱。
3. **论文详情**：概览、PDF 原文、OKF 文档树、图表、金字塔、鱼骨图谱。
4. **问答**：C→B→A 级联检索，回答附来源编号，库中无相关内容会明说、不编造。
5. **头脑风暴**：选角色与轮数，逐轮暂停可引导或跳过，最后给共识/分歧/待研究问题。
6. **知识图谱**：气泡+连线，拖拽节点、展开/收起论文节点、导出 JSON/GraphML。
7. **技能**：从目录/zip 安装，`examples/skills/numeric-check/` 是可安装的完整示例。

## 命令行

```powershell
python -m paperopenclaw serve                  # 启动服务
python -m paperopenclaw ingest D:\papers       # 批量导入目录
python -m paperopenclaw ingest a.pdf --json    # 导入单篇并输出 JSON
python -m paperopenclaw doctor                 # 环境诊断
python -m paperopenclaw info                   # 打印配置与统计
python -m paperopenclaw rebuild-kg             # 从 OKF 重建双图库
python -m paperopenclaw okf-verify <bundle>    # 校验 OKF Bundle
```

通用参数：`--data-dir <路径>`（或环境变量 `PAPEROPENCLAW_DATA_DIR`）。

## 数据目录

默认 `./data`，可整体拷贝迁移：

```
data/
├── config.json                    运行配置（含 API Key）
├── db/paperopenclaw.sqlite3       论文/作者/单位/资产/语义单元/双图库/会话/技能
├── db/vectors/                    ChromaDB：rag_a / rag_b / rag_c
├── papers/<sha256>/
│   ├── paper.pdf                  原始 PDF，不修改
│   ├── meta.json                  解析快照
│   ├── fig/fig1.png · tab/tab1.png
│   └── okf/                       OKF v0.2：index.md / log.md / paper.md /
│                                  fulltext.md（完整原文）/ sections/ / figures/ / tables/
├── skills/<id>/SKILL.md
├── agents/<session>/<agent>/      子智能体隔离工作目录
└── logs/
```

## 部署与分发

| 方式 | 命令 | 说明 |
| --- | --- | --- |
| **Docker** | `docker compose up -d` | 容器化网页部署，见 `Dockerfile` / `docker-compose.yml` |
| **桌面 GUI** | `desktop/build.ps1` / `desktop/build.sh` | Tauri 原生窗口，产物约 8MB |
| **源码分发包** | `cd backend && python -m build` | 产出 `paperopenclaw-1.0.0.tar.gz` + `.whl` |

详见 [`docs/05-packaging.md`](docs/05-packaging.md)。

## 开发

```powershell
python scripts/run_tests.py                 # 158 项测试（逐模块子进程隔离）
python scripts/check_contract.py            # 前端调用 ↔ 后端路由契约校验
python tests/test_ingest.py                 # 七阶段管线端到端（合成 PDF + 模拟模型）
cd web && npm run dev                       # 前端开发服务器（代理 /api 到后端）
```

### 测试策略

| 层次 | 方式 |
| --- | --- |
| 单元测试 | 纯函数与仓储，临时目录中的真实 SQLite |
| 模型交互 | 本地模拟的 OpenAI 兼容服务，按 system 提示词分发响应 |
| 入库管线 | 合成三页 PDF（含图注图与表注表）跑完整七阶段 |
| 网关 | 真实 uvicorn + httpx，覆盖 Range 请求与 SSE 流 |
| 契约 | 前端调用清单与后端 OpenAPI 做差集 |

不依赖 pytest，也不依赖任何网络访问。

## 设计要点

| 决策 | 理由 |
| --- | --- |
| 视觉大模型直读 PDF，几何裁剪仍由 PyMuPDF 完成 | 坐标是确定性问题，交给模型会得到「看似合理」的错误框 |
| OKF 保存忠实全文，蒸馏只进 RAG | Bundle 保持可读、可 diff、可迁移的档案属性 |
| C 层直接取 abstract，不由模型生成 | 金字塔顶点是客观事实，检索入口才可信 |
| 层间关联用关系表，不用向量 | 层级结构是确定性的，查表比向量召回精确 |
| 混合检索（向量 + BM25 + RRF） | 术语、指标名、编号的精确匹配是纯向量的弱项 |
| 鱼骨/知识图谱拆成两个独立图库 | 来源与语义不同（七维要点 vs 关键词），分别存储互不混淆 |
| 黑板是唯一的跨智能体通道 | 共享完整上下文会互相污染并让讨论退化成一个声音 |
| SSE 而非 WebSocket | 全是单向推送，HTTP 语义简单，代理友好 |

## 文档

| 文档 | 内容 |
| --- | --- |
| [01-requirements.md](docs/01-requirements.md) | 需求规格 |
| [02-architecture.md](docs/02-architecture.md) | 架构设计：分层、数据模型、管线、算法、ADR |
| [03-api-contract.md](docs/03-api-contract.md) | 接口契约：全部路由、请求响应、SSE 事件 |
| [04-operations.md](docs/04-operations.md) | 运维：部署、备份、排障、性能与安全 |
| [05-packaging.md](docs/05-packaging.md) | 打包分发：Docker、Tauri 桌面、sdist |

## 许可证

MIT
