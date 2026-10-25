"""登録画面の一覧の行をドラッグして表示順を並べ替える（W3 T8。#79）。

setup-sortable.js（HTML5 のドラッグ＆ドロップ）が実ブラウザで動き、保存のフォームが識別子の並びを送って表示順が
変わることを確かめる。並べ替えの POST の分岐（合わない並び・範囲の外の id・権限）は integration
（`test_setup_ordering.py` ほか）で見ている。
"""

from django.contrib.auth.models import User

from myapp.infrastructure import orm_models

from ..helpers import make_league, make_workspace, ws_url
from .base import PlaywrightTestCase


class SetupOrderingTest(PlaywrightTestCase):
    def setUp(self):
        super().setUp()
        owner = User.objects.create_user(username="owner", password="pass12345")
        self.workspace = make_workspace(owner=owner, name="並べ替えのワークスペース")
        self.leagues = [
            make_league(name, workspace=self.workspace, display_order=order)
            for order, name in enumerate(("一のリーグ", "二のリーグ", "三のリーグ"))
        ]
        self.teams = [
            orm_models.Team.objects.create(league=self.leagues[0], name=name, display_order=order)
            for order, name in enumerate(("甲チーム", "乙チーム", "丙チーム"))
        ]
        # ほかのリーグのチーム（並べ替えても変わらない）
        self.elsewhere = orm_models.Team.objects.create(
            league=self.leagues[1], name="別リーグのチーム", display_order=5
        )
        self._login()

    def _login(self):
        self.page.goto(self.live_server_url + "/accounts/login/")
        self.page.fill('input[name="username"]', "owner")
        self.page.fill('input[name="password"]', "pass12345")
        self.page.locator('input[name="password"]').press("Enter")
        self.page.wait_for_url(f"{self.live_server_url}/")

    def _url(self, name: str, *args: object) -> str:
        return self.live_server_url + ws_url(name, *args, workspace=self.workspace)

    def _row(self, row_id: int):
        return self.page.locator(f'tr[data-order-id="{row_id}"]')

    def test_dragging_a_league_row_and_saving_changes_the_order(self):
        self.page.goto(self._url("setup_leagues"))
        save = self.page.locator("#league-order")
        self.assertTrue(self.page.locator("#league-order-hint").is_visible(), "JS が動けば案内が出る")
        self.assertFalse(save.is_visible(), "並びを変えるまで保存のボタンは出ない")

        first, _, third = self.leagues
        self._row(third.id).drag_to(self._row(first.id))

        self.assertTrue(save.is_visible())
        save.locator('button:has-text("並び順を保存する")').click()
        self.page.wait_for_url(self._url("setup_leagues"))
        self.assertIn("リーグの表示順を保存しました。", self.page.content())
        orders = orm_models.League.objects.filter(workspace=self.workspace).order_by("display_order")
        self.assertEqual(list(orders.values_list("name", flat=True)), ["三のリーグ", "一のリーグ", "二のリーグ"])
        self.assertEqual(list(orders.values_list("display_order", flat=True)), [0, 1, 2])

    def test_dragging_a_team_row_inside_its_league(self):
        self.page.goto(self._url("setup_teams"))
        league = self.leagues[0]
        save = self.page.locator(f"#team-order-{league.id}")
        self.assertFalse(save.is_visible())

        first, second, _ = self.teams
        self._row(first.id).drag_to(self._row(second.id))
        # ほかのリーグの行へは落とせない（ドラッグしても並びが変わらず、そのリーグの保存のボタンも出ない）
        self._row(self.teams[2].id).drag_to(self._row(self.elsewhere.id))
        self.assertFalse(self.page.locator(f"#team-order-{self.leagues[1].id}").is_visible())

        self.assertTrue(save.is_visible())
        save.locator('button:has-text("並び順を保存する")').click()
        self.page.wait_for_url(self._url("setup_teams"))
        orders = orm_models.Team.objects.filter(league=league).order_by("display_order")
        self.assertEqual(list(orders.values_list("name", flat=True)), ["乙チーム", "甲チーム", "丙チーム"])
        self.assertEqual(orm_models.Team.objects.get(id=self.elsewhere.id).display_order, 5)

    def test_dragging_back_to_the_first_order_hides_the_save_button(self):
        self.page.goto(self._url("setup_leagues"))
        first, second, _ = self.leagues
        self._row(first.id).drag_to(self._row(second.id))
        self.assertTrue(self.page.locator("#league-order").is_visible())
        self._row(first.id).drag_to(self._row(second.id))
        self.assertFalse(self.page.locator("#league-order").is_visible(), "開いたときの並びに戻れば保存は要らない")
