"""仮想試合の投入コマンドが作る、開始・終了の時刻と観客数（#44 段階2）。"""

from datetime import time
from unittest.mock import patch

from myapp.domain.value_objects import GameTimes
from myapp.infrastructure import orm_models
from myapp.management.commands.seed_virtual_games import Command

from ..helpers import make_stadium, make_workspace
from .test_seed_virtual_games_rules import YEAR, SeedGamesBase, run

CAPACITY = 30000


class SeedGamesGameDayTest(SeedGamesBase):
    def setUp(self) -> None:
        super().setUp()
        self.stadium.capacity = CAPACITY
        self.stadium.save()
        run()

    def test_every_game_has_times(self):
        games = orm_models.Game.objects.filter(year=YEAR)
        self.assertGreater(games.count(), 0)
        for game in games:
            with self.subTest(game=game.id):
                self.assertIsNotNone(game.start_time)
                self.assertIn(game.start_time, (time(14, 0), time(18, 0)))
                self.assertEqual(game.start_time, time(14, 0) if game.played_on.weekday() >= 5 else time(18, 0))
                self.assertEqual(game.interruption_minutes, 0)
                # 保存した値が `GameTimes` の検査を通る組み合わせであること
                times = GameTimes(game.start_time, game.end_time, game.ends_next_day)
                self.assertGreater(times.net_minutes, 90)

    def test_attendance_follows_the_home_stadium(self):
        for game in orm_models.Game.objects.filter(year=YEAR):
            with self.subTest(game=game.id):
                if game.home_team_id == self.team.id:
                    self.assertIsNotNone(game.attendance)
                    self.assertLessEqual(game.attendance, CAPACITY)
                else:
                    self.assertIsNone(game.attendance, "本拠地の無い球団のホーム試合は観客数を作らない")

    def test_a_game_with_the_same_seed_has_the_same_result_and_times(self):
        def snapshot():
            return list(
                orm_models.Game.objects.filter(year=YEAR)
                .order_by("played_on", "home_team_id", "away_team_id")
                .values_list("played_on", "home_score", "away_score", "start_time", "end_time", "attendance")
            )

        first = snapshot()
        run("--replace")
        self.assertEqual(snapshot(), first)


class SeedGamesOutcomeUnchangedTest(SeedGamesBase):
    """時刻と観客数は別の乱数の列で作るので、作る・作らないで試合の結果（得点・打席）は変わらない。"""

    @staticmethod
    def outcome():
        plate_appearances = (
            orm_models.GamePlateAppearance.objects.filter(game__year=YEAR)
            .order_by("game__played_on", "game__home_team_id", "game__away_team_id", "sequence")
            .values_list("game__played_on", "game__home_team_id", "sequence", "batter_id", "pitcher_id", "result")
        )
        scores = orm_models.Game.objects.filter(year=YEAR).values_list(
            "played_on", "home_team_id", "away_team_id", "home_score", "away_score"
        )
        return list(plate_appearances), sorted(scores)

    def test_outcome_is_identical_without_the_game_day(self):
        with patch.object(Command, "_stamp_game_day", lambda self, game, capacity: None):
            run()
        without = self.outcome()
        self.assertIsNone(orm_models.Game.objects.filter(year=YEAR).first().start_time)

        run("--replace")

        self.assertGreater(len(without[0]), 0)
        self.assertEqual(self.outcome(), without)


class SeedGamesOtherWorkspaceStadiumTest(SeedGamesBase):
    """別のワークスペースの球場を指す球団（ORM の直書き）のホーム試合に、その球場の収容人数を使わない（W1b T4）。"""

    def setUp(self) -> None:
        super().setUp()
        self.stadium.capacity = CAPACITY
        self.stadium.save()
        other = make_stadium(
            "別ワークスペースの球場", workspace=make_workspace(name="別ワークスペース"), capacity=CAPACITY
        )
        orm_models.Team.objects.filter(id=self.rival.id).update(home_stadium=other)
        run()

    def test_the_capacity_of_another_workspaces_stadium_is_not_used(self):
        games = orm_models.Game.objects.filter(year=YEAR)
        rival_home = games.filter(home_team_id=self.rival.id)
        self.assertGreater(rival_home.count(), 0)
        self.assertFalse(
            rival_home.filter(attendance__isnull=False).exists(), "別のワークスペースの球場で観客数を作った"
        )
        # 空振りの防止: 自分のワークスペースの球場なら観客数を作る
        self.assertFalse(games.filter(home_team_id=self.team.id, attendance__isnull=True).exists())
