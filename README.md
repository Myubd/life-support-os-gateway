# life-support-os-gateway

「プライバシーファースト・ローカルAIエコシステム」を1つの入口にまとめる gateway。
ライフサポートOS(Archlife)・就活支援(interview_app)・学習支援(study-support)・
健康管理(health-support)、そして`local_ai_core`が提供する共通基盤
(権限・メモリー・ドキュメント・検索・オートメーション・アシスタント)を、
認証で保護された1つのHTTPオリジンから使えるようにする。

## これが解決すること

```
                        ┌───────────────────────────────────────┐
                        │  life-support-os-gateway               │
                        │  (このリポジトリ, 1プロセス, 認証あり)    │
                        │                                        │
  ブラウザ ────────────▶│  /            → 統合コンソール(認証UI込み)│
  (ログイン後、Cookieで   │  /core/*      → local_ai_core           │
   以降のアクセスを許可)  │  /api/life/*  → archlife-fastapi (proxy)│
                        │  /api/career/*→ interview_app backend  │
                        │  /api/study/* → study-support backend  │
                        │  /api/health/*→ health-support backend │
                        └───────────────────────────────────────┘
```

- `/` : 統合コンソール(`static/index.html`)。権限台帳・ドキュメントセンター・
  横断検索・アシスタント・オートメーションを1画面で操作できる。未ログインの
  場合はここでログイン画面を表示する。
- `/core/*` : `local_ai_core.api.build_core_router` が提供する共通API。
  permissions・memory・documents・search・schedule・knowledge・automation・
  assistant を1本のAPIとして提供する。
- `/api/life/*` `/api/career/*` `/api/study/*` `/api/health/*` : 各アプリの
  バックエンドへそのまま転送する単純なリバースプロキシ。
- `/auth/login` `/auth/logout` : 共有シークレットによるログイン/ログアウト。
- `/admin/backup` : `core.db`の手動バックアップ(認証必須)。

各アプリのPythonコードを1プロセスに無理やりマージしていない(`archlife-fastapi`と
`interview_app`backendはどちらも`db`/`core_sync`という同名モジュールを持っており、
1プロセスに同居させると名前空間が衝突するため)。その代わり、それぞれ従来通り
別プロセスで起動しておき、gatewayが単純なリバースプロキシで束ねる。この方針は
アプリが4つに増えた今も変わっていない。

## 起動方法

umbrella repo(`life-support-os`)の`docker-compose.yml`から起動するのが基本(単体起動の
手順は各アプリのREADMEを参照)。

```bash
cd life-support-os
docker compose --profile setup run --rm model_setup   # 初回のみ
docker compose up -d
```

起動後、`http://localhost:3000/health` が `{"ok": true, "profile_id": ...}` を返せば成功。
ダッシュボード自体は`http://localhost:3000/`から、ログイン画面 → `GATEWAY_AUTH_TOKEN`の
入力を経てアクセスする。

環境変数(主要なもの):

| 変数名 | 意味 | 既定値 |
|---|---|---|
| `GATEWAY_AUTH_TOKEN` | **必須**。gatewayという唯一の入口を守る共有シークレット。未設定だと起動を拒否する | なし(必ず設定すること) |
| `GATEWAY_ALLOWED_ORIGINS` | CORSで許可するオリジン(カンマ区切り) | `http://localhost:3000` 等 |
| `ARCHLIFE_BACKEND_URL` / `INTERVIEW_APP_BACKEND_URL` / `STUDY_SUPPORT_BACKEND_URL` / `HEALTH_SUPPORT_BACKEND_URL` | 各アプリの起動先 | 各アプリの既定ポート |
| `AUTOMATION_POLL_INTERVAL_SECONDS` | オートメーションルールの定期実行間隔(秒) | `3600` |
| `BACKUP_DIR` / `BACKUP_INTERVAL_HOURS` / `BACKUP_RETENTION_DAYS` | `core.db`の定期バックアップ設定 | `/backups` / `24` / `14` |
| `OLLAMA_URL` / `OLLAMA_MODEL` | ローカルLLM | local_ai_core既定値 |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | 外部APIをオプトインで使う場合のみ | なし |
| `LOCAL_AI_CORE_DB_PATH` / `LOCAL_AI_CORE_DEVICE_IDENTITY_PATH` | core.db等のパスを明示したい場合 | OS既定の共有ディレクトリ |

## 認証

`GATEWAY_AUTH_TOKEN`(共有シークレット)1本による認証。`/auth/login`にトークンを
POSTすると`HttpOnly`Cookieが発行され、以降のリクエストはこのCookieで判定される。
`/`(シェルのHTML自体)・`/health`・`/auth/*`は認証不要、それ以外(`/core/*` `/api/*`
`/admin/*`)はすべて保護される。OAuth/JWTのような大掛かりな仕組みにしていないのは、
現状このシステムが単一ユーザー・単一プロフィール前提だからで、複数人の権限を
区別する必要が出てきた時に初めて拡張すればよい、という判断による。

## フロントエンド側の変更点

各アプリのフロントエンドは、APIの接続先をこのgatewayの`/api/life/*` `/api/career/*`
`/api/study/*` `/api/health/*`に向けるだけでよい(エンドポイントのパス・
リクエスト/レスポンス形式は無変更)。共通基盤の機能(権限設定・ドキュメント
センター・横断検索・オートメーション・アシスタント)は統合コンソール(`/`)側に
すでに実装されているため、各アプリが個別に作り直す必要はない。

## 制約(現時点)

- リバースプロキシはREST(JSON)のみを想定しており、WebSocket/SSEには対応していない。
  ストリーミングAI応答など将来必要になった場合は別途対応する。
- 複数プロフィール(家族利用)は対象外という設計判断(プライバシー・セキュリティ
  最優先の単一ユーザー前提のため)。`GET /me/profile_id`は常に既定プロフィールを返す。
- `GATEWAY_COOKIE_SECURE`は既定で無効(HTTP/localhost運用が前提)。LAN経由での
  スマホアクセス等、TLS終端のある環境で使う場合は明示的に有効化すること。
- ナレッジグラフ(項目間の関連性)は未実装。全文検索(`/core/search`)までは
  対応済み。
