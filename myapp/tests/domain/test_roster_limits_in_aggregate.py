"""移籍の受け入れと、支配下の上限・外国人枠の検査が `Team` 集約の中にあること。Django も DB も使わない。

呼び出し側が事前に検査する約束だと、呼び忘れた経路が黙って上限を超える。上限を操作の引数で受け、
集約の中で検査するので、**呼び忘れても超えない**ことをここで確かめる。
"""

from dataclasses import replace
from unittest import TestCase

from myapp.domain.entities import FreeAgentDeclaration, League, Player, Team
from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
    ForeignPlayerQuotaExceeded,
    InvalidAcquisition,
    InvalidContract,
    RegisteredPlayerLimitExceeded,
)
from myapp.domain.value_objects import (
    AcquisitionRoute,
    ContractStatus,
    FreeAgencyKind,
    JerseyNumber,
    Position,
    RosterLimits,
)

UNLIMITED = RosterLimits.UNLIMITED
REGISTERED = ContractStatus.REGISTERED
DEVELOPMENTAL = ContractStatus.DEVELOPMENTAL


def limits(registered=None, foreign=None) -> RosterLimits:
    return RosterLimits(registered=registered, foreign=foreign)


class RosterLimitsTest(TestCase):
    def test_the_league_hands_over_both_limits(self):
        league = League(name="L", registered_player_limit=70, foreign_player_roster_limit=4)

        self.assertEqual(league.roster_limits, RosterLimits(registered=70, foreign=4))

    def test_unlimited_is_explicit_and_checks_nothing(self):
        self.assertEqual(RosterLimits.UNLIMITED, RosterLimits(registered=None, foreign=None))


class AddPlayerChecksTheLimitTest(TestCase):
    """`add_player` を直接呼んでも、上限いっぱいのチームには入れない（呼び出し側の事前検査に頼らない）。"""

    def setUp(self):
        self.team = Team(name="A", id=1, league_id=1)
        self.team.add_player("先客", JerseyNumber("5"), Position.PITCHER, 2026, limits=UNLIMITED)

    def test_a_registered_player_is_refused_at_the_limit(self):
        with self.assertRaises(RegisteredPlayerLimitExceeded):
            self.team.add_player("新人", JerseyNumber("6"), Position.PITCHER, 2026, limits=limits(registered=1))
        self.assertEqual(len(self.team.players), 1)

    def test_a_developmental_player_is_accepted_at_the_limit(self):
        self.team.add_player(
            "育成", JerseyNumber("120"), Position.PITCHER, 2026, contract=DEVELOPMENTAL, limits=limits(registered=1)
        )

        self.assertEqual(len(self.team.players), 2)

    def test_unlimited_accepts(self):
        self.team.add_player("新人", JerseyNumber("6"), Position.PITCHER, 2026, limits=UNLIMITED)

        self.assertEqual(len(self.team.players), 2)


class PromoteChecksTheLimitTest(TestCase):
    def test_promotion_is_refused_at_the_limit_without_a_prior_check(self):
        team = Team(name="A", id=1, league_id=1)
        team.add_player("先客", JerseyNumber("5"), Position.PITCHER, 2026, limits=UNLIMITED)
        trainee = team.add_player(
            "育成", JerseyNumber("120"), Position.PITCHER, 2026, contract=DEVELOPMENTAL, limits=UNLIMITED
        )
        trainee.id = 9

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            team.promote_player(9, JerseyNumber("30"), 2026, limits=limits(registered=1))

        self.assertIs(team.contract_of(trainee), DEVELOPMENTAL)


class AcceptTransferTest(TestCase):
    """`Team.accept_transfer`: 受け入れの検査と在籍の追加は集約が行う。"""

    def setUp(self):
        self.source = Team(name="元", id=1, league_id=1)
        self.destination = Team(name="先", id=2, league_id=1)
        self.player = self.source.add_player("山田", JerseyNumber("10"), Position.INFIELDER, 2020, limits=UNLIMITED)
        self.player.id = 5
        self.source.retire_player(self.player, 2025)

    def accept(self, number=7, contract=REGISTERED, via=None, year=2026, lim=UNLIMITED, player=None) -> None:
        self.destination.accept_transfer(player or self.player, JerseyNumber(number), year, contract, via, limits=lim)

    def test_it_opens_a_new_stint_and_activates_the_player(self):
        self.accept(via=AcquisitionRoute.TRADE)

        stint = self.destination.current_stint(self.player)
        self.assertEqual(
            (stint.from_year, stint.number.value, stint.acquired_via), (2026, "7", AcquisitionRoute.TRADE)
        )
        self.assertTrue(self.player.is_active)
        self.assertEqual(self.player.number.value, "7")
        self.assertIn(self.player, self.destination.players)
        self.assertEqual(len(self.player.career), 2)

    def test_a_duplicate_number_is_refused(self):
        self.destination.add_player("先客", JerseyNumber("7"), Position.PITCHER, 2020, limits=UNLIMITED)

        with self.assertRaises(DuplicateJerseyNumber):
            self.accept(number=7)

    def test_a_number_that_does_not_match_the_contract_is_refused(self):
        with self.assertRaises(InvalidContract):
            self.accept(number=7, contract=DEVELOPMENTAL)
        with self.assertRaises(InvalidContract):
            self.accept(number=150, contract=REGISTERED)

    def test_the_registered_limit_is_checked_for_a_registered_signing_only(self):
        self.destination.add_player("先客", JerseyNumber("5"), Position.PITCHER, 2020, limits=UNLIMITED)

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            self.accept(lim=limits(registered=1))
        self.accept(number=150, contract=DEVELOPMENTAL, lim=limits(registered=1))

    def test_the_foreign_roster_limit_is_checked(self):
        self.player.profile = replace(self.player.profile, is_foreign_player=True)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.accept(lim=limits(foreign=0))

    def test_a_free_agent_signing_needs_the_declaration(self):
        with self.assertRaises(InvalidAcquisition):
            self.accept(via=AcquisitionRoute.FREE_AGENT)

    def test_a_free_agent_signing_the_year_after_the_declaration_is_accepted(self):
        self.player.fa_declarations.append(FreeAgentDeclaration(2025, FreeAgencyKind.DOMESTIC))

        self.accept(via=AcquisitionRoute.FREE_AGENT)

        self.assertIs(self.destination.current_stint(self.player).acquired_via, AcquisitionRoute.FREE_AGENT)

    def test_a_route_that_conflicts_with_the_contract_is_refused(self):
        with self.assertRaises(InvalidAcquisition):
            self.accept(number=150, contract=DEVELOPMENTAL, via=AcquisitionRoute.DRAFT)

    def test_a_declaration_the_stints_no_longer_cover_is_refused(self):
        other = Player(name="宣言", number=JerseyNumber("11"), position=Position.PITCHER)
        other.career = []
        other.fa_declarations.append(FreeAgentDeclaration(2024, FreeAgencyKind.DOMESTIC))

        from myapp.domain.exceptions import InvalidFreeAgentDeclaration

        with self.assertRaises(InvalidFreeAgentDeclaration):
            self.accept(player=other)
