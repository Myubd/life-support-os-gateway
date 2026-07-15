# 配置先: life-support-os-gateway/Dockerfile
#
# ビルド時の注意: このDockerfileはdocker-compose側で
#   build:
#     context: ..                              # umbrella repoのルート
#     dockerfile: life-support-os-gateway/Dockerfile
# として呼ばれる想定(local_ai_coreをこのイメージにも同梱するため、
# gatewayフォルダ単体ではなくリポジトリ全体をbuild contextにする)。
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# 1. local_ai_core を先にインストール(umbrella repoルート直下にある想定)
COPY local_ai_core /local_ai_core
RUN pip install --no-cache-dir /local_ai_core

# 2. gateway本体
COPY life-support-os-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY life-support-os-gateway/ .

RUN mkdir -p /app/data
RUN useradd -m appuser && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 3000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]
