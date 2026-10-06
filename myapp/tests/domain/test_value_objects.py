"""値オブジェクトの単体テスト。

Django のテストランナー上で動くが、DB もモデルも一切使わない。
"""

from dataclasses import fields
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
            ["投手", "捕手", "内野手", "外野手", "指名打者"],
        )

    def test_only_pitcher_is_pitcher(self):
        self.assertTrue(Position.PITCHER.is_pitcher)
        for label in ["捕手", "内野手", "外野手", "指名打者"]:
            self.assertFalse(Position.from_label(label).is_pitcher)

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(InvalidPosition):
            Position.from_label("遊撃手")


class JerseyNumberTest(TestCase):
    def test_accepts_valid_numbers(self):
        for text in ["0", "00", "7", "99", "100", "999"]:
            with self.subTest(text=text):
                self.assertEqual(JerseyNumber(text).value, text)

    def test_zero_and_double_zero_are_different_numbers(self):
        self.assertNotEqual(JerseyNumber("0"), JerseyNumber("00"))
        self.assertEqual(str(JerseyNumber("00")), "00")

    def test_rejects_leading_zeros_other_than_double_zero(self):
        for text in ["01", "007", "000", "0000"]:
            with self.subTest(text=text), self.assertRaises(InvalidJerseyNumber):
                JerseyNumber(text)

    def test_rejects_out_of_range_and_malformed(self):
        for value in ["1000", "-1", "", "a", "背番号", "1.5", " ", "１２"]:
            with self.subTest(value=value), self.assertRaises(InvalidJerseyNumber):
                JerseyNumber(value)
        for value in [-1, 1000, None, 1.5, True]:
            with self.subTest(value=value), self.assertRaises(InvalidJerseyNumber):
                JerseyNumber(value)

    def test_int_is_turned_into_text_and_never_becomes_double_zero(self):
        self.assertEqual(JerseyNumber(0).value, "0")
        self.assertEqual(JerseyNumber(18).value, "18")

    def test_surrounding_spaces_are_trimmed(self):
        self.assertEqual(JerseyNumber(" 18 ").value, "18")

    def test_sort_key_orders_double_zero_then_zero_then_numerically(self):
        texts = ["100", "99", "10", "9", "1", "0", "00", "2"]
        ordered = sorted(texts, key=lambda t: JerseyNumber(t).sort_key)
        self.assertEqual(ordered, ["00", "0", "1", "2", "9", "10", "99", "100"])

    def test_three_digit_is_decided_by_notation(self):
        self.assertFalse(JerseyNumber("00").is_three_digit)
        self.assertFalse(JerseyNumber("99").is_three_digit)
        self.assertTrue(JerseyNumber("100").is_three_digit)

    def test_equality_is_by_value(self):
        self.assertEqual(JerseyNumber("10"), JerseyNumber("10"))
        self.assertNotEqual(JerseyNumber("10"), JerseyNumber("11"))


class InningsPitchedTest(TestCase):
    """野球表記（5.2 = 5回と2/3）の変換。"""

    def test_notation_to_outs(self):
        self.assertEqual(InningsPitched.from_notation(0).outs, 0)
        self.assertEqual(InningsPitched.from_notation(1).outs, 3)
        self.assertEqual(InningsPitched.from_notation("5.0").outs, 15)
        self.assertEqual(InningsPitched.from_notation("5.1").outs, 16)
        self.assertEqual(InningsPitched.from_notation("5.2").outs, 17)

    def test_outs_to_notation(self):
        self.assertEqual(InningsPitched(outs=17).to_notation(), Decimal("5.2"))
        self.assertEqual(InningsPitched(outs=18).to_notation(), Decimal("6.0"))

    def test_round_trip(self):
        for notation in ["0.0", "0.1", "3.2", "10.1", "162.2"]:
            with self.subTest(notation=notation):
                vo = InningsPitched.from_notation(notation)
                self.assertEqual(str(vo), f"{Decimal(notation):.1f}")

    def test_invalid_fraction_is_normalised(self):
        """5.3 は表記として存在しない。6.0 に繰り上がる。"""
        self.assertEqual(InningsPitched.from_notation("5.3").outs, 18)
        self.assertEqual(str(InningsPitched.from_notation("5.3")), "6.0")

    def test_empty_means_zero(self):
        self.assertEqual(InningsPitched.from_notation("").outs, 0)
        self.assertEqual(InningsPitched.from_notation(None).outs, 0)

    def test_negative_is_rejected(self):
        with self.assertRaises(InvalidInningsPitched):
            InningsPitched.from_notation("-1.0")

    def test_as_innings_is_a_real_number(self):
        self.assertAlmostEqual(InningsPitched.from_notation("5.2").as_innings, 17 / 3)

    def test_times_repeats_the_same_innings(self):
        """集計済みの行（同じ表記 × 件数）を足し合わせる。5.2 を2回で 11.1（10.4 ではない）。"""
        self.assertEqual(str(InningsPitched.from_notation("5.2").times(2)), "11.1")
        self.assertEqual(InningsPitched.from_notation("5.2").times(0), InningsPitched.zero())


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
        self.assertAlmostEqual(line.ops, line.on_base_percentage + line.slugging_percentage)

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

    def test_isolated_power_is_slugging_minus_average(self):
        # 10打数で本塁打2・単打1 → 長打率 .900、打率 .300
        line = BattingLine(at_bats=10, singles=1, home_runs=2)
        self.assertAlmostEqual(line.isolated_power, 0.9 - 0.3)

    def test_isolated_power_is_zero_for_singles_only(self):
        """単打だけの打者は長打力0。打率が高くても IsoP には表れない。"""
        line = BattingLine(at_bats=10, singles=5)
        self.assertAlmostEqual(line.isolated_power, 0.0)

    def test_ops_plus_is_a_ratio_to_league_average(self):
        line = BattingLine(at_bats=4, singles=1, doubles=1, walks=1)
        self.assertAlmostEqual(line.ops_plus(line.ops), 100.0)
        self.assertAlmostEqual(line.ops_plus(line.ops * 2), 50.0)

    def test_ops_plus_without_at_bats_is_zero(self):
        self.assertEqual(BattingLine().ops_plus(0.700), 0.0)

    def test_ops_plus_without_league_average_is_zero(self):
        line = BattingLine(at_bats=4, singles=1)
        self.assertEqual(line.ops_plus(0.0), 0.0)


class StatLineAdditionTest(TestCase):
    """成績の足し算が、すべての項目を積み上げること。

    __add__ が項目を手で列挙していた頃、打席から導く項目（得点・三振・盗塁・失点など）を
    足し忘れ、月別・年度別・チーム成績でその項目だけ 0 になっていた。例外にならないので、
    フィールドの一覧から機械的に突き合わせる。
    """

    def test_batting_line_sums_every_field(self):
        # 打数は安打の合計（単打〜本塁打）を下回れないので多めにする
        line = BattingLine(**{**{f.name: 1 for f in fields(BattingLine)}, "at_bats": 10})
        total = BattingLine.total([line, line])

        for field in fields(BattingLine):
            self.assertEqual(getattr(total, field.name), getattr(line, field.name) * 2, field.name)

    def test_pitching_line_sums_every_field(self):
        counts = {f.name: 1 for f in fields(PitchingLine) if f.name != "innings"}
        line = PitchingLine(innings=InningsPitched.from_notation("6.0"), **counts)
        total = PitchingLine.total([line, line])

        self.assertEqual(total.innings, InningsPitched.from_notation("12.0"))
        for name in counts:
            self.assertEqual(getattr(total, name), 2, name)


class PitchingLineTest(TestCase):
    def test_earned_run_average(self):
        # 9回で自責点3 → 防御率 3.00
        line = PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=3)
        self.assertAlmostEqual(line.earned_run_average, 3.0)

    def test_era_with_fractional_innings(self):
        # 5.2回（17アウト）で自責点2 → 2*27/17
        line = PitchingLine(innings=InningsPitched.from_notation("5.2"), earned_runs=2)
        self.assertAlmostEqual(line.earned_run_average, 2 * 27 / 17)

    def test_whip(self):
        # 9回で被安打6・与四球3 → WHIP 1.00
        line = PitchingLine(innings=InningsPitched.from_notation("9.0"), hits_allowed=6, walks_allowed=3)
        self.assertAlmostEqual(line.whip, 1.0)

    def test_strikeouts_per_nine(self):
        line = PitchingLine(innings=InningsPitched.from_notation("9.0"), strikeouts=12)
        self.assertAlmostEqual(line.strikeouts_per_nine, 12.0)

    def test_walks_per_nine(self):
        line = PitchingLine(
            innings=InningsPitched.from_notation("9.0"),
            walks_allowed=3,
            hit_by_pitch_allowed=2,
        )
        # 死球は分子に含めない
        self.assertAlmostEqual(line.walks_per_nine, 3.0)

    def test_winning_percentage(self):
        # 13勝4敗 → 13 ÷ 17。チームの勝率と同じく勝敗だけが分母
        line = PitchingLine(innings=InningsPitched.from_notation("150.0"), wins=13, losses=4)
        self.assertAlmostEqual(line.winning_percentage, 13 / 17)

    def test_winning_percentage_without_losses_is_one(self):
        line = PitchingLine(innings=InningsPitched.from_notation("150.0"), wins=13)
        self.assertAlmostEqual(line.winning_percentage, 1.0)

    def test_winning_percentage_without_decisions_is_zero(self):
        line = PitchingLine(innings=InningsPitched.from_notation("10.0"))
        self.assertEqual(line.winning_percentage, 0.0)

    def test_fip_base(self):
        # 9回で被本塁打1・与四球2・与死球1・奪三振9
        # → (13×1 + 3×3 − 2×9) ÷ 9 = 4 ÷ 9
        line = PitchingLine(
            innings=InningsPitched.from_notation("9.0"),
            hits_allowed=1,
            home_runs_allowed=1,
            walks_allowed=2,
            hit_by_pitch_allowed=1,
            strikeouts=9,
        )
        self.assertAlmostEqual(line.fip_base, 4 / 9)

    def test_fip_adds_the_league_constant(self):
        line = PitchingLine(
            innings=InningsPitched.from_notation("9.0"),
            hits_allowed=1,
            home_runs_allowed=1,
            strikeouts=9,
        )
        self.assertAlmostEqual(line.fip(3.10), line.fip_base + 3.10)

    def test_fip_of_an_unused_pitcher_is_zero(self):
        """未登板は定数を足さない。0回の投球に指標を与えると実力と無関係な値になる。"""
        self.assertEqual(PitchingLine().fip(3.10), 0.0)

    def test_era_plus_is_the_inverse_ratio_to_league_average(self):
        # リーグ平均と同じ防御率なら100。自分の防御率が半分（良い）なら200
        line = PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=2)
        self.assertAlmostEqual(line.era_plus(line.earned_run_average), 100.0)
        self.assertAlmostEqual(line.era_plus(line.earned_run_average / 2), 50.0)
        self.assertAlmostEqual(line.era_plus(line.earned_run_average * 2), 200.0)

    def test_era_plus_of_a_scoreless_pitcher_is_capped(self):
        """自責点0は比率が無限大になるため、上限値で頭打ちにする。"""
        line = PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=0)
        self.assertEqual(line.era_plus(3.50), PitchingLine.ERA_PLUS_CAP)

    def test_era_plus_of_an_unused_pitcher_is_zero(self):
        self.assertEqual(PitchingLine().era_plus(3.50), 0.0)

    def test_era_plus_without_league_average_is_zero(self):
        line = PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=2)
        self.assertEqual(line.era_plus(0.0), 0.0)

    def test_no_innings_does_not_divide_by_zero(self):
        line = PitchingLine()
        self.assertEqual(line.earned_run_average, 0.0)
        self.assertEqual(line.whip, 0.0)
        self.assertEqual(line.strikeouts_per_nine, 0.0)
        self.assertEqual(line.walks_per_nine, 0.0)
        self.assertEqual(line.fip_base, 0.0)

    def test_negative_value_is_rejected(self):
        with self.assertRaises(InvalidStatValue):
            PitchingLine(earned_runs=-1)

    def test_home_runs_allowed_cannot_exceed_hits_allowed(self):
        """被本塁打は被安打の内数。超える組み合わせは記録として成立しない。"""
        with self.assertRaises(InvalidStatValue):
            PitchingLine(hits_allowed=1, home_runs_allowed=2)

    def test_totals_add_the_new_counts(self):
        line = PitchingLine(
            innings=InningsPitched.from_notation("3.0"),
            hits_allowed=2,
            home_runs_allowed=1,
            hit_by_pitch_allowed=1,
        )
        total = PitchingLine.total([line, line])

        self.assertEqual(total.home_runs_allowed, 2)
        self.assertEqual(total.hit_by_pitch_allowed, 2)
