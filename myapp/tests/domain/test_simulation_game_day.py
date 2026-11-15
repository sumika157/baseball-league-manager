"""シミュレーションの時刻と観客数のモデル（#44 段階2）。Django も DB も使わない。

守りたいこと:
- 開始は平日 18:00・土日 14:00。終了 − 開始が正味の試合時間で、0時を越えたら翌日の旗が立つ
- 試合時間は打席が増えるほど長くなり、9回の試合の平均が NPB の水準（3時間00分〜3時間10分前後）に入る
- 観客数は収容人数を超えず、収容人数が分からなければ None、土日の平均が平日より高い
- **試合の結果（打席）は、時刻と観客数を作っても変わらない**（別の乱数源を使うため。統合テストで確認する）
"""

from datetime import date, time
from statistics import mean
from unittest import TestCase

from myapp.domain.pennant.ticket_price import TicketPrice
from myapp.domain.simulation.engine import simulate_game
from myapp.domain.simulation.game_day import simulated_attendance, simulated_times
from myapp.domain.simulation.manager import choose_active_roster
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.simulation.samples import average_club
from myapp.domain.value_objects import ForeignPlayerRules, GameStage

from .doubles import ScriptedRandom

WEEKDAY = date(2026, 4, 1)  # 水曜
SATURDAY = date(2026, 4, 4)
SAMPLES = 120


def _clubs():
    return choose_active_roster(average_club(1), rules=ForeignPlayerRules.UNLIMITED), choose_active_roster(
        average_club(2), rules=ForeignPlayerRules.UNLIMITED
    )


def _games(count: int, day: date = WEEKDAY):
    home, away = _clubs()
    return [
        simulate_game(
            make_random(seed),
            home,
            away,
            stage=GameStage.REGULAR,
            played_on=day,
            foreign_rules=ForeignPlayerRules.UNLIMITED,
        ).game
        for seed in range(count)
    ]


class StartTimeTest(TestCase):
    def test_weekday_is_night_and_weekend_is_day(self):
        game = _games(1)[0]
        rng = ScriptedRandom(0.5, 0.5)

        self.assertEqual(simulated_times(game, rng).start, time(18, 0))
        for weekend in (SATURDAY, date(2026, 4, 5)):
            game.played_on = weekend
            self.assertEqual(simulated_times(game, rng).start, time(14, 0))


class DurationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.games = _games(SAMPLES)

    def test_end_minus_start_is_the_net_duration_without_interruption(self):
        for game in self.games[:20]:
            times = simulated_times(game, make_random(1))
            self.assertEqual(times.interruption_minutes, 0)
            self.assertEqual(times.net_minutes, times.elapsed_minutes)

    def test_passing_midnight_sets_the_next_day_flag(self):
        game = _games(1)[0]  # 平日なので18:00開始
        # 平均的な長さなら日をまたがない
        self.assertFalse(simulated_times(game, ScriptedRandom(0.5, 0.0)).ends_next_day)
        # 打席を4倍（延長を重ねたような長い試合）にすると0時を越える
        game.plate_appearances = game.plate_appearances * 4
        times = simulated_times(game, ScriptedRandom(0.5, 0.0))
        self.assertTrue(times.ends_next_day)
        self.assertLess(times.end, times.start)

    def test_more_plate_appearances_take_longer(self):
        ordered = sorted(self.games, key=lambda game: len(game.plate_appearances))
        short, long = ordered[: SAMPLES // 4], ordered[-SAMPLES // 4 :]
        rng_values = (0.5, 0.0)  # 揺らぎ 0（cos(0)=1 で平均より少し長い方向に固定）
        short_avg = mean(simulated_times(game, ScriptedRandom(*rng_values)).net_minutes for game in short)
        long_avg = mean(simulated_times(game, ScriptedRandom(*rng_values)).net_minutes for game in long)
        self.assertGreater(long_avg, short_avg)

    def test_the_average_of_nine_inning_games_is_in_the_npb_band(self):
        nine = [game for game in self.games if game.line_score.innings == 9]
        self.assertGreater(len(nine), SAMPLES // 2)
        durations = [
            simulated_times(game, make_random(game_seed(0, 2026, str(i)))).net_minutes for i, game in enumerate(nine)
        ]
        # 目標は 3:00〜3:10（180〜190分）。帯は緩く取る（端を詰める調整はしない）
        self.assertTrue(170 <= mean(durations) <= 200, mean(durations))

    def test_same_seed_gives_the_same_times(self):
        game = self.games[0]
        self.assertEqual(simulated_times(game, make_random(5)), simulated_times(game, make_random(5)))


class AttendanceTest(TestCase):
    def test_never_exceeds_capacity_and_is_none_without_one(self):
        rng = make_random(3)
        for _ in range(500):
            for day in (WEEKDAY, SATURDAY):
                value = simulated_attendance(30000, day, rng)
                self.assertIsNotNone(value)
                self.assertLessEqual(value, 30000)
                self.assertGreaterEqual(value, 12000)
        self.assertIsNone(simulated_attendance(None, WEEKDAY, rng))

    def test_a_full_house_is_capped_at_the_capacity(self):
        self.assertEqual(simulated_attendance(30000, SATURDAY, ScriptedRandom(0.999999, 0.0)), 30000)

    def test_weekends_draw_more_than_weekdays(self):
        rng = make_random(11)
        weekday = mean(simulated_attendance(40000, WEEKDAY, rng) or 0 for _ in range(1000))
        weekend = mean(simulated_attendance(40000, SATURDAY, rng) or 0 for _ in range(1000))
        self.assertGreater(weekend, weekday)
        self.assertTrue(0.75 <= weekday / 40000 <= 0.85, weekday)
        self.assertTrue(0.85 <= weekend / 40000 <= 0.92, weekend)


class _CountingRandom:
    """引いた回数を数える乱数源（乱数を引く回数が人気の有無で変わらないことを確かめる）。"""

    def __init__(self, seed: int) -> None:
        self._rng = make_random(seed)
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self._rng.random()


class PopularityAttendanceTest(TestCase):
    """人気の観客数への効き（#208 E1。決定: 人気を渡さないか平均の50なら、足す前と完全に同じ値）。"""

    def test_average_popularity_or_none_gives_exactly_the_same_attendance(self):
        for seed in range(200):
            for day in (WEEKDAY, SATURDAY):
                with self.subTest(seed=seed, day=day):
                    before = simulated_attendance(32000, day, make_random(seed))
                    self.assertEqual(
                        simulated_attendance(32000, day, make_random(seed), home_popularity=50, away_popularity=50),
                        before,
                    )
                    self.assertEqual(
                        simulated_attendance(
                            32000, day, make_random(seed), home_popularity=None, away_popularity=None
                        ),
                        before,
                    )

    def test_popularity_does_not_change_how_many_numbers_are_drawn(self):
        """乱数を引く回数は人気で変わらない（同じ乱数源の後続の値をずらさない）。"""
        plain, popular = _CountingRandom(1), _CountingRandom(1)
        for _ in range(100):
            simulated_attendance(30000, WEEKDAY, plain)
            simulated_attendance(30000, WEEKDAY, popular, home_popularity=90, away_popularity=10)
        self.assertEqual(plain.calls, popular.calls)

    def test_a_more_popular_home_club_draws_more(self):
        means = []
        for popularity in (20, 50, 80):
            rng = make_random(21)
            means.append(
                mean(simulated_attendance(40000, WEEKDAY, rng, home_popularity=popularity) or 0 for _ in range(1000))
            )
        self.assertLess(means[0], means[1])
        self.assertLess(means[1], means[2])

    def test_the_visitor_counts_less_than_the_home_club(self):
        home_rng, away_rng = make_random(31), make_random(31)
        home = mean(simulated_attendance(40000, WEEKDAY, home_rng, home_popularity=80) or 0 for _ in range(1000))
        away = mean(simulated_attendance(40000, WEEKDAY, away_rng, away_popularity=80) or 0 for _ in range(1000))
        baseline = mean(simulated_attendance(40000, WEEKDAY, make_random(31)) or 0 for _ in range(1000))
        self.assertGreater(away, baseline)
        self.assertGreater(home, away)

    def test_the_most_popular_club_never_exceeds_the_capacity(self):
        rng = make_random(41)
        for _ in range(500):
            value = simulated_attendance(30000, SATURDAY, rng, home_popularity=100, away_popularity=100)
            self.assertIsNotNone(value)
            self.assertLessEqual(value, 30000)
        self.assertIsNone(simulated_attendance(None, WEEKDAY, rng, home_popularity=100))


class TicketPriceAttendanceTest(TestCase):
    """チケットの値段（#208 E2）は入場率の平均に効く。渡さないか標準なら今と完全に同じ値、乱数を引く回数も変えない。"""

    def test_no_price_or_the_standard_price_gives_exactly_the_same_attendance(self):
        for seed in range(200):
            for day in (WEEKDAY, SATURDAY):
                with self.subTest(seed=seed, day=day):
                    before = simulated_attendance(32000, day, make_random(seed), home_popularity=70)
                    self.assertEqual(
                        simulated_attendance(
                            32000, day, make_random(seed), home_popularity=70, ticket_price=TicketPrice.STANDARD
                        ),
                        before,
                    )

    def test_the_price_does_not_change_how_many_numbers_are_drawn(self):
        plain, priced = _CountingRandom(1), _CountingRandom(1)
        for _ in range(100):
            simulated_attendance(30000, WEEKDAY, plain)
            simulated_attendance(30000, WEEKDAY, priced, ticket_price=TicketPrice.HIGH)
        self.assertEqual(plain.calls, priced.calls)

    def test_a_higher_price_draws_fewer_fans_and_never_exceeds_the_capacity(self):
        """2026-11-14 ユーザー決定: 高いほど入場率が下がる（+0.08 / +0.04 / 0 / −0.05 / −0.10）。"""
        means = []
        for price in TicketPrice:
            rng = make_random(51)
            values = [
                simulated_attendance(30000, SATURDAY, rng, home_popularity=60, ticket_price=price) or 0
                for _ in range(1000)
            ]
            self.assertLessEqual(max(values), 30000)
            means.append(mean(values))
        self.assertEqual(means, sorted(means, reverse=True), "安い順に並べると観客数は減っていく")
        self.assertGreater(means[0], means[-1])
