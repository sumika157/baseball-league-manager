"""依存の組み立てに関する検査。

アプリケーションサービスはインターフェース（domain/repositories.py・
application/queries.py）だけに依存し、実装は組み立ての1か所（presentation/views.py の build_*()）で
差し込む。実装側でメソッド名が変わっても、この検査が無いと画面を開くまで
気づけないため、機械的に確かめる。

あわせて、**リポジトリと参照クエリが世界の範囲（`WorldScope`）を必須で受け取る**こと、
組み立て口が範囲を既定値なしで受け取り、範囲の取り違え（ペナント専用に実データ、実データ専用にペナント）を弾くことを確かめる。
"""

import inspect
from datetime import date
from inspect import Parameter, signature
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from myapp.application.club_management import ClubManagementService
from myapp.application.game_recording import GameRecordingService
from myapp.application.game_view import GameViewService
from myapp.application.league_setup import LeagueSetupService
from myapp.application.league_view import LeagueViewService
from myapp.application.pennant_championships import PostseasonHistoryReader
from myapp.application.pennant_club_details import ClubDetailsService
from myapp.application.pennant_history import PennantHistoryService
from myapp.application.pennant_home import PennantHomeService
from myapp.application.pennant_offseason import PennantOffseasonService
from myapp.application.pennant_offseason_view import OffseasonViewService
from myapp.application.pennant_postseason import PennantPostseasonService, PostseasonBracketReader
from myapp.application.pennant_postseason_view import PennantPostseasonViewService
from myapp.application.pennant_ratings import PennantRatingsViewService
from myapp.application.pennant_season import PennantSeasonService
from myapp.application.pennant_settings import PennantSettingsService
from myapp.application.pennant_view import PennantWorldViewService
from myapp.application.pennant_world import PennantWorldService
from myapp.application.queries import (
    AwardQuery,
    ChampionshipQuery,
    FieldingTotalsQuery,
    GameListQuery,
    HomeStadiumQuery,
    InvitationPreviewQuery,
    OffseasonQuery,
    PennantActivityQuery,
    PostseasonQuery,
    PostseasonStatsQuery,
    RatingsHistoryQuery,
    ServiceHistoryQuery,
    SetupQuery,
    SimulationContextQuery,
    TeamAnalysisQuery,
    TeamListQuery,
    WorkspaceAccessQuery,
    WorkspaceListQuery,
    WorkspaceManagementQuery,
    WorldSummaryQuery,
)
from myapp.application.roster import RosterService
from myapp.application.team_analysis import TeamAnalysisService
from myapp.application.team_view import TeamViewService
from myapp.application.workspaces import WorkspaceMemberService, WorkspaceService
from myapp.domain.exceptions import InvalidWorld
from myapp.domain.pennant.world import WorldScope
from myapp.domain.repositories import (
    AwardRepository,
    ChampionshipRepository,
    ClubPlanRepository,
    FixtureRepository,
    GameRepository,
    InvitationRepository,
    LeagueRepository,
    RatingsRepository,
    ReleasePlanRepository,
    StadiumRepository,
    TeamRepository,
    WorkspaceRepository,
    WorldRepository,
)
from myapp.infrastructure import queries, repositories
from myapp.infrastructure.queries import (
    DjangoAwardQuery,
    DjangoChampionshipQuery,
    DjangoFieldingTotalsQuery,
    DjangoGameListQuery,
    DjangoHomeStadiumQuery,
    DjangoInvitationPreviewQuery,
    DjangoOffseasonQuery,
    DjangoPennantActivityQuery,
    DjangoPostseasonQuery,
    DjangoPostseasonStatsQuery,
    DjangoRatingsHistoryQuery,
    DjangoServiceHistoryQuery,
    DjangoSetupQuery,
    DjangoSimulationContextQuery,
    DjangoTeamAnalysisQuery,
    DjangoTeamListQuery,
    DjangoWorkspaceAccessQuery,
    DjangoWorkspaceListQuery,
    DjangoWorkspaceManagementQuery,
    DjangoWorldSummaryQuery,
)
from myapp.infrastructure.repositories import (
    DjangoAwardRepository,
    DjangoChampionshipRepository,
    DjangoClubPlanRepository,
    DjangoFixtureRepository,
    DjangoGameRepository,
    DjangoInvitationRepository,
    DjangoLeagueRepository,
    DjangoRatingsRepository,
    DjangoReleasePlanRepository,
    DjangoStadiumRepository,
    DjangoTeamRepository,
    DjangoWorkspaceRepository,
    DjangoWorldRepository,
)
from myapp.presentation import views
from myapp.presentation.views import (
    build_club_details_service,
    build_club_service,
    build_game_view_service,
    build_invitation_preview_query,
    build_league_setup_service,
    build_league_view_service,
    build_offseason_view_service,
    build_pennant_history_service,
    build_pennant_home_service,
    build_pennant_offseason_service,
    build_pennant_postseason_service,
    build_pennant_postseason_view_service,
    build_pennant_ratings_service,
    build_pennant_season_service,
    build_pennant_settings_service,
    build_pennant_world_service,
    build_pennant_world_view,
    build_player_search_query,
    build_recording_service,
    build_roster_service,
    build_setup_query,
    build_team_analysis_service,
    build_team_view_service,
    build_workspace_access_query,
    build_workspace_list_query,
    build_workspace_management_query,
    build_workspace_member_service,
    build_workspace_service,
)

# 組み立てるときに DB を読まないので、ワークスペースの id はリテラルでよい（SimpleTestCase のまま通す）
REAL = WorldScope.real(1)
WORLD7 = WorldScope.pennant(1, 7)
WORLD8 = WorldScope.pennant(1, 8)

# 組み立て口の分類。views.build_* は、ちょうどどれか1つに入る（`test_every_builder_is_classified_once`）。
# 実データでもペナントでも組み立てられる（範囲に応じて年の基準が変わる）
BOTH_BUILDERS = (
    build_league_view_service,
    build_team_view_service,
    build_game_view_service,
    build_team_analysis_service,
)
# 実データ専用。ペナントの範囲を渡すと InvalidWorld
REAL_ONLY_BUILDERS = (
    build_recording_service,
    build_roster_service,
    build_player_search_query,
    build_pennant_world_service,
    build_league_setup_service,
    build_setup_query,
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
    build_pennant_postseason_service,
    build_pennant_postseason_view_service,
    build_pennant_settings_service,
)
# ワークスペースの台帳（W2 T1）。範囲もワークスペースの id も取らない（key・利用者からワークスペースそのものを引き、
# 中のデータは読まない）。名前で列挙し、黙って増えないようにする（`WorkspaceLedgersTest`）。
# W4 で、ワークスペースが決まる前の操作（作成・招待の受諾・招待の中身）と、id でワークスペースそのものを書く操作
# （設定・メンバー）の組み立て口を足した
WORKSPACE_LEDGER_BUILDERS = (
    build_workspace_access_query,
    build_workspace_list_query,
    build_workspace_service,
    build_workspace_member_service,
    build_invitation_preview_query,
)
# 1つのワークスペースのメンバーと招待だけを読む（W4）。範囲ではなくワークスペースの id を取る
MEMBER_BUILDERS = (build_workspace_management_query,)

# 範囲もワークスペースも受け取らずに作れる実装。W1b T3 で世界の台帳もワークスペースを受け取るようになり、空になった。
# ここに足す前に、範囲かワークスペースで絞れないかを考える（全範囲を読む実装は、ワークスペースをまたいで行を漏らす）
UNSCOPED: set[type] = set()
# 世界の台帳。範囲（ワークスペースと世界）ではなく、ワークスペースの id だけを受け取る
LEDGERS = {DjangoWorldRepository, DjangoWorldSummaryQuery}
# ワークスペースの台帳（W2 T1）。何も受け取らない（key・利用者は呼ぶときに渡す）。ここに足すのは
# 「ワークスペースそのものを引き、中のデータを読まない」実装だけ
WORKSPACE_LEDGERS = {
    DjangoWorkspaceAccessQuery,
    DjangoWorkspaceListQuery,
    DjangoWorkspaceRepository,
    DjangoInvitationRepository,
    DjangoInvitationPreviewQuery,
}
# 1つのワークスペースのメンバーと招待だけを読む参照（W4）。ワークスペースの id だけを受け取る
MEMBER_QUERIES = {DjangoWorkspaceManagementQuery}
# 何も受け取らずに作れる実装の名前（黙って増やさない）
TAKES_NOTHING = {
    "DjangoWorkspaceAccessQuery",
    "DjangoWorkspaceListQuery",
    "DjangoWorkspaceRepository",
    "DjangoInvitationRepository",
    "DjangoInvitationPreviewQuery",
}


class ProtocolConformanceTest(SimpleTestCase):
    IMPLEMENTATIONS = [
        (DjangoTeamRepository(REAL), TeamRepository),
        (DjangoGameRepository(REAL), GameRepository),
        (DjangoLeagueRepository(REAL), LeagueRepository),
        (DjangoStadiumRepository(REAL), StadiumRepository),
        (DjangoSetupQuery(REAL), SetupQuery),
        (DjangoWorldRepository(1), WorldRepository),
        (DjangoRatingsRepository(REAL), RatingsRepository),
        (DjangoFixtureRepository(REAL), FixtureRepository),
        (DjangoClubPlanRepository(REAL), ClubPlanRepository),
        (DjangoReleasePlanRepository(REAL), ReleasePlanRepository),
        (DjangoAwardRepository(REAL), AwardRepository),
        (DjangoAwardQuery(REAL), AwardQuery),
        (DjangoChampionshipRepository(REAL), ChampionshipRepository),
        (DjangoChampionshipQuery(REAL), ChampionshipQuery),
        (DjangoSimulationContextQuery(REAL), SimulationContextQuery),
        (DjangoFieldingTotalsQuery(REAL), FieldingTotalsQuery),
        (DjangoTeamListQuery(REAL), TeamListQuery),
        (DjangoGameListQuery(REAL), GameListQuery),
        (DjangoTeamAnalysisQuery(REAL), TeamAnalysisQuery),
        (DjangoServiceHistoryQuery(REAL), ServiceHistoryQuery),
        (DjangoPennantActivityQuery(REAL), PennantActivityQuery),
        (DjangoWorldSummaryQuery(1), WorldSummaryQuery),
        (DjangoOffseasonQuery(REAL), OffseasonQuery),
        (DjangoRatingsHistoryQuery(REAL), RatingsHistoryQuery),
        (DjangoHomeStadiumQuery(REAL), HomeStadiumQuery),
        (DjangoWorkspaceAccessQuery(), WorkspaceAccessQuery),
        (DjangoWorkspaceListQuery(), WorkspaceListQuery),
        (DjangoWorkspaceRepository(), WorkspaceRepository),
        (DjangoInvitationRepository(), InvitationRepository),
        (DjangoInvitationPreviewQuery(), InvitationPreviewQuery),
        (DjangoWorkspaceManagementQuery(1), WorkspaceManagementQuery),
        (DjangoPostseasonQuery(REAL), PostseasonQuery),
        (DjangoPostseasonStatsQuery(REAL), PostseasonStatsQuery),
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
        self.assertLessEqual(WORKSPACE_LEDGERS, set(found), "ワークスペースの台帳を拾えていません")
        for cls in found:
            if cls in WORKSPACE_LEDGERS:
                continue
            with self.subTest(implementation=cls.__name__):
                expected = "workspace_id" if cls in LEDGERS | MEMBER_QUERIES else "scope"
                parameters = signature(cls.__init__).parameters
                self.assertEqual(list(parameters), ["self", expected], f"{expected} だけを受け取っていません")
                self.assertIs(parameters[expected].default, Parameter.empty, f"{expected} に既定値があります")

    def test_a_scope_cannot_be_omitted(self):
        with self.assertRaises(TypeError):
            DjangoTeamRepository()  # type: ignore[call-arg]

    def test_a_workspace_cannot_be_omitted_from_the_ledgers(self):
        for cls in LEDGERS | MEMBER_QUERIES:
            with self.subTest(implementation=cls.__name__), self.assertRaises(TypeError):
                cls()

    def test_only_the_workspace_ledgers_take_nothing(self):
        """何も受け取らずに作れるのは、ワークスペースの台帳だけ（名前で固定し、黙って増やさない）。

        ワークスペースの台帳は key・利用者からワークスペースそのものを引くので、範囲を作る前に呼ばれる。
        ほかの実装が範囲を取らずに作れると、ワークスペースをまたいで行を読む。
        """
        # クラスそのものの signature は、継いだコンストラクタも解決して呼ぶときの引数を返す
        # （`__init__` を定義しなければ object の `()`。親の `__init__(self, scope)` を継げば `(scope)`）
        takes_nothing = {cls for cls in self._implementations() if not signature(cls).parameters}
        self.assertEqual({cls.__name__ for cls in takes_nothing}, TAKES_NOTHING)
        self.assertEqual(takes_nothing, WORKSPACE_LEDGERS)


class BuildServiceTest(SimpleTestCase):
    def test_all_dependencies_are_wired(self):
        """組み立てたサービスに、依存が1つも欠けていないこと。

        欠けたまま作れると、使う画面によって「None に find_all は無い」で
        落ちるサービスができてしまう。

        検査対象は `__init__` の引数に対応する属性だけ（引数 `teams` →
        属性 `_teams` の対応を前提にする）。内部の控えは初期値が None を
        取りうるので、`vars()` を丸ごと見ない。
        """
        self._assert_wired(build_league_view_service(REAL), LeagueViewService)

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

    def test_pennant_postseason_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_postseason_service(WORLD7), PennantPostseasonService)

    def test_the_postseason_brackets_are_fully_wired_for_both_users(self):
        """勝ち上がりの読み取りは、進める処理と締める処理の検査の両方で、依存を全部渡して組み立てる。"""
        self._assert_wired(build_pennant_postseason_service(WORLD7)._brackets, PostseasonBracketReader)
        self._assert_wired(build_pennant_offseason_service(WORLD7)._postseason, PostseasonBracketReader)

    def test_pennant_postseason_view_service_dependencies_are_wired_in_the_given_world(self):
        """勝ち上がりの表・GM ホームのカード・世界の終了の判定を読むサービスも、依存を全部、世界の範囲で渡す。"""
        service = build_pennant_postseason_view_service(WORLD7)
        self._assert_wired(service, PennantPostseasonViewService)
        self._assert_wired(service._brackets, PostseasonBracketReader)
        self.assertEqual(service._world_id, 7)
        self.assertEqual(service._postseason._scope, WORLD7)
        self.assertEqual(service._league_view._game_list_query._scope, WORLD7)
        self.assertEqual(service._brackets._postseason._scope, WORLD7)
        self.assertEqual(service._stats._scope, WORLD7)
        with self.assertRaises(InvalidWorld):
            build_pennant_postseason_view_service(REAL)

    def test_pennant_settings_service_dependencies_are_wired_in_the_given_world(self):
        """方式の設定も、依存を全部、世界の範囲で渡す（いまの年度のポストシーズンの試合を読み直す）。"""
        from django.db import transaction

        service = build_pennant_settings_service(WORLD7)
        self._assert_wired(service, PennantSettingsService)
        self._assert_wired(service._brackets, PostseasonBracketReader)
        self.assertEqual(service._world_id, 7)
        for name in ("_fixtures", "_context_query", "_postseason"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)
        self.assertEqual(service._brackets._postseason._scope, WORLD7)
        self.assertIs(service._atomic, transaction.atomic)
        with self.assertRaises(InvalidWorld):
            build_pennant_settings_service(REAL)

    def test_world_league_view_service_dependencies_are_wired(self):
        """世界の範囲の参照サービスも、実データ用と同じ検査にかける（依存は実データ用と同じ全部）。"""
        self._assert_wired(build_league_view_service(WORLD7), LeagueViewService)

    def test_team_view_service_dependencies_are_wired(self):
        """球団と選手の画面の参照サービスも同じ検査にかける（組み立て口は build_*() 関数だけ）。"""
        self._assert_wired(build_team_view_service(REAL), TeamViewService)

    def test_world_team_view_service_dependencies_are_wired(self):
        self._assert_wired(build_team_view_service(WORLD7), TeamViewService)

    def test_pennant_home_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_home_service(WORLD7), PennantHomeService)
        self._assert_wired(build_pennant_home_service(WORLD7)._postseason, PostseasonHistoryReader)

    def test_pennant_history_service_dependencies_are_wired(self):
        self._assert_wired(build_pennant_history_service(WORLD7), PennantHistoryService)
        self._assert_wired(build_pennant_history_service(WORLD7)._postseason, PostseasonHistoryReader)

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

    def test_league_setup_service_dependencies_are_wired(self):
        """リーグ・チーム・球場の登録のサービスも同じ検査にかける（W3。組み立て口は build_*() 関数だけ）。"""
        self._assert_wired(build_league_setup_service(REAL), LeagueSetupService)

    def test_workspace_service_dependencies_are_wired(self):
        """ワークスペースの作成・設定のサービス（W4）。上限は settings から、トランザクションは Django のもの。"""
        from django.db import transaction
        from django.test import override_settings

        from myapp.infrastructure.workspaces import new_workspace_key

        with override_settings(WORKSPACE_MAX_OWNED=7):
            service = build_workspace_service()
        self._assert_wired(service, WorkspaceService)
        self.assertIsInstance(service._workspaces, DjangoWorkspaceRepository)
        self.assertEqual(service._quota.max_owned_workspaces, 7)
        self.assertIs(service._new_key, new_workspace_key)
        self.assertIs(service._atomic, transaction.atomic)

    def test_workspace_member_service_dependencies_are_wired(self):
        """招待とメンバーのサービス（W4）。時計は Django の現在時刻（タイムゾーンつき）。"""
        from django.db import transaction
        from django.utils import timezone

        from myapp.infrastructure.workspaces import new_invitation_token

        service = build_workspace_member_service()
        self._assert_wired(service, WorkspaceMemberService)
        self.assertIsInstance(service._workspaces, DjangoWorkspaceRepository)
        self.assertIsInstance(service._invitations, DjangoInvitationRepository)
        self.assertIs(service._new_token, new_invitation_token)
        self.assertIs(service._clock, timezone.now)
        self.assertIs(service._atomic, transaction.atomic)

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
            LeagueViewService(teams=DjangoTeamRepository(REAL))  # type: ignore[call-arg]


class WorldViewScopeTest(SimpleTestCase):
    """世界の画面を読むサービスは、渡された世界の範囲だけを読む。"""

    SCOPED = (
        "_teams",
        "_team_list_query",
        "_leagues",
        "_game_list_query",
        "_player_stats_query",
    )

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_league_view_service(WORLD7)
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
        "promote_player",
        "declare_free_agency",
        "remove_free_agency_declaration",
    )

    def test_it_cannot_update_the_roster(self):
        """世界の画面を読む組み立てからは、ロスターの更新を呼べない（更新は `RosterService` だけが持つ）。"""
        service = build_league_view_service(WORLD7)
        for name in self.ROSTER_UPDATES:
            with self.subTest(method=name):
                self.assertFalse(hasattr(service, name))
                self.assertTrue(callable(getattr(RosterService, name)))

    def test_it_has_the_same_dependencies_as_the_real_one(self):
        """実データ用と同じ依存の組。片方だけに足して、世界の画面だけが落ちる形にしない。"""
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertTrue(hasattr(build_league_view_service(REAL), name))

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

    TEAM_VIEW_SCOPED = ("_teams", "_games", "_game_list_query", "_player_fielding_query", "_player_stats_query")

    def test_the_world_team_view_is_fixed_to_the_given_world(self):
        """世界の球団と選手の画面（選手の一覧・個人ページ）も、渡された世界の範囲だけを読み、年齢は世界の今日で数える。"""
        service = build_team_view_service(WORLD7)
        for name in self.TEAM_VIEW_SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)
        self.assertIsNot(service._today, date.today)

    def test_the_world_team_view_has_the_same_dependencies_as_the_real_one(self):
        """実データ用と同じ依存の組。片方だけに足して、世界の画面だけが落ちる形にしない。"""
        for name in self.TEAM_VIEW_SCOPED:
            with self.subTest(dependency=name):
                self.assertTrue(hasattr(build_team_view_service(REAL), name))

    def test_the_team_view_cannot_update_the_roster(self):
        """球団と選手の画面の参照サービスからも、ロスターの更新を呼べない。"""
        for scope in (REAL, WORLD7):
            service = build_team_view_service(scope)
            for name in self.ROSTER_UPDATES:
                with self.subTest(scope=scope, method=name):
                    self.assertFalse(hasattr(service, name))

    def test_the_real_data_uses_the_calendar_today(self):
        self.assertEqual(build_league_view_service(REAL)._today, date.today)
        self.assertEqual(build_team_view_service(REAL)._today, date.today)

    def test_the_calendar_today_is_not_used_for_a_world(self):
        """世界の範囲では、年齢の基準日は暦の今日ではなく世界の「今日」。"""
        self.assertIsNot(build_league_view_service(WORLD7)._today, date.today)

    def test_the_world_analysis_is_fixed_to_its_world_and_uses_the_worlds_today(self):
        """戦力分析も、世界の範囲だけを読み、試合の無い球団の既定の年は世界の今日から取る。"""
        service = build_team_analysis_service(WORLD7)
        for name in ("_team_list_query", "_analysis_query", "_service_history_query"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)
        self.assertIsNot(service._today, date.today)
        self.assertEqual(build_team_analysis_service(REAL)._today, date.today)

    def test_the_header_view_takes_a_workspace_and_reads_each_worlds_standings_in_its_own_scope(self):
        """見出しは世界の台帳から読み、自軍の順位だけ世界の範囲（渡されたワークスペースの世界）のサービスで読む。"""
        view = build_pennant_world_view(1)
        self.assertEqual(list(signature(build_pennant_world_view).parameters), ["workspace_id"])
        self.assertEqual(view._standings_for(7)._game_list_query._scope, WORLD7)
        self.assertEqual(view._standings_for(8)._game_list_query._scope, WORLD8)
        # 同率の決着に使う試合も、世界ごとの範囲で読む
        self.assertEqual(view._games_for(7)._scope, WORLD7)
        self.assertEqual(view._games_for(8)._scope, WORLD8)
        # 世界の終了の判定に使うポストシーズンの状況も、世界ごとの範囲で導く
        # （組み立て口を差し替えて、渡る範囲を捕まえる。DB を読まない）
        scopes: list[WorldScope] = []

        def capture(scope: WorldScope) -> mock.Mock:
            scopes.append(scope)
            return mock.Mock(status=lambda year: year)

        with mock.patch.object(views, "build_pennant_postseason_view_service", side_effect=capture):
            self.assertEqual(view._postseason_status_for(7, 2026), 2026)
            self.assertEqual(view._postseason_status_for(8, 2027), 2027)
        self.assertEqual(scopes, [WORLD7, WORLD8])


class RealScopeTest(SimpleTestCase):
    """実データの範囲を渡して組み立てたサービスは、すべて実データの範囲で読み書きすること。"""

    def test_build_league_view_service_is_fixed_to_the_real_scope(self):
        service = build_league_view_service(REAL)
        for name in ("_teams", "_team_list_query", "_leagues", "_game_list_query", "_player_stats_query"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_build_team_view_service_is_fixed_to_the_real_scope(self):
        service = build_team_view_service(REAL)
        for name in ("_teams", "_games", "_game_list_query", "_player_fielding_query", "_player_stats_query"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_build_recording_service_is_fixed_to_the_real_scope(self):
        service = build_recording_service(REAL)
        for name in ("_games", "_teams", "_leagues", "_home_stadiums"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_build_team_analysis_service_is_fixed_to_the_real_scope(self):
        service = build_team_analysis_service(REAL)
        for name in ("_team_list_query", "_analysis_query", "_service_history_query"):
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

    def test_build_league_setup_service_is_fixed_to_the_real_scope(self):
        """登録（リーグ・チーム・球場）は実データの操作。依存は全部、渡された実データの範囲で作る。"""
        service = build_league_setup_service(REAL)
        for name in ("_leagues", "_teams", "_stadiums"):
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, REAL)

    def test_queries_built_for_views_are_fixed_to_the_real_scope(self):
        self.assertEqual(build_player_search_query(REAL)._scope, REAL)
        self.assertEqual(build_setup_query(REAL)._scope, REAL)

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
        classes = (
            BOTH_BUILDERS,
            REAL_ONLY_BUILDERS,
            PENNANT_ONLY_BUILDERS,
            LEDGER_BUILDERS,
            WORKSPACE_LEDGER_BUILDERS,
            MEMBER_BUILDERS,
        )
        found = self._found_builders()
        self.assertGreaterEqual(len(found), 16, "検査の対象を拾えていません")
        for function in found:
            with self.subTest(build=function.__name__):
                self.assertEqual(sum(function in group for group in classes), 1, "分類がちょうど1つではありません")
        classified = [function for group in classes for function in group]
        self.assertEqual(sorted(f.__name__ for f in classified), sorted(f.__name__ for f in found))

    def test_every_builder_takes_a_required_scope(self):
        for function in self._found_builders():
            if function in LEDGER_BUILDERS + WORKSPACE_LEDGER_BUILDERS + MEMBER_BUILDERS:
                continue
            with self.subTest(build=function.__name__):
                parameters = signature(function).parameters
                self.assertEqual(list(parameters), ["scope"])
                self.assertIs(parameters["scope"].default, Parameter.empty, "範囲に既定値があります")

    def test_the_ledger_only_view_takes_a_required_workspace(self):
        for build in LEDGER_BUILDERS + MEMBER_BUILDERS:
            parameters = signature(build).parameters
            self.assertEqual(list(parameters), ["workspace_id"])
            self.assertIs(parameters["workspace_id"].default, Parameter.empty, "ワークスペースに既定値があります")

    def test_only_the_workspace_ledgers_are_built_without_a_scope_or_a_workspace(self):
        """範囲もワークスペースの id も取らない組み立て口は、ワークスペースの台帳だけ（名前で固定する）。"""
        takes_nothing = {f.__name__ for f in self._found_builders() if not signature(f).parameters}
        self.assertEqual(
            takes_nothing,
            {
                "build_workspace_access_query",
                "build_workspace_list_query",
                "build_workspace_service",
                "build_workspace_member_service",
                "build_invitation_preview_query",
            },
        )
        self.assertEqual(takes_nothing, {f.__name__ for f in WORKSPACE_LEDGER_BUILDERS})
        self.assertIsInstance(build_workspace_access_query(), DjangoWorkspaceAccessQuery)
        self.assertIsInstance(build_workspace_list_query(), DjangoWorkspaceListQuery)
        self.assertIsInstance(build_invitation_preview_query(), DjangoInvitationPreviewQuery)
        query = build_workspace_management_query(3)
        self.assertIsInstance(query, DjangoWorkspaceManagementQuery)
        self.assertEqual(query._workspace_id, 3)

    def test_the_world_scope_builders_were_folded_into_the_scope_taking_ones(self):
        self.assertFalse(hasattr(views, "build_world_view_service"))
        self.assertFalse(hasattr(views, "build_world_team_analysis_service"))
        self.assertFalse(hasattr(views, "build_world_game_view_service"))
        self.assertFalse(hasattr(views, "build_world_team_view_service"))
        self.assertFalse(hasattr(views, "build_world_league_view_service"))
        self.assertFalse(hasattr(views, "build_service"))
        self.assertFalse(hasattr(views, "build_admin_overview_service"))


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
        self.assertEqual(service._league_view._game_list_query._scope, WORLD7, "順位も世界の範囲のサービス")
        self.assertEqual(service._team_view._games._scope, WORLD7, "主力も世界の範囲のサービス")
        self.assertEqual(service._team_view._player_stats_query._scope, WORLD7)
        for name in ("_postseason", "_championships"):
            with self.subTest(dependency=f"_postseason.{name}"):
                self.assertEqual(getattr(service._postseason, name)._scope, WORLD7)

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
        self.assertEqual(service._league_view._game_list_query._scope, WORLD7, "順位・タイトルも世界の範囲のサービス")
        for name in ("_postseason", "_championships"):
            with self.subTest(dependency=f"_postseason.{name}"):
                self.assertEqual(getattr(service._postseason, name)._scope, WORLD7)

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
        "_championships",
        "_context_query",
        "_playing_time",
    )

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_pennant_offseason_service(WORLD7)
        self.assertEqual(service._world_id, 7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)
        self.assertEqual(service._titles._game_list_query._scope, WORLD7, "タイトルを導くのも世界の範囲のサービス")
        # ドラフトの指名順の最終順位（#198）も世界の範囲
        self.assertEqual(service._finishes._games._scope, WORLD7)
        self.assertEqual(service._finishes._league_view._game_list_query._scope, WORLD7)

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


class OuterWorkspaceTeamsTest(SimpleTestCase):
    """範囲を取らない `real_teams_of_outer_workspace` は、`infrastructure/queries.py` だけが使う（W2 T1）。

    外側の `Workspace` の行に相関した副問い合わせとしてしか正しく働かない。ほかの場所で別のモデルを外側にして
    使うと、その行の pk をワークスペースの id と取り違え、エラーにならずにほかのワークスペースの球団を読む。
    `myapp/` の .py（テストを除く）を走査し、名前を出すのが定義とワークスペースの一覧だけかを確かめる。
    """

    NAME = "real_teams_of_outer_workspace"
    ALLOWED = {Path("myapp/infrastructure/queries.py"), Path("myapp/infrastructure/scoping.py")}

    def test_only_the_workspace_list_uses_the_correlated_teams(self):
        root = Path(__file__).resolve().parents[3]
        users = {
            path.relative_to(root)
            for path in (root / "myapp").rglob("*.py")
            if "tests" not in path.relative_to(root).parts and self.NAME in path.read_text(encoding="utf-8")
        }
        self.assertIn(Path("myapp/infrastructure/queries.py"), users, "検査の対象を拾えていません")
        self.assertEqual(users - self.ALLOWED, set(), "ワークスペースの一覧の外で相関副問い合わせの球団を使っています")


class WorkspaceLedgerUsersTest(SimpleTestCase):
    """範囲を取らないワークスペースの台帳は、定義（`infrastructure/queries.py`）と `views.py` だけが使う（W2 T6）。

    台帳は key・利用者からワークスペースそのものを引くだけで、範囲を作る前（`workspace_view`・`/` の一覧）に呼ばれる。
    application・管理コマンド・テンプレートタグなど、範囲を受け取れる場所で台帳を引き始めると、範囲の外から
    ワークスペースを選び直す経路になる。`myapp/` の .py（テストを除く）を走査し、名前を出すのが
    この2つだけかを確かめる。

    W4 で足したワークスペースそのものの台帳（`DjangoWorkspaceRepository`・`DjangoInvitationRepository`・
    `DjangoInvitationPreviewQuery`）と、その組み立て口（作成・設定・招待・メンバー）も同じ扱いにする
    （定義の `infrastructure/repositories.py` を足す）。管理画面がワークスペースの削除をサービスに通すときは（W4 T6）、
    ここに `myapp/admin.py` を足す。運営者の管理コマンド `workspace_delete`（W4 T2）は、`--workspace` の key で指した
    ワークスペースを消すのが仕事なので使ってよい（範囲を受け取る経路ではなく、範囲そのものを消す）。
    ヘッダーの切り替えの一覧（W4 T3）は、コンテキストプロセッサが views の `my_workspaces` を通して読む（名前は
    views.py の外に出さない。範囲を選び直す経路ではなく、利用者 id で自分の一覧を引くだけ）。
    """

    NAMES = (
        "build_workspace_access_query",
        "build_workspace_list_query",
        "build_workspace_service",
        "build_workspace_member_service",
        "build_invitation_preview_query",
        "DjangoWorkspaceAccessQuery",
        "DjangoWorkspaceListQuery",
        "DjangoWorkspaceRepository",
        "DjangoInvitationRepository",
        "DjangoInvitationPreviewQuery",
    )
    ALLOWED = {
        Path("myapp/presentation/views.py"),
        Path("myapp/infrastructure/queries.py"),
        Path("myapp/infrastructure/repositories.py"),
        Path("myapp/management/commands/workspace_delete.py"),
    }

    def test_only_the_views_use_the_workspace_ledgers(self):
        root = Path(__file__).resolve().parents[3]
        users = set()
        for path in (root / "myapp").rglob("*.py"):
            if "tests" in path.relative_to(root).parts:
                continue
            text = path.read_text(encoding="utf-8")
            if any(name in text for name in self.NAMES):
                users.add(path.relative_to(root))
        self.assertIn(Path("myapp/presentation/views.py"), users, "検査の対象を拾えていません")
        self.assertEqual(users - self.ALLOWED, set(), "views.py の外でワークスペースの台帳を使っています")


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
            build_pennant_postseason_service,
            build_pennant_postseason_view_service,
            build_pennant_settings_service,
        }
        self.assertEqual(seen, {build.__name__ for build in expected})

    def test_the_postseason_history_reads_the_ledger_of_the_scopes_workspace(self):
        """日本一・ポストシーズンの結果の読み取り（GM ホーム・歴代の記録）も、範囲のワークスペースの台帳を使う。"""
        world = WorldScope.pennant(self.WORKSPACE, 7)
        for build in (build_pennant_home_service, build_pennant_history_service):
            with self.subTest(build=build.__name__):
                worlds = build(world)._postseason._worlds
                self.assertIsInstance(worlds, DjangoWorldRepository)
                self.assertEqual(worlds._workspace_id, self.WORKSPACE)

    def test_the_worlds_today_reads_the_ledger_of_the_scopes_workspace(self):
        """世界の「今日」（`_world_clock`）も、範囲のワークスペースの台帳で世界を引く。"""
        world = WorldScope.pennant(self.WORKSPACE, 7)
        for today in (
            build_league_view_service(world)._today,
            build_team_view_service(world)._today,
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


class LegacyWorkspaceIsGoneTest(SimpleTestCase):
    """一時関数のワークスペース（id が最小のもの）は、どこにも残っていない（W2 T4。#78）。

    画面は URL の key を `workspace_view` が解決し、管理コマンドは `--workspace <key>` を必須で受け取る
    （`real_scope_for_key`）。「id が最小のワークスペース」で推し量る関数が戻ると、ワークスペースが複数あるときに
    エラーにならず、別のワークスペースを読み書きする。名前で戻さない（テスト以外の `myapp/` に名前が無い）。
    """

    NAMES = ("legacy_workspace_id", "legacy_real_scope", "_legacy_workspace")

    def test_no_legacy_name_is_left_in_the_app(self):
        root = Path(__file__).resolve().parents[3]
        users = {
            (path.relative_to(root), name)
            for path in (root / "myapp").rglob("*.py")
            if "tests" not in path.relative_to(root).parts
            for name in self.NAMES
            if name in path.read_text(encoding="utf-8")
        }
        self.assertEqual(users, set(), "一時関数のワークスペース（id が最小）の名前が残っています")


class TeamPermissionQueryIsGoneTest(SimpleTestCase):
    """サイトのチームの書き込みの判定は、`workspace_view` が解決した `WorkspaceAccess` だけで行う（W2 T5。#78）。

    前の判定（`DjangoTeamPermissionQuery`・`build_permission_query`）は `is_staff` に範囲のどのチームの書き込みも
    許していた。URL にワークスペースが入った後に戻ると、公開ワークスペースにメンバーでない運営者が書き込める
    （閲覧の判定と書き込みの判定が食い違う）。名前で戻さない（テスト以外の `myapp/` に名前が無い）。
    """

    NAMES = ("DjangoTeamPermissionQuery", "build_permission_query", "TeamPermissionQuery")

    def test_no_team_permission_query_is_left_in_the_app(self):
        root = Path(__file__).resolve().parents[3]
        users = {
            (path.relative_to(root), name)
            for path in (root / "myapp").rglob("*.py")
            if "tests" not in path.relative_to(root).parts
            for name in self.NAMES
            if name in path.read_text(encoding="utf-8")
        }
        self.assertEqual(users, set(), "チームの権限の判定が WorkspaceAccess の外に残っています")

    def test_views_do_not_read_is_staff(self):
        """サイトの views・API は `user.is_staff`・`user.is_superuser` を読まない。

        管理画面のリンクの表示（`base.html`）はテンプレートだけが見る。
        """
        root = Path(__file__).resolve().parents[3]
        users = {
            path.relative_to(root)
            for path in (root / "myapp" / "presentation").glob("*.py")
            if ".is_staff" in path.read_text(encoding="utf-8") or ".is_superuser" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(users, set(), "サイトの判定に is_staff・is_superuser が残っています")


class PennantPostseasonScopeTest(SimpleTestCase):
    """ポストシーズンの進行と、締める処理が読む勝ち上がりは、渡された世界の範囲だけを読み書きする。"""

    SCOPED = ("_games", "_fixtures", "_ratings", "_plans", "_context_query")
    BRACKETS = ("_leagues", "_postseason")

    def test_every_dependency_is_fixed_to_the_given_world(self):
        service = build_pennant_postseason_service(WORLD7)
        self.assertEqual(service._world_id, 7)
        for name in self.SCOPED:
            with self.subTest(dependency=name):
                self.assertEqual(getattr(service, name)._scope, WORLD7)

    def test_the_brackets_read_only_the_given_world(self):
        for brackets in (
            build_pennant_postseason_service(WORLD7)._brackets,
            build_pennant_offseason_service(WORLD7)._postseason,
        ):
            for name in self.BRACKETS:
                with self.subTest(dependency=name):
                    self.assertEqual(getattr(brackets, name)._scope, WORLD7)
            self.assertEqual(brackets._league_view._game_list_query._scope, WORLD7, "順位も世界の範囲のサービス")
            self.assertEqual(brackets._finishes._games._scope, WORLD7, "同率の決着の材料も世界の範囲")
            self.assertEqual(brackets._finishes._league_view._game_list_query._scope, WORLD7)

    def test_a_different_world_gets_a_different_scope(self):
        self.assertEqual(build_pennant_postseason_service(WORLD8)._games._scope, WORLD8)
        self.assertEqual(build_pennant_postseason_service(WORLD8)._brackets._postseason._scope, WORLD8)

    def test_the_real_data_has_no_postseason(self):
        with self.assertRaises(InvalidWorld):
            build_pennant_postseason_service(REAL)

    def test_the_transaction_is_the_database_transaction(self):
        from django.db import transaction

        self.assertIs(build_pennant_postseason_service(WORLD7)._atomic, transaction.atomic)


class ForeignQuotaExemptionScopeTest(SimpleTestCase):
    """FA 資格の取得による外国人枠の対象外は、使う側と同じ範囲の記録だけで導く（実データと世界の FA を混ぜない）。"""

    def test_the_scorebook_reads_the_real_data(self):
        self.assertEqual(build_recording_service(REAL)._fa_exemptions._query._scope, REAL)

    def test_the_pennant_services_read_their_own_world(self):
        for build in (
            build_pennant_season_service,
            build_pennant_postseason_service,
            build_club_service,
            build_pennant_offseason_service,
        ):
            with self.subTest(build=build.__name__):
                self.assertEqual(build(WORLD7)._fa_exemptions._query._scope, WORLD7)
