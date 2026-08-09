"""値オブジェクトの単体テスト。

Django のテストランナー上で動くが、DB もモデルも一切使わない。
"""

from decimal import Decimal
from unittest import TestCase

from myapp.domain.exceptions import (
    InvalidInningsPitched,
    InvalidJerseyNumber,
    InvalidPosition,
    InvalidStatValue,
)
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)


class PositionTest(TestCase):
    def test_all_positions_are_available(self):
        """守備位置は5種類。指名打者も含まれる（欠落バグの再発防止）。"""
        self.assertEqual(
            Position.labels(),
            ['投手', '捕手', '内野手', '外野手', '指名打者'],
        )

    def test_only_pitcher_is_pitcher(self):
        self.assertTrue(Position.PITCHER.is_pitcher)
        for label in ['捕手', '内野手', '外野手', '指名打者']:
            self.assertFalse(Position.from_label(label).is_pitcher)

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(InvalidPosition):
            Position.from_label('遊撃手')


class JerseyNumberTest(TestCase):
    def test_accepts_valid_numbers(self):
        self.assertEqual(JerseyNumber(0).value, 0)
        self.assertEqual(JerseyNumber(999).value, 999)

    def test_string_is_normalised(self):
        self.assertEqual(JerseyNumber('18').value, 18)

    def test_rejects_out_of_range(self):
        with self.assertRaises(InvalidJerseyNumber):
            JerseyNumber(1000)
        with self.assertRaises(InvalidJerseyNumber):
            JerseyNumber(-1)

    def test_rejects_non_numeric(self):
        with self.assertRaises(InvalidJerseyNumber):
            JerseyNumber('背番号')

    def test_equality_is_by_value(self):
        self.assertEqual(JerseyNumber(10), JerseyNumber(10))
        self.assertNotEqual(JerseyNumber(10), JerseyNumber(11))


class InningsPitchedTest(TestCase):
    """野球表記（5.2 = 5回と2/3）の変換。"""

    def test_notation_to_outs(self):
        self.assertEqual(InningsPitched.from_notation(0).outs, 0)
        self.assertEqual(InningsPitched.from_notation(1).outs, 3)
        self.assertEqual(InningsPitched.from_notation('5.0').outs, 15)
        self.assertEqual(InningsPitched.from_notation('5.1').outs, 16)
        self.assertEqual(InningsPitched.from_notation('5.2').outs, 17)

    def test_outs_to_notation(self):
        self.assertEqual(InningsPitched(outs=17).to_notation(), Decimal('5.2'))
        self.assertEqual(InningsPitched(outs=18).to_notation(), Decimal('6.0'))

    def test_round_trip(self):
        for notation in ['0.0', '0.1', '3.2', '10.1', '162.2']:
            with self.subTest(notation=notation):
                vo = InningsPitched.from_notation(notation)
                self.assertEqual(str(vo), f"{Decimal(notation):.1f}")

    def test_invalid_fraction_is_normalised(self):
        """5.3 は表記として存在しない。6.0 に繰り上がる。"""
        self.assertEqual(InningsPitched.from_notation('5.3').outs, 18)
        self.assertEqual(str(InningsPitched.from_notation('5.3')), '6.0')

    def test_empty_means_zero(self):
        self.assertEqual(InningsPitched.from_notation('').outs, 0)
        self.assertEqual(InningsPitched.from_notation(None).outs, 0)

    def test_negative_is_rejected(self):
        with self.assertRaises(InvalidInningsPitched):
            InningsPitched.from_notation('-1.0')

    def test_as_innings_is_a_real_number(self):
        self.assertAlmostEqual(InningsPitched.from_notation('5.2').as_innings, 17 / 3)


class BattingLineTest(TestCase):
    def test_hits_is_the_sum_of_hit_types(self):
        line = BattingLine(at_bats=10, singles=2, doubles=1, triples=1, home_runs=1)
        self.assertEqual(line.hits, 5)

    def test_total_bases(self):
        line = BattingLine(at_bats=10, singles=2, doubles=1, triples=1, home_runs=1)
        # 2*1 + 1*2 + 1*3 + 1*4 = 11
        self.assertEqual(line.total_bases, 11)

    def test_batting_average(self):
        line = BattingLine(at_bats=4, singles=1)
        self.assertAlmostEqual(line.batting_average, 0.25)

    def test_on_base_percentage_includes_walks_and_hbp(self):
        line = BattingLine(at_bats=3, singles=1, walks=1, hit_by_pitch=1)
        # (1+1+1) / (3+1+1+0) = 0.6
        self.assertAlmostEqual(line.on_base_percentage, 0.6)

    def test_slugging_percentage(self):
        line = BattingLine(at_bats=4, home_runs=1)
        self.assertAlmostEqual(line.slugging_percentage, 1.0)

    def test_ops_is_obp_plus_slg(self):
        line = BattingLine(at_bats=4, singles=1, doubles=1, walks=1)
        self.assertAlmostEqual(
            line.ops, line.on_base_percentage + line.slugging_percentage
        )

    def test_no_at_bats_does_not_divide_by_zero(self):
        line = BattingLine()
        self.assertEqual(line.batting_average, 0.0)
        self.assertEqual(line.on_base_percentage, 0.0)
        self.assertEqual(line.slugging_percentage, 0.0)
        self.assertEqual(line.ops, 0.0)

    def test_negative_value_is_rejected(self):
        with self.assertRaises(InvalidStatValue):
            BattingLine(at_bats=-1)

    def test_hits_cannot_exceed_at_bats(self):
        with self.assertRaises(InvalidStatValue):
            BattingLine(at_bats=1, singles=2)


class PitchingLineTest(TestCase):
    def test_earned_run_average(self):
        # 9回で自責点3 → 防御率 3.00
        line = PitchingLine(innings=InningsPitched.from_notation('9.0'), earned_runs=3)
        self.assertAlmostEqual(line.earned_run_average, 3.0)

    def test_era_with_fractional_innings(self):
        # 5.2回（17アウト）で自責点2 → 2*27/17
        line = PitchingLine(innings=InningsPitched.from_notation('5.2'), earned_runs=2)
        self.assertAlmostEqual(line.earned_run_average, 2 * 27 / 17)

    def test_whip(self):
        # 9回で被安打6・与四球3 → WHIP 1.00
        line = PitchingLine(
            innings=InningsPitched.from_notation('9.0'), hits_allowed=6, walks_allowed=3
        )
        self.assertAlmostEqual(line.whip, 1.0)

    def test_strikeouts_per_nine(self):
        line = PitchingLine(innings=InningsPitched.from_notation('9.0'), strikeouts=12)
        self.assertAlmostEqual(line.strikeouts_per_nine, 12.0)

    def test_no_innings_does_not_divide_by_zero(self):
        line = PitchingLine()
        self.assertEqual(line.earned_run_average, 0.0)
        self.assertEqual(line.whip, 0.0)
        self.assertEqual(line.strikeouts_per_nine, 0.0)

    def test_negative_value_is_rejected(self):
        with self.assertRaises(InvalidStatValue):
            PitchingLine(earned_runs=-1)
