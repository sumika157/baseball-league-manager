"""ペナントの開幕年の範囲と、年齢を数えられない選手（#103）。

開幕年が選手の生年月日より前だと、試合の無い世界の「今日」（開幕日）で年齢が数えられず、
球団の画面・GM ホーム・選手ページ・能力の表が 500 になっていた。
範囲は domain の `World` と `earliest_start_year()` が決め、フォームの min/max も同じ定数から作る。
"""

from datetime import date

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.exceptions import InvalidWorld
from myapp.domain.pennant.world import MAX_START_YEAR
from myapp.domain.value_objects import Season
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_world_service

from .world_case import YEAR, WorldCase


class StartYearRangeTest(WorldCase):
    def create(self, start_year: int):
        return build_pennant_world_service().create_world(
            name="範囲の世界", owner_id=None, source_league_ids=[self.league.id], start_year=start_year, seed=3
        )

    def test_a_year_before_the_players_were_born_is_rejected_and_nothing_is_created(self):
        orm_models.Player.objects.filter(stints__team=self.team).update(birth_date=date(2000, 6, 1))
        before = orm_models.PennantWorld.objects.count()

        with self.assertRaises(InvalidWorld) as raised:
            self.create(2000)

        self.assertIn("2001年以降", str(raised.exception))
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)
        self.create(2001)  # 境界の年は作れる

    def test_a_year_after_the_latest_is_rejected(self):
        with self.assertRaises(InvalidWorld) as raised:
            self.create(MAX_START_YEAR + 1)

        self.assertIn(f"{MAX_START_YEAR}年まで", str(raised.exception))

    def test_the_list_opens_even_with_a_saved_world_after_the_latest_year(self):
        """上限は作るときだけ検査する。上限を超えた行があっても、世界の一覧は誰にでも開ける。"""
        orm_models.PennantWorld.objects.create(name="遅い世界", seed=1, start_year=Season.MAX_YEAR)

        response = self.client.get(reverse("pennant_index"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("遅い世界", response.content.decode())

    def test_an_early_year_posted_through_the_form_shows_the_earliest_year(self):
        self.client.force_login(User.objects.create_user("owner", password="x"))
        orm_models.Player.objects.filter(stints__team=self.team).update(birth_date=date(2000, 6, 1))
        before = orm_models.PennantWorld.objects.count()
        data = {
            "name": "早い",
            "leagues": [str(self.league.id)],
            "managed_team": str(self.team.id),
            "start_year": "2000",
            "seed": "1",
        }

        response = self.client.post(reverse("pennant_index"), data)

        self.assertEqual(response.status_code, 200)
        self.assertIn("2001年以降にしてください", response.content.decode())
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_real_data_screens_open_with_a_player_born_in_the_future(self):
        orm_models.Player.objects.filter(stints__team=self.team).update(birth_date=date(YEAR + 30, 1, 1))
        stint = orm_models.PlayerStint.objects.filter(team=self.team).first()
        assert stint is not None

        for url in (
            reverse("player_list", args=[self.team.id]),
            reverse("player_detail", args=[self.team.id, stint.player_id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_the_form_takes_its_range_from_the_domain(self):
        self.client.force_login(User.objects.create_user("owner", password="x"))

        content = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn(f'min="{Season.MIN_YEAR}"', content)
        self.assertIn(f'max="{MAX_START_YEAR}"', content)

    def test_a_year_out_of_range_posted_through_the_form_creates_nothing(self):
        self.client.force_login(User.objects.create_user("owner", password="x"))
        before = orm_models.PennantWorld.objects.count()
        data = {
            "name": "範囲外",
            "leagues": [str(self.league.id)],
            "managed_team": str(self.team.id),
            "seed": "1",
        }

        for year in (Season.MIN_YEAR - 1, MAX_START_YEAR + 1):
            with self.subTest(year=year):
                response = self.client.post(reverse("pennant_index"), {**data, "start_year": str(year)})

                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "以下でなければなりません" if year > MAX_START_YEAR else "以上でなければなりません",
                    response.content.decode(),
                )
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_a_world_at_the_latest_year_opens_every_screen(self):
        created = self.create(MAX_START_YEAR)
        assert created.world.id is not None

        for url in (
            reverse("pennant_world", args=[created.world.id]),
            reverse("pennant_club", args=[created.world.id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200, url)


class UncountableAgeTest(WorldCase):
    def test_screens_do_not_fail_when_a_player_is_born_after_the_world_date(self):
        orm_models.Player.objects.filter(stints__team__league__world_id=self.world_id).update(
            birth_date=date(YEAR + 20, 1, 1)
        )

        for url in self.world_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, follow=True).status_code, 200, url)

    def test_screens_do_not_fail_in_a_world_that_has_not_played_a_game(self):
        """試合の無い世界の「今日」は開幕日。生年月日より前の年に開幕した世界を再現する。"""
        created = build_pennant_world_service().create_world(
            name="空", owner_id=None, source_league_ids=[self.league.id], start_year=YEAR, seed=4
        )
        world_id = created.world.id
        assert world_id is not None
        orm_models.Player.objects.filter(stints__team__league__world_id=world_id).update(
            birth_date=date(YEAR + 5, 1, 1)
        )
        team = orm_models.Team.objects.filter(league__world_id=world_id).order_by("id").first()
        assert team is not None

        for url in (
            reverse("pennant_world", args=[world_id]),
            reverse("pennant_club", args=[world_id]),
            reverse("pennant_player_list", args=[world_id, team.id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, follow=True).status_code, 200, url)
