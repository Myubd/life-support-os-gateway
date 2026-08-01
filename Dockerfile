# 配置先: life-support-os-gateway/Dockerfile
#
# ビルド時の注意: このDockerfileはdocker-compose側で
#   build:
#     context: ..                              # umbrella repoのルート
#     dockerfile: life-support-os-gateway/Dockerfile
# として呼ばれる想定(local-ai-coreをこのイメージにも同梱するため、
# gatewayフォルダ単体ではなくリポジトリ全体をbuild contextにする)。
#
# 注意: フォルダ名は "local-ai-core"(ハイフン)。Pythonパッケージ名としての
# "local_ai_core"(アンダースコア)と紛らわしいので、COPY元のパスを
# 間違えないこと(実際にこのDockerfileの初版でこの点を取り違えていた)。
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# 1. local-ai-core を先にインストール(umbrella repoルート直下にある想定)
COPY local-ai-core /local-ai-core
RUN pip install --no-cache-dir /local-ai-core

# 2. gateway本体
COPY life-support-os-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY life-support-os-gateway/ .

RUN mkdir -p /app/data
# 共有ボリューム(/shared/core)用のディレクトリも、chownより前にここで
# 作っておく。先に作らないと、docker-composeがボリュームをマウントした時に
# root所有のままになり、非rootユーザー(appuser)で書き込めなくなって
# sqlite3.OperationalError: unable to open database file になる
# (このDockerfileの初版でこの点を見落としていた)。
RUN mkdir -p /shared/core
RUN useradd -m appuser && chown -R appuser /app /shared/core
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 3000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]
