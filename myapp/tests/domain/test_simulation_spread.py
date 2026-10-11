"""推定した能力の散らばりの調整（`simulation/spread.py`）の単体テスト。Django も DB も使わない。

順位を保つ・平均と標準偏差が目標（`samples.py` の分布）に合う・1〜100 に収まる・成績の少ない選手が
強くなりすぎない、を確かめる。目標の数字はテストに複製せず、`samples.py` の定数を使う。
"""

from statistics import fmean, pstdev
from unittest import TestCase

from myapp.domain.entities import Player
from myapp.domain.pennant.initial_ratings import career_record, estimate_initial_ratings
from myapp.domain.simulation.estimate import CareerRecord, LeagueNorms, estimate_batter_ratings
from myapp.domain.simulation.randomness import make_random, normal
from myapp.domain.simulation.ratings import RATING_MAX, RATING_MIN, BatterRatings, GrowthType, PitcherRatings
from myapp.domain.simulation.samples import BATTER_ITEM_OFFSET, BATTER_MEAN, BATTER_SD, PITCHER_MEAN, PITCHER_SD
from myapp.domain.simulation.spread import (
    MIN_POPULATION,
    spread_batter_ratings,
    spread_pitcher_ratings,
    spread_ratings,
)
from myapp.domain.value_objects import BattingLine, FieldingLine, InningsPitched, JerseyNumber, PitchingLine, Position

BATTER_ITEMS = tuple(BatterRatings.LABELS)
# 推定した能力に近い、狭い散らばり（平均 47・標準偏差 3.5）の野手・投手
NARROW_MEAN, NARROW_SD = 47.0, 3.5


def _narrow(rng) -> int:
    return max(1, min(100, round(NARROW_MEAN + NARROW_SD * normal(rng))))


def narrow_batters(count: int, seed: int = 1) -> list[BatterRatings]:
    rng = make_random(seed)
    return [
        BatterRatings(
            contact=_narrow(rng), power=_narrow(rng), eye=_narrow(rng), speed=_narrow(rng), fielding=_narrow(rng)
        )
        for _ in range(count)
    ]


def narrow_pitchers(count: int, seed: int = 2) -> list[PitcherRatings]:
    rng = make_random(seed)
    return [
        PitcherRatings(
            stuff=_narrow(rng),
            control=_narrow(rng),
            home_run_avoidance=_narrow(rng),
            stamina=40 + index % 25,
        )
        for index in range(count)
    ]


class SpreadBatterRatingsTest(TestCase):
    def setUp(self):
        self.before = narrow_batters(400)
        self.after = spread_batter_ratings(self.before)

    def test_the_mean_and_the_standard_deviation_match_the_target_item_by_item(self):
        for name in BATTER_ITEMS:
            values = [getattr(item, name) for item in self.after]
            with self.subTest(item=name):
                self.assertAlmostEqual(fmean(values), BATTER_MEAN + BATTER_ITEM_OFFSET.get(name, 0.0), delta=0.6)
                self.assertAlmostEqual(pstdev(values), BATTER_SD, delta=0.6)

    def test_the_order_of_players_is_kept_item_by_item(self):
        for name in BATTER_ITEMS:
            pairs = sorted(zip(self.before, self.after, strict=True), key=lambda pair: getattr(pair[0], name))
            mapped = [getattr(after, name) for _, after in pairs]
            with self.subTest(item=name):
                self.assertEqual(mapped, sorted(mapped), "推定値が高い選手は、調整後も低くならない")

    def test_a_player_below_the_mean_stays_below_the_mean(self):
        """平均より下に寄せた選手（成績の少ない選手）は、散らばりを広げても平均より上には出ない。"""
        for name in BATTER_ITEMS:
            raw_mean = fmean(getattr(item, name) for item in self.before)
            target = BATTER_MEAN + BATTER_ITEM_OFFSET.get(name, 0.0)
            for before, after in zip(self.before, self.after, strict=True):
                if getattr(before, name) < raw_mean:
                    self.assertLessEqual(getattr(after, name), target + 1, name)

    def test_every_value_is_within_the_rating_range_even_with_outliers(self):
        outliers = [*self.before, BatterRatings(100, 100, 100, 100, 100), BatterRatings(1, 1, 1, 1, 1)]

        for item in spread_batter_ratings(outliers):
            for name in BATTER_ITEMS:
                self.assertTrue(RATING_MIN <= getattr(item, name) <= RATING_MAX)

    def test_the_growth_type_is_not_touched(self):
        late = [BatterRatings(growth=GrowthType.LATE) if i % 2 else item for i, item in enumerate(self.before)]

        spread = spread_batter_ratings(late)

        self.assertEqual([item.growth for item in spread], [item.growth for item in late])

    def test_a_population_too_small_to_have_a_standard_deviation_is_left_alone(self):
        small = narrow_batters(MIN_POPULATION - 1)

        self.assertEqual(spread_batter_ratings(small), small)

    def test_a_population_without_any_difference_is_left_alone(self):
        same = [BatterRatings(50, 50, 50, 50, 50)] * MIN_POPULATION

        self.assertEqual(spread_batter_ratings(same), same)

    def test_it_returns_a_result_for_every_input_in_order(self):
        self.assertEqual(len(self.after), len(self.before))
        self.assertEqual(spread_batter_ratings([]), [])


class SpreadPitcherRatingsTest(TestCase):
    def setUp(self):
        self.before = narrow_pitchers(300)
        self.after = spread_pitcher_ratings(self.before)

    def test_the_mean_and_the_standard_deviation_match_the_target_item_by_item(self):
        for name in ("stuff", "control", "home_run_avoidance"):
            values = [getattr(item, name) for item in self.after]
            with self.subTest(item=name):
                self.assertAlmostEqual(fmean(values), PITCHER_MEAN, delta=0.6)
                self.assertAlmostEqual(pstdev(values), PITCHER_SD, delta=0.6)

    def test_the_order_is_kept(self):
        pairs = sorted(zip(self.before, self.after, strict=True), key=lambda pair: pair[0].stuff)
        mapped = [after.stuff for _, after in pairs]

        self.assertEqual(mapped, sorted(mapped))

    def test_stamina_is_left_as_estimated(self):
        """スタミナは先発 / 救援の2つに分かれる値で、成績から決まった先発の割合を映している。"""
        self.assertEqual([item.stamina for item in self.after], [item.stamina for item in self.before])


class SpreadMixedRatingsTest(TestCase):
    def test_batters_and_pitchers_are_spread_separately_and_keep_their_places(self):
        batters, pitchers = narrow_batters(100), narrow_pitchers(100)
        mixed: list[BatterRatings | PitcherRatings] = []
        for batter, pitcher in zip(batters, pitchers, strict=True):
            mixed.extend([batter, pitcher])

        spread = spread_ratings(mixed)

        self.assertEqual([type(item) for item in spread], [type(item) for item in mixed])
        self.assertEqual(spread[0::2], spread_batter_ratings(batters))
        self.assertEqual(spread[1::2], spread_pitcher_ratings(pitchers))


def _regular(player_id: int, index: int) -> CareerRecord:
    """規定打席に近い野手。本塁打・三振・四球が選手ごとに違う。"""
    return CareerRecord(
        player_id,
        Position.OUTFIELDER,
        batting=BattingLine(
            at_bats=500,
            singles=100 + index % 25,
            doubles=20 + index % 9,
            home_runs=5 + index % 30,
            walks=30 + index % 40,
            strikeouts=70 + index % 60,
        ),
    )


class EstimatedPopulationIsSpreadTest(TestCase):
    """推定の入口（`estimate_initial_ratings`）が、母集団を見渡して散らばりを合わせる。"""

    def setUp(self):
        regulars = [_regular(i, i) for i in range(1, 201)]
        bench = [CareerRecord(1000 + i, Position.OUTFIELDER) for i in range(100)]
        self.records = [*regulars, *bench]
        self.result = estimate_initial_ratings(self.records, seed=1, year=2027)
        self.regulars = [item.ratings for item in self.result[:200]]
        self.bench = [item.ratings for item in self.result[200:]]

    def test_the_standard_deviation_reaches_the_target(self):
        everyone = [item.ratings for item in self.result]
        for name in ("contact", "power", "eye"):
            with self.subTest(item=name):
                self.assertAlmostEqual(pstdev(getattr(r, name) for r in everyone), BATTER_SD, delta=0.8)

    def test_players_without_a_record_do_not_become_strong(self):
        """出場の無い選手は、推定で平均より低いところに寄せてある。散らばりを広げても、主力の平均より上にならない。"""
        for name in ("contact", "power", "eye"):
            regular_mean = fmean(getattr(r, name) for r in self.regulars)
            with self.subTest(item=name):
                self.assertLess(max(getattr(r, name) for r in self.bench), regular_mean)

    def test_the_order_of_the_raw_estimate_is_kept(self):
        records = [
            *(_regular(i, i) for i in range(1, 201)),
            *(CareerRecord(1000 + i, Position.OUTFIELDER) for i in range(100)),
        ]
        norms = LeagueNorms.of(records)
        raw = [estimate_batter_ratings(r.batting, r.fielding, r.position, norms) for r in records]
        everyone = [item.ratings for item in self.result]

        for name in ("contact", "power", "eye"):
            pairs = sorted(zip(raw, everyone, strict=True), key=lambda pair: getattr(pair[0], name))
            mapped = [getattr(after, name) for _, after in pairs]
            with self.subTest(item=name):
                self.assertEqual(mapped, sorted(mapped))


class CareerRecordTest(TestCase):
    def _player(self, **kwargs) -> Player:
        defaults = {"name": "選手", "number": JerseyNumber("7"), "position": Position.INFIELDER, "id": 41}
        return Player(**{**defaults, **kwargs})

    def test_the_record_is_made_from_the_source_player(self):
        player = self._player(batting=BattingLine(at_bats=100, singles=30))
        fielding = {41: FieldingLine(putouts=10, assists=20, errors=2)}

        record = career_record(player, fielding, year=2030)

        self.assertEqual((record.player_id, record.seed_player_id, record.position), (41, 41, Position.INFIELDER))
        self.assertEqual(record.batting, player.batting)
        self.assertEqual(record.fielding, fielding[41])

    def test_the_rating_can_be_attached_to_another_player_while_the_seed_stays_with_the_source(self):
        record = career_record(self._player(), {}, year=2030, player_id=900)

        self.assertEqual((record.player_id, record.seed_player_id), (900, 41))

    def test_no_fielding_means_an_empty_line(self):
        self.assertEqual(career_record(self._player(), {}, year=2030).fielding, FieldingLine())

    def test_a_pitcher_carries_the_pitching_line(self):
        line = PitchingLine(innings=InningsPitched(outs=30), strikeouts=12)
        record = career_record(self._player(position=Position.PITCHER, pitching=line), {}, year=2030)

        self.assertEqual(record.pitching, line)
