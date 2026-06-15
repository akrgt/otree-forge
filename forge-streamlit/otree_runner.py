# -*- coding: utf-8 -*-
"""oTree実行環境（専用venv）の管理モジュール．

StreamlitとoTreeは starlette / websockets の要求バージョンが衝突するため，
同一環境には同居させない．`otree test` は，このモジュールが管理する専用venv内の
otreeコマンドで実行する．この分離により，Streamlit本体は最新版を使える．

優先順位：
  1. 専用venv（~/.otree_forge_env，環境変数 OTREE_FORGE_ENV で変更可）
  2. PATH上の otree コマンド（ローカル開発でのフォールバック）
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

# requests はoTreeのbotランナーが実行時に必要とする
OTREE_PACKAGES = ["otree>=6,<7", "requests"]
ENV_DIR = Path(os.environ.get("OTREE_FORGE_ENV", str(Path.home() / ".otree_forge_env")))

# 直近のデモサーバの pid と一時ディレクトリを記録する状態ファイル．
# Streamlitセッションが切れて孤児プロセスが残っても，次回起動時に掃除できる
DEMO_STATE = Path(tempfile.gettempdir()) / "otree_forge_demo.json"


def _bin_dir():
    return ENV_DIR / ("Scripts" if os.name == "nt" else "bin")


def otree_exe():
    """利用可能な otree 実行ファイルのパスを返す（なければ None）．"""
    exe = _bin_dir() / ("otree.exe" if os.name == "nt" else "otree")
    if exe.exists():
        return str(exe)
    return shutil.which("otree")


def env_ready():
    return otree_exe() is not None


def setup_env(timeout=900):
    """専用venvを作成して oTree をインストールする（初回のみ・数分かかる）．

    Streamlit Community Cloud でも，ホームディレクトリは書き込み可能なため動作する．
    失敗した場合は RuntimeError を送出する．
    """
    if ENV_DIR.exists():
        shutil.rmtree(ENV_DIR)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "venv", str(ENV_DIR)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("venv作成がタイムアウトした（120秒）")
    if r.returncode != 0:
        raise RuntimeError(f"venv作成に失敗した：{r.stderr.strip()}")
    pip = _bin_dir() / ("pip.exe" if os.name == "nt" else "pip")
    try:
        r = subprocess.run(
            [str(pip), "install", "--no-input", "--quiet"] + OTREE_PACKAGES,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"oTreeのインストールがタイムアウトした（{timeout}秒）")
    if r.returncode != 0:
        raise RuntimeError(f"oTreeのインストールに失敗した：{(r.stdout + r.stderr)[-2000:]}")
    exe = otree_exe()
    if exe is None:
        raise RuntimeError("インストール後も otree コマンドが見つからない")
    return exe


def run_test(project_dir, config_name, timeout=300, export_dir=None):
    """生成済みプロジェクトに対して `otree test` を実行し (合否, ログ行) を返す．

    export_dir を指定すると，botがプレイした結果データをCSVで出力する
    （{app_name}.csv に1行＝1プレイヤー×1ラウンドの形式）．
    """
    exe = otree_exe()
    if exe is None:
        return False, ["oTree実行環境が未準備である"]
    cmd = [exe, "test", config_name]
    if export_dir is not None:
        cmd.append(f"--export={export_dir}")
    try:
        r = subprocess.run(
            cmd, cwd=project_dir, capture_output=True, text=True, timeout=timeout,
        )
        ok = r.returncode == 0 and "Bots completed session" in (r.stdout + r.stderr)
        log = (r.stdout + "\n" + r.stderr).strip().splitlines()
        return ok, log
    except subprocess.TimeoutExpired:
        return False, [f"タイムアウト（{timeout}秒）"]


def _kill_pg(pid):
    """プロセスグループごとSIGTERMを送る（既に終了していれば何もしない）"""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def cleanup_demo():
    """過去のデモサーバの孤児プロセスと一時ディレクトリを片付ける"""
    if not DEMO_STATE.exists():
        return
    try:
        state = json.loads(DEMO_STATE.read_text(encoding="utf-8"))
        _kill_pg(int(state.get("pid", 0)))
        d = state.get("dir", "")
        # 誤削除を防ぐため，このアプリが作ったディレクトリ名のみ削除する
        if d and Path(d).name.startswith("forge_demo_"):
            shutil.rmtree(d, ignore_errors=True)
    except (ValueError, OSError):
        pass
    DEMO_STATE.unlink(missing_ok=True)


def start_devserver(project_dir, port=8503):
    """生成済みプロジェクトで `otree devserver` を起動し，Popenを返す．

    参加者画面をブラウザで開いて実際にプレイできる（デモプレイ用）．
    出力は project_dir/devserver.log に書き出す．
    起動前に，前回の孤児プロセス・一時ディレクトリを掃除する（ポート衝突も防ぐ）．
    """
    exe = otree_exe()
    if exe is None:
        raise RuntimeError("oTree実行環境が未準備である")
    cleanup_demo()
    log = open(Path(project_dir) / "devserver.log", "w")
    # 子プロセス（autoreloader）ごと止められるよう，新しいプロセスグループで起動する
    proc = subprocess.Popen(
        [exe, "devserver", str(port)],
        cwd=project_dir, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    DEMO_STATE.write_text(
        json.dumps({"pid": proc.pid, "dir": str(project_dir)}), encoding="utf-8")
    return proc


def stop_devserver(proc):
    """devserverをプロセスグループごと終了し，一時ディレクトリも削除する"""
    _kill_pg(proc.pid)
    cleanup_demo()
