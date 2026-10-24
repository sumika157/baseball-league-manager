"""ワークスペースをまたいで行が漏れないこと（W1b T2。#172）。

範囲（`WorldScope`）がワークスペースを持つようになり、実データの条件は「そのワークスペースのリーグ」になった。
ワークスペース A（既定・id が最小＝今の URL が解決するもの）と B に実データを置き、B の名前は A のどの名前とも
重ならない**目印**にする。A の範囲で読んだもの・今の URL の画面に目印が出たら、それは漏れ。

- (1) `repositories`・`queries` の `Django*` をモジュールから列挙し、すべてを検査の表（`CHECKS`）で検査する。
  表に無いクラスがあれば落ちる（新しい実装を足したら、漏れないかをここで決めさせる）。
- (2) A の範囲で B の id を `find_by_id` すると NotFound。
- (3) 球場（W1b T4）: 範囲の球場はそのワークスペースの球場だけ（ペナントの範囲も同じ）。A の球団が B の球場を
  指していても、A の範囲の読み取りは本拠地なしとして読む。A の範囲で B の球場を本拠地にして保存すると
  `StadiumNotFound` で、チームも選手も書かれない。管理画面の組み合わせは `test_admin_workspaces.py`。
- (4) 世界の台帳（`DjangoWorldRepository`・`DjangoWorldSummaryQuery`。W1b T3）: A の台帳に B の世界が出ず、
  B の世界の id は `WorldNotFound`。A の台帳で B の世界を保存・削除できず、何も変わらない。今の URL で
  B の世界（`/pennant/<B の世界>/` とその下）を開くと 404、`/pennant/` には A の世界だけが出る。
- (5) `players_in(real(A))` に B の選手も A の世界の選手も出ない。
- 今の URL の画面に B の目印が出ない（一時関数が A に解決する）。一時関数は1つの要求で1回だけ読む。
"""

import inspect
from collections.abc import Callable
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
    DjangoSimulationContextQuery,
    DjangoTeamAnalysisQuery,
    DjangoTeamListQuery,
    DjangoTeamPermissionQuery,
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

from ..helpers import (
    build_service,
    make_league,
    make_stadium,
    make_workspace,
    play_game,
    real_scope,
    register_lineup,
    register_players,
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
    history = query.load_service_history([*t.b_batters, t.b_pitcher], YEAR)
    t.assertEqual((history.spans, history.stints, history.declarations), ([], [], []))
    t.assertEqual(query.load_service_history([], YEAR).records_from_year, YEAR, "B の試合の年に引きずられた")


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
    DjangoWorldRepository: _world_repository,
    DjangoWorldSummaryQuery: _world_summary_query,
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


class CurrentUrlsShowOnlyTheLegacyWorkspaceTest(WorkspaceCase):
    """今の URL（ワークスペースを含まない）は一時関数で A に解決し、B の目印を出さない。"""

    def _pages(self) -> list[str]:
        return [
            reverse("dashboard"),
            reverse("team_list"),
            reverse("standings"),
            reverse("standings_by_year", args=[YEAR]),
            reverse("game_list"),
            # 検索語は画面に出るので、目印そのものではなく目印の一部で探す
            f"{reverse('player_search')}?q=目印選手",
            f"{reverse('player_search')}?q=目印",
            reverse("league_detail", args=[self.league.id]),
            reverse("analysis_index"),
        ]

    def test_the_pages_do_not_show_the_other_workspace(self):
        for url in self._pages():
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, B_MARK)

    def test_the_pages_show_the_legacy_workspace(self):
        """検査が空振りしていないこと（A の行は出ている）。"""
        self.assertContains(self.client.get(reverse("team_list")), self.team.name)
        self.assertContains(self.client.get(f"{reverse('player_search')}?q=A打者"), "A打者1")

    def test_pages_by_the_other_workspaces_ids_are_404(self):
        for url in (
            reverse("player_list", args=[self.b_team.id]),
            reverse("league_detail", args=[self.b_league.id]),
            reverse("game_detail", args=[self.b_game_ids[0]]),
            reverse("team_analysis", args=[self.b_team.id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_workspace_is_read_once_per_request(self):
        """一時関数は1つの要求で1回だけ読む（範囲・権限の判定・API が別々に読まない）。"""
        staff = User.objects.create_user(username="staff-once", password="x", is_staff=True)
        self.client.force_login(staff)
        # 試合一覧は、画面の範囲と「登録の導線を出すか」（権限の判定）の両方で実データの範囲を使う
        for url in (reverse("game_list"), reverse("player_list", args=[self.team.id])):
            with self.subTest(url=url), CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(url).status_code, 200)
            reads = [q["sql"] for q in captured if 'FROM "myapp_workspace"' in q["sql"]]
            self.assertEqual(len(reads), 1, reads)


class NoWorkspaceTest(TestCase):
    """ワークスペースが1つも無い DB では、今の URL は 404（決定 2026-10-07 Q2）。管理画面のトップは開ける。"""

    def test_the_site_pages_are_404(self):
        for url in (reverse("dashboard"), reverse("team_list"), reverse("pennant_index")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_admin_index_still_opens_to_create_the_first_workspace(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "データの概況")


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
        response = self.client.get(reverse("team_list"))

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
    """(4) 今の URL（A に解決する）で B の世界を開くと 404。`/pennant/` には A の世界だけが出る。"""

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
            reverse("pennant_game_detail", args=[world, game.id]),
            reverse("pennant_world", args=[world]),
            reverse("pennant_history", args=[world]),
            reverse("pennant_team_history", args=[world, team]),
            reverse("pennant_offseason", args=[world, YEAR]),
            reverse("pennant_delete", args=[world]),
            reverse("pennant_club", args=[world]),
            reverse("pennant_standings", args=[world]),
            reverse("pennant_standings_by_year", args=[world, YEAR]),
            reverse("pennant_league_detail", args=[world, league]),
            reverse("pennant_league_titles", args=[world, league]),
            reverse("pennant_league_stats", args=[world, league]),
            reverse("pennant_team_list", args=[world]),
            reverse("pennant_game_list", args=[world]),
            reverse("pennant_player_list", args=[world, team]),
            reverse("pennant_analysis_index", args=[world]),
            reverse("pennant_team_analysis", args=[world, team]),
            reverse("pennant_player_detail", args=[world, team, player]),
        ]

    def test_the_pages_of_bs_world_are_404(self):
        # B の世界のオーナーでも、今の URL（A）からは開けない
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
            reverse("pennant_advance", args=[self.b_world_id]),
            reverse("pennant_close_season", args=[self.b_world_id]),
            reverse("pennant_delete", args=[self.b_world_id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {"target": "day", "confirm_name": B_WORLD}).status_code, 404)
        self.assertEqual(list(orm_models.PennantWorld.objects.order_by("id").values()), worlds_before)
        self.assertEqual(b_games.count(), games_before)

    def test_the_club_writes_to_bs_world_are_404_and_change_nothing(self):
        """編成（区画の手動化・保存）と戦力外の指名の POST も、B の世界なら 404 で何も書かない。"""
        self.client.force_login(self.b_owner)
        url = reverse("pennant_club", args=[self.b_world_id])
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
            reverse("pennant_world", args=[self.a_world_id]),
            reverse("pennant_team_list", args=[self.a_world_id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_index_lists_only_as_worlds(self):
        self.client.force_login(self.b_owner)

        response = self.client.get(reverse("pennant_index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, A_WORLD)
        self.assertNotContains(response, B_MARK)

    def test_worlds_in_other_workspaces_count_toward_the_limit(self):
        """上限は全ワークスペースで数える（作成の検査と同じ）。

        B に上限まで世界を持つ人には、A でも作成フォームを出さない。A の一覧に出る自分の世界は0でも、
        送れば拒まれるフォームを見せない。
        """
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

        response = self.client.get(reverse("pennant_index"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["worlds"].mine, [])
        self.assertTrue(response.context["worlds"].at_limit)
        self.assertContains(response, WORLD_LIMIT_MESSAGE)
        self.assertContains(response, "ほかのワークスペースの世界も数えます")
        self.assertNotContains(response, 'name="name"')

    def test_below_the_limit_the_form_is_shown(self):
        """検査が空振りしていないこと（B に1つだけなら、A で作成フォームが出る）。"""
        self.client.force_login(self.b_owner)

        response = self.client.get(reverse("pennant_index"))

        self.assertFalse(response.context["worlds"].at_limit)
        self.assertContains(response, 'name="name"')


class WorldScopeIsVerifiedAgainstTheLedgerTest(WorkspaceCase):
    """`_world_scope` は、今の URL のワークスペースの台帳で引けた世界にだけ範囲を作る。

    見出し（`_world_context`）が台帳を読んだ後なら、台帳を読み直さない（世界の画面のクエリを増やさない）。
    """

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def test_a_world_of_another_workspace_gets_no_scope(self):
        with self.assertRaises(Http404):
            views._world_scope(self.factory.get("/"), self.b_world_id)

    def test_the_header_of_another_workspaces_world_is_404(self):
        with self.assertRaises(Http404):
            views._world_context(self.factory.get("/"), self.b_world_id)

    def test_without_a_header_the_ledger_is_read_once_per_request(self):
        request = self.factory.get("/")
        views._workspace_id(request)
        with CaptureQueriesContext(connection) as captured:
            scope = views._world_scope(request, self.a_world_id)
            views._world_scope(request, self.a_world_id)
        self.assertEqual(scope, WorldScope.pennant(self.workspace.id, self.a_world_id))
        self.assertEqual(len(captured), 1, [q["sql"] for q in captured])

    def test_a_verified_world_does_not_open_another_workspaces_world_in_the_same_request(self):
        """同じ要求で A の世界を見出しで確かめた後でも、B の世界の範囲は作らない（確かめた id だけを通す）。"""
        request = self.factory.get("/")
        views._world_context(request, self.a_world_id)

        with self.assertRaises(Http404):
            views._world_scope(request, self.b_world_id)

    def test_after_the_header_the_ledger_is_not_read_again(self):
        request = self.factory.get("/")
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
    """ワークスペースが1つも無いとき、スコアブックの保存 API はほかのエラーと同じ JSON の形で 404 を返す。"""

    def test_the_api_returns_the_json_error(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

        response = self.client.post(
            reverse("api_game_scorebook", args=[1]), data="{}", content_type="application/json"
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "error": "ワークスペースがありません。"})
