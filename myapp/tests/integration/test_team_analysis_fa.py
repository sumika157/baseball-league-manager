"""戦力分析の FA 取得タブ。登録日数は試合の初出場〜最終出場から推定する。"""

from datetime import date

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import build_team_analysis_service
from .test_team_analysis import AnalysisCase


class TeamFaTabTest(AnalysisCase):
    def setUp(self):
        super().setUp()
        # 2018年から毎年、4/1 と 9/30 に出場する高卒の選手（毎年145日以上）
        self.veteran = self.person("ベテラン", 10, debut=2018, high_school="A高", from_year=2018)
        self.rookie = self.person("新人大卒", 11, debut=2026, university="B大", from_year=2026)
        self.mystery = self.person("学歴不明", 12, debut=2020, from_year=2020)
        self.old = self.person("記録前入団", 13, debut=2010, high_school="C高", from_year=2010)
        self.left = self.person("退団済み", 14, debut=2018, high_school="D高", from_year=2018, to_year=2025)
        for year in range(2018, 2027):
            self.season(self.veteran, year)
        self.season(self.old, 2026)
        self.game(self.team, self.rival, pitching=[(self.rookie, 1), (self.mystery, 1)])

    def person(self, name, number, *, debut, high_school="", university="", from_year, to_year=None):
        player_id = self.player(self.team, name, number, "投手", throws="右", from_year=from_year, to_year=to_year)
        orm_models.Player.objects.filter(id=player_id).update(
            debut_year=debut, high_school=high_school, university=university
        )
        return player_id

    def season(self, player_id, year, *, first=(4, 1), last=(9, 30)):
        """その年の初出場と最終出場の2試合。間は出場していなくても、登録日数は初〜最終で推定する。"""
        for month, day in (first, last):
            saved = self.game(self.team, self.rival, year=year, pitching=[(player_id, 1)])
            orm_models.Game.objects.filter(pk=saved.id).update(played_on=date(year, month, day))

    def analysis(self, **kwargs):
        return build_team_analysis_service().get_analysis(self.team.id, year=2026, tab="fa", **kwargs)

    def rows(self, **kwargs):
        return {row.name: row for row in self.analysis(**kwargs).fa_rows}

    def url(self):
        return reverse("team_analysis", args=[self.team.id])

    def test_nine_seasons_acquire_domestic_and_overseas(self):
        veteran = self.rows()["ベテラン"]
        self.assertEqual((veteran.education_label, veteran.debut_year, veteran.seasons), ("高卒", 2018, 9))
        self.assertEqual(veteran.domestic_label, "取得済み（2025年）")
        self.assertEqual(veteran.overseas_label, "取得済み（2026年）")
        self.assertTrue(veteran.includes_estimate)

    def test_days_are_estimated_from_first_to_last_appearance(self):
        # 初出場から最終出場までが145日未満の年は、日数を合算して数える（4/1〜5/20 は50日）
        short = self.person("短い年", 20, debut=2025, high_school="E高", from_year=2025)
        self.season(short, 2025, first=(4, 1), last=(5, 20))
        self.season(short, 2026, first=(4, 1), last=(6, 30))
        row = self.rows()["短い年"]
        # 50日 + 91日 = 141日（1シーズンに満たない）
        self.assertEqual((row.seasons, row.remainder_days), (0, 141))

    def test_shortened_university_rule_and_remaining_seasons(self):
        rookie = self.rows()["新人大卒"]
        self.assertEqual((rookie.education_label, rookie.seasons, rookie.remainder_days), ("大卒", 0, 1))
        self.assertEqual(rookie.domestic_label, "あと7シーズン")
        self.assertEqual(rookie.overseas_label, "あと9シーズン")

    def test_unknown_education_makes_only_domestic_unknown(self):
        """海外FAは学歴に関係なく9シーズンなので、学歴が分からなくても数える（外国人選手など）。"""
        row = self.rows()["学歴不明"]
        self.assertEqual(
            (row.education_label, row.domestic_label, row.overseas_label), ("不明", "不明", "あと9シーズン")
        )

    def test_debut_before_the_records_is_unknown_with_an_upper_bound(self):
        row = self.rows()["記録前入団"]
        self.assertEqual(row.domestic_label, "不明（あと最大7シーズン）")

    def test_only_players_on_the_roster_in_the_year(self):
        self.assertNotIn("退団済み", self.rows())
        self.assertEqual(len(self.rows()), 4)

    def test_acquired_come_first_then_nearest(self):
        names = [row.name for row in self.analysis().fa_rows]
        self.assertEqual(names[0], "ベテラン")
        self.assertEqual(names[-1], "学歴不明")
        self.assertLess(names.index("新人大卒"), names.index("記録前入団"))

    def test_developmental_years_are_not_counted(self):
        trainee = self.person("育成上がり", 30, debut=2022, high_school="F高", from_year=2022)
        orm_models.PlayerStint.objects.filter(player_id=trainee).update(
            signed_as="育成", promoted_year=2025, number_before_promotion=130
        )
        for year in range(2022, 2027):
            self.season(trainee, year)
        row = self.rows()["育成上がり"]
        # 2022〜2024 は育成なので数えない。2025・2026 の2シーズン
        self.assertEqual(row.seasons, 2)

    def test_page_states_the_estimate_and_the_records_limit(self):
        response = self.client.get(self.url(), {"year": 2026, "tab": "fa"})
        self.assertContains(response, "FA取得")
        self.assertContains(response, "登録日数は試合への出場（初出場〜最終出場）から推定した値です")
        self.assertContains(response, "試合の記録は2018年から")
        self.assertContains(response, "145日で1シーズン")
        self.assertContains(response, "取得済み（2025年）")
        self.assertContains(response, "player-tone-hand-right")

    def test_legend_counts_the_players_shown(self):
        legend = {item.category.label: item.count for item in self.analysis().legend}
        self.assertEqual(legend["右"], 4)

    def test_other_tabs_do_not_read_the_career(self):
        def count(tab):
            with CaptureQueriesContext(connection) as captured:
                build_team_analysis_service().get_analysis(self.team.id, year=2026, tab=tab)
            return len(captured)

        self.assertGreater(count("fa"), count("depth"))
        self.assertEqual(count("depth"), count("usage"))

    def test_query_count_does_not_grow_with_roster(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self.analysis()
            return len(captured)

        before = count()
        for number in range(40, 60):
            player_id = self.person(f"控え{number}", number, debut=2020, high_school="G高", from_year=2020)
            self.season(player_id, 2025)
        self.assertEqual(count(), before)
