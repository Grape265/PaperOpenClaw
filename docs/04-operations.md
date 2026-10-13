# PaperOpenClaw 运维手册

版本：v1.0

---

## 1. 部署

### 1.1 单机部署（推荐）

后端同时提供 API 与前端静态资源，**单进程单端口**即可对外服务：

```bash
cd backend
python -m paperopenclaw serve --host 127.0.0.1 --port 8787
```

生产环境建议用进程管理器托管，并显式指定数据目录：

```bash
PAPEROPENCLAW_DATA_DIR=/var/lib/paperopenclaw \
  python -m paperopenclaw serve --host 0.0.0.0 --port 8787 --no-browser
```

若前端未构建，`/` 会返回一段说明而非界面；接口与 `/docs` 仍可用。构建方式见 README。

### 1.2 资源需求

| 场景 | CPU | 内存 | 磁盘 |
| --- | --- | --- | --- |
| 仅使用远程模型与远程嵌入 | 2 核 | 1 GB | 每篇论文约 3–8 MB（PDF + 图表 + OKF） |
| 本地嵌入模型（bge-small 级） | 2 核 | 2 GB | 额外约 100 MB 模型 |
| 大批量导入（上千篇） | 4 核 | 4 GB | 向量库约为文本量的 1.5 倍 |

大模型调用是主要耗时，本地计算（版面分析、裁剪、哈希）在单篇论文上通常不到 2 秒。

### 1.3 反向代理

SSE 需要关闭缓冲，否则进度和流式回答会「攒着一起出」：

```nginx
location / {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    client_max_body_size 512m;
}
```

`proxy_read_timeout` 要足够长：解析一篇长论文可能持续数分钟。

### 1.4 对外暴露时的鉴权

默认监听 `127.0.0.1`，不鉴权也不暴露。若确实需要对外提供服务：

1. 在「设置 → 服务 → 访问令牌」中设置一个随机令牌；
2. 之后所有 `/api/v1` 请求需带 `X-Auth-Token: <令牌>` 或 `Authorization: Bearer <令牌>`；
3. 前端界面本身不携带令牌，因此对外时应配合反向代理做基础认证，或仅开放 API。

令牌只保存在本机 `data/config.json`，日志与接口回显均做掩码处理。

---

## 2. 备份与迁移

### 2.1 备份

数据目录是自包含的。停服后整目录拷贝即为完整备份：

```bash
# 停机备份
python -c "print('stop service first')"
tar czf paperopenclaw-$(date +%F).tar.gz -C /var/lib paperopenclaw
```

热备份（不停服）时，至少保证 SQLite 一致性：

```bash
sqlite3 data/db/paperopenclaw.sqlite3 ".backup 'backup.sqlite3'"
```

向量库可从关系库重建，因此**关系库 + papers/ 目录是不可替代的**，`db/vectors/` 属于可重建产物。

### 2.2 迁移到另一台机器

1. 停止服务；
2. 整目录拷贝到新机器；
3. 在新机器上设置 `PAPEROPENCLAW_DATA_DIR` 指向该目录；
4. 启动后访问「设置 → 运行诊断」确认各项通过。

路径在数据库中以数据目录的相对路径存储，因此跨操作系统迁移、跨盘符迁移都不需要改数据。

### 2.3 备份策略建议

| 对象 | 频率 | 说明 |
| --- | --- | --- |
| `data/papers/` | 每次批量导入后 | 原始 PDF 与图表，不可重建 |
| `data/db/paperopenclaw.sqlite3` | 每日 | 关系索引，不可重建 |
| `data/db/vectors/` | 可忽略 | 可经「重建向量库」恢复 |
| `data/config.json` | 变更时 | 含 API Key，注意权限 |

---

## 3. 常见故障排查

### 3.1 服务启动失败

先跑诊断：

```bash
python -m paperopenclaw doctor
```

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `数据库结构就绪` 后无输出 | 端口被占用 | 换端口 `--port 8791` |
| `数据目录可写` 失败 | 目录权限不足 | 修正权限或改用其他数据目录 |
| `无法初始化向量库` | ChromaDB 目录损坏 | 删除 `data/db/vectors/` 后重启并重建向量库 |
| `ModuleNotFoundError` | 依赖未安装 | `pip install -r backend/requirements.txt` |

### 3.2 导入失败

打开「导入」页面查看任务失败原因，或在论文详情页看 `error` 字段。常见分类：

| 错误码 / 信息 | 含义 | 处理 |
| --- | --- | --- |
| `PROVIDER_NOT_CONFIGURED` | 未配置解析模型 | 在设置页指定解析模型 |
| `PARSE_FAILED` | 模型返回无法通过校验 | 检查模型是否支持 PDF/图像；降低每批页数；换更强的模型 |
| `PROVIDER_ERROR（HTTP 4xx）` | 提供方拒绝请求 | 常见于模型名错误、上下文超限、图片过大；减小批页数或 DPI |
| `DUPLICATE_PAPER` | 哈希已存在且状态为就绪 | 正常去重；如需重跑请用「重解析」 |
| 图表数量为 0 | 版面分析未识别 | 调小「图表最小面积占比」，或改用「逐页渲染为图像」送模 |

导入失败不会删除已产生的文件，修正配置后点「重解析」即可，无需重新上传。

### 3.3 检索结果不理想

按顺序检查：

1. **嵌入方案**：设置页确认当前生效的提供方与维度。若刚改过嵌入配置，必须点「重建向量库」，
   否则新旧向量不在同一空间，检索结果会随机。
2. **相似度阈值**：`min_score` 过高会导致「库中无相关内容」；先用较低阈值确认能召回，
   再逐步提高。
3. **论文范围**：问答页的检索范围默认跟随勾选的论文，检查是否误选了不相关的子集。
4. **B 层为空**：若某篇论文的 B 层单元为 0，说明摘要模型未配置或该论文没有可归类的章节；
   检查论文详情页的金字塔结构与模型配置。

### 3.4 SSE 没有实时输出

- 反向代理未关闭缓冲（见 1.3）；
- 浏览器或代理设置了过短的读超时；
- 任务在订阅之前就已结束——此时服务端会补发一个合成的 `done` 事件，界面应直接显示结果。

### 3.5 技能不生效

1. 确认 `data/skills/<id>/SKILL.md` 存在且 frontmatter 含非空 `name`；
2. 在技能页点「刷新」触发重新扫描；
3. 确认技能处于「已启用」；
4. 脚本类工具要求脚本能读写传入的参数文件与输出文件路径，且输出为合法 JSON。

---

## 4. 性能调优

| 参数 | 位置 | 影响 |
| --- | --- | --- |
| `ingest.vision_batch_pages` | 设置 → 入库 | 每批页数越小越不容易超上下文，但请求数增多 |
| `ingest.image_dpi` | 设置 → 入库 | 只影响裁剪出的图表清晰度，不影响送模图像 |
| `ingest.render_dpi` | 设置 → 入库 | 送模图像的清晰度；过高会显著增加 token |
| `ingest.pdf_mode` | 设置 → 入库 | `file` 直传 PDF 最省事；模型不支持时用 `image` |
| `retrieval.top_k_c/b/a` | 设置 → 检索 | 决定证据量与 token 成本；B 层召回不足时可提高 |
| `retrieval.stop_at_b` | 设置 → 检索 | 开启后在 B 层证据充分时跳过 A 层，显著省 token |
| `agent.max_steps` | 设置 → 智能体 | 工具调用循环上限，过高会放大失败成本 |
| `ingest.max_concurrent_jobs` | 配置文件 | 并发导入数；受提供方速率限制约束，默认 1 |

大批量导入建议：

1. 先用 `python -m paperopenclaw ingest <目录>` 在后台跑完入库；
2. 入库全部结束后再执行一次「重建向量库」，让跨论文 C-C 关联在完整语料上重算
   （单篇入库时只能看到当时已存在的论文）。

---

## 5. 数据维护

### 5.1 重建向量库

在论文库或设置页点击「重建向量库」。适用于：

- 更换了嵌入模型或维度；
- 向量库损坏（`data/db/vectors/` 被误删）；
- 需要重算跨论文 C 层关联。

重建只依赖关系库中的语义单元，不调用解析模型，因此不会消耗解析额度。

### 5.2 删除论文

删除会一次性清理：关系库全部关联行（作者、单位、资产、OKF 概念索引、语义单元、图谱节点）、
向量库中的向量、以及 `papers/<hash>/` 整个目录。操作不可撤销，界面会二次确认。

### 5.3 OKF 校验

```bash
python -m paperopenclaw okf-verify data/papers/<hash>/okf
```

或使用论文详情页「OKF → 校验」。校验项：

- 每个非保留 `.md` 文件都有合法 YAML frontmatter 且 `type` 非空；
- `index.md` / `log.md` 结构符合规范（日志含 `YYYY-MM-DD` 日期标题）；
- `generated` 含 `by` 字段；
- 内部链接可解析（不符合规范仅告警，规范明确容忍断链）。

### 5.4 清理任务记录

`jobs` 表会随使用增长。服务启动时会把中断的任务标记为失败；长期运行可通过
`JobRepository.prune_finished()` 保留最近 N 条（默认保留 200 条已完成任务）。

---

## 6. 安全注意事项

1. **API Key 只存本地**：写入 `data/config.json`，Unix 下权限 `600`。接口回显掩码，
   日志过滤器会脱敏 `sk-` 前缀与 `Authorization` 头。
2. **技能可执行脚本**：技能是用户主动安装的，脚本工具会在服务进程的权限下运行。
   只安装可信来源的技能；不确定时先「停用」，查看详情页的指令与工具声明再启用。
3. **路径穿越防护**：`/files/*` 与资产接口的路径参数都会经数据目录根校验，
   越界返回 `PATH_OUT_OF_SCOPE`。
4. **不外传的数据**：使用本地嵌入方案时，只有送模的 PDF/图像会离开本机；
   检索与图谱计算完全在本地完成。
5. **反向代理层认证**：对外暴露但未设置访问令牌时，任何能访问该端口的人都能读取全部论文，
   请配合网络层访问控制。

---

## 7. 升级

1. 停止服务；
2. 备份 `data/`（至少 `papers/` 与 `db/paperopenclaw.sqlite3`）；
3. 更新代码与依赖：`pip install -r backend/requirements.txt`；
4. 重新构建前端：`cd web && npm install && npm run build`；
5. 启动服务。数据库结构在建表时使用 `CREATE TABLE IF NOT EXISTS`，
   已有数据不会被清空；若新增列，服务启动日志会提示需要重建相关数据。

版本兼容性：数据目录布局与 OKF `okf_version` 是稳定的对外契约，
升级不会改变论文哈希，因此外部脚本与备份可以长期复用。
