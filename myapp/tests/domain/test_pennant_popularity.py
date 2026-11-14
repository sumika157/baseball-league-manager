"""ペナントの球団の人気（#208 E1）。Django も DB も使わない。

守りたいこと（2026-11-14 ユーザーの決定と #208 の計画）:
- 人気は 1〜100 の整数で、初期値は全球団50（収容人数・勝率は使わない）
- 翌年の人気は、平均への戻り・勝率・リーグ優勝・日本一・スター選手で動き、範囲に収まり、同じ世界・年・球団なら同じ値
- 優勝・日本一・スターの加点は世界の平均を引いて足す（毎年どこかが優勝しても、世界全体の人気が上がり続けない）
- 行の無い球団は初期値で補う（この機能より前の世界）
"""

from statistics import mean
from unittest import TestCase

from myapp.domain.exceptions import InvalidPopularity, InvalidSeason
from myapp.domain.pennant.offseason_player import TrackRecord
from myapp.domain.pennant.popularity import (
    STAR_CAP,
    ClubPopularity,
    Popularity,
    PopularityFactors,
    initial_popularity,
    next_popularities,
    star_power,
    with_initial,
)

SEED = 20261114
YEAR = 2026
NEUTRAL = PopularityFactors(winning_percentage=0.5)


def _next(current: dict[int, int], factors: dict[int, PopularityFactors], *, seed: int = SEED) -> dict[int, int]:
    result = next_popularities(
        {team_id: Popularity(value) for team_id, value in current.items()}, factors, world_seed=seed, year=YEAR
    )
    return {team_id: popularity.value for team_id, popularity in result.items()}


class PopularityValueTest(TestCase):
    def test_accepts_one_to_one_hundred(self):
        self.assertEqual(Popularity(1).value, 1)
        self.assertEqual(Popularity(100).value, 100)

    def test_rejects_values_out_of_range_or_not_integers(self):
        for bad in (0, 101, -5, 50.0, True, "50"):
            with self.subTest(value=bad), self.assertRaises(InvalidPopularity):
                Popularity(bad)  # type: ignore[arg-type]

    def test_clamped_rounds_into_the_range(self):
        self.assertEqual(Popularity.clamped(54.6).value, 55)
        self.assertEqual(Popularity.clamped(-3.0).value, 1)
        self.assertEqual(Popularity.clamped(130.2).value, 100)

    def test_initial_popularity_is_fifty_for_every_club(self):
        """2026-11-14 ユーザー決定: 初期値は全球団50。"""
        self.assertEqual(initial_popularity(), Popularity(50))

    def test_a_club_popularity_rejects_an_impossible_year(self):
        with self.assertRaises(InvalidSeason):
            ClubPopularity(team_id=1, year=0, popularity=Popularity(50))

    def test_missing_rows_are_filled_with_the_initial_value(self):
        filled = with_initial([1, 2, 3], {2: Popularity(70)})
        self.assertEqual(filled, {1: Popularity(50), 2: Popularity(70), 3: Popularity(50)})


class NextPopularityTest(TestCase):
    def test_the_same_world_year_and_club_give_the_same_value(self):
        current = {1: 60, 2: 40}
        factors = {1: PopularityFactors(0.55), 2: PopularityFactors(0.45)}
        self.assertEqual(_next(current, factors), _next(current, factors))

    def test_the_noise_differs_between_worlds(self):
        current = dict.fromkeys(range(1, 31), 50)
        factors = dict.fromkeys(current, NEUTRAL)
        self.assertNotEqual(_next(current, factors, seed=1), _next(current, factors, seed=2))

    def test_a_mediocre_season_pulls_popularity_back_toward_fifty(self):
        """平均への戻り: 平凡な成績（勝率 .500・優勝なし）が続けば、高い人気は下がり、低い人気は上がる。"""
        high = [_next({1: 90}, {1: NEUTRAL}, seed=seed)[1] for seed in range(200)]
        low = [_next({1: 10}, {1: NEUTRAL}, seed=seed)[1] for seed in range(200)]
        self.assertLess(mean(high), 90)
        self.assertGreater(mean(low), 10)

    def test_winning_raises_popularity_and_losing_lowers_it(self):
        winners = [_next({1: 50}, {1: PopularityFactors(0.600)}, seed=seed)[1] for seed in range(200)]
        losers = [_next({1: 50}, {1: PopularityFactors(0.400)}, seed=seed)[1] for seed in range(200)]
        self.assertGreater(mean(winners), 52)
        self.assertLess(mean(losers), 48)

    def test_titles_and_stars_add_more_than_the_same_record_without_them(self):
        """リーグ優勝・日本一・スター選手は、同じ勝率でもそれぞれ人気を上げる（他の球団より上に）。"""
        record = 0.580
        variants = {
            1: PopularityFactors(record),
            2: PopularityFactors(record, league_champion=True),
            3: PopularityFactors(record, league_champion=True, japan_champion=True),
            4: PopularityFactors(record, star_power=STAR_CAP),
        }
        totals = dict.fromkeys(variants, 0)
        for seed in range(200):
            result = _next(dict.fromkeys(variants, 50), variants, seed=seed)
            for team_id, value in result.items():
                totals[team_id] += value
        self.assertGreater(totals[2], totals[1])
        self.assertGreater(totals[3], totals[2])
        self.assertGreater(totals[4], totals[1])

    def test_a_champion_season_lifts_popularity_by_several_points(self):
        """優勝（勝率 .600・リーグ優勝・日本一）で数点上がる（計画の目安は +5〜8。帯は緩く取る）。"""
        clubs = dict.fromkeys(range(1, 13), 50)
        factors = {team_id: PopularityFactors(0.5) for team_id in clubs}
        factors[1] = PopularityFactors(0.600, league_champion=True, japan_champion=True)
        gains = [_next(clubs, factors, seed=seed)[1] - 50 for seed in range(200)]
        self.assertTrue(4 <= mean(gains) <= 10, mean(gains))

    def test_the_world_does_not_drift_upward_because_someone_always_wins(self):
        """優勝・日本一・スターの加点は世界の平均を引いて足す。

        全員が平均から始め、勝率 .500 なら、誰かが優勝しても世界の平均は50のまま。
        """
        clubs = dict.fromkeys(range(1, 241), 50)
        factors = {
            team_id: PopularityFactors(
                0.5,
                league_champion=team_id % 6 == 0,
                japan_champion=team_id % 48 == 0,
                star_power=float(team_id % 5),
            )
            for team_id in clubs
        }
        self.assertAlmostEqual(mean(_next(clubs, factors).values()), 50, delta=0.6)

    def test_values_stay_in_range_at_the_extremes(self):
        top = {1: 100, 2: 1}
        factors = {
            1: PopularityFactors(1.0, league_champion=True, japan_champion=True, star_power=STAR_CAP),
            2: PopularityFactors(0.0),
        }
        for seed in range(100):
            result = _next(top, factors, seed=seed)
            self.assertTrue(all(1 <= value <= 100 for value in result.values()), result)

    def test_clubs_without_factors_are_treated_as_neutral(self):
        """材料の無い球団（その年に試合をしていない）は勝率 .500・優勝なし・スターなしとして扱う。"""
        current = {1: 70, 2: 30}
        self.assertEqual(_next(current, {}), _next(current, {1: NEUTRAL, 2: NEUTRAL}))

    def test_no_clubs_give_no_values(self):
        self.assertEqual(next_popularities({}, {}, world_seed=SEED, year=YEAR), {})


class StarPowerTest(TestCase):
    """スター選手（2026-10-12 ユーザー決定: 実績のあるベテラン・生え抜きのスター）。"""

    def test_no_titles_and_no_long_homegrown_tenure_is_zero(self):
        self.assertEqual(star_power([TrackRecord(seasons=15), TrackRecord(homegrown=True, seasons=3)]), 0.0)

    def test_titles_count_up_to_a_limit_per_player(self):
        self.assertGreater(star_power([TrackRecord(titles=2)]), star_power([TrackRecord(titles=1)]))
        self.assertEqual(star_power([TrackRecord(titles=3)]), star_power([TrackRecord(titles=9)]))

    def test_long_serving_homegrown_players_are_stars(self):
        self.assertGreater(star_power([TrackRecord(homegrown=True, seasons=10)]), 0.0)
        self.assertEqual(star_power([TrackRecord(homegrown=False, seasons=10)]), 0.0)

    def test_the_club_total_is_capped(self):
        many = [TrackRecord(homegrown=True, seasons=12, titles=3) for _ in range(20)]
        self.assertEqual(star_power(many), STAR_CAP)
