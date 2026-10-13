# 05 · 打包与分发

PaperOpenClaw 由两部分组成：

- **后端**：Python FastAPI（PDF 解析、RAG、向量库、图谱、LLM 调用），单进程托管 API；
- **前端**：Vue 3 界面，可被后端托管（网页模式），也可跑在 **Tauri 2 原生窗口**里（桌面模式）。

## 1. 源码分发包（sdist / wheel）

```bash
cd backend
python -m build             # 产出 dist/paperopenclaw-1.0.0.tar.gz 与 .whl
pip install .               # 或 pip install ".[local-embed]" / ".[desktop]"
```

安装后命令行入口为 `paperopenclaw`。

## 2. Docker（网页模式）

```bash
docker build -t paperopenclaw .
docker compose up -d        # http://localhost:8787
```

## 3. 桌面 GUI（Tauri 2，跨平台、不依赖 Python 的 UI 外壳）

桌面程序用 **Tauri 2**（Rust 外壳 + 系统 WebView），前端界面运行在原生窗口里；
启动时自动拉起 Python 后端（FastAPI），界面通过 `http://127.0.0.1:8787` 调用它。
UI 外壳本身不依赖 Python——它只是 Rust 进程 + 系统 WebView。

### 3.1 结构

```
desktop/
  src-tauri/            Rust 外壳（Cargo.toml / main.rs / tauri.conf.json / build.rs）
  icons/                应用图标
  build.ps1 / build.sh  一键构建脚本
```

### 3.2 构建

前置：Node.js、Rust 工具链（`cargo`）、以及已安装的 Python 后端（`pip install .`）。

```bash
# Windows
powershell -ExecutionPolicy Bypass -File desktop/build.ps1

# macOS / Linux
bash desktop/build.sh
```

脚本会：① 以桌面 API 基地址构建前端 → ② 安装 Tauri CLI → ③ `tauri build`。

产物：`desktop/src-tauri/target/release/paperopenclaw(.exe)`（体积小，约几 MB ~ 十几 MB）。

### 3.3 运行

直接运行可执行文件，或命令行：

```bash
./paperopenclaw                                    # 数据写到 ~/.paperopenclaw
PAPEROPENCLAW_PYTHON=/path/to/python ./paperopenclaw  # 指定后端 Python
PAPEROPENCLAW_DATA_DIR=/path/to/d  ./paperopenclaw    # 指定数据目录
PAPEROPENCLAW_PORT=9000             ./paperopenclaw    # 指定后端端口
```

> 桌面模式需要本机已安装 Python 后端（`pip install paperopenclaw` 或源码安装）。
> GUI 外壳（Rust/WebView）不依赖 Python；Python 只作为后端服务被拉起。

## 4. 相关文件

- `Dockerfile`、`docker-compose.yml`、`.dockerignore`
- `desktop/src-tauri/`（Tauri 工程）、`desktop/build.ps1`、`desktop/build.sh`
- `backend/pyproject.toml`（sdist/wheel 构建）
- `backend/paperopenclaw/desktop.py`（备用：pywebview 窗口入口，Tauri 未就绪时的回退）
