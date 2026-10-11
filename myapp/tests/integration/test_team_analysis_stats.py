"""戦力分析の成績一覧タブ。その年に在籍した選手のその年の成績を、選手一覧と同じ表で見せる。"""

from datetime import date

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine
from myapp.infrastructure import orm_models

from ..helpers import give_batting, give_pitching, play_game
from .test_team_analysis import AnalysisCase


class TeamStatsTabBase(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.slugger = self.player(self.team, "強打者", 3, "外野手")
        self.contact = self.player(self.team, "巧打者", 4, "内野手")
        self.ace = self.player(self.team, "エース", 18, "投手")
        self.rival_batter = self.player(self.rival, "相手打者", 33, "外野手")
        give_batting(self.team, self.rival, self.slugger, BattingLine(at_bats=10, singles=2, home_runs=3))
        give_batting(self.team, self.rival, self.contact, BattingLine(at_bats=10, singles=6), day=2)
        give_pitching(
            self.team, self.rival, self.ace, PitchingLine(innings=InningsPitched(outs=27), strikeouts=9), day=3
        )
        give_batting(self.team, self.rival, self.rival_batter, BattingLine(at_bats=4, singles=4), day=4)

    def url(self, team=None):
        return reverse("team_analysis", args=[(team or self.team).id])

    def get(self, **params):
        return self.client.get(self.url(), {"tab": "stats", **params})

    @staticmethod
    def names(response):
        return [row.name for row in response.context["players"]]


class TeamStatsTabTest(TeamStatsTabBase):
    def test_tab_is_listed_and_shows_batters_by_default(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([tab.key for tab in response.context["analysis"].tabs][-1], "stats")
        self.assertEqual(response.context["pos_mode"], "batter")
        self.assertCountEqual(self.names(response), ["強打者", "巧打者"])
        self.assertContains(response, "2026年の成績")
        self.assertContains(response, "OPS+")
        # 選手一覧（通算）への案内ではなく、この年の成績が見られるタブへの案内になっている
        self.assertNotContains(response, "この年だけの成績ではありません")

    def test_pitcher_mode(self):
        response = self.get(pos="pitcher")
        self.assertEqual(self.names(response), ["エース"])
        self.assertContains(response, "ERA+")
        self.assertNotContains(response, "OPS+")

    def test_only_that_years_stats_are_counted(self):
        give_batting(self.team, self.rival, self.slugger, BattingLine(at_bats=20, singles=20), year=2025)
        rows = {r.name: r for r in self.get().context["players"]}
        self.assertEqual((rows["強打者"].at_bats, rows["強打者"].hits), (10, 5))
        rows_2025 = {r.name: r for r in self.get(year=2025).context["players"]}
        self.assertEqual((rows_2025["強打者"].at_bats, rows_2025["強打者"].hits), (20, 20))

    def test_departed_player_of_that_year_is_listed_but_not_in_later_year(self):
        gone = self.player(self.team, "退団済み", 40, "内野手", to_year=2026)
        give_batting(self.team, self.rival, gone, BattingLine(at_bats=5, singles=1), day=5)
        self.assertIn("退団済み", self.names(self.get()))
        play_game(self.team, self.rival, year=2027)
        self.assertNotIn("退団済み", self.names(self.get(year=2027)))

    def test_mid_season_transfer_is_listed_on_both_clubs_with_all_games_counted(self):
        mover = self.player(self.rival, "シーズン途中", 7, "外野手", from_year=2020, to_year=2026)
        orm_models.PlayerStint.objects.create(player_id=mover, team=self.team, number="8", from_year=2026)
        give_batting(self.rival, self.team, mover, BattingLine(at_bats=4, singles=2), day=6)
        give_batting(self.team, self.rival, mover, BattingLine(at_bats=6, singles=1), day=7)
        row = next(r for r in self.get().context["players"] if r.name == "シーズン途中")
        self.assertEqual((row.at_bats, row.hits), (10, 3))
        rival_page = self.client.get(self.url(self.rival), {"tab": "stats"})
        rival_row = next(r for r in rival_page.context["players"] if r.name == "シーズン途中")
        self.assertEqual((rival_row.at_bats, rival_row.hits), (10, 3))

    def test_sort_and_default_direction(self):
        by_hits = self.get(sort="hits")
        self.assertEqual(self.names(by_hits)[0], "巧打者")
        self.assertEqual((by_hits.context["current_sort"], by_hits.context["current_descending"]), ("hits", True))
        ascending = self.get(sort="hits", dir="asc")
        self.assertEqual(self.names(ascending)[0], "強打者")

    def test_invalid_values_fall_back_to_defaults(self):
        response = self.get(pos="catcher", sort="nonsense", dir="sideways")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["pos_mode"], "batter")
        self.assertCountEqual(self.names(response), ["強打者", "巧打者"])
        self.assertEqual(response.context["current_sort"], self.get().context["current_sort"])

    def test_sort_links_keep_tab_year_and_color(self):
        response = self.get(year="2026", color="natural")
        self.assertContains(response, "tab=stats")
        self.assertContains(response, "color=natural&amp;sort=hits")

    def test_year_and_team_switches_keep_the_tab_and_batter_pitcher_choice(self):
        give_pitching(self.team, self.rival, self.ace, PitchingLine(innings=InningsPitched(outs=3)), year=2025)
        response = self.get(pos="pitcher", sort="strikeouts", dir="asc")
        self.assertContains(
            response, "?year=2025&amp;tab=stats&amp;color=hand&amp;pos=pitcher&amp;sort=strikeouts&amp;dir=asc"
        )
        self.assertContains(response, '<input type="hidden" name="pos" value="pitcher" />')
        self.assertContains(response, '<input type="hidden" name="tab" value="stats" />')

    def test_team_switch_goes_through_the_index_with_pos_and_sort(self):
        response = self.client.get(
            reverse("analysis_index"), {"team": self.rival.id, "tab": "stats", "pos": "pitcher", "sort": "wins"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=stats", response["Location"])
        self.assertIn("pos=pitcher", response["Location"])
        self.assertIn("sort=wins", response["Location"])

    def test_other_tabs_do_not_read_the_stats(self):
        response = self.client.get(self.url(), {"tab": "usage"})
        self.assertNotIn("players", response.context)
        self.assertNotContains(response, "2026年の成績")

    def test_anonymous_sees_no_edit_controls(self):
        response = self.get()
        self.assertNotContains(response, "編集")

    def test_query_count_does_not_grow_with_roster(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self.get()
            return len(captured)

        before = count()
        for number in range(40, 70):
            player_id = self.player(self.team, f"控え{number}", number, "外野手")
            give_batting(self.team, self.rival, player_id, BattingLine(at_bats=3, singles=1), day=number % 28 + 1)
        self.assertEqual(count(), before)


class TeamStatsTabToneTest(TeamStatsTabBase):
    """成績一覧の選手名にも、ページ全体の色分けを付ける。"""

    def setUp(self):
        super().setUp()
        orm_models.Player.objects.filter(id=self.slugger).update(bats="左")
        orm_models.Player.objects.filter(id=self.contact).update(bats="右")
        orm_models.Player.objects.filter(id=self.ace).update(throws="左")

    def legend_counts(self, response):
        return {item.category.key: item.count for item in response.context["stats"].legend if item.count}

    def test_names_carry_the_tone_class_of_the_hand_axis(self):
        response = self.get()
        by_name = {row.name: row.tone.key for row in response.context["players"]}
        self.assertEqual(by_name["強打者"], "hand-left")
        self.assertEqual(by_name["巧打者"], "hand-right")
        self.assertContains(response, 'class="player-tone player-tone-hand-left"')
        self.assertContains(response, 'class="player-tone player-tone-hand-right"')

    def test_natural_axis_is_used_when_chosen(self):
        response = self.get(color="natural")
        for row in response.context["players"]:
            self.assertIn(row.tone.key, {"natural-yes", "natural-no", "neutral"})
        self.assertContains(response, 'name="color" value="natural"')

    def test_legend_counts_match_the_rows_for_batters_and_pitchers(self):
        batters = self.get()
        self.assertEqual(sum(self.legend_counts(batters).values()), 2)
        self.assertEqual(self.legend_counts(batters), {"hand-left": 1, "hand-right": 1})
        pitchers = self.get(pos="pitcher")
        self.assertEqual(self.legend_counts(pitchers), {"hand-left": 1})

    def test_player_list_has_no_tone_class(self):
        response = self.client.get(reverse("player_list", args=[self.team.id]))
        self.assertNotContains(response, "player-tone")

    def test_player_without_games_is_listed_and_counted_in_the_legend(self):
        extra = self.player(self.team, "控え", 50, "外野手")
        response = self.get()
        self.assertIn(extra, [row.id for row in response.context["players"]])
        self.assertEqual(sum(self.legend_counts(response).values()), 3)

    def test_empty_table_shows_zero_legend_not_the_depth_counts(self):
        lonely = orm_models.Team.objects.create(league=self.league, name="野手だけ")
        self.player(lonely, "ただの野手", 1, "外野手", from_year=date.today().year)
        response = self.client.get(self.url(lonely), {"tab": "stats", "pos": "pitcher"})
        self.assertEqual(list(response.context["players"]), [])
        legend = response.context["stats"].legend
        self.assertTrue(legend)
        self.assertEqual([item.count for item in legend], [0] * len(legend))
        # デプス表の凡例（野手が1名）にはなっていない
        self.assertTrue(any(item.count for item in response.context["analysis"].legend))
        self.assertNotContains(response, "1名</span>")

    def test_stats_players_match_the_depth_roster_with_departed_and_transferred_players(self):
        gone = self.player(self.team, "退団済み", 40, "内野手", to_year=2026)
        give_batting(self.team, self.rival, gone, BattingLine(at_bats=5, singles=1), day=5)
        mover = self.player(self.rival, "シーズン途中", 7, "外野手", from_year=2020, to_year=2026)
        orm_models.PlayerStint.objects.create(player_id=mover, team=self.team, number="8", from_year=2026)
        give_batting(self.rival, self.team, mover, BattingLine(at_bats=4, singles=2), day=6)
        pitcher_gone = self.player(self.team, "退団投手", 41, "投手", to_year=2026)
        give_pitching(self.team, self.rival, pitcher_gone, PitchingLine(innings=InningsPitched(outs=3)), day=7)
        stats_ids = {row.id for pos in ("batter", "pitcher") for row in self.get(pos=pos).context["players"]}
        roster_ids = {entry.player_id for entry in self.get().context["analysis"].roster_tones}
        self.assertEqual(stats_ids, roster_ids)
        self.assertTrue({gone, mover, pitcher_gone} <= stats_ids)
