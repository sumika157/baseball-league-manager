"""作成で入れる見本のリーグ（W5 T2。#81）。

見本の中身の規則（名簿・日程・照合・規定）は domain の `test_sample_league.py` が見る。ここでは保存と画面:

- 見本ありで作ると、1リーグ・球場6・チーム6・選手150（在籍150）・試合36が新しいワークスペースの実データに入り、
  順位表は6チームが各12試合。ダッシュボード・順位表・試合の詳細・試合の編集の画面が開く。
- 試合に出た選手は、その年にそのチームの在籍がある（打撃はその行のチーム、投球はホームかビジター）。
- 見本はほかのワークスペースの行に触れない（全表の行を前後で比べる。増えた行は新しいワークスペースのもの）。
- 見本なしは空のまま。上限なら何も作らない。見本の途中で失敗するとワークスペースも見本も残らず、上限も消費しない
  （作成と同じトランザクション。2026-11-13 ユーザー決定 1）。
- 空でないワークスペースへの `seed` は `InvalidWorkspace` で何も書かない。
- 見本の入ったワークスペースは削除で全部消える。見本のリーグを分岐元にペナントの世界を作れる（W6 の前提）。
"""

from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db.models import Q
from django.test import TestCase
from django.urls import reverse

from myapp.domain.exceptions import InvalidGame, InvalidWorkspace, WorkspaceLimitReached
from myapp.domain.sample.league import (
    SAMPLE_GAME_COUNT,
    SAMPLE_GAMES_PER_TEAM,
    SAMPLE_PLAYERS_PER_TEAM,
    SAMPLE_TEAM_COUNT,
)
from myapp.domain.workspace import Visibility
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository
from myapp.presentation.views import build_pennant_world_service

from ..helpers import (
    build_league_view_service,
    build_workspace_sample_service,
    build_workspace_service,
    make_league,
    make_workspace,
    real_scope,
    ws_url,
)
from .test_workspace_delete import _all_rows, _rows_of


def _create(owner: User, name: str = "見本つき", *, with_sample_league: bool = True) -> orm_models.Workspace:
    key = build_workspace_service().create(
        owner_id=owner.id, name=name, visibility=Visibility.PRIVATE, with_sample_league=with_sample_league
    )
    return orm_models.Workspace.objects.get(key=key)


def _plant_another_workspace() -> orm_models.Workspace:
    """見本と比べるための、中身のあるほかのワークスペース（リーグ・球場・チーム）。"""
    other = make_workspace(name="よそ")
    league = make_league("よそのリーグ", workspace=other)
    stadium = orm_models.Stadium.objects.create(workspace=other, name="よその球場")
    orm_models.Team.objects.create(league=league, name="よその球団", home_stadium=stadium)
    return other


# 見本の期間の前の「今日」。見本の年は前年（`sample_year`）
TODAY = date(2026, 3, 1)
SAMPLE_YEAR = 2025


class SampleLeagueCase(TestCase):
    """見本ありで1つ作っておく（作成は1回あたり約1秒かかるので、読むだけの検査はこれを共有する）。"""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="sample-owner", password="x")
        cls.other = _plant_another_workspace()
        # 「今日」を見本の期間の前（2026年の開幕前）に固定する。見本の年は前年（2025）になり、現実の今日の年と
        # ずれる（`add_player` に加入年を渡し忘れると、加入が現実の今日の年になって在籍の検査で落ちる）
        with patch("myapp.presentation.views.timezone.localdate", return_value=TODAY):
            cls.workspace = _create(cls.owner)
        cls.scope = real_scope(cls.workspace)


class SampleLeagueContentsTest(SampleLeagueCase):
    def test_the_rows_of_the_sample_league(self):
        ws = self.workspace
        teams = orm_models.Team.objects.filter(league__workspace=ws)
        players = orm_models.Player.objects.filter(stints__team__league__workspace=ws).distinct()
        self.assertEqual(orm_models.League.objects.filter(workspace=ws).count(), 1)
        self.assertEqual(orm_models.Stadium.objects.filter(workspace=ws).count(), SAMPLE_TEAM_COUNT)
        self.assertEqual(teams.count(), SAMPLE_TEAM_COUNT)
        self.assertEqual(players.count(), SAMPLE_TEAM_COUNT * SAMPLE_PLAYERS_PER_TEAM)
        self.assertEqual(
            orm_models.PlayerStint.objects.filter(team__in=teams).count(), SAMPLE_TEAM_COUNT * SAMPLE_PLAYERS_PER_TEAM
        )
        self.assertEqual(orm_models.Game.objects.filter(home_team__in=teams).count(), SAMPLE_GAME_COUNT)
        self.assertFalse(teams.filter(home_stadium__isnull=True).exists(), "どのチームにも本拠地がある")
        self.assertTrue(
            orm_models.GamePlateAppearance.objects.filter(game__home_team__in=teams).exists(),
            "試合は打席まで入る（スコアブックで編集できる）",
        )

    def test_the_games_are_of_the_year_before_today(self):
        """試合はすべて「今日」より前（見本の年は `sample_year`。今日を固定したので前年）。"""
        games = orm_models.Game.objects.filter(home_team__league__workspace=self.workspace)
        self.assertEqual(set(games.values_list("year", flat=True)), {SAMPLE_YEAR})
        self.assertLess(max(games.values_list("played_on", flat=True)), TODAY)

    def test_every_team_played_twelve_games_in_the_standings(self):
        standings = build_league_view_service(self.scope).get_standings()

        [league] = standings.leagues
        self.assertEqual(len(league.rows), SAMPLE_TEAM_COUNT)
        self.assertEqual([row.games_played for row in league.rows], [SAMPLE_GAMES_PER_TEAM] * SAMPLE_TEAM_COUNT)

    def test_players_in_a_game_belong_to_their_team_in_that_year(self):
        def stint_covers(year: int) -> Q:
            return Q(stints__from_year__lte=year) & (Q(stints__to_year__isnull=True) | Q(stints__to_year__gte=year))

        games = orm_models.Game.objects.filter(home_team__league__workspace=self.workspace)
        for game in games:
            for line in game.batting_lines.all():
                with self.subTest(game=game.id, batter=line.player_id):
                    self.assertTrue(
                        orm_models.Player.objects.filter(
                            stint_covers(game.year), id=line.player_id, stints__team=line.team_id
                        ).exists()
                    )
            for line in game.pitching_lines.all():
                with self.subTest(game=game.id, pitcher=line.player_id):
                    self.assertTrue(
                        orm_models.Player.objects.filter(
                            stint_covers(game.year),
                            id=line.player_id,
                            stints__team__in=(game.home_team_id, game.away_team_id),
                        ).exists()
                    )

    def test_the_screens_open(self):
        self.client.force_login(self.owner)
        game = orm_models.Game.objects.filter(home_team__league__workspace=self.workspace).first()
        assert game is not None

        dashboard = self.client.get(ws_url("dashboard", workspace=self.workspace))
        self.assertContains(dashboard, "見本リーグ")
        self.assertNotContains(dashboard, "まだチームが登録されていません。")
        standings = self.client.get(ws_url("standings", workspace=self.workspace))
        self.assertContains(standings, "北浜シーガルズ")
        self.assertEqual(self.client.get(ws_url("game_detail", game.id, workspace=self.workspace)).status_code, 200)
        self.assertEqual(self.client.get(ws_url("game_edit", game.id, workspace=self.workspace)).status_code, 200)


class SampleLeagueIsolationTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="sample-owner", password="x")
        self.other = _plant_another_workspace()

    def test_rows_of_other_workspaces_are_untouched(self):
        before = _all_rows()

        workspace = _create(self.owner)

        after = _all_rows()
        mine = _rows_of(workspace.id)
        for label, rows in before.items():
            with self.subTest(table=label):
                self.assertEqual({pk: after[label].get(pk) for pk in rows}, rows, "前からある行が変わった")
                added = set(after[label]) - set(rows)
                self.assertLessEqual(added, mine.get(label, set()), "新しいワークスペースの外の行が増えた")
        self.assertTrue(_rows_of(self.other.id)["myapp.Team"], "比べる相手のワークスペースに行がある")

    def test_without_the_sample_the_workspace_is_empty(self):
        before = _all_rows()

        workspace = _create(self.owner, with_sample_league=False)

        after = _all_rows()
        added = {label: set(after[label]) - set(before[label]) for label in before}
        self.assertEqual(
            {label: ids for label, ids in added.items() if ids},
            {"myapp.Workspace": {workspace.id}},
            "ワークスペースの行だけ",
        )

    def test_the_limit_creates_nothing(self):
        for number in range(1, 4):
            make_workspace(self.owner, f"{number}つ目")
        before = _all_rows()

        with self.assertRaises(WorkspaceLimitReached):
            _create(self.owner)

        self.assertEqual(_all_rows(), before)

    def test_a_failure_in_the_sample_leaves_nothing_and_keeps_the_quota(self):
        """見本の試合の保存で失敗させる（球場・リーグ・チーム・選手は書いた後）。ワークスペースごと戻る。"""
        for number in range(1, 3):
            make_workspace(self.owner, f"{number}つ目")
        before = _all_rows()
        service = build_workspace_service()

        with (
            patch.object(DjangoGameRepository, "add_all", side_effect=InvalidGame("保存できません。")),
            self.assertRaises(InvalidGame),
        ):
            service.create(
                owner_id=self.owner.id, name="3つ目", visibility=Visibility.PRIVATE, with_sample_league=True
            )

        self.assertEqual(_all_rows(), before)
        self.assertTrue(service.can_create(self.owner.id), "上限を消費しない")

    def test_seeding_a_workspace_that_is_not_empty_writes_nothing(self):
        workspace = _create(self.owner, with_sample_league=False)
        make_league("先に入れたリーグ", workspace=workspace)
        before = _all_rows()

        with self.assertRaisesRegex(InvalidWorkspace, "空のワークスペースにだけ"):
            build_workspace_sample_service(real_scope(workspace)).seed(key=workspace.key)

        self.assertEqual(_all_rows(), before)

    def test_deleting_the_workspace_removes_the_whole_sample(self):
        workspace = _create(self.owner)
        mine = _rows_of(workspace.id)
        self.assertTrue(mine["myapp.GamePlateAppearance"], "消えたことを確かめられる")
        before = _all_rows()

        build_workspace_service().delete(workspace.id)

        expected = {
            label: {pk: row for pk, row in rows.items() if pk not in mine.get(label, set())}
            for label, rows in before.items()
        }
        self.assertEqual(_all_rows(), expected)

    def test_a_pennant_world_can_be_forked_from_the_sample_league(self):
        workspace = _create(self.owner)
        league = orm_models.League.objects.get(workspace=workspace)
        year = orm_models.Game.objects.filter(home_team__league=league).values_list("year", flat=True).first()
        assert year is not None

        created = build_pennant_world_service(real_scope(workspace)).create_world(
            name="見本の世界", owner_id=self.owner.id, source_league_ids=[league.id], start_year=year + 1, seed=1
        )

        self.assertEqual(created.team_count, SAMPLE_TEAM_COUNT)
        self.assertEqual(orm_models.PennantWorld.objects.get(id=created.world.id).workspace_id, workspace.id)


class SampleLeagueCreateScreenTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="creator", password="x")
        self.client.force_login(self.user)

    def test_checking_the_box_puts_the_sample_and_says_so(self):
        response = self.client.post(
            reverse("workspace_create"), {"name": "見本つき", "visibility": "private", "with_sample_league": "on"}
        )

        workspace = orm_models.Workspace.objects.get(owner=self.user)
        self.assertRedirects(response, ws_url("dashboard", workspace=workspace), fetch_redirect_response=False)
        self.assertEqual(orm_models.Team.objects.filter(league__workspace=workspace).count(), SAMPLE_TEAM_COUNT)
        dashboard = self.client.get(response["Location"])
        self.assertContains(
            dashboard,
            f"ワークスペース「見本つき」を作り、見本のリーグ（{SAMPLE_TEAM_COUNT}チーム・{SAMPLE_GAME_COUNT}試合）を入れました。",
        )

    def test_a_failure_in_the_sample_redraws_the_top_with_an_error(self):
        with patch.object(DjangoGameRepository, "add_all", side_effect=InvalidGame("保存できません。")):
            response = self.client.post(
                reverse("workspace_create"), {"name": "見本つき", "visibility": "private", "with_sample_league": "on"}
            )

        self.assertEqual(response.status_code, 200)
        [error] = response.context["create_form"].non_field_errors()
        self.assertIn("見本のリーグを入れられなかった", error)
        self.assertContains(response, "見本のリーグを入れられなかった")
        self.assertFalse(orm_models.Workspace.objects.filter(owner=self.user).exists())
