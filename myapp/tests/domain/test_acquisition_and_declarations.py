"""入団の経路と FA 宣言の単体テスト。Django も DB も使わない。"""

from datetime import date
from unittest import TestCase

from myapp.domain.entities import FreeAgentDeclaration, Player, Stint, Team, declaration_outcome
from myapp.domain.exceptions import InvalidAcquisition, InvalidFreeAgentDeclaration
from myapp.domain.value_objects import (
    AcquisitionRoute,
    ContractStatus,
    FreeAgencyKind,
    FreeAgencyOutcome,
    JerseyNumber,
    Position,
    StintPeriod,
    fa_destinations,
    fa_origin,
    free_agency_outcome,
)

DOMESTIC = FreeAgencyKind.DOMESTIC
STAYED = FreeAgencyOutcome.STAYED
MOVED = FreeAgencyOutcome.MOVED


def stint(team_id=1, from_year=2020, to_year=None, **kwargs):
    return Stint(
        team_id=team_id, number=kwargs.pop("number", JerseyNumber(10)), from_year=from_year, to_year=to_year, **kwargs
    )


def player(*career, declarations=()):
    return Player(
        name="山田",
        number=JerseyNumber(10),
        position=Position.INFIELDER,
        career=list(career),
        fa_declarations=list(declarations),
    )


class AcquisitionRouteTest(TestCase):
    def test_labels_are_the_single_source_of_choices(self):
        self.assertEqual(
            AcquisitionRoute.labels(),
            ["ドラフト", "育成ドラフト", "FA", "トレード", "自由契約からの獲得", "新外国人", "その他"],
        )

    def test_from_label_round_trips_and_rejects_unknown(self):
        self.assertIs(AcquisitionRoute.from_label("FA"), AcquisitionRoute.FREE_AGENT)
        with self.assertRaises(InvalidAcquisition):
            AcquisitionRoute.from_label("拾った")

    def test_unknown_route_is_none_and_needs_no_check(self):
        self.assertIsNone(stint().acquired_via)

    def test_developmental_draft_requires_a_developmental_contract(self):
        route = AcquisitionRoute.DEVELOPMENTAL_DRAFT
        with self.assertRaises(InvalidAcquisition):
            stint(acquired_via=route)  # 既定の区分は支配下
        stint(acquired_via=route, number=JerseyNumber(120), signed_as=ContractStatus.DEVELOPMENTAL)

    def test_draft_requires_a_registered_contract(self):
        with self.assertRaises(InvalidAcquisition):
            stint(
                acquired_via=AcquisitionRoute.DRAFT, number=JerseyNumber(120), signed_as=ContractStatus.DEVELOPMENTAL
            )
        stint(acquired_via=AcquisitionRoute.DRAFT)

    def test_other_routes_do_not_constrain_the_contract(self):
        for route in (AcquisitionRoute.FREE_AGENT, AcquisitionRoute.TRADE, AcquisitionRoute.NEW_FOREIGN):
            stint(acquired_via=route, number=JerseyNumber(120), signed_as=ContractStatus.DEVELOPMENTAL)
            stint(acquired_via=route)


class DeclarationInvariantsTest(TestCase):
    def test_kinds_are_domestic_and_overseas(self):
        self.assertEqual(FreeAgencyKind.labels(), ["国内", "海外"])
        with self.assertRaises(InvalidFreeAgentDeclaration):
            FreeAgencyKind.from_label("月面")

    def test_declaring_in_a_year_with_a_stint_is_accepted(self):
        p = player(stint(1, 2020, 2025))

        declaration = p.declare_free_agency(2025, DOMESTIC)

        self.assertEqual((declaration.year, declaration.kind), (2025, DOMESTIC))
        self.assertEqual(p.fa_declarations, [declaration])

    def test_the_same_year_cannot_be_declared_twice(self):
        p = player(stint())
        p.declare_free_agency(2024, DOMESTIC)

        with self.assertRaises(InvalidFreeAgentDeclaration):
            p.declare_free_agency(2024, FreeAgencyKind.OVERSEAS)
        self.assertEqual(len(p.fa_declarations), 1)

    def test_a_year_without_any_stint_is_rejected(self):
        p = player(stint(1, 2020, 2022))

        for year in (2019, 2023):
            with self.assertRaises(InvalidFreeAgentDeclaration):
                p.declare_free_agency(year, DOMESTIC)
        self.assertEqual(p.fa_declarations, [])

    def test_a_stint_at_any_team_counts(self):
        p = player(stint(1, 2020, 2022), stint(2, 2023))

        p.declare_free_agency(2023, DOMESTIC)

    def test_removing_a_declaration(self):
        p = player(stint(), declarations=[FreeAgentDeclaration(2024, DOMESTIC)])

        p.remove_free_agency_declaration(2024)

        self.assertEqual(p.fa_declarations, [])
        with self.assertRaises(InvalidFreeAgentDeclaration):
            p.remove_free_agency_declaration(2024)

    def test_removing_a_saved_declaration_records_its_id_for_the_repository(self):
        p = player(
            stint(), declarations=[FreeAgentDeclaration(2024, DOMESTIC, id=7), FreeAgentDeclaration(2023, DOMESTIC)]
        )

        p.remove_free_agency_declaration(2024)
        p.remove_free_agency_declaration(2023)  # 保存前の宣言（id なし）は記録しない

        self.assertEqual(p.removed_declaration_ids, [7])

    def test_a_refused_removal_records_nothing(self):
        p = player(
            stint(1, 2020, 2024),
            stint(2, 2025, acquired_via=AcquisitionRoute.FREE_AGENT),
            declarations=[FreeAgentDeclaration(2024, DOMESTIC, id=7)],
        )

        with self.assertRaises(InvalidFreeAgentDeclaration):
            p.remove_free_agency_declaration(2024)
        self.assertEqual(p.removed_declaration_ids, [])

    def test_a_declaration_behind_a_free_agent_signing_cannot_be_removed(self):
        declaration = FreeAgentDeclaration(2024, DOMESTIC)
        p = player(
            stint(1, 2020, 2024), stint(2, 2025, acquired_via=AcquisitionRoute.FREE_AGENT), declarations=[declaration]
        )

        with self.assertRaises(InvalidFreeAgentDeclaration):
            p.remove_free_agency_declaration(2024)
        self.assertEqual(p.fa_declarations, [declaration])


class AcquisitionAndDeclarationTest(TestCase):
    """経路が FA の在籍は、どれかの宣言の移籍先として導かれる在籍であること。"""

    @staticmethod
    def signed_as_fa(from_year, *declaration_years):
        return player(
            stint(1, 2015, from_year - 1),
            stint(2, from_year, acquired_via=AcquisitionRoute.FREE_AGENT),
            declarations=[FreeAgentDeclaration(y, DOMESTIC) for y in declaration_years],
        )

    def test_a_declaration_the_year_before_the_signing_is_enough(self):
        self.signed_as_fa(2025, 2024).ensure_acquisitions_declared()

    def test_no_declaration_is_rejected(self):
        with self.assertRaises(InvalidAcquisition):
            self.signed_as_fa(2025).ensure_acquisitions_declared()

    def test_a_declaration_two_years_before_is_not_enough(self):
        with self.assertRaises(InvalidAcquisition):
            self.signed_as_fa(2025, 2023).ensure_acquisitions_declared()

    def test_re_signing_with_the_same_team_cannot_be_a_free_agent_signing(self):
        # (a) 同じ球団で在籍を結び直しただけの在籍は、FA で入団したことにならない
        p = player(
            stint(1, 2015, 2024),
            stint(1, 2025, acquired_via=AcquisitionRoute.FREE_AGENT),
            declarations=[FreeAgentDeclaration(2024, DOMESTIC)],
        )

        with self.assertRaises(InvalidAcquisition):
            p.ensure_acquisitions_declared()

    def test_a_signing_without_the_declaring_teams_stint_is_rejected(self):
        # (b) 宣言の年に移籍先の在籍しか無い（宣言した球団の在籍が無い）
        p = player(
            stint(2, 2024, acquired_via=AcquisitionRoute.FREE_AGENT),
            declarations=[FreeAgentDeclaration(2024, DOMESTIC)],
        )

        with self.assertRaises(InvalidAcquisition):
            p.ensure_acquisitions_declared()

    def test_a_trade_after_staying_is_not_a_free_agent_signing_and_needs_no_declaration(self):
        # (c) 宣言して残留し、翌年にトレード。経路がトレードの在籍は FA の検査の対象外で、宣言の結果にもならない
        p = player(
            stint(1, 2015, 2025),
            stint(2, 2025, acquired_via=AcquisitionRoute.TRADE),
            declarations=[FreeAgentDeclaration(2024, DOMESTIC)],
        )

        p.ensure_acquisitions_declared()
        self.assertIs(p.outcome_of(p.fa_declarations[0]), STAYED)

    def test_declaring_after_a_mid_season_trade_is_not_a_free_agent_signing(self):
        # (d) 2024年のシーズン途中に A→B、同じ年に B で宣言。B の在籍を FA 入団にはできない
        p = player(
            stint(1, 2015, 2024),
            stint(2, 2024, acquired_via=AcquisitionRoute.FREE_AGENT),
            declarations=[FreeAgentDeclaration(2024, DOMESTIC)],
        )

        with self.assertRaises(InvalidAcquisition):
            p.ensure_acquisitions_declared()

    def test_a_declaration_is_needed_in_the_year_it_covers(self):
        from myapp.domain.entities import ensure_declarations_valid

        with self.assertRaises(InvalidFreeAgentDeclaration):
            ensure_declarations_valid([stint(1, 2020, 2022)], [FreeAgentDeclaration(2024, DOMESTIC)])
        with self.assertRaises(InvalidFreeAgentDeclaration):
            ensure_declarations_valid(
                [stint()], [FreeAgentDeclaration(2024, DOMESTIC), FreeAgentDeclaration(2024, FreeAgencyKind.OVERSEAS)]
            )
        ensure_declarations_valid([stint()], [FreeAgentDeclaration(2024, DOMESTIC)])

    def test_declaring_and_moving_with_an_unknown_route_is_allowed(self):
        # 不明は推測しない。宣言して移籍したのに経路が不明の在籍は許す
        p = player(stint(1, 2015, 2024), stint(2, 2025), declarations=[FreeAgentDeclaration(2024, DOMESTIC)])

        p.ensure_acquisitions_declared()

    def test_a_new_team_player_cannot_be_registered_as_a_free_agent(self):
        team = Team(name="A", id=1)

        with self.assertRaises(InvalidAcquisition):
            team.add_player("新人", JerseyNumber(10), Position.PITCHER, 2025, acquired_via=AcquisitionRoute.FREE_AGENT)
        self.assertEqual(team.players, [])

    def test_a_route_is_carried_onto_the_first_stint(self):
        team = Team(name="A", id=1)

        added = team.add_player("新人", JerseyNumber(10), Position.PITCHER, 2025, acquired_via=AcquisitionRoute.DRAFT)

        self.assertIs(added.career[0].acquired_via, AcquisitionRoute.DRAFT)

    def test_the_team_declares_and_removes_for_its_player(self):
        team = Team(name="A", id=1)
        added = team.add_player("新人", JerseyNumber(10), Position.PITCHER, 2025)
        added.id = 5

        team.declare_free_agency(5, 2025, DOMESTIC)
        self.assertEqual(len(added.fa_declarations), 1)
        team.remove_free_agency_declaration(5, 2025)
        self.assertEqual(added.fa_declarations, [])


class OutcomeTest(TestCase):
    """宣言の結果は保存せず、在籍から導く。"""

    @staticmethod
    def outcome(year, *career):
        return declaration_outcome(FreeAgentDeclaration(year, DOMESTIC), career)

    def test_staying_with_the_same_team_is_a_stay(self):
        self.assertIs(self.outcome(2024, stint(1, 2020)), STAYED)

    def test_a_new_stint_at_the_same_team_is_a_stay(self):
        # 契約を結び直して同じチームの在籍が改めて始まっても移籍ではない
        self.assertIs(self.outcome(2024, stint(1, 2020, 2024), stint(1, 2025)), STAYED)

    def test_declaring_after_a_mid_season_trade_is_judged_from_the_new_team(self):
        # (d) 2024年のシーズン途中に A→B へトレードし、同じ年に B で宣言して B に残留。起点は B なので残留
        trade = stint(2, 2024, acquired_via=AcquisitionRoute.TRADE)

        self.assertIs(self.outcome(2024, stint(1, 2020, 2024), trade), STAYED)

    def test_a_trade_after_staying_is_not_the_result_of_the_declaration(self):
        # (c) 2024年に宣言して残留し、2025年のシーズン途中にトレード。経路がトレードの在籍は宣言の結果とみなさない
        trade = stint(2, 2025, acquired_via=AcquisitionRoute.TRADE)

        self.assertIs(self.outcome(2024, stint(1, 2020, 2025), trade), STAYED)

    def test_a_free_agent_signing_the_next_year_is_a_move(self):
        signing = stint(2, 2025, acquired_via=AcquisitionRoute.FREE_AGENT)

        self.assertIs(self.outcome(2024, stint(1, 2020, 2024), signing), MOVED)

    def test_the_origin_prefers_the_stint_still_running_at_the_end_of_the_year(self):
        origin = fa_origin(2024, [StintPeriod(1, 2020, 2024), StintPeriod(2, 2024)])

        self.assertEqual(origin, StintPeriod(2, 2024))

    def test_the_origin_falls_back_to_the_stint_that_started_last_in_the_year(self):
        origin = fa_origin(2024, [StintPeriod(1, 2020, 2024), StintPeriod(2, 2024, 2024)])

        self.assertEqual(origin, StintPeriod(2, 2024, 2024))

    def test_the_origin_is_none_without_a_stint_in_the_year(self):
        self.assertIsNone(fa_origin(2024, [StintPeriod(1, 2010, 2020)]))

    def test_destinations_are_listed_by_start_year(self):
        periods = [StintPeriod(1, 2020, 2024), StintPeriod(3, 2025), StintPeriod(2, 2024, 2024)]

        self.assertEqual([p.team_id for p in fa_destinations(2024, periods)], [3])

    def test_joining_another_team_the_next_year_is_a_move(self):
        self.assertIs(self.outcome(2024, stint(1, 2020, 2024), stint(2, 2025)), MOVED)

    def test_joining_another_team_two_years_later_is_still_a_stay(self):
        self.assertIs(self.outcome(2024, stint(1, 2020, 2024), stint(2, 2026)), STAYED)

    def test_a_move_before_the_declaration_year_is_not_the_result(self):
        self.assertIs(self.outcome(2024, stint(1, 2018, 2022), stint(2, 2023)), STAYED)

    def test_without_a_stint_in_the_year_it_is_a_stay(self):
        self.assertIs(self.outcome(2024, stint(1, 2010, 2020)), STAYED)

    def test_the_player_derives_it_from_its_own_career(self):
        declaration = FreeAgentDeclaration(2024, DOMESTIC)
        p = player(stint(1, 2020, 2024), stint(2, 2025), declarations=[declaration])

        self.assertIs(p.outcome_of(declaration), MOVED)

    def test_the_core_rule_works_on_plain_periods(self):
        periods = [StintPeriod(1, 2020, 2024), StintPeriod(2, 2025)]

        self.assertIs(free_agency_outcome(2024, periods), MOVED)
        self.assertIs(free_agency_outcome(2024, periods[:1]), STAYED)


class ConsistencyAfterClosingStintsTest(TestCase):
    """在籍を閉じる操作のあとも、宣言の不変条件と経路 FA の整合を集約が見る。"""

    @staticmethod
    def team_with_declared_player():
        this_year = date.today().year
        team = Team(name="A", id=1)
        added = team.add_player("新人", JerseyNumber(10), Position.PITCHER, this_year - 3)
        added.id = 5
        team.declare_free_agency(5, this_year, DOMESTIC)
        return team, added, this_year

    def test_retiring_before_the_declaration_year_is_refused(self):
        team, added, this_year = self.team_with_declared_player()

        with self.assertRaises(InvalidFreeAgentDeclaration):
            team.retire_player(added, this_year - 1)

    def test_retiring_in_or_after_the_declaration_year_is_accepted(self):
        team, added, this_year = self.team_with_declared_player()

        team.retire_player(added, this_year)

        self.assertEqual(added.career[0].to_year, this_year)

    def test_a_declaration_ahead_of_the_clock_is_allowed_but_retiring_before_it_is_refused(self):
        """時計（今日の年）では制限しない（ペナントの世界は実際の年より先へ進む）。在籍が続く限り先の年も宣言でき、
        その年より前に在籍を閉じる退団は「どの在籍にも覆われない宣言」になるので拒否する。"""
        team = Team(name="A", id=1)
        added = team.add_player("新人", JerseyNumber(10), Position.PITCHER, 2020)
        ahead = date.today().year + 3
        added.declare_free_agency(ahead, DOMESTIC)

        with self.assertRaises(InvalidFreeAgentDeclaration):
            team.retire_player(added, ahead - 1)

    def test_a_signing_in_the_declaration_year_is_not_a_free_agent_signing(self):
        # オフの FA 移籍は宣言の翌年の加入で記録する。同じ年の加入は FA 入団にならない
        p = player(
            stint(1, 2015, 2024),
            stint(2, 2024, acquired_via=AcquisitionRoute.FREE_AGENT),
            declarations=[FreeAgentDeclaration(2024, DOMESTIC)],
        )
        with self.assertRaises(InvalidAcquisition):
            p.ensure_free_agency_consistent()

        p.career[1].from_year = 2025
        p.ensure_free_agency_consistent()
