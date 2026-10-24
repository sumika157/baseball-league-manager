"""戦力分析の FA 取得タブ。登録日数は試合の初出場〜最終出場から推定する。"""

from datetime import date
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.application.foreign_quota import ForeignQuotaExemptions
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoServiceHistoryQuery

from ..helpers import build_team_analysis_service, real_scope
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
        """`debut` は入団の年（ドラフトはその前年）。None ならドラフトなし。"""
        player_id = self.player(self.team, name, number, "投手", throws="右", from_year=from_year, to_year=to_year)
        orm_models.Player.objects.filter(id=player_id).update(
            draft_year=None if debut is None else debut - 1, high_school=high_school, university=university
        )
        return player_id

    def season(self, player_id, year, *, first=(4, 1), last=(9, 30)):
        """その年の初出場と最終出場の2試合。間は出場していなくても、登録日数は初〜最終で推定する。"""
        for month, day in (first, last):
            saved = self.game(self.team, self.rival, year=year, pitching=[(player_id, 1)])
            orm_models.Game.objects.filter(pk=saved.id).update(played_on=date(year, month, day))

    def analysis(self, **kwargs):
        return build_team_analysis_service(real_scope()).get_analysis(self.team.id, year=2026, tab="fa", **kwargs)

    def rows(self, **kwargs):
        return {row.name: row for row in self.analysis(**kwargs).fa_rows}

    def url(self):
        return reverse("team_analysis", args=[self.team.id])

    def test_nine_seasons_acquire_domestic_and_overseas(self):
        veteran = self.rows()["ベテラン"]
        self.assertEqual(
            (veteran.education_label, veteran.draft_label, veteran.seasons), ("高卒", "2017年ドラフト", 9)
        )
        self.assertEqual(veteran.domestic_label, "取得済み（2025年）")
        self.assertEqual(veteran.overseas_label, "取得済み（2026年）")
        self.assertTrue(veteran.includes_estimate)

    def test_declaration_restarts_the_count_for_reacquisition(self):
        """FA 宣言の翌年から数え直し、4シーズンで再び取得する（2022年に宣言 → 2023〜2026年の4シーズン）。"""
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.veteran, year=2022, kind="国内")
        veteran = self.rows()["ベテラン"]
        self.assertEqual(veteran.seasons, 4)
        self.assertEqual(veteran.domestic_label, "取得済み（2026年）")

    def test_declaration_after_the_shown_year_is_ignored(self):
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.veteran, year=2027, kind="国内")
        self.assertEqual(self.rows()["ベテラン"].domestic_label, "取得済み（2025年）")

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

    def test_undrafted_foreign_player_counts_from_the_first_recorded_year(self):
        """ドラフトの無い外国人選手は、記録より前の在籍の年を数えず、記録の最初の年から言い切る（#108）。

        海外リーグの年数を数えない NPB の規則と同じ扱い。国内FAは学歴によらず8シーズン。
        """
        foreigner = self.person("助っ人", 42, debut=None, university="米国の大学", from_year=2015)
        orm_models.Player.objects.filter(id=foreigner).update(is_foreign_player=True)
        for year in (2018, 2019):
            self.season(foreigner, year)
        row = self.rows()["助っ人"]
        self.assertEqual(row.draft_label, "")
        self.assertEqual((row.seasons, row.domestic_label, row.overseas_label), (2, "あと6シーズン", "あと7シーズン"))
        response = self.client.get(self.url(), {"year": 2026, "tab": "fa"})
        self.assertContains(response, "<th>ドラフト</th>", html=True)
        self.assertContains(response, "外国人選手（ドラフトなし）は記録のある最初の年から数えます")

    def test_undrafted_japanese_player_counts_from_the_first_stint(self):
        """ドラフトの入っていない日本人選手は、最初の在籍の加入年から数え、記録より前は「不明」のまま。"""
        unknown = self.person("ドラフト不明", 43, debut=None, high_school="H高", from_year=2015)
        self.season(unknown, 2018)
        self.assertEqual(self.rows()["ドラフト不明"].domestic_label, "不明（あと最大7シーズン）")

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
                build_team_analysis_service(real_scope()).get_analysis(self.team.id, year=2026, tab=tab)
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

    # --- 協約第82条 (4): 外国人は国内FAの取得（または FA 宣言）の翌年から外国人枠の対象外 ---

    def foreigner(self, name, number, *, from_year, seasons=()):
        """ドラフトを経ていない外国人選手。`seasons` の年に毎年1シーズン出場する。"""
        player_id = self.person(name, number, debut=None, from_year=from_year)
        orm_models.Player.objects.filter(id=player_id).update(is_foreign_player=True)
        for year in seasons:
            self.season(player_id, year)
        return player_id

    def exemptions(self, player_ids, year):
        return self.exemptions_reader().for_year(player_ids, year)

    def exemptions_reader(self) -> ForeignQuotaExemptions:
        """範囲を作る読み取り（ワークスペースを引く1本）を、クエリ数を数える区間の外で済ませる。"""
        return ForeignQuotaExemptions(DjangoServiceHistoryQuery(real_scope()))

    def test_a_foreigner_who_acquired_domestic_fa_is_marked_exempt_from_the_next_year(self):
        self.foreigner("取得助っ人", 44, from_year=2018, seasons=range(2018, 2026))

        row = self.rows()["取得助っ人"]

        self.assertEqual(row.domestic_label, "取得済み（2025年）")
        self.assertEqual(row.foreign_quota_exempt_from, 2026)
        response = self.client.get(self.url(), {"year": 2026, "tab": "fa"})
        self.assertContains(response, "外国人枠の対象外（2026年から）")

    def test_a_declaration_marks_a_foreigner_exempt_from_the_next_year(self):
        player_id = self.foreigner("宣言助っ人", 45, from_year=2024, seasons=(2024,))
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=player_id, year=2024, kind="海外")

        self.assertEqual(self.rows()["宣言助っ人"].foreign_quota_exempt_from, 2025)

    def test_only_foreigners_are_marked(self):
        """日本人選手は国内FAを取得しても外国人枠とは関係が無い（添え書きを出さない）。"""
        rows = self.rows()
        self.assertEqual(rows["ベテラン"].domestic_label, "取得済み（2025年）")
        self.assertIsNone(rows["ベテラン"].foreign_quota_exempt_from)

    def test_the_quota_uses_the_same_acquisition_as_the_tab(self):
        """枠の判定は、タブと同じ材料・同じ組み立てで取得年を導く。年 Y の扱いは Y−1 年までの記録で決まる。"""
        player_id = self.foreigner("取得助っ人", 44, from_year=2018, seasons=range(2018, 2026))

        self.assertEqual(dict(self.exemptions([player_id], 2026).exempt_from), {player_id: 2026})
        self.assertEqual(dict(self.exemptions([player_id], 2025).exempt_from), {}, "取得した年はまだ数える")
        self.assertFalse(self.exemptions([player_id], 2026).counts(player_id, is_foreign_player=True))
        self.assertTrue(self.exemptions([player_id], 2025).counts(player_id, is_foreign_player=True))

    def test_players_who_cannot_have_acquired_yet_are_not_read(self):
        """記録が浅く取得しえない外国人は、出場（重い）を読まずに外す。読むのは固定の4本だけ。"""
        player_id = self.foreigner("新しい助っ人", 46, from_year=2020, seasons=range(2020, 2026))

        with mock.patch.object(DjangoServiceHistoryQuery, "load_service_history") as load:
            self.assertEqual(dict(self.exemptions([player_id], 2026).exempt_from), {})
        load.assert_not_called()
        reader = self.exemptions_reader()
        with self.assertNumQueries(4):
            reader.for_year([player_id], 2026)

    def test_no_foreigners_reads_nothing(self):
        reader = self.exemptions_reader()
        with self.assertNumQueries(0):
            self.assertEqual(dict(reader.for_year([], 2026).exempt_from), {})
