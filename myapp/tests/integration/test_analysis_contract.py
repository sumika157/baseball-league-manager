"""戦力分析のデプス表の支配下・育成（人数・バッジ）と、年ごとの背番号。

その年の区分は `Stint.contract_in(年)`、背番号は `Stint.number_in(年)` が出典。
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import build_team_analysis_service
from .test_team_analysis import AnalysisCase

BADGE = '<span class="badge bg-primary">育成</span>'


class DepthContractTest(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.regular = self.player(self.team, "支配下の先発", 18, "投手", throws="右")
        # 2024年に育成（120）で入団し、2026年に支配下（30）へ上がった
        self.promoted = self.player(self.team, "昇格した投手", 30, "投手", throws="左", from_year=2024)
        orm_models.PlayerStint.objects.filter(player_id=self.promoted).update(
            signed_as="育成", promoted_year=2026, number_before_promotion=120
        )
        # 育成のまま（入団した年から）
        self.trainee = self.player(self.team, "育成の野手", 25, "内野手", bats="右", from_year=2025)
        orm_models.PlayerStint.objects.filter(player_id=self.trainee).update(signed_as="育成", number=125)
        for year in (2025, 2026):
            self.game(self.team, self.rival, year=year, pitching=[(self.regular, 1), (self.promoted, 2)])

    def analysis(self, **kwargs):
        return build_team_analysis_service().get_analysis(self.team.id, **kwargs)

    def url(self, **params):
        return reverse("team_analysis", args=[self.team.id]) + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    def test_the_promotion_year_is_registered_and_the_year_before_is_developmental(self):
        before = self.analysis(year=2025)
        after = self.analysis(year=2026)

        self.assertTrue(self.find(before.pitchers, "昇格した投手").is_developmental)
        self.assertFalse(self.find(after.pitchers, "昇格した投手").is_developmental)
        self.assertFalse(self.find(before.pitchers, "支配下の先発").is_developmental)

    def test_the_number_is_the_one_worn_in_that_year(self):
        before = self.analysis(year=2025)
        after = self.analysis(year=2026)

        self.assertEqual(self.find(before.pitchers, "昇格した投手").number, "120")
        self.assertEqual(self.find(after.pitchers, "昇格した投手").number, "30")

    def test_counts_per_row_and_table(self):
        analysis = self.analysis(year=2025)

        # 2025年: 投手は 支配下1（支配下の先発）・育成1（昇格前の投手）、野手は育成1
        self.assertEqual(
            (analysis.pitchers.count, analysis.pitchers.registered_count, analysis.pitchers.developmental_count),
            (2, 1, 1),
        )
        self.assertEqual(
            (analysis.fielders.count, analysis.fielders.registered_count, analysis.fielders.developmental_count),
            (1, 0, 1),
        )
        self.assertEqual((analysis.registered_count, analysis.developmental_count), (1, 2))
        # 行の人数の合計が表の人数に一致する
        for table in (analysis.pitchers, analysis.fielders):
            self.assertEqual(sum(r.registered_count for r in table.rows), table.registered_count)
            self.assertEqual(sum(r.developmental_count for r in table.rows), table.developmental_count)
            for row in table.rows:
                self.assertEqual(row.registered_count + row.developmental_count, row.count)

    def test_counts_after_the_promotion(self):
        analysis = self.analysis(year=2026)

        self.assertEqual((analysis.registered_count, analysis.developmental_count), (2, 1))

    def test_the_league_limit_is_carried_for_the_header(self):
        self.assertEqual(self.analysis(year=2026).registered_limit, 70)
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=None)
        self.assertIsNone(self.analysis(year=2026).registered_limit)

    def test_the_page_shows_the_badge_for_the_developmental_only(self):
        before = self.client.get(self.url(year=2025))
        after = self.client.get(self.url(year=2026))

        # 昇格前の投手と育成の野手に加え、育成の野手は2025年の加入なので入退団の表にも出る
        self.assertContains(before, BADGE, count=3)
        self.assertContains(after, BADGE, count=1)  # 育成の野手だけ
        self.assertContains(before, "支配下 1 / 70名 · 育成 2名")
        self.assertContains(after, "支配下 2 / 70名 · 育成 1名")

    def test_the_header_without_a_limit_has_no_slash(self):
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=None)

        self.assertContains(self.client.get(self.url(year=2026)), "支配下 2名 · 育成 1名")

    def test_usage_map_uses_the_number_of_the_year(self):
        self.game(
            self.team,
            self.rival,
            year=2025,
            day=2,
            batting=[(self.trainee, self.team, "遊", 0)],
        )
        analysis = self.analysis(year=2025, tab="usage")

        numbers = {p.name: p.number for box in analysis.usage_boxes for p in box.players}
        self.assertEqual(numbers["昇格した投手"], "120")

    def test_moves_use_the_number_of_the_year_and_the_badge(self):
        # 昇格した投手は2024年に加入。2024年の入退団の表では昇格前の番号・育成のバッジ
        self.game(self.team, self.rival, year=2024, pitching=[(self.regular, 1)])
        joined = self.analysis(year=2024).joiners

        row = next(r for r in joined if r.name == "昇格した投手")
        self.assertEqual(row.number, "120")
        self.assertTrue(row.is_developmental)

    def test_query_count_does_not_depend_on_the_contracts(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self.analysis(year=2026)
            return len(captured)

        before = count()
        for number in range(160, 180):
            player_id = self.player(self.team, f"育成{number}", number - 100, "投手", throws="右")
            orm_models.PlayerStint.objects.filter(player_id=player_id).update(signed_as="育成", number=number)
        self.assertEqual(count(), before)
