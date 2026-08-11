"""依存の組み立てに関する検査。

アプリケーションサービスはインターフェース（domain/repositories.py・
application/queries.py）だけに依存し、実装は組み立ての1か所（build_service）で
差し込む。実装側でメソッド名が変わっても、この検査が無いと画面を開くまで
気づけないため、機械的に確かめる。
"""

from django.test import SimpleTestCase

from myapp.application.queries import GameListQuery, TeamListQuery
from myapp.application.services import TeamApplicationService
from myapp.domain.repositories import GameRepository, LeagueRepository, TeamRepository
from myapp.infrastructure.queries import DjangoGameListQuery, DjangoTeamListQuery
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoTeamRepository,
)
from myapp.presentation.views import build_service


class ProtocolConformanceTest(SimpleTestCase):
    IMPLEMENTATIONS = [
        (DjangoTeamRepository, TeamRepository),
        (DjangoGameRepository, GameRepository),
        (DjangoLeagueRepository, LeagueRepository),
        (DjangoTeamListQuery, TeamListQuery),
        (DjangoGameListQuery, GameListQuery),
    ]

    def test_implementations_satisfy_interfaces(self):
        """infrastructure の実装がインターフェースを満たしていること。"""
        for implementation, interface in self.IMPLEMENTATIONS:
            with self.subTest(implementation=implementation.__name__):
                self.assertIsInstance(implementation(), interface)


class BuildServiceTest(SimpleTestCase):
    def test_all_dependencies_are_wired(self):
        """組み立てたサービスに、依存が1つも欠けていないこと。

        欠けたまま作れると、使う画面によって「None に find_all は無い」で
        落ちるサービスができてしまう。
        """
        service = build_service()
        missing = [name for name, value in vars(service).items() if value is None]
        self.assertEqual(missing, [], f"依存が渡されていません: {missing}")

    def test_requires_every_dependency(self):
        """依存を省略したサービスは作れないこと。"""
        with self.assertRaises(TypeError):
            TeamApplicationService(teams=DjangoTeamRepository())  # type: ignore[call-arg]
