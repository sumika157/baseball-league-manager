"""外国人枠（野球協約 第82条の2。1軍登録は計4人・うち投手3人・野手3人）の単体テスト。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain.entities import League
from myapp.domain.exceptions import DomainError, ForeignPlayerQuotaExceeded, RegisteredPlayerLimitExceeded
from myapp.domain.services.foreign_quota import counts_toward_foreign_quota, foreign_quota_exempt_from
from myapp.domain.value_objects import ForeignPlayerRules, Position, ensure_quota_not_exceeded

NPB = ForeignPlayerRules.NPB


class EnsureQuotaNotExceededTest(TestCase):
    """支配下の上限が使う、上限1つの判定。"""

    def test_allows_none_limit(self):
        ensure_quota_not_exceeded(1000, None, "エラー", RegisteredPlayerLimitExceeded)  # 例外にならない

    def test_allows_exactly_at_the_limit(self):
        ensure_quota_not_exceeded(3, 3, "エラー", RegisteredPlayerLimitExceeded)  # 例外にならない

    def test_raises_the_given_error_when_count_exceeds_limit(self):
        with self.assertRaises(RegisteredPlayerLimitExceeded):
            ensure_quota_not_exceeded(4, 3, "エラー", RegisteredPlayerLimitExceeded)


class ForeignPlayerRulesTest(TestCase):
    def test_the_npb_rules_follow_the_agreement(self):
        self.assertEqual((NPB.active, NPB.pitchers, NPB.batters), (4, 3, 3))
        self.assertEqual(NPB.label, "計4人・投手3人・野手3人")

    def test_a_league_follows_the_agreement_by_default(self):
        self.assertEqual(League(name="L").foreign_rules, NPB)

    def test_four_foreigners_split_two_and_two_or_three_and_one_are_within(self):
        for pitchers, batters in ((2, 2), (3, 1), (1, 3), (0, 3), (3, 0)):
            with self.subTest(pitchers=pitchers, batters=batters):
                NPB.ensure_within(pitchers=pitchers, batters=batters, subject="1軍登録")  # 例外にならない
                self.assertIsNone(NPB.violation(pitchers=pitchers, batters=batters))

    def test_the_fifth_foreigner_is_refused(self):
        with self.assertRaises(ForeignPlayerQuotaExceeded) as caught:
            NPB.ensure_within(pitchers=2, batters=3, subject="1軍登録")
        self.assertEqual(str(caught.exception), "1軍登録の外国人選手は計4人までです（5人）。")

    def test_the_fourth_pitcher_is_refused(self):
        with self.assertRaises(ForeignPlayerQuotaExceeded) as caught:
            NPB.ensure_within(pitchers=4, batters=0, subject="1軍登録")
        self.assertEqual(str(caught.exception), "1軍登録の外国人選手は投手3人までです（4人）。")

    def test_the_fourth_batter_is_refused(self):
        with self.assertRaises(ForeignPlayerQuotaExceeded) as caught:
            NPB.ensure_within(pitchers=0, batters=4, subject="オーダー")
        self.assertEqual(str(caught.exception), "オーダーの外国人選手は野手3人までです（4人）。")

    def test_none_is_unlimited(self):
        ForeignPlayerRules.UNLIMITED.ensure_within(pitchers=20, batters=20, subject="1軍登録")  # 例外にならない
        self.assertEqual(ForeignPlayerRules.UNLIMITED.label, "無制限")
        only_total = ForeignPlayerRules(active=4, pitchers=None, batters=None)
        only_total.ensure_within(pitchers=4, batters=0, subject="1軍登録")  # 例外にならない
        self.assertEqual(only_total.label, "計4人")

    def test_a_negative_limit_is_refused(self):
        with self.assertRaises(DomainError):
            ForeignPlayerRules(active=-1, pitchers=3, batters=3)

    def test_has_room_counts_the_category_and_the_total(self):
        self.assertTrue(NPB.has_room(pitchers=2, batters=1, is_pitcher=True))
        self.assertFalse(NPB.has_room(pitchers=3, batters=0, is_pitcher=True), "投手の枠")
        self.assertTrue(NPB.has_room(pitchers=3, batters=0, is_pitcher=False))
        self.assertFalse(NPB.has_room(pitchers=1, batters=3, is_pitcher=False), "野手の枠")
        self.assertFalse(NPB.has_room(pitchers=2, batters=2, is_pitcher=True), "計の枠")

    def test_batter_room_is_the_smaller_of_the_total_and_the_batter_limits(self):
        self.assertEqual(NPB.batter_room(pitchers=0, batters=0), 3)
        self.assertEqual(NPB.batter_room(pitchers=2, batters=0), 2)
        self.assertEqual(NPB.batter_room(pitchers=0, batters=3), 0)
        self.assertIsNone(ForeignPlayerRules.UNLIMITED.batter_room(pitchers=5, batters=5))

    def test_strictest_takes_the_minimum_of_each_component(self):
        loose = ForeignPlayerRules(active=5, pitchers=None, batters=2)
        tight = ForeignPlayerRules(active=4, pitchers=3, batters=3)

        self.assertEqual(
            ForeignPlayerRules.strictest([loose, tight, ForeignPlayerRules.UNLIMITED]),
            ForeignPlayerRules(active=4, pitchers=3, batters=2),
        )
        self.assertEqual(ForeignPlayerRules.strictest([]), ForeignPlayerRules.UNLIMITED)


class CountByRegisteredPositionTest(TestCase):
    """投手か野手かを数える入口は `ForeignPlayerRules.counts`（登録位置）。"""

    def test_only_a_registered_pitcher_counts_as_a_pitcher(self):
        positions = [Position.PITCHER, Position.CATCHER, Position.INFIELDER, Position.OUTFIELDER]
        positions.append(Position.DESIGNATED_HITTER)

        self.assertEqual(ForeignPlayerRules.counts(positions), (1, 4))

    def test_ensure_players_within_counts_the_positions(self):
        NPB.ensure_players_within([Position.PITCHER] * 3 + [Position.OUTFIELDER], subject="1軍登録")  # 例外にならない
        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, "投手3人までです（4人）"):
            NPB.ensure_players_within([Position.PITCHER] * 4, subject="1軍登録")

    def test_has_room_for_a_position(self):
        self.assertFalse(NPB.has_room_for([Position.PITCHER] * 3, Position.PITCHER))
        self.assertTrue(NPB.has_room_for([Position.PITCHER] * 3, Position.CATCHER))


class ForeignQuotaExemptFromTest(TestCase):
    """FA 資格の取得で外国人枠の対象外になる年（協約 第82条 (4)。公示の次の年度のシーズンから）。"""

    def test_the_year_after_the_acquisition(self):
        self.assertEqual(foreign_quota_exempt_from(acquired_year=2033, declared_years=()), 2034)

    def test_the_year_after_the_declaration(self):
        self.assertEqual(foreign_quota_exempt_from(acquired_year=None, declared_years=[2030]), 2031)

    def test_the_earlier_of_the_two(self):
        self.assertEqual(foreign_quota_exempt_from(acquired_year=2033, declared_years=[2035, 2031]), 2032)
        self.assertEqual(foreign_quota_exempt_from(acquired_year=2029, declared_years=[2031]), 2030)

    def test_none_without_either(self):
        self.assertIsNone(foreign_quota_exempt_from(acquired_year=None, declared_years=()))


class CountsTowardForeignQuotaTest(TestCase):
    def test_counts_in_the_acquisition_year_and_not_from_the_next_year(self):
        exempt_from = foreign_quota_exempt_from(acquired_year=2033, declared_years=())
        self.assertTrue(counts_toward_foreign_quota(is_foreign_player=True, exempt_from=exempt_from, year=2033))
        self.assertFalse(counts_toward_foreign_quota(is_foreign_player=True, exempt_from=exempt_from, year=2034))
        self.assertFalse(counts_toward_foreign_quota(is_foreign_player=True, exempt_from=exempt_from, year=2040))

    def test_a_foreigner_without_exemption_counts(self):
        self.assertTrue(counts_toward_foreign_quota(is_foreign_player=True, exempt_from=None, year=2040))

    def test_a_player_without_the_flag_never_counts(self):
        for exempt_from in (None, 2030):
            with self.subTest(exempt_from=exempt_from):
                self.assertFalse(
                    counts_toward_foreign_quota(is_foreign_player=False, exempt_from=exempt_from, year=2026)
                )
