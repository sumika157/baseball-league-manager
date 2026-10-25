"""選手の編集画面で、入力欄で Enter を押したときに「更新する」になること（W3 T4。#79）。

Enter での送信（暗黙の送信）は、フォームの文書順で最初の送信ボタンが押されたことになる（HTML の仕様）。
この画面は「支配下登録する」「主将にする」が「更新する」より前にあり、先頭に見えない「更新する」を置かないと、
プロフィールの欄で Enter を押しただけで昇格や主将の指名が起きる。どのボタンが押されたことになるかは
ブラウザが決めるので、実ブラウザで確かめる（HTML の並びは integration の `DefaultSubmitButtonTest`）。
"""

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

    def test_enter_in_a_profile_field_updates_and_changes_nothing_else(self):
        self._login()
        self.page.goto(self.live_server_url + ws_url("player_edit", self.team.id, self.player.id))

        self.page.get_by_label("よみがな").fill("イクセイタロウ")
        self.page.get_by_label("よみがな").press("Enter")

        # 更新が通ると選手一覧へ戻る（支配下登録・主将のボタンなら編集画面へ戻る）
        self.page.wait_for_url(f"**{ws_url('player_list', self.team.id)}**")
        self.assertEqual(orm_models.Player.objects.get(id=self.player.id).name_kana, "イクセイタロウ")
        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertEqual((stint.signed_as, stint.promoted_year, stint.number), ("育成", None, "120"))
        self.assertFalse(orm_models.Captaincy.objects.filter(player_id=self.player.id).exists())
