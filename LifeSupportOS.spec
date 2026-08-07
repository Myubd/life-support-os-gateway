# -*- mode: python ; coding: utf-8 -*-
#
# LifeSupportOS.exe: gateway + launch_gateway.py本体のビルド設定。
#
# archlife-fastapi/launch_fastapi.spec と同じ理由で、local_ai_coreが依存する
# httpx/cryptographyを明示的にcollect_allする(collect_all('local_ai_core')
# だけでは検出されないため)。
#
# gatewayリポジトリ自体は(archlife/interview_appと違い)local_ai_coreの
# 生コピーを持たず、`pip install /local-ai-core` でグローバルインストール
# する運用のため、collect_all('local_ai_core')でインストール済みパッケージから
# 収集する(archlifeのlaunch_fastapi.specと同じ方式)。
#
# フロントエンド(archlife/interview_app)のビルド済みdistは、
# `../archlife/archlife-frontend/dist` と
# `../interview_app/react-fastapi/frontend/dist-gateway` から
# frontend_dist_archlife / frontend_dist_interview という名前で同梱する。
# launch_gateway.py の frozen分岐(main関数内)がこの名前を前提にしている。
# ビルド前に両方とも `npm run build:electron` / `npm run build:gateway` を
# 済ませておくこと(無い場合はPyInstallerがエラーで止まる)。
import os
from PyInstaller.utils.hooks import copy_metadata, collect_all

datas_meta = []
for pkg in [
    'fastapi', 'uvicorn', 'starlette', 'pydantic', 'anyio', 'httpx',
    'cryptography', 'apscheduler',
]:
    try:
        datas_meta += copy_metadata(pkg)
    except Exception:
        pass

local_ai_core_datas, local_ai_core_binaries, local_ai_core_hiddenimports = collect_all('local_ai_core')
httpx_datas, httpx_binaries, httpx_hiddenimports = collect_all('httpx')
cryptography_datas, cryptography_binaries, cryptography_hiddenimports = collect_all('cryptography')
apscheduler_datas, apscheduler_binaries, apscheduler_hiddenimports = collect_all('apscheduler')

_FRONTEND_ARCHLIFE = os.path.join(SPECPATH, '..', 'archlife', 'archlife-frontend', 'dist')
_FRONTEND_INTERVIEW = os.path.join(SPECPATH, '..', 'interview_app', 'react-fastapi', 'frontend', 'dist-gateway')

_frontend_datas = []
if os.path.isdir(_FRONTEND_ARCHLIFE):
    _frontend_datas.append((_FRONTEND_ARCHLIFE, 'frontend_dist_archlife'))
if os.path.isdir(_FRONTEND_INTERVIEW):
    _frontend_datas.append((_FRONTEND_INTERVIEW, 'frontend_dist_interview'))

a = Analysis(
    ['launch_gateway.py'],
    pathex=[],
    binaries=local_ai_core_binaries + httpx_binaries + cryptography_binaries + apscheduler_binaries,
    datas=datas_meta + local_ai_core_datas + httpx_datas + cryptography_datas + apscheduler_datas + [
        ('main.py', '.'),
        ('auth.py', '.'),
        ('backup.py', '.'),
        ('automation_scheduler.py', '.'),
        ('restore_cli.py', '.'),
        # plugin_manifest.json: main.py の bootstrap_app() がファイルパスで
        # 直接読むため、PyInstallerの自動import解析だけでは拾われない。
        # archlife-fastapi/launch_fastapi.spec で実際に踏んだ問題と全く同じ。
        ('plugin_manifest.json', '.'),
        ('static', 'static'),
    ] + _frontend_datas,
    hiddenimports=[
        'uvicorn', 'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan', 'uvicorn.lifespan.on',
        'fastapi', 'fastapi.staticfiles', 'fastapi.responses',
        'starlette.staticfiles', 'starlette.responses',
        'anyio', 'anyio._backends._asyncio',
        'main', 'auth', 'backup', 'automation_scheduler',
        'apscheduler.schedulers.asyncio', 'apscheduler.triggers.interval',
        'apscheduler.triggers.cron', 'apscheduler.executors.asyncio',
        'apscheduler.jobstores.memory',
    ] + local_ai_core_hiddenimports + httpx_hiddenimports + cryptography_hiddenimports + apscheduler_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LifeSupportOS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    # NOTE: archlife/interview_appの各specと同じ理由でconsole=Falseにしない。
    # launch_gateway.pyのhide_console_window()が起動直後に隠す。
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LifeSupportOS',
)
