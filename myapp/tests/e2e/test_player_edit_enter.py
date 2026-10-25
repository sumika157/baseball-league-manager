"""選手の編集画面で、入力の保存と操作のボタンが別のフォームであること（W3 T4・T7。#79）。

Enter での送信（暗黙の送信）は、フォームの文書順で最初の送信ボタンが押されたことになる（HTML の仕様）。
T4 の時点では「支配下登録する」「主将にする」が「更新する」と同じフォームの前にあり、プロフィールの欄で Enter を
押しただけで昇格や主将の指名が起きた。T7 で操作のボタンを入力のフォームの外の別のフォームに分けたので、入力のフォームの
ボタンは「更新する」だけになり、操作のボタンを押しても入力欄の値は送られない。どのボタンが押されたことになるか・
どの欄が送られるかはブラウザが決めるので、実ブラウザで確かめる（HTML の形は integration の `ActionFormsTest`）。
"""

from urllib.parse import parse_qsl

from django.contrib.auth.models import User

from myapp.infrastructure import orm_models

from ..helpers import build_roster_service, make_league, real_scope, ws_url
from .base import PlaywrightTestCase


class PlayerEditEnterTest(PlaywrightTestCase):
    def setUp(self):
        super().setUp()
        league = make_league(name="Eリーグ")
        self.team = orm_models.Team.objects.create(league=league, name="ホームズ")
        # 育成選手（支配下登録のボタンが出る）で、主将ではない（主将にするボタンが出る）
        self.player = build_roster_service(real_scope()).register_player(
            self.team.id, "育成太郎", "120", "投手", contract_label="育成"
        )
        user = User.objects.create_user(username="manager", password="pass12345")
        self.team.managers.add(user)

    def _login(self):
        self.page.goto(self.live_server_url + "/accounts/login/")
        self.page.fill('input[name="username"]', "manager")
        self.page.fill('input[name="password"]', "pass12345")
        self.page.locator('input[name="password"]').press("Enter")
        self.page.wait_for_url(f"{self.live_server_url}/")

    def _open_edit(self):
        self._login()
        self.page.goto(self.live_server_url + ws_url("player_edit", self.team.id, self.player.id))

    def test_enter_in_a_profile_field_updates_and_changes_nothing_else(self):
        self._open_edit()

        self.page.get_by_label("よみがな").fill("イクセイタロウ")
        self.page.get_by_label("よみがな").press("Enter")

        # 更新が通ると選手一覧へ戻る（支配下登録・主将のボタンなら編集画面へ戻る）
        self.page.wait_for_url(f"**{ws_url('player_list', self.team.id)}**")
        self.assertEqual(orm_models.Player.objects.get(id=self.player.id).name_kana, "イクセイタロウ")
        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertEqual((stint.signed_as, stint.promoted_year, stint.number), ("育成", None, "120"))
        self.assertFalse(orm_models.Captaincy.objects.filter(player_id=self.player.id).exists())

    def test_an_action_button_does_not_send_the_input_fields(self):
        """欄を書き換えてから「主将にする」を押すと、主将になり、書き換えた欄は送られない（保存されない）。"""
        self._open_edit()

        self.page.get_by_label("よみがな").fill("イクセイタロウ")
        self.page.locator('input[name="name"]').fill("書き換えた名前")
        with self.page.expect_navigation(), self.page.expect_request(lambda r: r.method == "POST") as posted:
            self.page.get_by_role("button", name="主将にする").click()

        # 送られたのは csrf のトークンと押したボタンだけ（入力欄・プロフィールの目印は送られない）
        sent = {key for key, _ in parse_qsl(posted.value.post_data or "", keep_blank_values=True)}
        self.assertEqual(sent, {"csrfmiddlewaretoken", "appoint_captain"})
        self.assertTrue(orm_models.Captaincy.objects.filter(player_id=self.player.id).exists())
        player = orm_models.Player.objects.get(id=self.player.id)
        self.assertEqual((player.name, player.name_kana), ("育成太郎", ""))

    def test_enter_in_the_new_number_promotes(self):
        """支配下登録の新しい背番号の欄で Enter を押すと、そのフォームの「支配下登録する」が押される。"""
        self._open_edit()

        self.page.get_by_label("新しい背番号").fill("55")
        with self.page.expect_navigation():
            self.page.get_by_label("新しい背番号").press("Enter")

        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertEqual((stint.promoted_year is not None, stint.number), (True, "55"))
        self.assertEqual(orm_models.Player.objects.get(id=self.player.id).name_kana, "")
