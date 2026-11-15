"""監督の采配の傾向（`Tactics`。#210 M1）の単体テスト。Django も DB も使わない。

守りたいこと:
- 標準（`Tactics.STANDARD`）なら、傾向を足す前の定数・確率をそのまま使う（1打席も違わない）
- 各軸は段階について単調（先発の目安・代打を出しやすさ・盗塁の企図・犠打）
- 乱数を引く回数はどの傾向でも同じ（先発の目安・盗塁の企図・犠打の判定は1回ずつ）
- 段階ごとの表記が5つずつそろう
"""

from collections import Counter
from datetime import date
from unittest import TestCase

from myapp.domain.simulation.baseline import NPB
from myapp.domain.simulation.baserunning import DEFAULT_RULES, build_advances
from myapp.domain.simulation.engine import simulate_game
from myapp.domain.simulation.manager import (
    PINCH_HITTER_MARGIN,
    ForeignQuota,
    PinchHitSituation,
    SimBatter,
    SimPitcher,
    choose_active_roster,
    choose_pinch_hitter,
    starter_batter_target,
)
from myapp.domain.simulation.randomness import make_random
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.simulation.samples import average_club
from myapp.domain.simulation.tactics import TacticAxis, TacticLevel, Tactics
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    FieldingPosition,
    ForeignPlayerRules,
    GameStage,
    PlateAppearanceResult,
    Position,
)

from .doubles import OnlyRandom
from .markers import slow

LEVELS = list(TacticLevel)


def batter(player_id, position=Position.INFIELDER, **ratings) -> SimBatter:
    return SimBatter(player_id, f"野手{player_id}", position, BatterRatings(**ratings), False, throws=None)


def pitcher(player_id, **ratings) -> SimPitcher:
    return SimPitcher(player_id, f"投手{player_id}", PitcherRatings(**ratings), False)


def _all(level: int) -> Tactics:
    tactic = TacticLevel(level)
    return Tactics(starter_leash=tactic, pinch_hitting=tactic, base_stealing=tactic, bunting=tactic)


class StandardTacticsTest(TestCase):
    """標準の傾向は、傾向を足す前と同じ値を返す（ずらす量が0・確率をそのまま返す）。"""

    def test_standard_is_all_zero(self):
        self.assertEqual(Tactics.STANDARD, Tactics())
        self.assertEqual(Tactics.STANDARD, _all(0))

    def test_standard_returns_the_constants_unchanged(self):
        standard = Tactics.STANDARD
        self.assertEqual(standard.starter_batters_shift(), 0.0)
        self.assertEqual(standard.pinch_hitter_margin(PINCH_HITTER_MARGIN), PINCH_HITTER_MARGIN)
        for probability in (0.0, 0.0123456789, NPB.sacrifice_bunt_rate, 0.55, 0.999, 1.0):
            # 浮動小数の往復（odds への変換）も通さず、同じ値そのものを返す
            self.assertIs(standard.steal_attempt_rate(probability), probability)
            self.assertIs(standard.bunt_rate(probability), probability)

    def test_explicit_standard_gives_the_same_plate_appearances_as_no_argument(self):
        home = choose_active_roster(average_club(1), rules=ForeignPlayerRules.UNLIMITED)
        away = choose_active_roster(average_club(2), rules=ForeignPlayerRules.UNLIMITED)
        for seed in range(5):
            common = {
                "played_on": date(2026, 4, 1),
                "stage": GameStage.REGULAR,
                "foreign_rules": ForeignPlayerRules.UNLIMITED,
            }
            plain = simulate_game(make_random(seed), home, away, **common)
            explicit = simulate_game(
                make_random(seed), home, away, home_tactics=_all(0), away_tactics=Tactics.STANDARD, **common
            )
            self.assertEqual(plain.plate_appearances, explicit.plate_appearances)
            self.assertEqual(plain.lineup, explicit.lineup)

    def test_levels_outside_the_range_are_rejected(self):
        with self.assertRaises(ValueError):
            Tactics(base_stealing=3)


class MonotonicTest(TestCase):
    """どの軸も、段階が上がるほどその采配が多く・長くなる。"""

    def test_starter_leash_lengthens_the_outing(self):
        means = []
        for level in LEVELS:
            rng = make_random(7)
            tactics = Tactics(starter_leash=level)
            means.append(sum(starter_batter_target(rng, pitcher(1), tactics) for _ in range(300)) / 300)
        self.assertEqual(means, sorted(means))
        self.assertGreater(means[-1], means[0] + 4)

    def test_pinch_hitting_lowers_the_margin(self):
        margins = [Tactics(pinch_hitting=level).pinch_hitter_margin(PINCH_HITTER_MARGIN) for level in LEVELS]
        self.assertEqual(margins, sorted(margins, reverse=True))
        self.assertGreater(margins[0], 0)

    def test_a_pinch_hitter_who_is_only_slightly_better_is_sent_only_by_an_aggressive_manager(self):
        current = batter(1, Position.OUTFIELDER, contact=50, power=50, eye=50, speed=50)
        # 打撃の総合値が今の打者より少しだけ上（標準の「明確に上」には届かない）控え
        slightly = batter(2, Position.OUTFIELDER, contact=54, power=54, eye=54, speed=50)
        gap = slightly.ratings.batting_value - current.ratings.batting_value
        self.assertLess(gap, PINCH_HITTER_MARGIN)

        def decision(level: TacticLevel):
            return choose_pinch_hitter(
                PinchHitSituation(
                    inning=8,
                    lead=-1,
                    pinch_hitters_used=0,
                    current=current,
                    position=FieldingPosition.LEFT_FIELD,
                    bench=(slightly,),
                    quota=ForeignQuota(),
                    tactics=Tactics(pinch_hitting=level),
                )
            )

        self.assertIsNone(decision(TacticLevel.STANDARD))
        self.assertIsNotNone(decision(TacticLevel.MUCH_MORE))

    def test_steal_and_bunt_rates_rise_with_the_level(self):
        for method in (Tactics.steal_attempt_rate, Tactics.bunt_rate):
            for probability in (0.05, 0.3, 0.8):
                axis = "base_stealing" if method is Tactics.steal_attempt_rate else "bunting"
                rates = [method(Tactics(**{axis: level}), probability) for level in LEVELS]
                self.assertEqual(rates, sorted(rates))
                self.assertLess(rates[0], probability)
                self.assertGreater(rates[-1], probability)
                self.assertTrue(all(0.0 < rate < 1.0 for rate in rates))


class RandomCallsTest(TestCase):
    """傾向は平均と率をずらすだけで、乱数を引く回数を変えない。"""

    def test_starter_target_draws_the_same_number_of_times(self):
        counts = set()
        for level in LEVELS:
            rng = OnlyRandom(3)
            starter_batter_target(rng, pitcher(1), Tactics(starter_leash=level))
            counts.add(rng.calls)
        self.assertEqual(len(counts), 1)

    def test_a_steal_attempt_draws_once_whether_or_not_the_runner_goes(self):
        for level in LEVELS:
            rng = OnlyRandom(5)
            occupied = {Base.FIRST: 10}
            advances = build_advances(
                rng,
                DEFAULT_RULES,
                PlateAppearanceResult.STRIKEOUT_SWINGING,
                20,
                occupied,
                0,
                {10: 50},
                Tactics(base_stealing=level),
            )
            stole = any(a.reason in (AdvanceReason.STOLEN_BASE, AdvanceReason.CAUGHT_STEALING) for a in advances)
            # 企図の判定に1回、試みたら成否に1回
            self.assertEqual(rng.calls, 2 if stole else 1)

    def test_any_tactics_consume_the_same_random_numbers_per_game_decision(self):
        """傾向を変えても、1試合のうち乱数を使う判断の回数は打席の数と結果で決まる（傾向そのものは引かない）。

        同じ打席列になる標準どうしで、呼んだ回数が一致することを確かめる（傾向が乱数を余分に引けば、ずれる）。
        """
        home = choose_active_roster(average_club(1), rules=ForeignPlayerRules.UNLIMITED)
        away = choose_active_roster(average_club(2), rules=ForeignPlayerRules.UNLIMITED)
        common = {
            "played_on": date(2026, 4, 1),
            "stage": GameStage.REGULAR,
            "foreign_rules": ForeignPlayerRules.UNLIMITED,
        }
        first, second = OnlyRandom(11), OnlyRandom(11)
        simulate_game(first, home, away, **common)
        simulate_game(second, home, away, home_tactics=_all(0), away_tactics=_all(0), **common)
        self.assertEqual(first.calls, second.calls)


@slow
class TacticsInGamesTest(TestCase):
    """傾向は試合の形に表れる（盗塁・犠打の数、先発の受け持ち）。"""

    GAMES = 60

    def tally(self, tactics: Tactics) -> Counter:
        home = choose_active_roster(average_club(1), rules=ForeignPlayerRules.UNLIMITED)
        away = choose_active_roster(average_club(2), rules=ForeignPlayerRules.UNLIMITED)
        counts: Counter = Counter()
        for seed in range(self.GAMES):
            played = simulate_game(
                make_random(seed),
                home,
                away,
                played_on=date(2026, 4, 1),
                stage=GameStage.REGULAR,
                foreign_rules=ForeignPlayerRules.UNLIMITED,
                home_tactics=tactics,
                away_tactics=tactics,
            )
            for pa in played.plate_appearances:
                counts["bunts"] += pa.result is PlateAppearanceResult.SACRIFICE_BUNT
                counts["steal_attempts"] += sum(
                    a.reason in (AdvanceReason.STOLEN_BASE, AdvanceReason.CAUGHT_STEALING) for a in pa.advances
                )
            counts["starter_batters"] += sum(o.faced for o in played.outings if o.is_starter)
        return counts

    def test_aggressive_tactics_bunt_and_run_more_and_keep_starters_longer(self):
        low, high = self.tally(_all(-2)), self.tally(_all(2))
        self.assertGreater(high["bunts"], low["bunts"])
        self.assertGreater(high["steal_attempts"], low["steal_attempts"])
        self.assertGreater(high["starter_batters"], low["starter_batters"])


class LabelTest(TestCase):
    def test_every_axis_has_a_title_and_five_labels(self):
        for axis in TacticAxis:
            self.assertTrue(axis.title)
            labels = [axis.label(level) for level in LEVELS]
            self.assertEqual(len(set(labels)), 5)
            self.assertEqual(axis.label(TacticLevel.STANDARD), "標準")

    def test_axis_values_name_the_fields(self):
        tactics = Tactics(starter_leash=1, pinch_hitting=-1, base_stealing=2, bunting=-2)
        self.assertEqual([tactics.level(axis) for axis in TacticAxis], [1, -1, 2, -2])
