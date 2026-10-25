"""登録画面（リーグ・球場は W3 T2。#79）と、登録画面の門・空のワークスペース・クエリ数（チームを含む。T3）。

チームの画面そのものは `test_setup_teams.py`。

- 作成・編集・削除が画面から通る（新しいリーグは表示順の末尾）。
- 名前の空・長さ・重複、属性の不正は、フォームのエラー（200）で何も書かない（500 にしない）。
- 削除のリンクは消せる行だけに出す。消せない行の削除の画面は理由と「戻る」だけで、POST しても何も消えない。
- 門: 見るのも書くのも全体の編集権（持ち主・編集者）だけ。チーム担当・閲覧は 403、未ログインはログインへ
  （役割の表は `test_workspace_permissions.py`）。ヘッダーの「登録」とダッシュボードの案内も同じ条件。
- 空のワークスペース: id を取らない画面がどれも開け、持ち主にはダッシュボードに登録の案内が出る。
- 一覧のクエリ数は行数によらない。
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext

from myapp.infrastructure import orm_models
from myapp.presentation import views

from ..helpers import (
    make_editor,
    make_league,
    make_pennant_world,
    make_stadium,
    make_workspace,
    play_game,
    register_players,
    ws_url,
)
from .base import BaseCase
from .test_workspace_isolation import PAGE, URL_TABLE, snapshot_all_rows

LEAGUE_BODY = {
    "foreign_active": "4",
    "foreign_pitchers": "3",
    "foreign_batters": "3",
    "registered_player_limit": "70",
}
# 削除の確認のボタン（消せない行の画面には出ない）
DELETE_BUTTON = '<button type="submit" class="btn btn-danger">削除する</button>'


class SetupCase(BaseCase):
    """既定のワークスペースの持ち主でログインする。BaseCase のリーグはチーム2つ、球場はテストチームの本拠地。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.workspace.owner)

    def assert_form_error(self, response, message: str, before) -> None:
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)
        self.assertEqual(snapshot_all_rows(), before, "フォームのエラーなのに行が変わった")


class LeagueScreensTest(SetupCase):
    def test_the_list_shows_the_leagues_of_the_workspace(self):
        response = self.client.get(ws_url("setup_leagues"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テストリーグ")
        self.assertContains(response, "計4人・投手3人・野手3人")

    def test_creating_a_league_puts_it_at_the_end(self):
        orm_models.League.objects.filter(id=self.league.id).update(display_order=5)

        response = self.client.post(ws_url("setup_leagues"), {"name": "  新リーグ  ", **LEAGUE_BODY})

        self.assertRedirects(response, ws_url("setup_leagues"))
        league = orm_models.League.objects.get(name="新リーグ")
        self.assertEqual(league.workspace_id, self.workspace.id)
        self.assertEqual(league.display_order, 6)
        self.assertEqual(
            (league.foreign_active_limit, league.foreign_active_pitcher_limit, league.foreign_active_batter_limit),
            (4, 3, 3),
        )
        self.assertEqual(league.registered_player_limit, 70)

    def test_blank_limits_mean_unlimited(self):
        self.client.post(ws_url("setup_leagues"), {"name": "無制限リーグ"})

        league = orm_models.League.objects.get(name="無制限リーグ")
        self.assertIsNone(league.foreign_active_limit)
        self.assertIsNone(league.registered_player_limit)

    def test_the_create_form_starts_with_the_npb_defaults(self):
        form = self.client.get(ws_url("setup_leagues")).context["form"]

        self.assertEqual(
            [form[name].value() for name in LEAGUE_BODY],
            [4, 3, 3, 70],
        )
        self.assertNotIn("display_order", form.fields, "作成は末尾に入るので表示順の欄を持たない")

    def test_a_duplicate_name_is_a_form_error(self):
        before = snapshot_all_rows()

        response = self.client.post(ws_url("setup_leagues"), {"name": "テストリーグ", **LEAGUE_BODY})

        self.assert_form_error(response, "同じ名前のリーグ", before)

    def test_the_same_name_as_another_workspaces_league_is_allowed(self):
        make_league("よそのリーグ", workspace=make_workspace(name="よそ"))

        response = self.client.post(ws_url("setup_leagues"), {"name": "よそのリーグ", **LEAGUE_BODY})

        self.assertRedirects(response, ws_url("setup_leagues"))
        self.assertTrue(orm_models.League.objects.filter(workspace=self.workspace, name="よそのリーグ").exists())

    def test_blank_and_too_long_names_are_form_errors(self):
        before = snapshot_all_rows()
        for name in ("", "   ", "あ" * 51):
            with self.subTest(name=name):
                response = self.client.post(ws_url("setup_leagues"), {"name": name, **LEAGUE_BODY})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors["name"])
                self.assertEqual(snapshot_all_rows(), before)

    def test_negative_and_non_numeric_limits_are_form_errors(self):
        before = snapshot_all_rows()
        for field, value in (("foreign_active", "-1"), ("registered_player_limit", "abc"), ("foreign_batters", "1.5")):
            with self.subTest(field=field):
                body = {"name": "新リーグ", **LEAGUE_BODY, field: value}
                response = self.client.post(ws_url("setup_leagues"), body)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertEqual(snapshot_all_rows(), before)

    def test_editing_a_league(self):
        url = ws_url("setup_league_edit", self.league.id)
        orm_models.League.objects.filter(id=self.league.id).update(display_order=3)
        form = self.client.get(url).context["form"]
        self.assertNotIn("display_order", form.fields, "表示順は一覧のドラッグで決める（W3 T8）")
        self.assertNotContains(self.client.get(url), "表示順")
        self.assertEqual(form["name"].value(), "テストリーグ")

        body = {
            "name": "改名リーグ",
            **LEAGUE_BODY,
            "foreign_active": "",
            "registered_player_limit": "65",
        }
        response = self.client.post(url, body)

        self.assertRedirects(response, ws_url("setup_leagues"))
        league = orm_models.League.objects.get(id=self.league.id)
        self.assertEqual((league.name, league.display_order), ("改名リーグ", 3), "編集は表示順を変えない")
        self.assertIsNone(league.foreign_active_limit)
        self.assertEqual(league.registered_player_limit, 65)

    def test_renaming_to_a_taken_name_is_a_form_error(self):
        make_league("ほかのリーグ")
        before = snapshot_all_rows()

        response = self.client.post(
            ws_url("setup_league_edit", self.league.id), {"name": "ほかのリーグ", **LEAGUE_BODY}
        )

        self.assert_form_error(response, "同じ名前のリーグ", before)

    def test_the_delete_link_is_only_on_deletable_leagues(self):
        empty = make_league("空のリーグ")

        content = self.client.get(ws_url("setup_leagues")).content.decode()

        self.assertIn(ws_url("setup_league_delete", empty.id), content)
        self.assertNotIn(ws_url("setup_league_delete", self.league.id), content, "チームのあるリーグに削除のリンク")

    def test_a_league_with_teams_shows_the_reason_and_back_only(self):
        response = self.client.get(ws_url("setup_league_delete", self.league.id))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "チームが2つあるため削除できません")
        self.assertNotContains(response, DELETE_BUTTON)
        self.assertContains(response, f'href="{ws_url("setup_leagues")}"')

    def test_posting_to_delete_a_league_with_teams_deletes_nothing(self):
        before = snapshot_all_rows()

        response = self.client.post(ws_url("setup_league_delete", self.league.id))

        self.assert_form_error(response, "チームが2つあるため削除できません", before)
        self.assertNotContains(response, DELETE_BUTTON)

    def test_deleting_an_empty_league(self):
        empty = make_league("空のリーグ")
        url = ws_url("setup_league_delete", empty.id)
        self.assertContains(self.client.get(url), DELETE_BUTTON)

        response = self.client.post(url)

        self.assertRedirects(response, ws_url("setup_leagues"))
        self.assertFalse(orm_models.League.objects.filter(id=empty.id).exists())
        self.assertTrue(orm_models.League.objects.filter(id=self.league.id).exists())

    def test_a_pennant_league_id_is_404(self):
        """ペナントの世界のリーグは登録画面の範囲（実データ）の外。"""
        world = make_pennant_world("世界", seed=1, start_year=2026)
        world_league = make_league("世界のリーグ", world=world)
        before = snapshot_all_rows()
        for name in ("setup_league_edit", "setup_league_delete"):
            with self.subTest(name=name):
                url = ws_url(name, world_league.id)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, {"name": "改ざん"}).status_code, 404)
                self.assertEqual(snapshot_all_rows(), before)


class StadiumScreensTest(SetupCase):
    def test_creating_a_stadium(self):
        body = {
            "name": "新球場",
            "city": "大阪",
            "capacity": "36000",
            "surface": "人工芝",
            "roof": "ドーム",
            "opened_year": "1997",
        }

        response = self.client.post(ws_url("setup_stadiums"), body)

        self.assertRedirects(response, ws_url("setup_stadiums"))
        stadium = orm_models.Stadium.objects.get(name="新球場")
        self.assertEqual(stadium.workspace_id, self.workspace.id)
        self.assertEqual(
            (stadium.city, stadium.capacity, stadium.surface, stadium.roof, stadium.opened_year),
            ("大阪", 36000, "人工芝", "ドーム", 1997),
        )

    def test_a_stadium_with_only_a_name(self):
        self.client.post(ws_url("setup_stadiums"), {"name": "名前だけ"})

        stadium = orm_models.Stadium.objects.get(name="名前だけ")
        self.assertEqual((stadium.city, stadium.capacity, stadium.surface, stadium.roof), ("", None, "", ""))

    def test_the_choices_come_from_the_domain(self):
        form = self.client.get(ws_url("setup_stadiums")).context["form"]

        self.assertEqual([value for value, _ in form.fields["surface"].choices], ["", "天然芝", "人工芝", "土"])
        self.assertEqual([value for value, _ in form.fields["roof"].choices], ["", "屋外", "ドーム", "開閉式屋根"])

    def test_invalid_values_are_form_errors(self):
        before = snapshot_all_rows()
        cases = {
            "capacity": "-1",
            "surface": "砂",
            "roof": "テント",
            "opened_year": "1800",
            "city": "あ" * 101,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                response = self.client.post(ws_url("setup_stadiums"), {"name": "新球場", field: value})
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertEqual(snapshot_all_rows(), before)

    def test_a_duplicate_name_is_a_form_error(self):
        before = snapshot_all_rows()

        response = self.client.post(ws_url("setup_stadiums"), {"name": "テスト球場"})

        self.assert_form_error(response, "同じ名前の球場", before)

    def test_editing_a_stadium(self):
        url = ws_url("setup_stadium_edit", self.stadium.id)
        form = self.client.get(url).context["form"]
        self.assertEqual((form["name"].value(), form["city"].value()), ("テスト球場", "東京"))

        response = self.client.post(url, {"name": "改名球場", "city": "横浜", "capacity": "34000", "roof": "屋外"})

        self.assertRedirects(response, ws_url("setup_stadiums"))
        stadium = orm_models.Stadium.objects.get(id=self.stadium.id)
        self.assertEqual(
            (stadium.name, stadium.city, stadium.capacity, stadium.roof), ("改名球場", "横浜", 34000, "屋外")
        )

    def test_renaming_to_a_taken_name_is_a_form_error(self):
        make_stadium("ほかの球場")
        before = snapshot_all_rows()

        response = self.client.post(ws_url("setup_stadium_edit", self.stadium.id), {"name": "ほかの球場"})

        self.assert_form_error(response, "同じ名前の球場", before)

    def test_the_delete_link_is_only_on_stadiums_no_team_calls_home(self):
        free = make_stadium("空き球場")

        content = self.client.get(ws_url("setup_stadiums")).content.decode()

        self.assertIn(ws_url("setup_stadium_delete", free.id), content)
        self.assertNotIn(ws_url("setup_stadium_delete", self.stadium.id), content)

    def test_a_home_stadium_shows_the_reason_and_post_deletes_nothing(self):
        url = ws_url("setup_stadium_delete", self.stadium.id)
        response = self.client.get(url)
        self.assertContains(response, "本拠地にしているチーム（1チーム）があるため削除できません")
        self.assertNotContains(response, DELETE_BUTTON)
        before = snapshot_all_rows()

        response = self.client.post(url)

        self.assert_form_error(response, "削除できません", before)

    def test_a_stadium_only_a_world_team_calls_home_cannot_be_deleted(self):
        """球場はペナントの世界と共有する。世界の球団だけが本拠地にしている球場も消せず、編集の画面は世界に映ると知らせる。"""
        shared = make_stadium("共有の球場")
        world = make_pennant_world("世界", seed=1, start_year=2026)
        world_league = make_league("世界のリーグ", world=world)
        orm_models.Team.objects.create(league=world_league, name="世界の球団", home_stadium=shared)

        content = self.client.get(ws_url("setup_stadiums")).content.decode()
        self.assertNotIn(ws_url("setup_stadium_delete", shared.id), content)
        self.assertContains(self.client.get(ws_url("setup_stadium_edit", shared.id)), "変更は世界にも映ります")
        before = snapshot_all_rows()

        response = self.client.post(ws_url("setup_stadium_delete", shared.id))

        self.assert_form_error(response, "ペナントの世界の1球団", before)

    def test_deleting_a_free_stadium(self):
        free = make_stadium("空き球場")
        url = ws_url("setup_stadium_delete", free.id)
        self.assertContains(self.client.get(url), DELETE_BUTTON)

        response = self.client.post(url)

        self.assertRedirects(response, ws_url("setup_stadiums"))
        self.assertFalse(orm_models.Stadium.objects.filter(id=free.id).exists())
        self.assertTrue(orm_models.Stadium.objects.filter(id=self.stadium.id).exists())


SETUP_PAGES = ("setup_leagues", "setup_teams", "setup_stadiums")


class SetupGateTest(BaseCase):
    """門と導線。役割ごとの表は `test_workspace_permissions.py`。ここは画面ごとの細部。"""

    def setUp(self):
        super().setUp()
        self.manager = User.objects.create_user(username="manager", password="x")
        self.team.managers.add(self.manager)
        self.editor = make_editor(User.objects.create_user(username="editor", password="x"))

    def _all_setup_urls(self) -> list[str]:
        return [
            ws_url("setup_leagues"),
            ws_url("setup_league_edit", self.league.id),
            ws_url("setup_league_delete", self.league.id),
            ws_url("setup_teams"),
            ws_url("setup_team_edit", self.team.id),
            ws_url("setup_team_delete", self.team.id),
            ws_url("setup_stadiums"),
            ws_url("setup_stadium_edit", self.stadium.id),
            ws_url("setup_stadium_delete", self.stadium.id),
        ]

    def _reorder_bodies(self) -> dict[str, dict]:
        """並べ替え（W3 T8。POST だけ）の URL と、持ち主なら書ける本文（今の並びの逆）。"""
        return {
            ws_url("setup_leagues_reorder"): {"order": [self.league.id]},
            ws_url("setup_teams_reorder", self.league.id): {"order": [self.rival.id, self.team.id]},
        }

    def test_a_manager_gets_403_on_every_setup_url_and_writes_nothing(self):
        self.client.force_login(self.manager)
        before = snapshot_all_rows()
        bodies = {url: {"name": "侵入", **LEAGUE_BODY} for url in self._all_setup_urls()} | self._reorder_bodies()
        for url, body in bodies.items():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                response = self.client.post(url, body)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(snapshot_all_rows(), before)

    def test_the_gate_refuses_before_reading_or_writing(self):
        """門は解決済みの権限だけで判定し、参照クエリもサービスも組み立てる前に拒む。"""
        self.client.force_login(self.manager)
        with (
            patch.object(views, "build_setup_query", side_effect=AssertionError("門の前に読んだ")) as query,
            patch.object(views, "build_league_setup_service", side_effect=AssertionError("門の前に書いた")) as service,
        ):
            for url in [*self._all_setup_urls(), *self._reorder_bodies()]:
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
                    self.assertEqual(self.client.post(url, {"name": "侵入"}).status_code, 403)
        query.assert_not_called()
        service.assert_not_called()

    def test_an_editor_can_open_and_write(self):
        self.client.force_login(self.editor)
        for url in self._all_setup_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(ws_url("setup_stadiums"), {"name": "編集者の球場"})
        self.assertRedirects(response, ws_url("setup_stadiums"))
        url, body = list(self._reorder_bodies().items())[1]
        self.assertRedirects(self.client.post(url, body), ws_url("setup_teams"))
        self.assertEqual(
            [orm_models.Team.objects.get(id=i).display_order for i in (self.rival.id, self.team.id)], [0, 1]
        )

    def test_anonymous_is_sent_to_login(self):
        before = snapshot_all_rows()
        for url, body in self._reorder_bodies().items():
            with self.subTest(url=url):
                response = self.client.post(url, body)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])
                self.assertEqual(snapshot_all_rows(), before)
        for url in [*self._all_setup_urls(), *self._reorder_bodies()]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_the_header_link_follows_the_right(self):
        link = f'href="{ws_url("setup_leagues")}"'
        for user, shown in ((self.workspace.owner, True), (self.editor, True), (self.manager, False), (None, False)):
            with self.subTest(user=str(user)):
                if user is None:
                    self.client.logout()
                else:
                    self.client.force_login(user)
                content = self.client.get(ws_url("team_list")).content.decode()
                self.assertEqual(link in content, shown)


class EmptyWorkspaceTest(BaseCase):
    """リーグも球場も無いワークスペース（管理画面で作ったばかり）。"""

    def setUp(self):
        super().setUp()
        self.empty = make_workspace(name="空のワークスペース")
        self.viewer = User.objects.create_user(username="viewer", password="x")

    def test_every_page_without_an_id_opens(self):
        """id を取らない画面は、どれも空のまま開ける（500 にならない）。

        試合の登録だけは、チームが無いと対戦カードを選べないので、理由を出して試合一覧へ戻す（既存の振る舞い）。
        """
        self.client.force_login(self.empty.owner)
        pages = [name for name, (args, kind, _) in URL_TABLE.items() if kind == PAGE and not args]
        self.assertIn("setup_leagues", pages)
        for name in pages:
            with self.subTest(name=name):
                response = self.client.get(ws_url(name, workspace=self.empty))
                if name == "game_create":
                    self.assertRedirects(response, ws_url("game_list", workspace=self.empty))
                else:
                    self.assertEqual(response.status_code, 200)

    def test_the_dashboard_guides_only_those_who_can_set_up(self):
        guide = f'href="{ws_url("setup_leagues", workspace=self.empty)}" class="btn'
        editor = make_editor(User.objects.create_user(username="editor", password="x"), self.empty)
        for user, shown in ((self.empty.owner, True), (editor, True), (self.viewer, False), (None, False)):
            with self.subTest(user=str(user)):
                if user is None:
                    self.client.logout()
                else:
                    self.client.force_login(user)
                response = self.client.get(ws_url("dashboard", workspace=self.empty))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(guide in response.content.decode(), shown)

    def test_the_team_list_guides_only_those_who_can_set_up(self):
        guide = f'href="{ws_url("setup_teams", workspace=self.empty)}" class="btn'
        for user, shown in ((self.empty.owner, True), (self.viewer, False), (None, False)):
            with self.subTest(user=str(user)):
                if user is None:
                    self.client.logout()
                else:
                    self.client.force_login(user)
                response = self.client.get(ws_url("team_list", workspace=self.empty))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "管理画面から追加してください")
                self.assertEqual(guide in response.content.decode(), shown)

    def test_the_owner_builds_a_league_from_nothing(self):
        self.client.force_login(self.empty.owner)

        self.client.post(ws_url("setup_stadiums", workspace=self.empty), {"name": "はじめての球場"})
        self.client.post(ws_url("setup_leagues", workspace=self.empty), {"name": "はじめてのリーグ", **LEAGUE_BODY})
        league = orm_models.League.objects.get(workspace=self.empty)
        stadium = orm_models.Stadium.objects.get(workspace=self.empty)
        for name, home in (("はじめての球団", stadium.id), ("ふたつめの球団", "")):
            response = self.client.post(
                ws_url("setup_teams", workspace=self.empty), {"name": name, "league": league.id, "home_stadium": home}
            )
            self.assertRedirects(response, ws_url("setup_teams", workspace=self.empty))

        self.assertEqual((league.name, league.display_order), ("はじめてのリーグ", 0))
        self.assertEqual(stadium.name, "はじめての球場")
        self.assertEqual(
            list(
                orm_models.Team.objects.filter(league=league)
                .order_by("display_order")
                .values_list("name", "home_stadium_id", "display_order")
            ),
            [("はじめての球団", stadium.id, 0), ("ふたつめの球団", None, 1)],
        )


class SetupQueryCountTest(SetupCase):
    """一覧のクエリ数は行数によらない（`DjangoSetupQuery` の一覧はそれぞれクエリ1本）。"""

    def _count(self, url: str) -> int:
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(queries)

    def test_the_league_list(self):
        url = ws_url("setup_leagues")
        baseline = self._count(url)
        for i in range(4):
            league = make_league(f"追加リーグ{i}")
            orm_models.Team.objects.create(league=league, name=f"追加チーム{i}")

        with self.assertNumQueries(baseline):
            self.client.get(url)

    def test_the_stadium_list(self):
        url = ws_url("setup_stadiums")
        baseline = self._count(url)
        for i in range(4):
            stadium = make_stadium(f"追加球場{i}")
            orm_models.Team.objects.create(league=self.league, name=f"追加チーム{i}", home_stadium=stadium)

        with self.assertNumQueries(baseline):
            self.client.get(url)

    def test_the_team_list(self):
        """チームの一覧（と作成フォームの選択肢のリーグ・球場）も、チーム・在籍・試合・担当の数によらない。"""
        url = ws_url("setup_teams")
        baseline = self._count(url)
        manager = User.objects.create_user(username="manager", password="x")
        for i in range(4):
            league = make_league(f"追加リーグ{i}")
            team = orm_models.Team.objects.create(
                league=league, name=f"追加チーム{i}", home_stadium=make_stadium(f"追加球場{i}")
            )
            team.managers.add(manager)
            register_players(team, [(f"追加選手{i}", "1", "投手")])
        play_game(self.team, self.rival)

        with self.assertNumQueries(baseline):
            self.client.get(url)

    def test_the_edit_and_delete_pages(self):
        """編集と削除の確認も、行の数（チーム・本拠地・在籍の数）によらない。"""
        urls = [
            ws_url("setup_league_edit", self.league.id),
            ws_url("setup_league_delete", self.league.id),
            ws_url("setup_team_edit", self.team.id),
            ws_url("setup_team_delete", self.team.id),
            ws_url("setup_stadium_edit", self.stadium.id),
            ws_url("setup_stadium_delete", self.stadium.id),
        ]
        baselines = [self._count(url) for url in urls]
        register_players(self.team, [("追加選手", "1", "投手")])
        for i in range(4):
            orm_models.Team.objects.create(league=self.league, name=f"追加チーム{i}", home_stadium=self.stadium)

        for url, baseline in zip(urls, baselines, strict=True):
            with self.subTest(url=url), self.assertNumQueries(baseline):
                self.client.get(url)
