"""選手の編集画面でのプロフィールの編集（W3 T4。#79）。

プロフィールは基本情報と同じフォーム・同じ「更新する」で保存し（決定4。`RosterService.update_player` の1回の
集約の保存）、**節の目印（隠し欄）が送られてきたときだけ**更新する。目印の無い送信（退団・主将などのボタンや、
欄を持たない送信）でプロフィールを空で上書きしない（JSON API の部分更新と同じ罠。CLAUDE.md の既知の罠）。
操作のボタンは T7 で入力のフォームの外の別のフォームに分けた（`ActionFormsTest`）。
"""

from datetime import date
from html.parser import HTMLParser

from django.contrib.messages import get_messages

from myapp.infrastructure import orm_models
from myapp.presentation.forms import PROFILE_SECTION_MARKER

from ..helpers import login_as_manager, ws_url
from .base import BaseCase

# 送る14項目（ドラフトは年・順位・育成の3欄）と、保存されたときの列の値
FULL_PROFILE = {
    "name_kana": "ヤマダタロウ",
    "back_name": "T.YAMADA",
    "birth_date": "2001-04-02",
    "throws": "右",
    "bats": "左",
    "height_cm": "185",
    "weight_kg": "88",
    "birthplace": "大阪府",
    "draft_year": "2019",
    "draft_round": "1",
    "high_school": "大阪桐蔭高",
    "university": "早稲田大",
    "corporate_team": "トヨタ自動車",
    "nationality": "日本",
    "is_foreign_player": "on",
}
SAVED_COLUMNS = {
    "name_kana": "ヤマダタロウ",
    "back_name": "T.YAMADA",
    "birth_date": date(2001, 4, 2),
    "throws": "右",
    "bats": "左",
    "height_cm": 185,
    "weight_kg": 88,
    "birthplace": "大阪府",
    "draft_year": 2019,
    "draft_round": 1,
    "is_development_draft": False,
    "high_school": "大阪桐蔭高",
    "university": "早稲田大",
    "corporate_team": "トヨタ自動車",
    "nationality": "日本",
    "is_foreign_player": True,
}
BASIC = {"name": "山田", "number": "10", "position": "内野手"}


class PlayerProfileEditTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.player = self.roster.register_player(self.team.id, "山田", "10", "内野手")
        self.manager = login_as_manager(self.client, self.team)

    def url(self, player_id=None):
        return ws_url("player_edit", self.team.id, player_id or self.player.id)

    def columns(self):
        return orm_models.Player.objects.filter(id=self.player.id).values(*SAVED_COLUMNS).get()

    def post_profile(self, profile, **basic):
        return self.client.post(self.url(), {**BASIC, **basic, PROFILE_SECTION_MARKER: "1", **profile})

    def save_full_profile(self):
        response = self.post_profile(FULL_PROFILE)
        self.assertEqual(response.status_code, 302, self.error_of(response))

    def error_of(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    # --- 往復 ---

    def test_all_fourteen_items_round_trip(self):
        response = self.post_profile(FULL_PROFILE, name="山田太郎")

        self.assertEqual(response.status_code, 302, self.error_of(response))
        self.assertEqual(self.columns(), SAVED_COLUMNS)
        # 基本情報も同じ保存で書かれる
        self.assertEqual(orm_models.Player.objects.get(id=self.player.id).name, "山田太郎")
        # 編集画面を開き直すと、保存した値が入っている
        form = self.client.get(self.url()).context["profile_form"]
        self.assertEqual(
            {name: form[name].value() for name in SAVED_COLUMNS},
            SAVED_COLUMNS,
        )

    def test_development_draft_round_trips(self):
        player = self.roster.register_player(
            self.team.id, "育成", "120", "投手", contract_label="育成", acquired_via_label="育成ドラフト"
        )
        response = self.client.post(
            self.url(player.id),
            {
                "name": "育成",
                "number": "120",
                "position": "投手",
                PROFILE_SECTION_MARKER: "1",
                "draft_year": "2019",
                "draft_round": "3",
                "is_development_draft": "on",
            },
        )

        self.assertEqual(response.status_code, 302, self.error_of(response))
        row = orm_models.Player.objects.get(id=player.id)
        self.assertEqual((row.draft_year, row.draft_round, row.is_development_draft), (2019, 3, True))

    def test_blank_items_with_the_marker_clear_the_profile(self):
        """目印があれば、空の欄は「空にした」と読む（節を送った人が消した値）。"""
        self.save_full_profile()

        self.post_profile({})

        self.assertEqual(
            self.columns(),
            {
                **{name: "" for name, value in SAVED_COLUMNS.items() if isinstance(value, str)},
                "birth_date": None,
                "height_cm": None,
                "weight_kg": None,
                "draft_year": None,
                "draft_round": None,
                "is_development_draft": False,
                "is_foreign_player": False,
            },
        )

    def test_the_player_page_shows_the_profile(self):
        self.save_full_profile()

        response = self.client.get(ws_url("player_detail", self.team.id, self.player.id))

        for text in (
            "ヤマダタロウ",
            "T.YAMADA",
            "右投左打",
            "185",
            "大阪府",
            "2019年ドラフト1位",
            "早稲田大",
            "外国人",
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)

    # --- 目印の無い送信でプロフィールが消えない（再発防止） ---

    def test_a_post_without_the_marker_keeps_the_profile(self):
        """プロフィールの欄を持たない送信（基本情報だけ）は、プロフィールを空で上書きしない。"""
        self.save_full_profile()

        response = self.client.post(self.url(), {**BASIC, "name": "山田次郎"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.Player.objects.get(id=self.player.id).name, "山田次郎")
        self.assertEqual(self.columns(), SAVED_COLUMNS)

    def test_the_other_buttons_keep_the_profile(self):
        """操作のボタン（支配下登録・主将・退団）はプロフィールを読まない。

        今の画面ではボタンごとに別のフォームで目印も欄も送らない（`ActionFormsTest`）。目印つきの送信（T4 の前の
        画面を開いたままの人など）でも、ボタンが押されたらプロフィールを空で上書きしない。
        """
        self.save_full_profile()

        for button in ("promote", "appoint_captain", "remove_captain", "retire"):
            for marker in ({}, {PROFILE_SECTION_MARKER: "1"}):
                with self.subTest(button=button, marker=bool(marker)):
                    self.client.post(self.url(), {button: "1", "promote_number": "5", **marker})
                    self.assertEqual(self.columns(), SAVED_COLUMNS)

    def test_a_successful_promotion_keeps_the_profile(self):
        """育成選手の支配下登録が通っても、プロフィールは変わらない（上の検査の支配下登録はドメインの検査で止まるため）。"""
        developmental = self.roster.register_player(self.team.id, "育成", "120", "投手", contract_label="育成")
        self.client.post(
            self.url(developmental.id), {**BASIC, "number": "120", PROFILE_SECTION_MARKER: "1", **FULL_PROFILE}
        )
        saved = orm_models.Player.objects.filter(id=developmental.id).values(*SAVED_COLUMNS).get()
        self.assertEqual(saved, SAVED_COLUMNS)

        response = self.client.post(self.url(developmental.id), {"promote": "1", "promote_number": "55"})

        self.assertEqual(response.status_code, 302)
        stint = orm_models.PlayerStint.objects.get(player_id=developmental.id)
        self.assertEqual((stint.promoted_year is not None, stint.number), (True, "55"))
        self.assertEqual(orm_models.Player.objects.filter(id=developmental.id).values(*SAVED_COLUMNS).get(), saved)

    # --- 検査（管理画面と同じ domain の規則）。落ちたら何も保存しない ---

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 200)
        self.assertIn(message, " ".join(self.error_of(response)))
        self.assertEqual(self.columns(), SAVED_COLUMNS)
        self.assertEqual(orm_models.Player.objects.get(id=self.player.id).name, "山田")

    def test_an_unrealistic_height_is_refused_and_nothing_is_saved(self):
        self.save_full_profile()

        response = self.post_profile({**FULL_PROFILE, "height_cm": "301"}, name="改名")

        self.assert_refused(response, "身長の値が現実的ではありません")
        # 送った値のまま出し直す（14項目を打ち直させない）
        self.assertEqual(response.context["profile_form"]["height_cm"].value(), "301")

    def test_a_round_without_a_year_is_refused(self):
        self.save_full_profile()

        response = self.post_profile({**FULL_PROFILE, "draft_year": ""}, name="改名")

        self.assert_refused(response, "ドラフト年を入力してください")

    def test_a_draft_that_contradicts_the_first_stint_is_refused(self):
        """育成ドラフトで入団した選手を本指名にはできない（`ensure_draft_fits_career`）。"""
        player = self.roster.register_player(
            self.team.id, "育成", "121", "投手", contract_label="育成", acquired_via_label="育成ドラフト"
        )

        response = self.client.post(
            self.url(player.id),
            {"name": "改名", "number": "121", "position": "投手", PROFILE_SECTION_MARKER: "1", "draft_year": "2019"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("育成ドラフトで入団した選手は", " ".join(self.error_of(response)))
        row = orm_models.Player.objects.get(id=player.id)
        self.assertEqual((row.name, row.draft_year), ("育成", None))

    def test_a_non_numeric_height_is_a_form_error(self):
        self.save_full_profile()

        response = self.post_profile({**FULL_PROFILE, "height_cm": "高い"}, name="改名")

        self.assert_refused(response, "身長(cm):")

    def test_an_unknown_handedness_is_a_form_error(self):
        """投打の選択肢はドメインの `Handedness` から作る。"""
        self.save_full_profile()

        response = self.post_profile({**FULL_PROFILE, "throws": "右利き"}, name="改名")

        self.assert_refused(response, "投:")

    def test_a_too_long_back_name_is_a_form_error(self):
        self.save_full_profile()

        response = self.post_profile({**FULL_PROFILE, "back_name": "X" * 21}, name="改名")

        self.assert_refused(response, "背ネーム:")

    # --- 権限 ---

    def test_the_manager_of_another_team_is_refused(self):
        self.save_full_profile()
        login_as_manager(self.client, self.rival, username="rival-manager")

        response = self.post_profile({**FULL_PROFILE, "height_cm": "170"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.columns(), SAVED_COLUMNS)


class _Forms(HTMLParser):
    """`<form>` ごとに、送る欄の名前と送信ボタンの属性を文書順に集める（`<button>` の type の既定は submit）。"""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self._current = {"fields": set(), "submits": []}
            self.forms.append(self._current)
        elif self._current is None:
            return
        elif (tag == "button" and attributes.get("type", "submit") == "submit") or (
            tag == "input" and attributes.get("type") == "submit"
        ):
            self._current["submits"].append(attributes)
        elif tag in ("input", "select", "textarea") and "name" in attributes:
            self._current["fields"].add(attributes["name"])

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


# 操作のボタン（name）と、そのフォームが送ってよい欄。入力欄（名前・背番号・守備位置・プロフィール）は送らない
ACTION_FIELDS = {
    "promote": {"csrfmiddlewaretoken", "promote_number"},
    "appoint_captain": {"csrfmiddlewaretoken"},
    "remove_captain": {"csrfmiddlewaretoken"},
    "retire": {"csrfmiddlewaretoken"},
}


class ActionFormsTest(BaseCase):
    """選手の編集画面の操作のボタンは、入力の保存とは別のフォームにある（W3 T7。#79）。

    同じフォームにあると、欄を書き換えてからボタンを押したときに入力が黙って捨てられ、入力欄で Enter を押すと
    文書順で最初のボタン（支配下登録・主将の指名／解任）が押されたことになる（HTML の仕様。T4 で踏んだ）。
    入力のフォームの送信ボタンは「更新する」（name も value も持たない）1つだけで、
    操作のボタンはそれぞれ別のフォームにあり、入力欄の値を送らない。
    """

    def setUp(self):
        super().setUp()
        login_as_manager(self.client, self.team)

    def forms_of(self, player_id):
        parser = _Forms()
        parser.feed(self.client.get(ws_url("player_edit", self.team.id, player_id)).content.decode())
        return parser.forms

    def test_the_action_buttons_are_outside_the_edit_form(self):
        developmental = self.roster.register_player(self.team.id, "育成", "120", "投手", contract_label="育成")
        captain = self.roster.register_player(self.team.id, "主将", "1", "内野手")
        self.roster.appoint_captain(self.team.id, captain.id)
        plain = self.roster.register_player(self.team.id, "山田", "10", "内野手")
        cases = {
            "育成選手（支配下登録のボタンあり）": (developmental, {"promote", "appoint_captain", "retire"}),
            "主将（解任のボタン）": (captain, {"remove_captain", "retire"}),
            "主将でない": (plain, {"appoint_captain", "retire"}),
        }

        for label, (player, expected_actions) in cases.items():
            with self.subTest(label):
                forms = self.forms_of(player.id)
                edit_form = next(form for form in forms if PROFILE_SECTION_MARKER in form["fields"])
                self.assertTrue({"name", "number", "position"} <= edit_form["fields"])
                # 入力のフォームの送信ボタンは「更新する」1つだけ（Enter でもこれが押される）
                self.assertEqual(len(edit_form["submits"]), 1, edit_form["submits"])
                self.assertNotIn("name", edit_form["submits"][0])
                self.assertNotIn("value", edit_form["submits"][0])

                action_forms = {
                    button["name"]: form
                    for form in forms
                    for button in form["submits"]
                    if button.get("name") in ACTION_FIELDS
                }
                self.assertEqual(set(action_forms), expected_actions)
                for action, form in action_forms.items():
                    # ボタンごとに別のフォームで、入力欄の値を送らない
                    self.assertEqual(len(form["submits"]), 1, f"{action} のフォームにほかのボタンがある")
                    self.assertEqual(form["fields"], ACTION_FIELDS[action], action)
