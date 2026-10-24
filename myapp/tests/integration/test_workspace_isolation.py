"""ワークスペースをまたいで行が漏れないこと（W1b T2。#172）。

範囲（`WorldScope`）がワークスペースを持つようになり、実データの条件は「そのワークスペースのリーグ」になった。
ワークスペース A（既定）と B に実データを置き、B の名前は A のどの名前とも重ならない**目印**にする。
A の範囲で読んだもの・A の key の URL（`/w/<A の key>/...`）の画面に目印が出たら、それは漏れ。

- (1) `repositories`・`queries` の `Django*` をモジュールから列挙し、すべてを検査の表（`CHECKS`）で検査する。
  表に無いクラスがあれば落ちる（新しい実装を足したら、漏れないかをここで決めさせる）。
- (2) A の範囲で B の id を `find_by_id` すると NotFound。
- (3) 球場（W1b T4）: 範囲の球場はそのワークスペースの球場だけ（ペナントの範囲も同じ）。A の球団が B の球場を
  指していても、A の範囲の読み取りは本拠地なしとして読む。A の範囲で B の球場を本拠地にして保存すると
  `StadiumNotFound` で、チームも選手も書かれない。管理画面の組み合わせは `test_admin_workspaces.py`。
- (4) 世界の台帳（`DjangoWorldRepository`・`DjangoWorldSummaryQuery`。W1b T3）: A の台帳に B の世界が出ず、
  B の世界の id は `WorldNotFound`。A の台帳で B の世界を保存・削除できず、何も変わらない。A の key で
  B の世界（`/w/<A>/pennant/<B の世界>/` とその下）を開くと 404、A のペナントの一覧には A の世界だけが出る。
- (5) `players_in(real(A))` に B の選手も A の世界の選手も出ない。
- ワークスペースの台帳（`DjangoWorkspaceAccessQuery`・`DjangoWorkspaceListQuery`。W2 T1）: A の key で B の行が
  出ず、B の編集者・担当は A では部外者。一覧に、入っていないワークスペースは出ない。
- URL（W2 T4）: `workspace_urlpatterns` の全パターンを分類表（`URL_TABLE`）で検査する（表に無いパターンが
  あれば落ちる）。A の key の下で B の id は 404・A の画面に B の目印が出ない（選手検索を含む）・非公開の B は
  未ログインならログインへ、部外者は 404、B の編集者と担当は通る・A の画面のリンクは A の key の下か
  ワークスペースの外だけ。ワークスペースは1つの要求で1回だけ読む。
"""

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.entities import Game
from myapp.domain.entities import Team as DomainTeam
from myapp.domain.exceptions import (
    DomainError,
    GameNotFound,
    InvalidAward,
    InvalidWorld,
    LeagueNotFound,
    StadiumNotFound,
    TeamNotFound,
    WorldNotFound,
)
from myapp.domain.pennant.awards import Award
from myapp.domain.pennant.release import ReleasePlan
from myapp.domain.pennant.world import MAX_WORLDS_PER_OWNER, WORLD_LIMIT_MESSAGE, World, WorldScope
from myapp.domain.services.rankings import Title
from myapp.domain.value_objects import BattingLine, JerseyNumber, PitchingLine, Position, RosterLimits, Season
from myapp.domain.workspace import Visibility, WorkspaceRole
from myapp.infrastructure import orm_models, queries, repositories
from myapp.infrastructure.queries import (
    DjangoAwardQuery,
    DjangoFieldingTotalsQuery,
    DjangoGameListQuery,
    DjangoHomeStadiumQuery,
    DjangoOffseasonQuery,
    DjangoPennantActivityQuery,
    DjangoPlayerFieldingQuery,
    DjangoPlayerSearchQuery,
    DjangoPlayerStatsQuery,
    DjangoRatingsHistoryQuery,
    DjangoSeasonPlayingTimeQuery,
    DjangoServiceHistoryQuery,
    DjangoSimulationContextQuery,
    DjangoTeamAnalysisQuery,
    DjangoTeamListQuery,
    DjangoTeamPermissionQuery,
    DjangoWorkspaceAccessQuery,
    DjangoWorkspaceListQuery,
    DjangoWorldSummaryQuery,
)
from myapp.infrastructure.repositories import (
    DjangoAwardRepository,
    DjangoClubPlanRepository,
    DjangoFixtureRepository,
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoRatingsRepository,
    DjangoReleasePlanRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
)
from myapp.infrastructure.scoping import players_in, stadiums_in
from myapp.presentation import views
from myapp.presentation.club_input import ACTION_MANUAL, ACTION_SAVE, RELEASE_SECTION
from myapp.presentation.views import build_pennant_world_service
from myapp.urls import workspace_urlpatterns

from ..helpers import (
    build_service,
    build_workspace_access_query,
    make_editor,
    make_league,
    make_stadium,
    make_workspace,
    play_game,
    real_scope,
    register_lineup,
    register_players,
    ws_url,
)
from .base import ROSTER_CHECK, BaseCase

# B の目印。A の範囲で読んだもの・今の URL の画面のどこにも出てはならない
B_MARK = "Ｂ目印"
B_WORKSPACE = f"{B_MARK}ワークスペース"
B_LEAGUE = f"{B_MARK}リーグ"
B_STADIUM = f"{B_MARK}球場"
B_TEAM = f"{B_MARK}球団"
B_RIVAL = f"{B_MARK}相手"
B_PLAYER = f"{B_MARK}選手"
YEAR = 2026
# B の試合は A より前の年と後の年に置く（最新の年・最初の年・シーズンの一覧が B に引きずられないかを見る）
B_YEAR = YEAR + 1
B_EARLY_YEAR = YEAR - 1
A_WORLD = "Aの世界"
B_WORLD = f"{B_MARK}世界"


class WorkspaceCase(BaseCase):
    """ワークスペース A（`BaseCase` の既定のワークスペース。最初に作るので id が最小）と B の実データ。

    A: `BaseCase` のリーグ・チーム2つに打者9人と投手、記録済みの試合1つ。
    B: 目印の名前のリーグ・球場・チーム2つに打者9人と投手、記録済みの試合2つ（`B_EARLY_YEAR`・`B_YEAR`）。
    B には、ペナント専用の表（能力・日程・編成・戦力外）と守備の明細の行も直接置く（範囲の条件が
    表ごとに効いているかを、行がある状態で確かめるため）。
    世界: A のリーグから分岐した A の世界（`A_WORLD`）と、B のリーグから分岐した B の世界（`B_WORLD`。オーナーは
    `b_owner`）。世界の台帳の検査に使う。B の世界には表彰の行も1つ直接置く（表彰はペナントの世界にしか無い）。
    球場: A の相手チームと、A の世界のその写しの本拠地を、ORM で B の球場に向けておく（`_plant_stray_stadiums`。
    範囲の読み取りが B の球場を出さないことを、ほかの検査と同じ目印で確かめる）。
    準備はクラスで1回（`setUpTestData`）。
    """

    workspace_b: orm_models.Workspace
    a_batters: list[int]
    a_pitcher: int
    a_game_id: int
    b_league: orm_models.League
    b_stadium: orm_models.Stadium
    b_team: orm_models.Team
    b_rival: orm_models.Team
    b_batters: list[int]
    b_pitcher: int
    b_game_ids: list[int]
    a_world_id: int
    b_world_id: int
    b_owner: User
    b_award_player_id: int
    b_award_team_id: int
    a_world_stray_team_id: int
    a_declaration_year: int

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.workspace_b = make_workspace(name=B_WORKSPACE)
        assert cls.workspace.id < cls.workspace_b.id, "A の id が最小（今の URL が A に解決する）"

        # --- A ---
        service_a = build_service(real_scope())
        cls.a_batters = register_lineup(service_a, cls.team, prefix="A打者", first_number=1)
        (cls.a_pitcher,) = register_players(cls.team, [("A投手", 18, "投手")])
        cls.a_game_id = cls._play(cls.team, cls.rival, cls.a_batters[0], cls.a_pitcher, YEAR, None).id

        # --- B ---
        cls.b_league = make_league(B_LEAGUE, workspace=cls.workspace_b)
        cls.b_stadium = make_stadium(B_STADIUM, workspace=cls.workspace_b, capacity=30000)
        cls.b_team = orm_models.Team.objects.create(league=cls.b_league, name=B_TEAM, home_stadium=cls.b_stadium)
        cls.b_rival = orm_models.Team.objects.create(league=cls.b_league, name=B_RIVAL)
        cls.b_batters = register_lineup(None, cls.b_team, prefix=B_PLAYER, first_number=1)
        (cls.b_pitcher,) = register_players(cls.b_team, [(f"{B_PLAYER}投手", 18, "投手")])
        scope_b = real_scope(cls.workspace_b)
        cls.b_game_ids = [
            cls._play(cls.b_team, cls.b_rival, cls.b_batters[0], cls.b_pitcher, year, scope_b).id
            for year in (B_EARLY_YEAR, B_YEAR)
        ]
        cls._plant_b_rows()

        # --- 世界（A と B に1つずつ）---
        cls.b_owner = User.objects.create_user(username="b-owner", password="x")
        with patch(ROSTER_CHECK):
            cls.a_world_id = (
                build_pennant_world_service(real_scope())
                .create_world(name=A_WORLD, owner_id=None, source_league_ids=[cls.league.id], start_year=YEAR, seed=1)
                .world.id
            )
            cls.b_world_id = (
                build_pennant_world_service(scope_b)
                .create_world(
                    name=B_WORLD,
                    owner_id=cls.b_owner.id,
                    source_league_ids=[cls.b_league.id],
                    start_year=YEAR,
                    seed=1,
                )
                .world.id
            )
        cls._plant_b_award()
        cls._plant_stray_stadiums()
        cls._plant_a_declaration()

    @classmethod
    def _plant_a_declaration(cls) -> None:
        """A の打者の FA 宣言（B の宣言の目印と対で、範囲の読み取りが A の宣言だけを出すことを確かめる）。

        世界を作った後に置く（A の世界の分岐に写さない）。在籍の最初の年に置く。
        """
        stint = orm_models.PlayerStint.objects.filter(player_id=cls.a_batters[0]).order_by("from_year").first()
        assert stint is not None
        cls.a_declaration_year = stint.from_year
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=cls.a_batters[0], year=stint.from_year)

    @classmethod
    def _plant_stray_stadiums(cls) -> None:
        """A の球団に B の球場を指させる（W1b T4）。

        リポジトリは範囲の外の球場を本拠地として書かない（`StadiumNotFound`）ので、ORM で直接書く
        （管理画面の直書き・古いデータなど、書き込みの検査を通らない行の代わり）。A の範囲の読み取りは
        これを本拠地なしとして読み、B の球場の名前（目印）も収容人数も出さないこと。
        世界を作った後に置く（分岐は本拠地を写すので、先に置くと A の世界の分岐が `StadiumNotFound` になる）。
        """
        orm_models.Team.objects.filter(id=cls.rival.id).update(home_stadium=cls.b_stadium)
        cls.a_world_stray_team_id = orm_models.Team.objects.get(
            league__world_id=cls.a_world_id, name=cls.rival.name
        ).id
        orm_models.Team.objects.filter(id=cls.a_world_stray_team_id).update(home_stadium=cls.b_stadium)

    @classmethod
    def _plant_b_award(cls) -> None:
        """B の世界の表彰（シーズンを締めずに置くので ORM で直接置く。表彰はペナントの世界にしか無い）。"""
        league = orm_models.League.objects.get(world_id=cls.b_world_id)
        stint = orm_models.PlayerStint.objects.filter(team__league=league).order_by("id").first()
        assert stint is not None, "B の世界に選手が写っていない"
        cls.b_award_player_id = stint.player_id
        cls.b_award_team_id = stint.team_id
        orm_models.PennantAward.objects.create(
            league=league,
            year=YEAR,
            title=Title.HOME_RUNS.value,
            player_id=stint.player_id,
            team_id=stint.team_id,
            value="42",
        )

    @staticmethod
    def _play(home, away, batter, pitcher, year, scope):
        return play_game(
            home,
            away,
            home_score=2,
            away_score=1,
            year=year,
            day=2 if scope is not None else 1,
            batting={batter: BattingLine(at_bats=4, singles=2, home_runs=1)},
            pitching={pitcher: PitchingLine(strikeouts=3)},
            scope=scope,
        )

    @classmethod
    def _plant_b_rows(cls) -> None:
        """B の、範囲の条件を確かめたい表の行。リポジトリは実データにペナントの行を書かないので、ORM で直接置く。"""
        batter = cls.b_batters[0]
        orm_models.GameFieldingLine.objects.create(game_id=cls.b_game_ids[0], player_id=batter, putouts=3)
        # FA 宣言（選手の行に付く。範囲の外の選手の id を渡されても読まないことを確かめる）。在籍の最初の年に置く
        first_year = orm_models.PlayerStint.objects.filter(player_id=batter).order_by("from_year").first()
        assert first_year is not None
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=batter, year=first_year.from_year)
        for year in (YEAR, B_YEAR):
            orm_models.PennantPlayerRatings.objects.create(
                player_id=batter, year=year, growth="普通", contact=50, power=50, eye=50, speed=50, fielding=50
            )
        orm_models.PennantFixture.objects.create(
            date=date(B_YEAR, 9, 1), home_team=cls.b_team, visitor_team=cls.b_rival
        )
        orm_models.PennantClubPlan.objects.create(team=cls.b_team, closer_id=cls.b_pitcher)
        plan = orm_models.PennantReleasePlan.objects.create(team=cls.b_team, year=B_YEAR)
        orm_models.PennantReleasePlanEntry.objects.create(plan=plan, player_id=batter)

    def setUp(self):
        super().setUp()
        self.a = real_scope()
        self.b_ids = {self.b_team.id, self.b_rival.id}

    # --- 判定の部品 ---

    def assert_no_mark(self, value) -> None:
        """読んだ値（DTO・集約・その入れ物）の repr に B の目印が無い。"""
        self.assertNotIn(B_MARK, repr(value))

    def assert_hidden(self, read: Callable[[], object]) -> None:
        """B を指して読んでも、NotFound（DomainError）になるか、目印の無い値が返る。"""
        try:
            value = read()
        except DomainError:
            return
        self.assert_no_mark(value)


# --- (1) クラスごとの検査。A の範囲で読み、B の行が返らないこと ---


def _team_repository(t: WorkspaceCase) -> None:
    repo = DjangoTeamRepository(t.a)
    with t.assertRaises(TeamNotFound):
        repo.find_by_id(t.b_team.id)
    t.assertEqual({team.id for team in repo.find_all()}, {t.team.id, t.rival.id})
    with_roster = repo.find_all_with_roster()
    t.assertEqual({team.id for team in with_roster}, {t.team.id, t.rival.id})
    t.assert_no_mark(with_roster)
    t.assertEqual(repo.find_by_league_with_roster(t.b_league.id), [])
    signature = repo.roster_signature()
    register_players(t.b_team, [(f"{B_PLAYER}追加", 50, "外野手")])
    t.assertEqual(repo.roster_signature(), signature, "B の在籍が A の名簿の目印を変えた")
    # 書き込みも範囲の外には届かない
    b_aggregate = DjangoTeamRepository(real_scope(t.workspace_b)).find_by_id(t.b_team.id)
    with t.assertRaises(LeagueNotFound):
        repo.save(b_aggregate)


def _game_repository(t: WorkspaceCase) -> None:
    repo = DjangoGameRepository(t.a)
    for game_id in t.b_game_ids:
        with t.assertRaises(GameNotFound):
            repo.find_by_id(game_id)
    t.assertEqual([game.id for game in repo.find_all()], [t.a_game_id])
    t.assertEqual(repo.find_all(B_YEAR), [])
    t.assertEqual(repo.find_by_team(t.b_team.id), [])
    t.assertEqual(repo.find_between_teams(t.b_ids), [])


def _league_repository(t: WorkspaceCase) -> None:
    repo = DjangoLeagueRepository(t.a)
    with t.assertRaises(LeagueNotFound):
        repo.find_by_id(t.b_league.id)
    t.assertEqual([league.id for league in repo.find_all()], [t.league.id])


def _ratings_repository(t: WorkspaceCase) -> None:
    repo = DjangoRatingsRepository(t.a)
    batter = t.b_batters[0]
    t.assertEqual(repo.find_by_year(YEAR), [])
    t.assertEqual(repo.find_by_player(batter), [])
    t.assertEqual(repo.find_by_players([batter], B_YEAR), [])


def _fixture_repository(t: WorkspaceCase) -> None:
    repo = DjangoFixtureRepository(t.a)
    t.assertEqual(repo.find_all(), [])
    t.assertIsNone(repo.first_date())
    t.assertIsNone(repo.first_date(t.b_team.id))


def _club_plan_repository(t: WorkspaceCase) -> None:
    repo = DjangoClubPlanRepository(t.a)
    t.assertEqual(repo.find_all(), [])
    with t.assertRaises(TeamNotFound):
        repo.find_by_team(t.b_team.id)


def _release_plan_repository(t: WorkspaceCase) -> None:
    repo = DjangoReleasePlanRepository(t.a)
    with t.assertRaises(TeamNotFound):
        repo.find_by_team(t.b_team.id)
    repo.delete_all()
    t.assertTrue(orm_models.PennantReleasePlan.objects.filter(team=t.b_team).exists(), "A の範囲で B の行を消した")


def _player_fielding_query(t: WorkspaceCase) -> None:
    t.assertIsNone(DjangoPlayerFieldingQuery(t.a).for_player(t.b_batters[0], t.b_team.id))


def _fielding_totals_query(t: WorkspaceCase) -> None:
    t.assertEqual(DjangoFieldingTotalsQuery(t.a).totals_for([t.b_batters[0]]), {})


def _simulation_context_query(t: WorkspaceCase) -> None:
    query = DjangoSimulationContextQuery(t.a)
    t.assertEqual(query.last_played_on(), date(YEAR, 4, 1), "B の試合の日付を A の最後の試合日にした")
    teams = {team.team_id: team for team in query.teams()}
    t.assertEqual(set(teams), {t.team.id, t.rival.id})
    t.assertIsNone(teams[t.rival.id].stadium_capacity, "B の球場の収容人数を A の球団の本拠地として読んだ")
    context = query.load(before=date(B_YEAR + 1, 1, 1))
    t.assertEqual({team.team_id for team in context.teams}, {t.team.id, t.rival.id})
    t.assert_no_mark(context)


def _player_stats_query(t: WorkspaceCase) -> None:
    query = DjangoPlayerStatsQuery(t.a)
    t.assertEqual(query.list_season(t.b_league.id, B_YEAR), [])
    by_league = query.list_season_by_league(YEAR)
    t.assertNotIn(t.b_league.id, by_league)
    t.assert_no_mark(by_league)
    later = query.list_season_by_league(B_YEAR)
    t.assertNotIn(t.b_league.id, later)
    t.assert_no_mark(later)
    t.assertEqual(query.list_roster(year=None, team_id=t.b_team.id), [])
    t.assertEqual(query.list_roster(year=None, league_id=t.b_league.id), [])
    everyone = query.list_roster(year=None, with_profile=True, with_games=True)
    t.assertTrue(everyone, "A の選手を読めていない（検査が空振りする）")
    t.assert_no_mark(everyone)


def _player_search_query(t: WorkspaceCase) -> None:
    query = DjangoPlayerSearchQuery(t.a)
    t.assertEqual(query.search(B_PLAYER), [])
    t.assertEqual(len(query.search("A打者")), 9)


def _team_permission_query(t: WorkspaceCase) -> None:
    query = DjangoTeamPermissionQuery(t.a)
    staff = User.objects.create_user(username="staff-iso", password="x", is_staff=True)
    t.assertTrue(query.can_manage(staff, t.team.id))
    t.assertFalse(query.can_manage(staff, t.b_team.id))
    t.assertFalse(query.can_manage_any(staff, t.b_ids))


def _game_list_query(t: WorkspaceCase) -> None:
    query = DjangoGameListQuery(t.a)
    t.assertEqual([row.id for row in query.list_rows()], [t.a_game_id])
    t.assertEqual(query.list_rows(year=B_YEAR), [])
    t.assertEqual(query.list_rows(team_id=t.b_team.id), [])
    t.assertEqual(query.list_rows(league_id=t.b_league.id), [])
    t.assertEqual([game.id for game in query.list_for_standings()], [t.a_game_id])
    t.assertEqual(query.list_for_player(t.b_team.id, t.b_batters[0]), [])
    t.assertEqual(query.list_seasons(), [YEAR])
    t.assertEqual(query.list_seasons(league_id=t.b_league.id), [])
    t.assertEqual(set(query.count_by_team()), {t.team.id, t.rival.id})
    t.assertEqual(query.list_months(team_id=t.b_team.id), [])
    t.assertEqual(query.latest_year(), YEAR, "B の試合の年を A の最新の年にした")


def _pennant_activity_query(t: WorkspaceCase) -> None:
    query = DjangoPennantActivityQuery(t.a)
    t.assertEqual(query.game_notes(t.b_game_ids), {})
    period = {"after": date(YEAR - 1, 12, 31), "through": date(B_YEAR, 12, 31)}
    t.assertEqual(query.batting_between(t.b_team.id, **period), [])
    t.assertEqual(query.pitching_between(t.b_team.id, **period), [])


def _team_list_query(t: WorkspaceCase) -> None:
    summaries = DjangoTeamListQuery(t.a).list_summaries()
    t.assertEqual({summary.id for summary in summaries}, {t.team.id, t.rival.id})
    t.assert_no_mark(summaries)
    by_id = {summary.id: summary for summary in summaries}
    t.assertEqual((by_id[t.rival.id].stadium_name, by_id[t.rival.id].city), ("", ""), "B の球場を本拠地として読んだ")
    # 空振りの防止: A の球場は読める
    t.assertEqual((by_id[t.team.id].stadium_name, by_id[t.team.id].city), (t.stadium.name, t.stadium.city))


def _home_stadium_query(t: WorkspaceCase) -> None:
    query = DjangoHomeStadiumQuery(t.a)
    t.assertIsNone(query.home_stadium_of(t.b_team.id))
    t.assertIsNone(query.home_stadium_of(t.rival.id), "B の球場を A の球団の本拠地として読んだ")
    stadium = query.home_stadium_of(t.team.id)
    t.assertIsNotNone(stadium, "A の球場を読めていない（検査が空振りする）")
    t.assertEqual(stadium.name if stadium else None, t.stadium.name)


def _season_playing_time_query(t: WorkspaceCase) -> None:
    query = DjangoSeasonPlayingTimeQuery(t.a)
    for year in (B_EARLY_YEAR, YEAR, B_YEAR):
        t.assertFalse(set(query.for_year(year)) & {*t.b_batters, t.b_pitcher}, f"{year}年の B の出場が読めた")


def _offseason_query(t: WorkspaceCase) -> None:
    query = DjangoOffseasonQuery(t.a)
    t.assertFalse(query.has_ratings(B_YEAR))
    t.assert_hidden(lambda: query.facts(YEAR))


def _ratings_history_query(t: WorkspaceCase) -> None:
    t.assertIsNone(DjangoRatingsHistoryQuery(t.a).for_player(t.b_batters[0]))


def _team_analysis_query(t: WorkspaceCase) -> None:
    query = DjangoTeamAnalysisQuery(t.a)
    t.assertEqual(query.list_years(t.b_team.id), [])
    t.assert_hidden(lambda: query.load(t.b_team.id, YEAR))


def _service_history_query(t: WorkspaceCase) -> None:
    """FA の推定の材料（FA 取得タブと外国人枠の対象外。#204）。B の選手の出場・在籍・属性は読めない。"""
    query = DjangoServiceHistoryQuery(t.a)
    b_players = [*t.b_batters, t.b_pitcher]
    history = query.load_service_history(b_players, B_YEAR)
    t.assertEqual((history.spans, history.stints, history.declarations), ([], [], []))
    t.assertEqual(query.load_service_history([], B_YEAR).records_from_year, YEAR, "B の試合の年に引きずられた")
    subjects = query.load_fa_subjects(b_players, B_YEAR)
    t.assertEqual((subjects.subjects, subjects.declarations), ([], []), "B の選手が FA の推定の材料に載った")
    t.assertEqual(subjects.records_from_year, YEAR, "B の試合の年に引きずられた")
    # 空振りの防止: A の選手は読める
    t.assertEqual(
        {subject.player_id for subject in query.load_fa_subjects(t.a_batters, YEAR).subjects}, set(t.a_batters)
    )
    _fa_declarations(t, query)


def _fa_declarations(t: WorkspaceCase, query: DjangoServiceHistoryQuery) -> None:
    """FA 宣言は範囲の選手のものだけ読む。B の選手の id を混ぜて渡しても、B の宣言（目印の行）は出ない。

    宣言を読む3か所（FA の推定の材料・戦力分析の入退団・チームの集約）は同じ `declarations_in` を通る。
    戦力分析の入退団は範囲のチームの在籍から選手を引くので B の id を渡せない。集約の読み口を直接確かめる。
    """
    b_players = [*t.b_batters, t.b_pitcher]
    t.assertTrue(
        orm_models.PlayerFreeAgentDeclaration.objects.filter(player_id__in=b_players).exists(),
        "B の宣言の目印が無い（検査が空振りする）",
    )
    a_player = t.a_batters[0]
    mixed = [a_player, *b_players]
    through = max(B_YEAR, t.a_declaration_year)

    for declarations in (
        query.load_service_history(mixed, through).declarations,
        query.load_fa_subjects(mixed, through).declarations,
    ):
        t.assertEqual([declaration.player_id for declaration in declarations], [a_player])
    t.assertEqual(list(repositories._declarations_of(mixed, t.a)), [a_player])
    # 集約でも、A の選手は宣言を持つ
    team = DjangoTeamRepository(t.a).find_by_id(t.team.id)
    player = next(player for player in team.players if player.id == a_player)
    t.assertEqual([declaration.year for declaration in player.fa_declarations], [t.a_declaration_year])


def _award_repository(t: WorkspaceCase) -> None:
    """表彰はペナントの世界にしか無い。A の実データにも A の世界にも、B の世界の表彰は出ず、書けない。"""
    for scope in (t.a, WorldScope.pennant(t.workspace.id, t.a_world_id)):
        repo = DjangoAwardRepository(scope)
        t.assertEqual(repo.recorded_years(), frozenset(), f"{scope} で B の世界の表彰の年が読めた")
        t.assertEqual(repo.count_by_player(), {}, f"{scope} で B の世界の表彰が数えられた")
    b_league = orm_models.League.objects.get(world_id=t.b_world_id)
    stray = Award(
        year=B_YEAR,
        league_id=b_league.id,
        title=Title.HITS,
        player_id=t.b_award_player_id,
        team_id=t.b_award_team_id,
        value="200",
    )
    with t.assertRaises(InvalidAward):
        DjangoAwardRepository(WorldScope.pennant(t.workspace.id, t.a_world_id)).add_all([stray])
    t.assertEqual(
        orm_models.PennantAward.objects.filter(league=b_league).count(), 1, "A の範囲で B の世界に表彰を書いた"
    )


def _award_query(t: WorkspaceCase) -> None:
    for scope in (t.a, WorldScope.pennant(t.workspace.id, t.a_world_id)):
        query = DjangoAwardQuery(scope)
        t.assertEqual(query.list_awards(), [], f"{scope} で B の世界の表彰が読めた")
        t.assertEqual(query.list_awards(player_id=t.b_award_player_id), [])
    # 空振りの防止: B の世界の範囲では読める
    t.assertEqual(len(DjangoAwardQuery(WorldScope.pennant(t.workspace_b.id, t.b_world_id)).list_awards()), 1)


def _world_repository(t: WorkspaceCase) -> None:
    repo = DjangoWorldRepository(t.workspace.id)
    worlds = repo.find_all()
    t.assertEqual([world.id for world in worlds], [t.a_world_id], "A の台帳に A の世界だけが出ていません")
    t.assert_no_mark(worlds)
    with t.assertRaises(WorldNotFound):
        repo.find_by_id(t.b_world_id)


def _world_summary_query(t: WorkspaceCase) -> None:
    query = DjangoWorldSummaryQuery(t.workspace.id)
    summaries = query.list_all()
    t.assertEqual([summary.world_id for summary in summaries], [t.a_world_id], "A の台帳に A の世界だけが出ていません")
    t.assert_no_mark(summaries)
    with t.assertRaises(WorldNotFound):
        query.get(t.b_world_id)


def _b_members(t: WorkspaceCase) -> tuple[User, User]:
    """B の編集者と、B のチームの担当（どちらも A では部外者）。

    表の検査は1つのテストで続けて流れるので、2回目は同じ行を返す。
    """
    editor, _ = User.objects.get_or_create(username="b-editor-iso")
    orm_models.WorkspaceEditor.objects.get_or_create(workspace=t.workspace_b, user=editor)
    manager, _ = User.objects.get_or_create(username="b-manager-iso")
    t.b_team.managers.add(manager)
    return editor, manager


def _workspace_access_query(t: WorkspaceCase) -> None:
    """A の key で引くと A の行だけ。B の持ち主・編集者・担当は A では部外者（公開の A では閲覧者）。"""
    query = DjangoWorkspaceAccessQuery()
    editor, manager = _b_members(t)
    for user_id in (t.workspace_b.owner_id, editor.id, manager.id):
        with t.subTest(user_id=user_id):
            context = query.resolve(t.workspace.key, user_id)
            t.assertEqual(context.workspace_id, t.workspace.id)
            t.assertEqual(context.access.role, WorkspaceRole.VIEWER, "B のメンバーが A のメンバーになった")
            t.assertEqual(context.access.managed_team_ids, frozenset())
            t.assert_no_mark(context)
    # A のチームも担当していれば、A では A のチームだけが担当になる（B のチームは混ざらない）
    t.team.managers.add(manager)
    t.assertEqual(query.resolve(t.workspace.key, manager.id).access.managed_team_ids, frozenset({t.team.id}))
    t.team.managers.remove(manager)  # 後の検査（一覧）では A の部外者に戻す
    # 空振りの防止: B の key では B のメンバーとして引ける
    t.assertEqual(query.resolve(t.workspace_b.key, editor.id).access.role, WorkspaceRole.EDITOR)
    t.assertEqual(query.resolve(t.workspace_b.key, manager.id).access.managed_team_ids, frozenset({t.b_team.id}))


def _workspace_list_query(t: WorkspaceCase) -> None:
    """一覧は自分が入っているものだけ。B のメンバーの一覧に A は出ず、A の持ち主の一覧に B は出ない。"""
    query = DjangoWorkspaceListQuery()
    editor, manager = _b_members(t)
    for user_id in (editor.id, manager.id, t.workspace_b.owner_id):
        with t.subTest(user_id=user_id):
            t.assertEqual([row.key for row in query.list_for(user_id)], [t.workspace_b.key])
    a_rows = query.list_for(t.workspace.owner_id)
    t.assertEqual([row.key for row in a_rows], [t.workspace.key])
    t.assert_no_mark(a_rows)


# 検査の表。`repositories`・`queries` の `Django*` は、ちょうどここに載る
CHECKS: dict[type, Callable[[WorkspaceCase], None]] = {
    DjangoTeamRepository: _team_repository,
    DjangoGameRepository: _game_repository,
    DjangoLeagueRepository: _league_repository,
    DjangoRatingsRepository: _ratings_repository,
    DjangoFixtureRepository: _fixture_repository,
    DjangoClubPlanRepository: _club_plan_repository,
    DjangoReleasePlanRepository: _release_plan_repository,
    DjangoAwardRepository: _award_repository,
    DjangoAwardQuery: _award_query,
    DjangoPlayerFieldingQuery: _player_fielding_query,
    DjangoFieldingTotalsQuery: _fielding_totals_query,
    DjangoSimulationContextQuery: _simulation_context_query,
    DjangoPlayerStatsQuery: _player_stats_query,
    DjangoPlayerSearchQuery: _player_search_query,
    DjangoTeamPermissionQuery: _team_permission_query,
    DjangoGameListQuery: _game_list_query,
    DjangoPennantActivityQuery: _pennant_activity_query,
    DjangoTeamListQuery: _team_list_query,
    DjangoHomeStadiumQuery: _home_stadium_query,
    DjangoSeasonPlayingTimeQuery: _season_playing_time_query,
    DjangoOffseasonQuery: _offseason_query,
    DjangoRatingsHistoryQuery: _ratings_history_query,
    DjangoTeamAnalysisQuery: _team_analysis_query,
    DjangoServiceHistoryQuery: _service_history_query,
    DjangoWorldRepository: _world_repository,
    DjangoWorldSummaryQuery: _world_summary_query,
    DjangoWorkspaceAccessQuery: _workspace_access_query,
    DjangoWorkspaceListQuery: _workspace_list_query,
}


def _implementations() -> set[type]:
    found = set()
    for module in (repositories, queries):
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if name.startswith("Django") and cls.__module__ == module.__name__:
                found.add(cls)
    return found


class EveryImplementationIsCheckedTest(WorkspaceCase):
    def test_every_implementation_is_in_the_table(self):
        """新しい `Django*` を足したら、ここに検査を足すまで落ちる（漏れの検査の漏れを作らない）。"""
        found = _implementations()
        self.assertGreaterEqual(len(found), 20, "検査の対象を拾えていません")
        unlisted = sorted(cls.__name__ for cls in found - set(CHECKS))
        self.assertEqual(unlisted, [], "検査の表に無い実装があります")
        stale = sorted(cls.__name__ for cls in set(CHECKS) - found)
        self.assertEqual(stale, [], "表に、もう無い実装が残っています")

    def test_each_implementation_does_not_return_the_other_workspace(self):
        for cls, check in CHECKS.items():
            with self.subTest(implementation=cls.__name__):
                check(self)


class OtherWorkspaceIdsAreNotFoundTest(WorkspaceCase):
    """(2) A の範囲で B の id を引くと NotFound。逆も同じ（A は B から見えない）。"""

    def test_find_by_id_does_not_cross_workspaces(self):
        scope_b = real_scope(self.workspace_b)
        cases = [
            (DjangoTeamRepository(self.a).find_by_id, self.b_team.id, TeamNotFound),
            (DjangoLeagueRepository(self.a).find_by_id, self.b_league.id, LeagueNotFound),
            (DjangoGameRepository(self.a).find_by_id, self.b_game_ids[0], GameNotFound),
            (DjangoTeamRepository(scope_b).find_by_id, self.team.id, TeamNotFound),
            (DjangoLeagueRepository(scope_b).find_by_id, self.league.id, LeagueNotFound),
            (DjangoGameRepository(scope_b).find_by_id, self.a_game_id, GameNotFound),
        ]
        for find, other_id, not_found in cases:
            with self.subTest(find=find.__qualname__, id=other_id), self.assertRaises(not_found):
                find(other_id)

    def test_each_workspace_finds_its_own(self):
        """検査が空振りしていないこと（自分のワークスペースの行は引ける）。"""
        scope_b = real_scope(self.workspace_b)
        self.assertEqual(DjangoTeamRepository(scope_b).find_by_id(self.b_team.id).name, B_TEAM)
        self.assertEqual(DjangoTeamRepository(self.a).find_by_id(self.team.id).name, self.team.name)


class PlayersInTest(WorkspaceCase):
    """(5) 実データの選手は、そのワークスペースのリーグの球団に在籍がある選手（肯定形）。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.world_player_ids = set(
            orm_models.PlayerStint.objects.filter(team__league__world_id=cls.a_world_id).values_list(
                "player_id", flat=True
            )
        )
        cls.unattached = orm_models.Player.objects.create(name="在籍の無い選手")

    def test_the_real_players_of_a_are_only_the_players_of_a(self):
        ids = set(players_in(self.a).values_list("id", flat=True))

        self.assertEqual(ids, {*self.a_batters, self.a_pitcher})
        self.assertFalse(ids & {*self.b_batters, self.b_pitcher}, "B の選手が A の実データに出た")
        self.assertTrue(self.world_player_ids, "世界に選手が写っていない（検査が空振りする）")
        self.assertFalse(ids & self.world_player_ids, "A の世界の選手が A の実データに出た")

    def test_the_real_players_of_b_are_only_the_players_of_b(self):
        ids = set(players_in(real_scope(self.workspace_b)).values_list("id", flat=True))

        self.assertEqual(ids, {*self.b_batters, self.b_pitcher})

    def test_a_player_without_a_stint_is_in_no_scope(self):
        """在籍の無い選手はどの範囲にも出ない（管理画面だけが拾う。`test_admin.py`）。"""
        for scope in (self.a, real_scope(self.workspace_b)):
            with self.subTest(scope=str(scope)):
                self.assertFalse(players_in(scope).filter(id=self.unattached.id).exists())


class UrlsShowOnlyTheirWorkspaceTest(WorkspaceCase):
    """URL の key のワークスペースだけを読む。A の key の画面に B の目印を出さず、B の key では B が見える。"""

    def _pages(self) -> list[str]:
        return [
            ws_url("dashboard"),
            ws_url("team_list"),
            ws_url("standings"),
            ws_url("standings_by_year", YEAR),
            ws_url("game_list"),
            # 検索語は画面に出るので、目印そのものではなく目印の一部で探す
            f"{ws_url('player_search')}?q=目印選手",
            f"{ws_url('player_search')}?q=目印",
            ws_url("league_detail", self.league.id),
            ws_url("analysis_index"),
        ]

    def test_the_pages_do_not_show_the_other_workspace(self):
        for url in self._pages():
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, B_MARK)

    def test_the_pages_show_their_workspace(self):
        """検査が空振りしていないこと（A の key では A の行が、B の key では B の行だけが出る）。"""
        self.assertContains(self.client.get(ws_url("team_list")), self.team.name)
        self.assertContains(self.client.get(f"{ws_url('player_search')}?q=A打者"), "A打者1")
        b_teams = self.client.get(ws_url("team_list", workspace=self.workspace_b))
        self.assertContains(b_teams, B_TEAM)
        self.assertNotContains(b_teams, self.team.name)
        b_search = self.client.get(f"{ws_url('player_search', workspace=self.workspace_b)}?q=目印選手")
        self.assertContains(b_search, B_PLAYER)
        self.assertNotContains(b_search, "A打者")

    def test_pages_by_the_other_workspaces_ids_are_404(self):
        for url in (
            ws_url("player_list", self.b_team.id),
            ws_url("league_detail", self.b_league.id),
            ws_url("game_detail", self.b_game_ids[0]),
            ws_url("team_analysis", self.b_team.id),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_workspace_is_read_once_per_request(self):
        """ワークスペースは1つの要求で1回だけ読む（`workspace_view` が解決し、範囲・権限の判定が別々に読まない）。"""
        staff = User.objects.create_user(username="staff-once", password="x", is_staff=True)
        self.client.force_login(staff)
        # 試合一覧は、画面の範囲と「登録の導線を出すか」（権限の判定）の両方で実データの範囲を使う
        for url in (ws_url("game_list"), ws_url("player_list", self.team.id)):
            with self.subTest(url=url), CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(url).status_code, 200)
            reads = [q["sql"] for q in captured if 'FROM "myapp_workspace"' in q["sql"]]
            self.assertEqual(len(reads), 1, reads)


class NoWorkspaceTest(TestCase):
    """ワークスペースが1つも無い DB でも、`/` と管理画面のトップは開く。ワークスペースの中の URL は 404。"""

    def test_the_site_pages_are_404(self):
        for url in (ws_url("dashboard"), ws_url("team_list"), ws_url("pennant_index")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_top_page_opens(self):
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        self.client.force_login(User.objects.create_user(username="alone", password="x"))
        self.assertContains(self.client.get(reverse("home")), "まだどのワークスペースにも入っていません")

    def test_the_admin_index_still_opens_to_create_the_first_workspace(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        # 概況は運営向けの全ワークスペースの数字（W2 T2）。すべて0で、最初の1つを作る導線を出す
        self.assertContains(response, "ワークスペースがまだありません")


class WritesDoNotReachTheOtherWorkspaceTest(WorkspaceCase):
    """A の範囲で、B の試合・チーム・リーグを指して書くと NotFound で、何も変えない。

    ペナント専用の表（編成・戦力外）の書き込みは実データの範囲では書き込み自体を拒むので、ここでは扱わない
    （ペナントの範囲で、ほかのワークスペースの世界の球団に書けないことは `WorldLedgerWritesTest`）。
    """

    def _game(self, home, away, *, game_id=None) -> Game:
        return Game(
            id=game_id,
            season=Season(YEAR),
            played_on=date(YEAR, 5, 1),
            home_team_id=home.id,
            away_team_id=away.id,
            home_score=9,
            away_score=0,
        )

    def _snapshot(self):
        return (
            list(orm_models.Game.objects.order_by("id").values()),
            list(orm_models.League.objects.order_by("id").values()),
            list(orm_models.Team.objects.order_by("id").values()),
        )

    def test_game_team_and_league_writes_with_b_ids_raise_and_change_nothing(self):
        games, teams, leagues = (
            DjangoGameRepository(self.a),
            DjangoTeamRepository(self.a),
            DjangoLeagueRepository(self.a),
        )
        b_game = DjangoGameRepository(real_scope(self.workspace_b)).find_by_id(self.b_game_ids[0])
        b_game.home_score = 9
        b_league = DjangoLeagueRepository(real_scope(self.workspace_b)).find_by_id(self.b_league.id)
        b_league.display_order = 9
        writes = {
            # チームの検査が先に効く（B のチームは A の範囲に無い）
            "B の試合を保存": (lambda: games.save(b_game), TeamNotFound),
            "B のチームどうしの新しい試合": (lambda: games.save(self._game(self.b_team, self.b_rival)), TeamNotFound),
            "B の試合の id に A のチームを書く": (
                lambda: games.save(self._game(self.team, self.rival, game_id=self.b_game_ids[0])),
                GameNotFound,
            ),
            "B のチームの試合をまとめて追加": (
                lambda: games.add_all([self._game(self.b_team, self.b_rival)]),
                TeamNotFound,
            ),
            "B のリーグを保存": (lambda: leagues.save(b_league), LeagueNotFound),
            # A のリーグは範囲の中なので、リーグの検査を抜けてチームの検査（範囲の外のチームの id）で止まる
            "B のチームの id を A のリーグで保存": (
                lambda: teams.save(DomainTeam(name="乗っ取り", league_id=self.league.id, id=self.b_team.id)),
                TeamNotFound,
            ),
        }
        before = self._snapshot()
        for label, (write, expected) in writes.items():
            with self.subTest(write=label), self.assertRaises(expected):
                write()
        self.assertEqual(self._snapshot(), before, "A の範囲の書き込みが B の行を変えた")
        self.assertEqual(orm_models.Team.objects.get(id=self.b_team.id).league_id, self.b_league.id)


class MisbuiltWorldScopeGameWritesTest(WorkspaceCase):
    """取り違えた範囲（A のワークスペースに B の世界）で試合を書くと、締めた年の検査が台帳で世界を引けず拒む。

    ペナントの範囲の条件（`world_id = W`）はワークスペースを見ないので、B の世界の球団は範囲の検査を抜ける。
    締めた年の検査が世界の行を引けないときに飛ばすと、そのまま書けてしまう（検査を飛ばさない）。
    """

    def test_a_game_write_through_a_misbuilt_scope_raises_and_writes_nothing(self):
        misbuilt = DjangoGameRepository(WorldScope.pennant(self.workspace.id, self.b_world_id))
        home, away = orm_models.Team.objects.filter(league__world_id=self.b_world_id).order_by("id")[:2]
        game = Game(
            season=Season(YEAR),
            played_on=date(YEAR, 5, 1),
            home_team_id=home.id,
            away_team_id=away.id,
            home_score=0,
            away_score=0,
        )
        before = orm_models.Game.objects.count()
        for label, write in (("save", lambda: misbuilt.save(game)), ("add_all", lambda: misbuilt.add_all([game]))):
            with self.subTest(write=label), self.assertRaises(WorldNotFound):
                write()
        self.assertEqual(orm_models.Game.objects.count(), before, "取り違えた範囲で B の世界に試合を書いた")


class StadiumsInTest(WorkspaceCase):
    """(3) 球場はワークスペース直属。実データの範囲でもペナントの範囲でも、そのワークスペースの球場だけ（W1b T4）。"""

    def test_each_scope_sees_only_the_stadiums_of_its_workspace(self):
        a_world = WorldScope.pennant(self.workspace.id, self.a_world_id)
        b_world = WorldScope.pennant(self.workspace_b.id, self.b_world_id)
        for scope, expected in (
            (self.a, {self.stadium.id}),
            (a_world, {self.stadium.id}),
            (real_scope(self.workspace_b), {self.b_stadium.id}),
            (b_world, {self.b_stadium.id}),
        ):
            with self.subTest(scope=str(scope)):
                self.assertEqual(set(stadiums_in(scope).values_list("id", flat=True)), expected)

    def test_a_world_reads_the_stadiums_of_its_workspace_only(self):
        """A の世界の球団は A の球場を本拠地として読め、B の球場を指す球団は本拠地なしになる。"""
        a_world = WorldScope.pennant(self.workspace.id, self.a_world_id)
        copied = orm_models.Team.objects.get(league__world_id=self.a_world_id, name=self.team.name)
        query = DjangoHomeStadiumQuery(a_world)
        stadium = query.home_stadium_of(copied.id)
        self.assertEqual(stadium.name if stadium else None, self.stadium.name, "分岐した球団が元の球場を読めない")
        self.assertIsNone(query.home_stadium_of(self.a_world_stray_team_id))
        self.assert_no_mark(DjangoTeamListQuery(a_world).list_summaries())
        capacities = {team.team_id: team.stadium_capacity for team in DjangoSimulationContextQuery(a_world).teams()}
        self.assertIsNone(capacities[self.a_world_stray_team_id], "B の球場の収容人数で A の世界の観客数を作る")

    def test_a_fork_keeps_the_stadium_of_its_own_workspace(self):
        """B の世界の分岐は、B の範囲で B の球場を写せた（本拠地の検査を通る）。"""
        copied = orm_models.Team.objects.get(league__world_id=self.b_world_id, name=B_TEAM)
        self.assertEqual(copied.home_stadium_id, self.b_stadium.id)

    def test_the_team_list_page_does_not_show_bs_stadium(self):
        response = self.client.get(ws_url("team_list"))

        self.assertContains(response, self.stadium.name)
        self.assertNotContains(response, B_STADIUM)


class HomeStadiumWritesTest(WorkspaceCase):
    """(3) A の範囲で B の球場を本拠地にして保存すると `StadiumNotFound` で、チームも選手も書かれない（W1b T4）。"""

    def _snapshot(self):
        return (
            list(orm_models.Team.objects.order_by("id").values()),
            list(orm_models.Player.objects.order_by("id").values()),
            list(orm_models.PlayerStint.objects.order_by("id").values()),
        )

    def _with_new_player(self, team: DomainTeam) -> DomainTeam:
        team.add_player(
            "新入団", JerseyNumber("77"), Position.INFIELDER, from_year=YEAR, limits=RosterLimits.UNLIMITED
        )
        return team

    def test_saving_a_team_with_bs_stadium_raises_and_writes_nothing(self):
        a_world = WorldScope.pennant(self.workspace.id, self.a_world_id)
        copied_id = orm_models.Team.objects.get(league__world_id=self.a_world_id, name=self.team.name).id
        world_league_id = orm_models.League.objects.get(world_id=self.a_world_id).id

        def existing(scope, team_id):
            team = self._with_new_player(DjangoTeamRepository(scope).find_by_id(team_id))
            team.home_stadium_id = self.b_stadium.id
            return lambda: DjangoTeamRepository(scope).save(team)

        def new(scope, league_id):
            team = DomainTeam(name="新球団", league_id=league_id, home_stadium_id=self.b_stadium.id)
            return lambda: DjangoTeamRepository(scope).save(team)

        writes = {
            "A の球団の本拠地を B の球場に": existing(self.a, self.team.id),
            "A の新しい球団の本拠地が B の球場": new(self.a, self.league.id),
            "A の世界の球団の本拠地を B の球場に": existing(a_world, copied_id),
            "A の世界の新しい球団の本拠地が B の球場": new(a_world, world_league_id),
        }
        before = self._snapshot()
        for label, write in writes.items():
            with self.subTest(write=label):
                with CaptureQueriesContext(connection) as captured, self.assertRaises(StadiumNotFound):
                    write()
                # 書いてから巻き戻すのではなく、書く前に止まる
                written = [q["sql"] for q in captured if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE"))]
                self.assertEqual(written, [], "本拠地の検査の前に書いた")
        self.assertEqual(self._snapshot(), before, "B の球場を本拠地にする保存が、チームか選手を書いた")

    def test_a_team_can_take_a_stadium_of_its_own_workspace(self):
        """空振りの防止: 同じワークスペースの球場なら、実データでも世界でも本拠地にできる。"""
        a_world = WorldScope.pennant(self.workspace.id, self.a_world_id)
        for scope, team_id in ((self.a, self.rival.id), (a_world, self.a_world_stray_team_id)):
            with self.subTest(scope=str(scope)):
                repo = DjangoTeamRepository(scope)
                team = repo.find_by_id(team_id)
                team.home_stadium_id = self.stadium.id
                repo.save(team)
                self.assertEqual(orm_models.Team.objects.get(id=team_id).home_stadium_id, self.stadium.id)

    def test_a_team_pointing_outside_reads_without_a_stadium_and_saving_clears_it(self):
        """範囲の外の球場を指す行は、集約でも参照クエリと同じく本拠地なしとして読む。保存すると外の id は消える。"""
        a_world = WorldScope.pennant(self.workspace.id, self.a_world_id)
        for scope, team_id in ((self.a, self.rival.id), (a_world, self.a_world_stray_team_id)):
            with self.subTest(scope=str(scope)):
                repo = DjangoTeamRepository(scope)
                team = repo.find_by_id(team_id)
                self.assertIsNone(team.home_stadium_id, "範囲の外の球場を集約に読んだ")
                for loaded in (*repo.find_all(), *repo.find_all_with_roster()):
                    if loaded.id == team_id:
                        self.assertIsNone(loaded.home_stadium_id)
                team.name = "改名した相手"
                repo.save(team)
                row = orm_models.Team.objects.get(id=team_id)
                self.assertEqual((row.name, row.home_stadium_id), ("改名した相手", None))

    def test_a_fork_of_a_league_with_a_team_pointing_outside_copies_no_stadium(self):
        """範囲の外の球場を指す球団のリーグからも世界を作れ、写した球団は本拠地なし（「球場が見つかりません」で止まらない）。"""
        with patch(ROSTER_CHECK):
            world_id = (
                build_pennant_world_service(real_scope())
                .create_world(
                    name="分岐の確かめ", owner_id=None, source_league_ids=[self.league.id], start_year=YEAR, seed=2
                )
                .world.id
            )
        copies = dict(orm_models.Team.objects.filter(league__world_id=world_id).values_list("name", "home_stadium_id"))
        self.assertEqual(copies, {self.team.name: self.stadium.id, self.rival.name: None})


class MisbuiltWorldScopeStadiumWritesTest(WorkspaceCase):
    """取り違えた範囲（A のワークスペースに B の世界）で、B の世界の球団に A の球場を書けない（W1b T4）。

    ペナントの範囲の条件（`world_id = W`）はワークスペースを見ないので、B の世界の球団は範囲の検査を抜ける。
    球場の検査は、範囲の世界が球場のワークスペースに属すことも見る（`stadiums_in`）。
    """

    def test_a_stadium_of_a_cannot_be_written_to_a_team_of_bs_world(self):
        misbuilt = WorldScope.pennant(self.workspace.id, self.b_world_id)
        repo = DjangoTeamRepository(misbuilt)
        b_world_team = orm_models.Team.objects.filter(league__world_id=self.b_world_id).order_by("id").first()
        assert b_world_team is not None
        existing = repo.find_by_id(b_world_team.id)
        existing.home_stadium_id = self.stadium.id
        new = DomainTeam(name="新球団", league_id=b_world_team.league_id, home_stadium_id=self.stadium.id)
        self.assertFalse(stadiums_in(misbuilt).exists(), "取り違えた範囲に球場がある")
        before = list(orm_models.Team.objects.order_by("id").values())
        for label, team in (("既存の球団", existing), ("新しい球団", new)):
            with self.subTest(write=label), self.assertRaises(StadiumNotFound):
                repo.save(team)
        self.assertEqual(
            list(orm_models.Team.objects.order_by("id").values()), before, "取り違えた範囲で本拠地を書いた"
        )


class WorldLedgerWritesTest(WorkspaceCase):
    """(4) A の台帳で B の世界を保存・削除できず、何も変わらない。A の世界の範囲から B の世界の球団に書けない。"""

    def _snapshot(self):
        return (
            list(orm_models.PennantWorld.objects.order_by("id").values()),
            list(orm_models.League.objects.filter(world_id=self.b_world_id).order_by("id").values()),
            list(orm_models.Team.objects.filter(league__world_id=self.b_world_id).order_by("id").values()),
            list(orm_models.PennantReleasePlan.objects.order_by("id").values()),
        )

    def test_the_ledger_of_a_cannot_save_or_delete_a_world_of_b(self):
        ledger_a = DjangoWorldRepository(self.workspace.id)
        b_world = DjangoWorldRepository(self.workspace_b.id).find_by_id(self.b_world_id)
        b_world.name = "乗っ取り"
        hijack = World(
            id=self.b_world_id, name="乗っ取り", seed=1, start_year=YEAR, owner_id=None, workspace_id=self.workspace.id
        )
        writes = {
            # B の世界をそのまま（B のワークスペースのまま）A の台帳で保存する
            "B の世界を保存": (lambda: ledger_a.save(b_world), InvalidWorld),
            # ワークスペースを A に書き換えて、B の世界の id で保存する（`update_or_create(id=...)` の乗っ取り）
            "B の世界の id を A の世界として保存": (lambda: ledger_a.save(hijack), WorldNotFound),
            "B の世界を消す": (lambda: ledger_a.delete(self.b_world_id), WorldNotFound),
        }
        before = self._snapshot()
        for label, (write, expected) in writes.items():
            with self.subTest(write=label), self.assertRaises(expected):
                write()
        self.assertEqual(self._snapshot(), before, "A の台帳の書き込みが B の世界を変えた")
        self.assertEqual(orm_models.PennantWorld.objects.get(id=self.b_world_id).name, B_WORLD)

    def test_the_ledger_of_a_still_saves_its_own_world(self):
        """検査が空振りしていないこと（自分のワークスペースの世界は更新できる）。"""
        ledger_a = DjangoWorldRepository(self.workspace.id)
        world = ledger_a.find_by_id(self.a_world_id)
        world.name = "Aの世界（改名）"

        ledger_a.save(world)

        row = orm_models.PennantWorld.objects.get(id=self.a_world_id)
        self.assertEqual((row.name, row.workspace_id), ("Aの世界（改名）", self.workspace.id))

    def test_a_world_of_a_cannot_write_a_release_plan_for_a_team_of_bs_world(self):
        """A の世界の範囲（`pennant(A, A の世界)`）で、B の世界の球団の戦力外を指名できない。

        確かめているのはペナントの条件（`world_condition` の `world_id = W`。T2 の時点から成り立つ）で、台帳の
        絞り込み（T3）の回帰の検査ではない。世界をまたぐ書き込みがワークスペースをまたいでも止まることを、
        A の世界の範囲を正しく作れた後の側から確かめる
        （範囲を作らせない側は `WorldScopeIsVerifiedAgainstTheLedgerTest`）。
        """
        b_world_team = orm_models.Team.objects.filter(league__world_id=self.b_world_id).order_by("id").first()
        assert b_world_team is not None
        b_world_player = orm_models.PlayerStint.objects.filter(team=b_world_team).values_list("player_id", flat=True)[
            0
        ]
        repo = DjangoReleasePlanRepository(WorldScope.pennant(self.workspace.id, self.a_world_id))
        before = self._snapshot()

        with self.assertRaises(TeamNotFound):
            repo.save(ReleasePlan(team_id=b_world_team.id, year=YEAR, player_ids=frozenset({b_world_player})))

        self.assertEqual(self._snapshot(), before, "A の世界の範囲の書き込みが B の世界の行を変えた")


class WorldPagesOfTheOtherWorkspaceTest(WorkspaceCase):
    """(4) A の key の URL で B の世界を開くと 404。A のペナントの一覧には A の世界だけが出る。"""

    def _b_world_pages(self) -> list[str]:
        world = self.b_world_id
        league = orm_models.League.objects.filter(world_id=world).values_list("id", flat=True)[0]
        team = orm_models.Team.objects.filter(league__world_id=world).values_list("id", flat=True)[0]
        player = orm_models.PlayerStint.objects.filter(team_id=team).values_list("player_id", flat=True)[0]
        home, away = orm_models.Team.objects.filter(league__world_id=world).order_by("id")[:2]
        game = orm_models.Game.objects.create(
            year=YEAR, played_on=date(YEAR, 4, 2), home_team=home, away_team=away, home_score=1, away_score=0
        )
        return [
            ws_url("pennant_game_detail", world, game.id),
            ws_url("pennant_world", world),
            ws_url("pennant_history", world),
            ws_url("pennant_team_history", world, team),
            ws_url("pennant_offseason", world, YEAR),
            ws_url("pennant_delete", world),
            ws_url("pennant_club", world),
            ws_url("pennant_standings", world),
            ws_url("pennant_standings_by_year", world, YEAR),
            ws_url("pennant_league_detail", world, league),
            ws_url("pennant_league_titles", world, league),
            ws_url("pennant_league_stats", world, league),
            ws_url("pennant_team_list", world),
            ws_url("pennant_game_list", world),
            ws_url("pennant_player_list", world, team),
            ws_url("pennant_analysis_index", world),
            ws_url("pennant_team_analysis", world, team),
            ws_url("pennant_player_detail", world, team, player),
        ]

    def test_the_pages_of_bs_world_are_404(self):
        # B の世界のオーナーでも、A の key の URL からは開けない
        self.client.force_login(self.b_owner)
        for url in self._b_world_pages():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_writes_to_bs_world_are_404_and_change_nothing(self):
        self.client.force_login(self.b_owner)
        worlds_before = list(orm_models.PennantWorld.objects.order_by("id").values())
        b_games = orm_models.Game.objects.filter(home_team__league__world_id=self.b_world_id)
        games_before = b_games.count()
        for url in (
            ws_url("pennant_advance", self.b_world_id),
            ws_url("pennant_close_season", self.b_world_id),
            ws_url("pennant_delete", self.b_world_id),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {"target": "day", "confirm_name": B_WORLD}).status_code, 404)
        self.assertEqual(list(orm_models.PennantWorld.objects.order_by("id").values()), worlds_before)
        self.assertEqual(b_games.count(), games_before)

    def test_the_club_writes_to_bs_world_are_404_and_change_nothing(self):
        """編成（区画の手動化・保存）と戦力外の指名の POST も、B の世界なら 404 で何も書かない。"""
        self.client.force_login(self.b_owner)
        url = ws_url("pennant_club", self.b_world_id)
        plans = orm_models.PennantClubPlan.objects.filter(team__league__world_id=self.b_world_id)
        releases = orm_models.PennantReleasePlan.objects.filter(team__league__world_id=self.b_world_id)
        before = (list(plans.values()), list(releases.values()))
        posts = {
            "編成を手動にする": {"section": "lineup", "action": ACTION_MANUAL},
            "編成を保存する": {"section": "active", "action": ACTION_SAVE},
            "戦力外を手動にする": {"section": RELEASE_SECTION, "action": ACTION_MANUAL, "year": YEAR},
            "戦力外を保存する": {"section": RELEASE_SECTION, "action": ACTION_SAVE, "year": YEAR},
        }
        for label, data in posts.items():
            with self.subTest(post=label):
                self.assertEqual(self.client.post(url, data).status_code, 404)
        self.assertEqual((list(plans.values()), list(releases.values())), before)

    def test_the_pages_of_as_world_open(self):
        """検査が空振りしていないこと（A の世界は開ける）。"""
        for url in (
            ws_url("pennant_world", self.a_world_id),
            ws_url("pennant_team_list", self.a_world_id),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_index_lists_only_as_worlds(self):
        self.client.force_login(self.b_owner)

        response = self.client.get(ws_url("pennant_index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, A_WORLD)
        self.assertNotContains(response, B_MARK)

    def test_worlds_in_other_workspaces_count_toward_the_limit(self):
        """上限は全ワークスペースで数える（作成の検査と同じ）。

        B に上限まで世界を持つ人には、A でも作成フォームを出さない。A の一覧に出る自分の世界は0でも、
        送れば拒まれるフォームを見せない（A で世界を作れるよう、A の編集者にしておく）。
        """
        make_editor(self.b_owner)
        ledger_b = DjangoWorldRepository(self.workspace_b.id)
        for number in range(MAX_WORLDS_PER_OWNER - 1):  # B の世界（setUpTestData）と合わせて上限ちょうど
            ledger_b.save(
                World(
                    name=f"{B_MARK}世界{number}",
                    seed=1,
                    start_year=YEAR,
                    owner_id=self.b_owner.id,
                    workspace_id=self.workspace_b.id,
                )
            )
        self.client.force_login(self.b_owner)

        response = self.client.get(ws_url("pennant_index"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["worlds"].mine, [])
        self.assertTrue(response.context["worlds"].at_limit)
        self.assertContains(response, WORLD_LIMIT_MESSAGE)
        self.assertContains(response, "ほかのワークスペースの世界も数えます")
        self.assertNotContains(response, 'name="name"')

    def test_below_the_limit_the_form_is_shown(self):
        """検査が空振りしていないこと（B に1つだけなら、A の編集者には A で作成フォームが出る）。"""
        self.client.force_login(make_editor(self.b_owner))

        response = self.client.get(ws_url("pennant_index"))

        self.assertFalse(response.context["worlds"].at_limit)
        self.assertContains(response, 'name="name"')


class WorldScopeIsVerifiedAgainstTheLedgerTest(WorkspaceCase):
    """`_world_scope` は、今の URL のワークスペースの台帳で引けた世界にだけ範囲を作る。

    見出し（`_world_context`）が台帳を読んだ後なら、台帳を読み直さない（世界の画面のクエリを増やさない）。
    """

    def _request(self, workspace=None):
        """`workspace_view` を通った後の要求（key で解決したワークスペースを載せる。省けば A）。"""
        request = RequestFactory().get("/")
        request.workspace = build_workspace_access_query().resolve((workspace or self.workspace).key, None)
        return request

    def test_a_world_of_another_workspace_gets_no_scope(self):
        with self.assertRaises(Http404):
            views._world_scope(self._request(), self.b_world_id)

    def test_the_header_of_another_workspaces_world_is_404(self):
        with self.assertRaises(Http404):
            views._world_context(self._request(), self.b_world_id)

    def test_a_request_without_a_resolved_workspace_fails_loudly(self):
        """`workspace_view` を通らない要求で範囲を作らない（既定のワークスペースに黙って落とさない）。"""
        for read in (views._real_scope, lambda request: views._world_scope(request, self.a_world_id)):
            with self.subTest(read=read), self.assertRaises(views.WorkspaceNotResolved):
                read(RequestFactory().get("/"))

    def test_a_world_verified_in_another_workspace_is_not_trusted(self):
        """確かめた世界は（ワークスペース, 世界）の組で覚える。B で確かめた id を A の範囲では通さない。"""
        request = self._request(self.workspace_b)
        views._world_context(request, self.b_world_id)
        request.workspace = self._request().workspace

        with self.assertRaises(Http404):
            views._world_scope(request, self.b_world_id)

    def test_without_a_header_the_ledger_is_read_once_per_request(self):
        request = self._request()
        with CaptureQueriesContext(connection) as captured:
            scope = views._world_scope(request, self.a_world_id)
            views._world_scope(request, self.a_world_id)
        self.assertEqual(scope, WorldScope.pennant(self.workspace.id, self.a_world_id))
        self.assertEqual(len(captured), 1, [q["sql"] for q in captured])

    def test_a_verified_world_does_not_open_another_workspaces_world_in_the_same_request(self):
        """同じ要求で A の世界を見出しで確かめた後でも、B の世界の範囲は作らない（確かめた id だけを通す）。"""
        request = self._request()
        views._world_context(request, self.a_world_id)

        with self.assertRaises(Http404):
            views._world_scope(request, self.b_world_id)

    def test_after_the_header_the_ledger_is_not_read_again(self):
        request = self._request()
        views._world_context(request, self.a_world_id)
        with self.assertNumQueries(0):
            scope = views._world_scope(request, self.a_world_id)
        self.assertEqual(scope, WorldScope.pennant(self.workspace.id, self.a_world_id))


class WorldCreationWorkspaceTest(WorkspaceCase):
    """世界は、組み立て口に渡した実データの範囲のワークスペースに属す（一時関数のワークスペースではない）。"""

    def test_a_world_forked_from_b_belongs_to_b(self):
        # B の世界は `setUpTestData` が `build_pennant_world_service(real_scope(B))` で作った
        self.assertEqual(orm_models.PennantWorld.objects.get(id=self.b_world_id).workspace_id, self.workspace_b.id)
        world = DjangoWorldRepository(self.workspace_b.id).find_by_id(self.b_world_id)
        self.assertEqual(world.scope, WorldScope.pennant(self.workspace_b.id, self.b_world_id))


class ScorebookApiWithoutWorkspaceTest(TestCase):
    """無い key のスコアブックの保存 API は、ほかのエラーと同じ JSON の形で 404 を返す（画面の 404 を返さない）。"""

    def test_the_api_returns_the_json_error(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

        response = self.client.post(ws_url("api_game_scorebook", 1), data="{}", content_type="application/json")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "error": "ワークスペースが見つかりません。"})


# --- URL（W2 T4）: `/w/<key>/` の下の全パターン ---

# 種類。PAGE は GET で開く画面（読み書きが同居する画面も GET で確かめる）、WRITE は POST だけの書き込み
# （GET はホームへ戻すだけ）、API は JSON の保存 API（POST）
PAGE = "page"
WRITE = "write"
API = "api"


@dataclass(frozen=True)
class Side:
    """1つのワークスペースの、URL の引数に入れる id と年。"""

    team: int
    player: int
    game: int
    league: int
    year: int
    world: int
    world_team: int
    world_player: int
    world_league: int
    world_game: int
    world_year: int


# id の引数（年は id ではない。年だけの URL は「ほかのワークスペースの id」の検査の対象外）
ID_ARGS = frozenset(
    {"team", "player", "game", "league", "world", "world_team", "world_player", "world_league", "world_game"}
)

# 分類表。`workspace_urlpatterns` のパターンは、ちょうどここに載る（新しい URL を足したら、引数と種類をここで決める）。
# URL 名 → (引数（`Side` の属性名）, 種類, 閲覧にもログインが要るか)
URL_TABLE: dict[str, tuple[tuple[str, ...], str, bool]] = {
    "dashboard": ((), PAGE, False),
    "team_list": ((), PAGE, False),
    "player_search": ((), PAGE, False),
    "standings": ((), PAGE, False),
    "standings_by_year": (("year",), PAGE, False),
    "league_detail": (("league",), PAGE, False),
    "league_detail_by_year": (("league", "year"), PAGE, False),
    "league_titles": (("league",), PAGE, False),
    "league_titles_by_year": (("league", "year"), PAGE, False),
    "league_stats": (("league",), PAGE, False),
    "game_list": ((), PAGE, False),
    "game_create": ((), PAGE, True),
    "game_detail": (("game",), PAGE, False),
    "game_edit": (("game",), PAGE, True),
    "api_game_scorebook": (("game",), API, False),
    "analysis_index": ((), PAGE, False),
    "player_list": (("team",), PAGE, False),
    "team_analysis": (("team",), PAGE, False),
    "player_detail": (("team", "player"), PAGE, False),
    "player_edit": (("team", "player"), PAGE, True),
    "pennant_index": ((), PAGE, False),
    "pennant_world": (("world",), PAGE, False),
    "pennant_advance": (("world",), WRITE, False),
    "pennant_close_season": (("world",), WRITE, False),
    "pennant_offseason": (("world", "world_year"), PAGE, False),
    "pennant_history": (("world",), PAGE, False),
    "pennant_team_history": (("world", "world_team"), PAGE, False),
    "pennant_delete": (("world",), PAGE, False),
    "pennant_club": (("world",), PAGE, False),
    "pennant_standings": (("world",), PAGE, False),
    "pennant_standings_by_year": (("world", "world_year"), PAGE, False),
    "pennant_league_detail": (("world", "world_league"), PAGE, False),
    "pennant_league_detail_by_year": (("world", "world_league", "world_year"), PAGE, False),
    "pennant_league_titles": (("world", "world_league"), PAGE, False),
    "pennant_league_titles_by_year": (("world", "world_league", "world_year"), PAGE, False),
    "pennant_league_stats": (("world", "world_league"), PAGE, False),
    "pennant_team_list": (("world",), PAGE, False),
    "pennant_game_list": (("world",), PAGE, False),
    "pennant_game_detail": (("world", "world_game"), PAGE, False),
    "pennant_player_list": (("world", "world_team"), PAGE, False),
    "pennant_analysis_index": (("world",), PAGE, False),
    "pennant_team_analysis": (("world", "world_team"), PAGE, False),
    "pennant_player_detail": (("world", "world_team", "world_player"), PAGE, False),
}

# 下ごしらえにその年のオフの結果が無いので、自分のワークスペースでも 404 になる画面（ゲートの検査は別に効く）
ABSENT_IN_FIXTURE = frozenset({"pennant_offseason"})

# A の画面のリンクが向いてよい、ワークスペースの外の行き先（`/` は完全一致）
OUTSIDE_PREFIXES = ("/accounts/", "/admin/", "/static/", "?", "#", "https://cdn.jsdelivr.net/")
LINK = re.compile(r'(?:href|action)="([^"]*)"')


def _world_side_ids(world_id: int) -> tuple[int, int, int, int]:
    """世界の (球団, 選手, リーグ, 試合)。試合はここで1つ置く（世界の試合の画面を開くため）。"""
    league = orm_models.League.objects.filter(world_id=world_id).order_by("id").first()
    assert league is not None
    home, away = orm_models.Team.objects.filter(league=league).order_by("id")[:2]
    player = orm_models.PlayerStint.objects.filter(team=home).order_by("id").values_list("player_id", flat=True)[0]
    game = orm_models.Game.objects.create(
        year=YEAR, played_on=date(YEAR, 4, 2), home_team=home, away_team=away, home_score=1, away_score=0
    )
    return home.id, player, league.id, game.id


class EveryWorkspaceUrlTest(WorkspaceCase):
    """`workspace_urlpatterns` の全パターンを、分類表で A と B について検査する（W2 T4。設計書 7.）。

    - 表に無いパターン・表にしか無い名前があれば落ちる。すべて `workspace_view` で包まれている。
    - (1)(5) A の key の下で B の id（チーム・選手・試合・リーグ・世界とその中の id）を渡すと 404。
    - (2)(6)(7) A の画面（未ログイン・A の持ち主）に B の目印が出ず、リンクは A の key の下かワークスペースの外だけ。
    - (3) B を非公開にすると、未ログインはログインへ（`next` つき）、A の持ち主（B の部外者）は 404、
      B の編集者と担当はワークスペースの門を通る（`request.workspace` が B になる。画面ごとの権限はその先で決まる）。
    """

    side_a: Side
    side_b: Side

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        a_world = _world_side_ids(cls.a_world_id)
        b_world = _world_side_ids(cls.b_world_id)
        cls.side_a = Side(
            cls.team.id, cls.a_batters[0], cls.a_game_id, cls.league.id, YEAR, cls.a_world_id, *a_world, YEAR
        )
        cls.side_b = Side(
            cls.b_team.id, cls.b_batters[0], cls.b_game_ids[1], cls.b_league.id, B_YEAR, cls.b_world_id, *b_world, YEAR
        )

    def _url(self, name: str, side: Side, workspace=None) -> str:
        args, _, _ = URL_TABLE[name]
        return ws_url(name, *(getattr(side, arg) for arg in args), workspace=workspace)

    def _send(self, name: str, url: str):
        _, kind, _ = URL_TABLE[name]
        if kind == API:
            return self.client.post(url, data="{}", content_type="application/json")
        if kind == WRITE:
            return self.client.post(url, {})
        return self.client.get(url)

    def test_every_pattern_is_in_the_table(self):
        names = [pattern.name for pattern in workspace_urlpatterns]
        self.assertEqual(len(names), len(set(names)), "同じ名前のパターンがあります")
        self.assertEqual(sorted(set(names) - set(URL_TABLE)), [], "分類表に無い URL があります（表に足してください）")
        self.assertEqual(sorted(set(URL_TABLE) - set(names)), [], "表に、もう無い URL が残っています")

    def test_every_pattern_goes_through_workspace_view(self):
        """包み忘れが無い（`_w()` を通らずに `path()` で足した URL は、解決も公開範囲の判定も通らない）。"""
        wrapper_code = views.workspace_view(lambda request: None).__code__
        for pattern in workspace_urlpatterns:
            with self.subTest(name=pattern.name):
                callback = pattern.callback
                self.assertIs(callback.__code__, wrapper_code, "workspace_view で包まれていません")
                options = inspect.getclosurevars(callback).nonlocals
                _, kind, login = URL_TABLE[pattern.name]
                self.assertEqual((options["login"], options["json"]), (login, kind == API))

    def test_the_other_workspaces_ids_are_404_under_a(self):
        """(1)(5) A の key の下で B の id を渡すと 404（API は JSON の 404）。A の持ち主でも開けない。"""
        self.client.force_login(self.workspace.owner)
        checked = 0
        for name, (args, kind, _) in URL_TABLE.items():
            if not ID_ARGS.intersection(args):
                continue
            url = self._url(name, self.side_b)
            with self.subTest(name=name, url=url):
                response = self._send(name, url)
                self.assertEqual(response.status_code, 404)
                if kind == API:
                    self.assertFalse(response.json()["ok"])
                checked += 1
        self.assertGreaterEqual(checked, 30, "検査の対象を拾えていません")

    def test_the_pages_of_a_show_nothing_of_b_and_link_only_inside_a(self):
        """(2)(6)(7) A の画面に B の目印が出ず、リンクは A の key の下かワークスペースの外（`/` など）だけ。"""
        inside = f"/w/{self.workspace.key}/"
        for user in (None, self.workspace.owner):
            if user is not None:
                self.client.force_login(user)
            for name, (_, kind, _) in URL_TABLE.items():
                if kind != PAGE:
                    continue
                urls = [self._url(name, self.side_a)]
                if name == "player_search":
                    # 検索語は画面に出るので、目印そのものではなく目印の一部で探す
                    urls += [f"{urls[0]}?q=目印", f"{urls[0]}?q=打者"]
                for url in urls:
                    with self.subTest(user=str(user), url=url):
                        response = self.client.get(url)
                        expected = {200, 302, 403} | ({404} if name in ABSENT_IN_FIXTURE else set())
                        self.assertIn(response.status_code, expected)
                        if response.status_code == 302:
                            location = response["Location"]
                            self.assertTrue(location.startswith((inside, "/accounts/login/")), location)
                            continue
                        content = response.content.decode()
                        self.assertNotIn(B_MARK, content)
                        for link in LINK.findall(content):
                            self.assertTrue(
                                link == "/" or link.startswith((inside, *OUTSIDE_PREFIXES)),
                                f"{url} のリンク {link} が A の外へ出ています",
                            )

    def test_a_private_workspace_is_closed_to_outsiders(self):
        """(3) 非公開の B: 未ログインはログインへ、部外者（A の持ち主）は 404、B の編集者と担当は門を通る。"""
        orm_models.Workspace.objects.filter(id=self.workspace_b.id).update(visibility=Visibility.PRIVATE.value)
        editor, manager = _b_members(self)
        for name, (_, kind, _) in URL_TABLE.items():
            url = self._url(name, self.side_b, workspace=self.workspace_b)
            with self.subTest(name=name, user="anonymous"):
                self.client.logout()
                response = self._send(name, url)
                if kind == API:
                    self.assertEqual(response.status_code, 403)
                else:
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response["Location"], f"{reverse('login')}?next={url}")
            with self.subTest(name=name, user="outsider"):
                self.client.force_login(self.workspace.owner)
                response = self._send(name, url)
                self.assertEqual(response.status_code, 404)
                self.assertFalse(hasattr(response.wsgi_request, "workspace"), "部外者に門を通した")
            for member in (editor, manager):
                with self.subTest(name=name, user=member.username):
                    self.client.force_login(member)
                    response = self._send(name, url)
                    self.assertEqual(response.wsgi_request.workspace.key, self.workspace_b.key)

    def test_a_public_workspace_is_open_to_everyone(self):
        """空振りの防止: 公開の B なら、A の持ち主（部外者）も B の key の画面を読める。"""
        self.client.force_login(self.workspace.owner)
        for name in ("dashboard", "team_list", "player_list", "pennant_world"):
            with self.subTest(name=name):
                response = self.client.get(self._url(name, self.side_b, workspace=self.workspace_b))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, B_MARK)
