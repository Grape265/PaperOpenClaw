# PaperOpenClaw 容器镜像：后端 + 已构建前端，单进程托管。
# 构建：docker build -t paperopenclaw .
# 运行：docker run -p 8787:8787 -v paperopenclaw-data:/data -v paperopenclaw-models:/models paperopenclaw

# ---- 1. 前端构建 -----------------------------------------------------------
FROM node:20-alpine AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && npm install
COPY web/ ./
RUN npm run build

# ---- 2. 后端运行时 ---------------------------------------------------------
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PAPEROPENCLAW_DATA_DIR=/data \
    PAPEROPENCLAW_WEB_DIST=/app/web/dist \
    HF_HOME=/models \
    HF_ENDPOINT=https://hf-mirror.com

WORKDIR /app

# 运行时依赖：先装纯 Python 包，再单独装 CPU 版 torch（体积大，分层缓存）。
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r /app/backend/requirements.txt \
 && pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
        "transformers>=4.35" \
 && pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY backend/ /app/backend/
COPY --from=web /src/web/dist /app/web/dist

# 数据与模型挂载点（也允许使用卷持久化）。
RUN mkdir -p /data /models

EXPOSE 8787

ENV PYTHONPATH=/app/backend
CMD ["python", "-m", "paperopenclaw", "serve", "--host", "0.0.0.0", "--port", "8787", "--no-browser"]
