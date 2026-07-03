FROM python:3.11-slim

# 时区 + 必要工具
ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# 国内部署 pip 用清华镜像（部署时改：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

# 先 copy requirements 利用 docker cache
COPY requirements.txt ./
RUN pip install --no-cache-dir --default-timeout=120 --retries=5 -r requirements.txt

# 再 copy 源码
COPY . .

# a2a-gateway 容器内 8000 端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import httpx; r = httpx.get('http://127.0.0.1:8000/health', timeout=3); r.raise_for_status()" || exit 1

# 启动
CMD ["python3", "-u", "main.py"]
