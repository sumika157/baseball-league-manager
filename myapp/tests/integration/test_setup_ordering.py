"""登録画面の一覧の並べ替え（W3 T8。#79）。

2026-10-25 ユーザー決定: リーグ・チームの表示順は登録画面の一覧のドラッグだけで決める（編集画面の表示順の欄は消した）。
ドラッグそのもの（setup-sortable.js）は e2e `test_setup_ordering.py` が確かめる。ここは画面の HTML と並べ替えの POST。

- 一覧の行は識別子（`data-order-id`）を持ち、保存のフォームは今の並びの識別子を隠し欄 `order` に持って `hidden` で出る
  （JS が動かなければ並べ替えの導線は見えず、一覧は読める）。
- POST は識別子の並び。範囲の行の集まりとちょうど一致しない並び（欠け・余り・重複・ほかのリーグやワークスペースの id・
  数でない値・空）は、何も書かずに一覧を理由つきで出し直す（200）。範囲の外のリーグの URL は 404。GET は一覧へ戻す。
- 書き込みのクエリ数は行数によらない。
"""

import re

from django.db import connection
from django.test.utils import CaptureQueriesContext

from myapp.domain.setup import ORDER_MISMATCH
from myapp.infrastructure import orm_models

from ..helpers import make_league, make_pennant_world, make_workspace, ws_url
from .test_setup_screens import SetupCase
from .test_workspace_isolation import snapshot_all_rows


class SetupOrderingTest(SetupCase):
    """BaseCase のリーグ（テストチーム・相手チーム）に、リーグ2つとチーム1つを足す。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second = make_league("2番目のリーグ", display_order=1)
        cls.third = make_league("3番目のリーグ", display_order=2)
        orm_models.Team.objects.filter(id=cls.rival.id).update(display_order=1)
        cls.third_team = orm_models.Team.objects.create(league=cls.league, name="3番目のチーム", display_order=2)
        cls.elsewhere = orm_models.Team.objects.create(league=cls.second, name="別リーグのチーム")
        cls.b_workspace = make_workspace(name="B")
        cls.b_league = make_league("B のリーグ", workspace=cls.b_workspace)
        cls.b_team = orm_models.Team.objects.create(league=cls.b_league, name="B のチーム")

    def _league_order(self) -> list[int]:
        return list(
            orm_models.League.objects.filter(workspace=self.workspace)
            .order_by("display_order", "name")
            .values_list("id", flat=True)
        )

    def _team_order(self, league_id: int) -> list[int]:
        return list(
            orm_models.Team.objects.filter(league_id=league_id)
            .order_by("display_order", "name")
            .values_list("id", flat=True)
        )

    # --- 一覧の HTML ---

    def test_the_league_list_carries_the_ids_and_a_hidden_save_form(self):
        content = self.client.get(ws_url("setup_leagues")).content.decode()

        ids = [self.league.id, self.second.id, self.third.id]
        rows = [content.index(f'data-order-id="{i}"') for i in ids]
        self.assertEqual(rows, sorted(rows), "行は表示順に並ぶ")
        action = f'action="{ws_url("setup_leagues_reorder")}"'
        self.assertIn(action, content)
        form = content[content.index(action) : content.index("</form>", content.index(action))]
        self.assertIn("hidden>", form.split("\n")[0], "保存のフォームは JS が出すまで隠れている")
        self.assertEqual([int(value) for value in re.findall(r'name="order" value="(\d+)"', form)], ids)
        self.assertIn("myapp/js/setup-sortable.js", content)
        self.assertNotIn(">表示順<", content, "表示順の数値の列は出さない（並びが表示順）")

    def test_the_team_list_has_one_save_form_per_league(self):
        content = self.client.get(ws_url("setup_teams")).content.decode()

        for league_id, team_ids in (
            (self.league.id, [self.team.id, self.rival.id, self.third_team.id]),
            (self.second.id, [self.elsewhere.id]),
        ):
            with self.subTest(league=league_id):
                action = f'action="{ws_url("setup_teams_reorder", league_id)}"'
                self.assertIn(action, content)
                form = content[content.index(action) : content.index("</form>", content.index(action))]
                values = re.findall(r'name="order" value="(\d+)"', form)
                self.assertEqual([int(v) for v in values], team_ids)

    # --- 並べ替えの POST ---

    def test_reordering_the_leagues(self):
        order = [self.third.id, self.league.id, self.second.id]

        response = self.client.post(ws_url("setup_leagues_reorder"), {"order": order}, follow=True)

        self.assertRedirects(response, ws_url("setup_leagues"))
        self.assertEqual(self._league_order(), order)
        self.assertEqual(
            list(
                orm_models.League.objects.filter(id__in=order)
                .order_by("display_order")
                .values_list("display_order", flat=True)
            ),
            [0, 1, 2],
        )
        self.assertContains(response, "リーグの表示順を保存しました。")

    def test_reordering_the_teams_of_a_league(self):
        order = [self.third_team.id, self.team.id, self.rival.id]
        elsewhere = orm_models.Team.objects.filter(id=self.elsewhere.id).values().get()

        response = self.client.post(ws_url("setup_teams_reorder", self.league.id), {"order": order})

        self.assertRedirects(response, ws_url("setup_teams"))
        self.assertEqual(self._team_order(self.league.id), order)
        self.assertEqual(orm_models.Team.objects.filter(id=self.elsewhere.id).values().get(), elsewhere)

    def test_an_order_that_does_not_match_writes_nothing_and_shows_the_reason(self):
        leagues = [self.league.id, self.second.id, self.third.id]
        teams = [self.team.id, self.rival.id, self.third_team.id]
        league_url = ws_url("setup_leagues_reorder")
        team_url = ws_url("setup_teams_reorder", self.league.id)
        cases = {
            "リーグの欠け": (league_url, {"order": leagues[1:]}),
            "リーグの余り（B のリーグ）": (league_url, {"order": [*leagues, self.b_league.id]}),
            "リーグの重複": (league_url, {"order": [*leagues, leagues[0]]}),
            "数でない値": (league_url, {"order": [*leagues[:2], "x"]}),
            "本文なし": (league_url, {}),
            "チームの欠け": (team_url, {"order": teams[:2]}),
            "チームの余り（別リーグのチーム）": (team_url, {"order": [*teams, self.elsewhere.id]}),
            "チームの入れ替え（B のチーム）": (team_url, {"order": [*teams[:2], self.b_team.id]}),
            "ほかのリーグのチームだけ": (team_url, {"order": [self.elsewhere.id]}),
        }
        before = snapshot_all_rows()
        for label, (url, body) in cases.items():
            with self.subTest(label):
                response = self.client.post(url, body)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, ORDER_MISMATCH)
                self.assertNotContains(response, "表示順を保存しました")
                self.assertEqual(snapshot_all_rows(), before, "合わない並べ替えで行が変わった")

    def test_the_league_in_the_url_must_be_in_the_workspace(self):
        """B のリーグ・ペナントの世界のリーグは 404（範囲の外）。何も変わらない。"""
        world = make_pennant_world("世界", seed=1, start_year=2026)
        world_league = make_league("世界のリーグ", world=world)
        world_team = orm_models.Team.objects.create(league=world_league, name="世界の球団")
        before = snapshot_all_rows()
        for league_id, order in ((self.b_league.id, [self.b_team.id]), (world_league.id, [world_team.id])):
            with self.subTest(league=league_id):
                url = ws_url("setup_teams_reorder", league_id)
                self.assertEqual(self.client.post(url, {"order": order}).status_code, 404)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(snapshot_all_rows(), before)

    def test_get_goes_back_to_the_list(self):
        before = snapshot_all_rows()
        self.assertRedirects(self.client.get(ws_url("setup_leagues_reorder")), ws_url("setup_leagues"))
        self.assertRedirects(self.client.get(ws_url("setup_teams_reorder", self.league.id)), ws_url("setup_teams"))
        self.assertEqual(snapshot_all_rows(), before)

    def test_the_write_takes_the_same_queries_whatever_the_rows(self):
        def post_reversed() -> int:
            leagues = self._league_order()[::-1]
            teams = self._team_order(self.league.id)[::-1]
            with CaptureQueriesContext(connection) as captured:
                self.client.post(ws_url("setup_leagues_reorder"), {"order": leagues})
                self.client.post(ws_url("setup_teams_reorder", self.league.id), {"order": teams})
            return len(captured)

        baseline = post_reversed()
        for number in range(4):
            make_league(f"追加リーグ{number}")
            orm_models.Team.objects.create(league=self.league, name=f"追加チーム{number}")
        self.assertEqual(post_reversed(), baseline)
