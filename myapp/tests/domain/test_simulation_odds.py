"""能力 → 個人の率 → 対戦の確率（odds ratio 法）の単体テスト。Django も DB も使わない。

守りたい性質は3つ。
1. 能力50どうしなら、どの段の確率も `LeagueBaseline` と一致する（log5 の恒等性）
2. 各項目は、効くはずの向きに単調に効く
3. 能力の端（1や100）でも、確率は [0, 1] の内側に収まる
"""

import itertools
from dataclasses import replace
from typing import Any
from unittest import TestCase

from myapp.domain.simulation.baseline import NPB, LeagueBaseline
from myapp.domain.simulation.odds import (
    DEFAULT_SENSITIVITY,
    MatchupOdds,
    combine,
    from_odds,
    matchup,
    scaled_rate,
    to_odds,
)
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings

AVERAGE_BATTER = BatterRatings()
AVERAGE_PITCHER = PitcherRatings()


def _probabilities(odds: MatchupOdds) -> dict[str, float]:
    return {name: getattr(odds, name) for name in MatchupOdds.__dataclass_fields__}


class OddsRatioTest(TestCase):
    def test_a_neutral_opponent_leaves_the_rate_unchanged(self):
        """相手がリーグ平均なら、対戦の確率は個人の率そのもの。"""
        self.assertAlmostEqual(combine(0.3, 0.2, 0.2), 0.3)
        self.assertAlmostEqual(combine(0.2, 0.3, 0.2), 0.3)

    def test_the_method_is_symmetric_and_round_trips_odds(self):
        self.assertAlmostEqual(combine(0.3, 0.1, 0.2), combine(0.1, 0.3, 0.2))
        self.assertAlmostEqual(from_odds(to_odds(0.37)), 0.37)

    def test_a_better_batter_against_a_better_pitcher_lands_between(self):
        """打者が強く投手も強いと、打者だけが強いときより低い。"""
        weak_pitcher = combine(0.4, 0.3, 0.2)
        strong_pitcher = combine(0.4, 0.1, 0.2)
        self.assertGreater(weak_pitcher, strong_pitcher)

    def test_scaled_rate_is_unchanged_at_the_average_rating(self):
        self.assertEqual(scaled_rate(0.1, 0.5, 50), 0.1)

    def test_fifteen_points_scale_the_rate_by_exp_beta(self):
        self.assertAlmostEqual(scaled_rate(0.1, 0.5, 65) / 0.1, 2.718281828**0.5, places=6)


class AverageMatchupTest(TestCase):
    """能力50どうし。どの段も基準値と正確に一致する。"""

    def setUp(self):
        self.odds = matchup(AVERAGE_BATTER, AVERAGE_PITCHER)

    def test_every_stage_equals_the_baseline(self):
        baseline = NPB
        self.assertAlmostEqual(self.odds.hit_by_pitch, baseline.hit_by_pitch)
        self.assertAlmostEqual(self.odds.walk, baseline.walk_given_not_hit_by_pitch)
        self.assertAlmostEqual(self.odds.strikeout, baseline.strikeout_given_at_bat)
        self.assertAlmostEqual(self.odds.home_run, baseline.home_run_given_not_strikeout)
        self.assertAlmostEqual(self.odds.in_play_hit, baseline.babip)
        self.assertAlmostEqual(self.odds.double, baseline.double_share)
        self.assertAlmostEqual(self.odds.triple, baseline.triple_given_not_double)
        self.assertAlmostEqual(self.odds.error, baseline.error_share)

    def test_a_different_baseline_moves_the_average_matchup(self):
        """水準の調整は基準値で行う。基準値を変えれば、能力50どうしの確率がそのとおりに動く。"""
        higher = replace(NPB, strikeout=0.25)
        odds = matchup(AVERAGE_BATTER, AVERAGE_PITCHER, baseline=higher)
        self.assertAlmostEqual(odds.strikeout, higher.strikeout_given_at_bat)
        self.assertGreater(odds.strikeout, self.odds.strikeout)

    def test_the_implied_per_plate_appearance_rates_match_the_baseline(self):
        """段の確率を打席あたりに掛け戻すと、基準値の打席あたりの率に戻る。"""
        baseline = NPB
        not_hbp = 1.0 - self.odds.hit_by_pitch
        walk_per_pa = not_hbp * self.odds.walk
        self.assertAlmostEqual(walk_per_pa, baseline.walk)
        strikeout_per_pa = baseline.at_bat_share * self.odds.strikeout
        self.assertAlmostEqual(strikeout_per_pa, baseline.strikeout)
        home_run_per_pa = baseline.at_bat_share * (1.0 - self.odds.strikeout) * self.odds.home_run
        self.assertAlmostEqual(home_run_per_pa, baseline.home_run)


class MonotonicityTest(TestCase):
    """項目を1つだけ動かすと、確率が効く向きに動く。"""

    def _pairs(self, field: str, kind: str):
        """(低い方の対戦, 高い方の対戦)。kind は batter / pitcher / defense。"""
        low, high = 30, 70
        at_low: dict[str, Any] = {field: low}
        at_high: dict[str, Any] = {field: high}
        if kind == "batter":
            return (
                matchup(replace(AVERAGE_BATTER, **at_low), AVERAGE_PITCHER),
                matchup(replace(AVERAGE_BATTER, **at_high), AVERAGE_PITCHER),
            )
        if kind == "pitcher":
            return (
                matchup(AVERAGE_BATTER, replace(AVERAGE_PITCHER, **at_low)),
                matchup(AVERAGE_BATTER, replace(AVERAGE_PITCHER, **at_high)),
            )
        return matchup(AVERAGE_BATTER, AVERAGE_PITCHER, low), matchup(AVERAGE_BATTER, AVERAGE_PITCHER, high)

    def test_contact_lowers_strikeouts_and_raises_in_play_hits(self):
        low, high = self._pairs("contact", "batter")
        self.assertGreater(low.strikeout, high.strikeout)
        self.assertLess(low.in_play_hit, high.in_play_hit)

    def test_power_raises_home_runs_and_doubles(self):
        low, high = self._pairs("power", "batter")
        self.assertLess(low.home_run, high.home_run)
        self.assertLess(low.double, high.double)

    def test_eye_raises_walks(self):
        low, high = self._pairs("eye", "batter")
        self.assertLess(low.walk, high.walk)

    def test_speed_raises_triples(self):
        low, high = self._pairs("speed", "batter")
        self.assertLess(low.triple, high.triple)

    def test_stuff_raises_strikeouts(self):
        low, high = self._pairs("stuff", "pitcher")
        self.assertLess(low.strikeout, high.strikeout)

    def test_control_lowers_walks(self):
        low, high = self._pairs("control", "pitcher")
        self.assertGreater(low.walk, high.walk)

    def test_home_run_avoidance_lowers_home_runs(self):
        low, high = self._pairs("home_run_avoidance", "pitcher")
        self.assertGreater(low.home_run, high.home_run)

    def test_defense_lowers_hits_and_errors(self):
        low, high = self._pairs("fielding", "defense")
        self.assertGreater(low.in_play_hit, high.in_play_hit)
        self.assertGreater(low.error, high.error)

    def test_items_that_should_not_matter_do_not_move_unrelated_stages(self):
        """守備力は打者の三振や四球に効かない（飾りの項目や、意図しない漏れを作らない）。"""
        odds_low = matchup(replace(AVERAGE_BATTER, fielding=20), AVERAGE_PITCHER)
        odds_high = matchup(replace(AVERAGE_BATTER, fielding=90), AVERAGE_PITCHER)
        self.assertEqual(odds_low, odds_high)

    def test_stamina_does_not_change_a_single_plate_appearance(self):
        """スタミナは先発の受け持ちに効く項目で、1打席の確率には効かない。"""
        tired = matchup(AVERAGE_BATTER, replace(AVERAGE_PITCHER, stamina=10))
        fresh = matchup(AVERAGE_BATTER, replace(AVERAGE_PITCHER, stamina=90))
        self.assertEqual(tired, fresh)


class RangeTest(TestCase):
    def test_probabilities_stay_inside_zero_and_one_at_the_extremes(self):
        extremes = (1, 50, 100)
        for contact, power, eye, speed in itertools.product(extremes, repeat=4):
            batter = BatterRatings(contact, power, eye, speed, 50)
            for stuff, control, avoidance in itertools.product(extremes, repeat=3):
                pitcher = PitcherRatings(stuff, control, avoidance, 50)
                for defense in (1, 50, 100):
                    for name, value in _probabilities(matchup(batter, pitcher, defense)).items():
                        self.assertTrue(0.0 < value < 1.0, f"{name}={value} ({batter} / {pitcher} / {defense})")

    def test_stronger_sensitivity_widens_the_spread(self):
        """β を大きくすると、同じ能力差がより大きな率の差になる（ばらつきの調整は β で行う）。"""
        steeper = replace(DEFAULT_SENSITIVITY, power_home_run=DEFAULT_SENSITIVITY.power_home_run * 2)
        strong = replace(AVERAGE_BATTER, power=80)
        default = matchup(strong, AVERAGE_PITCHER).home_run
        wider = matchup(strong, AVERAGE_PITCHER, sensitivity=steeper).home_run
        self.assertGreater(wider, default)

    def test_baseline_is_a_frozen_value_object(self):
        with self.assertRaises(AttributeError):
            NPB.walk = 0.5  # type: ignore[misc]
        self.assertIsInstance(NPB, LeagueBaseline)
