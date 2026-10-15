"""ペナントの進行と投手の疲労（#97 段階1）。

疲労は保存せず、世界の直近の登板（`SimulationContextQuery` の最後の先発）から導く。手動の5人ローテーション
（毎日の試合で中4日の先発が出る形）でも、1日ずつ進めても2週間まとめて進めても同じ試合になること、
疲労が進行の経路まで届いていることを確かめる。
"""

from collections import defaultdict
from unittest import mock

from django.db import transaction

from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.simulation.fatigue import MIN_DAYS_BETWEEN_STARTS, fatigued, starter_rest_points
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_club_service

from .test_pennant_advance import SeasonCase


class FiveManRotationTest(SeasonCase):
    """自軍だけ手動の5人ローテーションにした世界。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        home = orm_models.Team.objects.get(league__world_id=cls.world_a, name=cls.home.name)
        service = build_club_service(cls.world_a)
        service.set_rotation(home.id, list(service.propose(home.id).rotation_ids[:5]))

    @classmethod
    def replay(cls, run):
        """`run` で世界を進めた結果を取り、世界を元に戻す。"""
        savepoint = transaction.savepoint()
        try:
            run()
            return cls.snapshot(cls.world_a)
        finally:
            transaction.savepoint_rollback(savepoint)

    def two_weeks(self):
        self.advance(self.world_a, AdvanceTarget.WEEK)
        self.advance(self.world_a, AdvanceTarget.WEEK)

    def test_the_five_man_rotation_starts_on_four_days_of_rest(self):
        """毎日の試合で5人を回すと中4日（5日後）の先発が出る（疲労が起きる形になっている）。"""
        self.two_weeks()
        starts = defaultdict(list)
        for line in orm_models.GamePitchingLine.objects.filter(
            game__home_team__league__world_id=self.world_a, appearance_order=1
        ).select_related("game"):
            starts[line.player_id].append(line.game.played_on)
        gaps = set()
        for days in starts.values():
            days.sort()
            gaps.update((later - earlier).days for earlier, later in zip(days, days[1:], strict=False))
        # 相手（AI の6人ローテ）は中5日を割らないので、中4日は自軍の5人ローテから
        self.assertIn(MIN_DAYS_BETWEEN_STARTS - 1, gaps)

    def test_days_one_by_one_equal_two_weeks_at_once(self):
        """中4日の疲労は、進めるの区切りをまたいでも同じに効く（最後の先発は世界の全試合から読む）。"""
        weeks = self.replay(self.two_weeks)
        played = len({row[0] for row in weeks[0]})

        one_by_one = self.replay(lambda: [self.advance(self.world_a, AdvanceTarget.DAY) for _ in range(played)])

        self.assertEqual(weeks, one_by_one)

    def test_the_fatigue_reaches_the_engine_through_the_advance(self):
        """進行の経路でも、中4日の先発は疲労を背負って投げる（能力の目減りまで届いている）。"""
        points: list[int] = []

        def spy(ratings, fatigue):
            points.append(fatigue)
            return fatigued(ratings, fatigue)

        with mock.patch("myapp.domain.simulation.engine.fatigued", side_effect=spy):
            self.two_weeks()

        self.assertIn(starter_rest_points(MIN_DAYS_BETWEEN_STARTS - 1), points)
