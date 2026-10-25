"""チームの登録画面（W3 T3。#79）。リーグ・球場の画面と門・空のワークスペースは `test_setup_screens.py`。

- 作成（本拠地は空でよい。新しいチームはそのリーグの末尾）・編集・削除が画面から通る。
- 所属リーグと本拠地の選択肢はワークスペースのリーグ・球場だけ。名前の空・長さ・重複はフォームのエラー。
- 所属リーグは試合が1つも無いときだけ変えられる（試合があると欄を出さず、POST しても変わらない）。移す先のリーグの
  支配下選手の登録枠を超えるなら移せない（フォームのエラー）。
- 削除のリンクは消せるチームだけ。消せないチームは理由と「戻る」だけで、削除の確認には外れる担当者の数を出す。
- #100 との接続: 画面で一から作った小さいリーグからは、世界を作れない（日本語のエラーで、何も残らない）。
"""

from django.contrib.auth.models import User

from myapp.infrastructure import orm_models

from ..helpers import (
    make_league,
    make_pennant_world,
    make_stadium,
    make_workspace,
    play_game,
    register_players,
    ws_url,
)
from .base import BaseCase
from .test_setup_screens import DELETE_BUTTON, LEAGUE_BODY, SetupCase
from .test_workspace_isolation import snapshot_all_rows


class TeamScreensTest(SetupCase):
    """チームの登録（W3 T3）。BaseCase のテストチームは本拠地あり・相手チームは本拠地なし、どちらも在籍・試合なし。"""

    def _edit_body(self, **overrides) -> dict:
        return {
            "name": "テストチーム",
            "league": str(self.league.id),
            "home_stadium": str(self.stadium.id),
            "display_order": "0",
            **overrides,
        }

    def test_the_list_shows_the_teams_with_their_league_and_home(self):
        response = self.client.get(ws_url("setup_teams"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row.name for row in response.context["teams"]], ["テストチーム", "相手チーム"])
        self.assertContains(response, "テスト球場")
        self.assertContains(response, f'href="{ws_url("setup_team_edit", self.team.id)}"')

    def test_the_choices_are_the_leagues_and_stadiums_of_the_workspace(self):
        other = make_workspace(name="よそ")
        make_league("よそのリーグ", workspace=other)
        make_stadium("よその球場", workspace=other)

        form = self.client.get(ws_url("setup_teams")).context["form"]

        self.assertEqual(form.fields["league"].choices, [(self.league.id, "テストリーグ")])
        self.assertEqual(form.fields["home_stadium"].choices, [("", "なし"), (self.stadium.id, "テスト球場")])
        self.assertNotIn("display_order", form.fields, "作成はリーグの末尾に入るので表示順の欄を持たない")

    def test_creating_a_team_puts_it_at_the_end_of_its_league(self):
        orm_models.Team.objects.filter(id=self.rival.id).update(display_order=4)

        response = self.client.post(
            ws_url("setup_teams"),
            {"name": "  新チーム  ", "league": self.league.id, "home_stadium": self.stadium.id},
        )

        self.assertRedirects(response, ws_url("setup_teams"))
        team = orm_models.Team.objects.get(name="新チーム")
        self.assertEqual(
            (team.league_id, team.home_stadium_id, team.display_order), (self.league.id, self.stadium.id, 5)
        )

    def test_a_team_without_a_home_stadium_is_allowed(self):
        response = self.client.post(ws_url("setup_teams"), {"name": "流浪のチーム", "league": self.league.id})

        self.assertRedirects(response, ws_url("setup_teams"))
        self.assertIsNone(orm_models.Team.objects.get(name="流浪のチーム").home_stadium_id)

    def test_a_duplicate_name_is_a_form_error(self):
        """チーム名はワークスペースの中で一意（リーグが違っても）。"""
        other = make_league("ほかのリーグ")
        before = snapshot_all_rows()

        response = self.client.post(ws_url("setup_teams"), {"name": "相手チーム", "league": other.id})

        self.assert_form_error(response, "同じ名前のチーム", before)
        self.assertTrue(response.context["form"].errors["name"])

    def test_invalid_names_and_unknown_choices_are_form_errors(self):
        before = snapshot_all_rows()
        cases = {
            "長すぎる名前": ("name", {"name": "あ" * 101, "league": self.league.id}),
            "空の名前": ("name", {"name": "  ", "league": self.league.id}),
            "無いリーグ": ("league", {"name": "新", "league": "999999"}),
            "無い球場": ("home_stadium", {"name": "新", "league": self.league.id, "home_stadium": "999999"}),
        }
        for label, (field, body) in cases.items():
            with self.subTest(label):
                response = self.client.post(ws_url("setup_teams"), body)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertEqual(snapshot_all_rows(), before)

    def test_editing_a_team(self):
        other = make_league("移籍先")
        free = make_stadium("新しい本拠地")
        url = ws_url("setup_team_edit", self.team.id)
        form = self.client.get(url).context["form"]
        self.assertEqual(
            [form[name].value() for name in ("name", "league", "home_stadium", "display_order")],
            ["テストチーム", self.league.id, self.stadium.id, 0],
        )

        response = self.client.post(
            url, self._edit_body(name="改名チーム", league=other.id, home_stadium=free.id, display_order="3")
        )

        self.assertRedirects(response, ws_url("setup_teams"))
        team = orm_models.Team.objects.get(id=self.team.id)
        self.assertEqual(
            (team.name, team.league_id, team.home_stadium_id, team.display_order), ("改名チーム", other.id, free.id, 3)
        )

    def test_clearing_the_home_stadium(self):
        self.client.post(ws_url("setup_team_edit", self.team.id), self._edit_body(home_stadium=""))

        self.assertIsNone(orm_models.Team.objects.get(id=self.team.id).home_stadium_id)

    def test_renaming_to_a_taken_name_is_a_form_error(self):
        before = snapshot_all_rows()

        response = self.client.post(ws_url("setup_team_edit", self.team.id), self._edit_body(name="相手チーム"))

        self.assert_form_error(response, "同じ名前のチーム", before)

    def test_a_team_with_games_has_no_league_field_and_keeps_its_league(self):
        """所属リーグは試合が1つも無いときだけ変えられる（決定 2）。欄を出さず理由を出し、POST で送られても
        変えない。"""
        play_game(self.team, self.rival)
        other = make_league("移籍先")
        url = ws_url("setup_team_edit", self.team.id)

        response = self.client.get(url)
        self.assertNotIn("league", response.context["form"].fields)
        self.assertContains(response, "試合が1試合あるため、所属リーグを変えられません")
        self.assertNotContains(response, 'name="league"')

        response = self.client.post(url, self._edit_body(name="改名だけ", league=other.id))

        self.assertRedirects(response, ws_url("setup_teams"))
        team = orm_models.Team.objects.get(id=self.team.id)
        self.assertEqual((team.name, team.league_id), ("改名だけ", self.league.id))

    def test_moving_over_the_new_leagues_registered_limit_is_a_form_error(self):
        """移す先のリーグの支配下選手の登録枠を超えるチームは移せない（W3 T3）。枠に収まれば移せる。"""
        register_players(self.rival, [("一郎", "1", "投手"), ("二郎", "2", "捕手")])
        small = make_league("小さいリーグ", registered_player_limit=1)
        url = ws_url("setup_team_edit", self.rival.id)
        body = {"name": "相手チーム", "league": small.id, "home_stadium": "", "display_order": "0"}
        before = snapshot_all_rows()

        response = self.client.post(url, body)

        self.assert_form_error(response, "移す先のリーグの支配下選手の登録枠（1人）を超えるため", before)
        self.assertTrue(response.context["form"].errors["league"])

        orm_models.League.objects.filter(id=small.id).update(registered_player_limit=2)
        self.assertRedirects(self.client.post(url, body), ws_url("setup_teams"))
        self.assertEqual(orm_models.Team.objects.get(id=self.rival.id).league_id, small.id)

    def test_the_delete_link_is_only_on_deletable_teams(self):
        register_players(self.team, [("一郎", "1", "投手")])

        content = self.client.get(ws_url("setup_teams")).content.decode()

        self.assertIn(ws_url("setup_team_delete", self.rival.id), content)
        self.assertNotIn(ws_url("setup_team_delete", self.team.id), content, "在籍のあるチームに削除のリンク")

    def test_a_team_with_records_shows_the_reason_and_post_deletes_nothing(self):
        register_players(self.team, [("一郎", "1", "投手")])
        play_game(self.team, self.rival)
        url = ws_url("setup_team_delete", self.team.id)
        response = self.client.get(url)
        self.assertContains(response, "選手の在籍の記録が1件・試合が1試合あるため削除できません")
        self.assertNotContains(response, DELETE_BUTTON)
        before = snapshot_all_rows()

        response = self.client.post(url)

        self.assert_form_error(response, "削除できません", before)
        self.assertNotContains(response, DELETE_BUTTON)

    def test_deleting_an_empty_team_tells_how_many_managers_come_off(self):
        manager = User.objects.create_user(username="manager", password="x")
        self.rival.managers.add(manager)
        url = ws_url("setup_team_delete", self.rival.id)
        response = self.client.get(url)
        self.assertContains(response, DELETE_BUTTON)
        self.assertContains(response, "このチームの担当者1人の割り当ても外れます。")
        self.assertNotContains(self.client.get(ws_url("setup_team_delete", self.team.id)), "担当者")

        response = self.client.post(url)

        self.assertRedirects(response, ws_url("setup_teams"))
        self.assertFalse(orm_models.Team.objects.filter(id=self.rival.id).exists())
        self.assertTrue(orm_models.Team.objects.filter(id=self.team.id).exists())
        self.assertTrue(User.objects.filter(id=manager.id).exists(), "担当者のユーザーは消えない")

    def test_a_pennant_team_id_is_404(self):
        """ペナントの世界の球団は登録画面の範囲（実データ）の外。"""
        world = make_pennant_world("世界", seed=1, start_year=2026)
        world_team = orm_models.Team.objects.create(league=make_league("世界のリーグ", world=world), name="世界の球団")
        before = snapshot_all_rows()
        for name in ("setup_team_edit", "setup_team_delete"):
            with self.subTest(name=name):
                url = ws_url(name, world_team.id)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, self._edit_body(name="改ざん")).status_code, 404)
                self.assertEqual(snapshot_all_rows(), before)

    def test_without_leagues_the_create_form_is_replaced_by_a_guide(self):
        """チームはリーグに属する。リーグの無いワークスペースでは作成フォームを出さず、リーグのタブへ案内する。"""
        empty = make_workspace(name="空")
        self.client.force_login(empty.owner)

        response = self.client.get(ws_url("setup_teams", workspace=empty))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "チームを作るには、先にリーグが要ります。")
        self.assertContains(response, f'href="{ws_url("setup_leagues", workspace=empty)}" class="btn')
        self.assertNotContains(response, 'name="league"')


class WorldFromASmallSetupTest(BaseCase):
    """#100 との接続: 画面で一から作った小さいリーグ（2チーム・各10人）は、試合を組めない名簿なので世界を作れない。

    世界の作成（`create_world` の名簿の検査）が日本語のエラーで止め、世界は何も残さない（作ってから最初の
    「進める」で止まる世界を作らない）。リーグ・チーム・選手の登録は、すべて画面の POST で行う。
    """

    def test_the_world_is_refused_in_japanese_and_nothing_is_left(self):
        empty = make_workspace(name="小さいワークスペース")
        self.client.force_login(empty.owner)
        self.client.post(ws_url("setup_leagues", workspace=empty), {"name": "小さいリーグ", **LEAGUE_BODY})
        league = orm_models.League.objects.get(workspace=empty)
        positions = ["投手", "投手", "捕手", "内野手", "内野手", "内野手", "内野手", "外野手", "外野手", "外野手"]
        for team_name in ("小さい球団", "もうひとつの球団"):
            self.client.post(ws_url("setup_teams", workspace=empty), {"name": team_name, "league": league.id})
            team = orm_models.Team.objects.get(league=league, name=team_name)
            for number, position in enumerate(positions, start=1):
                response = self.client.post(
                    ws_url("player_list", team.id, workspace=empty),
                    {"name": f"{team_name}{number}", "number": str(number), "position": position},
                )
                self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.PlayerStint.objects.filter(team__league=league).count(), 20)
        before = snapshot_all_rows()

        managed = orm_models.Team.objects.get(league=league, name="小さい球団")

        response = self.client.post(
            ws_url("pennant_index", workspace=empty),
            {
                "name": "小さい世界",
                "leagues": [str(league.id)],
                "managed_team": str(managed.id),
                "postseason_format": "NONE",
                "start_year": "2040",
            },
        )

        self.assertEqual(response.status_code, 200, "作れなかった理由を出して同じ画面に戻る")
        self.assertTrue(response.context["form"].is_valid(), "フォームの検証で止まっては、名簿の検査を確かめられない")
        self.assertContains(response, "小さい球団は、試合を組める名簿ではありません。")
        self.assertEqual(snapshot_all_rows(), before, "作れなかった世界の行が残った")
        self.assertFalse(orm_models.PennantWorld.objects.exists())
