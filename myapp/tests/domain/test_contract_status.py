"""契約区分（支配下／育成）の単体テスト。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain.entities import Stint, Team
from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
    InvalidContract,
    RegisteredPlayerLimitExceeded,
)
from myapp.domain.value_objects import ContractStatus, JerseyNumber, Position

REGISTERED = ContractStatus.REGISTERED
DEVELOPMENTAL = ContractStatus.DEVELOPMENTAL


class ContractStatusTest(TestCase):
    def test_labels_are_the_single_source_of_choices(self):
        self.assertEqual(ContractStatus.labels(), ["支配下", "育成"])

    def test_from_label_round_trips(self):
        self.assertIs(ContractStatus.from_label("育成"), DEVELOPMENTAL)
        self.assertIs(ContractStatus.from_label("支配下"), REGISTERED)

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(InvalidContract):
            ContractStatus.from_label("契約外")

    def test_developmental_needs_three_digits_and_registered_needs_two(self):
        DEVELOPMENTAL.ensure_number_fits(JerseyNumber(100))
        REGISTERED.ensure_number_fits(JerseyNumber(99))
        with self.assertRaises(InvalidContract):
            DEVELOPMENTAL.ensure_number_fits(JerseyNumber(99))
        with self.assertRaises(InvalidContract):
            REGISTERED.ensure_number_fits(JerseyNumber(100))


class StintContractTest(TestCase):
    def _stint(self, **kwargs):
        defaults = {"team_id": 1, "number": JerseyNumber(120), "from_year": 2024, "signed_as": DEVELOPMENTAL}
        return Stint(**{**defaults, **kwargs})

    def test_registered_signing_is_always_registered(self):
        stint = self._stint(number=JerseyNumber(10), signed_as=REGISTERED)

        self.assertIs(stint.contract_in(2020), REGISTERED)
        self.assertIs(stint.contract_now, REGISTERED)

    def test_developmental_without_promotion_stays_developmental(self):
        stint = self._stint()

        self.assertIs(stint.contract_in(2030), DEVELOPMENTAL)
        self.assertIs(stint.contract_now, DEVELOPMENTAL)

    def test_the_year_of_promotion_is_registered_and_the_year_before_is_not(self):
        stint = self._stint(number=JerseyNumber(30), promoted_year=2026, number_before_promotion=JerseyNumber(120))

        self.assertIs(stint.contract_in(2025), DEVELOPMENTAL)
        self.assertIs(stint.contract_in(2026), REGISTERED)
        self.assertIs(stint.contract_in(2027), REGISTERED)
        self.assertIs(stint.contract_now, REGISTERED)

    def test_promotion_year_before_joining_is_rejected(self):
        with self.assertRaises(InvalidContract):
            self._stint(promoted_year=2023, number_before_promotion=JerseyNumber(120))

    def test_promotion_year_after_leaving_is_rejected(self):
        with self.assertRaises(InvalidContract):
            self._stint(to_year=2025, promoted_year=2026, number_before_promotion=JerseyNumber(120))

    def test_promotion_year_is_allowed_up_to_the_leaving_year(self):
        stint = self._stint(
            number=JerseyNumber(30), to_year=2026, promoted_year=2026, number_before_promotion=JerseyNumber(120)
        )

        self.assertIs(stint.contract_in(2026), REGISTERED)

    def test_registered_signing_cannot_have_a_promotion_year(self):
        with self.assertRaises(InvalidContract):
            self._stint(
                number=JerseyNumber(10),
                signed_as=REGISTERED,
                promoted_year=2025,
                number_before_promotion=JerseyNumber(120),
            )

    def test_number_must_match_the_current_contract(self):
        self._stint().ensure_number_matches_contract()
        self._stint(
            number=JerseyNumber(30), promoted_year=2025, number_before_promotion=JerseyNumber(120)
        ).ensure_number_matches_contract()
        with self.assertRaises(InvalidContract):
            self._stint(number=JerseyNumber(30)).ensure_number_matches_contract()
        with self.assertRaises(InvalidContract):
            self._stint(
                number=JerseyNumber(120), promoted_year=2025, number_before_promotion=JerseyNumber(120)
            ).ensure_number_matches_contract()


class TeamContractTest(TestCase):
    def setUp(self):
        self.team = Team(name="テストチーム", id=1, league_id=1)

    def _add(self, name, number, contract=REGISTERED, from_year=2026):
        return self.team.add_player(
            name, JerseyNumber(number), Position.INFIELDER, from_year=from_year, contract=contract
        )

    # --- 背番号と区分 ---

    def test_add_player_defaults_to_registered(self):
        player = self._add("山田", 10)

        self.assertIs(self.team.contract_of(player), REGISTERED)

    def test_developmental_with_a_two_digit_number_is_rejected(self):
        with self.assertRaises(InvalidContract):
            self._add("育成", 99, DEVELOPMENTAL)

    def test_registered_with_a_three_digit_number_is_rejected(self):
        with self.assertRaises(InvalidContract):
            self._add("支配下", 100)

    def test_developmental_with_a_three_digit_number_is_accepted(self):
        player = self._add("育成", 100, DEVELOPMENTAL)

        self.assertIs(self.team.contract_of(player), DEVELOPMENTAL)

    def test_changing_a_number_must_keep_matching_the_contract(self):
        developmental = self._add("育成", 100, DEVELOPMENTAL)
        registered = self._add("支配下", 10)

        self.team.change_player_number(developmental, JerseyNumber(101))
        with self.assertRaises(InvalidContract):
            self.team.change_player_number(developmental, JerseyNumber(11))
        with self.assertRaises(InvalidContract):
            self.team.change_player_number(registered, JerseyNumber(110))

    # --- 昇格 ---

    def test_promotion_changes_the_contract_and_the_number_together(self):
        player = self._add("育成", 100, DEVELOPMENTAL, from_year=2024)

        player.id = 7
        self.team.promote_player(7, JerseyNumber(25), 2026)

        stint = self.team.current_stint(player)
        self.assertIs(self.team.contract_of(player), REGISTERED)
        self.assertEqual(player.number, JerseyNumber(25))
        self.assertEqual(stint.number, JerseyNumber(25))
        self.assertEqual(stint.promoted_year, 2026)
        self.assertIs(stint.contract_in(2025), DEVELOPMENTAL)

    def test_promotion_to_a_three_digit_number_is_rejected_and_changes_nothing(self):
        player = self._add("育成", 100, DEVELOPMENTAL)
        player.id = 7

        with self.assertRaises(InvalidContract):
            self.team.promote_player(7, JerseyNumber(150), 2026)

        self.assertIs(self.team.contract_of(player), DEVELOPMENTAL)
        self.assertEqual(player.number, JerseyNumber(100))

    def test_promotion_to_a_number_in_use_is_rejected(self):
        player = self._add("育成", 100, DEVELOPMENTAL)
        player.id = 7
        self._add("先輩", 25)

        with self.assertRaises(DuplicateJerseyNumber):
            self.team.promote_player(7, JerseyNumber(25), 2026)

    def test_registered_player_cannot_be_promoted(self):
        player = self._add("支配下", 10)
        player.id = 7

        with self.assertRaises(InvalidContract):
            self.team.promote_player(7, JerseyNumber(11), 2026)

    def test_promotion_before_joining_is_rejected(self):
        player = self._add("育成", 100, DEVELOPMENTAL, from_year=2026)
        player.id = 7

        with self.assertRaises(InvalidContract):
            self.team.promote_player(7, JerseyNumber(25), 2025)

    def test_a_player_who_has_left_cannot_be_promoted(self):
        player = self._add("育成", 100, DEVELOPMENTAL)
        player.id = 7
        self.team.retire_player(player, 2026)

        with self.assertRaises(InvalidContract):
            self.team.promote_player(7, JerseyNumber(25), 2026)

    # --- 昇格前の背番号 ---

    def test_promotion_keeps_the_number_worn_before(self):
        player = self._add("育成", 100, DEVELOPMENTAL, from_year=2024)
        player.id = 7

        self.team.promote_player(7, JerseyNumber(25), 2026)

        stint = self.team.current_stint(player)
        self.assertEqual(stint.number_before_promotion, JerseyNumber(100))
        self.assertEqual(stint.number_in(2025), JerseyNumber(100))
        self.assertEqual(stint.number_in(2026), JerseyNumber(25))

    # --- 支配下の上限（支配下が1人増えるときの検査） ---

    def test_room_exists_when_one_below_the_limit(self):
        self._add("A", 1)

        self.team.ensure_room_for_registered(2)  # 例外にならない

    def test_no_room_when_exactly_at_the_limit(self):
        self._add("A", 1)
        self._add("B", 2)

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            self.team.ensure_room_for_registered(2)

    def test_developmental_players_do_not_count(self):
        self._add("支配下", 1)
        self._add("育成1", 100, DEVELOPMENTAL)
        self._add("育成2", 101, DEVELOPMENTAL)

        self.assertEqual(self.team.registered_player_count, 1)
        self.team.ensure_room_for_registered(2)  # 例外にならない

    def test_none_means_unlimited(self):
        for number in range(1, 20):
            self._add(f"選手{number}", number)

        self.team.ensure_room_for_registered(None)  # 例外にならない

    def test_players_who_left_do_not_count(self):
        gone = self._add("退団", 1)
        self._add("在籍", 2)
        self.team.retire_player(gone, 2026)

        self.team.ensure_room_for_registered(2)  # 例外にならない

    def test_a_promotion_takes_a_slot(self):
        self._add("支配下", 1)
        player = self._add("育成", 100, DEVELOPMENTAL)
        player.id = 7
        self.team.ensure_room_for_registered(2)  # 育成は数えないので昇格の余地がある

        self.team.promote_player(7, JerseyNumber(25), 2026)

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            self.team.ensure_room_for_registered(2)
        self.team.ensure_room_for_registered(3)  # 例外にならない


class StintNumberPeriodsTest(TestCase):
    def _promoted(self, **kwargs):
        defaults = {
            "team_id": 1,
            "number": JerseyNumber(50),
            "from_year": 2020,
            "signed_as": DEVELOPMENTAL,
            "promoted_year": 2025,
            "number_before_promotion": JerseyNumber(120),
        }
        return Stint(**{**defaults, **kwargs})

    def test_a_promotion_year_needs_the_number_before(self):
        with self.assertRaises(InvalidContract):
            self._promoted(number_before_promotion=None)

    def test_the_number_before_needs_a_promotion_year(self):
        with self.assertRaises(InvalidContract):
            Stint(team_id=1, number=JerseyNumber(120), from_year=2020, signed_as=DEVELOPMENTAL,
                  number_before_promotion=JerseyNumber(110))  # fmt: skip

    def test_the_number_before_must_be_a_developmental_number(self):
        with self.assertRaises(InvalidContract):
            self._promoted(number_before_promotion=JerseyNumber(60)).ensure_number_matches_contract()

    def test_number_in_switches_at_the_promotion_year(self):
        stint = self._promoted()

        self.assertEqual(stint.number_in(2024), JerseyNumber(120))
        self.assertEqual(stint.number_in(2025), JerseyNumber(50))

    def test_a_stint_without_promotion_has_one_period(self):
        stint = Stint(team_id=1, number=JerseyNumber(50), from_year=2020, to_year=2022)

        self.assertEqual(stint.number_periods(), [(JerseyNumber(50), 2020, 2022)])
        self.assertEqual(stint.number_in(2021), JerseyNumber(50))

    def test_a_promoted_stint_has_two_periods(self):
        self.assertEqual(
            self._promoted().number_periods(),
            [(JerseyNumber(120), 2020, 2024), (JerseyNumber(50), 2025, None)],
        )

    def test_promotion_in_the_joining_year_has_no_period_before(self):
        self.assertEqual(self._promoted(promoted_year=2020).number_periods(), [(JerseyNumber(50), 2020, None)])

    def test_the_number_worn_before_promotion_does_not_clash_with_a_later_wearer(self):
        # 2015〜2023 に 50 を着けた人がいても、2025 に 50 で昇格した選手と期間が重ならない
        earlier = Stint(team_id=1, number=JerseyNumber(50), from_year=2015, to_year=2023)

        self.assertIsNone(self._promoted().shared_number(earlier))

    def test_the_number_worn_before_promotion_clashes_when_periods_overlap(self):
        other = Stint(team_id=1, number=JerseyNumber(120), from_year=2022, to_year=2023)

        self.assertEqual(self._promoted().shared_number(other), JerseyNumber(120))

    def test_the_new_number_clashes_with_a_current_wearer(self):
        other = Stint(team_id=1, number=JerseyNumber(50), from_year=2024)

        self.assertEqual(self._promoted().shared_number(other), JerseyNumber(50))


class EnsurePromotableTest(TestCase):
    def setUp(self):
        self.team = Team(name="テストチーム", id=1, league_id=1)

    def test_a_registered_player_is_not_promotable(self):
        player = self.team.add_player("支配下", JerseyNumber(10), Position.INFIELDER, from_year=2026)
        player.id = 7

        with self.assertRaisesRegex(InvalidContract, "育成選手ではない"):
            self.team.ensure_promotable(7)

    def test_a_developmental_player_is_promotable(self):
        player = self.team.add_player(
            "育成", JerseyNumber(100), Position.INFIELDER, from_year=2026, contract=DEVELOPMENTAL
        )
        player.id = 7

        self.team.ensure_promotable(7)  # 例外にならない
