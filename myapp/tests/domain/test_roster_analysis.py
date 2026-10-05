"""戦力分析（デプス表の区分・主な守備位置・年齢の帯）の規則。DB 不要。"""

import unittest
from datetime import date

from myapp.domain.services import roster_analysis as ra
from myapp.domain.value_objects import FieldingPosition as FP
from myapp.domain.value_objects import Handedness, Profile, Season


class PitcherRoleTest(unittest.TestCase):
    def test_boundaries(self):
        cases = [
            (0, 0, ra.PitcherRole.NOT_APPEARED),
            (4, 2, ra.PitcherRole.STARTER),  # ちょうど半分は先発
            (3, 1, ra.PitcherRole.RELIEVER),
            (3, 2, ra.PitcherRole.STARTER),
            (10, 0, ra.PitcherRole.RELIEVER),
            (1, 1, ra.PitcherRole.STARTER),
        ]
        for games, starts, expected in cases:
            with self.subTest(games=games, starts=starts):
                self.assertIs(ra.PitcherRole.of(games, starts), expected)


class FielderGroupTest(unittest.TestCase):
    def test_every_fielding_position_belongs_to_exactly_one_group(self):
        """守備位置を足したときに区分への割り当て忘れ（二重含む）が起きないこと。"""
        for position in FP:
            if position is FP.PITCHER or position.is_substitute_only:
                continue
            with self.subTest(position=position):
                owners = [group for group in ra.FielderGroup if position in group.positions]
                self.assertEqual(len(owners), 1)
                self.assertIs(ra.FielderGroup.of(position), owners[0])

    def test_mapping(self):
        self.assertIs(ra.FielderGroup.of(FP.SHORTSTOP), ra.FielderGroup.MIDDLE_INFIELD)
        self.assertIs(ra.FielderGroup.of(FP.SECOND_BASE), ra.FielderGroup.MIDDLE_INFIELD)
        self.assertIs(ra.FielderGroup.of(FP.FIRST_BASE), ra.FielderGroup.CORNER_INFIELD)
        self.assertIs(ra.FielderGroup.of(FP.CENTER_FIELD), ra.FielderGroup.OUTFIELD)
        self.assertIs(ra.FielderGroup.of(FP.DESIGNATED_HITTER), ra.FielderGroup.DESIGNATED_HITTER)

    def test_no_position_is_not_fielded(self):
        self.assertIs(ra.FielderGroup.of(None), ra.FielderGroup.NOT_FIELDED)
        self.assertIs(ra.FielderGroup.of(FP.PINCH_HITTER), ra.FielderGroup.NOT_FIELDED)


class PrimaryPositionTest(unittest.TestCase):
    def test_most_starts_wins_over_appearances(self):
        starts = {FP.SHORTSTOP: 50, FP.SECOND_BASE: 10}
        appearances = {FP.SHORTSTOP: 50, FP.SECOND_BASE: 120}
        self.assertIs(ra.primary_position(starts, appearances), FP.SHORTSTOP)

    def test_tie_goes_to_declaration_order(self):
        starts = {FP.LEFT_FIELD: 5, FP.SECOND_BASE: 5, FP.CATCHER: 5}
        self.assertIs(ra.primary_position(starts, starts), FP.CATCHER)

    def test_no_starts_falls_back_to_appearances(self):
        self.assertIs(ra.primary_position({}, {FP.RIGHT_FIELD: 7, FP.LEFT_FIELD: 3}), FP.RIGHT_FIELD)
        self.assertIs(ra.primary_position({FP.RIGHT_FIELD: 0}, {FP.LEFT_FIELD: 3}), FP.LEFT_FIELD)

    def test_pinch_hitter_and_runner_and_pitcher_are_ignored(self):
        appearances = {FP.PINCH_HITTER: 30, FP.PINCH_RUNNER: 20, FP.PITCHER: 9}
        self.assertIsNone(ra.primary_position({}, appearances))
        self.assertIs(ra.primary_position({}, {**appearances, FP.FIRST_BASE: 1}), FP.FIRST_BASE)

    def test_designated_hitter_counts(self):
        self.assertIs(
            ra.primary_position({FP.DESIGNATED_HITTER: 40}, {FP.DESIGNATED_HITTER: 40}), FP.DESIGNATED_HITTER
        )

    def test_nothing_is_none(self):
        self.assertIsNone(ra.primary_position({}, {}))


class AgeTest(unittest.TestCase):
    def test_reference_date_is_april_first(self):
        self.assertEqual(Season(2026).age_reference_date, date(2026, 4, 1))

    def test_born_on_april_first_is_counted(self):
        self.assertEqual(Profile(birth_date=date(2000, 4, 1)).age_in(Season(2026)), 26)

    def test_born_on_april_second_is_not_counted(self):
        self.assertEqual(Profile(birth_date=date(2000, 4, 2)).age_in(Season(2026)), 25)

    def test_unknown_birth_date(self):
        self.assertIsNone(Profile().age_in(Season(2026)))

    def test_born_after_reference_date_is_none(self):
        self.assertIsNone(Profile(birth_date=date(2026, 4, 2)).age_in(Season(2026)))

    def test_band_edges(self):
        cases = [(17, "18歳以下"), (18, "18歳以下"), (19, "19歳"), (39, "39歳"), (40, "40歳以上"), (45, "40歳以上")]
        for age, expected in cases:
            with self.subTest(age=age):
                self.assertEqual(ra.age_band(age), expected)
        self.assertEqual(ra.age_band(None), "不明")

    def test_distribution_fills_gaps_and_appends_unknown(self):
        rows = ra.age_distribution([20, 17, None], [22, 22, 45])
        self.assertEqual(
            [r.band for r in rows],
            ["18歳以下", "19歳", "20歳", "21歳", "22歳", *[f"{a}歳" for a in range(23, 40)], "40歳以上", "不明"],
        )
        by_band = {r.band: r for r in rows}
        self.assertEqual((by_band["18歳以下"].pitchers, by_band["18歳以下"].total), (1, 1))
        self.assertEqual((by_band["22歳"].fielders, by_band["22歳"].total), (2, 2))
        self.assertEqual(by_band["21歳"].total, 0)
        self.assertEqual(by_band["不明"].pitchers, 1)

    def test_distribution_empty(self):
        self.assertEqual(ra.age_distribution([], []), [])

    def test_average_age_uses_actual_ages_and_skips_unknown(self):
        self.assertEqual(ra.average_age([17, 40, None]), 28.5)
        self.assertIsNone(ra.average_age([None]))


class HandTest(unittest.TestCase):
    def test_hand_of(self):
        profile = Profile(throws=Handedness.LEFT, bats=Handedness.RIGHT)
        self.assertIs(ra.hand_of(profile, is_pitcher=True), Handedness.LEFT)
        self.assertIs(ra.hand_of(profile, is_pitcher=False), Handedness.RIGHT)
        self.assertIsNone(ra.hand_of(Profile(), is_pitcher=True))

    def test_pitcher_columns(self):
        self.assertEqual(ra.hand_columns([], is_pitcher=True), [Handedness.LEFT, Handedness.RIGHT])
        self.assertEqual(
            ra.hand_columns([Handedness.BOTH, None], is_pitcher=True),
            [Handedness.LEFT, Handedness.BOTH, Handedness.RIGHT, None],
        )

    def test_batter_columns_always_have_switch_hitters(self):
        self.assertEqual(ra.hand_columns([], is_pitcher=False), [Handedness.LEFT, Handedness.BOTH, Handedness.RIGHT])
        self.assertEqual(
            ra.hand_columns([None], is_pitcher=False), [Handedness.LEFT, Handedness.BOTH, Handedness.RIGHT, None]
        )

    def test_labels(self):
        self.assertEqual(ra.hand_label(Handedness.LEFT, is_pitcher=True), "左腕")
        self.assertEqual(ra.hand_label(Handedness.BOTH, is_pitcher=False), "両打")
        self.assertEqual(ra.hand_label(None, is_pitcher=False), "不明")


class DepthOrderTest(unittest.TestCase):
    def test_starts_then_games_then_number(self):
        players = [(5, 20, 11), (10, 10, 99), (5, 30, 7), (5, 30, 3)]
        ordered = sorted(players, key=lambda p: ra.depth_order(*p))
        self.assertEqual(ordered, [(10, 10, 99), (5, 30, 3), (5, 30, 7), (5, 20, 11)])
