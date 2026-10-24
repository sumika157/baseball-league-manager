"""ペナントの歴代の記録と球団史（#105）。

終わったシーズンだけを載せ、何も書かない。優勝は世界の終了と同じ関数（`final_standings`）、
タイトルは締めた年は表彰（#248。締めたときにタイトルの画面と同じ集計で導いて保存した受賞者。表彰そのものは
`test_pennant_awards.py`）、締めていない年は順位表の隣のタイトルの画面と同じ集計（`get_league_titles`）から出るので、
ここは「画面どうしが食い違わない」「終わっていないシーズンは載らない」を確かめる。
世界は球団2つ（1リーグ）なので、リーグの切り替えは選び方の単体で確かめる。
"""

from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.application.dto import LeagueOption, LeagueStandings, StandingRow
from myapp.application.pennant_history import _selected_league
from myapp.application.pennant_race import final_standings, own_race, tiebreak_for
from myapp.domain.exceptions import TeamNotFound
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.infrastructure import orm_models
from myapp.presentation.views import (
    build_pennant_history_service,
    build_pennant_home_service,
    build_pennant_offseason_service,
    build_pennant_season_service,
    build_pennant_world_view,
    build_world_league_view_service,
)

from .test_pennant_advance import YEAR
from .test_pennant_home import HomeCase


class HistoryCase(HomeCase):
    def history_url(self, world_id=None) -> str:
        return reverse("pennant_history", args=[self.world_a if world_id is None else world_id])

    def club_url(self, team_id, world_id=None) -> str:
        return reverse("pennant_team_history", args=[self.world_a if world_id is None else world_id, team_id])

    def finish_season(self) -> None:
        """日程を作り、いまのシーズンを最後まで進める（シーズン終了）。"""
        self.start_season()
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)

    def close_and_finish(self) -> None:
        """シーズンを締めて、次のシーズンを最後まで進める。"""
        build_pennant_offseason_service(self.world_a).close_season()
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)

    def context(self):
        return build_pennant_world_view().get_context(self.world_a)

    def history(self, **kwargs):
        return build_pennant_history_service(self.world_a).get_world_history(self.context(), **kwargs)

    def club(self, team_id):
        return build_pennant_history_service(self.world_a).get_club_history(self.context(), team_id)


class FinishedSeasonTest(HistoryCase):
    """終わったシーズンが載り、順位表・タイトルの画面と一致する。"""

    def test_the_champion_is_the_first_place_of_the_standings_screen(self):
        self.finish_season()
        (shown,) = build_world_league_view_service(self.world_a).get_league_standings(YEAR)
        leader = shown.rows[0]

        history = self.history(league_id=None)

        (year,) = history.years
        (cell,) = year.champions
        self.assertEqual(year.year, YEAR)
        self.assertEqual((cell.team_id, cell.team_name), (leader.team_id, leader.team_name))
        self.assertEqual(
            cell.record, f"{leader.wins}勝{leader.losses}敗" + (f"{leader.ties}分" if leader.ties else "")
        )
        self.assertEqual(cell.is_own, leader.team_id == self.context().managed_team_id)

    def test_the_title_holders_are_the_first_place_of_the_titles_screen(self):
        self.finish_season()
        view = build_world_league_view_service(self.world_a)
        league_id = self.history(league_id=None).selected_league_id
        assert league_id is not None
        titles = view.get_league_titles(league_id, YEAR)

        (year,) = self.history(league_id=None).years

        shown = {d.key: [e.player_id for e in d.entries or [] if e.rank == 1] for d in titles.departments}
        cells = {c.department_key: [e.player_id for e in c.holders] for c in (*year.batting, *year.pitching)}
        self.assertEqual(cells, shown)
        self.assertTrue(any(cells.values()), "1年ぶんの試合があるので、受賞者はいる")

    def test_the_columns_come_from_the_departments_of_the_titles(self):
        self.finish_season()
        league_id = self.history(league_id=None).selected_league_id
        assert league_id is not None
        titles = build_world_league_view_service(self.world_a).get_league_titles(league_id, YEAR)

        history = self.history(league_id=None)

        self.assertEqual([c.key for c in history.batting_columns], [d.key for d in titles.batting_departments])
        self.assertEqual([c.key for c in history.pitching_columns], [d.key for d in titles.pitching_departments])
        self.assertEqual([c.label for c in history.batting_columns][:1], ["首位打者"])

    def test_the_screens_show_the_champion_and_link_to_the_club_history(self):
        self.finish_season()
        (shown,) = build_world_league_view_service(self.world_a).get_league_standings(YEAR)
        leader = shown.rows[0]

        content = self.client.get(self.history_url()).content.decode()

        self.assertEqual(self.client.get(self.history_url()).status_code, 200)
        self.assertIn(f"{YEAR}年", content)
        self.assertIn(leader.team_name, content)
        self.assertIn(self.club_url(leader.team_id), content)
        self.assertIn("首位打者", content)

    def test_an_invalid_league_in_the_url_falls_back_to_the_default_league(self):
        self.finish_season()
        default = self.history(league_id=None).selected_league_id

        for value in ("abc", "²", "9999999", "-1", ""):
            with self.subTest(league=value):
                response = self.client.get(self.history_url(), {"league": value})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["history"].selected_league_id, default)

    def test_the_year_closed_stays_after_the_season_is_closed(self):
        """締めた後の開幕前は年度が翌年に進む。締めた年は、載ったままになる。"""
        self.finish_season()
        build_pennant_offseason_service(self.world_a).close_season()

        years = [y.year for y in self.history(league_id=None).years]

        self.assertEqual(years, [YEAR])


class UnfinishedSeasonTest(HistoryCase):
    """終わっていないシーズンは載らない（優勝もタイトルも確定していない）。"""

    def test_a_world_with_no_games_is_empty_but_opens(self):
        for url in (self.history_url(), self.club_url(self._home_id())):
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 200)
                self.assertIn("まだ終わったシーズンがありません", response.content.decode())

    def test_a_season_in_progress_is_not_listed(self):
        self.start_season()
        for _ in range(3):
            build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)

        self.assertEqual(self.history(league_id=None).years, [])
        self.assertEqual(self.club(self._home_id()).seasons, [])

    def test_the_season_in_progress_is_not_listed_after_a_finished_one(self):
        self.finish_season()
        build_pennant_offseason_service(self.world_a).close_season()
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)

        self.assertEqual([y.year for y in self.history(league_id=None).years], [YEAR])

    def _home_id(self) -> int:
        team = orm_models.Team.objects.get(league__world_id=self.world_a, name="ホーム球団")
        return team.id


class ClubHistoryTest(HistoryCase):
    def test_the_seasons_match_the_standings_and_the_totals_match_the_finale(self):
        self.finish_season()
        self.close_and_finish()
        view = build_world_league_view_service(self.world_a)
        (first,) = view.get_league_standings(YEAR)
        team_id = first.rows[0].team_id
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=team_id)

        with mock.patch("myapp.application.pennant_view.is_final_season", return_value=True):
            context = build_pennant_world_view().get_context(self.world_a)
            finale = build_pennant_home_service(self.world_a).get_home(context, include_advance=False).finale
            club = build_pennant_history_service(self.world_a).get_club_history(context, team_id)

        assert finale is not None
        self.assertEqual([s.year for s in club.seasons], [YEAR, YEAR + 1])
        for season in club.seasons:
            (league,) = view.get_league_standings(season.year)
            row = next(r for r in league.rows if r.team_id == team_id)
            self.assertEqual(
                (season.rank, season.wins, season.losses, season.ties, season.team_count),
                (row.rank, row.wins, row.losses, row.ties, len(league.rows)),
            )
            self.assertEqual(
                (season.winning_percentage, season.games_behind), (row.winning_percentage, row.games_behind)
            )
        self.assertEqual(club.record, finale.record, "通算は世界の終了と同じ関数を通る")
        self.assertEqual(club.titles, finale.titles)
        self.assertEqual([s.is_champion for s in club.seasons], [s.is_champion for s in finale.seasons])
        self.assertEqual(club.titles, sum(s.is_champion for s in club.seasons))

    def test_the_titles_are_only_the_players_of_the_club(self):
        self.finish_season()
        view = build_world_league_view_service(self.world_a)
        league_id = self.history(league_id=None).selected_league_id
        assert league_id is not None
        titles = view.get_league_titles(league_id, YEAR)
        teams = {t.id for t in view.list_teams().rows}

        for team_id in teams:
            with self.subTest(team_id=team_id):
                (season,) = self.club(team_id).seasons
                expected = {
                    (d.label, e.player_id)
                    for d in titles.departments
                    for e in d.entries or []
                    if e.rank == 1 and e.team_id == team_id
                }
                self.assertEqual({(t.label, t.player_id) for t in season.titles}, expected)
                self.assertTrue(all(t.team_id == team_id for t in season.titles))

    def test_a_retired_player_keeps_the_title_of_the_year_he_played(self):
        self.finish_season()
        self.close_and_finish()
        view = build_world_league_view_service(self.world_a)
        league_id = self.history(league_id=None).selected_league_id
        assert league_id is not None
        shown = view.get_league_titles(league_id, YEAR)
        holder = next(e for d in shown.departments for e in d.entries or [] if e.rank == 1)
        # 最初の年を最後に退団した（2年目の在籍は無い）ことにする
        orm_models.PlayerStint.objects.filter(player_id=holder.player_id).update(to_year=YEAR)

        history = self.history(league_id=None)

        (first, *_) = history.years
        cells = [
            c for c in (*first.batting, *first.pitching) if any(h.player_id == holder.player_id for h in c.holders)
        ]
        self.assertTrue(cells, "退団した選手の、在籍していた年のタイトルは残る")

    def test_a_team_outside_the_world_is_not_found(self):
        other_world_team = orm_models.Team.objects.filter(league__world_id=self.world_b).first()
        real_team = orm_models.Team.objects.filter(league__world_id__isnull=True).first()
        for team_id in (other_world_team.id, real_team.id, 999999):
            with self.subTest(team_id=team_id):
                self.assertEqual(self.client.get(self.club_url(team_id)).status_code, 404)
                with self.assertRaises(TeamNotFound):
                    self.club(team_id)

    def test_the_club_screen_is_open_to_anyone_and_offers_no_write_link(self):
        self.finish_season()
        (shown,) = build_world_league_view_service(self.world_a).get_league_standings(YEAR)

        for url in (self.history_url(), self.club_url(shown.rows[0].team_id)):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('method="post"', response.content.decode().lower())
                self.assertEqual(self.client.post(url).status_code, 405)

    def test_the_club_screen_shows_the_champion_mark_the_totals_and_the_standings_link(self):
        self.finish_season()
        (shown,) = build_world_league_view_service(self.world_a).get_league_standings(YEAR)
        leader, runner_up = shown.rows[:2]

        champion = self.client.get(self.club_url(leader.team_id)).content.decode()
        second = self.client.get(self.club_url(runner_up.team_id)).content.decode()

        self.assertIn("優勝", champion)
        self.assertIn("1回", champion)
        self.assertIn(reverse("pennant_standings_by_year", args=[self.world_a, YEAR]), champion)
        self.assertIn("0回", second)
        self.assertNotIn('badge bg-primary">優勝', second)


class LinksTest(HistoryCase):
    def test_the_world_bar_links_the_history_and_marks_it_active(self):
        content = self.client.get(self.history_url()).content.decode()

        self.assertIn(f'href="{self.history_url()}">歴代</a>', content)
        self.assertRegex(content, r'is-active"\s+href="' + self.history_url() + '">歴代')

    def test_the_team_page_links_the_club_history_only_inside_a_world(self):
        team = orm_models.Team.objects.filter(league__world_id=self.world_a).first()
        real = orm_models.Team.objects.filter(league__world_id__isnull=True).first()

        world_page = self.client.get(reverse("pennant_player_list", args=[self.world_a, team.id])).content.decode()
        real_page = self.client.get(reverse("player_list", args=[real.id])).content.decode()

        self.assertIn(self.club_url(team.id), world_page)
        self.assertNotIn("球団史", real_page)
        self.assertNotIn("/history/", real_page)

    def test_the_end_card_links_the_club_history_of_the_own_team(self):
        self.finish_season()
        (shown,) = build_world_league_view_service(self.world_a).get_league_standings(YEAR)
        team_id = shown.rows[0].team_id
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=team_id)

        with mock.patch("myapp.application.pennant_view.is_final_season", return_value=True):
            content = self.client.get(reverse("pennant_world", args=[self.world_a])).content.decode()

        self.assertIn("世界の終了", content)
        self.assertIn(f'href="{self.club_url(team_id)}">球団史を見る', content)


class QueryCountTest(HistoryCase):
    """クエリ数は球団・選手の数に比例しない。締めた年のタイトルは表彰を1本で読むので（#248）、年が増えて
    増えるのは順位の読み込みだけで、タイトルの集計（締めていない年の分）は増えない。"""

    def count(self, url) -> int:
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(queries)

    def test_a_closed_year_adds_fewer_queries_than_a_derived_one(self):
        team_id = orm_models.Team.objects.get(league__world_id=self.world_a, name="ホーム球団").id
        urls = (self.history_url(), self.club_url(team_id))
        none = [self.count(url) for url in urls]
        self.finish_season()
        one = [self.count(url) for url in urls]
        self.close_and_finish()
        two = [self.count(url) for url in urls]

        for index, url in enumerate(urls):
            with self.subTest(url=url):
                # none → one: 締めていない年が1つ（順位とタイトルの集計）。one → two: 締めた年が1つ増える（順位だけ）
                self.assertGreater(one[index], none[index])
                self.assertGreater(two[index], one[index])
                self.assertLess(two[index] - one[index], one[index] - none[index], "締めた年はタイトルを集計しない")


class SelectedLeagueTest(SimpleTestCase):
    """受賞者を見るリーグ。無効な指定はエラーにせず、既定のリーグ、それも無ければ先頭に落とす。"""

    OPTIONS = [LeagueOption(id=1, name="A"), LeagueOption(id=2, name="B"), LeagueOption(id=3, name="C")]

    def test_a_valid_request_is_used(self):
        self.assertEqual(_selected_league(self.OPTIONS, 3, 2).id, 3)

    def test_an_unknown_request_falls_back_to_the_default_league(self):
        self.assertEqual(_selected_league(self.OPTIONS, 99, 2).id, 2)
        self.assertEqual(_selected_league(self.OPTIONS, None, 2).id, 2)

    def test_without_a_valid_default_the_first_league_is_used(self):
        self.assertEqual(_selected_league(self.OPTIONS, 99, None).id, 1)
        self.assertEqual(_selected_league(self.OPTIONS, None, 99).id, 1)

    def test_no_league_means_no_selection(self):
        self.assertIsNone(_selected_league([], 1, 1))


class FinalStandingsTest(SimpleTestCase):
    """同率首位の決着は、前年の最終順位（規定で決めた優勝を1位にしたもの）を翌年に渡す。"""

    def row(self, rank, team_id):
        return StandingRow(rank, team_id, f"球団{team_id}", 80, 63, 0, 143, "", "")

    def game(self, year, winner, loser):
        return SimpleNamespace(
            year=year, is_recorded=True, winner_team_id=winner, home_team_id=winner, away_team_id=loser
        )

    def finals(self, years, standings, games):
        with mock.patch(
            "myapp.application.pennant_race.league_standings",
            lambda teams, leagues, year_games: [
                LeagueStandings(league_id=1, league_name="リーグ", rows=standings[year_games[0].year])
            ],
        ):
            return final_standings(years, teams=[], leagues=[], games_of=lambda year: games[year], order=[1, 2, 3])

    def test_last_years_rule_winner_is_ranked_first_when_the_next_year_is_tied_again(self):
        # 2026: 球団1と2が同率首位。直接対決で2が勝つ → 優勝は2（順位は2を1位、1を2位として次の年へ）
        # 2027: 球団1と2が同率首位。直接対決は五分。前年の順位で2が上 → 優勝は2（並び順なら1になる）
        standings = {
            2026: [self.row(1, 1), self.row(1, 2), self.row(3, 3)],
            2027: [self.row(1, 1), self.row(1, 2), self.row(3, 3)],
        }
        games = {
            2026: [self.game(2026, 2, 1), self.game(2026, 2, 1), self.game(2026, 1, 2)],
            2027: [self.game(2027, 1, 2), self.game(2027, 2, 1)],
        }

        first, second = self.finals([2026, 2027], standings, games)

        self.assertEqual(dict(first.champions), {1: 2})
        self.assertEqual(dict(second.champions), {1: 2})

    def test_a_gap_in_the_years_means_no_previous_rank(self):
        """前年が無い年（飛んだ年）は、開幕年と同じ扱い（前年の順位は使わない）。"""
        standings = {
            2026: [self.row(1, 1), self.row(1, 2)],
            2028: [self.row(1, 1), self.row(1, 2)],
        }
        games = {
            2026: [self.game(2026, 2, 1)],
            2028: [self.game(2028, 1, 2), self.game(2028, 2, 1)],
        }

        first, second = self.finals([2026, 2028], standings, games)

        self.assertEqual(dict(first.champions), {1: 2})
        self.assertEqual(dict(second.champions), {1: 1}, "前年の順位が無いので、並び順で球団1")

    def test_years_are_returned_oldest_first_and_a_year_without_games_has_no_champion(self):
        standings = {2026: [], 2027: [self.row(1, 1), self.row(2, 2)]}
        games = {2026: [self.game(2026, 1, 2)], 2027: [self.game(2027, 1, 2)]}

        with mock.patch(
            "myapp.application.pennant_race.league_standings",
            lambda teams, leagues, year_games: (
                [] if year_games[0].year == 2026 else [LeagueStandings(1, "リーグ", standings[2027])]
            ),
        ):
            finals = final_standings(
                [2027, 2026], teams=[], leagues=[], games_of=lambda year: games[year], order=[1, 2]
            )

        self.assertEqual([f.year for f in finals], [2026, 2027])
        self.assertEqual(dict(finals[0].champions), {})
        self.assertEqual(dict(finals[1].champions), {1: 1})


class ChampionAgreementTest(SimpleTestCase):
    """同率首位が2年続いても、優勝の決め方は2つの経路（`final_standings` と `tiebreak_for`）で一致する。

    前年の最終順位をたどる鎖は、`final_standings`（歴代・球団史・世界の終了）と `tiebreak_for`（進行中の優勝確定）に
    それぞれある。片方だけ直して食い違わないよう、同じ材料で突き合わせる。
    """

    ROWS = [
        StandingRow(1, 1, "球団1", 80, 63, 0, 143, "", ""),
        StandingRow(1, 2, "球団2", 80, 63, 0, 143, "", ""),
        StandingRow(3, 3, "球団3", 60, 83, 0, 143, "", ""),
    ]
    # 2026: 直接対決で球団2。2027: 直接対決は五分なので、前年の最終順位（球団2が1位）で球団2
    RESULTS = {2026: [(2, 1), (2, 1), (1, 2)], 2027: [(1, 2), (2, 1)]}

    def test_both_paths_pick_the_same_champion_in_two_tied_years(self):
        standings = [LeagueStandings(league_id=1, league_name="リーグ", rows=self.ROWS)]
        games = {
            year: [
                SimpleNamespace(year=year, is_recorded=True, winner_team_id=w, home_team_id=w, away_team_id=loser)
                for w, loser in results
            ]
            for year, results in self.RESULTS.items()
        }
        league_view = SimpleNamespace(
            get_league_standings=lambda year: standings if year in self.RESULTS else [],
            list_teams=lambda: SimpleNamespace(rows=[SimpleNamespace(id=n) for n in (1, 2, 3)]),
        )
        query = SimpleNamespace(
            list_rows=lambda year=None: [
                SimpleNamespace(
                    is_recorded=True,
                    winner_team_id=g.winner_team_id,
                    home_team_id=g.home_team_id,
                    away_team_id=g.away_team_id,
                )
                for g in games[year]
            ]
        )

        with mock.patch("myapp.application.pennant_race.league_standings", lambda t, lg, gs: standings):
            finals = final_standings([2026, 2027], teams=[], leagues=[], games_of=lambda y: games[y], order=[1, 2, 3])

        for final in finals:
            tiebreak = tiebreak_for(final.year, games=query, league_view=league_view)
            for team_id in (1, 2):
                race = own_race(standings, {}, team_id, tiebreak)
                with self.subTest(year=final.year, team_id=team_id):
                    self.assertEqual(
                        race is not None and race.clinched,
                        final.champions[1] == team_id,
                        "進行中の判定と最終の優勝が食い違っている",
                    )
        self.assertEqual([dict(f.champions) for f in finals], [{1: 2}, {1: 2}])
