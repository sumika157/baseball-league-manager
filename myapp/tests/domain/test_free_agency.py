"""FA 権の取得の見込み（domain/services/free_agency.py）。Django 不要。"""

import unittest
from datetime import date

from myapp.domain.services.free_agency import (
    EducationPath,
    FaStatusKind,
    ProEntry,
    ServiceTime,
    earliest_domestic_acquisition_year,
    estimated_service_days,
    fa_order,
    fa_outlook,
    first_domestic_acquisition_year,
    service_seasons,
)
from myapp.domain.value_objects import DraftPick, Profile


def full_years(*years: int, days: int = 145) -> dict[int, int]:
    return dict.fromkeys(years, days)


def drafted(entry_year):
    """ドラフトを経た選手のプロ入り（`entry_year` はドラフトの翌年＝入団の年）。None はプロ入りが分からない。"""
    if entry_year is None:
        return None
    return ProEntry.of(draft=DraftPick(entry_year - 1), is_foreign_player=False, first_stint_year=None)


def outlook(education, debut, days, *, through=2030, records_from=2000, declared=(), developmental=(), entry=None):
    """`debut` はドラフトを経た選手の入団の年。`entry` を渡せばそちらを使う（ドラフトの無い選手）。"""
    return fa_outlook(
        education=education,
        entry=entry if entry is not None else drafted(debut),
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
        # 2007年入団はドラフトが2006年なので短縮の対象外（ドラフトの年で判定する）
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
        # ドラフトが2007年より前なら、学歴によらず8シーズン
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

    def test_unknown_entry_cannot_be_counted(self):
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


class ProEntryTest(unittest.TestCase):
    """プロ入りの3種類（ドラフトあり／ドラフトなしの外国人／ドラフトなしの日本人）。"""

    def test_drafted_player_counts_from_the_year_after_the_draft(self):
        entry = ProEntry.of(draft=DraftPick(2019, round=1), is_foreign_player=False, first_stint_year=2021)
        self.assertEqual(entry.year, 2020)  # 最初の在籍の加入年ではなく、ドラフトの翌年

    def test_without_a_draft_counts_from_the_first_stint(self):
        self.assertEqual(ProEntry.of(draft=None, is_foreign_player=True, first_stint_year=2022).year, 2022)

    def test_without_a_draft_or_a_stint_cannot_be_counted(self):
        self.assertIsNone(ProEntry.of(draft=None, is_foreign_player=False, first_stint_year=None))

    def test_college_rule_is_judged_by_the_draft_year(self):
        """短縮は「2007年以降のドラフト」。入団の年ではなくドラフトの年そのもので判定する（境界）。"""
        draft_2006 = ProEntry.of(draft=DraftPick(2006), is_foreign_player=False, first_stint_year=None)
        draft_2007 = ProEntry.of(draft=DraftPick(2007), is_foreign_player=False, first_stint_year=None)
        self.assertEqual(
            outlook(EducationPath.UNIVERSITY, None, full_years(*range(2007, 2014)), entry=draft_2006).domestic.kind,
            FaStatusKind.REMAINING,
        )
        self.assertEqual(
            outlook(EducationPath.UNIVERSITY, None, full_years(*range(2008, 2015)), entry=draft_2007).domestic.kind,
            FaStatusKind.ACQUIRED,
        )

    def test_drafted_foreign_player_follows_the_same_rules(self):
        """ドラフトを経た外国籍の選手（日本の学校を出た選手など）は日本人と同じ規則（大卒なら7シーズン）。"""
        entry = ProEntry.of(draft=DraftPick(2014), is_foreign_player=True, first_stint_year=2015)
        result = outlook(EducationPath.UNIVERSITY, None, full_years(*range(2015, 2022)), entry=entry)
        self.assertEqual((result.domestic.kind, result.domestic.acquired_year), (FaStatusKind.ACQUIRED, 2021))


class UndraftedForeignPlayerTest(unittest.TestCase):
    """ドラフトを経ていない外国人選手。海外リーグの年数は数えず、記録の最初の年から言い切る（決定 C）。"""

    def entry(self, first_stint_year):
        return ProEntry.of(draft=None, is_foreign_player=True, first_stint_year=first_stint_year)

    def test_years_before_the_records_are_not_counted_and_not_unknown(self):
        result = outlook(
            EducationPath.UNKNOWN,
            None,
            full_years(2020, 2021),
            records_from=2020,
            through=2021,
            entry=self.entry(2015),
        )
        self.assertEqual(result.service, ServiceTime(2, 0))
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 6))
        self.assertEqual(result.domestic.label, "あと6シーズン")
        self.assertEqual((result.overseas.kind, result.overseas.remaining), (FaStatusKind.REMAINING, 7))

    def test_acquired_year_is_exact(self):
        result = outlook(
            EducationPath.UNKNOWN, None, full_years(*range(2020, 2028)), records_from=2020, entry=self.entry(2015)
        )
        self.assertEqual(result.domestic.label, "取得済み（2027年）")

    def test_domestic_needs_eight_even_with_a_university(self):
        """短縮はドラフトで入団した大学生・社会人だけ。ドラフトを経ていなければ学歴によらず8シーズン。"""
        result = outlook(EducationPath.UNIVERSITY, None, full_years(*range(2020, 2027)), entry=self.entry(2020))
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 1))

    def test_joining_after_the_records_start_counts_from_the_first_stint(self):
        result = outlook(
            EducationPath.UNKNOWN, None, full_years(2020, 2021, 2022), records_from=2018, entry=self.entry(2021)
        )
        self.assertEqual(result.service, ServiceTime(2, 0))

    def test_reacquisition_takes_four_seasons(self):
        days = full_years(*range(2020, 2033))
        result = outlook(EducationPath.UNKNOWN, None, days, through=2032, declared=[2028], entry=self.entry(2020))
        self.assertEqual((result.domestic.kind, result.domestic.acquired_year), (FaStatusKind.ACQUIRED, 2032))


class UndraftedJapanesePlayerTest(unittest.TestCase):
    """ドラフトが入力されていない日本人選手。今までどおり保守的に扱う。"""

    def entry(self, first_stint_year):
        return ProEntry.of(draft=None, is_foreign_player=False, first_stint_year=first_stint_year)

    def test_entering_by_2007_cannot_be_shortened(self):
        result = outlook(EducationPath.UNKNOWN, None, full_years(*range(2007, 2014)), entry=self.entry(2007))
        self.assertEqual((result.domestic.kind, result.domestic.remaining), (FaStatusKind.REMAINING, 1))

    def test_entering_from_2008_is_undecided_without_education(self):
        result = outlook(EducationPath.UNKNOWN, None, full_years(*range(2008, 2015)), entry=self.entry(2008))
        self.assertEqual(result.domestic.kind, FaStatusKind.UNKNOWN)

    def test_years_before_the_records_stay_unknown(self):
        result = outlook(
            EducationPath.HIGH_SCHOOL, None, full_years(2020, 2021), records_from=2020, entry=self.entry(2015)
        )
        self.assertEqual(result.domestic.label, "不明（あと最大6シーズン）")


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


def foreigner(first_stint_year: int) -> ProEntry:
    """ドラフトを経ていない外国人選手のプロ入り。"""
    entry = ProEntry.of(draft=None, is_foreign_player=True, first_stint_year=first_stint_year)
    assert entry is not None
    return entry


class FirstDomesticAcquisitionYearTest(unittest.TestCase):
    """国内 FA の最初の取得年（外国人枠の対象外の起点）。判定は `fa_outlook` と一致する。"""

    def first(self, entry, days, *, through=2040, records_from=2000, developmental=()):
        return first_domestic_acquisition_year(
            education=EducationPath.UNKNOWN,
            entry=entry,
            days_by_year=days,
            through_year=through,
            records_from_year=records_from,
            developmental_years=set(developmental),
        )

    def test_matches_the_acquired_year_of_the_outlook(self):
        days = full_years(*range(2026, 2036))
        self.assertEqual(self.first(foreigner(2026), days), 2033)
        shown = outlook(EducationPath.UNKNOWN, None, days, through=2040, entry=foreigner(2026))
        self.assertEqual(shown.domestic.acquired_year, 2033)

    def test_none_until_acquired(self):
        days = full_years(*range(2026, 2036))
        self.assertIsNone(self.first(foreigner(2026), days, through=2032))
        self.assertEqual(self.first(foreigner(2026), days, through=2033), 2033)

    def test_a_later_year_does_not_move_the_first_acquisition(self):
        days = full_years(*range(2026, 2040))
        self.assertEqual(self.first(foreigner(2026), days, through=2033), self.first(foreigner(2026), days))

    def test_the_latest_possible_year_is_returned_as_is(self):
        """記録より前の年を数えられない日本人選手は「遅くとも Y」で、その Y を返す。"""
        days = full_years(*range(2026, 2034))
        found = self.first(drafted(2018), days, records_from=2026)
        shown = outlook(EducationPath.UNKNOWN, 2018, days, through=2040, records_from=2026)
        self.assertTrue(shown.domestic.acquired_year_is_latest)
        self.assertEqual(found, shown.domestic.acquired_year)

    def test_developmental_years_delay_the_acquisition(self):
        days = full_years(*range(2026, 2036))
        self.assertEqual(self.first(foreigner(2026), days, developmental=[2026]), 2034)

    def test_unknown_entry_never_acquires(self):
        self.assertIsNone(self.first(None, full_years(*range(2026, 2040))))


class EarliestDomesticAcquisitionYearTest(unittest.TestCase):
    """取得しうる最も早い年（出場を読む前の絞り込み）。実際の取得年はこれより前にならない。"""

    def test_undrafted_foreigner_counts_from_the_later_of_entry_and_records(self):
        self.assertEqual(earliest_domestic_acquisition_year(foreigner(2018), 2026), 2033)
        self.assertEqual(earliest_domestic_acquisition_year(foreigner(2028), 2026), 2035)

    def test_the_college_rule_lowers_the_bound_to_seven_seasons(self):
        college = ProEntry.of(draft=DraftPick(2025), is_foreign_player=False, first_stint_year=None)
        assert college is not None
        self.assertEqual(earliest_domestic_acquisition_year(college, 2026), 2032)

    def test_the_bound_is_never_later_than_the_actual_first_acquisition(self):
        days = full_years(*range(2026, 2040))
        for entry in (foreigner(2026), foreigner(2020), drafted(2026), drafted(2015)):
            with self.subTest(entry=entry):
                for education in EducationPath:
                    acquired = first_domestic_acquisition_year(
                        education=education,
                        entry=entry,
                        days_by_year=days,
                        through_year=2045,
                        records_from_year=2026,
                    )
                    assert acquired is not None
                    self.assertLessEqual(earliest_domestic_acquisition_year(entry, 2026), acquired)
