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


class UsageMapTest(unittest.TestCase):
    def test_positions_are_pitcher_and_the_fielding_groups_in_declaration_order(self):
        self.assertEqual(
            ra.usage_map_positions(),
            (FP.PITCHER, FP.CATCHER, FP.FIRST_BASE, FP.SECOND_BASE, FP.THIRD_BASE, FP.SHORTSTOP)
            + (FP.LEFT_FIELD, FP.CENTER_FIELD, FP.RIGHT_FIELD, FP.DESIGNATED_HITTER),
        )

    def test_substitute_only_positions_have_no_box(self):
        positions = ra.usage_map_positions()
        self.assertNotIn(FP.PINCH_HITTER, positions)
        self.assertNotIn(FP.PINCH_RUNNER, positions)

    def test_box_shows_up_to_the_cap(self):
        cap = ra.MAX_PLAYERS_IN_BOX
        self.assertEqual(ra.usage_visible_count(0), 0)
        self.assertEqual(ra.usage_visible_count(1), 1)
        self.assertEqual(ra.usage_visible_count(cap), cap)
        self.assertEqual(ra.usage_visible_count(cap + 4), cap)


class MoveJudgementTest(unittest.TestCase):
    """入退団の区分。チームは A=1・B=2。同じ年に始まる在籍は、その年に終わった方が先、最後は stint_id の順。"""

    A, B = 1, 2

    @staticmethod
    def span(stint_id, team_id, from_year, to_year=None):
        return ra.StintSpan(stint_id, team_id, from_year, to_year)

    def test_first_stint_is_a_new_signing(self):
        own = self.span(1, self.A, 2026)
        judgement = ra.judge_join(own, [own])
        self.assertEqual((judgement.kind, judgement.other_team_id), (ra.MoveKind.NEW_SIGNING, None))

    def test_joining_after_another_team_is_a_transfer_with_the_previous_team(self):
        before = self.span(1, self.B, 2020, 2025)
        own = self.span(2, self.A, 2026)
        judgement = ra.judge_join(own, [before, own])
        self.assertEqual((judgement.kind, judgement.other_team_id), (ra.MoveKind.TRANSFER, self.B))

    def test_previous_team_is_the_immediately_preceding_stint(self):
        first = self.span(1, 3, 2018, 2019)
        second = self.span(2, self.B, 2020, 2025)
        own = self.span(3, self.A, 2026)
        self.assertEqual(ra.judge_join(own, [first, second, own]).other_team_id, self.B)

    def test_coming_back_to_the_same_team_is_a_rejoin(self):
        before = self.span(1, self.A, 2018, 2020)
        own = self.span(2, self.A, 2024)
        judgement = ra.judge_join(own, [before, own])
        self.assertEqual((judgement.kind, judgement.other_team_id), (ra.MoveKind.REJOINED, None))

    def test_later_stints_do_not_make_a_join_a_transfer(self):
        own = self.span(1, self.A, 2026, 2026)
        after = self.span(2, self.B, 2027)
        self.assertIs(ra.judge_join(own, [own, after]).kind, ra.MoveKind.NEW_SIGNING)

    def test_mid_season_move_is_a_transfer_on_both_sides(self):
        left = self.span(1, self.A, 2020, 2026)
        joined = self.span(2, self.B, 2026)
        self.assertEqual(ra.judge_join(joined, [left, joined]), ra.MoveJudgement(ra.MoveKind.TRANSFER, self.A))
        self.assertEqual(ra.judge_leave(left, [left, joined]), ra.MoveJudgement(ra.MoveKind.TRANSFER, self.B))

    def test_leaving_with_no_following_stint_is_a_departure(self):
        own = self.span(1, self.A, 2020, 2026)
        self.assertEqual(ra.judge_leave(own, [own]), ra.MoveJudgement(ra.MoveKind.DEPARTED))

    def test_next_year_start_at_another_team_is_a_transfer_but_two_years_later_is_not(self):
        own = self.span(1, self.A, 2020, 2026)
        cases = [(2026, ra.MoveKind.TRANSFER), (2027, ra.MoveKind.TRANSFER), (2028, ra.MoveKind.DEPARTED)]
        for from_year, expected in cases:
            with self.subTest(from_year=from_year):
                other = self.span(2, self.B, from_year)
                self.assertIs(ra.judge_leave(own, [own, other]).kind, expected)

    def test_following_stint_at_the_same_team_is_not_a_transfer(self):
        own = self.span(1, self.A, 2020, 2026)
        again = self.span(2, self.A, 2027)
        self.assertIs(ra.judge_leave(own, [own, again]).kind, ra.MoveKind.DEPARTED)

    def test_earlier_stints_do_not_make_a_leave_a_transfer(self):
        earlier = self.span(1, self.B, 2015, 2019)
        own = self.span(2, self.A, 2020, 2026)
        self.assertIs(ra.judge_leave(own, [earlier, own]).kind, ra.MoveKind.DEPARTED)

    def test_same_year_stints_are_ordered_by_id(self):
        first = self.span(1, self.A, 2026, 2026)
        second = self.span(2, self.B, 2026)
        self.assertEqual(ra.judge_leave(first, [first, second]).other_team_id, self.B)
        self.assertEqual(ra.judge_join(second, [first, second]).other_team_id, self.A)

    def test_same_year_stint_that_ended_comes_first_even_if_registered_later(self):
        """シーズン途中の移籍の経歴を後から足しても（移籍元の id が大きくても）、移籍元を先とみなす。"""
        joined = self.span(1, self.B, 2026)
        left = self.span(2, self.A, 2026, 2026)
        self.assertEqual(ra.judge_join(joined, [joined, left]), ra.MoveJudgement(ra.MoveKind.TRANSFER, self.A))
        self.assertEqual(ra.judge_leave(left, [joined, left]), ra.MoveJudgement(ra.MoveKind.TRANSFER, self.B))

    def test_join_and_leave_use_the_same_window_for_a_transfer(self):
        """間が1年以上空いた別球団からの加入は、退団側と同じく移籍とみなさない（同じ動きを両側で同じ区分にする）。"""
        # (前の球団を退団した年, 加入側の区分, 退団側の区分)。加入は2026年
        cases = [
            (2025, ra.MoveKind.TRANSFER, ra.MoveKind.TRANSFER),
            (2024, ra.MoveKind.NEW_SIGNING, ra.MoveKind.DEPARTED),
        ]
        for left_year, join_kind, leave_kind in cases:
            with self.subTest(left_year=left_year):
                before = self.span(1, self.B, 2018, left_year)
                own = self.span(2, self.A, 2026)
                self.assertIs(ra.judge_join(own, [before, own]).kind, join_kind)
                self.assertIs(ra.judge_leave(before, [before, own]).kind, leave_kind)

    def test_duplicate_stints_in_the_same_team_and_year_do_not_raise(self):
        one = self.span(1, self.A, 2026, 2026)
        two = self.span(2, self.A, 2026, 2026)
        self.assertIs(ra.judge_join(two, [one, two]).kind, ra.MoveKind.REJOINED)
        self.assertIs(ra.judge_leave(one, [one, two]).kind, ra.MoveKind.DEPARTED)

    def test_order_is_kind_then_number(self):
        rows = [
            (ra.MoveKind.REJOINED, 1),
            (ra.MoveKind.TRANSFER, 30),
            (ra.MoveKind.NEW_SIGNING, 99),
            (ra.MoveKind.TRANSFER, 5),
        ]
        rows.sort(key=lambda r: ra.move_order(*r))
        self.assertEqual(
            rows,
            [
                (ra.MoveKind.NEW_SIGNING, 99),
                (ra.MoveKind.TRANSFER, 5),
                (ra.MoveKind.TRANSFER, 30),
                (ra.MoveKind.REJOINED, 1),
            ],
        )
