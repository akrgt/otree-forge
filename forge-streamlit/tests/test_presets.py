# -*- coding: utf-8 -*-
"""プリセットの通し確認．

examples/*.json を1つずつ「仕様検証 → oTreeプロジェクト生成 → otree test（bot）」に通す．
forge.py や プリセットを直したあと，壊していないかをこの1本で確かめる．

    python -m pytest tests -q

otree 実行環境（otree_runner.otree_exe）が無い場合，bot の部分だけ skip する．
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import forge  # noqa: E402
import otree_runner  # noqa: E402

PRESETS = sorted((ROOT / "examples").glob("*.json"))


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_presets_exist():
    assert PRESETS, "examples/ にプリセットが無い"


@pytest.mark.parametrize("path", PRESETS, ids=lambda p: p.stem)
def test_validate_and_build(path, tmp_path):
    spec = _load(path)
    forge.validate(spec)
    out = tmp_path / "proj"
    forge.build(spec, out)
    assert (out / "settings.py").exists()
    for app in spec["apps"]:
        init = out / app["name"] / "__init__.py"
        assert init.exists()
        # 生成したPythonが構文として正しいこと
        compile(init.read_text(encoding="utf-8"), str(init), "exec")
        assert forge.emit_bot(app) is not None, f"{app['name']}: botを導出できない"


@pytest.mark.parametrize("path", PRESETS, ids=lambda p: p.stem)
def test_bots_complete(path, tmp_path):
    if not otree_runner.env_ready():
        pytest.skip("otree 実行環境が無い（アプリの「oTree実行環境をセットアップ」か pip install otree）")
    spec = _load(path)
    out = tmp_path / "proj"
    forge.build(spec, out)
    ok, log = otree_runner.run_test(out, spec["session"]["config_name"])
    assert ok, "otree test が失敗した：\n" + "\n".join(log[-40:])


def test_player_scope_check():
    """player しか使えないコード欄の group を，生成前の検証で拾う"""
    spec = _load(ROOT / "examples" / "dictator.json")
    page = next(p for p in spec["apps"][0]["pages"] if p.get("kind") != "wait")
    page["vars_for_template"] = "return dict(x=group.id_in_subsession)"
    with pytest.raises(forge.SpecError, match="player.group"):
        forge.validate(spec)
    page["vars_for_template"] = "group = player.group\nreturn dict(x=group.id_in_subsession)"
    forge.validate(spec)
