"""ほかのワークスペースの id を混ぜて書く経路は、どれも拒まれ、何も保存されない（W2 T5。#78）。

チームの書き込みの判定（`WorkspaceAccess.can_edit_teams`）は、渡したチームがそのワークスペースのチームかどうかを見ない
（持ち主・編集者はどの id でも可）。前の判定（`DjangoTeamPermissionQuery`）は「範囲の外のチームなら偽」を
兼ねていたので、その役目は範囲で絞ったサービスとリポジトリが負う。ここでは A の持ち主が A の key の下で
B の id を混ぜて送り、どれも 404 かフォームのエラー（500 にならない）で、DB のどの行も変わらないことを確かめる。
登録画面（W3）のリーグ・チーム・球場の id も同じ（範囲で絞った参照クエリとリポジトリが `*NotFound` にし、画面は 404。
チームの作成・編集の本文に混ぜたリーグ・球場の id はフォームのエラー）。
"""

import json

from myapp.infrastructure import orm_models
from myapp.presentation.forms import PROFILE_SECTION_MARKER

from ..helpers import GAME_DAY_UNRECORDED, ws_url
from .test_workspace_isolation import YEAR, WorkspaceCase, snapshot_all_rows


class CrossWorkspaceIdsTest(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.workspace.owner)
        self.before = snapshot_all_rows()

    def assert_nothing_changed(self):
        self.assertEqual(snapshot_all_rows(), self.before, "ほかのワークスペースの id を混ぜた書き込みが行を変えた")

    # --- 試合 ---

    def test_creating_a_game_with_b_teams_is_a_form_error(self):
        pairs = {
            "ホームが B": (self.b_team.id, self.rival.id),
            "ビジターが B": (self.team.id, self.b_rival.id),
            "両方が B": (self.b_team.id, self.b_rival.id),
        }
        for label, (home, away) in pairs.items():
            with self.subTest(label):
                response = self.client.post(
                    ws_url("game_create"),
                    {"year": str(YEAR), "played_on": f"{YEAR}-05-01", "home_team": home, "away_team": away},
                )
                self.assertEqual(response.status_code, 200, "フォームのエラーとして同じ画面に戻る")
                self.assert_nothing_changed()

    def _scorebook(self, game_id, **overrides):
        payload = {
            **GAME_DAY_UNRECORDED,
            "year": YEAR,
            "played_on": f"{YEAR}-06-01",
            "home_team": self.team.id,
            "away_team": self.rival.id,
            "lineup": [],
            "plate_appearances": [],
            **overrides,
        }
        return self.client.post(
            ws_url("api_game_scorebook", game_id), data=json.dumps(payload), content_type="application/json"
        )

    def test_the_scorebook_rejects_b_ids(self):
        b_batter = self.b_batters[0]
        a_batter = self.a_batters[0]
        cases = {
            "B の試合": (self.b_game_ids[0], {}, 404),
            "A の試合のホームを B のチームに": (self.a_game_id, {"home_team": self.b_team.id}, 400),
            "A の試合を B のチームどうしに": (
                self.a_game_id,
                {"home_team": self.b_team.id, "away_team": self.b_rival.id},
                400,
            ),
            "打順に B の選手": (
                self.a_game_id,
                {
                    "lineup": [
                        {
                            "team_id": self.rival.id,
                            "player_id": b_batter,
                            "batting_order": 1,
                            "slot_sequence": 0,
                            "fielding_position": "指",
                        }
                    ]
                },
                400,
            ),
            "打順の行のチームが B": (
                self.a_game_id,
                {
                    "lineup": [
                        {
                            "team_id": self.b_team.id,
                            "player_id": a_batter,
                            "batting_order": 1,
                            "slot_sequence": 0,
                            "fielding_position": "指",
                        }
                    ]
                },
                400,
            ),
        }
        for label, (game_id, overrides, status) in cases.items():
            with self.subTest(label):
                response = self._scorebook(game_id, **overrides)
                self.assertEqual(response.status_code, status, response.content.decode())
                self.assertFalse(response.json()["ok"])
                self.assert_nothing_changed()

    # --- 選手 ---

    def test_registering_a_player_to_a_b_team_is_404(self):
        response = self.client.post(
            ws_url("player_list", self.b_team.id), {"name": "侵入", "number": "99", "position": "内野手"}
        )
        self.assertEqual(response.status_code, 404)
        self.assert_nothing_changed()

    def test_every_player_edit_button_with_b_ids_is_404(self):
        buttons = {
            "編集": {"name": "改ざん", "number": "99", "position": "内野手"},
            "プロフィールの編集": {
                "name": "改ざん",
                "number": "99",
                "position": "内野手",
                PROFILE_SECTION_MARKER: "1",
                "birthplace": "改ざん",
                "height_cm": "180",
            },
            "退団": {"retire": "1"},
            "主将の指名": {"appoint_captain": "1"},
            "主将の解任": {"remove_captain": "1"},
            "支配下登録": {"promote": "1", "promote_number": "98"},
            "FA 宣言": {"declare_fa": "1", "fa_year": str(YEAR), "fa_kind": "国内"},
            "FA 宣言の取り消し": {"remove_fa": str(YEAR)},
        }
        targets = {
            "B のチームと B の選手": (self.b_team.id, self.b_batters[0]),
            "A のチームと B の選手": (self.team.id, self.b_batters[0]),
            "B のチームと A の選手": (self.b_team.id, self.a_batters[0]),
        }
        for target, (team_id, player_id) in targets.items():
            for button, data in buttons.items():
                with self.subTest(target=target, button=button):
                    response = self.client.post(ws_url("player_edit", team_id, player_id), data)
                    self.assertEqual(response.status_code, 404)
                    self.assert_nothing_changed()

    # --- 戦力分析 ---

    def test_the_analysis_entry_ignores_a_b_team(self):
        """`?team=` に B のチームを渡しても、B へは行かず A の先頭の球団へ落ちる（不正な値は既定に落とす）。"""
        response = self.client.get(ws_url("analysis_index"), {"team": self.b_team.id})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], ws_url("team_analysis", self.team.id))

    # --- 登録画面（リーグ・球場。W3 T2。#79） ---

    def test_the_setup_pages_with_b_league_and_stadium_ids_are_404(self):
        """A の持ち主が A の key の下で B のリーグ・球場の id を開く・送る。どれも 404 で、何も変わらない。"""
        league_body = {
            "name": "改ざん",
            "display_order": "0",
            "foreign_active": "4",
            "foreign_pitchers": "3",
            "foreign_batters": "3",
            "registered_player_limit": "70",
        }
        targets = {
            "リーグの編集": (ws_url("setup_league_edit", self.b_league.id), league_body),
            "リーグの削除": (ws_url("setup_league_delete", self.b_league.id), {}),
            "球場の編集": (ws_url("setup_stadium_edit", self.b_stadium.id), {"name": "改ざん", "capacity": "1"}),
            "球場の削除": (ws_url("setup_stadium_delete", self.b_stadium.id), {}),
        }
        for label, (url, body) in targets.items():
            with self.subTest(label):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, body).status_code, 404)
                self.assert_nothing_changed()

    def test_the_setup_lists_do_not_show_b_rows(self):
        for name in ("setup_leagues", "setup_stadiums"):
            with self.subTest(name=name):
                response = self.client.get(ws_url(name))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, self.b_league.name)
                self.assertNotContains(response, self.b_stadium.name)

    # --- 登録画面（チーム。W3 T3。#79） ---

    def test_creating_or_editing_a_team_with_b_league_or_stadium_is_a_form_error(self):
        """A のチームの作成・編集に B のリーグ・B の球場の id を混ぜると、フォームのエラー（200）で行は変わらない。

        編集は所属リーグを変えられる（試合の無い）チームで確かめる（試合のあるチームは所属リーグの欄を読まない）。
        """
        fresh = orm_models.Team.objects.create(league=self.league, name="試合の無いチーム")
        self.before = snapshot_all_rows()
        edit = ws_url("setup_team_edit", fresh.id)
        cases = {
            "作成に B のリーグ": (ws_url("setup_teams"), {"name": "新", "league": self.b_league.id}),
            "作成に B の球場": (
                ws_url("setup_teams"),
                {"name": "新", "league": self.league.id, "home_stadium": self.b_stadium.id},
            ),
            "編集で B のリーグへ": (edit, {"name": "改", "league": self.b_league.id, "display_order": "0"}),
            "編集で B の球場へ": (
                edit,
                {"name": "改", "league": self.league.id, "home_stadium": self.b_stadium.id, "display_order": "0"},
            ),
        }
        for label, (url, body) in cases.items():
            with self.subTest(label):
                response = self.client.post(url, body)
                self.assertEqual(response.status_code, 200, "フォームのエラーとして同じ画面に戻る")
                self.assertTrue(response.context["form"].errors)
                self.assert_nothing_changed()

    def test_the_team_setup_pages_with_b_team_ids_are_404(self):
        body = {"name": "改ざん", "league": self.league.id, "display_order": "0"}
        for name in ("setup_team_edit", "setup_team_delete"):
            with self.subTest(name=name):
                url = ws_url(name, self.b_team.id)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, body).status_code, 404)
                self.assert_nothing_changed()

    def test_the_team_setup_list_does_not_offer_b_rows(self):
        response = self.client.get(ws_url("setup_teams"))
        self.assertEqual(response.status_code, 200)
        for text in (self.b_team.name, self.b_league.name, self.b_stadium.name):
            self.assertNotContains(response, text)
