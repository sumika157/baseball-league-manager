"""ダッシュボード（ホーム画面）。全リーグの最新シーズンの要約。

役割はリーグ画面（1リーグを掘り下げる）・タイトル画面（部門の上位3人と過去の年）と分けている。
ここには全リーグの順位表と主な部門の首位、最新の試合日の結果だけを出す。
"""

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


def _leader(league, key):
    """そのリーグの部門の首位（RankingEntry）。部門が無ければ None。"""
    department = next((d for d in league.leaders if d.key == key), None)
    return department.leader if department else None


class DashboardLeadersTest(BaseCase):
    """部門の首位。出典はタイトル画面と同じシーズン集計。"""

    def test_leaders_are_the_main_departments_only(self):
        """主な6部門だけ。残りの部門はタイトル画面が受け持つ。"""
        play_game(self.team, self.rival)

        league = self.service.get_dashboard().leagues[0]

        self.assertEqual(
            [d.key for d in league.leaders],
            ["average", "home_runs", "rbi", "era", "wins", "saves"],
        )
        # 部門名はタイトル画面と同じ
        self.assertEqual(
            [d.label for d in league.leaders],
            ["首位打者", "本塁打王", "打点王", "最優秀防御率", "最多勝利", "最多セーブ"],
        )

    def test_only_the_leader_is_shown(self):
        a = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        b = self.roster.register_player(self.rival.id, "田中", 10, "外野手")
        play_game(
            self.team,
            self.rival,
            batting={
                a.id: BattingLine(at_bats=10, home_runs=1),
                b.id: BattingLine(at_bats=10, home_runs=4),
            },
        )

        league = self.service.get_dashboard().leagues[0]
        home_runs = next(d for d in league.leaders if d.key == "home_runs")

        self.assertEqual(
            [(e.player_name, e.team_name, e.value) for e in home_runs.entries], [("田中", "相手チーム", "4")]
        )

    def test_tied_leaders_are_all_shown(self):
        """同率の首位は全員出す。1人で切ると、タイトル画面（同率を並べる）と見え方が食い違う。"""
        a = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        b = self.roster.register_player(self.rival.id, "田中", 10, "外野手")
        c = self.roster.register_player(self.rival.id, "鈴木", 11, "外野手")
        play_game(
            self.team,
            self.rival,
            batting={
                a.id: BattingLine(at_bats=10, home_runs=3),
                b.id: BattingLine(at_bats=10, home_runs=3),
                c.id: BattingLine(at_bats=10, home_runs=1),
            },
        )

        league = self.service.get_dashboard().leagues[0]
        home_runs = next(d for d in league.leaders if d.key == "home_runs")

        self.assertEqual(sorted(e.player_name for e in home_runs.entries), ["山田", "田中"])
        body = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("山田", body)
        self.assertIn("田中", body)

    def test_leaders_count_only_the_latest_season(self):
        """通算ではなく最新シーズン。通算で出すと、タイトル画面と同じ部門名で別の数字になる。"""
        veteran = self.roster.register_player(self.team.id, "ベテラン", 3, "内野手")
        rookie = self.roster.register_player(self.rival.id, "若手", 5, "内野手")
        give_batting(self.team, self.rival, veteran.id, BattingLine(at_bats=40, home_runs=30), year=2025)
        give_batting(self.team, self.rival, veteran.id, BattingLine(at_bats=4, home_runs=1), year=2026)
        give_batting(self.team, self.rival, rookie.id, BattingLine(at_bats=4, home_runs=2), year=2026, day=2)

        league = self.service.get_dashboard().leagues[0]

        self.assertEqual(_leader(league, "home_runs").player_name, "若手")
        self.assertEqual(_leader(league, "home_runs").value, "2")

    def test_leaders_match_the_titles_page(self):
        """同じリーグ・同じシーズン・同じ部門の首位は、タイトル画面の1位と一致する（出典がずれたら落ちる）。"""
        slugger = self.roster.register_player(self.team.id, "大砲", 3, "内野手")
        ace = self.roster.register_player(self.team.id, "エース", 18, "投手")
        closer = self.roster.register_player(self.rival.id, "守護神", 22, "投手")
        for day in range(1, 4):
            play_game(
                self.team,
                self.rival,
                day=day,
                batting={slugger.id: BattingLine(at_bats=4, singles=2, home_runs=1, runs_batted_in=2)},
                pitching={
                    ace.id: PitchingLine(innings=InningsPitched.from_notation("8.0"), wins=1),
                    closer.id: PitchingLine(innings=InningsPitched.from_notation("1.0"), saves=1),
                },
            )

        league = self.service.get_dashboard().leagues[0]
        titles = {d.key: d for d in self.service.get_league_titles(self.league.id).departments}

        for department in league.leaders:
            with self.subTest(key=department.key):
                expected = titles[department.key].leader
                self.assertEqual(department.leader, expected)

    def test_leaders_count_interleague_games_like_the_titles_page(self):
        """交流戦も数える（タイトル画面と同じ）。成績にも、率の部門の規定の基準になる試合数にも。

        自軍はリーグ内1試合・交流戦2試合で3試合。規定打席 9.3・規定投球回 3 になり、
        リーグ内の1試合だけに出た選手（4打席・2回）は規定に届かない。試合数をリーグ内の
        対戦だけで数えると（1試合）この選手が規定に届いて首位になり、タイトル画面とずれる。
        """
        other = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other, name="Xチーム")
        regular = self.roster.register_player(self.team.id, "常連", 3, "内野手")
        spot = self.roster.register_player(self.team.id, "代打", 4, "内野手")
        starter = self.roster.register_player(self.team.id, "先発", 18, "投手")
        spot_pitcher = self.roster.register_player(self.team.id, "谷間", 19, "投手")
        play_game(
            self.team,
            self.rival,
            day=1,
            batting={
                regular.id: BattingLine(at_bats=4, singles=2),
                spot.id: BattingLine(at_bats=4, singles=4),
            },
            pitching={
                starter.id: PitchingLine(innings=InningsPitched.from_notation("7.0"), earned_runs=1),
                spot_pitcher.id: PitchingLine(innings=InningsPitched.from_notation("2.0")),
            },
        )
        for day in (2, 3):
            play_game(
                self.team,
                x,
                day=day,
                batting={regular.id: BattingLine(at_bats=4, singles=2)},
                pitching={starter.id: PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=1)},
            )

        league = next(g for g in self.service.get_dashboard().leagues if g.league_id == self.league.id)
        titles = {d.key: d for d in self.service.get_league_titles(self.league.id).departments}

        self.assertEqual(_leader(league, "average").player_name, "常連")
        self.assertEqual(_leader(league, "era").player_name, "先発")
        for department in league.leaders:
            with self.subTest(key=department.key):
                self.assertEqual(department.leader, titles[department.key].leader)

    def test_leaders_are_split_by_league(self):
        """タイトルはリーグの中で争われる。他リーグの選手と比べない。"""
        other = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other, name="Xチーム")
        y = orm_models.Team.objects.create(league=other, name="Yチーム")
        here = self.roster.register_player(self.team.id, "当リーグ", 10, "内野手")
        there = self.roster.register_player(x.id, "別リーグ選手", 10, "内野手")
        play_game(self.team, self.rival, batting={here.id: BattingLine(at_bats=10, home_runs=1)})
        play_game(x, y, batting={there.id: BattingLine(at_bats=10, home_runs=5)})

        leagues = {g.league_name: g for g in self.service.get_dashboard().leagues}

        self.assertEqual(_leader(leagues["テストリーグ"], "home_runs").player_name, "当リーグ")
        self.assertEqual(_leader(leagues["別リーグ"], "home_runs").player_name, "別リーグ選手")

    def test_league_without_games_has_no_leaders(self):
        league = self.service.get_dashboard().leagues[0]

        self.assertEqual(league.leaders, [])
        self.assertFalse(league.has_leaders)

    def test_leader_names_link_to_the_player_page(self):
        """選手名の行き先を、選手一覧・選手検索とそろえる（編集画面へは送らない）。"""
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        play_game(self.team, self.rival, batting={player.id: BattingLine(at_bats=10, home_runs=3)})

        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn(reverse("player_detail", args=[self.team.id, player.id]), body)
        self.assertNotIn(reverse("player_edit", args=[self.team.id, player.id]), body)

    def test_each_card_links_to_the_titles_and_the_league_page(self):
        """上位3人・残りの部門はタイトル画面、1リーグの掘り下げはリーグ画面へ送る。"""
        play_game(self.team, self.rival, year=2026)

        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn(reverse("league_titles_by_year", args=[self.league.id, 2026]), body)
        self.assertIn(reverse("league_detail", args=[self.league.id]), body)


class DashboardLeaguesTest(BaseCase):
    """全リーグを1画面に並べる。"""

    def test_every_league_is_shown_at_once(self):
        """タブで切り替えない（切り替えた先がリーグ画面の上半分と同じになるため）。"""
        other = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other, name="Xチーム")
        y = orm_models.Team.objects.create(league=other, name="Yチーム")
        play_game(self.team, self.rival)
        play_game(x, y)

        body = self.client.get(reverse("dashboard")).content.decode()

        for name in ("テストチーム", "相手チーム", "Xチーム", "Yチーム"):
            with self.subTest(team=name):
                self.assertIn(name, body)
        self.assertNotIn('role="tablist"', body)

    def test_teams_are_grouped_by_league(self):
        other = orm_models.League.objects.create(name="別リーグ")
        orm_models.Team.objects.create(league=other, name="Xチーム")

        board = self.service.get_dashboard()

        self.assertEqual(
            {g.league_name: [t.name for t in g.teams] for g in board.leagues},
            {"テストリーグ": ["テストチーム", "相手チーム"], "別リーグ": ["Xチーム"]},
        )

    def test_flat_team_list_is_still_available(self):
        self.assertEqual(len(self.service.get_dashboard().teams), 2)

    def test_leagues_without_teams_are_omitted(self):
        orm_models.League.objects.create(name="空リーグ")
        board = self.service.get_dashboard()

        self.assertNotIn("空リーグ", [g.league_name for g in board.leagues])

    def test_counts_are_not_shown(self):
        """登録数（リーグ・チーム・選手）は出さない。規模は管理画面の「データの概況」にある。"""
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertNotIn("stat-strip", body)

    def test_standings_show_the_latest_season(self):
        """順位表は最新シーズンのもの。年をさかのぼるのは順位表・リーグ画面が受け持つ。"""
        play_game(self.team, self.rival, home_score=5, away_score=3, year=2025)
        play_game(self.team, self.rival, home_score=2, away_score=7, year=2026)

        league = self.service.get_dashboard().leagues[0]

        self.assertEqual(league.standings_year, 2026)
        self.assertEqual([r.team_name for r in league.standings], ["相手チーム", "テストチーム"])

    def test_standings_replace_the_team_list(self):
        """順位表があるリーグではチームの一覧を出さない（同じチームの並びが二重になる）。"""
        play_game(self.team, self.rival)
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn("<th>順位</th>", body)
        # チームの一覧に差し替わっていない（在籍人数はそこにしか出ない）
        self.assertNotIn("tile-list-meta", body)

    def test_team_list_stands_in_when_no_game_has_been_played(self):
        """1試合も無いリーグでは順位表が作れないので、チームの一覧に差し替える。

        登録したばかりのチームがホームのどこにも出ない状態を作らないため。
        """
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn("tile-list-meta", body)
        self.assertIn("テストチーム", body)
        self.assertIn("まだ試合が行われていません。", body)

    def test_page_renders_without_any_data(self):
        orm_models.Team.objects.all().delete()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)


class DashboardLatestResultsTest(BaseCase):
    """全リーグの、最新の試合日の結果。"""

    def test_only_the_latest_day_is_shown(self):
        play_game(self.team, self.rival, day=1)
        play_game(self.rival, self.team, day=3)
        play_game(self.team, self.rival, day=2)

        results = self.service.get_dashboard().latest_results

        self.assertEqual([g.played_on.day for g in results], [3])

    def test_every_league_of_that_day_is_shown(self):
        """件数で切らない。同じ日の一部のリーグだけが残る形にしない。"""
        other = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other, name="Xチーム")
        y = orm_models.Team.objects.create(league=other, name="Yチーム")
        for _ in range(4):
            play_game(self.team, self.rival, day=5)
            play_game(x, y, day=5)

        results = self.service.get_dashboard().latest_results

        self.assertEqual(len(results), 8)
        # リーグの並び順（同じリーグの試合がまとまる）
        self.assertEqual(
            [r.home_team_name for r in results],
            ["テストチーム"] * 4 + ["Xチーム"] * 4,
        )

    def test_an_unrecorded_game_does_not_push_out_the_results(self):
        """登録しただけの先の日付の試合で、行われた試合の結果が消えない。"""
        play_game(self.team, self.rival, home_score=4, away_score=1, day=1)
        play_game(self.team, self.rival, day=9, recorded=False)

        results = self.service.get_dashboard().latest_results

        self.assertEqual([(r.played_on.day, r.home_score, r.away_score) for r in results], [(1, 4, 1)])

    def test_results_link_to_each_game_and_the_game_list(self):
        game = play_game(self.team, self.rival)
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn("の試合結果", body)
        self.assertIn(reverse("game_detail", args=[game.id]), body)
        self.assertIn(reverse("game_list"), body)

    def test_no_results_card_without_games(self):
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertNotIn("の試合結果", body)


class DashboardQueriesTest(BaseCase):
    def test_does_not_read_game_details(self):
        """順位表と結果は得点だけで決まる。明細まで読むと試合数ぶん重くなる。

        試合を増やしてもクエリ数が変わらないことで、明細を読んでいないと分かる。
        """
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        play_game(self.team, self.rival, day=1, batting={player.id: BattingLine(at_bats=4, singles=2)})

        with CaptureQueriesContext(connection) as first:
            self.client.get(reverse("dashboard"))

        for day in range(2, 12):
            play_game(self.team, self.rival, day=day, batting={player.id: BattingLine(at_bats=4, singles=1)})

        with CaptureQueriesContext(connection) as grown:
            self.client.get(reverse("dashboard"))

        self.assertEqual(len(grown.captured_queries), len(first.captured_queries))
