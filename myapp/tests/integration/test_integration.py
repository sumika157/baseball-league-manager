"""結合テスト。リポジトリの永続化と画面の動作を確認する。

成績は試合の記録から集計されるため、成績を持たせたい場合は
helpers の play_game / give_batting / give_pitching で試合を作る。
"""

import re
from datetime import date

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.entities import Game
from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
    ForeignPlayerQuotaExceeded,
    InvalidGame,
)
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
    Season,
    StadiumProfile,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoTeamRepository,
)

from ..helpers import (
    api_inning_rows,
    build_service,
    give_batting,
    give_pitching,
    login_as_manager,
    play_game,
    post_game_update,
)


class BaseCase(TestCase):
    def setUp(self):
        self.league = orm_models.League.objects.create(name="テストリーグ")
        self.stadium = orm_models.Stadium.objects.create(name="テスト球場", city="東京")
        self.team = orm_models.Team.objects.create(league=self.league, name="テストチーム", home_stadium=self.stadium)
        self.rival = orm_models.Team.objects.create(league=self.league, name="相手チーム")
        self.service = build_service()


class RepositoryRoundTripTest(BaseCase):
    """ORM ⇄ ドメインの往復でデータが失われないこと。"""

    def setUp(self):
        super().setUp()
        self.repo = DjangoTeamRepository()

    def test_save_and_reload_a_player(self):
        team = self.repo.find_by_id(self.team.id)
        player = team.add_player("山田", JerseyNumber(10), Position.INFIELDER)
        self.repo.save(team)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.name, "山田")
        self.assertEqual(saved.number.value, 10)
        self.assertEqual(saved.position, Position.INFIELDER)

    def test_batting_totals_come_from_games(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=2), day=1)
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=3, home_runs=1), day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 7)
        self.assertEqual(saved.batting.hits, 3)

    def test_innings_are_added_as_outs_not_decimals(self):
        """5.2 + 5.2 は 10.4 ではなく 11.1。"""
        player = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        line = PitchingLine(innings=InningsPitched.from_notation("5.2"), earned_runs=1)
        give_pitching(self.team, self.rival, player.id, line, day=1)
        give_pitching(self.team, self.rival, player.id, line, day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.pitching.innings.outs, 34)
        self.assertEqual(str(saved.pitching.innings), "11.1")
        self.assertEqual(saved.pitching.earned_runs, 2)

    def test_player_without_games_has_empty_stats(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 0)
        self.assertEqual(saved.pitching.innings.outs, 0)

    def test_duplicate_number_is_rejected_on_the_aggregate(self):
        team = self.repo.find_by_id(self.team.id)
        team.add_player("山田", JerseyNumber(10), Position.INFIELDER)
        self.repo.save(team)

        reloaded = self.repo.find_by_id(self.team.id)
        with self.assertRaises(DuplicateJerseyNumber):
            reloaded.add_player("田中", JerseyNumber(10), Position.OUTFIELDER)


class GameRepositoryTest(BaseCase):
    def test_round_trip(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        saved = play_game(
            self.team,
            self.rival,
            home_score=5,
            away_score=3,
            batting={player.id: BattingLine(at_bats=4, singles=2, runs_batted_in=1)},
        )

        reloaded = DjangoGameRepository().find_by_id(saved.id)

        self.assertEqual(reloaded.home_score, 5)
        self.assertEqual(reloaded.result_for(self.team.id), "win")
        self.assertEqual(len(reloaded.batting), 1)
        self.assertEqual(reloaded.batting[0].line.hits, 2)

    def test_same_team_is_rejected(self):
        with self.assertRaises(InvalidGame):
            play_game(self.team, self.team)

    def test_filter_by_season(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        self.assertEqual(len(DjangoGameRepository().find_all(2026)), 1)
        self.assertEqual(len(DjangoGameRepository().find_all()), 2)

    def test_recording_the_same_player_twice_overwrites(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        game = play_game(self.team, self.rival, batting={player.id: BattingLine(at_bats=4, singles=1)})
        game.record_batting(player.id, BattingLine(at_bats=4, home_runs=2))
        DjangoGameRepository().save(game)

        self.assertEqual(orm_models.GameBattingLine.objects.filter(game_id=game.id).count(), 1)
        reloaded = DjangoGameRepository().find_by_id(game.id)
        self.assertEqual(reloaded.batting[0].line.home_runs, 2)


class ApplicationServiceTest(BaseCase):
    def test_register_player(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")

        rows = self.service.list_batters(self.team.id).rows
        self.assertEqual([r.name for r in rows], ["山田"])

    def test_register_duplicate_number_is_rejected(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        with self.assertRaises(DuplicateJerseyNumber):
            self.service.register_player(self.team.id, "田中", 10, "外野手")

    def test_retire_player_frees_the_number(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.retire_player(self.team.id, player.id)

        # 退団後は同じ背番号を使える
        self.service.register_player(self.team.id, "田中", 10, "外野手")
        self.assertEqual(len(self.service.list_batters(self.team.id).rows), 1)

    def test_team_summary_counts_active_players(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.register_player(self.team.id, "佐藤", 18, "投手")

        summary = self.service.list_teams().rows[0]
        self.assertEqual(summary.league_name, "テストリーグ")
        self.assertEqual(summary.player_count, 2)


class PlayerListViewTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("player_list", args=[self.team.id])
        # 登録は書き込みなので担当者であることが要る（閲覧は誰でもできる）
        login_as_manager(self.client, self.team, username="editor")

    def test_register_via_form(self):
        self.client.post(self.url, {"name": "山田", "number": "10", "position": "内野手"})
        self.assertEqual(orm_models.PlayerStint.objects.filter(number=10).count(), 1)

    def test_duplicate_number_is_rejected_via_form(self):
        self.client.post(self.url, {"name": "山田", "number": "10", "position": "内野手"})
        self.client.post(self.url, {"name": "田中", "number": "10", "position": "外野手"})
        self.assertEqual(orm_models.PlayerStint.objects.filter(number=10).count(), 1)

    def test_non_numeric_number_is_rejected_without_crashing(self):
        response = self.client.post(self.url, {"name": "山田", "number": "あいう", "position": "内野手"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.Player.objects.count(), 0)

    def test_both_modes_render(self):
        self.assertEqual(self.client.get(f"{self.url}?pos=batter").status_code, 200)
        self.assertEqual(self.client.get(f"{self.url}?pos=pitcher").status_code, 200)

    def test_missing_team_returns_404(self):
        self.assertEqual(self.client.get(reverse("player_list", args=[9999])).status_code, 404)


class PlayerEditViewTest(BaseCase):
    def setUp(self):
        super().setUp()
        login_as_manager(self.client, self.team, username="editor")

    def _url(self, player_id):
        return reverse("player_edit", args=[self.team.id, player_id])

    def test_designated_hitter_keeps_position(self):
        """指名打者を編集しても投手に化けないこと（旧バグの再発防止）。"""
        player = self.service.register_player(self.team.id, "大谷", 17, "指名打者")

        response = self.client.get(self._url(player.id))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="指名打者" selected>', html=False)

    def test_update_basic_information(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.client.post(
            self._url(player.id),
            {
                "name": "山田太郎",
                "number": "11",
                "position": "外野手",
            },
        )

        detail = self.service.get_player_detail(self.team.id, player.id)
        self.assertEqual(detail.name, "山田太郎")
        self.assertEqual(detail.number, 11)
        self.assertEqual(detail.position, "外野手")

    def test_stats_are_shown_but_not_editable(self):
        """成績は試合の集計結果なので、この画面からは変更できない。"""
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=10, singles=3))

        # 打数を送っても無視される
        self.client.post(
            self._url(player.id),
            {
                "name": "山田",
                "number": "10",
                "position": "内野手",
                "at_bats": "999",
            },
        )

        self.assertEqual(self.service.get_player_detail(self.team.id, player.id).at_bats, 10)

    def test_totals_reflect_games(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=2), day=1)
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=6, home_runs=1), day=2)

        detail = self.service.get_player_detail(self.team.id, player.id)

        self.assertEqual(detail.at_bats, 10)
        self.assertAlmostEqual(detail.batting_average, 0.3)

    def test_retire_from_the_screen(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.client.post(self._url(player.id), {"retire": "1"})

        # 退団は在籍期間を閉じることで表す
        stint = orm_models.PlayerStint.objects.get(player_id=player.id)
        self.assertIsNotNone(stint.to_year)

    def test_duplicate_number_on_update_is_rejected(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        tanaka = self.service.register_player(self.team.id, "田中", 11, "外野手")

        self.client.post(
            self._url(tanaka.id),
            {
                "name": "田中",
                "number": "10",
                "position": "外野手",
            },
        )

        self.assertEqual(self.service.get_player_detail(self.team.id, tanaka.id).number, 11)

    def test_missing_player_returns_404(self):
        self.assertEqual(self.client.get(self._url(9999)).status_code, 404)


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

    def test_standings_pane_is_shown_first(self):
        """右カードの初期表示は順位表。チーム一覧はタブで切り替える。"""
        play_game(self.team, self.rival)
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn(f'class="tab-pane fade show active" id="league-{self.league.id}-standings"', body)
        self.assertIn(f'class="tab-pane fade" id="league-{self.league.id}-teams"', body)

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


class StandingsTest(BaseCase):
    def test_record_is_aggregated_from_games(self):
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        play_game(self.team, self.rival, home_score=2, away_score=2, day=2)
        play_game(self.team, self.rival, home_score=1, away_score=4, day=3)

        rows = {r.team_name: r for r in self.service.get_standings(2026).rows}

        self.assertEqual(rows["テストチーム"].wins, 1)
        self.assertEqual(rows["テストチーム"].losses, 1)
        self.assertEqual(rows["テストチーム"].ties, 1)
        self.assertEqual(rows["テストチーム"].games_played, 3)

    def test_rank_is_derived_from_winning_percentage(self):
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        play_game(self.team, self.rival, home_score=6, away_score=2, day=2)
        play_game(self.team, self.rival, home_score=1, away_score=4, day=3)

        rows = self.service.get_standings(2026).rows

        self.assertEqual([r.team_name for r in rows], ["テストチーム", "相手チーム"])
        self.assertEqual([r.rank for r in rows], [1, 2])
        self.assertEqual(rows[0].games_behind, "—")

    def test_teams_without_games_are_excluded(self):
        other = orm_models.Team.objects.create(league=self.league, name="未実施チーム")
        play_game(self.team, self.rival)

        names = [r.team_name for r in self.service.get_standings(2026).rows]

        self.assertNotIn(other.name, names)

    def test_defaults_to_the_latest_season(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        board = self.service.get_standings()

        self.assertEqual(board.year, 2026)
        self.assertEqual(board.available_years, [2026, 2025])

    def test_page_renders(self):
        play_game(self.team, self.rival, home_score=5, away_score=3)
        response = self.client.get(reverse("standings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テストチーム")

    def test_page_by_year(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        response = self.client.get(reverse("standings_by_year", args=[2025]))
        self.assertContains(response, "2025年")

    def test_page_without_any_game(self):
        response = self.client.get(reverse("standings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "試合がまだ登録されていません")


class SortingViewTest(BaseCase):
    def setUp(self):
        super().setUp()
        a = self.service.register_player(self.team.id, "少打", 1, "内野手")
        b = self.service.register_player(self.team.id, "多打", 2, "外野手")
        play_game(
            self.team,
            self.rival,
            batting={
                a.id: BattingLine(at_bats=20, singles=4, home_runs=1),
                b.id: BattingLine(at_bats=20, singles=2, home_runs=5),
            },
        )
        self.url = reverse("player_list", args=[self.team.id])

    def _names(self, query=""):
        listing = self.client.get(f"{self.url}{query}").context["listing"]
        return [r.name for r in listing.rows]

    def test_default_order_is_ops(self):
        self.assertEqual(self._names(), ["多打", "少打"])

    def test_sort_by_home_runs_ascending(self):
        self.assertEqual(self._names("?sort=home_runs&dir=asc"), ["少打", "多打"])

    def test_sort_by_home_runs_descending(self):
        self.assertEqual(self._names("?sort=home_runs&dir=desc"), ["多打", "少打"])

    def test_invalid_sort_key_does_not_break_the_page(self):
        response = self.client.get(f"{self.url}?sort=../../etc/passwd")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["listing"].sort, "ops")

    def test_sort_link_keeps_other_query_params(self):
        body = self.client.get(f"{self.url}?pos=pitcher").content.decode()
        self.assertIn("pos=pitcher", body)
        self.assertIn("sort=era", body)

    def test_header_shows_the_active_direction(self):
        body = self.client.get(f"{self.url}?sort=home_runs&dir=desc").content.decode()
        self.assertIn("sort-link is-active", body)

    def test_team_list_can_be_sorted(self):
        response = self.client.get(f"{reverse('team_list')}?sort=name&dir=asc")
        names = [t.name for t in response.context["teams"]]
        self.assertEqual(names, sorted(names))

    def test_team_list_defaults_to_manual_order(self):
        """名前順ではなく、管理画面で設定した表示順が既定になること。"""
        orm_models.Team.objects.update(display_order=5)
        orm_models.Team.objects.create(league=self.league, name="Zチーム", display_order=1)
        response = self.client.get(reverse("team_list"))

        # 名前順なら最後に来るはずの Z が、表示順1なので先頭に出る
        self.assertEqual(response.context["teams"][0].name, "Zチーム")
        self.assertEqual(response.context["current_sort"], "order")

    def test_standings_can_be_sorted(self):
        response = self.client.get(f"{reverse('standings')}?sort=wins&dir=desc")
        self.assertEqual(response.context["standings"].sort, "wins")


class TeamOrderingTest(BaseCase):
    def setUp(self):
        super().setUp()
        orm_models.Team.objects.filter(id=self.team.id).update(display_order=2, name="Aチーム")
        orm_models.Team.objects.filter(id=self.rival.id).update(display_order=1, name="Bチーム")

    def test_display_order_beats_name(self):
        names = [t.name for t in self.service.list_teams().rows]
        self.assertEqual(names, ["Bチーム", "Aチーム"])

    def test_dashboard_uses_the_same_order(self):
        names = [t.name for t in self.service.get_dashboard().teams]
        self.assertEqual(names, ["Bチーム", "Aチーム"])

    def test_same_order_falls_back_to_name(self):
        orm_models.Team.objects.update(display_order=0)
        names = [t.name for t in self.service.list_teams().rows]
        self.assertEqual(names, ["Aチーム", "Bチーム"])

    def _league_page(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        return self.client.get(f"/admin/myapp/league/{self.league.id}/change/")

    def test_admin_league_page_loads_the_sortable_script(self):
        self.assertContains(self._league_page(), "admin-inline-sortable.js")

    def test_team_changelist_can_be_reordered(self):
        """リーグ編集画面だけでなく、チーム一覧からも並べ替えられること。"""
        self.client.force_login(User.objects.create_superuser(username="t", password="x"))
        response = self.client.get("/admin/myapp/team/")

        self.assertContains(response, "admin-inline-sortable.js")
        self.assertContains(response, 'name="form-0-display_order"')

    def test_changelist_explains_how_to_reorder(self):
        """表示順の列は隠しているので、操作方法を画面で伝える。"""
        self.client.force_login(User.objects.create_superuser(username="t3", password="x"))

        for url in ("/admin/myapp/team/", "/admin/myapp/league/"):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "sortable-hint")

    def test_no_link_on_the_page_carries_a_sort(self):
        """絞り込みのリンクなどに並べ替えが紛れ込まないこと。

        1つでも残っていると、そこから並べ替えられない状態に入ってしまう。
        """
        self.client.force_login(User.objects.create_superuser(username="t6", password="x"))
        url = f"/admin/myapp/team/?league__id__exact={self.league.id}&o=1"

        body = self.client.get(url, follow=True).content.decode()

        self.assertNotIn("?o=", body)
        self.assertIn("admin-inline-sortable.js", body)

    def test_name_cell_is_rendered_as_a_header_cell(self):
        """一覧のリンク列は th で描かれる。

        つまみと折り返しの CSS が td だけを指していると効かないため、
        この前提が変わっていないことを確かめる。
        """
        self.client.force_login(User.objects.create_superuser(username="t5", password="x"))
        body = self.client.get("/admin/myapp/team/").content.decode()

        self.assertIn('<th class="field-name">', body)

    def test_order_input_is_submitted_but_the_column_is_hidden(self):
        """数値は送信するが列としては見せない（インラインと同じ扱い）。"""
        self.client.force_login(User.objects.create_superuser(username="t4", password="x"))
        body = self.client.get("/admin/myapp/team/").content.decode()

        self.assertIn('name="form-0-display_order"', body)
        # 列を隠す指定が読み込まれていること
        self.assertIn("myapp/css/admin-theme.css", body)
        self.assertIn("column-display_order", body)

    def test_team_changelist_is_ordered_by_league_then_order(self):
        self.client.force_login(User.objects.create_superuser(username="t2", password="x"))
        body = self.client.get("/admin/myapp/team/").content.decode()

        # リーグごとに区切られ、リーグ内は表示順に並ぶ
        self.assertIn("group-heading-row", body)
        self.assertLess(body.index("Bチーム"), body.index("Aチーム"))

    def test_order_field_is_submitted_but_not_shown(self):
        body = self._league_page().content.decode()

        self.assertIn('type="hidden" name="teams-0-display_order"', body)
        self.assertIn('class="column-display_order required hidden"', body)
        # インラインの表示順が数値欄として出ていないこと。
        # （リーグ自身の表示順は別の欄なので、ページ全体では数値欄が存在する）
        self.assertNotIn('type="number" name="teams-0-display_order"', body)


class HeaderNavigationTest(TestCase):
    """ヘッダーの導線が権限に応じて出し分けられること。"""

    ADMIN_LINK = 'class="nav-admin-link"'

    def setUp(self):
        self.staff = User.objects.create_user(username="staff", password="x", is_staff=True)
        self.member = User.objects.create_user(username="member", password="x")

    def test_staff_sees_admin_link(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, self.ADMIN_LINK)

    def test_normal_user_does_not_see_admin_link(self):
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse("dashboard")), self.ADMIN_LINK)

    def test_anonymous_does_not_see_admin_link(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, self.ADMIN_LINK)
        self.assertContains(response, "ログイン")

    def test_admin_page_actually_rejects_normal_user(self):
        self.client.force_login(self.member)
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_admin_page_accepts_staff(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/admin/").status_code, 200)


class AdminTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def test_admin_pages_use_the_admin_theme(self):
        for url in ["/admin/", "/admin/myapp/player/", "/admin/myapp/game/"]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, "myapp/css/admin-theme.css")
                self.assertNotContains(response, "myapp/css/theme.css")

    def test_site_pages_do_not_use_the_admin_theme(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "myapp/css/theme.css")
        self.assertNotContains(response, "myapp/css/admin-theme.css")

    def test_game_list_shows_the_result(self):
        play_game(self.team, self.rival, home_score=5, away_score=3)
        response = self.client.get("/admin/myapp/game/")
        self.assertContains(response, "テストチーム の勝ち")

    def test_game_edit_has_stat_inlines(self):
        game = play_game(self.team, self.rival)
        response = self.client.get(f"/admin/myapp/game/{game.id}/change/")
        self.assertContains(response, "打撃成績")
        self.assertContains(response, "投球成績")

    def test_player_list_shows_appearances(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=1))

        response = self.client.get("/admin/myapp/player/")
        self.assertContains(response, "field-appearances")


class AdminIndexTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def test_models_are_labelled_in_japanese(self):
        response = self.client.get("/admin/")
        for label in ["野球データ", "リーグ", "チーム", "選手", "試合"]:
            with self.subTest(label=label):
                self.assertContains(response, label)

    def test_models_follow_domain_order(self):
        body = self.client.get("/admin/").content.decode()
        positions = [
            body.index("/admin/myapp/league/"),
            body.index("/admin/myapp/team/"),
            body.index("/admin/myapp/player/"),
            body.index("/admin/myapp/game/"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_baseball_data_comes_before_auth(self):
        body = self.client.get("/admin/").content.decode()
        self.assertLess(body.index("/admin/myapp/"), body.index("/admin/auth/"))

    def test_overview_counts(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.register_player(self.team.id, "佐藤", 18, "投手")

        overview = self.service.get_admin_overview()

        self.assertEqual(overview.team_count, 2)
        self.assertEqual(overview.player_count, 2)
        self.assertEqual(overview.pitcher_count, 1)

    def test_overview_flags_players_without_stats(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.assertEqual(self.service.get_admin_overview().players_without_stats, 1)

        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=10, singles=3))
        self.assertEqual(self.service.get_admin_overview().players_without_stats, 0)

    def test_overview_flags_empty_teams_and_retired_players(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.retire_player(self.team.id, player.id)

        overview = self.service.get_admin_overview()

        self.assertEqual(overview.teams_without_players, 2)
        self.assertEqual(overview.retired_count, 1)

    def test_notes_appear_on_the_page(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.assertContains(self.client.get("/admin/"), "成績が未入力の選手")


class GameViewTest(BaseCase):
    """試合一覧・試合詳細（フェーズ1）。"""

    def setUp(self):
        super().setUp()
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self.game = play_game(
            self.team,
            self.rival,
            home_score=5,
            away_score=3,
            day=1,
            batting={self.batter.id: BattingLine(at_bats=4, singles=2, runs_batted_in=1)},
            pitching={
                self.pitcher.id: PitchingLine(innings=InningsPitched.from_notation("7.0"), earned_runs=2, strikeouts=8)
            },
        )

    def test_list_renders(self):
        response = self.client.get(reverse("game_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テストチーム")
        self.assertContains(response, "テストチーム の勝ち")

    def test_list_is_newest_first(self):
        play_game(self.team, self.rival, day=5)
        rows = self.client.get(reverse("game_list")).context["games"]

        self.assertEqual(rows[0].played_on.day, 5)

    def test_list_can_be_filtered_by_team(self):
        other = orm_models.Team.objects.create(league=self.league, name="第三チーム")
        play_game(other, self.rival, day=9)

        rows = self.client.get(f"{reverse('game_list')}?team={other.id}").context["games"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].home_team_name, "第三チーム")

    def test_list_can_be_filtered_by_year(self):
        play_game(self.team, self.rival, year=2025, day=1)
        rows = self.client.get(f"{reverse('game_list')}?year=2025").context["games"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].year, 2025)

    def test_invalid_filter_is_ignored(self):
        response = self.client.get(f"{reverse('game_list')}?year=abc&team=xyz")
        self.assertEqual(response.status_code, 200)

    def test_list_can_be_filtered_by_month(self):
        play_game(self.team, self.rival, month=7, day=20)

        rows = self.client.get(f"{reverse('game_list')}?year=2026&month=7").context["games"]

        self.assertEqual([r.played_on.month for r in rows], [7])

    def test_month_choices_come_from_the_games(self):
        play_game(self.team, self.rival, month=7, day=20)

        response = self.client.get(reverse("game_list"))

        self.assertEqual(response.context["months"], [4, 7])

    def test_default_is_the_latest_month_of_the_latest_season(self):
        """全件を一度に描くと重いため、開いた直後は直近の月だけを見せる。"""
        play_game(self.team, self.rival, year=2025, month=9, day=1)
        play_game(self.team, self.rival, month=7, day=20)

        response = self.client.get(reverse("game_list"))

        self.assertEqual(response.context["selected_year"], 2026)
        self.assertEqual(response.context["selected_month"], 7)
        self.assertEqual([r.played_on.month for r in response.context["games"]], [7])

    def test_month_without_games_falls_back_instead_of_erroring(self):
        """年やチームを変えると選んでいた月に試合が無いことがある。エラーにしない。"""
        response = self.client.get(f"{reverse('game_list')}?year=2026&month=12")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_month"], 4)

    def test_list_can_be_filtered_by_league(self):
        other_league = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other_league, name="Xチーム")
        y = orm_models.Team.objects.create(league=other_league, name="Yチーム")
        play_game(x, y, day=3)

        rows = self.client.get(f"{reverse('game_list')}?league={other_league.id}").context["games"]

        self.assertEqual([r.home_team_name for r in rows], ["Xチーム"])

    def test_interleague_games_appear_in_both_leagues(self):
        """リーグをまたぐ対戦は、どちらのリーグの日程にも現れる。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other_league, name="Xチーム")
        play_game(self.team, x, day=6)

        for league in (self.league, other_league):
            with self.subTest(league=league.name):
                rows = self.client.get(f"{reverse('game_list')}?league={league.id}").context["games"]
                self.assertIn(6, [r.played_on.day for r in rows])

    def test_dashboard_game_link_carries_the_league(self):
        """ダッシュボードのリーグタブから移ると、そのリーグの日程が開く。"""
        play_game(self.team, self.rival, day=2)

        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn(f"{reverse('game_list')}?league={self.league.id}", body)

    def test_league_tabs_are_shown_with_an_all_option(self):
        """リーグはダッシュボードと同じくタブで切り替える。"""
        other_league = orm_models.League.objects.create(name="別リーグ")

        body = self.client.get(reverse("game_list")).content.decode()

        for league in (self.league, other_league):
            with self.subTest(league=league.name):
                self.assertIn(f"league={league.id}", body)
        self.assertIn("すべて", body)

    def test_league_tab_keeps_the_selected_month(self):
        """リーグを切り替えても、見ている月は保つ。"""
        play_game(self.team, self.rival, month=7, day=20)

        body = self.client.get(f"{reverse('game_list')}?year=2026&month=7").content.decode()

        self.assertIn(f"league={self.league.id}&amp;month=7", body)

    def test_team_choices_follow_the_selected_league(self):
        """チームの選択肢は、選んでいるリーグの所属チームだけにする。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        orm_models.Team.objects.create(league=other_league, name="Xチーム")

        response = self.client.get(f"{reverse('game_list')}?league={self.league.id}")

        self.assertEqual(
            {t.name for t in response.context["teams"]},
            {"テストチーム", "相手チーム"},
        )

    def test_team_outside_the_league_is_dropped(self):
        """リーグを切り替えると、選んでいたチームがそのリーグにいないことがある。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other_league, name="Xチーム")

        response = self.client.get(f"{reverse('game_list')}?league={self.league.id}&team={x.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["selected_team"])

    def test_create_link_ignores_the_league_filter(self):
        """登録の導線は、リーグを絞っていても担当チームがあれば見せる。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        managed = orm_models.Team.objects.create(league=other_league, name="担当チーム")
        login_as_manager(self.client, managed)

        response = self.client.get(f"{reverse('game_list')}?league={self.league.id}")

        self.assertTrue(response.context["can_create_game"])

    def test_month_choices_follow_the_selected_league(self):
        """リーグを絞ると、そのリーグで試合がある月だけが選択肢になる。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other_league, name="Xチーム")
        y = orm_models.Team.objects.create(league=other_league, name="Yチーム")
        play_game(x, y, month=8, day=1)

        response = self.client.get(f"{reverse('game_list')}?league={other_league.id}")

        self.assertEqual(response.context["months"], [8])
        self.assertEqual(response.context["selected_month"], 8)

    def test_result_label_is_the_same_from_both_paths(self):
        """結果の文言は GameRow が持つ。参照クエリと集約の経路で食い違わないこと。"""
        from_query = self.client.get(reverse("game_list")).context["games"][0]
        from_aggregate = self.service.get_game_detail(self.game.id).game

        self.assertEqual(from_query.result, "テストチーム の勝ち")
        self.assertEqual(from_query.result, from_aggregate.result)

    def test_tie_is_labelled_the_same_from_both_paths(self):
        tie = play_game(self.team, self.rival, home_score=3, away_score=3, day=8)

        from_query = next(r for r in self.service.list_games().rows if r.id == tie.id)

        self.assertEqual(from_query.result, "引分")
        self.assertEqual(from_query.result, self.service.get_game_detail(tie.id).game.result)

    def test_list_does_not_read_game_details(self):
        """一覧は日付・チーム・スコアしか使わない。明細まで読むと件数ぶん重くなる。

        試合を増やしてもクエリ数が変わらないことで、明細を読んでいないと分かる。
        """
        with CaptureQueriesContext(connection) as first:
            self.client.get(reverse("game_list"))

        for day in range(2, 12):
            play_game(self.team, self.rival, day=day)

        with CaptureQueriesContext(connection) as grown:
            response = self.client.get(reverse("game_list"))

        self.assertEqual(len(response.context["games"]), 11)
        self.assertEqual(len(grown.captured_queries), len(first.captured_queries))

    def test_detail_shows_both_stat_kinds(self):
        response = self.client.get(reverse("game_detail", args=[self.game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "山田")
        self.assertContains(response, "佐藤")
        self.assertContains(response, "7.0")

    def test_detail_computes_rates_from_the_domain(self):
        detail = self.service.get_game_detail(self.game.id)

        self.assertAlmostEqual(detail.batting[0].batting_average, 0.5)
        # 7回で自責点2 → 2*27/21
        self.assertAlmostEqual(detail.pitching[0].earned_run_average, 2 * 27 / 21)

    def test_missing_game_returns_404(self):
        self.assertEqual(self.client.get(reverse("game_detail", args=[9999])).status_code, 404)


class PlayerDetailViewTest(BaseCase):
    """選手個人ページ（フェーズ1）。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        play_game(
            self.team,
            self.rival,
            home_score=5,
            away_score=3,
            day=1,
            batting={self.player.id: BattingLine(at_bats=4, singles=2)},
        )
        play_game(
            self.team,
            self.rival,
            home_score=1,
            away_score=4,
            day=2,
            batting={self.player.id: BattingLine(at_bats=6, home_runs=1)},
        )
        self.url = reverse("player_detail", args=[self.team.id, self.player.id])

    def test_page_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "山田")

    def test_shows_career_totals(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.detail.at_bats, 10)
        self.assertAlmostEqual(profile.detail.batting_average, 0.3)

    def test_lists_each_game_newest_first(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.appearances, 2)
        self.assertEqual([r.played_on.day for r in profile.games], [2, 1])

    def test_batter_rows_have_no_decision(self):
        """個人ページはその選手の働きを見る場所。野手にチームの勝敗は出さない。"""
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual([r.decision for r in profile.games], ["", ""])

    def test_pitcher_rows_show_the_pitchers_own_decision(self):
        """投手には本人に付いた記録（勝・敗・Ｓ・Ｈ）を、ボックススコアと同じ印で出す。"""
        ace = self.service.register_player(self.team.id, "エース", 18, "投手")
        give_pitching(
            self.team,
            self.rival,
            ace.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), wins=1),
            day=3,
        )
        closer = self.service.register_player(self.team.id, "守護神", 22, "投手")
        give_pitching(
            self.team,
            self.rival,
            closer.id,
            PitchingLine(innings=InningsPitched.from_notation("1.0"), saves=1),
            day=4,
        )

        ace_games = self.service.get_player_profile(self.team.id, ace.id).games
        closer_games = self.service.get_player_profile(self.team.id, closer.id).games

        self.assertEqual(ace_games[0].decision, "勝")
        self.assertEqual(closer_games[0].decision, "Ｓ")

    def test_opponent_is_shown_from_the_player_side(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)
        self.assertEqual(profile.games[0].opponent_name, "相手チーム")

    def test_games_without_the_player_are_excluded(self):
        play_game(self.team, self.rival, day=3)
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.appearances, 2)

    def test_player_without_games(self):
        other = self.service.register_player(self.team.id, "控え", 99, "内野手")
        response = self.client.get(reverse("player_detail", args=[self.team.id, other.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "出場した試合がまだありません")

    def test_player_list_links_to_the_profile(self):
        body = self.client.get(reverse("player_list", args=[self.team.id])).content.decode()
        self.assertIn(self.url, body)

    def test_missing_player_returns_404(self):
        self.assertEqual(self.client.get(reverse("player_detail", args=[self.team.id, 9999])).status_code, 404)


class TeamListByLeagueTest(BaseCase):
    """チーム一覧もリーグごとに分ける。"""

    def setUp(self):
        super().setUp()
        self.other = orm_models.League.objects.create(name="別リーグ")
        self.x = orm_models.Team.objects.create(league=self.other, name="Xチーム")

    def test_grouped_by_league(self):
        listing = self.service.list_teams_by_league()

        grouped = {g.league_name: [t.name for t in g.teams] for g in listing.rows}
        self.assertEqual(grouped, {"テストリーグ": ["テストチーム", "相手チーム"], "別リーグ": ["Xチーム"]})

    def test_leagues_without_teams_are_omitted(self):
        orm_models.League.objects.create(name="空リーグ")
        names = [g.league_name for g in self.service.list_teams_by_league().rows]

        self.assertNotIn("空リーグ", names)

    def test_sorting_applies_within_each_league(self):
        orm_models.Team.objects.create(league=self.other, name="Aチーム")

        listing = self.service.list_teams_by_league(sort="name", descending=False)
        grouped = {g.league_name: [t.name for t in g.teams] for g in listing.rows}

        self.assertEqual(grouped["別リーグ"], ["Aチーム", "Xチーム"])

    def test_page_shows_each_league_heading(self):
        response = self.client.get(reverse("team_list"))

        self.assertContains(response, "テストリーグ")
        self.assertContains(response, "別リーグ")
        self.assertEqual(len(response.context["leagues"]), 2)

    def test_heading_shows_the_team_count(self):
        body = self.client.get(reverse("team_list")).content.decode()

        # テストリーグは2チーム、別リーグは1チーム
        self.assertIn(">2チーム</span>", body)
        self.assertIn(">1チーム</span>", body)

    def test_admin_heading_shows_the_team_count(self):
        self.client.force_login(User.objects.create_superuser(username="c", password="x"))
        body = self.client.get("/admin/myapp/team/").content.decode()

        self.assertIn("テストリーグ（2チーム）", body)
        self.assertIn("別リーグ（1チーム）", body)

    def test_admin_count_does_not_query_per_row(self):
        """区切りごとに数えると行の数だけ問い合わせが増えるため、まとめて取る。"""
        self.client.force_login(User.objects.create_superuser(username="c2", password="x"))

        def count_queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get("/admin/myapp/team/")
            return len(captured)

        before = count_queries()
        for i in range(5):
            orm_models.Team.objects.create(league=self.other, name=f"T{i}")

        self.assertEqual(count_queries(), before)

    def test_flat_list_is_still_available_for_filters(self):
        """試合一覧の絞り込みなどは平坦な一覧を使う。"""
        rows = self.service.list_teams().rows
        self.assertEqual(len(rows), 3)


class LeagueScopedStandingsTest(BaseCase):
    """順位はリーグの中で争われる（フェーズ2）。"""

    def setUp(self):
        super().setUp()
        self.other_league = orm_models.League.objects.create(name="別リーグ")
        self.x = orm_models.Team.objects.create(league=self.other_league, name="Xチーム")
        self.y = orm_models.Team.objects.create(league=self.other_league, name="Yチーム")

        # テストリーグ側は僅差、別リーグ側は圧勝
        play_game(self.team, self.rival, home_score=2, away_score=1, day=1)
        play_game(self.x, self.y, home_score=10, away_score=0, day=1)

    def test_standings_are_split_by_league(self):
        board = self.service.get_standings(2026)

        names = {lg.league_name: [r.team_name for r in lg.rows] for lg in board.leagues}
        self.assertEqual(len(board.leagues), 2)
        self.assertEqual(names["テストリーグ"], ["テストチーム", "相手チーム"])
        self.assertEqual(names["別リーグ"], ["Xチーム", "Yチーム"])

    def test_other_league_teams_do_not_share_the_rank(self):
        """別リーグの1位どうしが同じ表で2位に落ちたりしないこと。"""
        board = self.service.get_standings(2026)

        leaders = [lg.rows[0] for lg in board.leagues]
        self.assertTrue(all(row.rank == 1 for row in leaders))

    def test_leagues_without_games_are_omitted(self):
        orm_models.League.objects.create(name="未実施リーグ")
        board = self.service.get_standings(2026)

        self.assertNotIn("未実施リーグ", [lg.league_name for lg in board.leagues])

    def test_page_shows_each_league_heading(self):
        response = self.client.get(reverse("standings"))

        self.assertContains(response, "テストリーグ")
        self.assertContains(response, "別リーグ")


class LeagueDetailTest(BaseCase):
    """リーグ画面（フェーズ2）。"""

    def setUp(self):
        super().setUp()
        self.outsider_league = orm_models.League.objects.create(name="別リーグ")
        self.outsider = orm_models.Team.objects.create(league=self.outsider_league, name="部外チーム")
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        self.url = reverse("league_detail", args=[self.league.id])

    def test_page_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テストリーグ")

    def test_shows_only_member_teams(self):
        detail = self.service.get_league_detail(self.league.id)

        names = [t.name for t in detail.teams]
        self.assertIn("テストチーム", names)
        self.assertNotIn("部外チーム", names)

    def test_shows_standings_and_recent_games(self):
        detail = self.service.get_league_detail(self.league.id)

        self.assertEqual(detail.standings[0].team_name, "テストチーム")
        self.assertEqual(len(detail.recent_games), 1)

    def test_games_of_other_leagues_are_excluded(self):
        another = orm_models.Team.objects.create(league=self.outsider_league, name="部外チーム2")
        play_game(self.outsider, another, day=2)

        detail = self.service.get_league_detail(self.league.id)
        self.assertEqual(len(detail.recent_games), 1)

    def test_season_can_be_selected(self):
        play_game(self.team, self.rival, year=2025, day=1)

        detail = self.service.get_league_detail(self.league.id, 2025)

        self.assertEqual(detail.year, 2025)
        self.assertEqual(detail.available_years, [2026, 2025])

    def test_league_without_games(self):
        detail = self.service.get_league_detail(self.outsider_league.id)

        self.assertEqual(detail.standings, [])
        self.assertIsNone(detail.year)

    def test_missing_league_returns_404(self):
        self.assertEqual(self.client.get(reverse("league_detail", args=[9999])).status_code, 404)

    def test_team_list_links_to_the_league(self):
        body = self.client.get(reverse("team_list")).content.decode()
        self.assertIn(self.url, body)


class AdminGroupingTest(BaseCase):
    """管理画面の一覧をリーグ・チームごとに区切る。

    標準テンプレートを差し替えているため、グループ化しない一覧が
    従来どおり出ることも確かめる。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.other = orm_models.League.objects.create(name="別リーグ")
        self.x = orm_models.Team.objects.create(league=self.other, name="Xチーム")
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.register_player(self.x.id, "田中", 20, "外野手")

    def test_team_list_has_league_headings(self):
        response = self.client.get("/admin/myapp/team/")

        self.assertContains(response, "group-heading-row")
        self.assertContains(response, "テストリーグ")
        self.assertContains(response, "別リーグ")

    def test_team_list_shows_each_league_once(self):
        body = self.client.get("/admin/myapp/team/").content.decode()
        # 見出しには所属チーム数も添える
        # 見出しには絞り込みリンクも付くため、見出し文言そのもので数える
        self.assertEqual(body.count("テストリーグ（2チーム）"), 1)

    def test_stint_list_groups_by_team(self):
        """所属はもう選手ではなく在籍が持つので、区切るのは在籍一覧。"""
        response = self.client.get("/admin/myapp/playerstint/")

        self.assertContains(response, "group-heading-row")
        self.assertContains(response, "テストリーグ · テストチーム")

    def _manually_ordered_teams(self):
        """手動の並びが名前順とは異なるチームを作る。"""
        for order, name in enumerate(("Cチーム", "Aチーム", "Bチーム"), start=1):
            orm_models.Team.objects.create(league=self.other, name=name, display_order=order)
        return ("Cチーム", "Aチーム", "Bチーム")

    def test_columns_cannot_be_sorted(self):
        """列での並べ替えは持たない。

        ドラッグした順がそのまま保存される順なので、列で並べ替えると
        見えている順と食い違う。両立しないため並べ替え自体を置かない。
        """
        for url in ("/admin/myapp/team/", "/admin/myapp/league/"):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()

                self.assertIn('id="result_list"', body)
                self.assertNotIn("?o=", body)
                self.assertNotIn("sortoptions", body)

    def test_sorting_written_in_the_url_is_dropped(self):
        """古いリンクや履歴から来ても、並べ替えられない状態に入らない。"""
        manual_order = self._manually_ordered_teams()

        response = self.client.get("/admin/myapp/team/?o=1")

        self.assertRedirects(response, "/admin/myapp/team/")
        body = self.client.get("/admin/myapp/team/?o=1", follow=True).content.decode()
        # 名前順ではなく手動の順のまま
        positions = [body.index(name) for name in manual_order]
        self.assertEqual(positions, sorted(positions))
        # 区切りも崩れず、ドラッグもできる
        self.assertEqual(body.count("テストリーグ（2チーム）"), 1)
        self.assertIn("admin-inline-sortable.js", body)

    def test_dropping_the_sort_keeps_the_other_parameters(self):
        """並べ替えだけを落とし、絞り込みは保つ。"""
        response = self.client.get(f"/admin/myapp/team/?league__id__exact={self.other.id}&o=1")

        self.assertRedirects(response, f"/admin/myapp/team/?league__id__exact={self.other.id}")

    def test_league_filter_is_still_available(self):
        """リーグを1つに絞る手段は絞り込みパネルが担う。"""
        self._manually_ordered_teams()

        body = self.client.get(f"/admin/myapp/team/?league__id__exact={self.other.id}").content.decode()

        self.assertNotIn("テストチーム", body)
        self.assertIn("Aチーム", body)

    def test_other_changelists_are_unaffected(self):
        """group_by を持たない一覧は従来どおり描画されること。"""
        for url in ["/admin/myapp/league/", "/admin/myapp/game/", "/admin/auth/user/"]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "group-heading-row")

    def test_result_rows_are_still_rendered(self):
        body = self.client.get("/admin/myapp/team/").content.decode()

        self.assertIn("result_list", body)
        self.assertIn("テストチーム", body)
        self.assertIn("Xチーム", body)


class WriteRequiresLoginTest(BaseCase):
    """閲覧は誰でも、書き込みはログインした人だけ。

    以前は試合だけがログイン必須で、選手の登録・編集・退団は
    未ログインのまま実行できていた。画面ごとに要否が違うと、
    どこが公開範囲なのか読み取れなくなる。
    """

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")

    def test_reading_needs_no_login(self):
        pages = [
            reverse("dashboard"),
            reverse("team_list"),
            reverse("player_list", args=[self.team.id]),
            reverse("player_detail", args=[self.team.id, self.player.id]),
            reverse("game_list"),
            reverse("standings"),
        ]
        for url in pages:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_writing_pages_redirect_to_login(self):
        for url in (
            reverse("game_create"),
            reverse("player_edit", args=[self.team.id, self.player.id]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_registering_a_player_without_login_changes_nothing(self):
        before = orm_models.Player.objects.count()

        response = self.client.post(
            reverse("player_list", args=[self.team.id]),
            {"name": "侵入", "number": "99", "position": "内野手"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(orm_models.Player.objects.count(), before)

    def test_editing_a_player_without_login_changes_nothing(self):
        response = self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"name": "改ざん", "number": "10", "position": "内野手"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(orm_models.Player.objects.get(pk=self.player.id).name, "山田")

    def test_retiring_a_player_without_login_changes_nothing(self):
        self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"retire": "1"},
        )

        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertIsNone(stint.to_year)

    def test_write_controls_are_hidden_from_anonymous_visitors(self):
        """押せない導線は見せない（試合の画面と同じ扱いに揃える）。"""
        listing = self.client.get(reverse("player_list", args=[self.team.id]))
        detail = self.client.get(reverse("player_detail", args=[self.team.id, self.player.id]))

        self.assertNotContains(listing, "新入団選手の登録")
        self.assertNotContains(detail, "player_edit")
        self.assertNotContains(detail, reverse("player_edit", args=[self.team.id, self.player.id]))


class GameEntryTest(BaseCase):
    """サイトからの試合登録と成績の一括入力（フェーズ3）。"""

    def setUp(self):
        super().setUp()
        self.user = login_as_manager(self.client, self.team, username="scorer")
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")

    def _create_game(self):
        return self.client.post(
            reverse("game_create"),
            {
                "year": "2026",
                "played_on": "2026-04-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "home_score": "5",
                "away_score": "3",
            },
        )

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse("game_create"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_create_game_then_go_to_stats(self):
        response = self._create_game()

        game = orm_models.Game.objects.get()
        self.assertEqual(game.home_score, 5)
        self.assertRedirects(response, reverse("game_edit", args=[game.id]))

    def test_same_team_is_rejected_without_crashing(self):
        response = self.client.post(
            reverse("game_create"),
            {
                "year": "2026",
                "played_on": "2026-04-01",
                "home_team": self.team.id,
                "away_team": self.team.id,
                "home_score": "0",
                "away_score": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.Game.objects.count(), 0)

    def test_edit_page_lists_the_roster(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        response = self.client.get(reverse("game_edit", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.context["payload"]
        batters = [p for roster in payload["rosters"] for p in roster["batters"]]
        pitchers = [p for roster in payload["rosters"] for p in roster["pitchers"]]
        names = {p["name"] for p in batters + pitchers}
        self.assertIn("山田", names)
        self.assertIn("佐藤", names)
        # 野手は打撃表、投手は投球表に振り分けられる
        self.assertEqual(len(batters), 1)
        self.assertEqual(len(pitchers), 1)

    def _stats_payload(self, game, **overrides):
        payload = {
            "year": 2026,
            "played_on": "2026-04-01",
            "home_team": self.team.id,
            "away_team": self.rival.id,
            "home_score": 5,
            "away_score": 3,
            "batting": [{"player_id": self.batter.id}],
            "pitching": [{"player_id": self.pitcher.id}],
            "innings": api_inning_rows(),
        }
        payload.update(overrides)
        return payload

    def test_save_stats_for_the_roster(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(
            self.client,
            game.id,
            self._stats_payload(
                game,
                batting=[{"player_id": self.batter.id, "at_bats": 4, "singles": 2}],
                pitching=[{"player_id": self.pitcher.id, "innings_pitched": "7.0", "strikeouts": 8}],
            ),
        )

        detail = self.service.get_player_detail(self.team.id, self.batter.id)
        self.assertEqual(detail.at_bats, 4)
        pitcher = self.service.get_player_detail(self.team.id, self.pitcher.id)
        self.assertEqual(pitcher.strikeouts, 8)
        self.assertEqual(pitcher.innings_pitched, "7.0")

    def test_blank_rows_are_not_recorded(self):
        """出場しなかった選手の行を残さない。"""
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(self.client, game.id, self._stats_payload(game))

        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)
        self.assertEqual(orm_models.GamePitchingLine.objects.count(), 0)

    def test_clearing_a_row_removes_the_record(self):
        """一度入力した選手を「出場していない」に戻せること。"""
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(
            self.client,
            game.id,
            self._stats_payload(game, batting=[{"player_id": self.batter.id, "at_bats": 4}]),
        )
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 1)

        post_game_update(self.client, game.id, self._stats_payload(game))
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)

    def test_existing_stats_are_prefilled(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(
            self.client,
            game.id,
            self._stats_payload(game, batting=[{"player_id": self.batter.id, "at_bats": 4}]),
        )

        payload = self.client.get(reverse("game_edit", args=[game.id])).context["payload"]
        batters = [p for roster in payload["rosters"] for p in roster["batters"]]
        batter_row = next(p for p in batters if p["player_id"] == self.batter.id)
        self.assertEqual(batter_row["at_bats"], 4)

    def test_score_can_be_corrected(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(self.client, game.id, self._stats_payload(game, home_score=9))

        self.assertEqual(orm_models.Game.objects.get().home_score, 9)

    def test_missing_game_returns_404(self):
        self.assertEqual(self.client.get(reverse("game_edit", args=[9999])).status_code, 404)


class BoxScoreEntryTest(BaseCase):
    """手動入力でもボックススコアとNPBの記録が揃うこと。

    イニングスコアと継投を入れれば、勝敗・セーブ・ホールドは規則で決まる。
    投手の欄に勝敗を入力させないのは、規則から一意に決まるものを人が入れると
    記録どうしが食い違うため。
    """

    def setUp(self):
        super().setUp()
        login_as_manager(self.client, self.team, self.rival)
        self.starter = self.service.register_player(self.team.id, "先発", 11, "投手")
        self.middle = self.service.register_player(self.team.id, "中継ぎ", 12, "投手")
        self.closer = self.service.register_player(self.team.id, "抑え", 13, "投手")
        self.batter = self.service.register_player(self.team.id, "4番", 3, "内野手")
        self.loser = self.service.register_player(self.rival.id, "相手先発", 21, "投手")
        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)
        self.url = reverse("game_edit", args=[self.game.id])

    def _payload(self, **overrides):
        payload = {
            "year": 2026,
            "played_on": "2026-04-01",
            "home_team": self.team.id,
            "away_team": self.rival.id,
            # ホーム2点・ビジター0点。抑えは1点差以内ではないが3点差以内で締める
            "home_score": 2,
            "away_score": 0,
            "batting": [
                {
                    "player_id": self.batter.id,
                    "at_bats": 4,
                    "singles": 2,
                    "runs_batted_in": 2,
                    "batting_order": 4,
                    "slot_sequence": 0,
                    "fielding_position": "一",
                }
            ],
            # ホームは 先発6回 → 中継ぎ2回 → 抑え1回、ビジターは先発が完投
            "pitching": [
                {"player_id": self.starter.id, "entered_inning": 1, "innings_pitched": "6.0"},
                {"player_id": self.middle.id, "entered_inning": 7, "innings_pitched": "2.0"},
                {"player_id": self.closer.id, "entered_inning": 9, "innings_pitched": "1.0"},
                {"player_id": self.loser.id, "entered_inning": 1, "innings_pitched": "9.0"},
            ],
            "innings": api_inning_rows(away=[0] * 9, home=[2] + [0] * 8),
        }
        payload.update(overrides)
        return payload

    def _line_of(self, player):
        return orm_models.GamePitchingLine.objects.get(game_id=self.game.id, player_id=player.id)

    def test_decisions_are_derived_from_the_line_score(self):
        post_game_update(self.client, self.game.id, self._payload())

        self.assertEqual(self._line_of(self.starter).wins, 1)
        self.assertEqual(self._line_of(self.loser).losses, 1)
        self.assertEqual(self._line_of(self.closer).saves, 1)
        self.assertEqual(self._line_of(self.middle).holds, 1)

    def test_the_winner_does_not_also_get_a_save(self):
        post_game_update(self.client, self.game.id, self._payload())

        self.assertEqual(self._line_of(self.starter).saves, 0)
        self.assertEqual(self._line_of(self.closer).wins, 0)

    def test_a_large_lead_yields_no_save(self):
        """5点差で登板した抑えにはセーブが付かない（1回だけの登板では）。"""
        post_game_update(
            self.client,
            self.game.id,
            self._payload(
                home_score=5,
                innings=api_inning_rows(away=[0] * 9, home=[5] + [0] * 8),
            ),
        )

        self.assertEqual(self._line_of(self.closer).saves, 0)
        self.assertEqual(self._line_of(self.starter).wins, 1)

    def test_the_line_score_must_match_the_final_score(self):
        response = post_game_update(self.client, self.game.id, self._payload(home_score=7))

        self.assertEqual(response.status_code, 400)
        self.assertIn("イニングスコアの合計が得点と一致しません", response.json()["error"])

    def test_lineup_is_saved_and_shown_in_the_box_score(self):
        post_game_update(self.client, self.game.id, self._payload())

        line = orm_models.GameBattingLine.objects.get(game_id=self.game.id, player_id=self.batter.id)
        self.assertEqual(line.batting_order, 4)
        self.assertEqual(line.fielding_position, "一")

        response = self.client.get(reverse("game_detail", args=[self.game.id]))
        self.assertContains(response, "打順")
        self.assertContains(response, "一")

    def test_appearance_order_follows_the_entered_inning(self):
        post_game_update(self.client, self.game.id, self._payload())

        orders = {
            self._line_of(p).player_id: self._line_of(p).appearance_order
            for p in (self.starter, self.middle, self.closer)
        }
        self.assertEqual(orders[self.starter.id], 1)
        self.assertEqual(orders[self.middle.id], 2)
        self.assertEqual(orders[self.closer.id], 3)

    def test_hold_points_accumulate_for_the_reliever(self):
        post_game_update(self.client, self.game.id, self._payload())

        detail = self.service.get_player_detail(self.team.id, self.middle.id)
        self.assertEqual(detail.holds, 1)
        self.assertEqual(detail.hold_points, 1)

    def test_line_score_is_shown_on_the_detail_page(self):
        post_game_update(self.client, self.game.id, self._payload())

        response = self.client.get(reverse("game_detail", args=[self.game.id]))

        self.assertContains(response, "linescore-table")
        self.assertEqual(len(response.context["detail"].line_score.columns), 9)

    def test_the_edit_form_has_no_win_or_save_inputs(self):
        """勝敗・セーブは導出するので、入力欄を置かない。"""
        response = self.client.get(self.url)

        payload = response.context["payload"]
        pitchers = [p for roster in payload["rosters"] for p in roster["pitchers"]]
        self.assertTrue(pitchers)
        for pitcher in pitchers:
            self.assertNotIn("wins", pitcher)
            self.assertNotIn("saves", pitcher)
        self.assertIn("entered_inning", pitchers[0])


class StadiumTest(BaseCase):
    """球場と本拠地。"""

    def test_team_summary_uses_the_stadium(self):
        summary = self.service.list_teams().rows[0]

        self.assertEqual(summary.stadium_name, "テスト球場")
        self.assertEqual(summary.city, "東京")

    def test_team_without_a_stadium(self):
        orm_models.Team.objects.filter(id=self.team.id).update(home_stadium=None)
        summary = {s.id: s for s in self.service.list_teams().rows}[self.team.id]

        self.assertEqual(summary.stadium_name, "")
        self.assertEqual(summary.city, "")

    def test_deleting_a_stadium_keeps_the_team(self):
        """球場を消してもチームは残る（本拠地が未設定になるだけ）。"""
        self.stadium.delete()

        self.assertTrue(orm_models.Team.objects.filter(id=self.team.id).exists())
        self.assertIsNone(orm_models.Team.objects.get(id=self.team.id).home_stadium)

    def test_page_shows_the_stadium(self):
        response = self.client.get(reverse("team_list"))
        self.assertContains(response, "テスト球場")

    def test_can_be_sorted_by_stadium(self):
        other = orm_models.Stadium.objects.create(name="あ球場")
        orm_models.Team.objects.filter(id=self.rival.id).update(home_stadium=other)

        rows = self.service.list_teams(sort="stadium", descending=False).rows
        self.assertEqual(rows[0].stadium_name, "あ球場")

    def test_teams_without_a_stadium_sort_last(self):
        rows = self.service.list_teams(sort="stadium", descending=False).rows
        self.assertEqual(rows[-1].stadium_name, "")

    def test_admin_page(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        response = self.client.get("/admin/myapp/stadium/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テスト球場")


class StadiumHomeTeamAssignmentTest(BaseCase):
    """球場の編集画面から本拠地を決められること。

    所属の出典は Team.home_stadium の1か所のまま。球場の側から
    編めるようにするだけで、関係を二重には持たない。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.dome = orm_models.Stadium.objects.create(name="新ドーム")
        self.url = f"/admin/myapp/stadium/{self.dome.id}/change/"

    def _save(self, team_ids, **overrides):
        payload = {
            "name": self.dome.name,
            "city": "",
            "capacity": "",
            "surface": "",
            "roof": "",
            "opened_year": "",
            "home_teams": [str(i) for i in team_ids],
        }
        payload.update(overrides)
        return self.client.post(self.url, payload)

    def _home_of(self, team):
        return orm_models.Team.objects.get(pk=team.id).home_stadium

    def test_form_offers_the_home_team_field(self):
        response = self.client.get(self.url)

        self.assertContains(response, "home_teams")
        self.assertContains(response, "本拠地とするチーム")

    def test_assigning_teams_moves_their_home(self):
        response = self._save([self.team.id, self.rival.id])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._home_of(self.team), self.dome)
        self.assertEqual(self._home_of(self.rival), self.dome)

    def test_removing_a_team_clears_its_home(self):
        self._save([self.team.id, self.rival.id])

        self._save([self.team.id])

        self.assertEqual(self._home_of(self.team), self.dome)
        self.assertIsNone(self._home_of(self.rival))

    def test_teams_of_other_stadiums_are_left_alone(self):
        """外すのはこの球場を本拠地にしていたチームだけ。

        テストチームはテスト球場が本拠地。新ドームの画面で相手チームだけを
        選んでも、テストチームの本拠地は動かない。
        """
        self._save([self.rival.id])

        self.assertEqual(self._home_of(self.team), self.stadium)
        self.assertEqual(self._home_of(self.rival), self.dome)

    def test_existing_assignment_is_shown_when_reopening(self):
        self._save([self.team.id])

        response = self.client.get(self.url)

        self.assertContains(response, f'value="{self.team.id}" selected')

    def test_saving_with_no_team_selected_clears_the_stadium(self):
        self._save([self.team.id])

        self._save([])

        self.assertIsNone(self._home_of(self.team))


class StadiumOrderingTest(BaseCase):
    """球場一覧の既定の並び。

    球場名順よりも、本拠地とするチームの並びをたどるほうが目的の球場に
    行き着きやすい。使われていない球場は末尾へ回す。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        # テストチーム(表示順1)・相手チーム(表示順2) の順に並ぶようにする
        orm_models.Team.objects.filter(pk=self.team.id).update(display_order=1)
        orm_models.Team.objects.filter(pk=self.rival.id).update(display_order=2)
        # 球場名の順（あ→た→わ）と、チームの並び順をわざと食い違わせる
        orm_models.Stadium.objects.filter(pk=self.stadium.id).update(name="わ球場")
        self.rival_stadium = orm_models.Stadium.objects.create(name="た球場")
        orm_models.Team.objects.filter(pk=self.rival.id).update(home_stadium=self.rival_stadium)
        self.unused = orm_models.Stadium.objects.create(name="あ球場")

    def _listed(self):
        body = self.client.get("/admin/myapp/stadium/").content.decode()
        return re.findall(r'<th class="field-name"><a[^>]*>([^<]+)</a>', body)

    def test_ordered_by_the_home_team_order(self):
        self.assertEqual(self._listed()[:2], ["わ球場", "た球場"])

    def test_stadiums_without_a_home_team_come_last(self):
        self.assertEqual(self._listed()[-1], "あ球場")

    def test_unused_stadiums_are_ordered_by_name_among_themselves(self):
        orm_models.Stadium.objects.create(name="い球場")

        listed = self._listed()

        self.assertEqual(listed[-2:], ["あ球場", "い球場"])

    def test_leagues_are_followed_before_teams(self):
        """リーグの表示順が先に効く。

        球場名でもチームの表示順でも最後に来る球場が、リーグを先に置いた
        ことで先頭へ来る。
        """
        orm_models.League.objects.filter(pk=self.league.id).update(display_order=1)
        other = orm_models.League.objects.create(name="別リーグ", display_order=0)
        far = orm_models.Team.objects.create(league=other, name="別リーグのチーム", display_order=99)
        first = orm_models.Stadium.objects.create(name="ん球場")
        orm_models.Team.objects.filter(pk=far.id).update(home_stadium=first)

        self.assertEqual(self._listed()[0], "ん球場")

    def test_columns_can_still_be_sorted(self):
        """既定を変えても、列を押しての並べ替えは残る。"""
        body = self.client.get("/admin/myapp/stadium/?o=1").content.decode()
        listed = re.findall(r'<th class="field-name"><a[^>]*>([^<]+)</a>', body)

        self.assertEqual(listed, sorted(listed))


class StadiumRoofTest(BaseCase):
    """屋根の種類。雨天中止があり得るかを分ける属性。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def test_roof_is_saved(self):
        response = self.client.post(
            "/admin/myapp/stadium/add/",
            {
                "name": "屋根つき球場",
                "city": "",
                "capacity": "",
                "surface": "",
                "roof": "ドーム",
                "opened_year": "",
                "home_teams": [],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.Stadium.objects.get(name="屋根つき球場").roof, "ドーム")

    def test_choices_come_from_the_domain(self):
        """選択肢を画面側に書き足せないようにしておく（出典はドメイン）。"""
        self.assertEqual(
            [value for value, _ in orm_models.Stadium.ROOF_CHOICES],
            list(StadiumProfile.ROOFS),
        )

    def test_unknown_roof_is_rejected(self):
        response = self.client.post(
            "/admin/myapp/stadium/add/",
            {
                "name": "ガラス球場",
                "city": "",
                "capacity": "",
                "surface": "",
                "roof": "ガラス張り",
                "opened_year": "",
                "home_teams": [],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(orm_models.Stadium.objects.filter(name="ガラス球場").exists())

    def test_roof_appears_in_the_changelist(self):
        orm_models.Stadium.objects.filter(pk=self.stadium.id).update(roof="開閉式屋根")

        response = self.client.get("/admin/myapp/stadium/")

        self.assertContains(response, "開閉式屋根")


class PlayerProfileTest(BaseCase):
    """選手のプロフィール項目。"""

    def test_profile_survives_the_round_trip(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(
            birth_date="1998-03-15",
            throws="右",
            bats="左",
            height_cm=180,
            weight_kg=78,
            birthplace="大阪府",
            debut_year=2021,
        )

        saved = DjangoTeamRepository().find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.profile.height_cm, 180)
        self.assertEqual(saved.profile.throws_bats, "右投左打")
        self.assertEqual(saved.profile.birthplace, "大阪府")

    def test_profile_is_optional(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        saved = DjangoTeamRepository().find_by_id(self.team.id).find_player(player.id)

        self.assertTrue(saved.profile.is_empty)

    def test_amateur_career_survives_the_round_trip(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(
            high_school="甲子園高校",
            university="六大学",
            corporate_team="○○重工",
        )

        saved = DjangoTeamRepository().find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.profile.amateur_path, "甲子園高校 → 六大学 → ○○重工")

    def test_name_kana_and_back_name_reach_the_player_page(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(name_kana="ヤマダタロウ", back_name="T.YAMADA")

        profile = self.service.get_player_profile(self.team.id, player.id)

        self.assertEqual(profile.name_kana, "ヤマダタロウ")
        self.assertEqual(profile.back_name, "T.YAMADA")

    def test_name_kana_and_back_name_appear_on_the_page(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(name_kana="ヤマダタロウ", back_name="T.YAMADA")

        body = self.client.get(reverse("player_detail", args=[self.team.id, player.id])).content.decode()

        self.assertIn("<rt>ヤマダタロウ</rt>", body)
        self.assertIn("T.YAMADA", body)

    def test_page_without_kana_or_back_name_stays_plain(self):
        """未入力の選手に空のルビや区切り記号を出さない。"""
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        body = self.client.get(reverse("player_detail", args=[self.team.id, player.id])).content.decode()

        self.assertNotIn("<ruby>", body)
        self.assertNotIn("back-name", body)

    def test_kana_identical_to_the_name_is_not_shown_as_ruby(self):
        """カタカナ名の選手（外国人など）は名前と読みが同じになるため、ルビを出さない。"""
        player = self.service.register_player(self.team.id, "デイミアン・ベル", 42, "外野手")
        orm_models.Player.objects.filter(id=player.id).update(name_kana="デイミアン・ベル")

        body = self.client.get(reverse("player_detail", args=[self.team.id, player.id])).content.decode()

        self.assertNotIn("<ruby>", body)

    def test_name_kana_and_back_name_survive_an_aggregate_save(self):
        """集約経由の保存で、ドメインが知らない項目として消えないこと。"""
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(name_kana="ヤマダタロウ", back_name="T.YAMADA")

        self.service.update_player(
            team_id=self.team.id, player_id=player.id, name="山田", number=11, position_label="内野手"
        )

        row = orm_models.Player.objects.get(id=player.id)
        self.assertEqual(row.name_kana, "ヤマダタロウ")
        self.assertEqual(row.back_name, "T.YAMADA")

    def test_amateur_career_appears_on_the_player_page(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(high_school="甲子園高校")

        response = self.client.get(reverse("player_detail", args=[self.team.id, player.id]))

        self.assertContains(response, "プロ入り前")
        self.assertContains(response, "甲子園高校")

    def test_player_without_amateur_career(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        profile = self.service.get_player_profile(self.team.id, player.id)

        self.assertEqual(profile.amateur_career, [])

    def test_admin_has_the_amateur_career_section(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.client.force_login(User.objects.create_superuser(username="am", password="x"))

        response = self.client.get(f"/admin/myapp/player/{player.id}/change/")

        self.assertContains(response, "プロ入り前の経歴")
        for field in ("high_school", "university", "corporate_team"):
            with self.subTest(field=field):
                self.assertContains(response, field)

    def test_admin_edit_page_has_the_profile_section(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

        response = self.client.get(f"/admin/myapp/player/{player.id}/change/")

        self.assertContains(response, "プロフィール")
        self.assertContains(response, "birth_date")
        self.assertContains(response, "birthplace")


class TransferTest(BaseCase):
    """移籍と経歴。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")

    def test_transfer_closes_the_old_stint_and_opens_a_new_one(self):
        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        stints = list(orm_models.PlayerStint.objects.filter(player_id=self.player.id).order_by("from_year"))
        self.assertEqual(len(stints), 2)
        self.assertEqual(stints[0].team_id, self.team.id)
        self.assertEqual(stints[0].to_year, 2026)
        self.assertEqual(stints[1].team_id, self.rival.id)
        self.assertEqual(stints[1].number, 7)
        self.assertIsNone(stints[1].to_year)

    def test_player_appears_on_the_new_roster_only(self):
        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        self.assertEqual(self.service.list_batters(self.team.id).rows, [])
        self.assertEqual([r.name for r in self.service.list_batters(self.rival.id).rows], ["山田"])

    def test_stats_follow_the_player_not_the_team(self):
        """成績は選手に紐づくので、移籍しても失われない。"""
        give_batting(self.team, self.rival, self.player.id, BattingLine(at_bats=10, singles=3))

        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        detail = self.service.get_player_detail(self.rival.id, self.player.id)
        self.assertEqual(detail.at_bats, 10)

    def test_the_old_number_becomes_available(self):
        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        # 空いた10番を別の選手が使える
        self.service.register_player(self.team.id, "田中", 10, "外野手")
        self.assertEqual([r.name for r in self.service.list_batters(self.team.id).rows], ["田中"])

    def test_number_in_use_at_the_destination_is_rejected(self):
        self.service.register_player(self.rival.id, "先客", 7, "外野手")

        with self.assertRaises(DuplicateJerseyNumber):
            self.service.transfer_player(
                self.player.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=7,
                year=2026,
            )

    def test_career_is_visible_on_the_team_aggregate(self):
        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        team = DjangoTeamRepository().find_by_id(self.rival.id)
        career = team.find_player(self.player.id).career

        self.assertEqual([s.team_name for s in career], ["相手チーム", "テストチーム"])

    def test_admin_shows_the_current_team(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        response = self.client.get("/admin/myapp/player/")

        self.assertContains(response, "テストチーム")
        self.assertContains(response, "field-current_number")

    def test_failed_transfer_leaves_the_source_team_untouched(self):
        """検査に失敗したら、元チームの退団も取り消されること（部分的な保存を残さない）。"""
        self.service.register_player(self.rival.id, "先客", 7, "外野手")

        with self.assertRaises(DuplicateJerseyNumber):
            self.service.transfer_player(
                self.player.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=7,
                year=2026,
            )

        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertIsNone(stint.to_year)
        self.assertEqual(stint.team_id, self.team.id)


class ForeignPlayerQuotaTest(BaseCase):
    """外国人枠（登録上限・試合出場上限）。"""

    def setUp(self):
        super().setUp()
        self.foreign = self.service.register_player(self.team.id, "助っ人", 50, "外野手")
        orm_models.Player.objects.filter(id=self.foreign.id).update(is_foreign_player=True)

    def test_transfer_rejects_when_destination_quota_exceeded(self):
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_roster_limit=0)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.service.transfer_player(
                self.foreign.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=99,
                year=2026,
            )

        # ロールバックされ、元チームの在籍は閉じられていない
        stint = orm_models.PlayerStint.objects.get(player_id=self.foreign.id)
        self.assertIsNone(stint.to_year)
        self.assertEqual(stint.team_id, self.team.id)

    def test_update_game_rejects_when_home_team_quota_exceeded(self):
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=0)
        game = play_game(self.team, self.rival)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.service.update_game(
                game.id,
                year=2026,
                played_on=game.played_on,
                home_team_id=self.team.id,
                away_team_id=self.rival.id,
                home_score=1,
                away_score=0,
                batting={self.foreign.id: BattingLine(at_bats=1, singles=1)},
            )
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)

    def test_update_game_rejects_when_away_team_quota_exceeded(self):
        """ホーム・ビジターは独立に判定する。ホームに助っ人がいなくても、
        ビジター側の上限超過は検出される。"""
        rival_foreign = self.service.register_player(self.rival.id, "助っ人2", 51, "外野手")
        orm_models.Player.objects.filter(id=rival_foreign.id).update(is_foreign_player=True)
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=0)
        game = play_game(self.team, self.rival)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.service.update_game(
                game.id,
                year=2026,
                played_on=game.played_on,
                home_team_id=self.team.id,
                away_team_id=self.rival.id,
                home_score=1,
                away_score=0,
                batting={rival_foreign.id: BattingLine(at_bats=1, singles=1)},
            )

    def test_update_game_allows_exactly_at_the_game_limit(self):
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=1)
        game = play_game(self.team, self.rival)

        self.service.update_game(
            game.id,
            year=2026,
            played_on=game.played_on,
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=1,
            away_score=0,
            batting={self.foreign.id: BattingLine(at_bats=1, singles=1)},
        )  # 例外にならない

        self.assertEqual(orm_models.GameBattingLine.objects.count(), 1)

    def test_update_game_with_no_limit_set_never_rejects(self):
        """空欄（無制限）はリーグの既定値（3人）とは別に、明示的に検証しておく。"""
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=None)
        game = play_game(self.team, self.rival)

        self.service.update_game(
            game.id,
            year=2026,
            played_on=game.played_on,
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=1,
            away_score=0,
            batting={self.foreign.id: BattingLine(at_bats=1, singles=1)},
        )  # 例外にならない

        self.assertEqual(orm_models.GameBattingLine.objects.count(), 1)


class CaptaincyApplicationTest(BaseCase):
    """主将の指名・解任。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")

    def test_appoint_and_remove_round_trip_through_the_database(self):
        self.service.appoint_captain(self.team.id, self.player.id, 2026)

        team = DjangoTeamRepository().find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, self.player.id)

        self.service.remove_captain(self.team.id, self.player.id, 2027)

        team = DjangoTeamRepository().find_by_id(self.team.id)
        self.assertIsNone(team.current_captain)

    def test_player_edit_appoints_a_captain(self):
        login_as_manager(self.client, self.team, username="u")

        response = self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"appoint_captain": "1"},
        )

        self.assertRedirects(response, reverse("player_edit", args=[self.team.id, self.player.id]))
        team = DjangoTeamRepository().find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, self.player.id)

    def test_player_edit_shows_duplicate_captain_error(self):
        other = self.service.register_player(self.team.id, "田中", 11, "外野手")
        self.service.appoint_captain(self.team.id, other.id)
        login_as_manager(self.client, self.team, username="u")

        response = self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"appoint_captain": "1"},
            follow=True,
        )

        self.assertContains(response, "には既に主将")
        team = DjangoTeamRepository().find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, other.id)


class AdminStintValidationTest(BaseCase):
    """管理画面から過去の経歴を登録するときの検証。

    管理画面はドメインを経由しないため、判定を素通しにすると
    「同じチームで同じ背番号の選手が同時に2人」を作れてしまう。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.other = self.service.register_player(self.team.id, "田中", 11, "外野手")

    def _add(self, **overrides):
        payload = {
            "player": self.other.id,
            "team": self.team.id,
            "number": "10",
            "from_year": "2020",
            "to_year": "",
        }
        payload.update(overrides)
        return self.client.post("/admin/myapp/playerstint/add/", payload)

    def test_past_stint_can_be_registered(self):
        """別チームでの過去の在籍は普通に登録できる。"""
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        self.client.post(
            "/admin/myapp/playerstint/add/",
            {
                "player": self.player.id,
                "team": past.id,
                "number": "55",
                "from_year": "2020",
                "to_year": "2023",
            },
        )

        stints = orm_models.PlayerStint.objects.filter(player_id=self.player.id)
        self.assertEqual(stints.count(), 2)
        self.assertTrue(stints.filter(team=past, number=55, to_year=2023).exists())

    def test_overlapping_number_is_rejected(self):
        response = self._add()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "期間が重なる同じ背番号は登録できません")
        self.assertFalse(orm_models.PlayerStint.objects.filter(player_id=self.other.id, number=10).exists())

    def test_same_number_is_allowed_when_periods_do_not_overlap(self):
        """期間が重ならなければ同じ背番号を使える。"""
        # 山田は10番を2024〜2025で使い終えている
        orm_models.PlayerStint.objects.filter(player_id=self.player.id).update(from_year=2024, to_year=2025)
        # 田中の既存の在籍は別の年にしておく（同じ年の二重加入を避けるため）
        orm_models.PlayerStint.objects.filter(player_id=self.other.id).update(from_year=2020, to_year=2021)

        self._add(from_year="2026", to_year="")

        self.assertTrue(
            orm_models.PlayerStint.objects.filter(player_id=self.other.id, number=10, from_year=2026).exists()
        )

    def test_joining_the_same_team_twice_in_a_year_is_rejected(self):
        """同じチームに同じ年から二重に加入することはない。"""
        response = self._add(number="99", from_year="2026")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "既に存在します")

    def test_leaving_before_joining_is_rejected(self):
        response = self._add(number="99", from_year="2026", to_year="2020")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "退団年が加入年より前")

    def test_blank_joining_year_falls_back_to_the_debut_year(self):
        """最初の在籍では加入年＝入団年になることがほとんど。

        同じ年を二度入力させる意味が無いので、空欄なら入団年で埋める。
        """
        orm_models.Player.objects.filter(pk=self.other.id).update(debut_year=2019)
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        self._add(team=past.id, number="55", from_year="")

        stint = orm_models.PlayerStint.objects.get(player_id=self.other.id, team=past)
        self.assertEqual(stint.from_year, 2019)

    def test_explicit_joining_year_wins_over_the_debut_year(self):
        orm_models.Player.objects.filter(pk=self.other.id).update(debut_year=2019)
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        self._add(team=past.id, number="55", from_year="2022")

        stint = orm_models.PlayerStint.objects.get(player_id=self.other.id, team=past)
        self.assertEqual(stint.from_year, 2022)

    def test_blank_joining_year_is_rejected_without_a_debut_year(self):
        """埋める材料が無いときだけ入力を求める。"""
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        response = self._add(team=past.id, number="55", from_year="")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "加入年を入力してください")
        self.assertFalse(orm_models.PlayerStint.objects.filter(team=past).exists())

    def test_editing_a_stint_does_not_conflict_with_itself(self):
        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)

        response = self.client.post(
            f"/admin/myapp/playerstint/{stint.id}/change/",
            {
                "player": self.player.id,
                "team": self.team.id,
                "number": "10",
                "from_year": "2024",
                "to_year": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        stint.refresh_from_db()
        self.assertEqual(stint.from_year, 2024)


class AdminCaptaincyValidationTest(BaseCase):
    """管理画面から主将を登録するときの検証。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.other = self.service.register_player(self.team.id, "田中", 11, "外野手")

    def _change_payload(self, player, **captaincy_overrides):
        payload = {
            "name": player.name,
            "position": "内野手",
            "birth_date": "",
            "throws": "",
            "bats": "",
            "height_cm": "",
            "weight_kg": "",
            "birthplace": "",
            "debut_year": "",
            "high_school": "",
            "university": "",
            "corporate_team": "",
            "nationality": "",
            "is_foreign_player": "",
            "stints-TOTAL_FORMS": "0",
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "captaincies-TOTAL_FORMS": "1",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
            "captaincies-0-team": str(self.team.id),
            "captaincies-0-from_year": "2026",
            "captaincies-0-to_year": "",
            "captaincies-0-id": "",
        }
        payload.update(captaincy_overrides)
        return payload

    def test_appointing_a_captain_via_admin(self):
        response = self.client.post(f"/admin/myapp/player/{self.player.id}/change/", self._change_payload(self.player))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            orm_models.Captaincy.objects.filter(
                player_id=self.player.id, team_id=self.team.id, to_year__isnull=True
            ).exists()
        )

    def test_duplicate_captain_is_rejected(self):
        orm_models.Captaincy.objects.create(player_id=self.other.id, team_id=self.team.id, from_year=2025)

        response = self.client.post(f"/admin/myapp/player/{self.player.id}/change/", self._change_payload(self.player))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "に主将です")

    def test_appointing_a_player_not_on_the_roster_is_rejected(self):
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        response = self.client.post(
            f"/admin/myapp/player/{self.player.id}/change/",
            self._change_payload(self.player, **{"captaincies-0-team": str(past.id)}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "在籍していないため主将にできません")


class AdminForeignPlayerQuotaTest(BaseCase):
    """管理画面から外国人選手を登録・移籍するときの、枠の検証。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_roster_limit=1)
        self.existing_foreign = self.service.register_player(self.team.id, "既存助っ人", 50, "外野手")
        orm_models.Player.objects.filter(id=self.existing_foreign.id).update(is_foreign_player=True)
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")

    def _player_change_payload(self, player, **overrides):
        payload = {
            "name": player.name,
            "position": "内野手",
            "birth_date": "",
            "throws": "",
            "bats": "",
            "height_cm": "",
            "weight_kg": "",
            "birthplace": "",
            "debut_year": "",
            "high_school": "",
            "university": "",
            "corporate_team": "",
            "nationality": "",
            "is_foreign_player": "",
            "stints-TOTAL_FORMS": "0",
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "captaincies-TOTAL_FORMS": "0",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
        }
        payload.update(overrides)
        return payload

    def test_marking_a_player_as_foreign_is_rejected_over_the_roster_limit(self):
        response = self.client.post(
            f"/admin/myapp/player/{self.player.id}/change/",
            self._player_change_payload(self.player, is_foreign_player="on"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "外国人選手登録数が上限")
        self.assertFalse(orm_models.Player.objects.get(id=self.player.id).is_foreign_player)

    def test_transferring_a_foreign_player_is_rejected_over_the_destination_limit(self):
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_roster_limit=0)

        response = self.client.post(
            "/admin/myapp/playerstint/add/",
            {
                "player": self.existing_foreign.id,
                "team": self.rival.id,
                "number": "77",
                "from_year": "2026",
                "to_year": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "外国人選手登録数が上限")
        self.assertFalse(
            orm_models.PlayerStint.objects.filter(player_id=self.existing_foreign.id, team_id=self.rival.id).exists()
        )


class AdminUsesDomainRulesTest(BaseCase):
    """管理画面から保存できる値と、ドメインが許す値をそろえる。

    管理画面はドメインを経由しないため、繋いでおかないと画面からだけ
    現実的でない値を保存できてしまう。在籍だけが検証されていて、
    球場とプロフィールは素通りだった。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def _player_payload(self, **overrides):
        payload = {
            "name": "検証太郎",
            "position": "投手",
            "birth_date": "",
            "throws": "",
            "bats": "",
            "height_cm": "",
            "weight_kg": "",
            "birthplace": "",
            "debut_year": "",
            "high_school": "",
            "university": "",
            "corporate_team": "",
            "stints-TOTAL_FORMS": "0",
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "captaincies-TOTAL_FORMS": "0",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
        }
        payload.update(overrides)
        return payload

    def test_unrealistic_height_is_rejected(self):
        response = self.client.post("/admin/myapp/player/add/", self._player_payload(height_cm="400"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "身長の値が現実的ではありません")
        self.assertFalse(orm_models.Player.objects.filter(name="検証太郎").exists())

    def test_debut_year_outside_the_season_range_is_rejected(self):
        response = self.client.post("/admin/myapp/player/add/", self._player_payload(debut_year="1800"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "シーズンは")
        self.assertFalse(orm_models.Player.objects.filter(name="検証太郎").exists())

    def test_realistic_profile_is_accepted(self):
        response = self.client.post(
            "/admin/myapp/player/add/",
            self._player_payload(
                height_cm="180",
                weight_kg="78",
                debut_year="2021",
                birthplace="大阪府",
            ),
        )

        self.assertEqual(response.status_code, 302)
        player = orm_models.Player.objects.get(name="検証太郎")
        self.assertEqual((player.height_cm, player.debut_year), (180, 2021))

    def test_stadium_opened_year_outside_the_season_range_is_rejected(self):
        response = self.client.post(
            "/admin/myapp/stadium/add/",
            {
                "name": "検証球場",
                "city": "",
                "capacity": "",
                "surface": "",
                "opened_year": "1800",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "シーズンは")
        self.assertFalse(orm_models.Stadium.objects.filter(name="検証球場").exists())

    def test_valid_stadium_is_accepted(self):
        response = self.client.post(
            "/admin/myapp/stadium/add/",
            {
                "name": "検証球場",
                "city": "仙台市",
                "capacity": "30000",
                "surface": "人工芝",
                "opened_year": "1950",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(orm_models.Stadium.objects.filter(name="検証球場").exists())


class AdminPlayerWithStintsTest(BaseCase):
    """選手登録画面から、在籍（経歴）を一緒に登録できること。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.past = orm_models.Team.objects.create(league=self.league, name="前所属")

    def _post(self, stints, debut_year=""):
        payload = {
            "name": "新人太郎",
            "position": "投手",
            "birth_date": "",
            "throws": "",
            "bats": "",
            "height_cm": "",
            "weight_kg": "",
            "birthplace": "",
            "debut_year": debut_year,
            "high_school": "",
            "university": "",
            "corporate_team": "",
            "stints-TOTAL_FORMS": str(len(stints)),
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "captaincies-TOTAL_FORMS": "0",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
        }
        for i, (team, number, from_year, to_year) in enumerate(stints):
            payload.update(
                {
                    f"stints-{i}-team": str(team.id),
                    f"stints-{i}-number": str(number),
                    f"stints-{i}-from_year": str(from_year),
                    f"stints-{i}-to_year": str(to_year),
                    f"stints-{i}-id": "",
                    f"stints-{i}-player": "",
                }
            )
        return self.client.post("/admin/myapp/player/add/", payload)

    def _created(self):
        return orm_models.Player.objects.filter(name="新人太郎").first()

    def test_new_player_can_be_registered_with_a_stint(self):
        """新規登録では選手がまだ保存されていない。

        その状態で既存の在籍と突き合わせようとして落ちていた。
        """
        response = self._post([(self.team, 18, 2024, "")])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._created().stints.count(), 1)

    def test_new_player_can_be_registered_with_a_transfer_history(self):
        """経歴を複数まとめて登録できる。"""
        self._post([(self.past, 18, 2018, 2021), (self.team, 11, 2022, "")])

        stints = self._created().stints.order_by("from_year")
        self.assertEqual(
            [(s.team_id, s.number, s.from_year, s.to_year) for s in stints],
            [(self.past.id, 18, 2018, 2021), (self.team.id, 11, 2022, None)],
        )

    def test_mid_season_transfer_is_allowed(self):
        """移籍元と移籍先が同じ年を共有するのは普通のこと。"""
        response = self._post([(self.past, 18, 2020, 2022), (self.team, 11, 2022, "")])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._created().stints.count(), 2)

    def test_rejoining_the_same_team_later_is_allowed(self):
        response = self._post([(self.team, 18, 2018, 2021), (self.team, 99, 2024, "")])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._created().stints.count(), 2)

    def test_overlapping_stints_at_the_same_team_are_rejected(self):
        """1行ずつの検証では、同時に送られた行どうしの矛盾に気づけない。"""
        response = self._post([(self.team, 18, 2018, 2021), (self.team, 11, 2020, "")])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じチームに同時に2度在籍することはできません")
        self.assertIsNone(self._created())

    def test_stint_takes_the_debut_year_entered_on_the_same_page(self):
        """入団年はまだ保存されていないが、同じ画面で入力されている。"""
        response = self._post([(self.team, 18, "", "")], debut_year="2021")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._created().stints.get().from_year, 2021)

    def test_number_taken_by_another_player_is_still_rejected(self):
        """新規登録でも、他の選手との背番号の重なりは弾く。"""
        self.service.register_player(self.team.id, "山田", 10, "内野手")

        response = self._post([(self.team, 10, 2026, "")])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "期間が重なる同じ背番号は登録できません")
        self.assertIsNone(self._created())


class LeagueAccordionTest(BaseCase):
    """リーグ一覧で所属チームを折りたたんで確認できること。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.register_player(self.team.id, "佐藤", 18, "投手")

    def _body(self):
        return self.client.get("/admin/myapp/league/").content.decode()

    def test_teams_are_listed_inside_details(self):
        body = self._body()

        self.assertIn('<details class="team-accordion">', body)
        self.assertIn("テストチーム", body)
        self.assertIn("相手チーム", body)

    def test_summary_shows_the_count(self):
        self.assertIn("<summary>2チーム</summary>", self._body())

    def test_each_team_links_to_its_edit_page(self):
        self.assertIn(f"/admin/myapp/team/{self.team.id}/change/", self._body())

    def test_active_player_count_is_shown(self):
        """在籍中の人数を出す。退団した選手は数えない。"""
        self.assertIn("2名", self._body())

    def test_retired_players_are_not_counted(self):
        player = self.service.register_player(self.team.id, "退団", 99, "内野手")
        self.service.retire_player(self.team.id, player.id)

        # 在籍中は2名のまま
        self.assertIn("2名", self._body())

    def test_league_without_teams(self):
        orm_models.League.objects.create(name="空リーグ")
        body = self._body()

        self.assertIn("空リーグ", body)
        self.assertIn("—", body)

    def test_team_names_are_escaped(self):
        """チーム名をそのまま埋め込まないこと。"""
        orm_models.Team.objects.create(league=self.league, name="<script>x</script>")

        self.assertNotIn("<script>x</script>", self._body())

    def test_query_count_does_not_grow_with_rows(self):
        """行ごとにチームを引くと一覧で N+1 になるため、先読みしている。

        リーグを増やしても問い合わせ数が変わらないことを確かめる
        （所属チームはまとめて1回で取る）。
        """

        def count_queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get("/admin/myapp/league/")
            return len(captured)

        before = count_queries()

        for i in range(5):
            league = orm_models.League.objects.create(name=f"L{i}")
            orm_models.Team.objects.create(league=league, name=f"T{i}")

        self.assertEqual(count_queries(), before)


class PlayerSearchTest(BaseCase):
    """選手を名前で探す。所属を知らなくてもたどり着けるようにする。"""

    def setUp(self):
        super().setUp()
        self.yamada = self.service.register_player(self.team.id, "山田太郎", 10, "内野手")
        self.yamamoto = self.service.register_player(self.team.id, "山本次郎", 11, "投手")
        self.tanaka = self.service.register_player(self.rival.id, "田中三郎", 7, "外野手")
        self.url = reverse("player_search")

    def _search(self, keyword):
        return self.client.get(f"{self.url}?q={keyword}").context["results"]

    def test_partial_match(self):
        names = [r.name for r in self._search("山")]
        self.assertEqual(sorted(names), ["山本次郎", "山田太郎"])

    def test_exact_name(self):
        self.assertEqual([r.name for r in self._search("田中三郎")], ["田中三郎"])

    def test_shows_the_current_team_and_league(self):
        row = self._search("田中三郎")[0]

        self.assertEqual(row.team_name, "相手チーム")
        self.assertEqual(row.league_name, "テストリーグ")
        self.assertEqual(row.number, 7)
        self.assertTrue(row.is_active)

    def test_retired_players_are_found(self):
        """退団した選手も探せる。経歴を確認したい場面があるため。"""
        self.service.retire_player(self.team.id, self.yamada.id)

        row = next(r for r in self._search("山田") if r.name == "山田太郎")

        self.assertFalse(row.is_active)
        self.assertEqual(row.team_name, "テストチーム")

    def test_transferred_player_shows_the_current_team(self):
        self.service.transfer_player(
            self.yamada.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=99,
            year=2026,
        )

        row = self._search("山田太郎")[0]

        self.assertEqual(row.team_name, "相手チーム")
        self.assertEqual(row.number, 99)

    def test_no_match(self):
        response = self.client.get(f"{self.url}?q=存在しない名前")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "見つかりませんでした")

    def test_empty_keyword_shows_the_form_only(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["searched"])
        self.assertEqual(response.context["results"], [])

    def test_search_box_is_on_every_page(self):
        for name in ("dashboard", "team_list", "game_list"):
            with self.subTest(page=name):
                self.assertContains(self.client.get(reverse(name)), "app-search")

    def test_results_link_to_the_player_page(self):
        body = self.client.get(f"{self.url}?q=田中").content.decode()
        self.assertIn(reverse("player_detail", args=[self.rival.id, self.tanaka.id]), body)


class LeagueOrderingTest(BaseCase):
    """リーグの表示順を管理画面から並べ替えられること。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        # 名前順なら A → Z だが、表示順で逆にする
        orm_models.League.objects.filter(id=self.league.id).update(name="Zリーグ", display_order=1)
        self.first = orm_models.League.objects.create(name="Aリーグ", display_order=2)
        orm_models.Team.objects.create(league=self.first, name="Aチーム")

    def test_display_order_beats_name(self):
        names = [lg.name for lg in DjangoLeagueRepository().find_all()]
        self.assertEqual(names, ["Zリーグ", "Aリーグ"])

    def test_same_order_falls_back_to_name(self):
        orm_models.League.objects.update(display_order=0)
        names = [lg.name for lg in DjangoLeagueRepository().find_all()]
        self.assertEqual(names, ["Aリーグ", "Zリーグ"])

    def test_admin_list_is_ordered_and_editable(self):
        body = self.client.get("/admin/myapp/league/").content.decode()

        self.assertLess(body.index("Zリーグ"), body.index("Aリーグ"))
        # 一覧から直接編集できる（ドラッグの結果もここに入る）
        self.assertIn('name="form-0-display_order"', body)

    def test_admin_loads_the_sortable_script(self):
        self.assertContains(self.client.get("/admin/myapp/league/"), "admin-inline-sortable.js")

    def test_order_is_reflected_in_standings(self):
        play_game(self.team, self.rival, day=1)
        a2 = orm_models.Team.objects.create(league=self.first, name="A2チーム")
        play_game(orm_models.Team.objects.get(name="Aチーム"), a2, day=1)

        names = [g.league_name for g in self.service.get_standings(2026).leagues]
        self.assertEqual(names, ["Zリーグ", "Aリーグ"])

    def test_order_is_reflected_in_the_team_list(self):
        names = [g.league_name for g in self.service.list_teams_by_league().rows]
        self.assertEqual(names, ["Zリーグ", "Aリーグ"])

    def test_order_is_reflected_in_dashboard_tabs(self):
        names = [g.league_name for g in self.service.get_dashboard().leagues]
        self.assertEqual(names, ["Zリーグ", "Aリーグ"])


class MatchupViewTest(BaseCase):
    """対戦成績（フェーズ4）。リーグ画面に、順位表と同じ並びで並べる。"""

    def setUp(self):
        super().setUp()
        self.third = orm_models.Team.objects.create(league=self.league, name="第三チーム")
        # テストチームは相手チームに2勝0敗、第三チームに0勝1敗
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        play_game(self.team, self.rival, home_score=2, away_score=1, day=2)
        play_game(self.third, self.team, home_score=6, away_score=0, day=3)
        self.url = reverse("league_detail", args=[self.league.id])

    def _table(self):
        return self.service.get_league_detail(self.league.id).matchups

    def test_rows_and_columns_share_the_order(self):
        table = self._table()

        self.assertEqual([c.team_id for c in table.columns], [r.team_id for r in table.rows])

    def test_record_against_each_opponent(self):
        table = self._table()
        row = next(r for r in table.rows if r.team_id == self.team.id)
        cells = {c.opponent_id: c.label for c in row.cells}

        self.assertEqual(cells[self.rival.id], "2-0-0")
        self.assertEqual(cells[self.third.id], "0-1-0")
        self.assertEqual(row.total_label, "2-1-0")

    def test_own_column_is_blank(self):
        table = self._table()
        row = next(r for r in table.rows if r.team_id == self.team.id)
        own = next(c for c in row.cells if c.is_self)

        self.assertIsNone(own.opponent_id)
        self.assertEqual(own.label, "—")

    def test_winning_and_losing_are_marked(self):
        table = self._table()
        row = next(r for r in table.rows if r.team_id == self.team.id)
        cells = {c.opponent_id: c for c in row.cells}

        self.assertTrue(cells[self.rival.id].is_winning)
        self.assertTrue(cells[self.third.id].is_losing)

    def test_page_shows_the_table(self):
        response = self.client.get(self.url)

        self.assertContains(response, "対戦成績")
        self.assertContains(response, "2-0-0")

    def test_table_follows_the_selected_season(self):
        play_game(self.team, self.rival, year=2025, home_score=0, away_score=9, day=1)

        table = self.service.get_league_detail(self.league.id, 2025).matchups
        row = next(r for r in table.rows if r.team_id == self.team.id)

        self.assertEqual(row.total_label, "0-1-0")

    def test_league_without_games_has_no_table(self):
        empty = orm_models.League.objects.create(name="無試合リーグ")
        orm_models.Team.objects.create(league=empty, name="新チーム")

        self.assertIsNone(self.service.get_league_detail(empty.id).matchups)


class MonthlySplitViewTest(BaseCase):
    """月別成績（フェーズ4）。通算値では見えない調子の波を出す。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self._play(month=4, day=1, at_bats=4, singles=1)
        self._play(month=4, day=2, at_bats=4, singles=1)
        self._play(month=5, day=1, at_bats=4, singles=3)
        self.url = reverse("player_detail", args=[self.team.id, self.player.id])

    def _play(self, *, month, day, **line):
        game = Game(
            season=Season(2026),
            played_on=date(2026, month, day),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=1,
            away_score=0,
        )
        game.record_batting(self.player.id, BattingLine(**line))
        DjangoGameRepository().save(game)

    def _months(self, player_id=None):
        profile = self.service.get_player_profile(self.team.id, player_id or self.player.id)
        return profile.months

    def test_grouped_by_month_oldest_first(self):
        self.assertEqual([m.label for m in self._months()], ["2026年4月", "2026年5月"])

    def test_rate_comes_from_the_monthly_total(self):
        april, may = self._months()

        self.assertEqual(april.appearances, 2)
        self.assertAlmostEqual(april.batting_average, 0.25)
        self.assertAlmostEqual(may.batting_average, 0.75)

    def test_months_without_appearance_are_omitted(self):
        play_game(self.team, self.rival, year=2026, day=20)

        self.assertEqual(len(self._months()), 2)

    def test_player_without_games_has_no_months(self):
        self.assertEqual(self._months(self.pitcher.id), [])

    def test_page_shows_the_table(self):
        response = self.client.get(self.url)

        self.assertContains(response, "月別成績")
        self.assertContains(response, "2026年4月")

    def test_page_of_a_player_without_games_omits_the_table(self):
        response = self.client.get(reverse("player_detail", args=[self.team.id, self.pitcher.id]))

        self.assertNotContains(response, "月別成績")


class LeagueTitlesViewTest(BaseCase):
    """リーグのタイトル一覧（フェーズ4）。

    ダッシュボードのランキングは通算成績だが、タイトルはシーズンごとに
    争われるので、対象シーズンの試合だけから成績を積み直す。
    """

    def setUp(self):
        super().setUp()
        self.slugger = self.service.register_player(self.team.id, "大砲", 3, "内野手")
        self.ace = self.service.register_player(self.team.id, "エース", 18, "投手")
        self.url = reverse("league_titles", args=[self.league.id])

    def _departments(self, year=None):
        titles = self.service.get_league_titles(self.league.id, year)
        return {d.key: d for d in titles.departments}

    def test_home_run_leader(self):
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=10, home_runs=3),
            day=1,
        )

        leader = self._departments()["home_runs"].leader

        self.assertEqual(leader.player_name, "大砲")
        self.assertEqual(leader.value, "3")

    def test_runs_batted_in_leader(self):
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=10, singles=4, runs_batted_in=7),
            day=1,
        )

        leader = self._departments()["rbi"].leader

        self.assertEqual(leader.player_name, "大砲")
        self.assertEqual(leader.value, "7")

    def test_rate_departments_require_qualification(self):
        """1打数1安打の選手は首位打者にしない。規定打席で絞る。"""
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=1, singles=1),
            day=1,
        )

        self.assertEqual(self._departments()["average"].entries, [])

    def test_qualified_batter_takes_the_batting_title(self):
        # 1試合なら規定打席は ceil(1 × 3.1) = 4 打席
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=4, singles=2),
            day=1,
        )

        leader = self._departments()["average"].leader

        self.assertEqual(leader.player_name, "大砲")
        self.assertEqual(leader.value, ".500")

    def test_era_department_requires_qualifying_innings(self):
        give_pitching(
            self.team,
            self.rival,
            self.ace.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=2),
            day=1,
        )

        leader = self._departments()["era"].leader

        self.assertEqual(leader.player_name, "エース")
        self.assertEqual(leader.value, "2.00")

    def test_win_and_save_leaders(self):
        """勝利・セーブも部門になる。数そのものが記録なので規定は設けない。"""
        give_pitching(
            self.team,
            self.rival,
            self.ace.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), wins=1),
            day=1,
        )
        closer = self.service.register_player(self.team.id, "守護神", 22, "投手")
        give_pitching(
            self.team,
            self.rival,
            closer.id,
            PitchingLine(innings=InningsPitched.from_notation("1.0"), saves=1),
            day=2,
        )

        departments = self._departments()

        self.assertEqual(departments["wins"].leader.player_name, "エース")
        self.assertEqual(departments["wins"].leader.value, "1")
        self.assertEqual(departments["saves"].leader.player_name, "守護神")
        self.assertEqual(departments["saves"].leader.value, "1")

    def test_departments_are_scoped_to_the_season(self):
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=10, home_runs=5),
            year=2025,
            day=1,
        )
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=10, home_runs=1),
            year=2026,
            day=1,
        )

        self.assertEqual(self._departments(2025)["home_runs"].leader.value, "5")
        self.assertEqual(self._departments(2026)["home_runs"].leader.value, "1")

    def test_players_of_another_league_are_not_listed(self):
        other_league = orm_models.League.objects.create(name="別リーグ")
        other = orm_models.Team.objects.create(league=other_league, name="別チーム")
        opponent = orm_models.Team.objects.create(league=other_league, name="別の相手")
        outsider = self.service.register_player(other.id, "他リーグの大砲", 9, "内野手")
        give_batting(other, opponent, outsider.id, BattingLine(at_bats=10, home_runs=9), day=1)
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=10, home_runs=1),
            day=2,
        )

        names = [e.player_name for e in self._departments()["home_runs"].entries]

        self.assertEqual(names, ["大砲"])

    def test_page_renders_every_department(self):
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=4, singles=2, home_runs=1, runs_batted_in=3),
            day=1,
        )

        response = self.client.get(self.url)

        for label in ["首位打者", "本塁打王", "打点王", "最優秀防御率", "最多勝利", "最多セーブ", "最多奪三振"]:
            self.assertContains(response, label)

    def test_page_without_games_says_so(self):
        response = self.client.get(self.url)

        self.assertContains(response, "まだ登録されていません")

    def test_missing_league_returns_404(self):
        self.assertEqual(self.client.get(reverse("league_titles", args=[9999])).status_code, 404)

    def test_league_page_links_to_the_titles_page(self):
        play_game(self.team, self.rival)

        response = self.client.get(reverse("league_detail", args=[self.league.id]))

        self.assertContains(response, reverse("league_titles_by_year", args=[self.league.id, 2026]))


class LeagueStatsViewTest(BaseCase):
    """リーグの成績一覧。所属する全選手の通算成績を1つの表で見る。"""

    def setUp(self):
        super().setUp()
        self.slugger = self.service.register_player(self.team.id, "大砲", 3, "内野手")
        self.contact = self.service.register_player(self.rival.id, "安打製造機", 7, "外野手")
        self.ace = self.service.register_player(self.team.id, "エース", 18, "投手")
        self.url = reverse("league_stats", args=[self.league.id])

    def test_batters_span_teams_within_the_league(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=10, home_runs=2), day=1)
        give_batting(self.rival, self.team, self.contact.id, BattingLine(at_bats=10, singles=5), day=2)

        rows = self.service.get_league_stats(self.league.id).listing.rows

        self.assertEqual({r.player.name for r in rows}, {"大砲", "安打製造機"})
        self.assertEqual({r.team_name for r in rows}, {"テストチーム", "相手チーム"})

    def test_players_of_another_league_are_not_listed(self):
        other_league = orm_models.League.objects.create(name="別リーグ")
        other = orm_models.Team.objects.create(league=other_league, name="別チーム")
        self.service.register_player(other.id, "他リーグの大砲", 9, "内野手")

        rows = self.service.get_league_stats(self.league.id).listing.rows

        self.assertNotIn("他リーグの大砲", [r.player.name for r in rows])

    def test_default_batter_sort_is_ops(self):
        listing = self.service.get_league_stats(self.league.id).listing

        self.assertEqual(listing.sort, "ops")
        self.assertTrue(listing.descending)

    def test_sort_key_from_url_is_applied(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=10, home_runs=2), day=1)
        give_batting(self.rival, self.team, self.contact.id, BattingLine(at_bats=10, singles=5), day=2)

        listing = self.service.get_league_stats(self.league.id, sort="average").listing

        self.assertEqual(listing.sort, "average")
        self.assertEqual([r.player.name for r in listing.rows], ["安打製造機", "大砲"])

    def test_invalid_sort_key_falls_back_to_the_default(self):
        listing = self.service.get_league_stats(self.league.id, sort="怪しいキー").listing

        self.assertEqual(listing.sort, "ops")

    def test_pitcher_mode_lists_pitchers(self):
        give_pitching(
            self.team,
            self.rival,
            self.ace.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), wins=1, strikeouts=9),
            day=1,
        )

        rows = self.service.get_league_stats(self.league.id, pitchers=True).listing.rows

        self.assertEqual([r.player.name for r in rows], ["エース"])
        self.assertEqual(rows[0].player.wins, 1)

    def test_page_renders_and_links_to_player_pages(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=10, home_runs=2), day=1)

        body = self.client.get(self.url).content.decode()

        self.assertIn(reverse("player_detail", args=[self.team.id, self.slugger.id]), body)
        self.assertIn("成績一覧", body)

    def test_pitcher_page_renders(self):
        response = self.client.get(f"{self.url}?pos=pitcher")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "防御率")

    def test_missing_league_returns_404(self):
        self.assertEqual(self.client.get(reverse("league_stats", args=[9999])).status_code, 404)

    def test_league_page_links_here(self):
        response = self.client.get(reverse("league_detail", args=[self.league.id]))

        self.assertContains(response, reverse("league_stats", args=[self.league.id]))


class TeamMonthlySplitViewTest(BaseCase):
    """チームの月別成績（フェーズ4）。個人の月別成績と対になる推移。"""

    def setUp(self):
        super().setUp()
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.url = reverse("player_list", args=[self.team.id])

    def test_grouped_by_month_oldest_first(self):
        play_game(self.team, self.rival, month=5, day=1)
        play_game(self.team, self.rival, month=4, day=1)

        rows = self.service.list_team_monthly_splits(self.team.id)

        self.assertEqual([r.label for r in rows], ["2026年4月", "2026年5月"])

    def test_record_and_rate_per_month(self):
        play_game(
            self.team,
            self.rival,
            month=4,
            day=1,
            home_score=5,
            away_score=3,
            batting={self.batter.id: BattingLine(at_bats=4, singles=1)},
        )
        play_game(
            self.team,
            self.rival,
            month=4,
            day=2,
            home_score=1,
            away_score=2,
            batting={self.batter.id: BattingLine(at_bats=4, singles=1)},
        )

        april = self.service.list_team_monthly_splits(self.team.id)[0]

        self.assertEqual(april.games_played, 2)
        self.assertEqual(april.record_label, "1-1-0")
        self.assertAlmostEqual(april.batting_average, 0.25)  # 2/8

    def test_the_opponents_lines_are_not_counted(self):
        """試合の明細には相手の選手も入る。自チームの分だけを合計する。"""
        theirs = self.service.register_player(self.rival.id, "相手", 1, "内野手")
        play_game(
            self.team,
            self.rival,
            month=4,
            batting={
                self.batter.id: BattingLine(at_bats=4, singles=1),
                theirs.id: BattingLine(at_bats=4, singles=4),
            },
        )

        april = self.service.list_team_monthly_splits(self.team.id)[0]

        self.assertAlmostEqual(april.batting_average, 0.25)

    def test_page_shows_the_table(self):
        play_game(self.team, self.rival, month=4)

        response = self.client.get(self.url)

        self.assertContains(response, "月別成績")
        self.assertContains(response, "2026年4月")

    def test_team_without_games_has_no_months(self):
        self.assertEqual(self.service.list_team_monthly_splits(self.team.id), [])
        self.assertNotContains(self.client.get(self.url), "月別成績")


class RelativeMetricsTest(BaseCase):
    """対リーグ相対指標（フェーズ4）。OPS+ と ERA+。

    どちらもリーグ平均を100とした指数。リーグ全体の合計から基準を作るため、
    リーグを知らないアプリ層では確定できない。
    """

    def setUp(self):
        super().setUp()
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        give_batting(
            self.team,
            self.rival,
            self.batter.id,
            BattingLine(at_bats=10, singles=2, home_runs=1),
            day=1,
        )
        give_pitching(
            self.team,
            self.rival,
            self.pitcher.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=3),
            day=2,
        )

    def test_a_lone_player_sits_at_the_league_average(self):
        """リーグにその選手しかいなければ、本人がリーグ平均そのもの＝100。"""
        batter = self.service.get_player_detail(self.team.id, self.batter.id)
        pitcher = self.service.get_player_detail(self.team.id, self.pitcher.id)

        self.assertAlmostEqual(batter.ops_plus, 100.0)
        self.assertAlmostEqual(pitcher.era_plus, 100.0)

    def test_a_better_batter_scores_above_a_hundred(self):
        """リーグ平均より良い打者は100を超える。"""
        weak = self.service.register_player(self.team.id, "田中", 11, "外野手")
        give_batting(self.team, self.rival, weak.id, BattingLine(at_bats=10, singles=1), day=3)

        rows = {r.name: r for r in self.service.list_batters(self.team.id).rows}

        self.assertGreater(rows["山田"].ops_plus, 100.0)
        self.assertLess(rows["田中"].ops_plus, 100.0)

    def test_era_plus_rewards_the_lower_earned_run_average(self):
        """ERA+ は防御率が低いほど大きい（FIP と向きが逆）。"""
        weak = self.service.register_player(self.team.id, "鈴木", 19, "投手")
        give_pitching(
            self.team,
            self.rival,
            weak.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=9),
            day=4,
        )

        rows = {r.name: r for r in self.service.list_pitchers(self.team.id).rows}

        self.assertGreater(rows["佐藤"].era_plus, rows["鈴木"].era_plus)
        self.assertGreater(rows["佐藤"].era_plus, 100.0)

    def test_players_in_another_league_do_not_move_the_baseline(self):
        """指数はリーグごとに閉じる。他リーグの成績は基準に混ぜない。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        other = orm_models.Team.objects.create(league=other_league, name="別チーム")
        opponent = orm_models.Team.objects.create(league=other_league, name="別の相手")
        slugger = self.service.register_player(other.id, "大砲", 3, "内野手")
        give_batting(
            other,
            opponent,
            slugger.id,
            BattingLine(at_bats=10, home_runs=5),
            day=5,
        )

        # 別リーグに強打者が現れても、こちらのリーグの基準は動かない
        detail = self.service.get_player_detail(self.team.id, self.batter.id)

        self.assertAlmostEqual(detail.ops_plus, 100.0)

    def test_lists_and_pages_show_the_indexes(self):
        batters = self.client.get(reverse("player_list", args=[self.team.id]))
        pitchers = self.client.get(f"{reverse('player_list', args=[self.team.id])}?pos=pitcher")
        page = self.client.get(reverse("player_detail", args=[self.team.id, self.batter.id]))

        self.assertContains(batters, "OPS+")
        self.assertContains(pitchers, "ERA+")
        self.assertContains(page, "OPS+")


class AdvancedMetricsTest(BaseCase):
    """指標の拡充（フェーズ4）。FIP と IsoP。"""

    def setUp(self):
        super().setUp()
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_pitching(
            self.team,
            self.rival,
            self.pitcher.id,
            PitchingLine(
                innings=InningsPitched.from_notation("9.0"),
                earned_runs=3,
                hits_allowed=6,
                home_runs_allowed=1,
                walks_allowed=2,
                hit_by_pitch_allowed=1,
                strikeouts=9,
            ),
            day=1,
        )
        give_batting(
            self.team,
            self.rival,
            self.batter.id,
            BattingLine(at_bats=10, singles=1, home_runs=2),
            day=2,
        )

    def test_new_counts_survive_the_round_trip(self):
        detail = self.service.get_player_detail(self.team.id, self.pitcher.id)

        self.assertEqual(detail.home_runs_allowed, 1)
        self.assertEqual(detail.hit_by_pitch_allowed, 1)

    def test_fip_uses_the_league_constant(self):
        """リーグに1人しかいなければ、その投手の FIP は防御率と一致する。

        定数はリーグ全体の防御率と素点の差なので、本人＝リーグ全体のとき
        素点との差がそのまま埋まる。
        """
        detail = self.service.get_player_detail(self.team.id, self.pitcher.id)

        self.assertAlmostEqual(detail.fip, detail.earned_run_average)

    def test_isolated_power(self):
        detail = self.service.get_player_detail(self.team.id, self.batter.id)

        self.assertAlmostEqual(detail.isolated_power, detail.slugging_percentage - detail.batting_average)

    def test_pitcher_list_shows_fip(self):
        response = self.client.get(f"{reverse('player_list', args=[self.team.id])}?pos=pitcher")

        self.assertContains(response, "FIP")
        self.assertContains(response, "BB/9")
        self.assertAlmostEqual(response.context["players"][0].fip, 3.0)

    def test_batter_list_shows_iso(self):
        response = self.client.get(reverse("player_list", args=[self.team.id]))

        self.assertContains(response, "IsoP")
        self.assertAlmostEqual(response.context["players"][0].isolated_power, 0.6)

    def test_pitchers_can_be_sorted_by_fip(self):
        weak = self.service.register_player(self.team.id, "田中", 19, "投手")
        give_pitching(
            self.team,
            self.rival,
            weak.id,
            PitchingLine(
                innings=InningsPitched.from_notation("9.0"),
                earned_runs=9,
                hits_allowed=12,
                home_runs_allowed=4,
                walks_allowed=5,
            ),
            day=3,
        )

        listing = self.service.list_pitchers(self.team.id, sort="fip")

        self.assertEqual([r.name for r in listing.rows], ["佐藤", "田中"])

    def test_team_totals_include_fip(self):
        totals = self.service.get_team_totals(self.team.id)

        self.assertAlmostEqual(totals.fip, totals.earned_run_average)

    def test_player_page_shows_fip(self):
        response = self.client.get(reverse("player_detail", args=[self.team.id, self.pitcher.id]))

        self.assertContains(response, "FIP")

    def test_entry_form_saves_the_new_counts(self):
        login_as_manager(self.client, self.team, username="scorer")
        self.client.post(
            reverse("game_create"),
            {
                "year": "2026",
                "played_on": "2026-05-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "home_score": "1",
                "away_score": "0",
            },
        )
        game = orm_models.Game.objects.latest("id")

        response = self.client.get(reverse("game_edit", args=[game.id]))
        payload = response.context["payload"]
        pitchers = [p for roster in payload["rosters"] for p in roster["pitchers"]]

        pitching_rows = [
            {
                "player_id": p["player_id"],
                **(
                    {
                        "innings_pitched": "6.0",
                        "hits_allowed": 5,
                        "home_runs_allowed": 2,
                        "hit_by_pitch_allowed": 1,
                    }
                    if p["player_id"] == self.pitcher.id
                    else {}
                ),
            }
            for p in pitchers
        ]
        post_game_update(
            self.client,
            game.id,
            {
                "year": 2026,
                "played_on": "2026-05-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "home_score": 1,
                "away_score": 0,
                "batting": [],
                "pitching": pitching_rows,
                "innings": api_inning_rows(),
            },
        )

        line = orm_models.GamePitchingLine.objects.get(game=game, player_id=self.pitcher.id)
        self.assertEqual(line.home_runs_allowed, 2)
        self.assertEqual(line.hit_by_pitch_allowed, 1)

    def test_home_runs_beyond_hits_allowed_are_rejected(self):
        """被本塁打は被安打の内数。超える入力は保存させない。"""
        login_as_manager(self.client, self.team, username="scorer2")
        self.client.post(
            reverse("game_create"),
            {
                "year": "2026",
                "played_on": "2026-06-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "home_score": "1",
                "away_score": "0",
            },
        )
        game = orm_models.Game.objects.latest("id")
        payload = self.client.get(reverse("game_edit", args=[game.id])).context["payload"]
        pitchers = [p for roster in payload["rosters"] for p in roster["pitchers"]]

        pitching_rows = [
            {
                "player_id": p["player_id"],
                **(
                    {"innings_pitched": "6.0", "hits_allowed": 1, "home_runs_allowed": 3}
                    if p["player_id"] == self.pitcher.id
                    else {}
                ),
            }
            for p in pitchers
        ]
        response = post_game_update(
            self.client,
            game.id,
            {
                "year": 2026,
                "played_on": "2026-06-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "home_score": 1,
                "away_score": 0,
                "batting": [],
                "pitching": pitching_rows,
                "innings": api_inning_rows(),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(orm_models.GamePitchingLine.objects.filter(game=game).exists())


class TeamManagerPermissionTest(BaseCase):
    """チーム担当者制（フェーズ5）。

    ログインしただけでは書き込めない。担当するチームが関わる範囲だけを
    編集でき、管理ユーザーは担当に関わらず全権を持つ。
    """

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.game = play_game(self.team, self.rival)
        # 相手チームとも別リーグとも無関係な、どこも担当していないチーム
        self.outsider = orm_models.Team.objects.create(league=self.league, name="無関係チーム")

    def _player_edit_url(self):
        return reverse("player_edit", args=[self.team.id, self.player.id])

    # --- 選手 ---

    def test_manager_can_open_player_edit(self):
        login_as_manager(self.client, self.team)
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 200)

    def test_logged_in_non_manager_is_rejected(self):
        """ログインしていても担当外なら編集できない。"""
        self.client.force_login(User.objects.create_user(username="other", password="x"))
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 403)

    def test_manager_of_another_team_is_rejected(self):
        login_as_manager(self.client, self.outsider)
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 403)

    def test_staff_can_edit_any_team(self):
        self.client.force_login(User.objects.create_user(username="staff", password="x", is_staff=True))
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 200)

    def test_anonymous_is_sent_to_login_not_403(self):
        """未ログインは拒否ではなくログインへ誘導する。まだ入る余地があるため。"""
        response = self.client.get(self._player_edit_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_non_manager_cannot_register_a_player(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.post(
            reverse("player_list", args=[self.team.id]),
            {"name": "田中", "number": "11", "position": "外野手"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(orm_models.PlayerStint.objects.filter(number=11).exists())

    def test_player_list_hides_write_controls_from_non_managers(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.get(reverse("player_list", args=[self.team.id]))

        self.assertEqual(response.status_code, 200)  # 閲覧はできる
        self.assertNotContains(response, "新入団選手の登録")

    def test_player_list_shows_write_controls_to_managers(self):
        login_as_manager(self.client, self.team)

        response = self.client.get(reverse("player_list", args=[self.team.id]))

        self.assertContains(response, "新入団選手の登録")

    # --- 試合 ---

    def test_manager_of_either_side_can_edit_the_game(self):
        """試合は2チームにまたがる。どちらか一方の担当者なら編集できる。"""
        login_as_manager(self.client, self.rival)
        self.assertEqual(self.client.get(reverse("game_edit", args=[self.game.id])).status_code, 200)

    def test_manager_of_an_uninvolved_team_cannot_edit_the_game(self):
        login_as_manager(self.client, self.outsider)
        self.assertEqual(self.client.get(reverse("game_edit", args=[self.game.id])).status_code, 403)

    def test_game_detail_hides_the_edit_link_from_non_managers(self):
        login_as_manager(self.client, self.outsider)

        response = self.client.get(reverse("game_detail", args=[self.game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "記録を編集")

    def test_creating_a_game_between_teams_you_do_not_manage_is_refused(self):
        login_as_manager(self.client, self.outsider)

        response = self.client.post(
            reverse("game_create"),
            {
                "year": "2026",
                "played_on": "2026-04-02",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "home_score": "1",
                "away_score": "0",
            },
            follow=True,
        )

        self.assertContains(response, "どちらのチームも担当していない")
        self.assertEqual(orm_models.Game.objects.count(), 1)  # 既存の1件のみ

    def test_game_list_hides_the_create_link_when_you_manage_nothing(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.get(reverse("game_list"))

        self.assertNotContains(response, "試合を登録")


class AuthTest(TestCase):
    def test_login_redirect_url_resolves(self):
        from django.conf import settings

        self.assertTrue(reverse(settings.LOGIN_REDIRECT_URL))

    def test_signup_page_is_reachable(self):
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 200)
