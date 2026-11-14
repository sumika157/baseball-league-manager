"""シーズン中の補強の窓（#207）と、期中の支配下登録の照合・背番号・育成の何年目（#170 P3a）。

期待値の根拠: NPB の野球協約（シーズン中に支配下選手を新しく登録できるのは7月31日まで）、#207 の方針
（オフは対象外・判定は世界の今日で）、#170 のユーザーの決定 Q4 A（窓は締めた後（開幕前）から7月31日まで）、
NPB の育成選手に関する規約（契約は3年。満了の判定は `development.is_contract_expiring`）。
"""

import unittest
from datetime import date

from myapp.domain.entities import ensure_room_for_registered
from myapp.domain.exceptions import InvalidContract, RegisteredPlayerLimitExceeded, ReinforcementClosed
from myapp.domain.pennant.development import (
    DEVELOPMENT_CONTRACT_YEARS,
    contract_season,
    contract_start_year,
    is_contract_expiring,
    midseason_promotion_number,
)
from myapp.domain.pennant.reinforcement import (
    MidseasonRegistration,
    RegistrationKind,
    ReinforcementWindow,
    ensure_registration_matches_stint,
)
from myapp.domain.value_objects import AcquisitionRoute, DraftPick

YEAR = 2027


class ReinforcementWindowTest(unittest.TestCase):
    def is_open(self, next_game_on: date | None) -> bool:
        return ReinforcementWindow.is_open(next_regular_game_on=next_game_on)

    def test_the_deadline_is_july_31(self):
        """期限の日は7月31日（野球協約）。"""
        self.assertEqual(ReinforcementWindow.deadline(YEAR), date(YEAR, 7, 31))

    def test_it_is_open_from_after_the_close_to_july_31(self):
        """締めた直後（次の公式戦は翌年の開幕日）・開幕日・シーズン中・7月31日の試合の前は開いている（Q4 A）。"""
        for next_game_on in (date(YEAR, 3, 27), date(YEAR, 5, 10), date(YEAR, 7, 30), date(YEAR, 7, 31)):
            with self.subTest(next_game_on=next_game_on):
                self.assertTrue(self.is_open(next_game_on))

    def test_it_is_closed_from_august_1(self):
        """次の公式戦が8月1日以降なら閉じている（7月31日の試合を終えた後）。"""
        for next_game_on in (date(YEAR, 8, 1), date(YEAR, 9, 30)):
            with self.subTest(next_game_on=next_game_on):
                self.assertFalse(self.is_open(next_game_on))

    def test_it_is_closed_without_a_regular_season_game_left(self):
        """公式戦の対戦が残っていない（シーズン終了・ポストシーズン・締める前）なら閉じている。締める処理とは重ならない。"""
        self.assertFalse(self.is_open(None))

    def test_ensure_open_explains_the_reason_in_japanese(self):
        ReinforcementWindow.ensure_open(next_regular_game_on=date(YEAR, 7, 31))
        with self.assertRaisesRegex(ReinforcementClosed, "7月31日"):
            ReinforcementWindow.ensure_open(next_regular_game_on=date(YEAR, 8, 1))
        with self.assertRaisesRegex(ReinforcementClosed, "公式戦が残っていない"):
            ReinforcementWindow.ensure_open(next_regular_game_on=None)

    def test_the_review_dates_are_june_1_and_july_1_inside_the_window(self):
        """AI の判定日は6月1日・7月1日（#170 Q2 A）。どれも期限の日以前。"""
        dates = ReinforcementWindow.review_dates(YEAR)
        self.assertEqual(dates, (date(YEAR, 6, 1), date(YEAR, 7, 1)))
        self.assertTrue(all(self.is_open(day) for day in dates))


def registration(kind: RegistrationKind) -> MidseasonRegistration:
    return MidseasonRegistration(team_id=1, player_id=2, registered_on=date(YEAR, 5, 1), kind=kind)


class RegistrationMatchesStintTest(unittest.TestCase):
    def test_the_registration_year_must_be_the_promotion_year(self):
        """昇格の登録は、在籍の昇格の年＝登録日の年（加入年と経路は見ない）。"""
        promotion = registration(RegistrationKind.PROMOTION)
        ensure_registration_matches_stint(promotion, promoted_year=YEAR, from_year=YEAR - 3, acquired_via=None)
        for promoted_year in (None, YEAR - 1, YEAR + 1):
            with self.subTest(promoted_year=promoted_year), self.assertRaises(InvalidContract):
                ensure_registration_matches_stint(
                    promotion, promoted_year=promoted_year, from_year=YEAR - 3, acquired_via=None
                )

    def test_a_signing_must_join_in_the_registration_year_as_a_new_foreign_player(self):
        """獲得の登録（#169 F3）は、在籍の加入年＝登録日の年で、入団の経路が新外国人。昇格の年は見ない。"""
        signing = registration(RegistrationKind.SIGNING)
        new_foreign = AcquisitionRoute.NEW_FOREIGN
        ensure_registration_matches_stint(signing, promoted_year=None, from_year=YEAR, acquired_via=new_foreign)
        for from_year, route in (
            (YEAR - 1, new_foreign),
            (YEAR + 1, new_foreign),
            (YEAR, AcquisitionRoute.DRAFT),
            (YEAR, None),
        ):
            with self.subTest(from_year=from_year, route=route), self.assertRaises(InvalidContract):
                ensure_registration_matches_stint(signing, promoted_year=None, from_year=from_year, acquired_via=route)


class ContractSeasonTest(unittest.TestCase):
    def test_the_start_is_the_development_draft_entry_year_or_the_stint(self):
        """起点は育成ドラフトの入団の年（指名の翌年）、それ以外（本指名・指名なし）は今の在籍の開始年。"""
        development = DraftPick(year=YEAR - 2, round=3, is_development=True)
        self.assertEqual(contract_start_year(development, joined_year=YEAR), YEAR - 1)
        regular = DraftPick(year=YEAR - 2, round=3)
        self.assertEqual(contract_start_year(regular, joined_year=YEAR), YEAR)
        self.assertEqual(contract_start_year(None, joined_year=YEAR - 4), YEAR - 4)

    def test_the_season_counts_from_one_and_expires_in_the_contract_years(self):
        """起点の年が1年目。`DEVELOPMENT_CONTRACT_YEARS`（3）年目から満了の年。"""
        self.assertEqual(contract_season(YEAR, YEAR), 1)
        last = YEAR + DEVELOPMENT_CONTRACT_YEARS - 1
        self.assertEqual(contract_season(YEAR, last), DEVELOPMENT_CONTRACT_YEARS)
        self.assertFalse(is_contract_expiring(YEAR, last - 1))
        self.assertTrue(is_contract_expiring(YEAR, last))


class MidseasonPromotionNumberTest(unittest.TestCase):
    def test_the_number_is_a_free_registered_number(self):
        """支配下の番号（0・00・1〜99）のうち、使われていない番号。"""
        used = {str(number) for number in range(1, 99)}
        number = midseason_promotion_number(set(used), world_seed=1, year=YEAR, team_id=3, player_id=10)
        self.assertNotIn(number, used)
        self.assertIn(number, {"99", "0", "00"})

    def test_the_same_state_draws_the_same_number(self):
        first = midseason_promotion_number({"1", "2"}, world_seed=1, year=YEAR, team_id=3, player_id=10)
        again = midseason_promotion_number({"1", "2"}, world_seed=1, year=YEAR, team_id=3, player_id=10)
        self.assertEqual(first, again)

    def test_the_drawn_number_is_marked_as_used(self):
        used = {"1"}
        number = midseason_promotion_number(used, world_seed=1, year=YEAR, team_id=3, player_id=10)
        self.assertIn(number, used)


class RoomForRegisteredTest(unittest.TestCase):
    def test_one_more_registered_player_must_fit_the_limit(self):
        """上限70人なら69人までは1人増やせ、70人では増やせない（集約と画面が同じ関数で判定する）。"""
        ensure_room_for_registered("球団", 69, 70)
        ensure_room_for_registered("球団", 500, None)
        with self.assertRaisesRegex(RegisteredPlayerLimitExceeded, "上限（70人）"):
            ensure_room_for_registered("球団", 70, 70)


if __name__ == "__main__":
    unittest.main()
