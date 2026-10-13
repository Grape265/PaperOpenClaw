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

from PyInstaller.utils.hooks import collect_data_files

repo = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821
backend = os.path.join(repo, "backend")
entry = os.path.join(backend, "paperopenclaw", "desktop.py")
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PaperOpenClaw",
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
