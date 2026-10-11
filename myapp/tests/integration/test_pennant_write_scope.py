"""進める処理が書き込みロックを持つのは、保存の間だけ（検査と行の組み立てはトランザクションの外）。

本番の SQLite は書き手が1つで、`BEGIN IMMEDIATE` から他の書き込みを止める。試合の検査と行の組み立て
（CPU の仕事）を `atomic` の外へ出しても、**できる行は変わらない**ことと、外に出ていることを確かめる。
"""

from contextlib import contextmanager

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from myapp.domain.exceptions import InvalidGame
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.presentation.views import build_pennant_season_service

from .test_pennant_advance import SeasonCase


class _SaveOneByOne:
    """`add_all` の代わりに、変更前から使っている `save()`（1試合ずつの update_or_create）で書く。

    一括の書き込みと同じ行ができるかを、別の経路を物差しにして確かめるための差し替え。
    """

    def __init__(self, inner):
        self._inner = inner

    def prepare_add_all(self, games):
        def write():
            for game in games:
                self._inner.save(game)

        return write

    def __getattr__(self, name):
        return getattr(self._inner, name)


class RowsDoNotChangeTest(SeasonCase):
    def _replay(self, *, one_by_one: bool):
        """世界を1週間進めた結果を取り、世界を元に戻す（球団の id が結果に効くので、同じ世界で比べる）。"""
        savepoint = transaction.savepoint()
        try:
            service = build_pennant_season_service(self.world_a)
            if one_by_one:
                service._games = _SaveOneByOne(service._games)
            report = service.advance(AdvanceTarget.WEEK)
            return report.games, self.snapshot(self.world_a)
        finally:
            transaction.savepoint_rollback(savepoint)

    def test_bulk_rows_equal_the_rows_saved_one_game_at_a_time(self):
        bulk_games, bulk = self._replay(one_by_one=False)
        save_games, saved = self._replay(one_by_one=True)

        self.assertGreater(bulk_games, 0)
        self.assertEqual(bulk_games, save_games)
        self.assertEqual(bulk, saved)

    def test_the_games_read_back_equal_the_games_made(self):
        """作った試合と、保存して読み戻した試合が同じ（打席・明細・イニングスコアの欠けが無い）。"""
        service = build_pennant_season_service(self.world_a)
        made = []
        inner = service._games

        class Recording:
            def prepare_add_all(self, games):
                made.extend(games)
                return inner.prepare_add_all(games)

            def __getattr__(self, name):
                return getattr(inner, name)

        service._games = Recording()
        service.advance(AdvanceTarget.WEEK)

        self.assertGreater(len(made), 0)
        for game in made:
            assert game.id is not None
            read = inner.find_by_id(game.id)
            self.assertEqual(
                [(p.sequence, p.batter_id, p.pitcher_id, p.result, p.fielded_by) for p in read.plate_appearances],
                [(p.sequence, p.batter_id, p.pitcher_id, p.result, p.fielded_by) for p in game.plate_appearances],
            )
            self.assertEqual(
                {(e.player_id, e.line) for e in read.batting}, {(e.player_id, e.line) for e in game.batting}
            )
            self.assertEqual(
                {(e.player_id, e.line) for e in read.pitching}, {(e.player_id, e.line) for e in game.pitching}
            )
            self.assertEqual(
                {(e.player_id, e.line) for e in read.fielding}, {(e.player_id, e.line) for e in game.fielding}
            )
            self.assertEqual(read.line_score, game.line_score)


class LockScopeTest(SeasonCase):
    def _spy(self, service):
        """検査・組み立て（prepare）と、トランザクションの出入りの順を記録する。"""
        events: list[str] = []
        inner_games = service._games
        inner_atomic = service._atomic

        class Games:
            def prepare_add_all(self, games):
                events.append("prepare")
                write = inner_games.prepare_add_all(games)

                def spy_write():
                    events.append("write")
                    write()

                return spy_write

            def __getattr__(self, name):
                return getattr(inner_games, name)

        @contextmanager
        def atomic():
            events.append("enter")
            with inner_atomic():
                yield
            events.append("exit")

        service._games = Games()
        service._atomic = atomic
        return events

    def test_the_check_and_the_row_building_happen_outside_the_transaction(self):
        service = build_pennant_season_service(self.world_a)
        service.ensure_schedule()
        events = self._spy(service)

        service.advance(AdvanceTarget.DAY)

        self.assertEqual(events, ["prepare", "enter", "write", "exit"])

    def test_a_game_that_fails_the_check_never_opens_a_transaction(self):
        service = build_pennant_season_service(self.world_a)
        service.ensure_schedule()
        events = self._spy(service)
        fixtures_before = len(self.fixtures_of(self.world_a))

        def refuse(games):
            raise InvalidGame("検査に通らない試合")

        service._games.prepare_add_all = refuse

        with self.assertRaises(InvalidGame):
            service.advance(AdvanceTarget.DAY)

        self.assertEqual(events, [], "検査で落ちたときは、書き込みのトランザクションを開かない")
        self.assertEqual(len(self.fixtures_of(self.world_a)), fixtures_before)
        self.assertEqual(self.games_of(self.world_a), [])

    def test_preparing_writes_nothing_until_the_returned_function_is_called(self):
        service = build_pennant_season_service(self.world_a)
        service.ensure_schedule()
        made = []
        inner = service._games

        class Recording:
            def prepare_add_all(self, games):
                made.extend(games)
                return lambda: None

            def __getattr__(self, name):
                return getattr(inner, name)

        service._games = Recording()
        service.advance(AdvanceTarget.DAY)
        self.assertEqual(self.games_of(self.world_a), [], "返った関数を呼ばなければ何も書かれない")

        with CaptureQueriesContext(connection) as queries:
            prepared = inner.prepare_add_all(made)
        self.assertFalse(
            [q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))],
            "準備の間に書き込みの SQL を流さない",
        )
        self.assertEqual(self.games_of(self.world_a), [])

        with transaction.atomic():
            prepared()
        self.assertEqual(len(self.games_of(self.world_a)), len(made))
