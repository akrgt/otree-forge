# -*- coding: utf-8 -*-
"""oTree Forge Studio — Streamlit版
forge.py（生成エンジン）を直接importして使う：エンジンはPython側の単一実装である．
React版との最大の違いは，サーバ側で `otree test`（bot検証）まで実行できること．
"""
import base64
import copy
import io
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

import forge  # 単一情報源の生成エンジン
import otree_runner  # oTree専用venvの管理（Streamlitとの依存衝突を回避）

st.set_page_config(page_title="oTree Forge Studio", layout="wide", page_icon="🧪")

ROOT = Path(__file__).parent
EXAMPLES = ROOT / "examples"
DEMO_PORT = 8503  # デモプレイ用devserverのポート

# ---------------------------------------------------------------- presets

BLANK = {
    "esl_version": "0.1",
    "meta": {"name": "my_experiment", "display_name": "新しい実験", "doc": ""},
    "session": {
        "config_name": "my_experiment", "display_name": "新しい実験",
        "app_sequence": ["my_experiment"], "num_demo_participants": 2,
        "language_code": "ja", "currency_code": "JPY", "use_points": True,
    },
    "apps": [{
        "name": "my_experiment", "doc": "",
        "constants": {"players_per_group": 2, "num_rounds": 1, "extra": []},
        "models": {"subsession": [], "group": [], "player": []},
        "logic": [],
        "pages": [{"name": "Introduction", "kind": "info", "title": "実験の説明",
                   "body": "<p>ここに実験の説明を書く．</p>"}],
    }],
}

PRESET_LABELS = {"blank": "白紙から作る", "dictator": "独裁者ゲーム",
                 "trust": "信頼ゲーム", "public_goods": "公共財ゲーム（3人・3R）",
                 "survey": "アンケート（入力形式の見本）"}


def load_presets():
    presets = {"blank": BLANK}
    for f in sorted(EXAMPLES.glob("*.json")):
        presets[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return presets


SNIPPETS = [
    ("利得：独裁者ゲーム", "group",
     "p1, p2 = group.get_players()\np1.payoff = group.kept\np2.payoff = C.ENDOWMENT - group.kept"),
    ("利得：最後通牒ゲーム", "group",
     "p1, p2 = group.get_players()\nif group.accepted:\n    p1.payoff = C.ENDOWMENT - group.offer\n    p2.payoff = group.offer\nelse:\n    p1.payoff = cu(0)\n    p2.payoff = cu(0)"),
    ("利得：公共財ゲーム", "group",
     "players = group.get_players()\ntotal = sum(p.contribution for p in players)\nshare = total * C.MULTIPLIER / C.PLAYERS_PER_GROUP\nfor p in players:\n    p.payoff = C.ENDOWMENT - p.contribution + share"),
    ("利得：信頼ゲーム", "group",
     "p1, p2 = group.get_players()\np1.payoff = C.ENDOWMENT - group.sent + group.sent_back\np2.payoff = group.sent * C.MULTIPLIER - group.sent_back"),
    ("反復：全員に同じ処理", "group", "for p in group.get_players():\n    p.payoff = cu(0)"),
    ("反復：役割で分岐", "group",
     "for p in group.get_players():\n    if p.id_in_group == 1:\n        p.payoff = group.kept\n    else:\n        p.payoff = C.ENDOWMENT - group.kept"),
    ("反復：自分以外の合計", "player",
     "others = player.get_others_in_group()\nothers_total = sum(o.contribution for o in others)"),
    ("ラウンド：累積利得", "player", "total_payoff = sum(p.payoff for p in player.in_all_rounds())"),
    ("ラウンド：前ラウンド参照", "player",
     "if player.round_number > 1:\n    prev = player.in_round(player.round_number - 1)"),
    ("ラウンド：ランダム1回払い", "player",
     "import random\nif player.round_number == C.NUM_ROUNDS:\n    selected = random.randint(1, C.NUM_ROUNDS)\n    player.participant.vars['paid_round'] = selected\n    for p in player.in_all_rounds():\n        if p.round_number != selected:\n            p.payoff = cu(0)"),
    ("割当：処置を交互に", "subsession",
     "import itertools\ntreatments = itertools.cycle(['control', 'treatment'])\nfor p in subsession.get_players():\n    p.treatment = next(treatments)"),
    ("割当：毎ラウンド再マッチ", "subsession", "subsession.group_randomly()"),
    ("割当：第1ラウンドと同じ組", "subsession",
     "if subsession.round_number > 1:\n    subsession.group_like_round(1)"),
]

# 入力形式（widget）の表示名とESL値の対応
WIDGET_LABELS = {"": "標準（数値欄/テキスト欄/ドロップダウン）",
                 "RadioSelect": "ラジオボタン（縦）",
                 "RadioSelectHorizontal": "ラジオボタン（横）＝リッカート尺度向き",
                 "CheckboxInput": "チェックボックス（bool型のみ）",
                 "Slider": "スライダー（min/max必須）",
                 "SliderNoAnchor": "スライダー・つまみ非表示（アンカリング回避，min/max必須）",
                 "ButtonSelect": "ボタン選択（押すと即送信して次へ，choices必須）",
                 "StarRating": "星評価（int型，max=星の数）",
                 "LikertMatrix": "リッカート表（同じchoicesの連続欄を1つの表に，choices必須）",
                 "NumberStepper": "数値ステッパー（−/＋ボタン）",
                 "MultiCheckbox": "複数選択（str型にカンマ区切り保存，choices必須）"}
WIDGET_FROM_LABEL = {v: k for k, v in WIDGET_LABELS.items()}

# デザインテーマで選べるフォント（標準以外はGoogle Fonts名）
THEME_FONTS = ["標準", "Noto Sans JP", "Noto Serif JP", "M PLUS Rounded 1c",
               "Zen Maru Gothic", "Kosugi Maru"]

COND_CHOICES = ["全員に表示", "プレイヤー1のみ", "プレイヤー2のみ",
                "第1ラウンドのみ", "最終ラウンドのみ", "カスタム式"]
COND_MAP = {
    "プレイヤー1のみ": "player.id_in_group == 1",
    "プレイヤー2のみ": "player.id_in_group == 2",
    "第1ラウンドのみ": "player.round_number == 1",
    "最終ラウンドのみ": "player.round_number == C.NUM_ROUNDS",
}
COND_REV = {v: k for k, v in COND_MAP.items()}

# ------------------------------------------------- ブロックパレット・フロー図

LIKERT5 = [[1, "全くそう思わない"], [2, "そう思わない"], [3, "どちらでもない"],
           [4, "そう思う"], [5, "強くそう思う"]]


def _uniq(base, existing):
    """既存名と衝突しない名前を返す（base, base2, base3, …）"""
    if base not in existing:
        return base
    i = 2
    while f"{base}{i}" in existing:
        i += 1
    return f"{base}{i}"


def _pnames(app):
    return {p["name"] for p in app["pages"]}


def _fnames(app, level):
    return {f["name"] for f in app["models"][level]}


def blk_info(app):
    app["pages"].append({"name": _uniq("Info", _pnames(app)), "kind": "info",
                         "title": "説明", "body": "<p>ここに説明を書く．</p>"})


def blk_consent(app):
    fn = _uniq("consent", _fnames(app, "player"))
    app["models"]["player"].append({
        "name": fn, "type": "bool", "label": "研究目的でのデータ利用に同意します",
        "widget": "CheckboxInput", "test_value": True})
    app["pages"].append({"name": _uniq("Consent", _pnames(app)), "kind": "form",
                         "form_model": "player", "form_fields": [fn],
                         "title": "同意の確認", "body": ""})


def blk_numeric(app):
    fn = _uniq("answer", _fnames(app, "player"))
    app["models"]["player"].append({
        "name": fn, "type": "int", "label": "あなたの回答を入力してください",
        "min": 0, "max": 100, "test_value": 50})
    app["pages"].append({"name": _uniq("Decision", _pnames(app)), "kind": "form",
                         "form_model": "player", "form_fields": [fn],
                         "title": "意思決定", "body": ""})


def blk_likert(app):
    names = []
    for i in range(1, 4):
        fn = _uniq(f"q{i}", _fnames(app, "player"))
        app["models"]["player"].append({
            "name": fn, "type": "int", "label": f"質問{i}（質問文をここに書く）",
            "choices": LIKERT5, "widget": "LikertMatrix", "test_value": 3})
        names.append(fn)
    app["pages"].append({"name": _uniq("Questionnaire", _pnames(app)), "kind": "form",
                         "form_model": "player", "form_fields": names,
                         "title": "アンケート", "body": ""})


def blk_wtp(app):
    fn = _uniq("wtp", _fnames(app, "player"))
    app["models"]["player"].append({
        "name": fn, "type": "int", "label": "いくらまで支払ってもよいですか",
        "min": 0, "max": 100, "left_label": "0", "right_label": "100",
        "widget": "SliderNoAnchor", "test_value": 50})
    app["pages"].append({"name": _uniq("WTP", _pnames(app)), "kind": "form",
                         "form_model": "player", "form_fields": [fn],
                         "title": "支払意思額の測定", "body": ""})


def blk_wait_payoff(app):
    ln = _uniq("set_payoffs", {f["name"] for f in app.get("logic", [])})
    app.setdefault("logic", []).append({
        "name": ln, "level": "group",
        "body": "for p in group.get_players():\n    p.payoff = cu(0)  # TODO: 利得式を書く"})
    app["pages"].append({"name": _uniq("ResultsWaitPage", _pnames(app)), "kind": "wait",
                         "after_all_players_arrive": ln,
                         "body": "他の参加者を待っています…"})


def blk_results(app):
    app["pages"].append({"name": _uniq("Results", _pnames(app)), "kind": "info",
                         "title": "結果", "body": "<p>あなたの利得：{{ player.payoff }}</p>"})


def blk_chat(app):
    # oTree組み込みの {{ chat }} タグを使う（live_method不要）
    app["pages"].append({
        "name": _uniq("Chat", _pnames(app)), "kind": "info",
        "title": "話し合い", "timeout_seconds": 120,
        "body": ("<p>同じグループのメンバーと自由に話し合ってください．"
                 "残り時間が0になると自動的に先へ進みます．</p>\n{{ chat }}")})


def blk_live(app):
    fn = _uniq("total_clicks", _fnames(app, "group"))
    app["models"]["group"].append({
        "name": fn, "type": "int", "initial": 0,
        "label": "クリック合計", "doc": "ライブページの集計見本"})
    app["pages"].append({
        "name": _uniq("LiveDemo", _pnames(app)), "kind": "info",
        "title": "リアルタイム集計（見本）",
        "body": (
            "<p>ボタンを押すと，グループ全員の画面の合計が即時に増える"
            "（リアルタイム通信の見本．自由に書き換えてよい）．</p>\n"
            '<button type="button" class="btn btn-primary" '
            'onclick="liveSend({})">＋1 を送る</button>\n'
            '<p class="mt-3">グループ合計：<b><span id="live_total">0</span></b> 回</p>\n'
            "<script>\n"
            "function liveRecv(data) {\n"
            "  document.getElementById('live_total').textContent = data.total;\n"
            "}\n"
            "</script>"),
        "live_method": (f"group = player.group\n"
                        f"group.{fn} += 1\n"
                        f"return {{0: dict(total=group.{fn})}}"),
        "live_test": "method(1, {})",
    })


def blk_chart(app):
    # セクション形式で追加する（GUIでそのまま組み替えられる）
    app["pages"].append({
        "name": _uniq("PayoffChart", _pnames(app)), "kind": "info",
        "title": "結果グラフ",
        "content": [
            {"type": "paragraph", "text": "グループ内の各メンバーの利得："},
            {"type": "chart", "kind": "bar", "label": "利得", "source": "group_payoff"},
        ],
        "js_vars": DEFAULT_CHART_JS_VARS,
    })


BLOCKS = [
    ("📄 説明ページ", "実験の説明・教示文のページを追加する", blk_info),
    ("☑ 同意ページ", "同意チェックボックスつきのページを追加する", blk_consent),
    ("✏️ 数値入力", "数値1欄の意思決定ページを追加する（min/maxは後から調整）", blk_numeric),
    ("📊 リッカート3問", "5件法のリッカート表（3問）ページを追加する", blk_likert),
    ("🎚 WTP測定", "つまみ非表示スライダーで支払意思額を測るページを追加する", blk_wtp),
    ("⏳ 待機＋利得計算", "全員到着で利得計算を実行する待機ページを追加する", blk_wait_payoff),
    ("🏁 結果ページ", "利得を表示する結果ページを追加する", blk_results),
    ("📈 結果グラフ", "メンバー別利得の棒グラフページを追加する（利得計算の後に置く）", blk_chart),
    ("💬 チャット", "グループ内チャットページを追加する（制限時間つき）", blk_chat),
    ("⚡ ライブページ", "リアルタイム通信（liveSend/liveRecv）の見本ページを追加する", blk_live),
]


def flow_dot(spec, current_idx):
    """全appのpage_sequenceをDOT形式のフロー図にする（表示条件は分岐ラベルで示す）"""
    style = {"info": ("note", "#dbeafe"), "form": ("box", "#dcfce7"),
             "wait": ("hexagon", "#ffedd5")}
    lines = [
        "digraph G {",
        "  rankdir=LR;",
        '  graph [fontname="sans-serif", fontsize=11];',
        '  node [fontname="sans-serif", fontsize=10, style=filled];',
        '  edge [color="#94a3b8"];',
    ]
    prev_last = None
    for ai, a in enumerate(spec["apps"]):
        border = "#16a34a" if ai == current_idx else "#cbd5e1"
        lines.append(f"  subgraph cluster_{ai} {{")
        lines.append(f'    label="{ai + 1}. {a["name"]}"; color="{border}"; style=rounded;')
        nids = []
        for pi, pg in enumerate(a["pages"]):
            nid = f"a{ai}p{pi}"
            shape, fill = style.get(pg.get("kind"), ("box", "#eeeeee"))
            lab = pg["name"]
            if pg.get("kind") == "form":
                ff = pg.get("form_fields", [])
                lab += "\\n入力: " + ", ".join(ff[:3]) + ("…" if len(ff) > 3 else "")
            if pg.get("kind") == "wait" and pg.get("after_all_players_arrive"):
                lab += "\\n実行: " + pg["after_all_players_arrive"]
            cond = pg.get("display_if")
            if cond:
                lab += "\\n表示: " + COND_REV.get(cond, cond)
            lab = lab.replace('"', '\\"')
            lines.append(f'    {nid} [label="{lab}", shape={shape}, fillcolor="{fill}"];')
            nids.append(nid)
        lines.append("  }")
        for x, y in zip(nids, nids[1:]):
            lines.append(f"  {x} -> {y};")
        if prev_last and nids:
            lines.append(f'  {prev_last} -> {nids[0]} [style=dashed, label="次のapp"];')
        if nids:
            prev_last = nids[-1]
    lines.append("}")
    return "\n".join(lines)


# ------------------------------------------------- セクションエディタ

SEC_LABELS = {"heading": "🔠 見出し", "paragraph": "📝 段落", "alert": "⚠️ 強調ボックス",
              "image": "🖼 画像", "table": "📋 表", "columns": "◫ 2カラム",
              "field": "✏️ フォーム欄", "chart": "📈 グラフ", "html": "</> HTML"}

# グラフセクション・ブロックで使う既定のjs_vars（メンバー別利得）
DEFAULT_CHART_JS_VARS = (
    "players = player.group.get_players()\n"
    "return dict(labels=[f'参加者{p.id_in_group}' for p in players],\n"
    "            values=[float(p.payoff) for p in players])")

# グラフの「データ源」：選ぶとjs_varsを自動生成する（customのみ手書き）
CHART_SOURCES = {
    "group_payoff": "メンバー別の利得",
    "group_field": "メンバー別の入力値（欄を選ぶ）",
    "rounds_payoff": "ラウンド推移（自分の利得）",
    "rounds_field": "ラウンド推移（自分の入力値）",
    "custom": "カスタム（js_varsを自分で書く）",
}


def make_chart_js_vars(source, field=None):
    """データ源の選択からjs_vars本体（Pythonコード）を生成する"""
    if source == "group_field" and field:
        return (
            "players = player.group.get_players()\n"
            "return dict(labels=[f'参加者{p.id_in_group}' for p in players],\n"
            f"            values=[float(p.field_maybe_none('{field}') or 0) for p in players])")
    if source == "rounds_payoff":
        return (
            "rounds = player.in_all_rounds()\n"
            "return dict(labels=[f'R{p.round_number}' for p in rounds],\n"
            "            values=[float(p.payoff or 0) for p in rounds])")
    if source == "rounds_field" and field:
        return (
            "rounds = player.in_all_rounds()\n"
            "return dict(labels=[f'R{p.round_number}' for p in rounds],\n"
            f"            values=[float(p.field_maybe_none('{field}') or 0) for p in rounds])")
    return DEFAULT_CHART_JS_VARS


# js_vars欄の定番パターン（置換挿入用）
JS_VARS_PATTERNS = {
    "メンバー別の利得": DEFAULT_CHART_JS_VARS,
    "ラウンド推移（自分の利得）": make_chart_js_vars("rounds_payoff"),
    "複数系列の例（自由に編集）": (
        "players = player.group.get_players()\n"
        "return dict(\n"
        "    labels=[f'参加者{p.id_in_group}' for p in players],\n"
        "    values=[float(p.payoff) for p in players],\n"
        "    my_id=player.id_in_group,\n"
        ")"),
}

# HTMLセクションの定番スニペット（末尾に追記挿入する）
HTML_SNIPPETS = {
    "YouTube動画の埋め込み": ('<div class="ratio ratio-16x9 my-3">\n'
                          '  <iframe src="https://www.youtube.com/embed/動画ID" '
                          'allowfullscreen></iframe>\n</div>'),
    "音声プレーヤー": '<audio controls src="音声ファイルのURL" class="w-100 my-2"></audio>',
    "大きな数字（利得の強調表示）": ('<div class="text-center my-4">\n'
                          '  <div class="display-4 fw-bold">{{ player.payoff }}</div>\n'
                          '  <div class="text-muted">あなたの利得</div>\n</div>'),
    "折りたたみ（詳細説明）": ('<details class="my-2">\n  <summary>詳しい説明を見る</summary>\n'
                       '  <div class="mt-2">ここに詳細を書く．</div>\n</details>'),
    "リンクボタン": ('<a href="https://example.com" target="_blank" '
               'class="btn btn-outline-primary my-2">資料を開く</a>'),
    "区切り線": '<hr class="my-4">',
    "余白": '<div style="height: 2rem"></div>',
    "グループチャット": '{{ chat }}',
    "ライブ通信の送受信（liveSend/liveRecv）": (
        '<button type="button" class="btn btn-primary"\n'
        '        onclick="liveSend({action: \'click\'})">送信する</button>\n'
        '<p class="mt-3">受信値：<b><span id="live_out">—</span></b></p>\n'
        '<script>\n'
        'function liveRecv(data) {\n'
        "  document.getElementById('live_out').textContent = JSON.stringify(data);\n"
        '}\n'
        '</script>'),
}

# ライブページ（live_method）の既定コード：受信→グループ全員へ送り返す
DEFAULT_LIVE_METHOD = (
    "# data は liveSend() で送られたもの．返り値は {宛先id: データ}（0=グループ全員）\n"
    "return {0: data}")


def default_section(t, pg):
    """セクション追加時の初期値を返す"""
    if t == "heading":
        return {"type": "heading", "text": "見出し", "level": 4}
    if t == "paragraph":
        return {"type": "paragraph", "text": "本文を書く．"}
    if t == "alert":
        return {"type": "alert", "style": "info", "text": "重要な注意事項を書く．"}
    if t == "image":
        return {"type": "image", "src": "", "width": 60, "caption": ""}
    if t == "table":
        return {"type": "table", "header": ["項目", "値"], "rows": [["例", "100"]]}
    if t == "columns":
        return {"type": "columns", "left": "左の内容", "right": "右の内容"}
    if t == "field":
        placed = {s.get("name") for s in pg.get("content", []) if s.get("type") == "field"}
        avail = [n for n in pg.get("form_fields", []) if n not in placed]
        return {"type": "field", "name": avail[0] if avail else ""}
    if t == "chart":
        return {"type": "chart", "kind": "bar", "label": "利得", "source": "group_payoff"}
    return {"type": "html", "code": ""}


def edit_sections(pg, app, pi):
    """ページのcontent（セクション配列）を編集するUIを描画する"""
    content = pg["content"]
    for si, sec in enumerate(content):
        t = sec.get("type", "html")
        h1, h2, h3, h4 = st.columns([6, 1, 1, 1])
        h1.markdown(f"**{si + 1}. {SEC_LABELS.get(t, t)}**")
        if h2.button("↑", key=K(f"sec_up_{pi}_{si}")) and si > 0:
            content[si - 1], content[si] = content[si], content[si - 1]
            load_spec(spec, keep_app=True, keep_test=True); st.rerun()
        if h3.button("↓", key=K(f"sec_dn_{pi}_{si}")) and si < len(content) - 1:
            content[si + 1], content[si] = content[si], content[si + 1]
            load_spec(spec, keep_app=True, keep_test=True); st.rerun()
        if h4.button("✕", key=K(f"sec_del_{pi}_{si}")):
            content.pop(si)
            load_spec(spec, keep_app=True, keep_test=True); st.rerun()

        if t == "heading":
            sec["text"] = st.text_input("見出し", value=sec.get("text", ""),
                                        key=K(f"sec_h_{pi}_{si}"), label_visibility="collapsed")
        elif t == "paragraph":
            sec["text"] = st.text_area("段落", value=sec.get("text", ""), height=70,
                                       key=K(f"sec_p_{pi}_{si}"), label_visibility="collapsed")
        elif t == "alert":
            ac1, ac2 = st.columns([1, 4])
            styles = {"info": "青（情報）", "warning": "黄（注意）",
                      "success": "緑（OK）", "danger": "赤（警告）"}
            cur = sec.get("style", "info")
            sec["style"] = ac1.selectbox("色", list(styles), format_func=styles.get,
                                         index=list(styles).index(cur) if cur in styles else 0,
                                         key=K(f"sec_as_{pi}_{si}"))
            sec["text"] = ac2.text_area("内容", value=sec.get("text", ""), height=70,
                                        key=K(f"sec_at_{pi}_{si}"))
        elif t == "image":
            ic1, ic2, ic3 = st.columns([3, 2, 1])
            sec["src"] = ic1.text_input("画像URL（_static配下 or https://…）",
                                        value=sec.get("src", ""), key=K(f"sec_is_{pi}_{si}"))
            sec["caption"] = ic2.text_input("キャプション", value=sec.get("caption", ""),
                                            key=K(f"sec_ic_{pi}_{si}"))
            sec["width"] = int(ic3.number_input("幅%", 10, 100, value=int(sec.get("width", 100)),
                                                key=K(f"sec_iw_{pi}_{si}")))
        elif t == "table":
            txt = "\n".join(" | ".join(str(c) for c in r)
                            for r in [sec.get("header", [])] + sec.get("rows", []))
            edited = st.text_area("表（1行＝1レコード，セルは | 区切り，1行目＝見出し）",
                                  value=txt, height=90, key=K(f"sec_tb_{pi}_{si}"))
            cells = [[c.strip() for c in line.split("|")]
                     for line in edited.splitlines() if line.strip()]
            sec["header"] = cells[0] if cells else []
            sec["rows"] = cells[1:] if len(cells) > 1 else []
        elif t == "columns":
            cc1, cc2 = st.columns(2)
            sec["left"] = cc1.text_area("左", value=sec.get("left", ""), height=70,
                                        key=K(f"sec_cl_{pi}_{si}"))
            sec["right"] = cc2.text_area("右", value=sec.get("right", ""), height=70,
                                         key=K(f"sec_cr_{pi}_{si}"))
        elif t == "field":
            avail = pg.get("form_fields", [])
            if avail:
                cur = sec.get("name")
                sec["name"] = st.selectbox(
                    "この位置に置く欄", avail,
                    index=avail.index(cur) if cur in avail else 0,
                    key=K(f"sec_f_{pi}_{si}"))
            else:
                st.warning("form_fields が空——上のフォーム欄選択で先に欄を選ぶ")
        elif t == "chart":
            sc1, sc2 = st.columns([3, 2])
            cur_src = sec.get("source", "custom" if pg.get("js_vars") else "group_payoff")
            sec["source"] = sc1.selectbox(
                "データ源（js_varsを自動生成する）", list(CHART_SOURCES),
                format_func=CHART_SOURCES.get,
                index=(list(CHART_SOURCES).index(cur_src)
                       if cur_src in CHART_SOURCES else 0),
                key=K(f"sec_gs_{pi}_{si}"))
            num_fields = [f["name"] for f in app["models"].get("player", [])
                          if f.get("type") in ("int", "float", "currency")]
            if sec["source"] in ("group_field", "rounds_field"):
                if num_fields:
                    curf = sec.get("field")
                    sec["field"] = sc2.selectbox(
                        "グラフ化する欄（player・数値型）", num_fields,
                        index=num_fields.index(curf) if curf in num_fields else 0,
                        key=K(f"sec_gf_{pi}_{si}"))
                else:
                    sc2.warning("playerに数値型の欄がない——モデルタブで先に定義する")
            # データ源がカスタム以外なら，js_varsを選択に合わせて自動更新する
            if sec["source"] != "custom":
                pg["js_vars"] = make_chart_js_vars(sec["source"], sec.get("field"))
            gc1, gc2 = st.columns(2)
            kinds = {"bar": "棒グラフ", "line": "折れ線"}
            curk = sec.get("kind", "bar")
            sec["kind"] = gc1.selectbox("種類", list(kinds), format_func=kinds.get,
                                        index=list(kinds).index(curk) if curk in kinds else 0,
                                        key=K(f"sec_gk_{pi}_{si}"))
            sec["label"] = gc2.text_input("系列名", value=sec.get("label", "値"),
                                          key=K(f"sec_gl_{pi}_{si}"))
            if sec["source"] == "custom" and not pg.get("js_vars"):
                st.caption("⚠ データ源として js_vars（labels と values を返す）が必要——下で定義する")
        else:  # html
            sec["code"] = st.text_area("HTML（エスケープハッチ）", value=sec.get("code", ""),
                                       height=90, key=K(f"sec_ht_{pi}_{si}"))
            hp1, hp2 = st.columns([3, 1])
            snip = hp1.selectbox("定番スニペット", list(HTML_SNIPPETS),
                                 key=K(f"sec_hs_{pi}_{si}"), label_visibility="collapsed")
            if hp2.button("末尾に挿入", key=K(f"sec_hi_{pi}_{si}"), width="stretch"):
                cur = sec.get("code", "").rstrip()
                sec["code"] = (cur + "\n" if cur else "") + HTML_SNIPPETS[snip]
                load_spec(spec, keep_app=True, keep_test=True); st.rerun()

    nc1, nc2 = st.columns([3, 1])
    new_t = nc1.selectbox("追加するセクション", list(SEC_LABELS), format_func=SEC_LABELS.get,
                          key=K(f"sec_new_{pi}"), label_visibility="collapsed")
    if nc2.button("＋ 追加", key=K(f"sec_add_{pi}"), width="stretch"):
        content.append(default_section(new_t, pg))
        # グラフ追加時はデータ源のjs_varsも用意する
        if new_t == "chart" and not pg.get("js_vars"):
            pg["js_vars"] = DEFAULT_CHART_JS_VARS
        load_spec(spec, keep_app=True, keep_test=True); st.rerun()
    if pg["kind"] == "form":
        placed = {s.get("name") for s in content if s.get("type") == "field"}
        rest = [n for n in pg.get("form_fields", []) if n not in placed]
        if rest:
            st.caption(f"未配置のフォーム欄（{', '.join(rest)}）は本文の後ろに自動で入る")


# ------------------------------------------------- 参加者画面ライブプレビュー

def _std_input_html(f):
    """oTree標準widget（{{ formfield }} 相当）のプレビュー用HTMLを返す"""
    esc = forge._esc
    name = f.get("name", "")
    label = esc(f.get("label") or name)
    ftype = f.get("type", "int")
    w = f.get("widget")
    ch = f.get("choices")
    if ch:
        pairs = [(c[0], c[1]) if isinstance(c, (list, tuple)) else (c, c) for c in ch]
        if w in ("RadioSelect", "RadioSelectHorizontal"):
            inline = " form-check-inline" if w == "RadioSelectHorizontal" else ""
            items = "".join(
                f'<div class="form-check{inline}">'
                f'<input class="form-check-input" type="radio" name="{name}" id="{name}_{i}">'
                f'<label class="form-check-label" for="{name}_{i}">{esc(lab)}</label></div>'
                for i, (_, lab) in enumerate(pairs))
            return f'<div class="mb-3"><p class="form-label">{label}</p>{items}</div>'
        opts = "".join(f"<option>{esc(lab)}</option>" for _, lab in pairs)
        return (f'<div class="mb-3"><label class="form-label">{label}</label>'
                f'<select class="form-select" style="max-width: 20rem">{opts}</select></div>')
    if ftype == "bool":
        if w == "CheckboxInput":
            return (f'<div class="form-check mb-3">'
                    f'<input class="form-check-input" type="checkbox" id="{name}">'
                    f'<label class="form-check-label" for="{name}">{label}</label></div>')
        return (f'<div class="mb-3"><p class="form-label">{label}</p>'
                f'<div class="form-check"><input class="form-check-input" type="radio" '
                f'name="{name}"><label class="form-check-label">はい</label></div>'
                f'<div class="form-check"><input class="form-check-input" type="radio" '
                f'name="{name}"><label class="form-check-label">いいえ</label></div></div>')
    if ftype == "longstr":
        return (f'<div class="mb-3"><label class="form-label">{label}</label>'
                f'<textarea class="form-control" rows="3"></textarea></div>')
    if ftype in ("int", "float", "currency"):
        attrs = "".join(f' {k}="{v}"' for k, v in (("min", f.get("min")), ("max", f.get("max")))
                        if isinstance(v, (int, float)))
        return (f'<div class="mb-3"><label class="form-label">{label}</label>'
                f'<input type="number" class="form-control" style="max-width: 14rem"{attrs}></div>')
    return (f'<div class="mb-3"><label class="form-label">{label}</label>'
            f'<input type="text" class="form-control"></div>')


def _fmt_cu(v):
    """通貨値をoTree実機に近い書式にする（use_points/通貨コードを反映）"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return v
    sess = st.session_state.spec.get("session", {})
    if sess.get("use_points", True):
        return f"{x:,.0f}ポイント"
    code = sess.get("currency_code", "JPY")
    return f"¥{x:,.0f}" if code == "JPY" else f"{x:,.2f} {code}"


def _preview_vars(app):
    """テンプレート変数のサンプル値（定数・test_value）を集める"""
    c = app["constants"]
    vals = {
        "C.PLAYERS_PER_GROUP": c.get("players_per_group") if c.get("players_per_group") else "—",
        "C.NUM_ROUNDS": c.get("num_rounds", 1),
        "player.round_number": 1, "player.id_in_group": 1,
        "player.payoff": _fmt_cu(1000),
        "group.total_contribution": "···", "subsession.round_number": 1,
        # 組み込みチャットはプレビューでは動かないため見た目だけのモックを置く
        "chat": ('<div class="card my-3"><div class="card-body text-muted small">'
                 '💬 グループチャット（実機で動作する——プレビューでは送受信しない）'
                 '</div></div>'),
    }
    const_ns = forge._const_ns(app)  # "cu(1000)" 等も数値に解決される
    for e in c.get("extra", []):
        v = const_ns.get(e["name"], e["value"])
        vals[f"C.{e['name']}"] = _fmt_cu(v) if e.get("type") == "currency" else v
    for lvl in ("player", "group", "subsession"):
        for f in app["models"].get(lvl, []):
            tv = f.get("test_value")
            if tv is not None and f.get("type") == "currency":
                tv = _fmt_cu(tv)
            vals[f"{lvl}.{f['name']}"] = "···" if tv is None else tv
    return vals


def _subst_template_vars(src, vals):
    """{{ 式 }} をサンプル値で置換する（不明な式は ··· とする）"""
    def rep(m):
        return str(vals.get(m.group(1).strip(), "···"))
    return re.sub(r"\{\{\s*([^}]*?)\s*\}\}", rep, src)


def render_preview_html(pg, app, theme):
    """ページ仕様から参加者画面の近似HTML（自己完結ドキュメント）を生成する"""
    if pg["kind"] == "wait":
        title = "お待ちください"
        msg = forge._esc(pg.get("body") or "他の参加者の到着を待っています…")
        content = (f'<div class="card mt-4"><div class="card-body text-center text-muted py-5">'
                   f'<div class="spinner-border spinner-border-sm me-2"></div>{msg}</div></div>')
    else:
        # 生成エンジンの出力（カスタムwidgetの実HTML込み）を流用し，
        # oTree専用タグだけプレビュー用に実体化する
        tpl = forge.emit_page_html(pg, app)
        head, rest = tpl.split("{{ endblock }}", 1)
        title = head.replace("{{ block title }}", "").strip()
        content = rest.replace("{{ block content }}", "").rsplit("{{ endblock }}", 1)[0].strip()
        fdefs = {f["name"]: forge.resolve_field_nums(f, app)
                 for f in app["models"].get(pg.get("form_model", "player"), [])}
        content = re.sub(
            r"\{\{\s*formfield\s+'(\w+)'\s*\}\}",
            lambda m: _std_input_html(fdefs.get(m.group(1), {"name": m.group(1), "type": "str"})),
            content)
        content = content.replace(
            "{{ next_button }}",
            '<button class="btn btn-primary otree-btn-next mt-3">次へ</button>')
        vals = _preview_vars(app)
        title = _subst_template_vars(title, vals)
        content = _subst_template_vars(content, vals)
    css = forge.theme_css(theme or {})
    links = forge.font_links(theme or {})
    # js_vars はサーバで実行されるため，プレビューでは空配列を返すスタブで代用する．
    # liveSend もサーバ通信のため，プレビューでは何もしないスタブにする
    stub = ('<script>var js_vars = new Proxy({}, { get: function () { return []; } });'
            'function liveSend(x) { /* プレビューでは送信しない */ }'
            '</script>\n')
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
{links}<style>
{css}
</style></head>
<body>{stub}<div class="container" style="max-width: 720px; padding: 1.5rem 1rem;">
<h2 class="otree-title mb-3">{title}</h2>
{content}
</div></body></html>"""


ESL_SCHEMA_DOC = """あなたはoTree実験のESL(Experiment Specification Language) v0.1仕様を書く専門家である．
ESLはJSONで，トップレベルは esl_version("0.1")/meta{name,display_name,doc}/session{config_name,display_name,app_sequence,num_demo_participants,language_code,currency_code,use_points}/apps[]．
appは name(Python識別子)/doc/constants{players_per_group,num_rounds,extra[{name,type,value}]}/models{subsession[],group[],player[]}/logic[]/pages[]．
フィールドは {name,type,label,doc,min,max,choices,widget,test_value}．typeは currency|int|float|bool|str|longstr．min/max/valueに文字列を与えるとPython式として埋め込まれる（例 "C.ENDOWMENT"）．ただしstr/longstr型の値は文字列リテラルとして扱う（式不可）．
入力形式：choices はリスト（[1,2,3]）または[値,ラベル]の組のリスト（[[1,"低"],[5,"高"]]）で，既定はドロップダウン表示．widget は RadioSelect|RadioSelectHorizontal|CheckboxInput|Slider|SliderNoAnchor|ButtonSelect|StarRating|LikertMatrix|NumberStepper|MultiCheckbox．
- リッカート尺度（1問）：int + choices + RadioSelectHorizontal．複数問を表にまとめるなら各欄に LikertMatrix（同じchoicesの欄が連続すると1つの表になる）
- 同意チェック：bool + CheckboxInput／自由記述：longstr（textarea表示）
- Slider/SliderNoAnchor：数値型でmin/max必須（任意で step・initial・left_label・right_label）．SliderNoAnchor は初期つまみ非表示でクリックするまで未回答扱い（アンカリング回避，WTP測定等に推奨）
- ButtonSelect：choices必須．押した瞬間に送信して次ページへ進む（全欄がButtonSelectなら次へボタンは出ない）
- StarRating：int型，max=星の数（min 1推奨）／NumberStepper：数値型，−/＋ボタン
- MultiCheckbox：str型 + choices必須．選択値をカンマ区切りで保存（test_value は "a,b" 形式）
choicesがある欄の test_value は choices の値から選ぶ（MultiCheckboxを除く）．
ページの本文は body（生HTML）の代わりに content（セクション配列）でも書ける．contentがあればbodyは無視される．セクション：{type:"heading",text,level?}/{type:"paragraph",text}/{type:"alert",style:info|warning|success|danger,text}/{type:"image",src,width?,caption?}/{type:"table",header[],rows[][]}/{type:"columns",left,right}/{type:"field",name}（formページのみ，form_fieldsの欄をその位置に配置．未配置の欄は本文の後ろに自動挿入）/{type:"chart",kind:bar|line,label}（js_varsのlabels/valuesを描画）/{type:"html",code}．textにはインラインHTMLと {{ C.X }} 等が使える．
ページに raw_html: true を付けると本文HTMLを完全手動とし，formfields/next_buttonを自動挿入しない（{{ formfield 'x' }} や {{ next_button }} を本文に自分で書く）．
ページに js_vars（Pythonコード，return dict(...) で終える）を置くと，テンプレートJSの js_vars 変数にデータが渡る．Chart.jsをCDNで読み込み <canvas> に描画すればグラフページを作れる．
トップレベルに任意で theme{font,base_color,bg_color,text_color,font_size} を置くと参加者画面全体のフォント・配色を変えられる（fontはGoogle Fonts名，色は#RRGGBB）．
トップレベルに任意で rooms[{name,display_name,participant_labels?[],use_secure_urls?}] を置くと固定URL（/room/部屋名）で参加者を受け入れられる（実験室・オンライン本番用）．nameは英数字，participant_labels は座席・ID等のリスト（_rooms/部屋名.txt として生成），use_secure_urls にはラベルが必要．
logicは {name,level(group|player|subsession),body}．bodyは関数本体のPython（def行なし・インデント0基準），通貨は cu()．
pagesは順序がpage_sequence．kind=info{name,title,body,display_if?}/form{name,form_model,form_fields[],title,body,display_if?}/wait{name,after_all_players_arrive?,body?,group_by_arrival_time?}．
bodyはHTMLでoTree記法 {{ C.X }} 等が使える．display_ifはPython式．
waitページに group_by_arrival_time: true を付けると到着順に players_per_group 人ずつ組にする（appの最初のページである場合のみ可）．
ページ（wait以外）に live_method（Pythonコード，playerとdataを受け取り {宛先id: データ} を返す．0=グループ全員）を置くとライブページになる．本文側は liveSend(データ) で送信し function liveRecv(data){...} で受信する．live_test（例 "method(1, {})"）を付けるとbotテスト中にliveSendを模擬できる．グループチャットは live不要で本文に {{ chat }} と書くだけでよい．
逐次手番は form(P1のみ)→wait→form(P2のみ)→wait(利得計算)→結果 のパターン．

厳守する制約：
- session.config_name と app_sequence の要素は app.name と一致させる
- formページは form_fields を1つ以上持ち，全フィールドが models.{form_model} に定義済みであること
- formで使う全フィールドに test_value を付ける（数値リテラル，min/max の範囲内）
- フィールド名に payoff/round_number/id_in_group/role 等のoTree予約語を使わない
- 利得計算は logic（level=group）に書き，waitページの after_all_players_arrive から名前で参照する
- 定数は C.NAME で参照する．C.PLAYERS_PER_GROUP と C.NUM_ROUNDS は自動定義される
- logicのbodyと display_if は構文的に正しいPythonであること"""


# ---------------------------------------------------------------- AI helpers

# 環境変数 FORGE_AI_MODEL で差し替え可能（既定は最新のSonnet）
AI_MODEL = os.environ.get("FORGE_AI_MODEL", "claude-sonnet-4-5")
# 大きな仕様でもJSONが途中で切れないよう余裕を持たせる
AI_MAX_TOKENS = 16000


def _extract_json(text):
    """応答テキストからJSON部分のみを取り出す（コードフェンス等を除去）"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("応答にJSONが含まれていない")
    return json.loads(text[start:end + 1])


def _call_claude(api_key, messages):
    import requests
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": AI_MODEL, "max_tokens": AI_MAX_TOKENS, "temperature": 0,
              "messages": messages},
        timeout=180,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")


def ai_generate_spec(api_key, current_spec, instruction, max_attempts=3):
    """AIで仕様を生成し，forge.validate で検証する．

    検証エラーはAIに差し戻して自動修正させる（最大 max_attempts 回）．
    成功時は (new_spec, 試行回数) を返し，失敗時は RuntimeError を送出する．
    """
    prompt = (ESL_SCHEMA_DOC + "\n\n# 現在編集中の仕様\n"
              + json.dumps(current_spec, ensure_ascii=False)
              + "\n\n# ユーザーの指示\n" + instruction
              + "\n\n# 出力規則\n- 更新後の完全なESL仕様を有効なJSONのみで出力（コードフェンス・説明禁止）"
              + "\n- 未指定のデザイン判断は常識的に仮置きし，metaのdocに「要確認: 〜」と記す")
    messages = [{"role": "user", "content": prompt}]
    last_err = None
    for attempt in range(1, max_attempts + 1):
        text = _call_claude(api_key, messages)
        try:
            new_spec = _extract_json(text)
            if new_spec.get("esl_version") != "0.1" or not new_spec.get("apps"):
                raise forge.SpecError("esl_version='0.1' と apps[] は必須である")
            forge.validate(new_spec)
            for a in new_spec["apps"]:
                if forge.emit_bot(a) is None:
                    raise forge.SpecError(
                        f"app '{a.get('name')}' のformで使う全フィールドに"
                        "数値の test_value を付けること（botテスト導出に必須）")
            return new_spec, attempt
        except (ValueError, KeyError, json.JSONDecodeError, forge.SpecError) as e:
            last_err = e
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                    f"出力した仕様に次の問題がある：\n{e}\n"
                    "全て修正した完全なESL仕様を，有効なJSONのみで再出力すること．"},
            ]
    raise RuntimeError(f"{max_attempts}回試行しても妥当な仕様を生成できなかった．最後のエラー：{last_err}")


# ---------------------------------------------------------------- state

if "spec" not in st.session_state:
    st.session_state.spec = load_presets()["dictator"]
    st.session_state.ver = 0
st.session_state.setdefault("app_idx", 0)

def K(name):
    """プリセット読込・app切替で全ウィジェットを再初期化するための世代付きキー"""
    return f"v{st.session_state.ver}_a{st.session_state.app_idx}_{name}"

def load_spec(new_spec, keep_app=False, keep_test=False):
    """仕様を差し替えて全ウィジェットを再初期化する．

    keep_app: 編集中のapp位置を保つ（ページ追加等のapp内編集で使う）
    keep_test: botテスト結果を保つ（仕様の取り替えでない場合に使う）
    """
    idx = st.session_state.app_idx if keep_app else 0
    st.session_state.spec = copy.deepcopy(new_spec)
    st.session_state.ver += 1
    st.session_state.app_idx = min(idx, len(new_spec["apps"]) - 1)
    if not keep_test:
        st.session_state.pop("test_result", None)  # 古い仕様のテスト結果は捨てる

spec = st.session_state.spec
if st.session_state.app_idx >= len(spec["apps"]):
    st.session_state.app_idx = 0
app = spec["apps"][st.session_state.app_idx]

# ---- 編集履歴（undo/redo）----
# 各実行の開始時点で，仕様が前回スナップショットから変わっていれば積む．
# ウィジェット操作による変更は次の実行の冒頭で検出される（1操作=1スナップショット）．
_HIST_MAX = 80
hist = st.session_state.setdefault("hist", [json.dumps(spec, ensure_ascii=False, sort_keys=True)])
st.session_state.setdefault("hist_pos", 0)
_cur = json.dumps(spec, ensure_ascii=False, sort_keys=True)
if _cur != hist[st.session_state.hist_pos]:
    del hist[st.session_state.hist_pos + 1:]   # redo枝は捨てる
    hist.append(_cur)
    if len(hist) > _HIST_MAX:
        del hist[0]
    st.session_state.hist_pos = len(hist) - 1


def _restore_hist(pos):
    st.session_state.hist_pos = pos
    load_spec(json.loads(st.session_state.hist[pos]), keep_app=True, keep_test=True)
    st.rerun()


def coerce(v):
    """数値らしき文字列→数値，空→None，それ以外→式文字列"""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def coerce_typed(v, ftype):
    """型を考慮した値変換．str/longstr型は文字列のまま保持する（数値化しない）"""
    if ftype in ("str", "longstr"):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        s = str(v)
        return None if s.strip() in ("", "nan") else s
    return coerce(v)


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.markdown("## 🧪 oTree Forge")
    st.caption("EXPERIMENT SPEC STUDIO · ESL v0.1 (Streamlit)")

    u1, u2 = st.columns(2)
    pos = st.session_state.hist_pos
    if u1.button("↩ 元に戻す", disabled=pos == 0, width="stretch",
                 help="直前の編集を取り消す"):
        _restore_hist(pos - 1)
    if u2.button("↪ やり直す", disabled=pos >= len(st.session_state.hist) - 1,
                 width="stretch", help="取り消した編集を再適用する"):
        _restore_hist(pos + 1)

    presets = load_presets()
    pk = st.selectbox("プリセット", list(presets.keys()),
                      format_func=lambda x: PRESET_LABELS.get(x, x), key="preset_sel")
    if st.button("このプリセットを読み込む（現在の編集は破棄）"):
        load_spec(presets[pk])
        st.rerun()

    st.divider()
    st.markdown("### ✦ AIで作る・修正する")
    default_key = os.environ.get("ANTHROPIC_API_KEY", "")
    try:
        if (ROOT / ".streamlit" / "secrets.toml").exists() or (Path.home() / ".streamlit" / "secrets.toml").exists():
            default_key = st.secrets.get("ANTHROPIC_API_KEY", default_key)
    except Exception:
        pass
    api_key = st.text_input("Anthropic APIキー", type="password",
                            value=default_key,
                            help="入力されたキーはこのセッションのメモリ上にのみ保持される")
    ai_instr = st.text_area("作りたい実験／変更内容（日本語）",
                            placeholder="例：最後通牒ゲームに変えて．拒否なら両者の利得は0")
    if st.button("実行", disabled=not (api_key and ai_instr.strip())):
        with st.spinner("仕様を生成・検証中…（エラーがあれば自動修正する）"):
            try:
                new_spec, attempts = ai_generate_spec(api_key, spec, ai_instr)
                load_spec(new_spec)
                st.session_state.ai_msg = (
                    "仕様を更新した（検証済み" + (f"・自動修正{attempts - 1}回" if attempts > 1 else "") + "）")
                st.rerun()
            except Exception as e:
                st.error(f"失敗：{e}")
    msg = st.session_state.pop("ai_msg", None)
    if msg:
        st.success(msg)

# ---------------------------------------------------------------- layout

st.markdown("## oTree :green[Forge] Studio")
left, right = st.columns([11, 9], gap="large")

# ============================ 左：編集 ============================
with left:
    # ---- 編集対象appの切替・追加・削除（複数app対応） ----
    idx = st.session_state.app_idx
    ab1, ab2, ab3, ab4, ab5 = st.columns([5, 1, 1, 2, 2])
    sel = ab1.selectbox(
        "編集するアプリ（実験の進行順）", range(len(spec["apps"])),
        format_func=lambda i: f"{i + 1}. {spec['apps'][i]['name']}",
        index=idx, key=f"appsel_v{st.session_state.ver}_{idx}")
    if sel != idx:
        st.session_state.app_idx = sel
        st.rerun()
    if ab2.button("↑", disabled=idx == 0, help="このappを1つ前へ"):
        spec["apps"][idx - 1], spec["apps"][idx] = spec["apps"][idx], spec["apps"][idx - 1]
        st.session_state.app_idx = idx - 1
        st.session_state.ver += 1
        st.rerun()
    if ab3.button("↓", disabled=idx == len(spec["apps"]) - 1, help="このappを1つ後へ"):
        spec["apps"][idx + 1], spec["apps"][idx] = spec["apps"][idx], spec["apps"][idx + 1]
        st.session_state.app_idx = idx + 1
        st.session_state.ver += 1
        st.rerun()
    if ab4.button("＋ app追加", help="アンケート等を別appとして後ろに追加する"):
        existing = {a["name"] for a in spec["apps"]}
        n = len(spec["apps"]) + 1
        while f"app{n}" in existing:
            n += 1
        new_app = copy.deepcopy(BLANK["apps"][0])
        new_app["name"] = f"app{n}"
        spec["apps"].append(new_app)
        st.session_state.app_idx = len(spec["apps"]) - 1
        st.session_state.ver += 1
        st.rerun()
    if ab5.button("✕ app削除", disabled=len(spec["apps"]) == 1):
        spec["apps"].pop(idx)
        st.session_state.app_idx = 0
        st.session_state.ver += 1
        st.rerun()

    tab_basic, tab_model, tab_logic, tab_pages = st.tabs(
        ["基本・定数", "モデル", "ロジック", "ページ"])

    # ---- 基本・定数 ----
    with tab_basic:
        c1, c2 = st.columns(2)
        name = c1.text_input("アプリ名（NAME_IN_URL）", value=app["name"], key=K("appname"))
        app["name"] = name
        # セッション設定はapp一覧から自動導出する（先頭appを代表名とする）
        spec["meta"]["name"] = spec["apps"][0]["name"]
        spec["session"]["config_name"] = spec["apps"][0]["name"]
        spec["session"]["app_sequence"] = [a["name"] for a in spec["apps"]]
        disp = c2.text_input("表示名", value=spec["session"].get("display_name", ""), key=K("dispname"))
        spec["session"]["display_name"] = disp
        spec["meta"]["display_name"] = disp
        app["doc"] = st.text_area("説明（doc）", value=app.get("doc", ""), key=K("appdoc"), height=68)
        c3, c4, c5 = st.columns(3)
        with c3:
            ppg_cur = app["constants"].get("players_per_group")
            no_group = st.checkbox("グループなし（個人課題）", value=ppg_cur is None,
                                   key=K("nogrp"),
                                   help="アンケート等，他の参加者を待たない実験で使う"
                                        "（PLAYERS_PER_GROUP = None）")
            if no_group:
                app["constants"]["players_per_group"] = None
            else:
                app["constants"]["players_per_group"] = st.number_input(
                    "グループ人数", 2, 50,
                    value=ppg_cur if isinstance(ppg_cur, int) and ppg_cur >= 2 else 2,
                    key=K("ppg"))
        app["constants"]["num_rounds"] = c4.number_input(
            "ラウンド数", 1, 200, value=app["constants"].get("num_rounds", 1), key=K("rounds"))
        spec["session"]["num_demo_participants"] = c5.number_input(
            "デモ参加者数", 1, 100, value=spec["session"].get("num_demo_participants", 2), key=K("ndemo"))

        st.markdown("###### 定数（class C に追加される）")
        cdf = pd.DataFrame(
            [{"name": e["name"], "type": e["type"], "value": str(e["value"])}
             for e in app["constants"].get("extra", [])],
            columns=["name", "type", "value"])
        ced = st.data_editor(
            cdf, num_rows="dynamic", key=K("consts"), width="stretch",
            column_config={
                "name": st.column_config.TextColumn("名前（大文字推奨）"),
                "type": st.column_config.SelectboxColumn("型", options=list(forge.FIELD_TYPES)),
                "value": st.column_config.TextColumn("値（数値または式）"),
            })
        app["constants"]["extra"] = [
            {"name": str(r["name"]).strip(), "type": r["type"] or "int",
             "value": coerce_typed(r["value"], r["type"] or "int")}
            for _, r in ced.iterrows() if str(r["name"]).strip() and str(r["name"]).strip() != "nan"
        ]

        st.markdown("###### 🎨 デザインテーマ（参加者画面の見た目・全app共通）")
        use_theme = st.checkbox("カスタムテーマを使う", value=bool(spec.get("theme")),
                                key=K("usetheme"),
                                help="フォント・配色を全ページに一括適用する"
                                     "（_templates/global/Page.html を自動生成）")
        if use_theme:
            th = spec.setdefault("theme", {})
            tc1, tc2, tc3, tc4, tc5 = st.columns([2, 1, 1, 1, 1])
            cur_font = th.get("font") or "標準"
            sel_font = tc1.selectbox(
                "フォント", THEME_FONTS,
                index=THEME_FONTS.index(cur_font) if cur_font in THEME_FONTS else 0,
                key=K("thfont"),
                help="標準以外はGoogle Fontsから読み込む（参加者側にネット接続が必要）")
            th["font"] = None if sel_font == "標準" else sel_font
            th["base_color"] = tc2.color_picker("基調色", th.get("base_color", "#2563eb"),
                                                key=K("thbase"),
                                                help="見出し・ボタン・進捗バーの色")
            th["bg_color"] = tc3.color_picker("背景色", th.get("bg_color", "#ffffff"), key=K("thbg"))
            th["text_color"] = tc4.color_picker("文字色", th.get("text_color", "#212529"), key=K("thtext"))
            th["font_size"] = int(tc5.number_input("文字サイズpx", 12, 24,
                                                   value=int(th.get("font_size", 16)), key=K("thfs")))
        else:
            spec.pop("theme", None)

        st.markdown("###### 🚪 部屋（rooms・固定URLで参加者を受け入れる）")
        st.caption("部屋を作ると `http://サーバ/room/部屋名` という**固定URL**ができる．"
                   "実験室PCのブックマークや事前配布リンクに使え，セッションを作り直しても"
                   "URLは変わらない．参加者ラベルを入れると出席確認と席の対応づけができる")
        for ri, room in enumerate(list(spec.get("rooms", []))):
            with st.expander(f"🚪 {room.get('name') or '（部屋名未設定）'}",
                             expanded=not room.get("name")):
                rc1, rc2, rc3 = st.columns([2, 2, 1])
                room["name"] = rc1.text_input(
                    "部屋名（URLの一部になる英数字）", value=room.get("name", ""),
                    key=K(f"room_name_{ri}"), placeholder="econlab")
                room["display_name"] = rc2.text_input(
                    "表示名（管理画面用）", value=room.get("display_name", ""),
                    key=K(f"room_disp_{ri}"), placeholder="経済実験室")
                if rc3.button("✕ 削除", key=K(f"room_del_{ri}")):
                    spec["rooms"].pop(ri)
                    load_spec(spec, keep_app=True, keep_test=True); st.rerun()
                lab_txt = st.text_area(
                    "参加者ラベル（1行に1人．空なら誰でも入室できる部屋になる）",
                    value="\n".join(room.get("participant_labels") or []),
                    key=K(f"room_lab_{ri}"), height=80,
                    placeholder="PC01\nPC02\nPC03",
                    help="実験室の座席番号や学籍IDなど．参加者は "
                         "/room/部屋名?participant_label=PC01 のURLで入室し，"
                         "データにラベルが記録される")
                labels = [s.strip() for s in lab_txt.splitlines() if s.strip()]
                if labels:
                    room["participant_labels"] = labels
                else:
                    room.pop("participant_labels", None)
                sec = st.checkbox(
                    "セキュアURL（ラベルごとに推測不能なURLを発行する）",
                    value=bool(room.get("use_secure_urls")),
                    disabled=not labels, key=K(f"room_sec_{ri}"),
                    help="参加者ラベルの設定が必要．URLを知らない人の成りすまし入室を防ぐ"
                         "（オンライン実験向き）")
                if sec and labels:
                    room["use_secure_urls"] = True
                else:
                    room.pop("use_secure_urls", None)
        if st.button("＋ 部屋を追加"):
            rooms_list = spec.setdefault("rooms", [])
            existing = {r.get("name") for r in rooms_list}
            n = len(rooms_list) + 1
            while f"room{n}" in existing:
                n += 1
            rooms_list.append({"name": f"room{n}", "display_name": ""})
            load_spec(spec, keep_app=True, keep_test=True); st.rerun()
        if not spec.get("rooms"):
            spec.pop("rooms", None)

    # ---- モデル ----
    with tab_model:
        st.caption("formページで使うフィールドには test_value を必ず入れる——botテストの自動導出に必要である．"
                   "min/max には数値のほか式（C.ENDOWMENT 等）も書ける．"
                   "choices を入れると選択式になる（既定はドロップダウン，入力形式でラジオに変更可）．")
        for level, label in [("group", "Group（組レベル）"), ("player", "Player（個人レベル）"),
                             ("subsession", "Subsession（ラウンドレベル）")]:
            st.markdown(f"###### {label}")
            orig = {f["name"]: f for f in app["models"].get(level, [])}
            fdf = pd.DataFrame(
                [{"name": f.get("name", ""), "type": f.get("type", "int"),
                  "label": f.get("label", ""), "min": "" if f.get("min") is None else str(f.get("min")),
                  "max": "" if f.get("max") is None else str(f.get("max")),
                  "choices": (json.dumps(f["choices"], ensure_ascii=False)
                              if f.get("choices") else ""),
                  "widget": WIDGET_LABELS.get(f.get("widget", ""), WIDGET_LABELS[""]),
                  "test_value": "" if f.get("test_value") is None else str(f.get("test_value"))}
                 for f in app["models"].get(level, [])],
                columns=["name", "type", "label", "min", "max", "choices", "widget", "test_value"])
            fed = st.data_editor(
                fdf, num_rows="dynamic", key=K(f"fields_{level}"), width="stretch",
                column_config={
                    "name": st.column_config.TextColumn("フィールド名"),
                    "type": st.column_config.SelectboxColumn("型", options=list(forge.FIELD_TYPES)),
                    "label": st.column_config.TextColumn("ラベル（質問文）", width="large"),
                    "min": st.column_config.TextColumn("min"),
                    "max": st.column_config.TextColumn("max"),
                    "choices": st.column_config.TextColumn(
                        "choices（選択肢）", width="medium",
                        help='例：1,2,3,4,5 ／ [[1,"低"],[3,"中"],[5,"高"]]（値とラベルの組はJSONで）'),
                    "widget": st.column_config.SelectboxColumn(
                        "入力形式", options=list(WIDGET_FROM_LABEL), width="medium"),
                    "test_value": st.column_config.TextColumn("test_value"),
                })
            out = []
            for _, r in fed.iterrows():
                nm = str(r["name"]).strip()
                if not nm or nm == "nan":
                    continue
                f = dict(orig.get(nm, {}))  # docなど未表示キーを保存
                f["name"], f["type"] = nm, (r["type"] or "int")
                lab = str(r["label"]).strip()
                if lab and lab != "nan":
                    f["label"] = lab
                else:
                    f.pop("label", None)
                for kk in ("min", "max", "test_value"):
                    # test_value はフィールド型に従う（str型なら "1" を数値化しない）
                    v = coerce_typed(r[kk], f["type"]) if kk == "test_value" else coerce(r[kk])
                    if v is None:
                        f.pop(kk, None)
                    else:
                        f[kk] = v
                # choices：JSON（ラベル付き）またはカンマ区切り（値のみ）を受け付ける
                ch = str(r["choices"]).strip()
                if ch and ch != "nan":
                    try:
                        f["choices"] = json.loads(ch)
                    except json.JSONDecodeError:
                        parts = [x.strip() for x in ch.split(",") if x.strip()]
                        # str型の選択肢は文字列のまま保持する
                        f["choices"] = (parts if f["type"] in ("str", "longstr")
                                        else [coerce(x) for x in parts])
                else:
                    f.pop("choices", None)
                w = WIDGET_FROM_LABEL.get(str(r["widget"]), "")
                if w:
                    f["widget"] = w
                else:
                    f.pop("widget", None)
                out.append(f)
            app["models"][level] = out

    # ---- ロジック ----
    with tab_logic:
        st.caption("フック関数の本体をPythonで注入する（エスケープハッチL2）．"
                   "WaitPageの after_all_players_arrive から名前で参照される．")
        for li, fn in enumerate(app.get("logic", [])):
            with st.expander(f"⚙ {fn['name']}（{fn['level']}）", expanded=True):
                c1, c2, c3 = st.columns([3, 2, 1])
                fn["name"] = c1.text_input("関数名", value=fn["name"], key=K(f"lg_name_{li}"))
                fn["level"] = c2.selectbox("レベル", ["group", "player", "subsession"],
                                           index=["group", "player", "subsession"].index(fn["level"]),
                                           key=K(f"lg_lvl_{li}"))
                if c3.button("削除", key=K(f"lg_del_{li}")):
                    app["logic"].pop(li)
                    load_spec(spec, keep_app=True, keep_test=True)
                    st.rerun()
                fn["body"] = st.text_area("関数本体（Python）", value=fn["body"],
                                          key=K(f"lg_body_{li}"), height=140)
                sc1, sc2 = st.columns([3, 1])
                snip = sc1.selectbox("定番パターン", range(len(SNIPPETS)),
                                     format_func=lambda x: f"{SNIPPETS[x][0]}（{SNIPPETS[x][1]}）",
                                     key=K(f"lg_snip_{li}"))
                if sc2.button("挿入", key=K(f"lg_ins_{li}")):
                    _, lvl, body = SNIPPETS[snip]
                    fn["level"] = lvl
                    cur = fn["body"].strip()
                    fn["body"] = body if not cur or cur == "pass" else fn["body"].rstrip() + "\n\n" + body
                    load_spec(spec, keep_app=True, keep_test=True)
                    st.rerun()
        if st.button("＋ フック関数を追加"):
            app.setdefault("logic", []).append(
                {"name": f"hook_{len(app.get('logic', [])) + 1}", "level": "group", "body": "pass"})
            load_spec(spec, keep_app=True, keep_test=True)
            st.rerun()

    # ---- ページ ----
    with tab_pages:
        # フロー図：いま組み立てている実験の流れを俯瞰する
        st.markdown("###### 🗺 実験フロー（緑枠＝編集中のapp）")
        try:
            st.graphviz_chart(flow_dot(spec, st.session_state.app_idx),
                              width="stretch")
        except Exception as e:
            st.caption(f"フロー図を描画できない：{e}")

        # ブロックパレット：定型のページ一式（フィールド・ロジック込み）をワンクリックで追加する
        st.markdown("###### 🧱 ブロックパレット（クリックで末尾に追加）")
        bcols = st.columns(4)
        for bi, (blabel, bhelp, bfunc) in enumerate(BLOCKS):
            if bcols[bi % 4].button(blabel, help=bhelp, key=K(f"blk_{bi}"),
                                    width="stretch"):
                bfunc(app)
                load_spec(spec, keep_app=True, keep_test=True)
                st.rerun()
        st.divider()

        logic_names = [f["name"] for f in app.get("logic", [])]
        for pi, pg in enumerate(app["pages"]):
            icon = {"info": "📄", "form": "✏️", "wait": "⏳"}[pg["kind"]]
            with st.expander(f"{pi + 1}. {icon} {pg['name']}", expanded=False):
                c1, c2, c3, c4, c5 = st.columns([3, 2, 1, 1, 1])
                pg["name"] = c1.text_input("ページ名", value=pg["name"], key=K(f"pg_name_{pi}"))
                pg["kind"] = c2.selectbox("種別", ["info", "form", "wait"],
                                          index=["info", "form", "wait"].index(pg["kind"]),
                                          format_func={"info": "説明", "form": "入力", "wait": "待機"}.get,
                                          key=K(f"pg_kind_{pi}"))
                if c3.button("↑", key=K(f"pg_up_{pi}")) and pi > 0:
                    app["pages"][pi - 1], app["pages"][pi] = app["pages"][pi], app["pages"][pi - 1]
                    load_spec(spec, keep_app=True, keep_test=True); st.rerun()
                if c4.button("↓", key=K(f"pg_dn_{pi}")) and pi < len(app["pages"]) - 1:
                    app["pages"][pi + 1], app["pages"][pi] = app["pages"][pi], app["pages"][pi + 1]
                    load_spec(spec, keep_app=True, keep_test=True); st.rerun()
                if c5.button("✕", key=K(f"pg_del_{pi}")):
                    app["pages"].pop(pi)
                    load_spec(spec, keep_app=True, keep_test=True); st.rerun()

                if pg["kind"] == "wait":
                    hook = st.selectbox("全員到着時フック", ["（なし）"] + logic_names,
                                        index=(logic_names.index(pg.get("after_all_players_arrive")) + 1
                                               if pg.get("after_all_players_arrive") in logic_names else 0),
                                        key=K(f"pg_hook_{pi}"))
                    if hook == "（なし）":
                        pg.pop("after_all_players_arrive", None)
                    else:
                        pg["after_all_players_arrive"] = hook
                    pg["body"] = st.text_input("待機メッセージ", value=pg.get("body", ""), key=K(f"pg_wmsg_{pi}"))
                    gbat_ok = pi == 0 and app["constants"].get("players_per_group") is not None
                    gbat = st.checkbox(
                        "到着順にグループを作る（group_by_arrival_time）",
                        value=bool(pg.get("group_by_arrival_time")),
                        disabled=not gbat_ok, key=K(f"pg_gbat_{pi}"),
                        help="先に到着した人から順に players_per_group 人ずつ組にする．"
                             "オンライン実験で全員の同時開始を待たずに始められる．"
                             "appの最初のページである場合のみ使える（グループ人数の指定も必要）")
                    if gbat:
                        pg["group_by_arrival_time"] = True
                    else:
                        pg.pop("group_by_arrival_time", None)
                    if not gbat_ok and pg.get("group_by_arrival_time"):
                        pg.pop("group_by_arrival_time", None)
                    continue

                tc1, tc2 = st.columns([3, 1])
                pg["title"] = tc1.text_input("タイトル", value=pg.get("title", ""), key=K(f"pg_title_{pi}"))
                to = tc2.number_input("制限時間（秒，0=なし）", 0, 7200,
                                      value=int(pg.get("timeout_seconds") or 0),
                                      key=K(f"pg_to_{pi}"),
                                      help="超過すると自動で次ページへ進む（timeout_seconds）")
                if to:
                    pg["timeout_seconds"] = to
                else:
                    pg.pop("timeout_seconds", None)

                # 表示条件ビルダー
                cur_expr = pg.get("display_if", "") or ""
                default_choice = "全員に表示" if not cur_expr else COND_REV.get(cur_expr, "カスタム式")
                cc1, cc2 = st.columns([2, 3])
                choice = cc1.selectbox("表示条件", COND_CHOICES,
                                       index=COND_CHOICES.index(default_choice), key=K(f"pg_cond_{pi}"))
                if choice == "全員に表示":
                    pg.pop("display_if", None)
                elif choice == "カスタム式":
                    expr = cc2.text_input("Python式", value=cur_expr,
                                          placeholder="player.id_in_group == 1 and player.round_number > 1",
                                          key=K(f"pg_condx_{pi}"))
                    if expr.strip():
                        pg["display_if"] = expr.strip()
                    else:
                        pg.pop("display_if", None)
                else:
                    pg["display_if"] = COND_MAP[choice]
                    cc2.caption(f"式：`{pg['display_if']}`")

                if pg["kind"] == "form":
                    fm = st.radio("form_model", ["group", "player"], horizontal=True,
                                  index=["group", "player"].index(pg.get("form_model", "player")),
                                  key=K(f"pg_fm_{pi}"))
                    pg["form_model"] = fm
                    avail = [f["name"] for f in app["models"].get(fm, [])]
                    pg["form_fields"] = st.multiselect(
                        "フォーム欄（モデルで定義したものから選ぶ）", avail,
                        default=[x for x in pg.get("form_fields", []) if x in avail],
                        key=K(f"pg_ff_{pi}"))
                    if not avail:
                        st.warning(f"models.{fm} にフィールドがない——「モデル」タブで先に定義する")

                mode = st.radio(
                    "本文の作り方", ["🧩 セクションで組む", "</> HTML直書き"], horizontal=True,
                    index=0 if pg.get("content") is not None else 1,
                    key=K(f"pg_mode_{pi}"),
                    help="セクション：見出し・段落・画像・表・グラフ等を並べて組む（HTML不要）．"
                         "HTML直書き：本文を1つのHTMLとして自由に書く")
                if mode.startswith("🧩"):
                    pg.pop("raw_html", None)
                    if pg.get("content") is None:
                        # 既存の本文HTMLは1つのHTMLセクションとして引き継ぐ
                        pg["content"] = ([{"type": "html", "code": pg["body"]}]
                                         if pg.get("body") else [])
                        pg.pop("body", None)
                    edit_sections(pg, app, pi)
                else:
                    if pg.get("content") is not None:
                        # セクションをHTMLに変換して引き継ぐ（フォーム欄は自動挿入に戻る）
                        fdefs_cur = {f["name"]: f for f in
                                     app["models"].get(pg.get("form_model", "player"), [])}
                        pg["body"] = forge.sections_to_html(pg["content"], fdefs_cur)
                        pg.pop("content", None)
                    pg["body"] = st.text_area(
                        "本文HTML（{{ C.X }} などのoTree記法が使える）",
                        value=pg.get("body", ""), key=K(f"pg_body_{pi}"), height=100)
                    raw = st.checkbox(
                        "HTMLを完全手動にする（フォーム欄・次へボタンを自動挿入しない）",
                        value=bool(pg.get("raw_html")), key=K(f"pg_raw_{pi}"),
                        help="ONにすると本文がそのままページになる．{{ formfield 'x' }} や "
                             "{{ next_button }}，<script>等を自分で書ける（エスケープハッチL3相当）")
                    if raw:
                        pg["raw_html"] = True
                    else:
                        pg.pop("raw_html", None)
                # グラフセクションがデータ源から自動生成している間は手書き欄をロックする
                auto_jv = any(s.get("type") == "chart" and s.get("source", "custom") != "custom"
                              for s in (pg.get("content") or []))
                has_jv = st.checkbox(
                    "JavaScriptへデータを渡す（js_vars）——グラフ描画などに使う",
                    value=bool(pg.get("js_vars")) or auto_jv, disabled=auto_jv,
                    key=K(f"pg_jv_{pi}"),
                    help="return dict(...) で終えるPythonコードを書くと，本文の<script>内で "
                         "js_vars.キー名 としてアクセスできる（Chart.js等でグラフ化）")
                if auto_jv:
                    st.code(pg.get("js_vars", ""), language="python")
                    st.caption("グラフの「データ源」から自動生成中——"
                               "手で編集するにはデータ源を「カスタム」にする")
                elif has_jv:
                    pg["js_vars"] = st.text_area(
                        "js_vars 本体（player を受け取り dict を返す）",
                        value=pg.get("js_vars") or "return dict()",
                        key=K(f"pg_jvb_{pi}"), height=90)
                    jp1, jp2 = st.columns([3, 1])
                    pat = jp1.selectbox("定番パターン", list(JS_VARS_PATTERNS),
                                        key=K(f"pg_jvp_{pi}"), label_visibility="collapsed")
                    if jp2.button("で置き換え", key=K(f"pg_jvi_{pi}"), width="stretch"):
                        pg["js_vars"] = JS_VARS_PATTERNS[pat]
                        load_spec(spec, keep_app=True, keep_test=True); st.rerun()
                else:
                    pg.pop("js_vars", None)

                # ライブページ（リアルタイム通信）
                has_live = st.checkbox(
                    "⚡ リアルタイム通信を使う（ライブページ）——チャット・市場・投票集計など",
                    value=bool(pg.get("live_method")), key=K(f"pg_lv_{pi}"),
                    help="ページを離れずにサーバと通信する．本文側の liveSend(データ) が "
                         "live_method に届き，返り値が liveRecv(data) に配られる")
                if has_live:
                    pg["live_method"] = st.text_area(
                        "live_method 本体（player と data を受け取り {宛先id: データ} を返す．0=グループ全員）",
                        value=pg.get("live_method") or DEFAULT_LIVE_METHOD,
                        key=K(f"pg_lvb_{pi}"), height=110)
                    st.caption("本文には送信ボタン等の `liveSend({...})` と，受信関数 "
                               "`function liveRecv(data) {...}` の `<script>` を書く——"
                               "HTMLセクションの定番スニペット「ライブ通信の送受信」が雛形になる")
                    lt = st.text_input(
                        "botテスト時のliveSend模擬（任意）",
                        value=pg.get("live_test", ""), key=K(f"pg_lvt_{pi}"),
                        placeholder="method(1, {})   # プレイヤー1がliveSendした扱いで実行",
                        help="空ならbotテストでlive_methodは呼ばれない（ページ送りのみ検証）")
                    if lt.strip():
                        pg["live_test"] = lt.strip()
                    else:
                        pg.pop("live_test", None)
                else:
                    pg.pop("live_method", None)
                    pg.pop("live_test", None)
                vars_chips = ([f"C.{e['name']}" for e in app["constants"].get("extra", [])]
                              + ["player.round_number", "player.payoff"]
                              + [f"group.{f['name']}" for f in app["models"].get("group", [])]
                              + [f"player.{f['name']}" for f in app["models"].get("player", [])])
                st.caption("使える変数：" + "　".join(f"`{{{{ {v} }}}}`" for v in vars_chips))

        if st.button("＋ ページを追加"):
            app["pages"].append({"name": f"Page{len(app['pages']) + 1}", "kind": "info",
                                 "title": "", "body": ""})
            load_spec(spec, keep_app=True, keep_test=True); st.rerun()

# ============================ 右：検証・生成 ============================
with right:
    # 検証
    try:
        forge.validate(spec)
        issues = []
    except forge.SpecError as e:
        issues = str(e).split("\n")[1:]
    # botテストはセッション全体を通しでプレイするため，全appで導出可能であること
    bots_missing = [a["name"] for a in spec["apps"] if forge.emit_bot(a) is None]
    bot_ok = not bots_missing

    s1, s2 = st.columns(2)
    s1.metric("仕様の検証", "OK ✓" if not issues else f"警告 {len(issues)} 件")
    s2.metric("bot自動導出", "可 ●" if bot_ok else "不可 ○")
    for msg in issues:
        st.warning(msg.lstrip(" -"))
    # 静的診断：生成はできるが実セッションで問題になりやすい構成（デッドロックの芽等）
    if not issues:
        hints = forge.lint(spec)
        if hints:
            with st.expander(f"💡 設計上の注意 {len(hints)} 件（生成は可能）", expanded=False):
                for h in hints:
                    st.info(h)
    if not bot_ok:
        st.caption("formで使うフィールドの test_value が不足している"
                   f"（対象app：{', '.join(bots_missing)}．モデルタブで入力）")

    # 生成プレビュー
    pv0, pv1, pv2 = st.tabs(["👁 参加者画面プレビュー",
                             f"{app['name']}/__init__.py（生成プレビュー）", "ESL仕様（JSON）"])
    with pv0:
        if app["pages"]:
            pnames = [p["name"] for p in app["pages"]]
            sel = st.selectbox("プレビューするページ", pnames, key=K("pv_page"),
                               label_visibility="collapsed")
            pg_sel = app["pages"][pnames.index(sel)]
            try:
                html_doc = render_preview_html(pg_sel, app, spec.get("theme"))
                # st.components.v1.html は廃止予定のため st.iframe（data URL）を使う
                src = ("data:text/html;base64,"
                       + base64.b64encode(html_doc.encode("utf-8")).decode("ascii"))
                st.iframe(src, height=560)
            except Exception as e:
                st.error(f"プレビューを描画できない：{e}")
            st.caption("サンプル値（test_value・定数）で描画した近似プレビューである．"
                       "js_varsを使うグラフはデータが空で表示される．正確な画面はデモプレイで確認する．")
        else:
            st.info("ページがまだない——左の「ページ」タブで追加する")
    with pv1:
        try:
            st.code(forge.emit_app_init(app), language="python")
        except Exception as e:
            st.error(f"生成エラー：{e}")
    with pv2:
        st.code(json.dumps(spec, ensure_ascii=False, indent=2), language="json")

    # 書き出し
    def project_zip_bytes():
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "proj"
            forge.build(spec, out)
            (out / "esl_spec.json").write_text(
                json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in sorted(out.rglob("*")):
                    if p.is_file():
                        z.write(p, p.relative_to(out))
            return buf.getvalue()

    d1, d2 = st.columns(2)
    d1.download_button("仕様JSONを保存", json.dumps(spec, ensure_ascii=False, indent=2),
                       file_name=f"esl_spec_{spec['meta']['name']}.json", mime="application/json",
                       width="stretch")
    # data に関数を渡すと，クリック時のみzipを生成する（毎rerunのビルドを避ける）
    d2.download_button("oTreeプロジェクト（zip）",
                       (b"" if issues else project_zip_bytes),
                       file_name=f"{spec['meta']['name']}_otree.zip", mime="application/zip",
                       disabled=bool(issues), width="stretch")

    # otree test 実行（Streamlit版の独自機能）
    # 依存衝突を避けるため，oTreeは otree_runner が管理する専用venvで実行する
    st.divider()
    # botのSubmit行：/p/<参加者コード>/<app>/<ページ>/<番号>, {送信データ}
    SUBMIT_RE = re.compile(r"Submit /p/(\w+)/\w+/(\w+)/\d+(?:,\s*(\{.*\}))?")

    if otree_runner.env_ready():
        if st.button("🧪 otree test を実行（botが実験を自動プレイして検証）",
                     disabled=bool(issues) or not bot_ok, width="stretch"):
            with st.spinner("生成 → botがプレイ中…（30秒前後）"):
                with tempfile.TemporaryDirectory() as td:
                    out = Path(td) / "proj"
                    forge.build(spec, out)
                    export = Path(td) / "export"
                    ok, log = otree_runner.run_test(
                        out, spec["session"]["config_name"], export_dir=export)
                    csv_path = export / f"{app['name']}.csv"
                    df = pd.read_csv(csv_path) if csv_path.exists() else None
            # エクスポートCSVにはラウンド列がないため（oTree 6で確認），
            # 「1行＝1人×1ラウンド・ラウンド順」の並びを利用して出現順から復元する
            if df is not None and "subsession.round_number" not in df.columns:
                df["subsession.round_number"] = (
                    df.groupby("participant.id_in_session").cumcount() + 1)
            # 結果はsession_stateに保存する（他のウィジェット操作のrerunで消えないように）
            st.session_state.test_result = {
                "ok": ok, "log": log, "df": df,
                "app_name": app["name"], "at": time.strftime("%H:%M:%S")}

        res = st.session_state.get("test_result")
        if res:
            ok, log, df = res["ok"], res["log"], res["df"]
            if ok:
                st.success("botテスト合格——この実験は最後まで通しでプレイ可能である")
            else:
                st.error("botテスト失敗——ログを確認して仕様を修正する")
            st.caption(f"実行 {res['at']}・対象app {res['app_name']}"
                       "（仕様を変更した場合は再実行する）")
            res_app = next((a for a in spec["apps"] if a["name"] == res["app_name"]), app)

            # botの行動ログ：どの参加者がどのページで何を送信したか
            pid = {}
            if df is not None:
                pid = dict(zip(df["participant.code"], df["participant.id_in_session"]))
            actions = []
            for line in log:
                m = SUBMIT_RE.search(line)
                if m:
                    code, page, data = m.groups()
                    actions.append({"参加者": f"P{pid.get(code, '?')}",
                                    "ページ": page,
                                    "送信した値": data or "（次へボタンのみ）"})
            if actions:
                st.markdown("###### botの行動（ページ遷移と送信値，実行順）")
                st.dataframe(pd.DataFrame(actions), width="stretch", hide_index=True)

            # botがプレイした結果データ：入力値と計算された利得を確認できる
            if df is not None:
                cols = (["participant.id_in_session", "subsession.round_number",
                         "player.id_in_group"]
                        + [f"player.{f['name']}" for f in res_app["models"].get("player", [])]
                        + [f"group.{f['name']}" for f in res_app["models"].get("group", [])]
                        + ["player.payoff"])
                cols = [c for c in cols if c in df.columns]
                sort_keys = [c for c in ("subsession.round_number", "participant.id_in_session")
                             if c in df.columns]
                view = df[cols].sort_values(sort_keys) if sort_keys else df[cols]
                st.markdown("###### botがプレイした結果データ（1行＝1人×1ラウンド）")
                st.dataframe(view, width="stretch", hide_index=True)
                st.caption("利得計算ロジックが意図通りかをここで確認する"
                           "（例：払い戻し＝拠出合計×倍率÷人数）")

            with st.expander("実行ログ全文"):
                st.code("\n".join(log), language="text")

        # デモプレイ：実際の参加者画面をブラウザで開いて動作を見る
        st.divider()
        st.markdown("###### 👀 デモプレイ（実際の参加者画面で動作を見る）")

        def start_demo():
            """現在の仕様でプロジェクトを生成しdevserverを起動する．成否を返す"""
            demo_dir = Path(tempfile.mkdtemp(prefix="forge_demo_"))
            forge.build(spec, demo_dir)
            proc = otree_runner.start_devserver(demo_dir, DEMO_PORT)
            time.sleep(2.5)  # 起動失敗（ポート衝突等）を検知するため少し待つ
            if proc.poll() is not None:
                st.error("デモサーバの起動に失敗した")
                st.code((demo_dir / "devserver.log").read_text()[-1500:], language="text")
                return False
            st.session_state.demo_proc = proc
            return True

        demo_proc = st.session_state.get("demo_proc")
        demo_running = demo_proc is not None and demo_proc.poll() is None
        if demo_running:
            st.success(f"デモサーバ稼働中 → [デモページを開く](http://localhost:{DEMO_PORT}/demo)")
            for r in spec.get("rooms") or []:
                if r.get("name"):
                    st.caption(f"🚪 部屋 '{r['name']}' → "
                               f"http://localhost:{DEMO_PORT}/room/{r['name']} "
                               "（管理画面の Rooms からセッションを割り当てて使う）")
            st.caption("デモページで実験を選ぶと参加者リンクが発行される．"
                       "人数分のタブ（またはウィンドウ）で開いて，画面を見ながらプレイする．")
            c1, c2 = st.columns(2)
            if c1.button("現在の仕様で再起動（変更を反映）",
                         disabled=bool(issues), width="stretch"):
                otree_runner.stop_devserver(demo_proc)
                time.sleep(1)  # ポート解放を待つ
                if start_demo():
                    st.rerun()
            if c2.button("デモサーバを停止", width="stretch"):
                otree_runner.stop_devserver(demo_proc)
                st.session_state.demo_proc = None
                st.rerun()
        else:
            if st.button("▶ デモサーバを起動（現在の仕様で実際にプレイできる）",
                         disabled=bool(issues), width="stretch"):
                if start_demo():
                    st.rerun()
            st.caption(f"起動後 http://localhost:{DEMO_PORT}/demo で参加者画面を開ける"
                       "（ローカル・研究室サーバ向け．Streamlit Community Cloudでは外部から届かない）")
    else:
        st.caption("botテストにはoTree実行環境が必要である（生成・書き出しには不要）．"
                   "下のボタンで専用venvに自動インストールする．")
        if st.button("⬇ oTree実行環境をセットアップ（初回のみ・数分かかる）",
                     width="stretch"):
            with st.spinner("専用venvを作成しoTreeをインストール中…"):
                try:
                    otree_runner.setup_env()
                    st.success("セットアップ完了——botテストが使えるようになった")
                    st.rerun()
                except Exception as e:
                    st.error(f"セットアップ失敗：{e}")
