#!/usr/bin/env python3
"""forge.py — ESL (Experiment Specification Language) v0.1 -> oTree project generator.

Usage:
    python3 forge.py validate <spec.json>
    python3 forge.py build <spec.json> -o <output_dir>

設計方針（v0.1）：
  * 生成は一方向（ESL -> コード）．逆パースはしない．
  * 柔軟性は3段階のエスケープハッチで担保する：
      L1: 式文字列  — min/max/display_if 等に Python 式をそのまま書ける
      L2: コード片  — logic[] にフック関数の本体を文字列で注入する
      L3: 生成後編集 — 生成物は素の oTree プロジェクトなので直接編集できる
  * 仕様から PlayerBot（自動テスト）も導出する．test_value が全フォーム欄に
    あればテスト可能性が仕様レベルで保証される．
"""

import argparse
import html
import json
import keyword
import re
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

FIELD_TYPES = {
    "currency": "CurrencyField",
    "int": "IntegerField",
    "float": "FloatField",
    "bool": "BooleanField",
    "str": "StringField",
    "longstr": "LongStringField",
}

LOGIC_LEVELS = {
    "group": "group: Group",
    "player": "player: Player",
    "subsession": "subsession: Subsession",
}

PAGE_KINDS = {"info", "form", "wait"}


# ---------------------------------------------------------------- validation

class SpecError(Exception):
    pass


# oTreeが組み込みで持つ属性名．フィールド名に使うと生成物が壊れる
RESERVED_FIELDS = {
    "payoff", "round_number", "id_in_group", "id_in_subsession", "role",
    "participant", "session", "group", "subsession", "player",
}

# oTree 6が公式にサポートする入力widget
OTREE_WIDGETS = {"RadioSelect", "RadioSelectHorizontal", "CheckboxInput"}
# forgeがテンプレートHTMLとして生成する独自入力形式
CUSTOM_WIDGETS = {
    "Slider",          # スライダー（つまみあり，値をライブ表示）
    "SliderNoAnchor",  # スライダー（初期つまみ非表示．アンカリング回避．クリックするまで未回答）
    "ButtonSelect",    # 選択肢ボタン（押すと即送信して次ページへ）
    "StarRating",      # 星評価（クリックで選択）
    "LikertMatrix",    # リッカート表（同じchoicesの連続フィールドを1つの表にまとめる）
    "NumberStepper",   # 数値ステッパー（−/＋ボタンつき数値欄）
    "MultiCheckbox",   # 複数選択チェックボックス（str型にカンマ区切りで保存）
}
VALID_WIDGETS = OTREE_WIDGETS | CUSTOM_WIDGETS


def _ident(name, where):
    if not name.isidentifier() or keyword.iskeyword(name):
        raise SpecError(f"{where}: '{name}' はPython識別子として不正である")


def _check_expr(expr, where, err):
    """L1エスケープハッチ（式文字列）の構文を事前検査する"""
    try:
        compile(str(expr), "<expr>", "eval")
    except SyntaxError as e:
        err(f"{where}: 式 '{expr}' に構文エラーがある（{e.msg}）")


def _check_code(body, where, err):
    """L2エスケープハッチ（コード片）の構文を事前検査する．

    生成時は関数本体として埋め込まれるため，return を含むコードも妥当である．
    そこで関数で包んでからコンパイルする（行番号は1行ずれる分を補正する）．
    """
    src = "def _f():\n" + textwrap.indent(body or "pass", "    ")
    try:
        compile(src, "<code>", "exec")
    except SyntaxError as e:
        err(f"{where}: コード{max(1, (e.lineno or 1) - 1)}行目に構文エラーがある（{e.msg}）")


def _num(v):
    """数値リテラルのみ返す（boolと式文字列はNone）"""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def validate(spec):
    """意味論レベルの検証．JSON Schema的な構文検証はv0では簡略化．"""
    errors = []

    def err(msg):
        errors.append(msg)

    if spec.get("esl_version") != "0.1":
        err("esl_version は '0.1' のみ対応")

    app_names = []
    for app in spec.get("apps", []):
        aname = app.get("name", "?")
        app_names.append(aname)
        try:
            _ident(aname, "app.name")
        except SpecError as e:
            err(str(e))

        # 定数
        consts = app.get("constants", {})
        nr = consts.get("num_rounds", 1)
        if not isinstance(nr, int) or nr < 1:
            err(f"{aname}: num_rounds は1以上の整数とする")
        # oTreeの制約：PLAYERS_PER_GROUP は None（グループなし）か2以上の整数
        ppg = consts.get("players_per_group")
        if ppg is not None and (not isinstance(ppg, int) or ppg < 2):
            err(f"{aname}: players_per_group は2以上の整数かNone（グループなし）とする")
        seen_consts = set()
        for e_ in consts.get("extra", []):
            cname = e_.get("name", "")
            try:
                _ident(cname, f"{aname}.constants")
            except SpecError as e:
                err(str(e))
            if cname in seen_consts:
                err(f"{aname}: 定数 '{cname}' が重複")
            seen_consts.add(cname)
            if e_.get("type") not in FIELD_TYPES:
                err(f"{aname}.constants.{cname}: 不明な型 '{e_.get('type')}'")
            if e_.get("type") not in ("str", "longstr") and is_expr(e_.get("value")):
                _check_expr(e_["value"], f"{aname}.constants.{cname}", err)

        # モデル
        models = app.get("models", {})
        model_fields = {}
        for level in ("subsession", "group", "player"):
            seen = set()
            for f in models.get(level, []):
                fname = f.get("name", "")
                where = f"{aname}.models.{level}.{fname}"
                try:
                    _ident(fname, f"{aname}.models.{level}")
                except SpecError as e:
                    err(str(e))
                if fname in seen:
                    err(f"{aname}.models.{level}: フィールド名 '{fname}' が重複")
                seen.add(fname)
                if fname in RESERVED_FIELDS:
                    err(f"{where}: '{fname}' はoTreeの予約語であり使用できない")
                if f.get("type") not in FIELD_TYPES:
                    err(f"{where}: 不明な型 '{f.get('type')}'")
                # str/longstr型の値は文字列リテラルなので式チェックの対象外
                if f.get("type") not in ("str", "longstr"):
                    for key in ("min", "max", "initial", "test_value"):
                        if is_expr(f.get(key)):
                            _check_expr(f[key], f"{where}.{key}", err)
                # test_value が数値リテラルなら min/max の範囲内かを検査する
                mn, mx, tv = _num(f.get("min")), _num(f.get("max")), _num(f.get("test_value"))
                if tv is not None:
                    if mn is not None and tv < mn:
                        err(f"{where}: test_value {tv} が min {mn} 未満")
                    if mx is not None and tv > mx:
                        err(f"{where}: test_value {tv} が max {mx} 超過")
                if mn is not None and mx is not None and mn > mx:
                    err(f"{where}: min {mn} が max {mx} を超えている")
                # 入力形式（choices / widget）の整合性
                w = f.get("widget")
                if w and w not in VALID_WIDGETS:
                    err(f"{where}: widget は {sorted(VALID_WIDGETS)} のいずれか")
                if w == "CheckboxInput" and f.get("type") != "bool":
                    err(f"{where}: CheckboxInput は bool 型のみに使える")
                ch = f.get("choices")
                if w in ("RadioSelect", "RadioSelectHorizontal") \
                        and f.get("type") != "bool" and ch is None:
                    err(f"{where}: ラジオ表示には choices が必要（bool型を除く）")
                if w in ("Slider", "SliderNoAnchor"):
                    if f.get("type") not in ("int", "float", "currency"):
                        err(f"{where}: {w} は数値型（int/float/currency）のみに使える")
                    if f.get("min") is None or f.get("max") is None:
                        err(f"{where}: {w} には min と max が必須")
                    st_ = f.get("step")
                    if st_ is not None and (_num(st_) is None or _num(st_) <= 0):
                        err(f"{where}: step は正の数値とする")
                if w == "ButtonSelect" and ch is None:
                    err(f"{where}: ButtonSelect には choices が必要")
                if w == "StarRating":
                    if f.get("type") != "int":
                        err(f"{where}: StarRating は int 型のみに使える")
                    if _num(f.get("max")) is None:
                        err(f"{where}: StarRating には max（星の数）を数値で指定する")
                if w == "LikertMatrix" and ch is None:
                    err(f"{where}: LikertMatrix には choices が必要")
                if w == "NumberStepper" and f.get("type") not in ("int", "float", "currency"):
                    err(f"{where}: NumberStepper は数値型のみに使える")
                if w == "MultiCheckbox":
                    if f.get("type") != "str":
                        err(f"{where}: MultiCheckbox は str 型のみに使える（カンマ区切りで保存）")
                    if ch is None:
                        err(f"{where}: MultiCheckbox には choices が必要")
                if ch is not None:
                    if not isinstance(ch, list) or not ch:
                        err(f"{where}: choices は空でないリストとする")
                    else:
                        values = [c[0] if isinstance(c, list) else c for c in ch]
                        tv2 = f.get("test_value")
                        # MultiCheckboxはカンマ区切りの複合値なので包含チェックの対象外
                        if tv2 is not None and w != "MultiCheckbox" and tv2 not in values \
                                and not (is_expr(tv2) and f.get("type") not in ("str", "longstr")):
                            err(f"{where}: test_value {tv2!r} が choices に含まれない")
                model_fields.setdefault(level, set()).add(fname)

        # ロジック
        logic_names = set()
        for fn in app.get("logic", []):
            lname = fn.get("name", "")
            try:
                _ident(lname, f"{aname}.logic")
            except SpecError as e:
                err(str(e))
            if lname in logic_names:
                err(f"{aname}.logic: 関数名 '{lname}' が重複")
            logic_names.add(lname)
            if fn.get("level") not in LOGIC_LEVELS:
                err(f"{aname}.logic.{lname}: level は {sorted(LOGIC_LEVELS)} のいずれか")
            _check_code(fn.get("body", ""), f"{aname}.logic.{lname}", err)

        # ページ
        page_names = set()
        pages = app.get("pages", [])
        if not pages:
            err(f"{aname}: ページが1つもない")
        for pg in pages:
            pname = pg.get("name", "?")
            try:
                _ident(pname, f"{aname}.pages")
            except SpecError as e:
                err(str(e))
            if pname in page_names:
                err(f"{aname}: ページ名 '{pname}' が重複")
            page_names.add(pname)

            kind = pg.get("kind")
            if kind not in PAGE_KINDS:
                err(f"{aname}.{pname}: kind は {sorted(PAGE_KINDS)} のいずれか")
            if pg.get("display_if"):
                _check_expr(pg["display_if"], f"{aname}.{pname}.display_if", err)
            if pg.get("vars_for_template"):
                _check_code(pg["vars_for_template"], f"{aname}.{pname}.vars_for_template", err)
            if pg.get("js_vars"):
                _check_code(pg["js_vars"], f"{aname}.{pname}.js_vars", err)
            # ライブページ（リアルタイム通信）
            if pg.get("live_method"):
                if kind == "wait":
                    err(f"{aname}.{pname}: live_method は待機ページには置けない")
                _check_code(pg["live_method"], f"{aname}.{pname}.live_method", err)
            if pg.get("live_test"):
                if not pg.get("live_method"):
                    err(f"{aname}.{pname}: live_test には live_method が必要")
                _check_code(pg["live_test"], f"{aname}.{pname}.live_test", err)
            # content（セクション配列）の検査
            content = pg.get("content")
            if content is not None:
                if not isinstance(content, list):
                    err(f"{aname}.{pname}: content はセクションのリストとする")
                    content = []
                for si, sec in enumerate(content):
                    if not isinstance(sec, dict) or sec.get("type") not in SECTION_TYPES:
                        err(f"{aname}.{pname}.content[{si}]: type は "
                            f"{list(SECTION_TYPES)} のいずれか")
                        continue
                    if sec["type"] == "field":
                        if kind != "form":
                            err(f"{aname}.{pname}.content[{si}]: "
                                "フォーム欄セクションはformページでのみ使える")
                        elif sec.get("name") not in pg.get("form_fields", []):
                            err(f"{aname}.{pname}.content[{si}]: 欄 '{sec.get('name')}' が "
                                "form_fields に含まれていない")
                    if sec["type"] == "chart" and not pg.get("js_vars"):
                        err(f"{aname}.{pname}.content[{si}]: グラフには js_vars "
                            "（labels/valuesを返す）が必要")
            if kind == "form":
                fm = pg.get("form_model")
                if fm not in ("group", "player"):
                    err(f"{aname}.{pname}: form_model は 'group' か 'player'")
                else:
                    if not pg.get("form_fields"):
                        err(f"{aname}.{pname}: form欄を1つ以上指定する")
                    declared = model_fields.get(fm, set())
                    for ff in pg.get("form_fields", []):
                        if ff not in declared:
                            err(f"{aname}.{pname}: form欄 '{ff}' が models.{fm} に未定義")
            if kind == "wait":
                hook = pg.get("after_all_players_arrive")
                if hook and hook not in logic_names:
                    err(f"{aname}.{pname}: after_all_players_arrive '{hook}' が logic に未定義")
                if pg.get("group_by_arrival_time"):
                    # oTreeの制約：到着順グループ化はappの最初のページでのみ使える
                    if pages.index(pg) != 0:
                        err(f"{aname}.{pname}: group_by_arrival_time は "
                            "appの最初のページ（先頭の待機ページ）でのみ使える")
                    if app.get("constants", {}).get("players_per_group") is None:
                        err(f"{aname}.{pname}: group_by_arrival_time には "
                            "players_per_group の指定が必要")

    # セッション設定
    sess = spec.get("session", {})
    if not sess.get("config_name"):
        err("session.config_name が未設定")
    if not sess.get("app_sequence"):
        err("session.app_sequence が空")
    ndemo = sess.get("num_demo_participants", 2)
    app_ppg = {a.get("name"): a.get("constants", {}).get("players_per_group")
               for a in spec.get("apps", [])}
    for a in sess.get("app_sequence", []) or []:
        if a not in app_names:
            err(f"session.app_sequence の '{a}' に対応するappが定義されていない")
            continue
        # デモ参加者数がグループ人数で割り切れないとセッション作成に失敗する
        ppg_a = app_ppg.get(a)
        if isinstance(ppg_a, int) and isinstance(ndemo, int) and ndemo % ppg_a != 0:
            err(f"session: num_demo_participants {ndemo} が "
                f"{a} の players_per_group {ppg_a} で割り切れない")

    # 部屋（任意）：固定URLで参加者を受け入れるoTreeのrooms設定
    rooms = spec.get("rooms") or []
    if not isinstance(rooms, list):
        err("rooms はリストとする")
        rooms = []
    room_names = set()
    for i, r in enumerate(rooms):
        if not isinstance(r, dict):
            err(f"rooms[{i}]: オブジェクトとする")
            continue
        rname = r.get("name", "")
        if not re.fullmatch(r"[A-Za-z0-9_]+", str(rname)):
            err(f"rooms[{i}]: name '{rname}' は英数字とアンダースコアのみとする")
        if rname in room_names:
            err(f"rooms[{i}]: name '{rname}' が重複")
        room_names.add(rname)
        labels = r.get("participant_labels")
        if labels is not None:
            if (not isinstance(labels, list) or not labels
                    or any(not str(x).strip() for x in labels)):
                err(f"rooms[{i}] '{rname}': participant_labels は空でない文字列のリストとする")
            elif len(labels) != len(set(map(str, labels))):
                err(f"rooms[{i}] '{rname}': participant_labels に重複がある")
        if r.get("use_secure_urls") and not labels:
            err(f"rooms[{i}] '{rname}': use_secure_urls には participant_labels が必要"
                "（oTreeの制約：participant_label_file なしでは使えない）")

    # テーマ（任意）：参加者画面のフォント・配色
    theme = spec.get("theme")
    if theme:
        for key in ("base_color", "bg_color", "text_color"):
            v = theme.get(key)
            if v is not None and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(v)):
                err(f"theme.{key}: '{v}' は #RRGGBB 形式の色とする")
        fs = theme.get("font_size")
        if fs is not None and (not isinstance(fs, int) or not 8 <= fs <= 40):
            err("theme.font_size は8〜40の整数とする")

    if errors:
        raise SpecError("仕様検証エラー:\n  - " + "\n  - ".join(errors))


def lint(spec):
    """致命的でない設計上の注意点（デッドロックの芽など）を警告として返す．

    validate() がエラー（生成不能）を扱うのに対し，lint() は「生成はできるが
    実セッションで問題になりやすい構成」を検出する．
    """
    warns = []
    for app in spec.get("apps", []):
        aname = app.get("name", "?")
        ppg = app.get("constants", {}).get("players_per_group")
        pages = app.get("pages", [])
        waits = [p for p in pages if p.get("kind") == "wait"]

        # 1) グループなしでの待機ページは「全参加者」の到着待ちになる
        if ppg is None and waits:
            warns.append(
                f"{aname}: players_per_group が未指定（グループなし）のため，待機ページは"
                "セッションの全参加者を待つ．オンライン実験では1人の離脱で全員が止まる——"
                "少人数グループ化か timeout の検討を勧める")

        # 2) groupレベルのlogicがあるのにグループなし
        if ppg is None and any(f.get("level") == "group" for f in app.get("logic", [])):
            warns.append(
                f"{aname}: グループなし（players_per_group=None）だが group レベルの"
                "logic がある．group.get_players() は全参加者を返す点に注意")

        for i, pg in enumerate(pages):
            if pg.get("kind") != "wait":
                continue
            pname = pg.get("name", "?")
            # 3) 待機ページの直前ページが「一部の人にだけ表示」かつ制限時間なし
            if i > 0:
                prev = pages[i - 1]
                if prev.get("kind") != "wait" and prev.get("display_if") \
                        and not prev.get("timeout_seconds"):
                    warns.append(
                        f"{aname}.{pname}: 直前の '{prev.get('name')}' は一部の参加者に"
                        "しか表示されず，制限時間もない．表示対象者が操作しないと全員が"
                        "待ち続ける——直前ページに timeout_seconds を勧める")
            # 4) 連続する待機ページ
            if i > 0 and pages[i - 1].get("kind") == "wait":
                warns.append(
                    f"{aname}.{pname}: 待機ページが連続している．2つ目は通常不要である")
            # 5) 先頭の待機ページ（到着順グループ化以外）
            if i == 0 and not pg.get("group_by_arrival_time"):
                warns.append(
                    f"{aname}.{pname}: 最初のページが待機ページである．到着順グループ化"
                    "（group_by_arrival_time）を使わないなら通常は説明ページを先に置く")
        # 6) 待機＋利得計算の後に結果ページがない
        if waits and waits[-1].get("after_all_players_arrive"):
            after = pages[pages.index(waits[-1]) + 1:]
            if not after:
                warns.append(
                    f"{aname}: 利得計算の待機ページで終わっている．結果ページを"
                    "後ろに置かないと参加者は利得を確認できない")
    return warns


# ------------------------------------------------------------- expr helpers

def is_expr(v):
    """値がリテラルでなくPython式（エスケープハッチL1）かどうか．"""
    return isinstance(v, str)


def currency_literal(v):
    return v if is_expr(v) else f"cu({v})"


def py_value(v, ftype=None):
    # str/longstr型の値は文字列リテラルであり，式として埋め込まない
    if ftype in ("str", "longstr"):
        return repr(v)
    if is_expr(v):
        return v  # 式はそのまま埋め込む
    if ftype == "currency":
        return f"cu({v})"
    return repr(v)


# ------------------------------------------------------------ code emitters

def emit_field(f):
    args = []
    if f.get("label") is not None:
        args.append(f"label={f['label']!r}")
    if f.get("doc"):
        args.append(f"doc={f['doc']!r}")
    for key in ("min", "max", "initial"):
        if f.get(key) is not None:
            args.append(f"{key}={py_value(f[key], f['type'])}")
    # MultiCheckboxのchoicesはUI上のみの選択肢であり，保存値はカンマ区切りの複合文字列
    # になるため，モデル側のchoices制約（単一値の検証）には渡さない
    if f.get("choices") is not None and f.get("widget") != "MultiCheckbox":
        args.append(f"choices={f['choices']!r}")
    if f.get("blank"):
        args.append("blank=True")
    # Slider/ButtonSelectはテンプレート側で手書きinputとして生成するため，
    # モデル側にはoTree公式widgetのみを出力する
    if f.get("widget") in OTREE_WIDGETS:
        args.append(f"widget=widgets.{f['widget']}")
    return f"    {f['name']} = models.{FIELD_TYPES[f['type']]}({', '.join(args)})"


def emit_model(cls, base, fields):
    lines = [f"class {cls}({base}):"]
    if fields:
        lines += [emit_field(f) for f in fields]
    else:
        lines.append("    pass")
    return "\n".join(lines)


def emit_logic(fn):
    sig = LOGIC_LEVELS[fn["level"]]
    body = textwrap.indent(fn["body"].rstrip() or "pass", "    ")
    return f"def {fn['name']}({sig}):\n{body}"


def emit_page(pg):
    lines = []
    if pg["kind"] == "wait":
        lines.append(f"class {pg['name']}(WaitPage):")
        inner = []
        if pg.get("group_by_arrival_time"):
            inner.append("    group_by_arrival_time = True")
        if pg.get("body"):
            inner.append(f"    body_text = {pg['body']!r}")
        if pg.get("after_all_players_arrive"):
            inner.append(f"    after_all_players_arrive = {pg['after_all_players_arrive']!r}")
        lines += inner or ["    pass"]
        return "\n".join(lines)

    lines.append(f"class {pg['name']}(Page):")
    inner = []
    if pg["kind"] == "form":
        inner.append(f"    form_model = {pg['form_model']!r}")
        inner.append(f"    form_fields = {pg['form_fields']!r}")
    if pg.get("timeout_seconds"):
        inner.append(f"    timeout_seconds = {pg['timeout_seconds']}")
    if pg.get("display_if"):
        inner.append("    @staticmethod")
        inner.append("    def is_displayed(player: Player):")
        inner.append(f"        return {pg['display_if']}")
    if pg.get("vars_for_template"):
        inner.append("    @staticmethod")
        inner.append("    def vars_for_template(player: Player):")
        inner.append(textwrap.indent(pg["vars_for_template"].rstrip(), "        "))
    if pg.get("js_vars"):
        # テンプレートJSの js_vars 変数にdictを渡す（グラフ描画等に使う）
        inner.append("    @staticmethod")
        inner.append("    def js_vars(player: Player):")
        inner.append(textwrap.indent(pg["js_vars"].rstrip(), "        "))
    if pg.get("live_method"):
        # ライブページ：liveSend(data) を受けて dict（宛先id->データ，0=全員）を返す
        inner.append("    @staticmethod")
        inner.append("    def live_method(player: Player, data):")
        inner.append(textwrap.indent(pg["live_method"].rstrip(), "        "))
    lines += inner or ["    pass"]
    return "\n".join(lines)


def emit_bot(app):
    """仕様からPlayerBotを導出する．全form欄にtest_valueがあれば生成可能．"""
    steps = []
    for pg in app["pages"]:
        if pg["kind"] == "wait":
            continue
        if pg["kind"] == "form":
            fields = {}
            for ff in pg.get("form_fields", []):
                model = pg.get("form_model")
                fdef = next((f for f in app["models"].get(model, []) if f["name"] == ff), None)
                if fdef is None or "test_value" not in fdef:
                    return None  # テスト値が欠けていればbotは生成しない
                fields[ff] = py_value(fdef["test_value"], fdef["type"])
            if not fields:
                return None
            kv = ", ".join(f"{k}={v}" for k, v in fields.items())
            step = f"yield {pg['name']}, dict({kv})"
        else:
            step = f"yield {pg['name']}"
        cond = pg.get("display_if")
        if cond:
            cond = re.sub(r"\b(player|group|subsession|participant)\b", r"self.\1", cond)
            step = f"if {cond}:\n    {step}"
        steps.append(step)
    body = textwrap.indent("\n".join(steps), "        ")
    return f"class PlayerBot(Bot):\n    def play_round(self):\n{body}"


def emit_call_live_method(app):
    """live_test を持つライブページから，botテスト中のliveSend模擬関数を導出する"""
    live_pages = [pg for pg in app.get("pages", [])
                  if pg.get("live_method") and pg.get("live_test")]
    if not live_pages:
        return None
    lines = ["def call_live_method(method, **kwargs):",
             "    page_name = kwargs['page_class'].__name__"]
    for pg in live_pages:
        lines.append(f"    if page_name == {pg['name']!r}:")
        lines.append(textwrap.indent(pg["live_test"].rstrip(), "        "))
    return "\n".join(lines)


def emit_app_init(app):
    c = app["constants"]
    const_lines = [
        f"    NAME_IN_URL = {app['name']!r}",
        f"    PLAYERS_PER_GROUP = {c.get('players_per_group')!r}".replace("'None'", "None"),
        f"    NUM_ROUNDS = {c.get('num_rounds', 1)}",
    ]
    for extra in c.get("extra", []):
        const_lines.append(f"    {extra['name']} = {py_value(extra['value'], extra['type'])}")

    parts = [
        "from otree.api import *",
        "",
        "",
        f"doc = {app.get('doc', '')!r}",
        "",
        "",
        "class C(BaseConstants):",
        "\n".join(const_lines),
        "",
        "",
        emit_model("Subsession", "BaseSubsession", app["models"].get("subsession", [])),
        "",
        "",
        emit_model("Group", "BaseGroup", app["models"].get("group", [])),
        "",
        "",
        emit_model("Player", "BasePlayer", app["models"].get("player", [])),
        "",
        "",
        "# --- logic (ESLから注入されたフック) ---",
    ]
    for fn in app.get("logic", []):
        parts += [emit_logic(fn), "", ""]
    parts.append("# --- pages ---")
    for pg in app["pages"]:
        parts += [emit_page(pg), "", ""]
    bot = emit_bot(app)
    if bot:
        parts += ["# --- bots (仕様から自動導出) ---", bot, "", ""]
        clm = emit_call_live_method(app)
        if clm:
            parts += [clm, "", ""]
    seq = ", ".join(p["name"] for p in app["pages"])
    parts.append(f"page_sequence = [{seq}]")
    return "\n".join(parts) + "\n"


def _esc(v):
    """ラベル・選択肢値をHTML属性/テキストに埋め込む前にエスケープする"""
    return html.escape(str(v), quote=True)


def _tmpl_num(v):
    """数値はそのまま，式はoTreeテンプレート参照（{{ ... }}）としてHTMLに埋める"""
    return f"{{{{ {v} }}}}" if is_expr(v) else v


def _const_ns(app):
    """定数（C.*）の数値名前空間を作る（min/max式の事前評価用）"""
    c = app["constants"]
    ns = {}
    if isinstance(c.get("players_per_group"), int):
        ns["PLAYERS_PER_GROUP"] = c["players_per_group"]
    if isinstance(c.get("num_rounds"), int):
        ns["NUM_ROUNDS"] = c["num_rounds"]
    for e in c.get("extra", []):
        v = e.get("value")
        if isinstance(v, str):
            # "cu(1000)" のような表記も数値に解決する
            try:
                v = eval(v, {"__builtins__": {}}, {"cu": float})
            except Exception:
                continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ns[e["name"]] = v
    return ns


def eval_const_expr(expr, app):
    """C.* のみを参照する式を数値に事前評価する（不可ならNone）．

    currency定数はテンプレート描画だと「1,000ポイント」等の書式付き文字列になり，
    HTMLのmin/max属性として不正になる．そこで定数参照は生成時に数値へ畳み込む．
    """
    ns = SimpleNamespace(**_const_ns(app))
    try:
        val = eval(expr, {"__builtins__": {}}, {"C": ns, "cu": float})
    except Exception:
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return int(val) if float(val).is_integer() else val
    return None


def resolve_field_nums(f, app):
    """min/max/initial の定数式を数値に解決したフィールド定義のコピーを返す"""
    out = dict(f)
    for k in ("min", "max", "initial"):
        v = out.get(k)
        if is_expr(v):
            ev = eval_const_expr(v, app)
            if ev is not None:
                out[k] = ev
    return out


def _fill(tmpl, **kw):
    """@key@ 形式のプレースホルダを置換する（CSS/JSの波括弧と衝突しないため）"""
    for k, v in kw.items():
        tmpl = tmpl.replace(f"@{k}@", str(v))
    return tmpl


def _choice_pairs(f):
    return [(c[0], c[1]) if isinstance(c, (list, tuple)) else (c, c)
            for c in f.get("choices", [])]


_SLIDER_NOANCHOR_TMPL = '''<div class="mb-3">
  <label class="form-label">@label@</label>
  <div class="d-flex align-items-center" style="gap: 1rem">
    <span class="text-muted small">@left@</span>
    <input type="range" class="form-range" id="ui_@name@"
           min="@min@" max="@max@" step="@step@" value="@min@">
    <span class="text-muted small">@right@</span>
    <span id="@name@_out" class="badge bg-primary fs-6 d-none" style="min-width: 3.5em"></span>
  </div>
  <div id="@name@_hint" class="form-text">バーをクリックすると値が表示されます</div>
  <input type="hidden" name="@name@" id="id_@name@" value="">
</div>
<style>
  /* クリックするまでつまみを隠す（アンカリング回避） */
  #ui_@name@::-webkit-slider-thumb { opacity: 0; }
  #ui_@name@::-moz-range-thumb { opacity: 0; }
  #ui_@name@.touched::-webkit-slider-thumb { opacity: 1; }
  #ui_@name@.touched::-moz-range-thumb { opacity: 1; }
</style>
<script>
(function () {
  var ui = document.getElementById('ui_@name@');
  var hid = document.getElementById('id_@name@');
  var out = document.getElementById('@name@_out');
  function set() {
    ui.classList.add('touched');
    hid.value = ui.value;
    out.textContent = ui.value;
    out.classList.remove('d-none');
    document.getElementById('@name@_hint').classList.add('d-none');
  }
  ui.addEventListener('input', set);
  ui.addEventListener('pointerup', set);
})();
</script>'''

_STAR_RATING_TMPL = '''<div class="mb-3">
  <label class="form-label d-block">@label@</label>
  <div id="stars_@name@" style="font-size: 2rem; cursor: pointer; user-select: none; color: #f0a500">@spans@</div>
  <input type="hidden" name="@name@" id="id_@name@" value="">
</div>
<script>
(function () {
  var box = document.getElementById('stars_@name@');
  var hid = document.getElementById('id_@name@');
  box.addEventListener('click', function (e) {
    var v = e.target.dataset.v;
    if (!v) return;
    hid.value = v;
    box.querySelectorAll('span').forEach(function (s) {
      s.textContent = (Number(s.dataset.v) <= Number(v)) ? '★' : '☆';
    });
  });
})();
</script>'''

_STEPPER_TMPL = '''<div class="mb-3">
  <label class="form-label" for="id_@name@">@label@</label>
  <div class="input-group" style="max-width: 14rem">
    <button type="button" class="btn btn-outline-secondary"
            onclick="document.getElementById('id_@name@').stepDown()">−</button>
    <input type="number" name="@name@" id="id_@name@" class="form-control text-center"
           min="@min@" max="@max@" step="@step@" value="@init@">
    <button type="button" class="btn btn-outline-secondary"
            onclick="document.getElementById('id_@name@').stepUp()">＋</button>
  </div>
</div>'''

_MULTICHECK_TMPL = '''<div class="mb-3" id="mc_@name@">
  <p class="form-label">@label@</p>
@boxes@
  <input type="hidden" name="@name@" id="id_@name@" value="">
</div>
<script>
(function () {
  var box = document.getElementById('mc_@name@');
  var hid = document.getElementById('id_@name@');
  box.addEventListener('change', function () {
    var vals = [];
    box.querySelectorAll('input[type=checkbox]:checked').forEach(function (c) { vals.push(c.value); });
    hid.value = vals.join(',');
  });
})();
</script>'''


def emit_input_html(f):
    """フィールド1つ分の入力HTMLを返す（標準は {{ formfield }}，独自widgetは手書きinput）"""
    name = f["name"]
    label = _esc(f.get("label", name))
    w = f.get("widget")

    if w == "Slider":
        mn = _tmpl_num(f.get("min", 0))
        mx = _tmpl_num(f.get("max", 100))
        step = f.get("step", 1)
        init = _tmpl_num(f["initial"] if f.get("initial") is not None else f.get("min", 0))
        return f'''<div class="mb-3">
  <label class="form-label" for="id_{name}">{label}</label>
  <div class="d-flex align-items-center" style="gap: 1rem">
    <span class="text-muted small">{_esc(f.get("left_label", ""))}</span>
    <input type="range" class="form-range" name="{name}" id="id_{name}"
           min="{mn}" max="{mx}" step="{step}" value="{init}"
           oninput="document.getElementById('{name}_out').textContent = this.value">
    <span class="text-muted small">{_esc(f.get("right_label", ""))}</span>
    <span id="{name}_out" class="badge bg-primary fs-6" style="min-width: 3.5em">{init}</span>
  </div>
</div>'''

    if w == "SliderNoAnchor":
        return _fill(_SLIDER_NOANCHOR_TMPL, name=name, label=label,
                     min=_tmpl_num(f.get("min", 0)), max=_tmpl_num(f.get("max", 100)),
                     step=f.get("step", 1),
                     left=_esc(f.get("left_label", "")), right=_esc(f.get("right_label", "")))

    if w == "ButtonSelect":
        btns = [f'    <button type="submit" name="{name}" value="{_esc(val)}" '
                f'class="btn btn-outline-primary btn-lg m-1">{_esc(lab)}</button>'
                for val, lab in _choice_pairs(f)]
        return (f'<div class="mb-3">\n  <p class="form-label">{label}</p>\n'
                + "\n".join(btns) + "\n</div>")

    if w == "StarRating":
        n = int(f["max"])
        spans = "".join(f'<span data-v="{i}">☆</span>' for i in range(1, n + 1))
        return _fill(_STAR_RATING_TMPL, name=name, label=label, spans=spans)

    if w == "NumberStepper":
        mn = "" if f.get("min") is None else _tmpl_num(f["min"])
        mx = "" if f.get("max") is None else _tmpl_num(f["max"])
        init = f["initial"] if f.get("initial") is not None else (f.get("min") or 0)
        return _fill(_STEPPER_TMPL, name=name, label=label, min=mn, max=mx,
                     step=f.get("step", 1), init=_tmpl_num(init))

    if w == "MultiCheckbox":
        boxes = []
        for i, (val, lab) in enumerate(_choice_pairs(f)):
            boxes.append(
                f'  <div class="form-check">\n'
                f'    <input class="form-check-input" type="checkbox" value="{_esc(val)}" id="mc_{name}_{i}">\n'
                f'    <label class="form-check-label" for="mc_{name}_{i}">{_esc(lab)}</label>\n'
                f'  </div>')
        return _fill(_MULTICHECK_TMPL, name=name, label=label, boxes="\n".join(boxes))

    return f"{{{{ formfield '{name}' }}}}"


def emit_matrix_html(group_fields):
    """連続するLikertMatrixフィールドを1つの表（行=質問，列=選択肢）にまとめる"""
    cols = _choice_pairs(group_fields[0])
    head = "".join(f'<th class="text-center">{_esc(lab)}</th>' for _, lab in cols)
    rows = []
    for f in group_fields:
        cells = "".join(
            f'<td class="text-center"><input class="form-check-input" type="radio" '
            f'name="{f["name"]}" value="{_esc(val)}" required></td>' for val, _ in cols)
        rows.append(f'    <tr><td>{_esc(f.get("label", f["name"]))}</td>{cells}</tr>')
    return ('<table class="table table-striped align-middle">\n'
            f'  <thead><tr><th></th>{head}</tr></thead>\n  <tbody>\n'
            + "\n".join(rows) + "\n  </tbody>\n</table>")


# 本文を構造化して組むセクション（content配列）の種類
SECTION_TYPES = ("heading", "paragraph", "alert", "image", "table",
                 "columns", "field", "chart", "html")


def render_section(sec, fdefs, idx):
    """contentの1セクションをHTMLにする．

    段落等の本文テキストは従来のbody HTMLと同じ扱い（作成者の入力をそのまま埋め込み，
    {{ C.X }} 等のoTree記法・インラインHTMLを許す）．
    """
    t = sec.get("type")
    if t == "heading":
        lvl = sec.get("level", 4)
        return f'<h{lvl} class="mt-3">{sec.get("text", "")}</h{lvl}>'
    if t == "paragraph":
        return f'<p>{sec.get("text", "")}</p>'
    if t == "alert":
        style = sec.get("style", "info")
        return f'<div class="alert alert-{style}">{sec.get("text", "")}</div>'
    if t == "image":
        width = sec.get("width", 100)
        cap = (f'<figcaption class="figure-caption text-center">{sec.get("caption", "")}'
               '</figcaption>' if sec.get("caption") else "")
        return ('<figure class="figure d-block text-center my-3">'
                f'<img src="{_esc(sec.get("src", ""))}" class="figure-img img-fluid" '
                f'style="max-width: {width}%" alt="">{cap}</figure>')
    if t == "table":
        head = "".join(f"<th>{c}</th>" for c in sec.get("header", []))
        rows = "\n".join(
            "    <tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
            for r in sec.get("rows", []))
        return ('<table class="table table-striped">\n'
                f'  <thead><tr>{head}</tr></thead>\n  <tbody>\n{rows}\n  </tbody>\n</table>')
    if t == "columns":
        return ('<div class="row my-3">'
                f'<div class="col-sm">{sec.get("left", "")}</div>'
                f'<div class="col-sm">{sec.get("right", "")}</div></div>')
    if t == "field":
        f = fdefs.get(sec.get("name"))
        if f is None:
            return f"{{{{ formfield '{sec.get('name', '')}' }}}}"
        if f.get("widget") == "LikertMatrix":
            return emit_matrix_html([f])
        return emit_input_html(f)
    if t == "chart":
        cid = f"chart_{idx}"
        kind = sec.get("kind", "bar")
        label = _esc(sec.get("label", "値"))
        return f"""<canvas id="{cid}" style="max-height: 320px"></canvas>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
new Chart(document.getElementById('{cid}'), {{
  type: '{kind}',
  data: {{ labels: js_vars.labels,
          datasets: [{{ label: '{label}', data: js_vars.values,
                       backgroundColor: '#60a5fa', borderColor: '#2563eb' }}] }},
  options: {{ plugins: {{ legend: {{ display: false }} }},
             scales: {{ y: {{ beginAtZero: true }} }} }}
}});
</script>"""
    if t == "html":
        return sec.get("code", "")
    return ""


def sections_to_html(content, fdefs=None, with_fields=False):
    """content配列を本文HTMLにする（GUIの「HTML直書きへ変換」等で使う）．

    with_fields=False ではフォーム欄セクションを除外する
    （HTML直書きモードでは欄が本文の後ろへ自動挿入されるため，重複を避ける）．
    """
    fdefs = fdefs or {}
    parts = []
    for i, sec in enumerate(content):
        if sec.get("type") == "field" and not with_fields:
            continue
        h = render_section(sec, fdefs, i)
        if h:
            parts.append(h)
    return "\n".join(parts)


def emit_page_html(pg, app=None):
    fdefs = {}
    if app is not None and pg["kind"] == "form":
        # min/max等の定数式（C.ENDOWMENT等）は数値に解決してからHTML属性に埋める
        fdefs = {f["name"]: resolve_field_nums(f, app)
                 for f in app["models"].get(pg.get("form_model"), [])}

    # content（セクション配列）があれば本文を構造から組み立てる．無ければbody（生HTML）
    placed = set()
    if pg.get("content") is not None:
        parts = []
        for i, sec in enumerate(pg["content"]):
            parts.append(render_section(sec, fdefs, i))
            if sec.get("type") == "field":
                placed.add(sec.get("name"))
        body = "\n".join(p for p in parts if p)
    else:
        body = pg.get("body", "")

    blocks = [
        "{{ block title }}",
        pg.get("title", pg["name"]),
        "{{ endblock }}",
        "{{ block content }}",
        body,
    ]
    # raw_html: 本文を完全手動とし，フォーム欄・次へボタンを自動挿入しない（L3相当）
    if pg.get("raw_html"):
        blocks.append("{{ endblock }}")
        return "\n".join(blocks) + "\n"

    show_next = True
    if pg["kind"] == "form":
        fields = [fdefs.get(n, {"name": n}) for n in pg.get("form_fields", [])]
        # セクションで配置済みの欄は除き，残りを本文の後ろに自動挿入する
        rest = [f for f in fields if f["name"] not in placed]
        # 連続するLikertMatrix（同一choices）は1つの表にまとめて出力する
        items, buf = [], []

        def flush():
            if buf:
                items.append(emit_matrix_html(list(buf)))
                buf.clear()

        for f in rest:
            if f.get("widget") == "LikertMatrix":
                if buf and json.dumps(buf[0].get("choices")) != json.dumps(f.get("choices")):
                    flush()
                buf.append(f)
            else:
                flush()
                items.append(emit_input_html(f))
        flush()
        blocks += items
        # 全欄がボタン選択なら，ボタン自体が送信するため次へボタンは不要
        if fields and all(f.get("widget") == "ButtonSelect" for f in fields):
            show_next = False
    if show_next:
        blocks.append("{{ next_button }}")
    blocks.append("{{ endblock }}")
    return "\n".join(blocks) + "\n"


# ------------------------------------------------------------------- theme

def font_links(theme):
    """Google Fontsの読み込みタグを返す（フォント未指定なら空文字）"""
    font = theme.get("font")
    if not font:
        return ""
    fam = str(font).replace(" ", "+")
    return ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            f'<link href="https://fonts.googleapis.com/css2?family={fam}:wght@400;700'
            '&display=swap" rel="stylesheet">\n')


def theme_css(theme):
    """テーマ設定からCSS本体を生成する（生成物とGUIプレビューで共用）"""
    font = theme.get("font")
    family = (f'"{font}", ' if font else "") + "sans-serif"
    base = theme.get("base_color", "#2563eb")
    return f"""body {{
  font-family: {family};
  font-size: {theme.get("font_size", 16)}px;
  background-color: {theme.get("bg_color", "#ffffff")};
  color: {theme.get("text_color", "#212529")};
}}
.otree-title {{ color: {base}; }}
.btn-primary {{ background-color: {base}; border-color: {base}; }}
.progress-bar {{ background-color: {base}; }}"""


def emit_global_page(theme):
    """全ページ共通の土台テンプレート（_templates/global/Page.html）を生成する"""
    return (f'{{{{ extends "otree/Page.html" }}}}\n\n'
            f'{{{{ block global_styles }}}}\n'
            f'{font_links(theme)}<style>\n{theme_css(theme)}\n</style>\n'
            f'{{{{ endblock }}}}\n')


def rooms_config(spec):
    """ESLのrooms[]からsettings.pyのROOMSリストを作る"""
    out = []
    for r in spec.get("rooms") or []:
        d = dict(name=r["name"], display_name=r.get("display_name") or r["name"])
        if r.get("participant_labels"):
            d["participant_label_file"] = f"_rooms/{r['name']}.txt"
        if r.get("use_secure_urls"):
            d["use_secure_urls"] = True
        out.append(d)
    return out


def emit_settings(spec):
    s = spec["session"]
    cfg = dict(
        name=s["config_name"],
        display_name=s.get("display_name", s["config_name"]),
        app_sequence=s["app_sequence"],
        num_demo_participants=s.get("num_demo_participants", 2),
    )
    rooms_lines = ",\n    ".join(repr(r) for r in rooms_config(spec))
    rooms_block = f"ROOMS = [\n    {rooms_lines},\n]" if rooms_lines else "ROOMS = []"
    return f"""from os import environ

SESSION_CONFIGS = [
    {cfg!r},
]

SESSION_CONFIG_DEFAULTS = dict(
    real_world_currency_per_point={s.get('real_world_currency_per_point', 1.0)},
    participation_fee={s.get('participation_fee', 0.0)},
    doc="",
)

PARTICIPANT_FIELDS = []
SESSION_FIELDS = []

{rooms_block}

LANGUAGE_CODE = {s.get('language_code', 'ja')!r}
REAL_WORLD_CURRENCY_CODE = {s.get('currency_code', 'JPY')!r}
USE_POINTS = {s.get('use_points', True)}

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = environ.get('OTREE_ADMIN_PASSWORD')

DEMO_PAGE_INTRO_HTML = ""

SECRET_KEY = '{'forge-' + spec['meta']['name']}'

INSTALLED_APPS = ['otree']
"""


# -------------------------------------------------------------------- build

def build(spec, outdir: Path):
    validate(spec)
    outdir.mkdir(parents=True, exist_ok=True)
    for aux in ("_static", "_templates"):
        (outdir / aux).mkdir(exist_ok=True)
        (outdir / aux / ".gitkeep").write_text("", encoding="utf-8")
    # テーマがあれば全ページ共通の土台テンプレートを出力する
    if spec.get("theme"):
        gdir = outdir / "_templates" / "global"
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / "Page.html").write_text(emit_global_page(spec["theme"]), encoding="utf-8")
    (outdir / "settings.py").write_text(emit_settings(spec), encoding="utf-8")
    (outdir / "requirements.txt").write_text("otree\n", encoding="utf-8")
    # 部屋の参加者ラベルファイル（1行1ラベル）
    for r in spec.get("rooms") or []:
        if r.get("participant_labels"):
            rdir = outdir / "_rooms"
            rdir.mkdir(exist_ok=True)
            (rdir / f"{r['name']}.txt").write_text(
                "\n".join(str(x).strip() for x in r["participant_labels"]) + "\n",
                encoding="utf-8")
    for app in spec["apps"]:
        appdir = outdir / app["name"]
        appdir.mkdir(exist_ok=True)
        (appdir / "__init__.py").write_text(emit_app_init(app), encoding="utf-8")
        if emit_bot(app):
            # oTreeのbotランナーは {app}.tests を探すため，シムを置く
            names = "PlayerBot"
            if emit_call_live_method(app):
                names += ", call_live_method"
            (appdir / "tests.py").write_text(
                f"from . import {names}  # noqa: F401\n", encoding="utf-8"
            )
        for pg in app["pages"]:
            if pg["kind"] == "wait":
                continue
            (appdir / f"{pg['name']}.html").write_text(emit_page_html(pg, app), encoding="utf-8")
    print(f"OK: oTreeプロジェクトを {outdir} に生成した")


def main():
    ap = argparse.ArgumentParser(description="ESL -> oTree generator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("spec")
    b = sub.add_parser("build")
    b.add_argument("spec")
    b.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    if args.cmd == "validate":
        validate(spec)
        print("OK: 仕様は妥当である")
    else:
        build(spec, Path(args.out))


if __name__ == "__main__":
    try:
        main()
    except SpecError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
