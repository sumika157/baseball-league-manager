"""伸びしろ（#308 P1）。値・事前の分布・成長への効き方・割り当て（分岐した選手・候補・外国人・既存の世界）。"""

from __future__ import annotations

import random
import unittest
from dataclasses import replace
from statistics import fmean, pstdev

from myapp.domain.exceptions import InvalidRatings
from myapp.domain.pennant import aging
from myapp.domain.pennant.aging import BATTER_CURVES, PITCHER_CURVES, age_ratings, expected_change
from myapp.domain.pennant.draft import draw_rookie
from myapp.domain.pennant.draft_meeting import POOL_POTENTIAL_MEAN, draw_pool, scouting_report
from myapp.domain.pennant.initial_ratings import estimate_initial_ratings
from myapp.domain.pennant.outlook import with_filled_potential
from myapp.domain.simulation.estimate import POTENTIAL_RANGE, CareerRecord, draw_potential
from myapp.domain.simulation.ratings import (
    BatterRatings,
    GrowthType,
    PitcherRatings,
    Potential,
    potential_multiplier,
)
from myapp.domain.value_objects import Position
from myapp.tests.domain.test_draft_meeting import club, run
from myapp.tests.domain.test_offseason import YEAR, plan, player
from myapp.tests.domain.test_offseason import club as offseason_club


class CountingRandom:
    """`random()` しか持たず、呼ばれた回数を数える乱数源。"""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self._rng.random()


class PotentialValueTest(unittest.TestCase):
    def test_range_is_one_to_hundred(self) -> None:
        for bad in (0, 101, 50.0, True):
            with self.subTest(value=bad), self.assertRaises(InvalidRatings):
                Potential(bad)  # type: ignore[arg-type]  # 検査に通らない値を渡すのが目的
        self.assertEqual(Potential(1).value, 1)
        self.assertEqual(Potential(100).value, 100)

    def test_fifty_is_the_average_multiplier(self) -> None:
        self.assertEqual(Potential(50).multiplier, 1.0)
        self.assertEqual(Potential(100).multiplier, 2.0)
        self.assertEqual(Potential(25).multiplier, 0.5)
        multipliers = [Potential(value).multiplier for value in range(1, 101)]
        self.assertEqual(multipliers, sorted(multipliers))
        self.assertEqual(len(set(multipliers)), 100)

    def test_a_bare_integer_is_refused(self) -> None:
        """素の整数を渡すと、締めるとき（倍率を読むとき）まで気づけないので、組み立てで止める。"""
        with self.assertRaises(InvalidRatings):
            BatterRatings(potential=77)  # type: ignore[arg-type]  # 検査に通らない値を渡すのが目的
        with self.assertRaises(InvalidRatings):
            PitcherRatings(potential=77)  # type: ignore[arg-type]  # 同上

    def test_ratings_without_potential_count_as_average(self) -> None:
        self.assertEqual(potential_multiplier(None), 1.0)
        self.assertEqual(potential_multiplier(Potential(70)), 1.4)
        self.assertIsNone(BatterRatings().potential)
        self.assertIsNone(PitcherRatings().potential)


class DrawPotentialTest(unittest.TestCase):
    def test_follows_the_prior_and_uses_random_twice(self) -> None:
        rng = CountingRandom(5)
        values = [draw_potential(rng).value for _ in range(4000)]
        self.assertEqual(rng.calls, 8000)
        low, high = POTENTIAL_RANGE
        self.assertTrue(all(low <= value <= high for value in values))
        self.assertAlmostEqual(fmean(values), 50, delta=1.0)
        self.assertAlmostEqual(pstdev(values), 15, delta=1.0)
        self.assertAlmostEqual(fmean(value / 50 for value in values), 1.0, delta=0.02)


class GrowthTest(unittest.TestCase):
    """伸びしろは伸び（正の変化）にだけ掛かる。無い（None）と普通（50）は #308 より前と1点も変わらない。"""

    def test_none_and_average_change_nothing(self) -> None:
        for curves in (BATTER_CURVES, PITCHER_CURVES):
            for item in curves.values():
                for age in range(16, 45):
                    for growth in GrowthType:
                        for value in (30, 50, 80):
                            before = expected_change(item, age=age, growth=growth, value=value)
                            self.assertEqual(
                                before, expected_change(item, age=age, growth=growth, value=value, potential=None)
                            )
                            self.assertEqual(
                                before,
                                expected_change(item, age=age, growth=growth, value=value, potential=Potential(50)),
                            )

    def test_aging_with_none_and_average_gives_the_same_ratings(self) -> None:
        for base in (BatterRatings(40, 45, 50, 55, 60, GrowthType.EARLY), PitcherRatings(40, 50, 60, 45)):
            for age in (18, 22, 27, 33, 38):
                for seed in range(20):
                    plain = age_ratings(base, age=age, rng=random.Random(seed))
                    average = age_ratings(replace(base, potential=Potential(50)), age=age, rng=random.Random(seed))
                    self.assertEqual(replace(plain, potential=Potential(50)), average)

    def test_scales_only_the_growth(self) -> None:
        item = BATTER_CURVES["contact"]
        young = expected_change(item, age=20, growth=GrowthType.NORMAL, value=50)
        self.assertGreater(young, 0)
        self.assertAlmostEqual(
            expected_change(item, age=20, growth=GrowthType.NORMAL, value=50, potential=Potential(80)), young * 1.6
        )
        self.assertAlmostEqual(
            expected_change(item, age=20, growth=GrowthType.NORMAL, value=50, potential=Potential(20)), young * 0.4
        )
        for age in (30, 35, 40):
            with self.subTest(age=age):
                plain = expected_change(item, age=age, growth=GrowthType.NORMAL, value=50)
                self.assertLess(plain, 0)
                for value in (10, 90):
                    self.assertEqual(
                        expected_change(item, age=age, growth=GrowthType.NORMAL, value=50, potential=Potential(value)),
                        plain,
                    )

    def test_high_ratings_still_grow_at_half(self) -> None:
        item = BATTER_CURVES["power"]
        big = Potential(90)
        self.assertAlmostEqual(
            expected_change(item, age=21, growth=GrowthType.LATE, value=aging.HIGH_RATING, potential=big),
            expected_change(item, age=21, growth=GrowthType.LATE, value=50, potential=big) * aging.HIGH_RATING_GAIN,
        )

    def test_a_younger_entrant_spreads_more_by_potential(self) -> None:
        """27歳までの伸びの差（伸びしろ 65 と 35）は、高校（18歳）> 大学（22歳）> 社会人（24歳）。"""

        def growth_to_27(age: int, potential: Potential) -> float:
            item = BATTER_CURVES["contact"]
            return sum(
                expected_change(item, age=year, growth=GrowthType.NORMAL, value=40, potential=potential)
                for year in range(age, 27)
            )

        gaps = [growth_to_27(age, Potential(65)) - growth_to_27(age, Potential(35)) for age in (18, 22, 24)]
        self.assertEqual(gaps, sorted(gaps, reverse=True))
        self.assertGreater(gaps[2], 0)


class ForkedPlayersTest(unittest.TestCase):
    """世界の作成: 分岐した選手は事前の分布から引く（用途 `potential-<分岐元 id>`）。"""

    records = [CareerRecord(player_id, Position.OUTFIELDER) for player_id in range(1, 120)]

    def test_every_player_gets_a_potential_from_the_seed(self) -> None:
        first = estimate_initial_ratings(self.records, seed=1, year=YEAR)
        self.assertTrue(all(item.ratings.potential is not None for item in first))
        self.assertEqual(first, estimate_initial_ratings(self.records, seed=1, year=YEAR))
        values = [item.ratings.potential.value for item in first if item.ratings.potential]
        self.assertGreater(len(set(values)), 10)
        other = estimate_initial_ratings(self.records, seed=2, year=YEAR)
        self.assertNotEqual([item.ratings.potential for item in first], [item.ratings.potential for item in other])

    def test_a_player_keeps_the_potential_when_others_are_added(self) -> None:
        few = estimate_initial_ratings(self.records[:5], seed=3, year=YEAR)
        many = estimate_initial_ratings(self.records, seed=3, year=YEAR)
        self.assertEqual([item.ratings.potential for item in few], [item.ratings.potential for item in many[:5]])


class RookiePotentialTest(unittest.TestCase):
    """新人: 候補は `draft-potential-<番号>`（`draw_rookie` の乱数の流れの外）。外国人の獲得の候補は
    候補ごとの別の流れ（`test_foreign_market.py`）。"""

    def test_draw_rookie_itself_does_not_draw_a_potential(self) -> None:
        rookie = draw_rookie(random.Random(1), Position.PITCHER, year=YEAR, used_names=set())
        self.assertIsNone(rookie.ratings.potential)

    def test_every_prospect_has_a_potential_and_the_pool_is_rebuilt_the_same(self) -> None:
        planned = {Position.PITCHER: 10, Position.INFIELDER: 6}
        pool = draw_pool(planned, world_seed=4, year=YEAR, used_names=set())
        values = [prospect.rookie.ratings.potential for prospect in pool.prospects]
        self.assertTrue(all(value is not None for value in values))
        self.assertGreater(len(set(values)), 10)
        again = draw_pool(planned, world_seed=4, year=YEAR, used_names=set())
        self.assertEqual(values, [prospect.rookie.ratings.potential for prospect in again.prospects])

    def test_the_scouting_report_shows_only_an_estimate_of_the_potential(self) -> None:
        """#308 P2: 球団は伸びしろを推定（`ScoutingReport.potential`）でだけ見る。推定の能力に本当の値が漏れない。"""
        pool = draw_pool({Position.PITCHER: 3, Position.INFIELDER: 3}, world_seed=4, year=YEAR, used_names=set())
        for prospect in pool.prospects:
            report = scouting_report(prospect, 1, world_seed=4, year=YEAR)
            self.assertIsNone(report.ratings.potential)
            self.assertIsInstance(report.potential, Potential)

    def test_prospects_draw_the_potential_around_the_pool_mean_of_their_route(self) -> None:
        """候補は経路ごとに合わせ込んだ平均（`POOL_POTENTIAL_MEAN`。
        AI が伸びしろを見て上から取るぶん、普通の50より低い。
        #308 P3 段階0）。"""
        pool = draw_pool({Position.PITCHER: 300, Position.INFIELDER: 300}, world_seed=4, year=YEAR, used_names=set())
        self.assertTrue(all(prospect.rookie.ratings.potential is not None for prospect in pool.prospects))
        for path, mean in POOL_POTENTIAL_MEAN.items():
            values = [
                potential.value
                for prospect in pool.prospects
                if prospect.route.value == path.value and (potential := prospect.rookie.ratings.potential)
            ]
            with self.subTest(path=path):
                self.assertGreater(len(values), 100)
                self.assertAlmostEqual(fmean(values), mean, delta=2.0)
                self.assertLess(mean, 50)

    def test_every_draftee_has_a_potential(self) -> None:
        result = run([club(team_id) for team_id in range(1, 5)])
        self.assertTrue(result.draftees)
        self.assertTrue(all(draftee.ratings.potential is not None for draftee in result.draftees))
        self.assertEqual(result.draftees, run([club(team_id) for team_id in range(1, 5)]).draftees)


class FillPotentialTest(unittest.TestCase):
    """既存の世界（#308 より前）: 締めるときに伸びしろの無い選手へ引き、翌年の行に書く。締める年の行は変えない。"""

    def test_fills_only_a_missing_potential_with_a_year_independent_draw(self) -> None:
        missing = BatterRatings()
        filled = with_filled_potential(missing, player_id=7, world_seed=1)
        self.assertIsNotNone(filled.potential)
        self.assertEqual(filled, with_filled_potential(missing, player_id=7, world_seed=1))
        self.assertEqual(replace(filled, potential=None), missing, "伸びしろのほかは変えない")
        kept = BatterRatings(potential=Potential(12))
        self.assertIs(with_filled_potential(kept, player_id=7, world_seed=1), kept)

    def test_closing_writes_the_potential_to_the_next_year_only(self) -> None:
        players = [player(player_id) for player_id in range(1, 41)]
        holding = player(41)
        assert holding.ratings is not None
        holding = replace(
            holding,
            ratings=replace(holding.ratings, ratings=replace(holding.ratings.ratings, potential=Potential(77))),
        )
        result = plan([offseason_club(*players, holding)])
        self.assertTrue(result.retained)
        for change in result.retained:
            with self.subTest(player=change.player_id):
                self.assertIsNotNone(change.after.ratings.potential)
                if change.player_id == holding.player_id:
                    self.assertEqual(change.before.ratings.potential, Potential(77))
                    self.assertEqual(change.after.ratings.potential, Potential(77))
                else:
                    self.assertIsNone(change.before.ratings.potential, "締める年の行は書き換えない")
                    self.assertEqual(
                        change.after.ratings.potential,
                        with_filled_potential(
                            change.before.ratings, player_id=change.player_id, world_seed=1
                        ).potential,
                    )
        self.assertTrue(all(draftee.ratings.potential is not None for draftee in result.draftees))


if __name__ == "__main__":
    unittest.main()
