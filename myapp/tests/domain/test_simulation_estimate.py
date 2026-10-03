"""実成績からの能力の推定（`simulation/estimate.py`）の単体テスト。Django も DB も使わない。

推定は `odds.py` の「能力 → 個人の率」の逆写像。能力 r の選手が出すはずの率の成績を大量に作り、
そこから r が戻ってくること（逆写像であること）と、収縮・出場の少ない選手の扱いを確かめる。
"""

import math
from unittest import TestCase

from myapp.domain.simulation import estimate as estimate_module
from myapp.domain.simulation.baseline import NPB
from myapp.domain.simulation.estimate import (
    GROWTH_PRIOR,
    REPLACEMENT_GAP,
    STARTER_STAMINA,
    UNKNOWN_STAMINA,
    CareerRecord,
    LeagueNorms,
    draw_growth_type,
    estimate_batter_ratings,
    estimate_pitcher_ratings,
)
from myapp.domain.simulation.odds import DEFAULT_SENSITIVITY as S
from myapp.domain.simulation.odds import scaled_rate
from myapp.domain.simulation.randomness import make_random
from myapp.domain.simulation.ratings import RATING_MAX, RATING_MIN, GrowthType
from myapp.domain.value_objects import BattingLine, FieldingLine, InningsPitched, PitchingLine, Position

BIG = 200_000  # 収縮の影響が無視できるほど大きい標本
NO_NORMS = LeagueNorms()


def batter_line(*, contact=50.0, power=50.0, eye=50.0, speed=50.0, plate_appearances=BIG) -> BattingLine:
    """能力がそれぞれ contact・power・eye・speed の打者が、平均の投手と当たって出す成績。"""
    b = NPB
    hit_by_pitch = round(plate_appearances * b.hit_by_pitch)
    walks = round((plate_appearances - hit_by_pitch) * scaled_rate(b.walk_given_not_hit_by_pitch, S.eye_walk, eye))
    sacrifices = round(plate_appearances * b.sacrifice)
    at_bats = plate_appearances - hit_by_pitch - walks - sacrifices
    strikeouts = round(at_bats * scaled_rate(b.strikeout_given_at_bat, S.contact_strikeout, contact))
    home_runs = round((at_bats - strikeouts) * scaled_rate(b.home_run_given_not_strikeout, S.power_home_run, power))
    in_play = at_bats - strikeouts - home_runs
    non_home_run_hits = round(in_play * scaled_rate(b.babip, S.contact_in_play_hit, contact))
    doubles = round(non_home_run_hits * scaled_rate(b.double_share, S.power_double, power))
    triples = round((non_home_run_hits - doubles) * scaled_rate(b.triple_given_not_double, S.speed_triple, speed))
    return BattingLine(
        at_bats=at_bats,
        singles=non_home_run_hits - doubles - triples,
        doubles=doubles,
        triples=triples,
        home_runs=home_runs,
        walks=walks,
        hit_by_pitch=hit_by_pitch,
        sacrifice_flies=sacrifices,
        strikeouts=strikeouts,
    )


def pitcher_line(*, stuff=50.0, control=50.0, avoidance=50.0, faced=BIG, starts=0) -> PitchingLine:
    """能力がそれぞれ stuff・control・avoidance の投手が、平均の打者と当たって出す成績。"""
    b = NPB
    hit_by_pitch = round(faced * b.hit_by_pitch)
    walks = round((faced - hit_by_pitch) * scaled_rate(b.walk_given_not_hit_by_pitch, S.control_walk, control))
    at_bats = faced - hit_by_pitch - walks - round(b.sacrifice * faced)
    strikeouts = round(at_bats * scaled_rate(b.strikeout_given_at_bat, S.stuff_strikeout, stuff))
    home_runs = round(
        (at_bats - strikeouts) * scaled_rate(b.home_run_given_not_strikeout, S.avoidance_home_run, avoidance)
    )
    hits = home_runs + round((at_bats - strikeouts - home_runs) * b.babip)
    outs = faced - hits - walks - hit_by_pitch
    return PitchingLine(
        innings=InningsPitched(outs=outs),
        strikeouts=strikeouts,
        hits_allowed=hits,
        walks_allowed=walks,
        home_runs_allowed=home_runs,
        hit_by_pitch_allowed=hit_by_pitch,
        starts=starts,
    )


def estimate_batter(line, *, position=Position.OUTFIELDER, fielding=None, norms=NO_NORMS):
    return estimate_batter_ratings(line, fielding or FieldingLine(), position, norms)


class BatterInverseTest(TestCase):
    """能力 r の選手の成績から、r が戻ってくる。"""

    def test_an_average_line_is_average(self):
        ratings = estimate_batter(batter_line())

        self.assertEqual((ratings.contact, ratings.power, ratings.eye, ratings.speed), (50, 50, 50, 50))

    def test_contact_is_recovered_from_strikeouts_and_balls_in_play(self):
        for rating in (30, 42, 58, 70, 85):
            with self.subTest(rating=rating):
                self.assertAlmostEqual(estimate_batter(batter_line(contact=rating)).contact, rating, delta=1)

    def test_power_is_recovered_from_home_runs_and_doubles(self):
        for rating in (30, 42, 58, 70, 85):
            with self.subTest(rating=rating):
                self.assertAlmostEqual(estimate_batter(batter_line(power=rating)).power, rating, delta=1)

    def test_eye_is_recovered_from_walks(self):
        for rating in (30, 42, 58, 70, 85):
            with self.subTest(rating=rating):
                self.assertAlmostEqual(estimate_batter(batter_line(eye=rating)).eye, rating, delta=1)

    def test_speed_is_recovered_from_triples(self):
        for rating in (35, 65):
            with self.subTest(rating=rating):
                # 三塁打は起きにくい。標本が大きくても数が少ないので、許容を広げる
                self.assertAlmostEqual(estimate_batter(batter_line(speed=rating)).speed, rating, delta=4)

    def test_one_item_does_not_move_the_others(self):
        ratings = estimate_batter(batter_line(power=75))

        self.assertEqual((ratings.contact, ratings.eye), (50, 50))

    def test_more_home_runs_means_more_power(self):
        weaker = BattingLine(at_bats=500, singles=100, home_runs=10)
        stronger = BattingLine(at_bats=500, singles=100, home_runs=30)

        self.assertGreater(estimate_batter(stronger).power, estimate_batter(weaker).power)

    def test_more_strikeouts_means_less_contact(self):
        fewer = BattingLine(at_bats=500, singles=120, strikeouts=60)
        more = BattingLine(at_bats=500, singles=120, strikeouts=140)

        self.assertLess(estimate_batter(more).contact, estimate_batter(fewer).contact)

    def test_the_result_stays_within_the_rating_range(self):
        extremes = [
            BattingLine(at_bats=BIG, home_runs=BIG // 2, walks=BIG // 2, singles=BIG // 2),
            BattingLine(at_bats=BIG, strikeouts=BIG),
            BattingLine(at_bats=1, strikeouts=1),
        ]
        for line in extremes:
            with self.subTest(line=line):
                ratings = estimate_batter(line)
                for name in ("contact", "power", "eye", "speed", "fielding"):
                    self.assertTrue(RATING_MIN <= getattr(ratings, name) <= RATING_MAX, name)


class BatterShrinkageTest(TestCase):
    def test_a_small_sample_is_pulled_toward_the_average(self):
        """同じ率でも、少ない打席は偶然の振れかもしれないので平均へ寄る。"""
        big = estimate_batter(batter_line(power=75, plate_appearances=BIG))
        small = estimate_batter(batter_line(power=75, plate_appearances=500))

        self.assertGreater(big.power, small.power)
        self.assertGreater(small.power, 50)

    def test_the_stability_point_is_where_the_sample_and_the_prior_weigh_the_same(self):
        """安定する機会数 k で、実際の率と寄せ先の率が半々になる（設計書 3.2 の定義）。"""
        k = 17000.0  # 丸めの影響が出ないように大きくしてある（k の値そのものは関係ない）
        observed = 2 * NPB.home_run_given_not_strikeout  # 基準値の2倍の率で、ちょうど k 回の機会

        evidence = estimate_module._evidence(
            round(observed * k), round(k), NPB.home_run_given_not_strikeout, S.power_home_run, k
        )

        halfway = 1.5 * NPB.home_run_given_not_strikeout  # 基準値と実際の率の中間
        expected = 15 / S.power_home_run * math.log(halfway / NPB.home_run_given_not_strikeout)
        self.assertAlmostEqual(evidence.offset, expected, delta=0.05)

    def test_nobody_in_the_record_is_below_average_by_the_replacement_gap(self):
        """成績の無い選手は、1軍の平均（50）より低い（2軍の控えが1軍の平均並みに見えないように）。"""
        ratings = estimate_batter(BattingLine())

        expected = 50 - REPLACEMENT_GAP
        self.assertEqual((ratings.contact, ratings.power, ratings.eye, ratings.speed), (expected,) * 4)

    def test_playing_time_moves_the_prior_gradually(self):
        none = estimate_batter(BattingLine()).eye
        half = estimate_batter(BattingLine(at_bats=170, singles=45, strikeouts=36, walks=14)).eye
        regular = estimate_batter(BattingLine(at_bats=420, singles=110, strikeouts=90, walks=35)).eye

        self.assertLess(none, half)
        self.assertLessEqual(half, regular + 1)


class FieldingEstimateTest(TestCase):
    def setUp(self):
        norms_line = CareerRecord(
            player_id=1,
            position=Position.INFIELDER,
            fielding=FieldingLine(putouts=3000, assists=5000, errors=160),
        )
        self.norms = LeagueNorms.of([norms_line])
        self.regular = BattingLine(at_bats=500, singles=130)

    def _fielding(self, errors, chances=1000, position=Position.INFIELDER):
        line = FieldingLine(putouts=chances - errors, errors=errors)
        return estimate_batter(self.regular, position=position, fielding=line, norms=self.norms).fielding

    def test_fewer_errors_than_the_league_means_better_fielding(self):
        self.assertGreater(self._fielding(errors=5), self._fielding(errors=20))

    def test_the_norm_is_the_rate_of_the_same_position_class(self):
        """守備位置の区分ごとに失策率が違うので、目印は区分ごと。目印の無い区分は事前の平均のまま。"""
        outfield = self._fielding(errors=5, position=Position.OUTFIELDER)
        outfield_without_record = estimate_batter(self.regular, position=Position.OUTFIELDER).fielding

        self.assertEqual(outfield, outfield_without_record)

    def test_catchers_start_higher_and_designated_hitters_lower(self):
        by_position = {
            position: estimate_batter(self.regular, position=position).fielding
            for position in (Position.CATCHER, Position.INFIELDER, Position.OUTFIELDER, Position.DESIGNATED_HITTER)
        }

        self.assertGreater(by_position[Position.CATCHER], by_position[Position.INFIELDER])
        self.assertGreater(by_position[Position.INFIELDER], by_position[Position.OUTFIELDER])
        self.assertGreater(by_position[Position.OUTFIELDER], by_position[Position.DESIGNATED_HITTER])

    def test_few_chances_barely_move_the_estimate(self):
        few = self._fielding(errors=0, chances=20)
        none = estimate_batter(self.regular, position=Position.INFIELDER, norms=self.norms).fielding

        self.assertAlmostEqual(few, none, delta=2)


class SpeedEstimateTest(TestCase):
    def setUp(self):
        runners = [
            CareerRecord(
                player_id=i,
                position=Position.OUTFIELDER,
                batting=BattingLine(
                    at_bats=450, singles=100, walks=40, hit_by_pitch=4, stolen_bases=8 + i % 3, caught_stealing=3
                ),
            )
            for i in range(30)
        ]
        self.norms = LeagueNorms.of(runners)

    def _speed(self, stolen_bases, caught_stealing):
        line = BattingLine(
            at_bats=450,
            singles=100,
            walks=40,
            hit_by_pitch=4,
            stolen_bases=stolen_bases,
            caught_stealing=caught_stealing,
        )
        return estimate_batter(line, norms=self.norms).speed

    def test_the_norm_is_taken_from_the_league(self):
        self.assertGreater(self.norms.steal_attempts_per_reach, 0.0)
        self.assertTrue(0.0 < self.norms.steal_success < 1.0)

    def test_a_base_stealer_is_faster_than_a_runner_who_stays_put(self):
        self.assertGreater(self._speed(40, 8), 50)
        self.assertLess(self._speed(0, 0), 50)
        self.assertGreater(self._speed(40, 8), self._speed(0, 0))

    def test_the_same_attempts_with_more_success_mean_more_speed(self):
        self.assertGreater(self._speed(30, 4), self._speed(15, 19))

    def test_without_a_league_norm_steals_say_nothing(self):
        line = BattingLine(at_bats=450, singles=100, walks=40, stolen_bases=60, caught_stealing=5)

        self.assertEqual(
            estimate_batter(line, norms=NO_NORMS).speed,
            estimate_batter(BattingLine(at_bats=450, singles=100, walks=40)).speed,
        )


class LeagueNormsTest(TestCase):
    def test_too_little_data_gives_no_norm(self):
        records = [CareerRecord(1, Position.OUTFIELDER, batting=BattingLine(at_bats=400, stolen_bases=3))]

        norms = LeagueNorms.of(records)

        self.assertEqual(norms.steal_attempts_per_reach, 0.0)
        self.assertEqual(dict(norms.error_rate), {})

    def test_errors_are_counted_by_position_class_and_pitchers_are_left_out(self):
        records = [
            CareerRecord(1, Position.INFIELDER, fielding=FieldingLine(putouts=500, errors=20)),
            CareerRecord(2, Position.INFIELDER, fielding=FieldingLine(putouts=480, errors=10)),
            CareerRecord(3, Position.PITCHER, fielding=FieldingLine(putouts=5000, errors=1000)),
        ]

        norms = LeagueNorms.of(records)

        self.assertEqual(set(norms.error_rate), {Position.INFIELDER})
        self.assertAlmostEqual(norms.error_rate[Position.INFIELDER], 30 / 1010)


class PitcherInverseTest(TestCase):
    def test_an_average_line_is_average(self):
        ratings = estimate_pitcher_ratings(pitcher_line())

        self.assertEqual((ratings.stuff, ratings.control, ratings.home_run_avoidance), (50, 50, 50))

    def test_each_item_is_recovered(self):
        for rating in (30, 42, 58, 70, 85):
            with self.subTest(rating=rating):
                self.assertAlmostEqual(estimate_pitcher_ratings(pitcher_line(stuff=rating)).stuff, rating, delta=1)
                self.assertAlmostEqual(estimate_pitcher_ratings(pitcher_line(control=rating)).control, rating, delta=1)
                self.assertAlmostEqual(
                    estimate_pitcher_ratings(pitcher_line(avoidance=rating)).home_run_avoidance, rating, delta=1
                )

    def test_a_small_sample_is_pulled_toward_the_average(self):
        big = estimate_pitcher_ratings(pitcher_line(stuff=75))
        small = estimate_pitcher_ratings(pitcher_line(stuff=75, faced=700))

        self.assertGreater(big.stuff, small.stuff)
        self.assertGreater(small.stuff, 50)

    def test_home_run_avoidance_is_the_slowest_to_settle(self):
        """被本塁打は運の影響が大きく、同じ標本でも球威や制球より平均へ強く寄る。"""
        line = pitcher_line(stuff=75, control=25, avoidance=75, faced=1500)
        ratings = estimate_pitcher_ratings(line)

        self.assertLess(abs(ratings.home_run_avoidance - 50), abs(ratings.stuff - 50))

    def test_no_record_is_below_average_by_the_replacement_gap(self):
        ratings = estimate_pitcher_ratings(PitchingLine())

        expected = 50 - REPLACEMENT_GAP
        self.assertEqual((ratings.stuff, ratings.control, ratings.home_run_avoidance), (expected,) * 3)

    def test_the_result_stays_within_the_rating_range(self):
        for line in (
            PitchingLine(innings=InningsPitched(outs=300), strikeouts=300, hits_allowed=0),
            PitchingLine(innings=InningsPitched(outs=3), walks_allowed=100, hits_allowed=100, home_runs_allowed=100),
        ):
            with self.subTest(line=line):
                ratings = estimate_pitcher_ratings(line)
                for name in ("stuff", "control", "home_run_avoidance", "stamina"):
                    self.assertTrue(RATING_MIN <= getattr(ratings, name) <= RATING_MAX, name)


class StaminaEstimateTest(TestCase):
    def _starter(self, batters_per_start, starts=25):
        # 1先発あたりの対戦打者数 = アウト + 被安打 + 与四球。内訳は平均的な投手の割合にしておく
        faced = round(batters_per_start * starts)
        return pitcher_line(faced=faced, starts=starts)

    def test_without_any_record_it_is_in_between_starter_and_reliever(self):
        self.assertEqual(estimate_pitcher_ratings(PitchingLine()).stamina, UNKNOWN_STAMINA)

    def test_a_reliever_has_low_stamina_and_a_starter_high(self):
        reliever = estimate_pitcher_ratings(pitcher_line(faced=250, starts=0)).stamina
        starter = estimate_pitcher_ratings(self._starter(24)).stamina

        self.assertLess(reliever, starter)
        self.assertLess(reliever, 45)
        self.assertGreater(starter, 50)

    def test_longer_outings_mean_more_stamina(self):
        short = estimate_pitcher_ratings(self._starter(20)).stamina
        long = estimate_pitcher_ratings(self._starter(29)).stamina

        self.assertLess(short, long)

    def test_a_starter_with_few_starts_stays_near_the_prior(self):
        few = estimate_pitcher_ratings(self._starter(35, starts=1)).stamina
        many = estimate_pitcher_ratings(self._starter(35, starts=30)).stamina

        self.assertLess(few, many)
        self.assertLess(few, STARTER_STAMINA)


class GrowthTypeTest(TestCase):
    class Fixed:
        def __init__(self, value):
            self.value = value

        def random(self):
            return self.value

    def test_it_uses_a_single_random_number_in_the_order_early_normal_late(self):
        self.assertIs(draw_growth_type(self.Fixed(0.0), age=None, strength=0.0), GrowthType.EARLY)
        self.assertIs(draw_growth_type(self.Fixed(0.5), age=None, strength=0.0), GrowthType.NORMAL)
        self.assertIs(draw_growth_type(self.Fixed(0.99), age=None, strength=0.0), GrowthType.LATE)

    def _shares(self, age, strength, draws=4000):
        rng = make_random(7)
        counts = dict.fromkeys(GrowthType, 0)
        for _ in range(draws):
            counts[draw_growth_type(rng, age=age, strength=strength)] += 1
        return {growth: count / draws for growth, count in counts.items()}

    def test_the_prior_applies_without_an_age(self):
        shares = self._shares(None, 2.0)

        for growth, expected in GROWTH_PRIOR.items():
            self.assertAlmostEqual(shares[growth], expected, delta=0.03)

    def test_veterans_keep_the_prior_whatever_their_ability(self):
        shares = self._shares(31, 2.0)

        for growth, expected in GROWTH_PRIOR.items():
            self.assertAlmostEqual(shares[growth], expected, delta=0.03)

    def test_a_good_young_player_is_more_likely_to_be_an_early_bloomer(self):
        good = self._shares(21, 2.0)
        poor = self._shares(21, -2.0)

        self.assertGreater(good[GrowthType.EARLY], poor[GrowthType.EARLY])
        self.assertGreater(poor[GrowthType.LATE], good[GrowthType.LATE])
        self.assertGreater(good[GrowthType.EARLY], GROWTH_PRIOR[GrowthType.EARLY] + 0.03)
