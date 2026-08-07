"""
paths.py
--------
全アプリが「同じ core.db / 同じ device_identity」を指すようにするための、
唯一のパス解決ロジック。

背景(なぜこれが要るか):
- 各アプリ(Archlife, interview_app, 今後追加するアプリ)が、それぞれ自分の
  インストールフォルダ基準で core.db や device_identity.json のパスを
  独自に組み立てていると、"local_ai_core を使っている" つもりでも、実際には
  端末上に複数の core.db / 複数の device_id が並存してしまう。
  そうなると schedule_items や memory_items がアプリ間で一切共有されず、
  「共通データ基盤」が名前だけのものになる。
- これを避けるため、パス解決は必ずこのモジュールの関数を経由する。
  各アプリ側で独自のフォールバック(自分のインストールフォルダ配下など)を
  実装しないこと。

優先順位:
1. 環境変数(LOCAL_AI_CORE_DB_PATH / LOCAL_AI_CORE_DEVICE_IDENTITY_PATH)が
   設定されていればそれを使う(最優先。テスト時に ":memory:" 相当の分離や
   一時ディレクトリを指定する用途にも使う)。
2. 未設定なら、OSごとの「共有アプリデータ」ディレクトリ配下の固定ファイル名
   (core.db / device_identity.json)を使う。同じ端末上のどのアプリから
   呼んでも同一のファイルを指すようにするのが目的。

複数プロフィール(家族利用等)は「1つの core.db の中に複数の profiles 行を
持つ」ことで表現する設計であり、"複数の core.db を持つ" ことでは表現しない。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ECOSYSTEM_DIR_NAME_WINDOWS = "ArchLifeEcosystem"
_ECOSYSTEM_DIR_NAME_MAC = "ArchLifeEcosystem"
_ECOSYSTEM_DIR_NAME_LINUX = "archlife-ecosystem"

_CORE_DB_ENV = "LOCAL_AI_CORE_DB_PATH"
_DEVICE_IDENTITY_ENV = "LOCAL_AI_CORE_DEVICE_IDENTITY_PATH"

# 後方互換: 移行前の interview_app / Archlife がそれぞれ個別に見ていた環境変数名。
# 新しい LOCAL_AI_CORE_DB_PATH が優先されるが、既存のデプロイ設定
# (.env ファイルなど)をすぐ書き換えられない場合のために当面は読む。
_LEGACY_CORE_DB_ENVS = ("CORE_DB_PATH",)


def _shared_app_data_dir() -> Path:
    """OSごとの「全アプリ共有」データディレクトリを返す(未作成なら作成しない。
    呼び出し側でファイルパスを組み立てた後に mkdir すること)。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / _ECOSYSTEM_DIR_NAME_WINDOWS
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _ECOSYSTEM_DIR_NAME_MAC
    # Linux / その他Unix系: XDG Base Directory Specification に従う
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / _ECOSYSTEM_DIR_NAME_LINUX


def _resolve(env_name: str, filename: str, legacy_env_names: tuple[str, ...] = ()) -> str:
    env_path = os.environ.get(env_name, "")
    if env_path:
        return env_path
    for legacy_name in legacy_env_names:
        legacy_path = os.environ.get(legacy_name, "")
        if legacy_path:
            return legacy_path

    base = _shared_app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return str(base / filename)


def get_core_db_path() -> str:
    """全アプリ共通の core.db の絶対パス。

    アプリ側で個別にフォールバックを実装せず、必ずこの関数を呼ぶこと。
    テスト時は環境変数 LOCAL_AI_CORE_DB_PATH に ":memory:" や
    一時ファイルパスを設定して隔離する。
    """
    return _resolve(_CORE_DB_ENV, "core.db", _LEGACY_CORE_DB_ENVS)


def get_device_identity_path() -> str:
    """全アプリ共通の device_identity.json の絶対パス。

    core.db と同じ理由で、これも共有ディレクトリに固定する。
    ここが各アプリでバラバラだと、core.db を共有していても
    アプリごとに別の device_id が発行され、profiles が分裂してしまう。
    """
    return _resolve(_DEVICE_IDENTITY_ENV, "device_identity.json")
