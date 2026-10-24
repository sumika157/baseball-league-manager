"""ドラフトの指名（`DraftPick`）と、最初の在籍の経路との整合の単体テスト。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain.entities import Stint, ensure_draft_fits_career
from myapp.domain.exceptions import InvalidDraftPick, InvalidProfile, InvalidSeason
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus, DraftPick, JerseyNumber

DRAFT = AcquisitionRoute.DRAFT
DEVELOPMENTAL_DRAFT = AcquisitionRoute.DEVELOPMENTAL_DRAFT


def stint(from_year, route, *, team_id=1):
    developmental = route is DEVELOPMENTAL_DRAFT
    return Stint(
        team_id=team_id,
        number=JerseyNumber("120" if developmental else "10"),
        from_year=from_year,
        signed_as=ContractStatus.DEVELOPMENTAL if developmental else ContractStatus.REGISTERED,
        acquired_via=route,
    )


class DraftPickTest(TestCase):
    def test_year_is_validated_as_a_season(self):
        self.assertEqual(DraftPick("2021").year, 2021)
        with self.assertRaises(InvalidSeason):
            DraftPick(1800)

    def test_round_must_be_one_or_more(self):
        for bad in (0, -1):
            with self.subTest(round=bad), self.assertRaises(InvalidDraftPick):
                DraftPick(2020, round=bad)

    def test_round_is_converted_to_an_integer(self):
        self.assertEqual(DraftPick(2020, round="3").round, 3)

    def test_non_numeric_round_is_rejected(self):
        with self.assertRaises(InvalidDraftPick):
            DraftPick(2020, round="一")

    def test_round_may_be_unknown(self):
        """入団年から移した選手は順位が分からない。"""
        self.assertIsNone(DraftPick(2020).round)

    def test_errors_are_profile_errors(self):
        """管理画面などプロフィールの誤りとして扱う所で、同じように受けられる。"""
        self.assertTrue(issubclass(InvalidDraftPick, InvalidProfile))

    def test_label(self):
        cases = [
            (DraftPick(2020, round=1), "2020年ドラフト1位"),
            (DraftPick(2020, round=3, is_development=True), "2020年育成ドラフト3位"),
            (DraftPick(2020), "2020年ドラフト"),
            (DraftPick(2020, is_development=True), "2020年育成ドラフト"),
        ]
        for pick, label in cases:
            with self.subTest(label=label):
                self.assertEqual(pick.label, label)

    def test_order_label(self):
        """オフの結果の「指名」の列（#198 D1）。順位が無ければ空。"""
        self.assertEqual(DraftPick(2020, round=1).order_label, "1位")
        self.assertEqual(DraftPick(2020, round=3, is_development=True).order_label, "育成3位")
        self.assertEqual(DraftPick(2020).order_label, "")

    def test_entry_year_is_the_next_year(self):
        self.assertEqual(DraftPick(2019, round=1).entry_year, 2020)

    def test_acquisition_route(self):
        self.assertIs(DraftPick(2020, round=1).acquisition_route, DRAFT)
        self.assertIs(DraftPick(2020, round=1, is_development=True).acquisition_route, DEVELOPMENTAL_DRAFT)

    def test_fits_the_matching_route(self):
        DraftPick(2020, round=1).ensure_fits_route(DRAFT)
        DraftPick(2020, round=1, is_development=True).ensure_fits_route(DEVELOPMENTAL_DRAFT)

    def test_developmental_route_rejects_a_regular_pick(self):
        with self.assertRaises(InvalidDraftPick):
            DraftPick(2020, round=1).ensure_fits_route(DEVELOPMENTAL_DRAFT)

    def test_draft_route_rejects_a_developmental_pick(self):
        with self.assertRaises(InvalidDraftPick):
            DraftPick(2020, round=1, is_development=True).ensure_fits_route(DRAFT)

    def test_other_routes_are_not_checked(self):
        """ドラフトの後に FA などで移った在籍は、ドラフトの区分と関係ない。"""
        for route in (AcquisitionRoute.FREE_AGENT, AcquisitionRoute.TRADE, AcquisitionRoute.NEW_FOREIGN):
            with self.subTest(route=route):
                DraftPick(2020, round=1, is_development=True).ensure_fits_route(route)


class DraftFitsCareerTest(TestCase):
    def test_first_stint_developmental_rejects_a_regular_pick(self):
        with self.assertRaises(InvalidDraftPick):
            ensure_draft_fits_career(DraftPick(2019, round=2), [stint(2020, DEVELOPMENTAL_DRAFT)])

    def test_only_the_first_stint_is_checked(self):
        """2つ目の在籍の経路（ここでは不自然な「育成ドラフト」）は見ない。"""
        career = [stint(2020, DRAFT), stint(2024, DEVELOPMENTAL_DRAFT, team_id=2)]
        ensure_draft_fits_career(DraftPick(2019, round=2), career)
        with self.assertRaises(InvalidDraftPick):
            ensure_draft_fits_career(
                DraftPick(2019, round=2), list(reversed([stint(2018, DEVELOPMENTAL_DRAFT), *career]))
            )

    def test_unknown_route_no_draft_or_no_stint_is_not_checked(self):
        unknown = Stint(team_id=1, number=JerseyNumber("10"), from_year=2020)
        ensure_draft_fits_career(DraftPick(2019, round=1, is_development=True), [unknown])
        ensure_draft_fits_career(None, [stint(2020, DEVELOPMENTAL_DRAFT)])
        ensure_draft_fits_career(DraftPick(2019, round=1, is_development=True), [])
