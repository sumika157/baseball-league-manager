"""ペナントの GM ホーム・進める・世界の作成と削除（P4b）。

読むのは誰でもでき、作成・進める・削除は書き込みなので権限を確かめる（未ログインはログインへ、
ほかの人は 403）。進める操作の核は、二重送信で進めすぎないこと（`expected_today`）と、
1回の上限（150試合）を守ること。世界は球団2つ（1リーグ）なので、1日に1試合だけ進む。
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.application.dto import LeagueStandings, OwnTeamSummary, StandingRow, WorldContext
from myapp.application.pennant_home import PennantHomeService
from myapp.domain.exceptions import AlreadyAdvanced, InvalidSchedule
from myapp.domain.pennant.schedule import AdvanceTarget, Fixture
from myapp.domain.pennant.season import SCREEN_ADVANCE_TARGETS, SeasonPhase, summary_period
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_season_service

from .test_pennant_advance import SEASON_GAMES, SeasonCase


class HomeCase(SeasonCase):
    """世界A（オーナーは `owner`）。受け持つ球団は「ホーム球団」。"""

    owner: User
    other: User

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user("owner", password="x")
        cls.other = User.objects.create_user("other", password="x")
        orm_models.PennantWorld.objects.filter(id=cls.world_a).update(owner_id=cls.owner.id)

    def home_url(self, world_id=None) -> str:
        return reverse("pennant_world", args=[self.world_a if world_id is None else world_id])

    def advance_url(self, world_id=None) -> str:
        return reverse("pennant_advance", args=[self.world_a if world_id is None else world_id])

    def post_advance(self, target, *, expected="", world_id=None):
        return self.client.post(self.advance_url(world_id), {"target": target, "expected_today": expected})

    def last_message(self, response) -> str:
        """直前の応答で出た通知。前の応答の通知が未読のまま残ることがあるので、最後の1件を見る。"""
        return [str(message) for message in get_messages(response.wsgi_request)][-1]

    def games_in(self, world_id=None) -> int:
        return orm_models.Game.objects.filter(home_team__league__world_id=world_id or self.world_a).count()

    def start_season(self) -> None:
        """日程だけ作る（まだ1試合も進めない）。"""
        build_pennant_season_service(self.world_a).ensure_schedule()


class HomeScreenTest(HomeCase):
    def test_anyone_can_read_the_home(self):
        response = self.client.get(self.home_url())

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("GM ホーム", content)
        self.assertIn("開幕日", content, "開幕前は開幕日を出す")

    def test_the_home_hides_write_links_from_anonymous_and_other_users(self):
        for user in (None, self.other):
            with self.subTest(user=user):
                self.client.logout()
                if user is not None:
                    self.client.force_login(user)
                content = self.client.get(self.home_url()).content.decode()
                self.assertNotIn(self.advance_url(), content)
                self.assertNotIn(reverse("pennant_delete", args=[self.world_a]), content)

    def test_the_owner_sees_the_advance_form_and_the_delete_link(self):
        self.start_season()
        self.client.force_login(self.owner)

        content = self.client.get(self.home_url()).content.decode()

        self.assertIn(f'action="{self.advance_url()}"', content)
        self.assertIn("1日進める", content)
        self.assertIn("1週間進める", content)
        self.assertIn(reverse("pennant_delete", args=[self.world_a]), content)

    def test_an_admin_who_is_not_the_owner_sees_nothing_to_write(self):
        """管理ユーザーの権限は世界に及ばない（世界は遊ぶ人のセーブデータ）。"""
        self.client.force_login(User.objects.create_superuser("root", password="x"))

        content = self.client.get(self.home_url()).content.decode()

        self.assertNotIn(self.advance_url(), content)

    def test_the_buttons_show_the_end_date_and_the_game_count(self):
        self.start_season()
        self.client.force_login(self.owner)

        content = self.client.get(self.home_url()).content.decode()

        self.assertRegex(content, r"\d+月\d+日（.）まで · 1試合", "1日（1試合）")
        self.assertRegex(content, r"\d+月\d+日（.）まで · 6試合", "1週間（月曜が休みなので6試合）")

    def test_without_a_schedule_only_the_first_day_is_offered(self):
        """日程の無い世界（コマンドで作った直後）は、日程が無いので終わる日も試合数も出せない。"""
        self.client.force_login(self.owner)

        content = self.client.get(self.home_url()).content.decode()

        self.assertIn("1日進める", content)
        self.assertNotIn("1週間進める", content)

    def test_the_next_games_and_the_standings_after_a_few_days(self):
        self.start_season()
        for _ in range(3):
            build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)

        content = self.client.get(self.home_url()).content.decode()

        self.assertIn("次の試合", content)
        self.assertIn("ライバル球団", content)
        self.assertEqual(content.count("自軍</span>"), 1, "順位表の自軍の行")
        self.assertIn("直近10試合", content)

    def test_the_finished_season_offers_nothing_to_advance(self):
        self.start_season()
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)
        self.client.force_login(self.owner)

        content = self.client.get(self.home_url()).content.decode()

        self.assertIn("シーズンは終了しました", content)
        self.assertIn("最終順位", content)
        self.assertNotIn("1日進める", content)

    def test_the_standings_switch_is_shown_only_when_there_are_several_leagues(self):
        self.start_season()
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)

        content = self.client.get(self.home_url()).content.decode()

        self.assertNotIn('name="league"', content, "リーグが1つなら切り替えは要らない")

    def test_a_world_without_a_managed_team_still_opens(self):
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=None)

        self.assertEqual(self.client.get(self.home_url()).status_code, 200)

    def test_an_unknown_world_is_404_for_every_pennant_write_screen(self):
        self.client.force_login(self.owner)
        missing = self.world_a + 1000

        for url in (
            self.home_url(missing),
            self.advance_url(missing),
            reverse("pennant_delete", args=[missing]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)


class AdvancePermissionTest(HomeCase):
    def test_a_get_goes_back_to_the_home_for_everyone(self):
        for user in (None, self.other, self.owner):
            with self.subTest(user=user):
                self.client.logout()
                if user is not None:
                    self.client.force_login(user)
                self.assertRedirects(self.client.get(self.advance_url()), self.home_url())
        self.assertEqual(self.games_in(), 0)

    def test_an_anonymous_post_goes_to_the_login(self):
        response = self.post_advance("day")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(self.games_in(), 0)

    def test_another_user_cannot_advance(self):
        self.client.force_login(self.other)

        self.assertEqual(self.post_advance("day").status_code, 403)
        self.assertEqual(self.games_in(), 0)

    def test_a_superuser_who_is_not_the_owner_cannot_advance(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))

        self.assertEqual(self.post_advance("day").status_code, 403)
        self.assertEqual(self.games_in(), 0)

    def test_a_world_without_an_owner_cannot_be_advanced_from_the_screen(self):
        """コマンドで作ってオーナーのいない世界は、だれも画面から進められない。"""
        self.client.force_login(self.owner)

        self.assertEqual(self.post_advance("day", world_id=self.world_b).status_code, 403)

    def test_another_world_is_not_advanced(self):
        self.client.force_login(self.owner)

        self.post_advance("day")

        self.assertEqual(self.games_in(self.world_b), 0)


class AdvanceTest(HomeCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_the_first_day_creates_the_schedule_and_returns_with_a_since(self):
        response = self.post_advance("day")

        self.assertEqual(self.games_in(), 1)
        today = orm_models.Game.objects.get(home_team__league__world_id=self.world_a).played_on
        since = (today - timedelta(days=1)).isoformat()
        self.assertRedirects(response, f"{self.home_url()}?since={since}", fetch_redirect_response=False)

    def test_the_result_is_shown_after_advancing_and_survives_a_reload(self):
        response = self.post_advance("day")
        url = response["Location"]

        first = self.client.get(url).content.decode()
        again = self.client.get(url).content.decode()

        for content in (first, again):
            self.assertIn("進めた結果", content)
            self.assertRegex(content, r"[○●△]")
            self.assertIn("linescore-table", content, "自軍の試合が1つだけならスコアボードを出す")

    def test_the_since_is_the_day_before_when_advancing_from_a_played_day(self):
        self.post_advance("day")
        today = build_pennant_season_service(self.world_a)._context_query.last_played_on()

        response = self.post_advance("day", expected=today.isoformat())

        self.assertEqual(response["Location"], f"{self.home_url()}?since={today.isoformat()}")
        self.assertEqual(self.games_in(), 2)

    def test_a_week_advances_the_games_of_seven_days(self):
        self.post_advance("day")
        today = build_pennant_season_service(self.world_a)._context_query.last_played_on()

        self.post_advance("week", expected=today.isoformat())

        self.assertEqual(self.games_in(), 7, "最初の1日と、1週間の6試合（月曜は休み）")

    def test_to_the_next_own_game(self):
        self.start_season()

        self.post_advance("next_managed_game")

        self.assertEqual(self.games_in(), 1, "自軍は毎日試合があるので、次の自軍の試合は次の日")

    def test_a_stale_expected_today_advances_nothing(self):
        """二重送信や、別の画面で先に進めたあとの送信では、さらに進めない。"""
        self.post_advance("day")
        self.assertEqual(self.games_in(), 1)

        response = self.post_advance("day", expected="")

        self.assertEqual(self.games_in(), 1)
        self.assertRedirects(response, self.home_url(), fetch_redirect_response=False)
        self.assertEqual(self.last_message(response), "既に進んでいます。最新の状態を表示しました。")

    def test_a_missing_expected_today_is_treated_as_stale(self):
        self.post_advance("day")

        response = self.client.post(self.advance_url(), {"target": "day"})

        self.assertEqual(self.games_in(), 1)
        self.assertEqual(self.last_message(response), "既に進んでいます。最新の状態を表示しました。")

    def test_the_same_form_sent_twice_advances_once(self):
        first = self.post_advance("day")
        second = self.post_advance("day")

        self.assertIn("since=", first["Location"])
        self.assertEqual(second["Location"], self.home_url())
        self.assertEqual(self.games_in(), 1)

    def test_targets_the_screen_does_not_offer_are_refused(self):
        for target in ("month_end", "season_end", "nonsense", ""):
            with self.subTest(target=target):
                response = self.post_advance(target)

                self.assertRedirects(response, self.home_url(), fetch_redirect_response=False)
                self.assertEqual(self.last_message(response), "その進め方はできません。")
        self.assertEqual(self.games_in(), 0)

    def test_a_finished_season_says_there_is_nothing_to_advance(self):
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)
        today = build_pennant_season_service(self.world_a)._context_query.last_played_on()
        before = self.games_in()

        response = self.post_advance("day", expected=today.isoformat())

        self.assertEqual(self.games_in(), before)
        self.assertEqual(self.last_message(response), "進める試合がありません。")

    def test_the_limit_on_one_advance_is_kept(self):
        """1回の上限を超える範囲は、何も作らずに断る（画面の応答が返らなくなる前に）。"""
        service = build_pennant_season_service(self.world_a)
        service.ensure_schedule()

        with self.assertRaises(InvalidSchedule) as raised:
            service.advance(AdvanceTarget.WEEK, max_games=3)

        self.assertIn("3試合まで", str(raised.exception))
        self.assertEqual(self.games_in(), 0)

    def test_the_view_passes_the_limit(self):
        with mock.patch("myapp.presentation.views.MAX_GAMES_PER_ADVANCE", 3):
            self.start_season()
            self.post_advance("day")  # 1試合は通る
            today = build_pennant_season_service(self.world_a)._context_query.last_played_on()
            response = self.post_advance("week", expected=today.isoformat())

        self.assertEqual(self.games_in(), 1)
        self.assertIn("3試合まで", self.last_message(response))

    def test_the_service_refuses_when_the_world_has_moved_on(self):
        """画面を開いたときの今日と違えば、何も作らずに止まる（照合は、今日を読んだ直後に service が行う）。"""
        service = build_pennant_season_service(self.world_a)
        service.advance(AdvanceTarget.DAY)
        today = service._context_query.last_played_on()

        for stale in (None, date(2000, 1, 1)):
            with self.subTest(expected=stale), self.assertRaises(AlreadyAdvanced) as raised:
                service.advance(AdvanceTarget.DAY, expected_today=stale)
            self.assertIn("既に進んでいます", str(raised.exception))
        self.assertEqual(self.games_in(), 1)
        service.advance(AdvanceTarget.DAY, expected_today=today)
        self.assertEqual(self.games_in(), 2)

    def test_the_service_accepts_none_as_a_world_without_games(self):
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY, expected_today=None)

        self.assertEqual(self.games_in(), 1)

    def test_the_commands_are_not_limited(self):
        """管理コマンドは上限を渡さない（シーズン終了まで通しで進められる）。"""
        report = build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)

        self.assertEqual(report.games, SEASON_GAMES)
        self.assertGreater(report.games, 100)


class AdvanceOptionsTest(SimpleTestCase):
    """進める選択肢（終わる日付と試合数）は、日程から導く。上限を超える範囲と重なる範囲は出さない。"""

    def fixtures(self, days: int, *, per_day: int = 1) -> list[Fixture]:
        start = date(2026, 4, 1)
        return [
            Fixture(date=start + timedelta(days=day), home_team_id=1 + n, visitor_team_id=100 + n)
            for day in range(days)
            for n in range(per_day)
        ]

    def options(self, fixtures, *, today=None, managed=1):
        world = WorldContext(
            world_id=1,
            name="世界",
            phase=SeasonPhase.IN_SEASON,
            season_year=2026,
            today=today,
            managed_team_id=managed,
            managed_team_name="自軍",
            default_league_id=None,
        )
        return PennantHomeService._advance_options(world, fixtures)

    def test_the_end_date_and_games_come_from_the_schedule(self):
        options = self.options(self.fixtures(10, per_day=2), today=date(2026, 3, 31))

        by_target = {option.target: option for option in options}
        self.assertEqual((by_target["day"].end_date, by_target["day"].games), (date(2026, 4, 1), 2))
        self.assertEqual((by_target["week"].end_date, by_target["week"].games), (date(2026, 4, 7), 14))

    def test_a_range_over_the_limit_is_not_offered(self):
        with mock.patch("myapp.application.pennant_home.MAX_GAMES_PER_ADVANCE", 10):
            options = self.options(self.fixtures(10, per_day=2), today=date(2026, 3, 31))

        self.assertEqual([option.target for option in options], ["day"], "1週間は14試合で上限（10）を超える")

    def test_a_range_equal_to_another_is_offered_once(self):
        """自軍が毎日試合をするなら、「次の自軍の試合まで」は「1日」と同じ範囲になる。"""
        options = self.options(self.fixtures(10), today=date(2026, 3, 31))

        self.assertEqual([option.target for option in options], ["day", "week"])

    def test_the_next_own_game_is_offered_when_it_differs_from_a_day(self):
        fixtures = [
            Fixture(date=date(2026, 4, 1), home_team_id=2, visitor_team_id=3),
            Fixture(date=date(2026, 4, 2), home_team_id=1, visitor_team_id=3),
        ]

        options = self.options(fixtures, today=date(2026, 3, 31))

        self.assertEqual(
            [option.target for option in options], ["day", "next_managed_game"], "1週間は次の自軍の試合と同じ範囲"
        )

    def test_only_screen_targets_are_offered_in_their_order(self):
        """画面に出す範囲と順序は domain の SCREEN_ADVANCE_TARGETS が出典（ここに別の並びを持たない）。"""
        fixtures = [
            Fixture(date=date(2026, 4, 1), home_team_id=2, visitor_team_id=3),
            *self.fixtures(10, per_day=1)[1:],
        ]

        offered = [AdvanceTarget(option.target) for option in self.options(fixtures, today=date(2026, 3, 31))]

        self.assertTrue(offered)
        self.assertEqual(offered, [t for t in SCREEN_ADVANCE_TARGETS if t in offered])

    def test_nothing_is_offered_after_the_season(self):
        self.assertEqual(self.options([], today=date(2026, 10, 1)), [])


class SummaryTest(HomeCase):
    def advance_days(self, count: int) -> date:
        """日程を作って `count` 日進める。進めたあとの今日（最後の試合日）を返す。"""
        service = build_pennant_season_service(self.world_a)
        service.ensure_schedule()
        for _ in range(count):
            service.advance(AdvanceTarget.DAY)
        today = service._context_query.last_played_on()
        assert today is not None  # mypy 用（1日以上進めたので試合日がある）
        return today

    def played_dates(self) -> list[date]:
        """世界Aの試合日（古い順）。月曜は休みなので、日数ではなく実際の試合日で期間を決める。"""
        return sorted(
            orm_models.Game.objects.filter(home_team__league__world_id=self.world_a).values_list(
                "played_on", flat=True
            )
        )

    def test_a_since_that_cannot_be_read_shows_no_summary(self):
        today = self.advance_days(2)

        for since in ("abc", "2026-13-40", "", (today + timedelta(days=1)).isoformat(), today.isoformat()):
            with self.subTest(since=since):
                response = self.client.get(f"{self.home_url()}?since={since}")

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("進めた結果", response.content.decode())

    def test_a_valid_since_shows_the_games_after_it(self):
        self.advance_days(3)
        since = self.played_dates()[0]

        content = self.client.get(f"{self.home_url()}?since={since.isoformat()}").content.decode()

        self.assertIn("進めた結果", content)
        self.assertNotIn("linescore-table", content, "自軍の試合が複数のときはスコアボードを出さない")

    def test_the_summary_period_is_rounded_to_a_month(self):
        today = date(2026, 6, 30)

        self.assertEqual(summary_period(date(2026, 3, 1), today), (date(2026, 5, 30), today))
        self.assertEqual(summary_period(date(2026, 6, 20), today), (date(2026, 6, 20), today))
        self.assertIsNone(summary_period(None, today))
        self.assertIsNone(summary_period(today, today))
        self.assertIsNone(summary_period(today + timedelta(days=1), today))
        self.assertIsNone(summary_period(date(2026, 6, 1), None))

    def test_the_summary_is_read_from_the_games_of_the_period(self):
        self.advance_days(4)
        home = self._home(since=self.played_dates()[1])

        summary = home.summary
        self.assertIsNotNone(summary)
        self.assertEqual(len(summary.own_games), 2)
        self.assertEqual(summary.wins + summary.losses + summary.ties, 2)
        self.assertIsNotNone(summary.rank_before, "期間の前にも試合があるので、前の順位が出る")
        self.assertIsNotNone(summary.rank_after)
        self.assertEqual(
            [game.played_on for game in summary.own_games],
            sorted(game.played_on for game in summary.own_games),
            "自軍の試合は日付の古い順",
        )

    def test_the_first_advance_has_no_rank_before(self):
        today = self.advance_days(1)

        summary = self._home(since=today - timedelta(days=1)).summary

        self.assertIsNotNone(summary)
        self.assertIsNone(summary.rank_before)
        self.assertEqual(len(summary.own_games), 1)
        self.assertIsNotNone(summary.line_score)

    def test_the_home_reads_a_fixed_number_of_queries(self):
        """世界の全試合を毎回集約として読まない。試合数が増えてもクエリ数は同じ。"""
        self.advance_days(2)
        self.client.get(self.home_url())

        def count() -> int:
            with CaptureQueriesContext(connection) as captured:
                self.client.get(self.home_url())
            return len(captured)

        short = count()
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.WEEK)
        self.assertEqual(count(), short)

    def _home(self, *, since):
        from myapp.presentation.views import build_pennant_home_service, build_pennant_world_view

        context = build_pennant_world_view().get_context(self.world_a)
        return build_pennant_home_service(self.world_a).get_home(context, since=since, include_advance=False)


class StandingsSwitchTest(SimpleTestCase):
    def standings(self):
        def rows(*team_ids):
            return [
                StandingRow(
                    rank=index,
                    team_id=team_id,
                    team_name=f"球団{team_id}",
                    wins=1,
                    losses=0,
                    ties=0,
                    games_played=1,
                    winning_percentage="1.000",
                    games_behind="—",
                )
                for index, team_id in enumerate(team_ids, start=1)
            ]

        return [
            LeagueStandings(league_id=1, league_name="セ", rows=rows(1, 2)),
            LeagueStandings(league_id=2, league_name="パ", rows=rows(3, 4)),
            LeagueStandings(league_id=3, league_name="地", rows=rows(5)),
        ]

    def test_the_own_league_comes_first(self):
        result = PennantHomeService._home_standings(
            self.standings(), 2026, own_league_id=2, requested=None, default_league_id=1
        )

        self.assertEqual([league.name for league in result.leagues], ["パ", "セ", "地"])
        self.assertEqual(result.selected.league_name, "パ")

    def test_a_requested_league_is_shown(self):
        result = PennantHomeService._home_standings(
            self.standings(), 2026, own_league_id=2, requested=3, default_league_id=1
        )

        self.assertEqual(result.selected.league_name, "地")

    def test_an_unknown_league_falls_back_to_the_own_league(self):
        result = PennantHomeService._home_standings(
            self.standings(), 2026, own_league_id=2, requested=99, default_league_id=1
        )

        self.assertEqual(result.selected.league_name, "パ")

    def test_without_games_nothing_is_selected(self):
        result = PennantHomeService._home_standings([], 2026, own_league_id=2, requested=None, default_league_id=1)

        self.assertEqual((result.leagues, result.selected), ([], None))


class OwnTeamLeaderTest(SimpleTestCase):
    """首位の判定は順位（1位）から。表示用の「ゲーム差」の表記（「—」）には頼らない（再発防止）。"""

    def summary(self, *, rank, games_behind):
        return OwnTeamSummary(
            team_id=1,
            team_name="ホーム球団",
            league_name="進行リーグ",
            phase=SeasonPhase.IN_SEASON,
            rank=rank,
            wins=3,
            losses=2,
            ties=0,
            winning_percentage=".600",
            games_behind=games_behind,
            games_played=5,
            remaining_games=138,
            last_ten="3勝2敗",
            streak="1連勝",
            opening_date=None,
        )

    def test_the_first_place_is_the_leader_whatever_the_games_behind_notation(self):
        self.assertTrue(self.summary(rank=1, games_behind="-").is_leader)
        self.assertFalse(self.summary(rank=2, games_behind="—").is_leader)
        self.assertFalse(self.summary(rank=None, games_behind="—").is_leader, "試合が無ければ順位は付かない")
