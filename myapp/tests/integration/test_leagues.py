"""リーグの画面。所属チーム・リーグ詳細・タイトル・成績一覧。"""

from django.contrib.auth.models import User
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
    give_pitching,
    play_game,
)
from .base import BaseCase


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
