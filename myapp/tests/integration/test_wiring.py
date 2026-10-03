"""依存の組み立てに関する検査。

アプリケーションサービスはインターフェース（domain/repositories.py・
application/queries.py）だけに依存し、実装は組み立ての1か所（build_service）で
差し込む。実装側でメソッド名が変わっても、この検査が無いと画面を開くまで
気づけないため、機械的に確かめる。

あわせて、**リポジトリと参照クエリが世界の範囲（`WorldScope`）を必須で受け取る**こと、
実データ用の組み立てが実データの範囲に固定されていることを確かめる。
"""

import inspect
from datetime import date
from inspect import Parameter, signature

from django.test import SimpleTestCase

from myapp.application.game_recording import GameRecordingService
from myapp.application.pennant_season import PennantSeasonService
from myapp.application.pennant_view import PennantWorldViewService
from myapp.application.pennant_world import PennantWorldService
from myapp.application.queries import (
    FieldingTotalsQuery,
    GameListQuery,
    SimulationContextQuery,
    TeamListQuery,
    WorldSummaryQuery,
)
from myapp.application.services import TeamApplicationService
from myapp.domain.exceptions import InvalidWorld
from myapp.domain.pennant.world import WorldScope
from myapp.domain.repositories import (
    FixtureRepository,
    GameRepository,
    LeagueRepository,
    RatingsRepository,
    TeamRepository,
    WorldRepository,
)
from myapp.infrastructure import queries, repositories
from myapp.infrastructure.queries import (
    DjangoFieldingTotalsQuery,
    DjangoGameListQuery,
    DjangoSimulationContextQuery,
    DjangoTeamListQuery,
    DjangoWorldSummaryQuery,
)
from myapp.infrastructure.repositories import (
    DjangoFixtureRepository,
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoRatingsRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
)
from myapp.presentation.views import (
    build_pennant_season_service,
    build_pennant_world_service,
    build_pennant_world_view,
    build_permission_query,
    build_player_search_query,
    build_recording_service,
    build_service,
    build_world_view_service,
)

REAL = WorldScope.real()

# 世界そのものの台帳なので、範囲を持たない
UNSCOPED = {DjangoWorldRepository, DjangoWorldSummaryQuery}


class ProtocolConformanceTest(SimpleTestCase):
    IMPLEMENTATIONS = [
        (DjangoTeamRepository(REAL), TeamRepository),
        (DjangoGameRepository(REAL), GameRepository),
        (DjangoLeagueRepository(REAL), LeagueRepository),
        (DjangoWorldRepository(), WorldRepository),
        (DjangoRatingsRepository(REAL), RatingsRepository),
        (DjangoFixtureRepository(REAL), FixtureRepository),
        (DjangoSimulationContextQuery(REAL), SimulationContextQuery),
        (DjangoFieldingTotalsQuery(REAL), FieldingTotalsQuery),
        (DjangoTeamListQuery(REAL), TeamListQuery),
        (DjangoGameListQuery(REAL), GameListQuery),
        (DjangoWorldSummaryQuery(), WorldSummaryQuery),
    ]

    def test_implementations_satisfy_interfaces(self):
        """infrastructure の実装がインターフェースを満たしていること。"""
        for implementation, interface in self.IMPLEMENTATIONS:
            with self.subTest(implementation=type(implementation).__name__):
                self.assertIsInstance(implementation, interface)


class ScopeIsRequiredTest(SimpleTestCase):
    """範囲を渡さずに作れるリポジトリ・参照クエリがあってはならない。

    既定値があると「渡し忘れ」が型検査にも実行にも引っかからず、ペナントの画面に
    実データが出る形で現れる。新しい Django* の実装を足したときも、ここが拾う。
    """

    def _implementations(self):
        for module in (repositories, queries):
            for name, cls in inspect.getmembers(module, inspect.isclass):
                if name.startswith("Django") and cls.__module__ == module.__name__ and cls not in UNSCOPED:
                    yield cls

    def test_every_implementation_requires_a_scope(self):
        found = list(self._implementations())
        self.assertGreaterEqual(len(found), 8, "検査の対象を拾えていません")
        for cls in found:
            with self.subTest(implementation=cls.__name__):
                parameters = signature(cls.__init__).parameters
                self.assertIn("scope", parameters, "範囲（scope）を受け取っていません")
                self.assertIs(parameters["scope"].default, Parameter.empty, "範囲に既定値があります")

    def test_a_scope_cannot_be_omitted(self):
        with self.assertRaises(TypeError):
            DjangoTeamRepository()  # type: ignore[call-arg]


class BuildServiceTest(SimpleTestCase):
    def test_all_dependencies_are_wired(self):
        """組み立てたサービスに、依存が1つも欠けていないこと。

        欠けたまま作れると、使う画面によって「None に find_all は無い」で
        落ちるサービスができてしまう。

        検査対象は `__init__` の引数に対応する属性だけ（引数 `teams` →
        属性 `_teams` の対応を前提にする）。内部の控えは初期値が None を
        取りうるので、`vars()` を丸ごと見ない。
        """
        self._assert_wired(build_service(), TeamApplicationService)

    def test_recording_service_dependencies_are_wired(self):
        """スコアブックの保存サービスも同じ検査にかける。

        組み立てが増えるたびに検査を書き忘れると、この形の事故だけが素通りする。
        """
        self._assert_wired(build_recording_service(), GameRecordingService)

    def test_pennant_world_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_world_service(), PennantWorldService)

    def test_pennant_season_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_season_service(7), PennantSeasonService)

    def test_world_view_service_dependencies_are_wired(self):
        """世界の範囲の参照サービスも、実データ用と同じ検査にかける（依存は実データ用と同じ全部）。"""
        self._assert_wired(build_world_view_service(7), TeamApplicationService)

    def test_pennant_world_view_dependencies_are_wired(self):
        self._assert_wired(build_pennant_world_view(), PennantWorldViewService)

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
        service = build_world_view_service(7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WorldScope.pennant(7))

    def test_it_has_the_same_dependencies_as_the_real_one(self):
        """実データ用と同じ依存の組。片方だけに足して、世界の画面だけが落ちる形にしない。"""
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertTrue(hasattr(build_service(), name))

    def test_the_real_data_uses_the_calendar_today(self):
        self.assertEqual(build_service()._today, date.today)

    def test_the_real_data_cannot_be_viewed_as_a_world(self):
        for bad in (0, -1, True):
            with self.subTest(world_id=bad), self.assertRaises(InvalidWorld):
                build_world_view_service(bad)

    def test_the_header_view_takes_no_world_and_reads_each_worlds_standings_in_its_own_scope(self):
        """見出しは世界の台帳から読み、自軍の順位だけ世界の範囲のサービスで読む。"""
        view = build_pennant_world_view()
        self.assertEqual(list(signature(build_pennant_world_view).parameters), [])
        self.assertEqual(view._standings_for(7)._games._scope, WorldScope.pennant(7))
        self.assertEqual(view._standings_for(8)._games._scope, WorldScope.pennant(8))


class RealScopeTest(SimpleTestCase):
    """実データ用の組み立ては、すべて実データの範囲に固定されていること。"""

    def test_build_service_is_fixed_to_the_real_scope(self):
        service = build_service()
        for name in ("_teams", "_team_list_query", "_games", "_leagues", "_game_list_query", "_player_fielding_query"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_build_recording_service_is_fixed_to_the_real_scope(self):
        service = build_recording_service()
        for name in ("_games", "_teams", "_leagues"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_queries_built_for_views_are_fixed_to_the_real_scope(self):
        self.assertEqual(build_permission_query()._scope, REAL)
        self.assertEqual(build_player_search_query()._scope, REAL)

    def test_build_functions_take_no_scope(self):
        """範囲を引数に取る形にしない（既定値で切り替えると、渡し忘れが混入になる）。"""
        for build in (build_service, build_recording_service, build_permission_query, build_player_search_query):
            with self.subTest(build=build.__name__):
                self.assertEqual(list(signature(build).parameters), [])

    def test_pennant_world_service_reads_the_real_data(self):
        """世界の作成は、分岐元を実データの範囲で読む。"""
        service = build_pennant_world_service()
        self.assertEqual(service._real_leagues._scope, REAL)
        self.assertEqual(service._real_teams._scope, REAL)
        self.assertEqual(service._real_fielding._scope, REAL, "守備成績も分岐元（実データ）から読む")

    def test_pennant_world_service_writes_to_the_given_world(self):
        """写し先のリポジトリは、渡された世界の範囲で組み立てられる。"""
        repositories_ = build_pennant_world_service()._repositories_for(WorldScope.pennant(7))
        self.assertEqual(repositories_.leagues._scope, WorldScope.pennant(7))
        self.assertEqual(repositories_.teams._scope, WorldScope.pennant(7))
        self.assertEqual(repositories_.ratings._scope, WorldScope.pennant(7))


class PennantSeasonScopeTest(SimpleTestCase):
    """シーズン進行は、渡された世界の範囲だけを読み書きする。"""

    SCOPED = ("_leagues", "_teams", "_games", "_fixtures", "_ratings", "_context_query")

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_pennant_season_service(7)
        self.assertEqual(service._world_id, 7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WorldScope.pennant(7))

    def test_a_different_world_gets_a_different_scope(self):
        self.assertEqual(build_pennant_season_service(8)._games._scope, WorldScope.pennant(8))

    def test_the_real_data_cannot_be_advanced(self):
        """世界の id は必須で、実データを指す形（0・負・None・真偽値）は弾く。"""
        for bad in (0, -1, True):
            with self.subTest(world_id=bad), self.assertRaises(InvalidWorld):
                build_pennant_season_service(bad)

    def test_the_transaction_is_the_database_transaction(self):
        from django.db import transaction

        self.assertIs(build_pennant_season_service(7)._atomic, transaction.atomic)
