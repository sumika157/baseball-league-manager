"""育成の昇格と育成契約の満了（`domain/pennant/development.py`。#170 P1）と、締める処理への組み込み。DB 不要。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from myapp.domain.pennant.development import (
    DEVELOPMENT_CONTRACT_YEARS,
    MAX_PROMOTIONS,
    PROMOTION_MARGIN,
    RESIGN_MARGIN,
    confirm_promotions,
    decide_expirations,
    decide_promotions,
    development_prospect_value,
    development_start_year,
    is_contract_expiring,
)
from myapp.domain.pennant.draft import USUAL_DRAFTEES, draftee_count
from myapp.domain.pennant.offseason import OffseasonPlayer, plan_departures
from myapp.domain.pennant.release import decide_releases, release_counts
from myapp.domain.pennant.roster import OPENING_REGISTERED_BAND
from myapp.domain.value_objects import ContractStatus, DraftPick, Position

from .test_release import SEED, START_YEAR, YEAR, club, member, roster

DEVELOPMENT = ContractStatus.DEVELOPMENTAL


def prospect(player_id: int, value: int, *, entered_year: int = YEAR, position: Position = Position.PITCHER):
    """育成ドラフトで `entered_year` 年に入った30歳の育成（見込み＝今の能力。`PEAK_AGE` 以上）。"""
    player = member(player_id, position=position, value=value, contract=DEVELOPMENT, entered_year=entered_year)
    profile = replace(player.profile, draft=DraftPick(entered_year - 1, is_development=True))
    return replace(player, profile=profile, number=str(100 + player_id))


def departures(players: list[OffseasonPlayer], **kwargs):
    return plan_departures([club(players)], year=YEAR, world_seed=SEED, start_year=START_YEAR, **kwargs)


class DevelopmentStartYearTest(unittest.TestCase):
    def test_development_draftee_starts_in_the_entry_year(self) -> None:
        self.assertEqual(development_start_year(prospect(1, 40, entered_year=2020)), 2020)

    def test_others_start_in_the_current_stint(self) -> None:
        # 本指名で入った選手が育成で在籍している（自由契約から育成で再び入った）なら、今の在籍の開始年
        player = replace(member(1, contract=DEVELOPMENT, entered_year=2018), joined_year=2025)
        self.assertEqual(development_start_year(player), 2025)


class ContractExpiryTest(unittest.TestCase):
    def test_expires_after_the_third_season(self) -> None:
        self.assertEqual(DEVELOPMENT_CONTRACT_YEARS, 3)
        self.assertFalse(is_contract_expiring(2025, 2026))
        self.assertTrue(is_contract_expiring(2025, 2027))

    def test_expires_every_year_after_resigning(self) -> None:
        # 再契約は1シーズンごと（NPB）。3年を過ぎた育成は毎年が満了の年
        self.assertTrue(is_contract_expiring(2025, 2028))
        self.assertTrue(is_contract_expiring(2020, 2030))


class DecidePromotionsTest(unittest.TestCase):
    def wished(self, players: list[OffseasonPlayer]) -> tuple[int, ...]:
        return decide_promotions(club(players), players, year=YEAR, start_year=START_YEAR, world_seed=SEED)

    def test_no_development_players(self) -> None:
        self.assertEqual(self.wished(roster(64)), ())

    def test_promotes_a_prospect_above_the_player_pushed_out(self) -> None:
        # 支配下は全員 50。入れ替わる選手（50）＋余裕を上回る育成だけ
        strong = prospect(200, int(50 + PROMOTION_MARGIN) + 2)
        weak = prospect(201, int(50 + PROMOTION_MARGIN))
        self.assertEqual(self.wished([*roster(64), strong, weak]), (200,))

    def test_at_most_max_promotions_in_value_order(self) -> None:
        prospects = [prospect(200 + index, 60 + index) for index in range(MAX_PROMOTIONS + 2)]
        wished = self.wished([*roster(64), *prospects])
        self.assertEqual(len(wished), MAX_PROMOTIONS)
        self.assertEqual(wished, tuple(200 + index for index in reversed(range(2, MAX_PROMOTIONS + 2))))

    def test_compares_with_the_next_player_in_the_release_order(self) -> None:
        # 支配下の弱い1人（40）が先に外れる順。ふつうの戦力外（4人）の後ろは 50 なので、45 の育成は上がらない
        players = roster(64)
        players[0] = replace(players[0], ratings=member(1, value=40, position=players[0].position).ratings)
        self.assertEqual(self.wished([*players, prospect(200, 45)]), ())


class ConfirmPromotionsTest(unittest.TestCase):
    def test_keeps_the_usual_draftees(self) -> None:
        # 戦力外の後に支配下 60人（ふつうの新人 6人で上端）。昇格は削り、新人は削らない
        players = [*roster(60), prospect(200, 70)]
        self.assertEqual(confirm_promotions(club(players), players, (200,), year=YEAR, world_seed=SEED), ())

    def test_numbers_are_free_registered_numbers(self) -> None:
        players = [*roster(58), prospect(200, 70), prospect(201, 69)]
        promoted = confirm_promotions(club(players), players, (200, 201), year=YEAR, world_seed=SEED)
        self.assertEqual([item.player_id for item in promoted], [200, 201])
        used = {player.number for player in players}
        numbers = [item.number for item in promoted]
        self.assertEqual(len(set(numbers)), 2)
        for number in numbers:
            self.assertNotIn(number, used)
            self.assertLessEqual(int(number), 99)
        self.assertEqual(
            draftee_count(60, registered_limit=70), draftee_count(58, registered_limit=70), "新人の人数は変わらない"
        )


class DecideExpirationsTest(unittest.TestCase):
    def expired(self, players: list[OffseasonPlayer], promoted: tuple[int, ...] = ()) -> tuple[int, ...]:
        return decide_expirations(
            club(players), players, promoted=promoted, year=YEAR, start_year=START_YEAR, world_seed=SEED
        )

    def test_releases_only_weak_players_whose_contract_expires(self) -> None:
        bar = development_prospect_value(Position.PITCHER) - RESIGN_MARGIN
        first = YEAR - DEVELOPMENT_CONTRACT_YEARS + 1
        weak_expiring = prospect(200, int(bar) - 2, entered_year=first)
        strong_expiring = prospect(201, int(bar) + 2, entered_year=first)
        weak_second_year = prospect(202, int(bar) - 2, entered_year=first + 1)
        self.assertEqual(self.expired([*roster(10), weak_expiring, strong_expiring, weak_second_year]), (200,))

    def test_promoted_players_do_not_expire(self) -> None:
        weak = prospect(200, 20, entered_year=2015)
        self.assertEqual(self.expired([weak], promoted=(200,)), ())


class PlanDeparturesTest(unittest.TestCase):
    def test_releases_make_room_for_promotions_on_top_of_the_draftees(self) -> None:
        players = [*roster(64), prospect(200, 70), prospect(201, 68)]
        result = departures(players)
        self.assertEqual([item.player_id for item in result.promotions], [200, 201])
        registered = sum(1 for player in players if player.is_registered and player.player_id not in result.retired)
        usual = release_counts(registered=registered, registered_limit=70).registered
        self.assertEqual(len(result.released), usual + 2, "戦力外が昇格の分も空ける（上乗せ）")
        team = result.draft_teams[0]
        self.assertEqual(team.registered_count, OPENING_REGISTERED_BAND.high - USUAL_DRAFTEES)
        self.assertEqual(team.development_count, 0)
        promoted = {player.player_id: player for player in result.survivors if player.player_id in (200, 201)}
        for promotion in result.promotions:
            player = promoted[promotion.player_id]
            self.assertTrue(player.is_registered)
            self.assertEqual(player.number, promotion.number)
            self.assertEqual(player.promoted_year, YEAR + 1)
            self.assertIn(promotion.number, team.used_numbers)
        self.assertNotIn("300", team.used_numbers, "昇格前の番号は空く")

    def test_without_development_players_nothing_changes(self) -> None:
        # 昇格も満了も無い球団は今までと同じ戦力外
        players = roster(64)
        result = departures(players)
        self.assertEqual(result.promotions, ())
        self.assertEqual(result.expired, ())
        remaining = [player for player in players if player.player_id not in result.retired]
        expected = decide_releases(club(players), remaining, year=YEAR, start_year=START_YEAR, world_seed=SEED)
        self.assertEqual(result.released, tuple(sorted(expected)))

    def test_players_promoted_this_year_are_not_released(self) -> None:
        # 締める年に上がった選手（オフの昇格は翌年の年で書く）は、弱い控えでも戦力外にしない
        players = roster(64)
        players[0] = replace(players[0], ratings=member(1, value=20).ratings, promoted_year=YEAR)
        self.assertNotIn(1, departures(players).released)

    def test_expired_players_leave_and_free_the_development_count(self) -> None:
        weak = prospect(200, 10, entered_year=2015)
        result = departures([*roster(64), weak])
        self.assertEqual(result.expired, (200,))
        self.assertNotIn(200, {player.player_id for player in result.survivors})
        self.assertEqual(result.draft_teams[0].development_count, 0)

    def test_manual_releases_still_keep_the_draftees(self) -> None:
        # 手動で0人を指名した球団（支配下 64人）は空きが新人の分しかないので、昇格しない
        players = [*roster(64), prospect(200, 70)]
        result = departures(players, manual_releases={1: frozenset()})
        self.assertEqual(result.promotions, ())


if __name__ == "__main__":
    unittest.main()
