"""管理画面のトップの、運営向けの全ワークスペースの概況（W2 T2。`admin.operator_overview`）。

数字は全ワークスペースの実データを数え、ペナントの世界の行は数えない。リンク先の一覧（管理画面は全ワークスペースの
実データ。決定 2026-10-07 Q4）と件数がそろう。クエリ数はワークスペースの数によらない。
"""

from django.contrib.auth.models import User
from django.test import TestCase

from myapp.admin import OPERATOR_TOP_WORKSPACES, operator_overview
from myapp.domain.workspace import Visibility
from myapp.infrastructure import orm_models

from ..helpers import make_league, make_pennant_world, make_workspace, play_game, real_scope, world_scope

# 概況を作るクエリの数（ワークスペースの数・公開範囲の内訳・ユーザー数・リーグ・チーム・選手・試合・上位）
OVERVIEW_QUERIES = 7


def _team(league, name):
    return orm_models.Team.objects.create(league=league, name=name)


def _player(team, name):
    player = orm_models.Player.objects.create(name=name)
    orm_models.PlayerStint.objects.create(player=player, team=team, number="1", from_year=2026)
    return player


def _games(home, away, count, *, scope):
    """未記録の試合を数だけ作る（記録済みにすると記録用の選手が増え、選手の数がずれる）。"""
    for day in range(1, count + 1):
        play_game(home, away, day=day, recorded=False, scope=scope)


class OperatorOverviewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = User.objects.create_superuser(username="root", password="x")
        cls.owner_a = User.objects.create_user(username="owner-a", password="x")
        cls.owner_b = User.objects.create_user(username="owner-b", password="x")
        cls.workspace_a = make_workspace(cls.owner_a, "Aのワークスペース", key="aaaaaaaaaa")
        cls.workspace_b = make_workspace(
            cls.owner_b, "Bのワークスペース", key="bbbbbbbbbb", visibility=Visibility.PRIVATE
        )
        # A: リーグ1・チーム2・選手1・試合1
        league_a = make_league("Aのリーグ", workspace=cls.workspace_a)
        team_a, rival_a = _team(league_a, "Aのチーム"), _team(league_a, "Aの相手")
        _player(team_a, "Aの選手")
        _games(team_a, rival_a, 1, scope=real_scope(cls.workspace_a))
        # B: リーグ2・チーム3・選手2・試合3
        league_b = make_league("Bのリーグ", workspace=cls.workspace_b)
        make_league("Bの空のリーグ", workspace=cls.workspace_b)
        team_b, rival_b = _team(league_b, "Bのチーム"), _team(league_b, "Bの相手")
        _team(league_b, "Bの3つ目")
        _player(team_b, "Bの選手")
        _player(rival_b, "Bの相手の選手")
        _games(team_b, rival_b, 3, scope=real_scope(cls.workspace_b))
        # 在籍の無い選手（管理画面の選手の一覧には出るので数える）
        orm_models.Player.objects.create(name="在籍の無い選手")
        # A のペナントの世界: リーグ・球団・選手・試合はどれも数えない
        world = make_pennant_world("Aの世界", workspace=cls.workspace_a, seed=1, start_year=2026)
        league_w = make_league("世界のリーグ", world=world)
        team_w, rival_w = _team(league_w, "世界の球団"), _team(league_w, "世界の相手")
        _player(team_w, "世界の選手")
        _games(team_w, rival_w, 5, scope=world_scope(world.pk))

    def test_the_rows_of_every_workspace_are_counted(self):
        overview = operator_overview()

        self.assertEqual(overview.workspace_count, 2)
        self.assertEqual(overview.public_workspace_count, 1)
        self.assertEqual(overview.private_workspace_count, 1)
        self.assertEqual(overview.user_count, 3)
        self.assertEqual(overview.league_count, 3)
        self.assertEqual(overview.team_count, 5)
        self.assertEqual(overview.player_count, 4)
        self.assertEqual(overview.game_count, 4)

    def test_the_workspaces_with_more_games_come_first(self):
        rows = operator_overview().top_workspaces

        self.assertEqual(
            [(r.name, r.key, r.owner_name, r.game_count) for r in rows],
            [
                ("Bのワークスペース", "bbbbbbbbbb", "owner-b", 3),
                # 世界の試合（5）は数えない
                ("Aのワークスペース", "aaaaaaaaaa", "owner-a", 1),
            ],
        )
        self.assertEqual([r.workspace_id for r in rows], [self.workspace_b.pk, self.workspace_a.pk])

    def test_only_the_top_workspaces_are_listed(self):
        """上位は試合の多い順に `OPERATOR_TOP_WORKSPACES` 件まで。同数（試合0）は古い順。"""
        extras = [make_workspace(name=f"追加{i}") for i in range(OPERATOR_TOP_WORKSPACES)]

        rows = operator_overview().top_workspaces

        self.assertEqual(len(rows), OPERATOR_TOP_WORKSPACES)
        self.assertEqual(
            [r.workspace_id for r in rows],
            [self.workspace_b.pk, self.workspace_a.pk, *[w.pk for w in extras[: OPERATOR_TOP_WORKSPACES - 2]]],
        )

    def test_the_numbers_match_the_admin_lists(self):
        """概況のリンク先の一覧（全ワークスペースの実データ）と件数がそろう。"""
        self.client.force_login(self.root)
        overview = operator_overview()
        cases = {
            "/admin/myapp/league/": overview.league_count,
            "/admin/myapp/team/": overview.team_count,
            "/admin/myapp/player/": overview.player_count,
            "/admin/myapp/game/": overview.game_count,
            "/admin/myapp/workspace/": overview.workspace_count,
        }
        for url, count in cases.items():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.context["cl"].result_count, count)

    def test_the_query_count_does_not_grow_with_the_workspaces(self):
        with self.assertNumQueries(OVERVIEW_QUERIES):
            operator_overview()

        for i in range(3):
            workspace = make_workspace(name=f"増やした{i}")
            league = make_league(f"増やしたリーグ{i}", workspace=workspace)
            _games(_team(league, f"増やした{i}"), _team(league, f"増やした相手{i}"), 2, scope=real_scope(workspace))

        with self.assertNumQueries(OVERVIEW_QUERIES):
            operator_overview()

    def test_the_admin_index_shows_the_overview(self):
        self.client.force_login(self.root)

        response = self.client.get("/admin/")

        self.assertContains(response, "全ワークスペースの概況")
        self.assertContains(response, "試合の多いワークスペース")
        self.assertContains(response, "<code>bbbbbbbbbb</code>", html=True)
        self.assertContains(response, f"?home_team__league__workspace__id__exact={self.workspace_b.pk}")
        self.assertNotContains(response, "ワークスペースがまだありません")


class OperatorOverviewWithoutWorkspaceTest(TestCase):
    """ワークスペースが1つも無い DB でも、管理画面のトップは開ける（運営者が最初の1つを作る入口）。"""

    def test_everything_is_zero(self):
        overview = operator_overview()

        self.assertEqual(
            (
                overview.workspace_count,
                overview.league_count,
                overview.team_count,
                overview.player_count,
                overview.game_count,
                overview.top_workspaces,
            ),
            (0, 0, 0, 0, 0, []),
        )

    def test_the_admin_index_opens_and_leads_to_the_first_workspace(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "全ワークスペースの概況")
        self.assertContains(response, "ワークスペースがまだありません")
        self.assertContains(response, "/admin/myapp/workspace/add/")
