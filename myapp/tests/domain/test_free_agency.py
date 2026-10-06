"""FA 権の取得の見込み（domain/services/free_agency.py）。Django 不要。"""

import unittest
from datetime import date

from myapp.domain.services.free_agency import (
    EducationPath,
    FaStatusKind,
    ServiceTime,
    estimated_service_days,
    fa_order,
    fa_outlook,
    service_seasons,
)
from myapp.domain.value_objects import Profile


def full_years(*years: int, days: int = 145) -> dict[int, int]:
    return dict.fromkeys(years, days)


def outlook(education, debut, days, *, through=2030, records_from=2000, declared=(), developmental=()):
    return fa_outlook(
        education=education,
        debut_year=debut,
        days_by_year=days,
        through_year=through,
        records_from_year=records_from,
        declared_years=list(declared),
        developmental_years=set(developmental),
    )


class ServiceSeasonsTest(unittest.TestCase):
    def test_exactly_145_days_is_one_season_and_144_is_zero(self):
        self.assertEqual(service_seasons({2020: 145}), ServiceTime(1, 0))
        self.assertEqual(service_seasons({2020: 144}), ServiceTime(0, 144))

    def test_short_years_are_summed(self):
        self.assertEqual(service_seasons({2020: 144, 2021: 1}), ServiceTime(1, 0))
        self.assertEqual(service_seasons({2020: 100, 2021: 100}), ServiceTime(1, 55))

    def test_remainder_carries_over_to_the_next_years(self):
        days = {2020: 100, 2021: 100, 2022: 100}
        self.assertEqual(service_seasons(days), ServiceTime(2, 10))

    def test_full_year_does_not_touch_the_remainder(self):
        self.assertEqual(service_seasons({2020: 100, 2021: 150, 2022: 50}), ServiceTime(2, 5))

    def test_estimated_days_are_inclusive(self):
        self.assertEqual(estimated_service_days(date(2026, 4, 1), date(2026, 4, 1)), 1)
        self.assertEqual(estimated_service_days(date(2026, 4, 1), date(2026, 4, 30)), 30)


class EducationPathTest(unittest.TestCase):
    def test_corporate_wins_over_university_and_high_school(self):
        profile = Profile(high_school="A高", university="B大", corporate_team="C社")
        self.assertIs(EducationPath.of(profile), EducationPath.CORPORATE)

    def test_university_wins_over_high_school(self):
        self.assertIs(EducationPath.of(Profile(high_school="A高", university="B大")), EducationPath.UNIVERSITY)

    def test_high_school_only(self):
        self.assertIs(EducationPath.of(Profile(high_school="A高")), EducationPath.HIGH_SCHOOL)

    def test_nothing_is_unknown(self):
        self.assertIs(EducationPath.of(Profile()), EducationPath.UNKNOWN)


class FaOutlookTest(unittest.TestCase):
    def test_high_school_needs_eight_domestic_and_nine_overseas(self):
        days = full_years(*range(2010, 2018))  # 8シーズン
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, days)
        self.assertEqual(result.domestic.kind, FaStatusKind.ACQUIRED)
        self.assertEqual(result.domestic.acquired_year, 2017)
        self.assertEqual((result.overseas.kind, result.overseas.remaining), (FaStatusKind.REMAINING, 1))
        nine = outlook(EducationPath.HIGH_SCHOOL, 2010, full_years(*range(2010, 2019)))
        self.assertEqual((nine.overseas.kind, nine.overseas.acquired_year), (FaStatusKind.ACQUIRED, 2018))

    def test_university_before_2007_needs_eight(self):
        result = outlook(EducationPath.UNIVERSITY, 2006, full_years(*range(2006, 2013)))
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 1))

    def test_university_entering_in_2007_is_drafted_in_2006_and_needs_eight(self):
        # 入団はドラフトの翌年。2007年入団はドラフトが2006年なので短縮の対象外
        result = outlook(EducationPath.UNIVERSITY, 2007, full_years(*range(2007, 2014)))
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 1))

    def test_university_entering_in_2008_is_drafted_in_2007_and_needs_seven(self):
        result = outlook(EducationPath.UNIVERSITY, 2008, full_years(*range(2008, 2015)))
        self.assertEqual((result.domestic.kind, result.domestic.acquired_year), (FaStatusKind.ACQUIRED, 2014))

    def test_corporate_from_2007_needs_seven(self):
        result = outlook(EducationPath.CORPORATE, 2015, full_years(*range(2015, 2022)))
        self.assertEqual(result.domestic.kind, FaStatusKind.ACQUIRED)

    def test_high_school_is_not_shortened_after_2007(self):
        result = outlook(EducationPath.HIGH_SCHOOL, 2015, full_years(*range(2015, 2022)))
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 1))

    def test_reacquisition_takes_four_seasons_from_the_year_after_the_declaration(self):
        days = full_years(*range(2010, 2022))
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, days, through=2021, declared=[2017])
        # 2018〜2021 の4シーズンで再取得。宣言までの8シーズンは数えない
        self.assertEqual((result.domestic.kind, result.domestic.acquired_year), (FaStatusKind.ACQUIRED, 2021))
        self.assertEqual((result.overseas.kind, result.overseas.acquired_year), (FaStatusKind.ACQUIRED, 2021))
        self.assertEqual(result.service, ServiceTime(4, 0))

    def test_reacquisition_counts_from_the_latest_declaration(self):
        days = full_years(*range(2010, 2024))
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, days, through=2023, declared=[2016, 2020])
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 1))

    def test_developmental_years_are_not_counted(self):
        days = full_years(2010, 2011, 2012, 2013)
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, days, developmental=[2010, 2011])
        self.assertEqual(result.service, ServiceTime(2, 0))
        self.assertEqual(result.domestic.remaining, 6)

    def test_developmental_years_do_not_add_to_the_remainder(self):
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, {2010: 100, 2011: 100}, developmental=[2010])
        self.assertEqual(result.service, ServiceTime(0, 100))

    def test_unknown_education_is_acquired_once_eight_seasons_are_counted(self):
        result = outlook(EducationPath.UNKNOWN, 2010, full_years(*range(2010, 2018)))
        self.assertEqual((result.domestic.kind, result.domestic.acquired_year), (FaStatusKind.ACQUIRED, 2017))

    def test_unknown_education_with_seven_seasons_is_still_unknown(self):
        # 大卒なら7で取得済みだが、高卒なら8まで届かない。学歴が分からないので決められない
        result = outlook(EducationPath.UNKNOWN, 2010, full_years(*range(2010, 2017)))
        self.assertEqual(result.domestic.kind, FaStatusKind.UNKNOWN)

    def test_unknown_education_before_the_college_rule_needs_eight(self):
        # ドラフトが2007年より前（入団が2007年以前）なら、学歴によらず8シーズン
        result = outlook(EducationPath.UNKNOWN, 2007, full_years(*range(2007, 2014)))
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 1))
        later = outlook(EducationPath.UNKNOWN, 2008, full_years(*range(2008, 2014)))
        self.assertEqual(later.domestic.kind, FaStatusKind.UNKNOWN)

    def test_acquired_year_is_the_latest_when_earlier_years_are_missing(self):
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, full_years(*range(2020, 2028)), records_from=2020)
        self.assertTrue(result.domestic.acquired_year_is_latest)
        self.assertEqual(result.domestic.label, "取得済み（遅くとも2027年）")
        exact = outlook(EducationPath.HIGH_SCHOOL, 2020, full_years(*range(2020, 2028)), records_from=2020)
        self.assertFalse(exact.domestic.acquired_year_is_latest)
        self.assertEqual(exact.domestic.label, "取得済み（2027年）")

    def test_unknown_education_makes_only_domestic_unknown(self):
        """国内FAだけが学歴で変わる。海外FAは学歴に関係なく9シーズンなので数える（外国人選手など）。"""
        result = outlook(EducationPath.UNKNOWN, 2010, full_years(2010, 2011))
        self.assertIs(result.domestic.kind, FaStatusKind.UNKNOWN)
        self.assertEqual(result.service, ServiceTime(2, 0))
        self.assertEqual((result.overseas.kind, result.overseas.remaining), (FaStatusKind.REMAINING, 7))

    def test_unknown_education_still_acquires_overseas(self):
        result = outlook(EducationPath.UNKNOWN, 2010, full_years(*range(2010, 2019)))
        self.assertEqual((result.overseas.kind, result.overseas.acquired_year), (FaStatusKind.ACQUIRED, 2018))

    def test_unknown_debut_year_cannot_be_counted(self):
        result = outlook(EducationPath.HIGH_SCHOOL, None, full_years(2010))
        self.assertIsNone(result.service)
        self.assertEqual(result.domestic.kind, FaStatusKind.UNKNOWN)
        self.assertEqual(result.domestic.label, "不明")

    def test_debut_before_records_is_unknown_with_an_upper_bound(self):
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, full_years(2020, 2021), records_from=2020)
        self.assertEqual(result.domestic.kind, FaStatusKind.UNKNOWN)
        self.assertEqual(result.domestic.remaining, 6)
        self.assertEqual(result.domestic.label, "不明（あと最大6シーズン）")

    def test_debut_before_records_is_still_acquired_when_the_counted_seasons_suffice(self):
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, full_years(*range(2020, 2028)), records_from=2020)
        self.assertEqual((result.domestic.kind, result.domestic.acquired_year), (FaStatusKind.ACQUIRED, 2027))

    def test_declaration_after_the_records_start_makes_the_count_complete(self):
        result = outlook(
            EducationPath.HIGH_SCHOOL, 2010, full_years(2021, 2022), records_from=2020, through=2022, declared=[2020]
        )
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 2))

    def test_estimate_flag_is_off_without_any_days(self):
        self.assertFalse(outlook(EducationPath.HIGH_SCHOOL, 2020, {}).includes_estimate)
        self.assertTrue(outlook(EducationPath.HIGH_SCHOOL, 2020, {2020: 1}).includes_estimate)

    def test_labels(self):
        result = outlook(EducationPath.HIGH_SCHOOL, 2010, full_years(*range(2010, 2018)))
        self.assertEqual(result.domestic.label, "取得済み（2017年）")
        self.assertEqual(result.overseas.label, "あと1シーズン")


class FaOrderTest(unittest.TestCase):
    def test_acquired_then_nearest_then_unknown(self):
        acquired = outlook(EducationPath.HIGH_SCHOOL, 2010, full_years(*range(2010, 2018)))
        near = outlook(EducationPath.HIGH_SCHOOL, 2010, full_years(*range(2010, 2017)))
        far = outlook(EducationPath.HIGH_SCHOOL, 2010, full_years(2010))
        partial = outlook(EducationPath.HIGH_SCHOOL, 2000, full_years(2010), records_from=2010)
        unknown = outlook(EducationPath.UNKNOWN, 2010, {})
        ranked = sorted([unknown, partial, far, near, acquired], key=fa_order)
        self.assertEqual(ranked, [acquired, near, far, partial, unknown])


if __name__ == "__main__":
    unittest.main()
