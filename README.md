# life-support-os-gateway

「プライバシーファースト・ローカルAIエコシステム」を1つの入口にまとめる gateway。
就活支援(interview_app)とライフサポートOS(Archlife)、そして
`local_ai_core` の新モジュール(documents / automation / assistant)を、
1つのHTTPオリジンから使えるようにする。

## これが解決すること

これまで:

```
フロントエンドA(Archlife) → archlife-fastapi:8080
フロントエンドB(interview_app) → interview_app backend:8000
共通機能(メモリー/権限/ドキュメント/オートメーション/アシスタント) → ???(呼び出し口がなかった)
```

これから:

```
                        ┌─────────────────────────────┐
                        │  life-support-os-gateway     │
                        │  (このリポジトリ, 1プロセス)   │
                        │                              │
  フロントエンド ───────▶│  /core/*      → local_ai_core │
  (どちらのUIからでも)    │  /api/life/*  → archlife-fastapi (proxy)
                        │  /api/career/*→ interview_app backend (proxy)
                        └─────────────────────────────┘
```

- `/core/*` : `local_ai_core.api.build_core_router` が提供する共通API。
  permissions(許可の一覧・付与・失効)、memory、documents(ドキュメントセンター)、
  schedule、knowledge、automation、assistant を1本のAPIとして提供する。
- `/api/life/*` : 既存の `archlife-fastapi`(ポート8080想定)へそのまま転送する。
- `/api/career/*` : 既存の `interview_app` の `react-fastapi/backend`(ポート8000想定)へ
  そのまま転送する。

3つのアプリのPythonコードを1プロセスに無理やりマージしていない(`archlife-fastapi` と
`interview_app` backend はどちらも `db` / `core_sync` という同名モジュールを持っており、
1プロセスに同居させると名前空間が衝突するため)。その代わり、それぞれ従来通り
別プロセスで起動しておき、gatewayが単純なリバースプロキシで束ねる。

## 起動方法

前提として、以下の3つが起動していること:

```bash
# 1) archlife-fastapi (Archlife/archlife-fastapi/ で)
uvicorn main:app --port 8080

# 2) interview_app backend (interview_app/react-fastapi/backend/ で)
uvicorn app:app --port 8000   # 実際のエントリポイント名はinterview_app側のREADMEを確認

# 3) このgateway
cd life-support-os-gateway
pip install -r requirements.txt
uvicorn main:app --port 3000
```

環境変数(すべて任意):

| 変数名 | 意味 | 既定値 |
|---|---|---|
| `ARCHLIFE_BACKEND_URL` | archlife-fastapiの起動先 | `http://localhost:8080` |
| `INTERVIEW_APP_BACKEND_URL` | interview_app backendの起動先 | `http://localhost:8000` |
| `OLLAMA_URL` / `OLLAMA_MODEL` | ローカルLLM | local_ai_core既定値 |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | 外部APIをオプトインで使う場合のみ | なし |
| `LOCAL_AI_CORE_DB_PATH` / `LOCAL_AI_CORE_DEVICE_IDENTITY_PATH` | core.db等のパスを明示したい場合 | OS既定の共有ディレクトリ |

起動後、`http://localhost:3000/health` が `{"ok": true, "profile_id": ...}` を返せば成功。

## フロントエンド側の変更点

Archlife/interview_appのフロントエンドは、APIの接続先を今までの個別ポートから
このgatewayの `/api/life/*` `/api/career/*` に向けるだけでよい(エンドポイントの
パス・リクエスト/レスポンス形式は無変更)。加えて、新モジュールを使う画面
(許可設定・ドキュメントセンター・オートメーション・アシスタント)は `/core/*` を呼ぶ。

## 制約(現時点)

- リバースプロキシはREST(JSON)のみを想定しており、WebSocket/SSEには対応していない。
  ストリーミングAI応答など将来必要になった場合は別途対応する。
- 複数プロフィール(家族利用)はまだUI側の選択導線がない。`GET /me/profile_id` が
  常に既定プロフィールを返す。
- automationの定期実行(スケジューラ)は未実装。現状は `/core/automation/rules/{id}/run`
  を手動、またはフロントエンド側のタイマーから叩く運用を想定している。
