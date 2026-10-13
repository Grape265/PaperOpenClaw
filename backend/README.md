# PaperOpenClaw

基于 OpenClaw 式智能体架构的论文知识库：OKF 忠实存储、A/B/C 金字塔 RAG、七维鱼骨知识图谱、
多智能体头脑风暴与技能系统。

## 安装（源码）

```bash
cd backend
pip install -e .              # 开发安装
pip install .                 # 常规安装
pip install ".[local-embed]"  # 附带本地嵌入模型依赖（torch/transformers）
```

## 启动

```bash
python -m paperopenclaw serve                 # 默认 http://127.0.0.1:8787
python -m paperopenclaw ingest paper.pdf      # 命令行导入
python -m paperopenclaw doctor                # 运行诊断
python -m paperopenclaw rebuild-kg            # 重建双图库
```

## 前端

前端在 `web/`（Vue 3 + Vite）。构建后由后端自动托管：

```bash
cd web && npm install && npm run build
```

## 打包

见 `docs/05-packaging.md`：Docker 镜像与 Windows/macOS/Linux 桌面可执行文件。
