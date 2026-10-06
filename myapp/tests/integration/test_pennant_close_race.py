"""シーズンを締める処理は、読み込みと計算を書き込みトランザクションの外で行う。

外で計算している間に別のリクエストが締めた・進めたときは、中で状態の変化に気づき、古い読み込みを
書かずに既存の例外で断る。前提の検査・二重実行の検査・原子性は `test_pennant_offseason.py`。
"""

from contextlib import contextmanager

from django.db import transaction

from myapp.domain.exceptions import AlreadyClosed, SeasonNotFinished
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import JerseyNumber, Position, RosterLimits
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoTeamRepository
from myapp.presentation.views import build_pennant_offseason_service, build_pennant_season_service

from .test_pennant_advance import SEASON_GAMES
from .test_pennant_offseason import YEAR, ClosedSeasonCase


class CloseRaceTest(ClosedSeasonCase):
    def _service_with_rival(self, rival):
        """計算（`_prepare`）が済んだ直後、書く前に `rival()` を実行するサービス（別のリクエストの割り込み）。"""
        service = build_pennant_offseason_service(self.world_a)
        prepare = service._prepare

        def prepare_then_interrupt(world, year):
            prepared = prepare(world, year)
            rival()
            return prepared

        service._prepare = prepare_then_interrupt
        return service

    def test_the_read_and_the_calculation_happen_before_the_transaction_opens(self):
        service = build_pennant_offseason_service(self.world_a)
        events: list[str] = []
        prepare, commit, atomic = service._prepare, service._commit, service._atomic

        @contextmanager
        def spy_atomic():
            events.append("enter")
            with atomic():
                yield
            events.append("exit")

        service._prepare = lambda world, year: (events.append("prepare"), prepare(world, year))[1]
        service._commit = lambda world, prepared: (events.append("commit"), commit(world, prepared))[1]
        service._atomic = spy_atomic

        service.close_season()

        self.assertEqual(events, ["prepare", "enter", "commit", "exit"])

    def test_another_request_closing_in_the_meantime_is_refused_and_nothing_is_written_twice(self):
        # 1回だけ締めた世界の姿（比べる相手）。巻き戻して、割り込まれる側を同じ世界でやり直す
        savepoint = transaction.savepoint()
        self.close()
        closed_once = self.counts()
        transaction.savepoint_rollback(savepoint)
        service = self._service_with_rival(lambda: build_pennant_offseason_service(self.world_a).close_season())

        with self.assertRaises(AlreadyClosed) as raised:
            service.close_season()

        self.assertEqual(raised.exception.year, YEAR)
        # 割り込んだ側の1回ぶんだけが残る（選手・在籍・退団・能力・日程が二重にならない）
        self.assertEqual(self.counts(), closed_once)
        self.assertEqual(closed_once["fixtures"], SEASON_GAMES)

    def test_a_player_added_in_the_meantime_stops_the_close_and_is_not_overwritten(self):
        """外で読んだ名簿を中で `save()` すると、割り込みで加わった選手を消す。在籍の目印で気づいて断る。"""

        def add_a_player():
            repository = DjangoTeamRepository(WorldScope.pennant(self.world_a))
            team = repository.find_all_with_roster()[0]
            team.add_player(
                "途中加入", JerseyNumber("99"), Position.PITCHER, from_year=YEAR, limits=RosterLimits.UNLIMITED
            )
            repository.save(team)

        service = self._service_with_rival(add_a_player)

        with self.assertRaisesMessage(AlreadyClosed, "世界の状態が変わりました"):
            service.close_season()

        self.assertTrue(
            orm_models.Player.objects.filter(name="途中加入", stints__to_year__isnull=True).exists(),
            "割り込みで加わった選手は消えない",
        )
        self.assertEqual(orm_models.PennantFixture.objects.filter(home_team__league__world_id=self.world_a).count(), 0)
        self.assertEqual(self.counts()["closed"], 0, "締める書き込みはしていない")

    def test_another_request_advancing_in_the_meantime_is_refused(self):
        def close_and_advance():
            build_pennant_offseason_service(self.world_a).close_season()
            build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)

        service = self._service_with_rival(close_and_advance)

        with self.assertRaises(SeasonNotFinished):
            service.close_season()

    def test_a_close_without_interruption_still_writes_everything(self):
        before = self.counts()

        result = build_pennant_offseason_service(self.world_a).close_season()

        after = self.counts()
        self.assertEqual(result.year, YEAR)
        self.assertGreater(after["ratings"], before["ratings"])
        self.assertEqual(after["fixtures"], result.fixture_count)
