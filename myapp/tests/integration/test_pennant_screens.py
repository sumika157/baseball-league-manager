"""ペナントの世界の範囲の参照画面（P4a）。

誰でも開ける（未ログインを含む）・編集の導線が出ない・空の世界でも落ちない、を確かめる。
世界の外へリンクが出ないこと・他の世界の id が 404 になることは `test_world_isolation.py`。
"""

from datetime import date

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.exceptions import WorldNotFound
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoWorldSummaryQuery
from myapp.presentation.views import build_pennant_world_service, build_world_view_service

from .base import BaseCase
from .world_case import PENNANT_RIVAL, PENNANT_TEAM, PENNANT_WORLD, YEAR, WorldCase

# 書き込みの導線の目印（どれも世界の画面には出ない）
EDIT_MARKERS = ("記録を編集", "試合を登録", "新入団選手の登録", "/edit/", "/games/new/")


def create_empty_world(case: WorldCase) -> int:
    """試合がまだ無い世界を、実データのリーグから作る。"""
    created = build_pennant_world_service().create_world(
        name="空の世界", owner_id=None, source_league_ids=[case.league.id], start_year=YEAR + 1, seed=2
    )
    assert created.world.id is not None
    return created.world.id


class WorldScreensOpenTest(WorldCase):
    def test_every_screen_opens_without_login(self):
        urls = self.world_urls()
        self.assertGreaterEqual(len(urls), 13)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, follow=True).status_code, 200, url)

    def test_the_world_entry_is_the_gm_home(self):
        """世界の入口は GM ホーム（P4a の間は順位表への案内だった）。"""
        response = self.client.get(reverse("pennant_world", args=[self.world_id]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("GM ホーム", response.content.decode())

    def test_screens_show_the_world_not_the_real_data(self):
        standings = self.client.get(reverse("pennant_standings", args=[self.world_id])).content.decode()

        self.assertIn(PENNANT_TEAM, standings)
        self.assertNotIn("テストチーム", standings)

    def test_the_unknown_world_is_404(self):
        for name in ("pennant_world", "pennant_standings", "pennant_team_list", "pennant_game_list"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=[self.world_id + 100])).status_code, 404)


class WorldScreensAreReadOnlyTest(WorldCase):
    def _assert_no_edit_link(self):
        for url in self.world_urls():
            response = self.client.get(url, follow=True)
            self.assertEqual(response.status_code, 200, url)
            content = response.content.decode()
            for marker in EDIT_MARKERS:
                self.assertNotIn(marker, content, f"{url} に書き込みの導線「{marker}」が出ています")

    def test_no_edit_link_for_anonymous(self):
        self._assert_no_edit_link()

    def test_no_edit_link_even_for_a_staff_user(self):
        """実データなら編集できる管理ユーザーにも、世界の画面では出さない。"""
        self.client.force_login(User.objects.create_superuser("root", password="x"))

        self._assert_no_edit_link()

    def test_a_post_to_a_pennant_team_is_not_allowed(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        url = reverse("pennant_player_list", args=[self.world_id, self.pennant_team.id])
        before = orm_models.PlayerStint.objects.filter(team=self.pennant_team).count()

        response = self.client.post(url, {"name": "紛れ込み", "number": 77, "position": "内野手"})

        self.assertEqual(response.status_code, 405)
        self.assertEqual(orm_models.PlayerStint.objects.filter(team=self.pennant_team).count(), before)

    def test_the_game_detail_says_it_is_a_simulation(self):
        content = self.client.get(reverse("pennant_game_detail", args=[self.world_id, self.pennant_game_id]))
        real = self.client.get(reverse("game_detail", args=[self.real_game.id]))

        self.assertIn("シミュレーション", content.content.decode())
        self.assertNotIn("シミュレーション", real.content.decode())


class WorldBarTest(WorldCase):
    def test_the_bar_and_the_breadcrumb_start_from_the_world(self):
        content = self.client.get(reverse("pennant_standings", args=[self.world_id])).content.decode()

        self.assertIn(f'href="{reverse("pennant_index")}"', content)
        self.assertIn(PENNANT_WORLD, content)
        self.assertIn(f"{YEAR}年4月2日時点", content, "今日は最後に試合をした日")
        self.assertIn("シーズン終了", content, "試合を消化して、未消化の対戦が残っていない")
        self.assertNotIn("ダッシュボード", content, "パンくずの起点は世界")

    def test_the_bar_says_in_season_while_fixtures_remain(self):
        orm_models.PennantFixture.objects.create(
            date=date(YEAR, 4, 3), home_team=self.pennant_team, visitor_team=self.pennant_rival
        )

        content = self.client.get(reverse("pennant_standings", args=[self.world_id])).content.decode()

        self.assertIn("シーズン中", content)

    def test_the_bar_links_stay_in_the_world(self):
        content = self.client.get(reverse("pennant_team_list", args=[self.world_id])).content.decode()

        for name in ("pennant_standings", "pennant_game_list", "pennant_team_list"):
            self.assertIn(f'href="{reverse(name, args=[self.world_id])}"', content)

    def test_the_real_data_has_no_bar(self):
        content = self.client.get(reverse("standings")).content.decode()

        self.assertNotIn("ペナント</span>", content)
        self.assertIn("ダッシュボード", content)

    def test_before_the_first_game_the_bar_says_before_opening(self):
        content = self.client.get(reverse("pennant_standings", args=[create_empty_world(self)])).content.decode()

        self.assertIn("開幕前", content)
        self.assertNotIn("時点", content)


class OwnTeamMarkTest(WorldCase):
    def test_the_managed_team_is_marked_in_the_standings(self):
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(managed_team_id=self.pennant_team.id)

        content = self.client.get(reverse("pennant_standings", args=[self.world_id])).content.decode()

        self.assertEqual(content.count("自軍</span>"), 1, "受け持つ球団の行だけに印が付く")
        self.assertIn("fw-semibold", content)
        self.assertIn(f"自軍 {PENNANT_TEAM}", content, "世界バーにも受け持つ球団を出す")

    def test_without_a_managed_team_nothing_is_marked(self):
        content = self.client.get(reverse("pennant_standings", args=[self.world_id])).content.decode()

        self.assertNotIn("自軍", content)

    def test_the_real_standings_have_no_mark(self):
        self.assertNotIn("自軍", self.client.get(reverse("standings")).content.decode())


class EmptyWorldTest(WorldCase):
    def test_an_empty_world_does_not_crash(self):
        """試合がまだ無い世界でも、全画面が開く。空の状態の文言は世界向け。"""
        empty = create_empty_world(self)

        urls = self.world_urls(empty)
        self.assertGreaterEqual(len(urls), 12)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, follow=True).status_code, 200, url)
        standings = self.client.get(reverse("pennant_standings", args=[empty])).content.decode()
        self.assertIn("まだ試合が行われていません", standings)
        self.assertNotIn("管理画面の", standings, "実データ向けの案内を出さない")


class WorldAgeTest(WorldCase):
    def test_age_is_counted_on_the_worlds_date(self):
        """年齢は暦の今日ではなく、世界の今日（最後に試合をした 4月2日）で数える。"""
        player_id = self.pennant_players(self.pennant_team)[0]
        orm_models.Player.objects.filter(id=player_id).update(birth_date=date(YEAR - 25, 4, 3))

        listing = build_world_view_service(self.world_id).list_batters(self.pennant_team.id)

        row = next(row for row in listing.rows if row.id == player_id)
        self.assertEqual(row.age, 24, "誕生日の前日なので、世界の日付ではまだ24歳")


class WorldListTest(WorldCase):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("秘密のオーナー", password="x")
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(
            owner_id=self.owner.id, managed_team_id=self.pennant_team.id
        )

    def test_anyone_can_read_the_list_and_the_owner_is_not_shown(self):
        response = self.client.get(reverse("pennant_index"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(PENNANT_WORLD, content)
        self.assertIn(PENNANT_TEAM, content)
        self.assertNotIn("秘密のオーナー", content)
        self.assertIn("1位", content)
        self.assertIn("1-0-0", content)

    def test_the_create_form_is_for_a_logged_in_user_only(self):
        anonymous = self.client.get(reverse("pennant_index")).content.decode()
        self.client.force_login(self.owner)
        logged_in = self.client.get(reverse("pennant_index")).content.decode()

        self.assertNotIn('name="leagues"', anonymous)
        self.assertIn('name="leagues"', logged_in)

    def test_the_header_links_to_pennant_for_everyone(self):
        anonymous = self.client.get(reverse("standings")).content.decode()
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        staff = self.client.get(reverse("standings")).content.decode()

        self.assertIn(f'href="{reverse("pennant_index")}"', anonymous)
        self.assertLess(
            staff.index(reverse("pennant_index")), staff.index("管理画面"), "並びは「ペナント」→「管理画面」"
        )


class EmptyWorldListTest(BaseCase):
    def test_an_empty_list(self):
        content = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn("まだ世界がありません", content)


class WorldSummaryQueryTest(WorldCase):
    """世界の見出しは、世界の数にかかわらず一定のクエリ数で読む（世界ごとに読み直さない）。"""

    def test_the_summary_values(self):
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(managed_team_id=self.pennant_team.id)
        orm_models.PennantFixture.objects.create(
            date=date(YEAR, 4, 3), home_team=self.pennant_team, visitor_team=self.pennant_rival
        )

        summary = DjangoWorldSummaryQuery().get(self.world_id)

        self.assertEqual(summary.name, PENNANT_WORLD)
        self.assertEqual(summary.managed_team_name, PENNANT_TEAM)
        self.assertEqual(summary.default_league_id, self.pennant_league.id)
        self.assertEqual(summary.last_played_on, date(YEAR, 4, 2))
        self.assertTrue(summary.has_pending_fixtures)

    def test_other_worlds_and_real_data_are_not_picked_up(self):
        """実データと別の世界に、より遅い試合と未消化の対戦があっても、この世界の見出しには入らない。"""
        other = create_empty_world(self)
        other_team = orm_models.Team.objects.filter(league__world_id=other).order_by("id")
        late = date(YEAR, 9, 30)
        orm_models.Game.objects.create(
            year=YEAR, played_on=late, home_team=other_team[0], away_team=other_team[1], home_score=1, away_score=0
        )
        orm_models.PennantFixture.objects.create(date=late, home_team=other_team[0], visitor_team=other_team[1])
        orm_models.Game.objects.create(
            year=YEAR, played_on=late, home_team=self.team, away_team=self.rival, home_score=1, away_score=0
        )
        orm_models.PennantFixture.objects.create(date=late, home_team=self.team, visitor_team=self.rival)

        summary = DjangoWorldSummaryQuery().get(self.world_id)

        self.assertEqual(summary.last_played_on, date(YEAR, 4, 2))
        self.assertFalse(summary.has_pending_fixtures)
        listed = {s.world_id: s for s in DjangoWorldSummaryQuery().list_all()}
        self.assertEqual(listed[other].last_played_on, late)
        self.assertTrue(listed[other].has_pending_fixtures)

    def test_an_unknown_world_is_not_found(self):
        with self.assertRaises(WorldNotFound):
            DjangoWorldSummaryQuery().get(self.world_id + 100)

    def test_the_query_count_does_not_grow_with_the_number_of_worlds(self):
        def count() -> int:
            with CaptureQueriesContext(connection) as captured:
                DjangoWorldSummaryQuery().list_all()
            return len(captured)

        one = count()
        create_empty_world(self)
        create_empty_world(self)

        self.assertEqual(count(), one)
        self.assertEqual(len(DjangoWorldSummaryQuery().list_all()), 3)

    def test_the_list_screen_reads_only_the_managed_season(self):
        """自軍の順位は、その年の試合だけを読む（全シーズンの試合を組み立てない）。"""
        service = build_world_view_service(self.world_id)

        leagues = service.get_league_standings(YEAR)
        other_year = service.get_league_standings(YEAR + 5)

        self.assertEqual([row.team_name for row in leagues[0].rows], [PENNANT_TEAM, PENNANT_RIVAL])
        self.assertEqual(other_year, [])
