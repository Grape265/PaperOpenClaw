# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：把 PaperOpenClaw 后端 + 前端打包成单一可执行文件。

在对应操作系统上执行（PyInstaller 不支持跨平台交叉编译）：

    Windows:  python -m PyInstaller packaging/paperopenclaw.spec
    macOS:    python -m PyInstaller packaging/paperopenclaw.spec
    Linux:    python -m PyInstaller packaging/paperopenclaw.spec

产物在 dist/ 下；数据目录默认写到用户主目录（~/.paperopenclaw）。

注意：建议在「只安装本应用依赖的干净虚拟环境」里打包，避免把机器上无关的
tensorflow / torchvision / dask 等重型包一并打进去。本 spec 已用 excludes 尽量
排除这些可选项。
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

repo = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821
backend = os.path.join(repo, "backend")
entry = os.path.join(backend, "paperopenclaw", "server_entry.py")
web_dist = os.path.join(repo, "web", "dist")

datas = []
binaries = []
hiddenimports = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan", "uvicorn.lifespan.on",
    "uvicorn.middleware.proxy_headers",
    "pydantic", "pydantic.deprecated.decorator",
    # 应用直接 import 的第三方包。
    "fitz", "jieba", "networkx", "chromadb", "chromadb.config",
    "onnxruntime", "PIL", "multipart", "yaml",
    # 原生窗口。
    "webview", "webview.platforms.winforms", "webview.platforms.cocoa",
    "webview.platforms.qt", "webview.platforms.gtk",
]

# jieba 的分词词典（数据文件，不随 import 自动收集）。
try:
    datas += collect_data_files("jieba")
except Exception:
    pass

# chromadb 依赖链的数据文件（jsonschema → rfc3987_syntax → lark 的 .lark 语法文件，
# 以及 opentelemetry 等 chromadb 运行所需子模块），整体收集避免缺文件。
for pkg in ("chromadb", "jsonschema", "rfc3987", "rfc3987_syntax", "lark",
            "opentelemetry"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# SQLite schema（package-data，PyInstaller 不自动收集，需显式加入）。
schema_sql = os.path.join(backend, "paperopenclaw", "storage", "schema.sql")
if os.path.isfile(schema_sql):
    datas.append((schema_sql, "paperopenclaw/storage"))

# 前端静态资源。
if os.path.isdir(web_dist):
    datas.append((web_dist, "web/dist"))

a = Analysis(
    [entry],
    pathex=[backend],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 体积控制的关键：不打包本地嵌入的 torch/transformers（约 200MB+）。
        # 桌面版默认降级为「离线词法 / 远程嵌入」，功能不受影响。
        "torch", "transformers", "sentence_transformers", "tokenizers",
        # 与本文档无关的重型/可选依赖，避免脏环境把它们一起打进来。
        "tkinter", "matplotlib", "pandas", "scipy", "IPython", "notebook",
        "tensorflow", "keras", "tf_keras", "tensorboard", "tensorflow_io",
        "dask", "distributed", "numba", "llvmlite",
        "torchvision", "torchaudio", "torchtext",
        "gradio", "streamlit", "pygame", "opencv", "cv2",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "sympy", "plotly", "bokeh", "seaborn",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# onedir：产出「文件夹」而非单文件 exe——Python 运行时 + 全部依赖解压在文件夹里，
# 用户拿到后无需安装 Python / pip，直接运行即可。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="paperopenclaw-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="paperopenclaw-backend",
)
