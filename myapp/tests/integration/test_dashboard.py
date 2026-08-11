"""ダッシュボード（ホーム画面）の概況とランキング。"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    PitchingLine,
)
from myapp.infrastructure import orm_models

from ..helpers import (
    give_batting,
    play_game,
)
from .base import BaseCase


class DashboardTest(BaseCase):
    def test_counts(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.register_player(self.team.id, "佐藤", 18, "投手")

        board = self.service.get_dashboard()

        self.assertEqual(board.team_count, 2)
        self.assertEqual(board.batter_count, 1)
        self.assertEqual(board.pitcher_count, 1)

    def test_ranking_spans_teams_within_a_league(self):
        a = self.service.register_player(self.team.id, "山田", 10, "内野手")
        b = self.service.register_player(self.rival.id, "田中", 10, "外野手")
        play_game(
            self.team,
            self.rival,
            batting={
                a.id: BattingLine(at_bats=10, singles=1),
                b.id: BattingLine(at_bats=10, home_runs=4),
            },
        )

        rankings = self.service.get_dashboard().leagues[0].rankings

        self.assertEqual([e.player_name for e in rankings.average_leaders], ["田中", "山田"])
        self.assertEqual(rankings.average_leaders[0].team_name, "相手チーム")
        self.assertEqual(rankings.average_leaders[0].value, ".400")

    def test_rankings_are_split_by_league(self):
        """タイトルはリーグの中で争われる。他リーグの選手と同じ表に並べない。"""
        other = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other, name="Xチーム")
        y = orm_models.Team.objects.create(league=other, name="Yチーム")

        here = self.service.register_player(self.team.id, "当リーグ", 10, "内野手")
        there = self.service.register_player(x.id, "別リーグ選手", 10, "内野手")
        play_game(self.team, self.rival, batting={here.id: BattingLine(at_bats=10, singles=3)})
        play_game(x, y, batting={there.id: BattingLine(at_bats=10, home_runs=5)})

        rankings = {
            g.league_name: [e.player_name for e in g.rankings.average_leaders]
            for g in self.service.get_dashboard().leagues
        }

        self.assertEqual(rankings["テストリーグ"], ["当リーグ"])
        self.assertEqual(rankings["別リーグ"], ["別リーグ選手"])

    def test_win_and_save_rankings(self):
        """投手のランキングは防御率・勝利・セーブ。NPB の個人成績ページにならう。"""
        ace = self.service.register_player(self.team.id, "エース", 18, "投手")
        closer = self.service.register_player(self.team.id, "守護神", 22, "投手")
        play_game(
            self.team,
            self.rival,
            pitching={
                ace.id: PitchingLine(innings=InningsPitched.from_notation("8.0"), wins=1),
                closer.id: PitchingLine(innings=InningsPitched.from_notation("1.0"), saves=1),
            },
        )

        rankings = self.service.get_dashboard().leagues[0].rankings

        self.assertEqual([e.player_name for e in rankings.win_leaders], ["エース"])
        self.assertEqual([e.player_name for e in rankings.save_leaders], ["守護神"])

    def test_rbi_ranking(self):
        slugger = self.service.register_player(self.team.id, "大砲", 3, "内野手")
        give_batting(self.team, self.rival, slugger.id, BattingLine(at_bats=10, home_runs=2, runs_batted_in=5))

        rankings = self.service.get_dashboard().leagues[0].rankings

        self.assertEqual(rankings.rbi_leaders[0].player_name, "大砲")
        self.assertEqual(rankings.rbi_leaders[0].value, "5")

    def test_ranking_names_link_to_the_player_page(self):
        """選手名の行き先を、選手一覧・選手検索とそろえる。

        ここだけ編集画面へ飛んでいた。読みに来た人を書き込み画面へ
        送ることになるうえ、未ログインだとログイン画面に弾かれる。
        """
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        play_game(
            self.team,
            self.rival,
            batting={player.id: BattingLine(at_bats=10, home_runs=3)},
        )

        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn(reverse("player_detail", args=[self.team.id, player.id]), body)
        self.assertNotIn(reverse("player_edit", args=[self.team.id, player.id]), body)

    def test_league_without_records_still_has_a_tab(self):
        """チームがあるリーグは、記録が無くてもタブを出す（チーム一覧を見るため）。"""
        league = self.service.get_dashboard().leagues[0]

        self.assertEqual(league.league_name, "テストリーグ")
        self.assertFalse(league.rankings.has_any)
        self.assertEqual(league.standings, [])

    def test_page_renders(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テストチーム")

    def test_teams_are_grouped_into_league_tabs(self):
        """チームが増えると1つの並びでは読みにくいため、リーグごとに分ける。"""
        other = orm_models.League.objects.create(name="別リーグ")
        orm_models.Team.objects.create(league=other, name="Xチーム")

        board = self.service.get_dashboard()

        self.assertEqual(
            {g.league_name: [t.name for t in g.teams] for g in board.leagues},
            {"テストリーグ": ["テストチーム", "相手チーム"], "別リーグ": ["Xチーム"]},
        )

    def test_flat_team_list_is_still_available(self):
        self.assertEqual(len(self.service.get_dashboard().teams), 2)

    def test_first_league_tab_is_selected(self):
        other = orm_models.League.objects.create(name="別リーグ")
        orm_models.Team.objects.create(league=other, name="Xチーム")
        body = self.client.get(reverse("dashboard")).content.decode()

        # 最初のリーグのタブだけが選択済みで、対応する中身が表示される
        self.assertRegex(body, rf'tab-pane fade show active"\s+id="league-pane-{self.league.id}"')
        self.assertNotRegex(body, rf'tab-pane fade show active"\s+id="league-pane-{other.id}"')

    def test_each_league_has_a_tab_and_a_pane(self):
        other = orm_models.League.objects.create(name="別リーグ")
        orm_models.Team.objects.create(league=other, name="Xチーム")
        body = self.client.get(reverse("dashboard")).content.decode()

        for league in (self.league, other):
            with self.subTest(league=league.name):
                self.assertIn(f"#league-pane-{league.id}", body)
                self.assertIn(f'id="league-pane-{league.id}"', body)

    def test_leagues_without_teams_are_omitted(self):
        orm_models.League.objects.create(name="空リーグ")
        board = self.service.get_dashboard()

        self.assertNotIn("空リーグ", [g.league_name for g in board.leagues])

    def test_standings_show_the_latest_season(self):
        """順位表カードは最新シーズンのもの。年は選ばせず、概況に徹する。"""
        play_game(self.team, self.rival, home_score=5, away_score=3, year=2025)
        play_game(self.team, self.rival, home_score=2, away_score=7, year=2026)

        league = self.service.get_dashboard().leagues[0]

        self.assertEqual(league.standings_year, 2026)
        self.assertEqual([r.team_name for r in league.standings], ["相手チーム", "テストチーム"])

    def test_standings_replace_the_team_list(self):
        """右カードは順位表だけ。同じチームの並びを二重に出さない。"""
        play_game(self.team, self.rival)
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn("<span>順位表</span>", body)
        # チームの一覧に差し替わっていない（在籍人数はそこにしか出ない）
        self.assertNotIn("<span>チーム</span>", body)
        self.assertNotIn("tile-list-meta", body)

    def test_team_list_stands_in_when_no_game_has_been_played(self):
        """1試合も無いリーグでは順位表が作れないので、チーム一覧に差し替える。

        登録したばかりのチームが概況のどこにも出ない状態を作らないため。
        """
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn("<span>チーム</span>", body)
        self.assertIn("tile-list-meta", body)
        self.assertIn("テストチーム", body)
        self.assertIn("まだ試合が行われていません。", body)

    def test_recent_games_are_newest_first(self):
        """右カラムの高さを埋めるだけでなく、「いま何が起きているか」を出す。"""
        play_game(self.team, self.rival, day=1)
        play_game(self.rival, self.team, day=3)
        play_game(self.team, self.rival, day=2)

        league = self.service.get_dashboard().leagues[0]

        self.assertEqual([g.played_on.day for g in league.recent_games], [3, 2, 1])

    def test_recent_games_are_capped(self):
        """概況なので件数を絞る。さかのぼるのは試合一覧が受け持つ。"""
        for day in range(1, 9):
            play_game(self.team, self.rival, day=day)

        league = self.service.get_dashboard().leagues[0]

        self.assertEqual(len(league.recent_games), 5)

    def test_recent_games_stay_within_the_league(self):
        """他リーグの試合を混ぜない（順位表と同じ範囲だけを見る）。"""
        other = orm_models.League.objects.create(name="別リーグ")
        a = orm_models.Team.objects.create(league=other, name="Xチーム")
        b = orm_models.Team.objects.create(league=other, name="Yチーム")
        play_game(self.team, self.rival, day=1)
        play_game(a, b, day=2)

        boards = {g.league_name: g.recent_games for g in self.service.get_dashboard().leagues}

        self.assertEqual([r.home_team_name for r in boards["テストリーグ"]], ["テストチーム"])
        self.assertEqual([r.home_team_name for r in boards["別リーグ"]], ["Xチーム"])

    def test_recent_games_card_links_to_each_game(self):
        game = play_game(self.team, self.rival)
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn("<span>直近の試合</span>", body)
        self.assertIn(reverse("game_detail", args=[game.id]), body)

    def test_ranking_cards_link_to_the_league_stats(self):
        """各ランキングカードから、その部門で並べた成績一覧へ飛べる。"""
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=10, home_runs=3))
        body = self.client.get(reverse("dashboard")).content.decode()

        stats_url = reverse("league_stats", args=[self.league.id])
        for query in (
            "pos=batter&amp;sort=average",
            "pos=batter&amp;sort=home_runs",
            "pos=batter&amp;sort=rbi",
            "pos=pitcher&amp;sort=era",
            "pos=pitcher&amp;sort=wins",
            "pos=pitcher&amp;sort=saves",
        ):
            with self.subTest(query=query):
                self.assertIn(f"{stats_url}?{query}", body)
        self.assertIn("成績一覧を見る", body)

    def test_standings_card_does_not_read_game_details(self):
        """順位表は得点だけで決まる。明細まで読むと試合数ぶん重くなる。

        試合を増やしてもクエリ数が変わらないことで、明細を読んでいないと分かる。
        """
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        play_game(self.team, self.rival, day=1, batting={player.id: BattingLine(at_bats=4, singles=2)})

        with CaptureQueriesContext(connection) as first:
            self.client.get(reverse("dashboard"))

        for day in range(2, 12):
            play_game(self.team, self.rival, day=day, batting={player.id: BattingLine(at_bats=4, singles=1)})

        with CaptureQueriesContext(connection) as grown:
            self.client.get(reverse("dashboard"))

        self.assertEqual(len(grown.captured_queries), len(first.captured_queries))

    def test_page_renders_without_any_data(self):
        orm_models.Team.objects.all().delete()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
