"""依存の組み立てに関する検査。

アプリケーションサービスはインターフェース（domain/repositories.py・
application/queries.py）だけに依存し、実装は組み立ての1か所（build_service）で
差し込む。実装側でメソッド名が変わっても、この検査が無いと画面を開くまで
気づけないため、機械的に確かめる。

あわせて、**リポジトリと参照クエリが世界の範囲（`WorldScope`）を必須で受け取る**こと、
組み立て口が範囲を既定値なしで受け取り、範囲の取り違え（ペナント専用に実データ、実データ専用にペナント）を弾くことを確かめる。
"""

import inspect
from datetime import date
from inspect import Parameter, signature
from pathlib import Path

from django.test import SimpleTestCase

from myapp.application.club_management import ClubManagementService
from myapp.application.game_recording import GameRecordingService
from myapp.application.game_view import GameViewService
from myapp.application.pennant_club_details import ClubDetailsService
from myapp.application.pennant_history import PennantHistoryService
from myapp.application.pennant_home import PennantHomeService
from myapp.application.pennant_offseason import PennantOffseasonService
from myapp.application.pennant_offseason_view import OffseasonViewService
from myapp.application.pennant_ratings import PennantRatingsViewService
from myapp.application.pennant_season import PennantSeasonService
from myapp.application.pennant_view import PennantWorldViewService
from myapp.application.pennant_world import PennantWorldService
from myapp.application.queries import (
    AwardQuery,
    FieldingTotalsQuery,
    GameListQuery,
    HomeStadiumQuery,
    OffseasonQuery,
    PennantActivityQuery,
    RatingsHistoryQuery,
    SimulationContextQuery,
    TeamAnalysisQuery,
    TeamListQuery,
    WorldSummaryQuery,
)
from myapp.application.roster import RosterService
from myapp.application.services import TeamApplicationService
from myapp.application.team_analysis import TeamAnalysisService
from myapp.domain.exceptions import InvalidWorld
from myapp.domain.pennant.world import WorldScope
from myapp.domain.repositories import (
    AwardRepository,
    ClubPlanRepository,
    FixtureRepository,
    GameRepository,
    LeagueRepository,
    RatingsRepository,
    ReleasePlanRepository,
    TeamRepository,
    WorldRepository,
)
from myapp.infrastructure import queries, repositories
from myapp.infrastructure.queries import (
    DjangoAwardQuery,
    DjangoFieldingTotalsQuery,
    DjangoGameListQuery,
    DjangoHomeStadiumQuery,
    DjangoOffseasonQuery,
    DjangoPennantActivityQuery,
    DjangoRatingsHistoryQuery,
    DjangoSimulationContextQuery,
    DjangoTeamAnalysisQuery,
    DjangoTeamListQuery,
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
from myapp.presentation import views
from myapp.presentation.views import (
    build_club_details_service,
    build_club_service,
    build_game_view_service,
    build_offseason_view_service,
    build_pennant_history_service,
    build_pennant_home_service,
    build_pennant_offseason_service,
    build_pennant_ratings_service,
    build_pennant_season_service,
    build_pennant_world_service,
    build_pennant_world_view,
    build_permission_query,
    build_player_search_query,
    build_recording_service,
    build_roster_service,
    build_service,
    build_team_analysis_service,
)

# 組み立てるときに DB を読まないので、ワークスペースの id はリテラルでよい（SimpleTestCase のまま通す）
REAL = WorldScope.real(1)
WORLD7 = WorldScope.pennant(1, 7)
WORLD8 = WorldScope.pennant(1, 8)

# 組み立て口の分類。views.build_* は、ちょうどどれか1つに入る（`test_every_builder_is_classified_once`）。
# 実データでもペナントでも組み立てられる（範囲に応じて年の基準が変わる）
BOTH_BUILDERS = (build_service, build_game_view_service, build_team_analysis_service)
# 実データ専用。ペナントの範囲を渡すと InvalidWorld
REAL_ONLY_BUILDERS = (
    build_recording_service,
    build_roster_service,
    build_permission_query,
    build_player_search_query,
    build_pennant_world_service,
)
# 世界の台帳だけを読む。範囲ではなくワークスペースの id を取る（台帳をそのワークスペースで絞り、世界ごとの範囲を作る）
LEDGER_BUILDERS = (build_pennant_world_view,)
# ペナント専用。実データの範囲を渡すと InvalidWorld
PENNANT_ONLY_BUILDERS = (
    build_pennant_season_service,
    build_pennant_offseason_service,
    build_pennant_ratings_service,
    build_pennant_home_service,
    build_pennant_history_service,
    build_club_service,
    build_club_details_service,
    build_offseason_view_service,
)

# 範囲もワークスペースも受け取らずに作れる実装。W1b T3 で世界の台帳もワークスペースを受け取るようになり、空になった。
# ここに足す前に、範囲かワークスペースで絞れないかを考える（全範囲を読む実装は、ワークスペースをまたいで行を漏らす）
UNSCOPED: set[type] = set()
# 世界の台帳。範囲（ワークスペースと世界）ではなく、ワークスペースの id だけを受け取る
LEDGERS = {DjangoWorldRepository, DjangoWorldSummaryQuery}


class ProtocolConformanceTest(SimpleTestCase):
    IMPLEMENTATIONS = [
        (DjangoTeamRepository(REAL), TeamRepository),
        (DjangoGameRepository(REAL), GameRepository),
        (DjangoLeagueRepository(REAL), LeagueRepository),
        (DjangoWorldRepository(1), WorldRepository),
        (DjangoRatingsRepository(REAL), RatingsRepository),
        (DjangoFixtureRepository(REAL), FixtureRepository),
        (DjangoClubPlanRepository(REAL), ClubPlanRepository),
        (DjangoReleasePlanRepository(REAL), ReleasePlanRepository),
        (DjangoAwardRepository(REAL), AwardRepository),
        (DjangoAwardQuery(REAL), AwardQuery),
        (DjangoSimulationContextQuery(REAL), SimulationContextQuery),
        (DjangoFieldingTotalsQuery(REAL), FieldingTotalsQuery),
        (DjangoTeamListQuery(REAL), TeamListQuery),
        (DjangoGameListQuery(REAL), GameListQuery),
        (DjangoTeamAnalysisQuery(REAL), TeamAnalysisQuery),
        (DjangoPennantActivityQuery(REAL), PennantActivityQuery),
        (DjangoWorldSummaryQuery(1), WorldSummaryQuery),
        (DjangoOffseasonQuery(REAL), OffseasonQuery),
        (DjangoRatingsHistoryQuery(REAL), RatingsHistoryQuery),
        (DjangoHomeStadiumQuery(REAL), HomeStadiumQuery),
    ]

    def test_implementations_satisfy_interfaces(self):
        """infrastructure の実装がインターフェースを満たしていること。"""
        for implementation, interface in self.IMPLEMENTATIONS:
            with self.subTest(implementation=type(implementation).__name__):
                self.assertIsInstance(implementation, interface)


class ScopeIsRequiredTest(SimpleTestCase):
    """範囲（`scope`）かワークスペース（`workspace_id`）を渡さずに作れるリポジトリ・参照クエリがあってはならない。

    既定値があると「渡し忘れ」が型検査にも実行にも引っかからず、ペナントの画面に実データが出る・
    ほかのワークスペースの行が出る形で現れる。新しい Django* の実装を足したときも、ここが拾う。
    世界の台帳（`LEDGERS`）だけがワークスペースを受け取り、ほかはすべて範囲を受け取る。
    """

    def _implementations(self):
        for module in (repositories, queries):
            for name, cls in inspect.getmembers(module, inspect.isclass):
                if name.startswith("Django") and cls.__module__ == module.__name__ and cls not in UNSCOPED:
                    yield cls

    def test_nothing_is_unscoped(self):
        self.assertEqual(UNSCOPED, set(), "範囲もワークスペースも受け取らない実装があります")

    def test_every_implementation_requires_a_scope_or_a_workspace(self):
        found = list(self._implementations())
        self.assertGreaterEqual(len(found), 20, "検査の対象を拾えていません")
        self.assertLessEqual(LEDGERS, set(found), "台帳を拾えていません")
        for cls in found:
            with self.subTest(implementation=cls.__name__):
                expected = "workspace_id" if cls in LEDGERS else "scope"
                parameters = signature(cls.__init__).parameters
                self.assertEqual(list(parameters), ["self", expected], f"{expected} だけを受け取っていません")
                self.assertIs(parameters[expected].default, Parameter.empty, f"{expected} に既定値があります")

    def test_a_scope_cannot_be_omitted(self):
        with self.assertRaises(TypeError):
            DjangoTeamRepository()  # type: ignore[call-arg]

    def test_a_workspace_cannot_be_omitted_from_the_ledgers(self):
        for cls in LEDGERS:
            with self.subTest(implementation=cls.__name__), self.assertRaises(TypeError):
                cls()


class BuildServiceTest(SimpleTestCase):
    def test_all_dependencies_are_wired(self):
        """組み立てたサービスに、依存が1つも欠けていないこと。

        欠けたまま作れると、使う画面によって「None に find_all は無い」で
        落ちるサービスができてしまう。

        検査対象は `__init__` の引数に対応する属性だけ（引数 `teams` →
        属性 `_teams` の対応を前提にする）。内部の控えは初期値が None を
        取りうるので、`vars()` を丸ごと見ない。
        """
        self._assert_wired(build_service(REAL), TeamApplicationService)

    def test_recording_service_dependencies_are_wired(self):
        """スコアブックの保存サービスも同じ検査にかける。

        組み立てが増えるたびに検査を書き忘れると、この形の事故だけが素通りする。
        """
        self._assert_wired(build_recording_service(REAL), GameRecordingService)

    def test_pennant_world_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_world_service(REAL), PennantWorldService)

    def test_pennant_season_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_season_service(WORLD7), PennantSeasonService)

    def test_pennant_offseason_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_offseason_service(WORLD7), PennantOffseasonService)

    def test_world_view_service_dependencies_are_wired(self):
        """世界の範囲の参照サービスも、実データ用と同じ検査にかける（依存は実データ用と同じ全部）。"""
        self._assert_wired(build_service(WORLD7), TeamApplicationService)

    def test_pennant_home_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_home_service(WORLD7), PennantHomeService)

    def test_pennant_history_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_history_service(WORLD7), PennantHistoryService)

    def test_pennant_world_view_dependencies_are_wired(self):
        self._assert_wired(build_pennant_world_view(1), PennantWorldViewService)

    def test_club_service_dependencies_are_wired(self):
        self._assert_wired(build_club_service(WORLD7), ClubManagementService)

    def test_club_details_service_dependencies_are_wired(self):
        self._assert_wired(build_club_details_service(WORLD7), ClubDetailsService)

    def test_pennant_ratings_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_ratings_service(WORLD7), PennantRatingsViewService)

    def test_team_analysis_service_dependencies_are_wired(self):
        """戦力分析のサービスも同じ検査にかける（組み立て口は build_*() 関数だけ）。"""
        self._assert_wired(build_team_analysis_service(REAL), TeamAnalysisService)

    def test_world_team_analysis_service_dependencies_are_wired(self):
        self._assert_wired(build_team_analysis_service(WORLD7), TeamAnalysisService)

    def test_roster_service_dependencies_are_wired(self):
        """契約区分（昇格）のサービスも同じ検査にかける（組み立て口は build_*() 関数だけ）。"""
        self._assert_wired(build_roster_service(REAL), RosterService)

    def test_game_view_service_dependencies_are_wired(self):
        """試合の参照サービスも同じ検査にかける（組み立て口は build_*() 関数だけ）。"""
        self._assert_wired(build_game_view_service(REAL), GameViewService)

    def test_world_game_view_service_dependencies_are_wired(self):
        self._assert_wired(build_game_view_service(WORLD7), GameViewService)

    def _assert_wired(self, service, cls):
        parameters = [name for name in signature(cls.__init__).parameters if name != "self"]
        self.assertTrue(parameters, "依存が1つも宣言されていません")

        missing = [name for name in parameters if getattr(service, f"_{name}", None) is None]
        self.assertEqual(missing, [], f"依存が渡されていません: {missing}")

    def test_requires_every_dependency(self):
        """依存を省略したサービスは作れないこと。"""
        with self.assertRaises(TypeError):
            TeamApplicationService(teams=DjangoTeamRepository(REAL))  # type: ignore[call-arg]


class WorldViewScopeTest(SimpleTestCase):
    """世界の画面を読むサービスは、渡された世界の範囲だけを読む。"""

    SCOPED = (
        "_teams",
        "_team_list_query",
        "_games",
        "_leagues",
        "_game_list_query",
        "_player_fielding_query",
        "_player_stats_query",
    )

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_service(WORLD7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)

    # ロスターの更新（年を省くと現実の今日の年が入るものを含む）。参照のサービスには置かない
    ROSTER_UPDATES = (
        "register_player",
        "update_player",
        "retire_player",
        "transfer_player",
        "appoint_captain",
        "remove_captain",
    )

    def test_it_cannot_update_the_roster(self):
        """世界の画面を読む組み立てからは、ロスターの更新を呼べない（更新は `RosterService` だけが持つ）。"""
        service = build_service(WORLD7)
        for name in self.ROSTER_UPDATES:
            with self.subTest(method=name):
                self.assertFalse(hasattr(service, name))
                self.assertTrue(callable(getattr(RosterService, name)))

    def test_it_has_the_same_dependencies_as_the_real_one(self):
        """実データ用と同じ依存の組。片方だけに足して、世界の画面だけが落ちる形にしない。"""
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertTrue(hasattr(build_service(REAL), name))

    GAME_VIEW_SCOPED = ("_teams", "_games", "_game_list_query")

    def test_the_world_game_view_is_fixed_to_the_given_world(self):
        """世界の試合の画面（一覧・詳細）も、渡された世界の範囲だけを読む。"""
        service = build_game_view_service(WORLD7)
        for name in self.GAME_VIEW_SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)

    def test_the_world_game_view_has_the_same_dependencies_as_the_real_one(self):
        """実データ用と同じ依存の組。片方だけに足して、世界の画面だけが落ちる形にしない。"""
        for name in self.GAME_VIEW_SCOPED:
            with self.subTest(dependency=name):
                self.assertTrue(hasattr(build_game_view_service(REAL), name))

    def test_the_real_data_uses_the_calendar_today(self):
        self.assertEqual(build_service(REAL)._today, date.today)

    def test_the_calendar_today_is_not_used_for_a_world(self):
        """世界の範囲では、年齢の基準日は暦の今日ではなく世界の「今日」。"""
        self.assertIsNot(build_service(WORLD7)._today, date.today)

    def test_the_world_analysis_is_fixed_to_its_world_and_uses_the_worlds_today(self):
        """戦力分析も、世界の範囲だけを読み、試合の無い球団の既定の年は世界の今日から取る。"""
        service = build_team_analysis_service(WORLD7)
        for name in ("_team_list_query", "_analysis_query"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)
        self.assertIsNot(service._today, date.today)
        self.assertEqual(build_team_analysis_service(REAL)._today, date.today)

    def test_the_header_view_takes_a_workspace_and_reads_each_worlds_standings_in_its_own_scope(self):
        """見出しは世界の台帳から読み、自軍の順位だけ世界の範囲（渡されたワークスペースの世界）のサービスで読む。"""
        view = build_pennant_world_view(1)
        self.assertEqual(list(signature(build_pennant_world_view).parameters), ["workspace_id"])
        self.assertEqual(view._standings_for(7)._games._scope, WORLD7)
        self.assertEqual(view._standings_for(8)._games._scope, WORLD8)
        # 同率の決着に使う試合も、世界ごとの範囲で読む
        self.assertEqual(view._games_for(7)._scope, WORLD7)
        self.assertEqual(view._games_for(8)._scope, WORLD8)


class RealScopeTest(SimpleTestCase):
    """実データの範囲を渡して組み立てたサービスは、すべて実データの範囲で読み書きすること。"""

    def test_build_service_is_fixed_to_the_real_scope(self):
        service = build_service(REAL)
        for name in ("_teams", "_team_list_query", "_games", "_leagues", "_game_list_query", "_player_fielding_query"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_build_recording_service_is_fixed_to_the_real_scope(self):
        service = build_recording_service(REAL)
        for name in ("_games", "_teams", "_leagues", "_home_stadiums"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_build_team_analysis_service_is_fixed_to_the_real_scope(self):
        service = build_team_analysis_service(REAL)
        for name in ("_team_list_query", "_analysis_query"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_build_game_view_service_is_fixed_to_the_real_scope(self):
        service = build_game_view_service(REAL)
        for name in ("_teams", "_games", "_game_list_query"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_build_roster_service_is_fixed_to_the_real_scope(self):
        """昇格・FA 宣言は実データの管理画面の操作。ペナントの世界の球団には触れない。"""
        service = build_roster_service(REAL)
        for name in ("_teams", "_leagues"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_queries_built_for_views_are_fixed_to_the_real_scope(self):
        self.assertEqual(build_permission_query(REAL)._scope, REAL)
        self.assertEqual(build_player_search_query(REAL)._scope, REAL)

    def test_real_only_builders_reject_a_pennant_scope(self):
        """実データ専用の組み立て口にペナントの範囲を渡す取り違えは、組み立てるところで止める。"""
        for build in REAL_ONLY_BUILDERS:
            with self.subTest(build=build.__name__), self.assertRaises(InvalidWorld):
                build(WORLD7)

    def test_pennant_only_builders_reject_the_real_scope(self):
        """ペナント専用の組み立て口に実データの範囲を渡す取り違えは、組み立てるところで止める。"""
        for build in PENNANT_ONLY_BUILDERS:
            with self.subTest(build=build.__name__), self.assertRaises(InvalidWorld):
                build(REAL)

    def test_pennant_world_service_reads_the_real_data(self):
        """世界の作成は、分岐元を実データの範囲で読む。"""
        service = build_pennant_world_service(REAL)
        self.assertEqual(service._real_leagues._scope, REAL)
        self.assertEqual(service._real_teams._scope, REAL)
        self.assertEqual(service._real_fielding._scope, REAL, "守備成績も分岐元（実データ）から読む")
        self.assertEqual(service._workspace_id, REAL.workspace_id, "作る世界は分岐元の範囲のワークスペースに属す")

    def test_pennant_world_service_writes_to_the_given_world(self):
        """写し先のリポジトリは、渡された世界の範囲で組み立てられる。"""
        repositories_ = build_pennant_world_service(REAL)._repositories_for(WORLD7)
        self.assertEqual(repositories_.leagues._scope, WORLD7)
        self.assertEqual(repositories_.teams._scope, WORLD7)
        self.assertEqual(repositories_.ratings._scope, WORLD7)


class BuildersRequireScopeTest(SimpleTestCase):
    """組み立て口は範囲（`scope`）を既定値なしで受け取る。台帳だけを読む見出しはワークスペースの id を受け取る。"""

    @staticmethod
    def _found_builders():
        return [
            function
            for name, function in inspect.getmembers(views, inspect.isfunction)
            if name.startswith("build_") and function.__module__ == views.__name__
        ]

    def test_every_builder_is_classified_once(self):
        """views の `build_*` は、実データ専用・ペナント専用・両方・台帳のちょうど1つに入る。

        新しい組み立て口を足したら、どの範囲を受け取るかをここで決めさせる（分類の漏れ・二重を拾う）。
        """
        classes = (BOTH_BUILDERS, REAL_ONLY_BUILDERS, PENNANT_ONLY_BUILDERS, LEDGER_BUILDERS)
        found = self._found_builders()
        self.assertGreaterEqual(len(found), 16, "検査の対象を拾えていません")
        for function in found:
            with self.subTest(build=function.__name__):
                self.assertEqual(sum(function in group for group in classes), 1, "分類がちょうど1つではありません")
        classified = [function for group in classes for function in group]
        self.assertEqual(sorted(f.__name__ for f in classified), sorted(f.__name__ for f in found))

    def test_every_builder_takes_a_required_scope(self):
        for function in self._found_builders():
            if function in LEDGER_BUILDERS:
                continue
            with self.subTest(build=function.__name__):
                parameters = signature(function).parameters
                self.assertEqual(list(parameters), ["scope"])
                self.assertIs(parameters["scope"].default, Parameter.empty, "範囲に既定値があります")

    def test_the_ledger_only_view_takes_a_required_workspace(self):
        for build in LEDGER_BUILDERS:
            parameters = signature(build).parameters
            self.assertEqual(list(parameters), ["workspace_id"])
            self.assertIs(parameters["workspace_id"].default, Parameter.empty, "ワークスペースに既定値があります")

    def test_the_world_scope_builders_were_folded_into_the_scope_taking_ones(self):
        self.assertFalse(hasattr(views, "build_world_view_service"))
        self.assertFalse(hasattr(views, "build_world_team_analysis_service"))
        self.assertFalse(hasattr(views, "build_world_game_view_service"))


class PennantSeasonScopeTest(SimpleTestCase):
    """シーズン進行は、渡された世界の範囲だけを読み書きする。"""

    SCOPED = ("_leagues", "_teams", "_games", "_fixtures", "_ratings", "_plans", "_context_query")

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_pennant_season_service(WORLD7)
        self.assertEqual(service._world_id, 7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)

    def test_a_different_world_gets_a_different_scope(self):
        self.assertEqual(build_pennant_season_service(WORLD8)._games._scope, WORLD8)

    def test_the_real_data_cannot_be_advanced(self):
        """実データの範囲では、ペナントの世界のシーズンを進める口を組み立てられない。"""
        with self.assertRaises(InvalidWorld):
            build_pennant_season_service(REAL)

    def test_the_transaction_is_the_database_transaction(self):
        from django.db import transaction

        self.assertIs(build_pennant_season_service(WORLD7)._atomic, transaction.atomic)
        # 世界の作成と日程の生成を1つにする（失敗したら世界を残さない）のも、本物のトランザクション
        self.assertIs(build_pennant_world_service(REAL)._atomic, transaction.atomic)


class PennantHomeScopeTest(SimpleTestCase):
    """GM ホームの材料は、渡された世界の範囲だけを読む。"""

    SCOPED = ("_games", "_game_records", "_fixtures", "_activity", "_team_records", "_leagues")

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_pennant_home_service(WORLD7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)
        self.assertEqual(service._teams._games._scope, WORLD7, "順位・主力も世界の範囲のサービス")

    def test_a_different_world_gets_a_different_scope(self):
        self.assertEqual(build_pennant_home_service(WORLD8)._activity._scope, WORLD8)

    def test_the_real_data_cannot_be_read_as_a_home(self):
        with self.assertRaises(InvalidWorld):
            build_pennant_home_service(REAL)


class PennantHistoryScopeTest(SimpleTestCase):
    """歴代の記録と球団史は、渡された世界の範囲だけを読む。"""

    SCOPED = ("_games", "_team_records", "_leagues", "_awards")

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_pennant_history_service(WORLD7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)
        self.assertEqual(service._teams._games._scope, WORLD7, "順位・タイトルも世界の範囲のサービス")

    def test_a_different_world_gets_a_different_scope(self):
        self.assertEqual(build_pennant_history_service(WORLD8)._games._scope, WORLD8)

    def test_the_real_data_cannot_be_read_as_a_history(self):
        with self.assertRaises(InvalidWorld):
            build_pennant_history_service(REAL)


class PennantOffseasonScopeTest(SimpleTestCase):
    """シーズンを締める処理と戦力外の指名は、渡された世界の範囲だけを読み書きする。"""

    SCOPED = (
        "_leagues",
        "_teams",
        "_fixtures",
        "_ratings",
        "_plans",
        "_releases",
        "_awards",
        "_context_query",
        "_playing_time",
    )

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_pennant_offseason_service(WORLD7)
        self.assertEqual(service._world_id, 7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)
        self.assertEqual(service._titles._games._scope, WORLD7, "タイトルを導くのも世界の範囲のサービス")

    def test_the_real_data_cannot_be_closed(self):
        with self.assertRaises(InvalidWorld):
            build_pennant_offseason_service(REAL)


class ClubServiceScopeTest(SimpleTestCase):
    """編成の管理は、渡された世界の範囲だけを読み書きする。"""

    SCOPED = ("_plans", "_ratings", "_fixtures", "_context_query")

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_club_service(WORLD7)
        self.assertEqual(service._world_id, 7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)

    def test_a_different_world_gets_a_different_scope(self):
        self.assertEqual(build_club_service(WORLD8)._plans._scope, WORLD8)

    def test_the_real_data_has_no_club_service(self):
        with self.assertRaises(InvalidWorld):
            build_club_service(REAL)


class ClubDetailsScopeTest(SimpleTestCase):
    """編成画面の詳細は、渡された世界の範囲だけを読む。"""

    SCOPED = ("_stats", "_ratings")

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_club_details_service(WORLD7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)

    def test_the_real_data_has_no_club_details_service(self):
        with self.assertRaises(InvalidWorld):
            build_club_details_service(REAL)


class PennantRatingsScopeTest(SimpleTestCase):
    """能力の表示は、渡された世界の範囲だけを読む。"""

    SCOPED = ("_stats", "_ratings", "_history", "_fixtures", "_context_query")

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_pennant_ratings_service(WORLD7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)

    def test_a_different_world_gets_a_different_scope(self):
        self.assertEqual(build_pennant_ratings_service(WORLD8)._ratings._scope, WORLD8)

    def test_the_real_data_has_no_ratings_service(self):
        with self.assertRaises(InvalidWorld):
            build_pennant_ratings_service(REAL)


class OffseasonViewScopeTest(SimpleTestCase):
    """オフの結果の参照は、渡された世界の範囲だけを読む。"""

    def test_dependencies_are_wired_and_fixed_to_the_given_world(self):
        service = build_offseason_view_service(WORLD7)
        self.assertIsInstance(service, OffseasonViewService)
        self.assertEqual(service._offseason._scope, WORLD7)

    def test_a_different_world_gets_a_different_scope(self):
        self.assertEqual(build_offseason_view_service(WORLD8)._offseason._scope, WORLD8)

    def test_the_real_data_has_no_offseason(self):
        with self.assertRaises(InvalidWorld):
            build_offseason_view_service(REAL)


class AdminOnlyScopeExceptionTest(SimpleTestCase):
    """全ワークスペースの実データを読む `admin_real_queryset` は、`admin.py` だけが使う（決定 2026-10-07 Q4）。

    「全範囲を読む API を作らない」規則の唯一の例外なので、ほかの場所（画面・リポジトリ・管理コマンド）に
    広がると、ワークスペースをまたいで行が漏れる。`myapp/` の .py を走査し、名前を出すのが定義と管理画面だけか
    （テストは除く）を確かめる。
    """

    NAME = "admin_real_queryset"
    ALLOWED = {Path("myapp/admin.py"), Path("myapp/infrastructure/scoping.py")}

    def test_only_the_admin_uses_the_all_workspaces_queryset(self):
        root = Path(__file__).resolve().parents[3]
        users = {
            path.relative_to(root)
            for path in (root / "myapp").rglob("*.py")
            if "tests" not in path.relative_to(root).parts and self.NAME in path.read_text(encoding="utf-8")
        }
        self.assertIn(Path("myapp/admin.py"), users, "検査の対象を拾えていません")
        self.assertEqual(users - self.ALLOWED, set(), "admin.py 以外が管理画面専用の関数を使っています")


class LedgersAreScopedToTheWorkspaceTest(SimpleTestCase):
    """組み立て口は、世界の台帳を範囲のワークスペースで組み立てる（W1b T3）。

    台帳が別のワークスペース（一時関数のものなど）で組み立てられると、範囲の世界が台帳で見つからない・
    ほかのワークスペースの世界が引ける形で現れる。範囲には既定のワークスペース（1）と違う id を使う。
    """

    WORKSPACE = 3

    def _built(self):
        real, world = WorldScope.real(self.WORKSPACE), WorldScope.pennant(self.WORKSPACE, 7)
        for build in BOTH_BUILDERS:
            yield build, build(world)
        for build in REAL_ONLY_BUILDERS:
            yield build, build(real)
        for build in PENNANT_ONLY_BUILDERS:
            yield build, build(world)
        for build in LEDGER_BUILDERS:
            yield build, build(self.WORKSPACE)

    def test_every_ledger_a_builder_holds_is_of_the_scopes_workspace(self):
        seen = set()
        for build, service in self._built():
            for name, value in vars(service).items():
                if isinstance(value, tuple(LEDGERS)):
                    seen.add(build.__name__)
                    with self.subTest(build=build.__name__, dependency=name):
                        self.assertEqual(value._workspace_id, self.WORKSPACE)
        # 台帳を持つ組み立て口を拾えていること（検査の空振りを防ぐ。世界の「今日」の台帳は次の検査）
        expected = {
            build_pennant_world_view,
            build_pennant_world_service,
            build_pennant_season_service,
            build_pennant_offseason_service,
            build_pennant_ratings_service,
            build_club_service,
            build_offseason_view_service,
        }
        self.assertEqual(seen, {build.__name__ for build in expected})

    def test_the_worlds_today_reads_the_ledger_of_the_scopes_workspace(self):
        """世界の「今日」（`_world_clock`）も、範囲のワークスペースの台帳で世界を引く。"""
        world = WorldScope.pennant(self.WORKSPACE, 7)
        for today in (
            build_service(world)._today,
            build_team_analysis_service(world)._today,
            build_pennant_ratings_service(world)._today,
            build_club_details_service(world)._today,
        ):
            with self.subTest(today=today):
                worlds = inspect.getclosurevars(today).nonlocals["worlds"]
                self.assertIsInstance(worlds, DjangoWorldRepository)
                self.assertEqual(worlds._workspace_id, self.WORKSPACE)


class WorldWorkspaceResolverTest(SimpleTestCase):
    """世界の行からワークスペースを導く `workspace_id_of_world` は、管理コマンドの入口だけが使う（W1b T3）。

    台帳を通さずに世界のワークスペースを決めるので、画面が使うと今の URL のワークスペースに無い世界を開けてしまう
    （画面は今の URL のワークスペースの台帳で世界を引く）。`myapp/` の .py（テストを除く）を走査する。
    """

    NAME = "workspace_id_of_world"
    ALLOWED = {Path("myapp/infrastructure/workspaces.py"), Path("myapp/management/workspaces.py")}

    def test_only_the_management_commands_resolve_a_worlds_workspace(self):
        root = Path(__file__).resolve().parents[3]
        users = {
            path.relative_to(root)
            for path in (root / "myapp").rglob("*.py")
            if "tests" not in path.relative_to(root).parts and self.NAME in path.read_text(encoding="utf-8")
        }
        self.assertIn(Path("myapp/management/workspaces.py"), users, "検査の対象を拾えていません")
        self.assertEqual(users - self.ALLOWED, set(), "管理コマンドの入口以外が世界のワークスペースを直接導いています")
