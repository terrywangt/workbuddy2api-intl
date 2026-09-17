FROM python:3.12-slim

WORKDIR /srv

# 时区数据（Asia/Shanghai 定时调度需要 tzdata）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

ENV TZ=Asia/Shanghai \
    DATA_DIR=/data \
    PORT=8789 \
    PYTHONUNBUFFERED=1

EXPOSE 8789

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8789/health', timeout=3)"

CMD ["python3", "-m", "app.proxy", "--host", "0.0.0.0"]