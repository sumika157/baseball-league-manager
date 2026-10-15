"""成績の一覧表の出場試合数（野手）と登板数（投手）の列。

選手一覧（戦力分析の成績一覧も同じ表）とリーグの成績一覧に並べ、見出しで並べ替えられる。
"""

from django.urls import reverse

from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine

from ..helpers import give_batting, give_pitching
from .base import BaseCase


class GamesPlayedColumnTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.regular = self.roster.register_player(self.team.id, "正選手", 1, "内野手")
        self.bench = self.roster.register_player(self.team.id, "控え", 2, "外野手")
        self.starter = self.roster.register_player(self.team.id, "先発", 11, "投手")
        self.reliever = self.roster.register_player(self.team.id, "中継ぎ", 12, "投手")
        for day in (1, 2, 3):
            give_batting(self.team, self.rival, self.regular.id, BattingLine(at_bats=4, singles=1), day=day)
            give_pitching(
                self.team,
                self.rival,
                self.reliever.id,
                PitchingLine(innings=InningsPitched.from_notation("1.0")),
                day=day,
            )
        # 打席の回らなかった途中出場も1試合に数える
        give_batting(self.team, self.rival, self.bench.id, BattingLine(), day=4)
        give_pitching(
            self.team, self.rival, self.starter.id, PitchingLine(innings=InningsPitched.from_notation("7.0")), day=4
        )

    def test_the_roster_lists_games_and_appearances(self):
        batters = {row.name: row.games for row in self.service.list_batters(self.team.id).rows}
        pitchers = {row.name: row.appearances for row in self.service.list_pitchers(self.team.id).rows}

        self.assertEqual(batters, {"正選手": 3, "控え": 1})
        self.assertEqual(pitchers, {"先発": 1, "中継ぎ": 3})

    def test_the_roster_sorts_by_games_and_appearances_most_first(self):
        batters = self.service.list_batters(self.team.id, sort="games")
        pitchers = self.service.list_pitchers(self.team.id, sort="appearances")

        self.assertEqual((batters.sort, batters.descending), ("games", True))
        self.assertEqual([row.name for row in batters.rows], ["正選手", "控え"])
        self.assertEqual((pitchers.sort, pitchers.descending), ("appearances", True))
        self.assertEqual([row.name for row in pitchers.rows], ["中継ぎ", "先発"])

    def test_league_stats_list_and_sort_by_games_and_appearances(self):
        batters = self.service.get_league_stats(self.league.id, sort="games", descending=False).listing
        pitchers = self.service.get_league_stats(self.league.id, pitchers=True, sort="appearances").listing

        self.assertEqual([(r.player.name, r.player.games) for r in batters.rows], [("控え", 1), ("正選手", 3)])
        self.assertEqual([(r.player.name, r.player.appearances) for r in pitchers.rows], [("中継ぎ", 3), ("先発", 1)])

    def test_pages_show_the_columns(self):
        pages = (
            (reverse("player_list", args=[self.team.id]), "試合"),
            (reverse("player_list", args=[self.team.id]) + "?pos=pitcher", "登板"),
            (reverse("league_stats", args=[self.league.id]), "試合"),
            (reverse("league_stats", args=[self.league.id]) + "?pos=pitcher", "登板"),
        )
        for url, heading in pages:
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertContains(response, "sort=" + ("appearances" if heading == "登板" else "games"))
                self.assertContains(response, f">{heading}")
