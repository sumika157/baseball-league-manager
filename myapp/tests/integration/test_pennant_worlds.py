"""ペナントの世界の作成・一覧・削除（P4b）。

作成と削除は書き込みなので権限を確かめる（未ログインはログインへ、ほかの人は 403）。
上限（1人の世界の数・元にするリーグの数）と、フォームの検証エラー（日本語）も。
GM ホームと進める操作は `test_pennant_home.py`。
"""

from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.urls import reverse

from myapp.application.pennant_season import PennantSeasonService
from myapp.domain.exceptions import InvalidSchedule, InvalidWorld
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import MAX_WORLDS_PER_OWNER
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_season_service, build_pennant_world_service, build_service

from ..helpers import play_game
from .test_pennant_advance import register_club
from .test_pennant_home import HomeCase


class CreateWorldTest(HomeCase):
    def form(self, **overrides) -> dict:
        data = {
            "name": "新しい世界",
            "leagues": [str(self.league_id)],
            "managed_team": str(self.home.id),
            "start_year": "2040",
            "seed": "5",
        }
        data.update(overrides)
        return data

    def test_the_list_is_open_to_anyone_and_has_no_form_for_anonymous(self):
        content = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn("世界A", content)
        self.assertNotIn('name="leagues"', content)

    def test_the_form_is_shown_to_a_logged_in_user(self):
        self.client.force_login(self.other)

        content = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn('name="leagues"', content)
        self.assertIn("<optgroup", content, "受け持つ球団はリーグごとの optgroup")
        self.assertIn("作成に数秒かかります", content)
        self.assertIn("<details", content, "シードは詳細設定の中")

    def test_the_default_year_is_the_year_after_the_latest_real_season(self):
        play_game(self.home, self.rival, home_score=1, away_score=0, year=2031)
        self.client.force_login(self.other)

        content = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn('name="start_year" value="2032"', content)

    def test_an_anonymous_post_creates_nothing(self):
        before = orm_models.PennantWorld.objects.count()

        response = self.client.post(reverse("pennant_index"), self.form())

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_a_valid_post_creates_the_world_and_goes_to_its_home(self):
        self.client.force_login(self.other)

        response = self.client.post(reverse("pennant_index"), self.form())

        world = orm_models.PennantWorld.objects.get(name="新しい世界")
        self.assertRedirects(response, reverse("pennant_world", args=[world.id]), fetch_redirect_response=False)
        self.assertEqual(world.owner_id, self.other.id)
        self.assertEqual(world.seed, 5)
        self.assertEqual(world.start_year, 2040)
        self.assertEqual(world.managed_team.name, self.home.name)
        self.assertTrue(
            orm_models.PennantFixture.objects.filter(home_team__league__world=world).exists(), "日程も作る"
        )

    def test_the_new_world_home_opens_for_the_creator_with_the_advance_form(self):
        self.client.force_login(self.other)
        response = self.client.post(reverse("pennant_index"), self.form(), follow=True)

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("新しい世界", content)
        self.assertIn("1日進める", content)
        self.assertRegex(content, r"\d+月\d+日（.）まで · 1試合")

    def test_without_a_seed_one_is_chosen(self):
        self.client.force_login(self.other)

        self.client.post(reverse("pennant_index"), self.form(seed=""))

        self.assertTrue(orm_models.PennantWorld.objects.filter(name="新しい世界").exists())

    def test_the_form_errors_are_shown_in_japanese_and_nothing_is_created(self):
        self.client.force_login(self.other)
        before = orm_models.PennantWorld.objects.count()
        other_league = orm_models.League.objects.create(name="別リーグ")
        other_team = orm_models.Team.objects.create(league=other_league, name="別球団")
        register_club(build_service(), other_team, "別")
        cases = {
            "no league": (self.form(leagues=[]), "このフィールドは必須です。"),
            "no team": (self.form(managed_team=""), "このフィールドは必須です。"),
            "team outside the leagues": (
                self.form(managed_team=str(other_team.id)),
                "受け持つ球団は、選んだリーグの球団から選んでください。",
            ),
            "no name": (self.form(name=" "), "このフィールドは必須です。"),
            "bad year": (self.form(start_year="abc"), "整数を入力してください。"),
            "bad seed": (self.form(seed="-1"), "この値は 0 以上でなければなりません。"),
        }
        for label, (data, error) in cases.items():
            with self.subTest(label):
                response = self.client.post(reverse("pennant_index"), data)

                self.assertEqual(response.status_code, 200)
                self.assertIn(error, response.content.decode())
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_the_entered_values_stay_in_the_form_on_an_error(self):
        self.client.force_login(self.other)

        content = self.client.post(reverse("pennant_index"), self.form(managed_team="")).content.decode()

        self.assertIn('value="新しい世界"', content)
        self.assertIn('value="2040"', content)

    def test_more_than_eight_leagues_are_refused(self):
        with self.assertRaises(InvalidWorld) as raised:
            build_pennant_world_service().create_world(
                name="多すぎ", owner_id=None, source_league_ids=list(range(1, 10)), start_year=2040, seed=1
            )

        self.assertIn("8つまで", str(raised.exception))

    def test_the_number_of_worlds_per_owner_is_limited(self):
        self.client.force_login(self.other)
        for number in range(MAX_WORLDS_PER_OWNER):
            response = self.client.post(reverse("pennant_index"), self.form(name=f"世界{number}"))
            self.assertEqual(response.status_code, 302, number)

        content = self.client.get(reverse("pennant_index")).content.decode()
        self.assertNotIn('name="leagues"', content, "上限に達したらフォームを出さない")
        self.assertIn(f"作れる世界は1人{MAX_WORLDS_PER_OWNER}つまで", content)
        self.assertIn("alert-secondary", content)

        # フォームを出さなくても、POST は弾く
        before = orm_models.PennantWorld.objects.count()
        response = self.client.post(reverse("pennant_index"), self.form(name="あふれた世界"))
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)
        self.assertIn(
            f"作れる世界は1人{MAX_WORLDS_PER_OWNER}つまで", [str(m) for m in get_messages(response.wsgi_request)][0]
        )

    def test_the_limit_is_per_owner(self):
        self.client.force_login(self.other)
        for number in range(MAX_WORLDS_PER_OWNER):
            self.client.post(reverse("pennant_index"), self.form(name=f"世界{number}"))
        self.client.force_login(self.owner)

        response = self.client.post(reverse("pennant_index"), self.form(name="別の人の世界"))

        self.assertEqual(response.status_code, 302)

    def test_the_list_splits_my_worlds_from_others_and_hides_the_owner(self):
        self.client.force_login(self.owner)

        content = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn("あなたの世界", content)
        self.assertIn("ほかの人の世界", content)
        self.assertLess(content.index("世界A"), content.index("ほかの人の世界"))
        self.assertGreater(content.index("世界B"), content.index("ほかの人の世界"))
        self.assertNotIn("owner", content.split("<main")[1], "オーナーの名前は出さない（ヘッダーのログイン名は除く）")

    def test_anonymous_sees_every_world_as_someone_elses(self):
        content = self.client.get(reverse("pennant_index")).content.decode()

        self.assertNotIn("あなたの世界", content)
        self.assertIn("ほかの人の世界", content)

    def test_the_empty_list_says_login_is_needed(self):
        orm_models.PennantWorld.objects.all().delete()

        content = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn("まだ世界がありません。ログインすると作成できます。", content)


class DeleteWorldTest(HomeCase):
    def delete_url(self, world_id=None) -> str:
        return reverse("pennant_delete", args=[self.world_a if world_id is None else world_id])

    def test_anonymous_goes_to_the_login_for_both_methods(self):
        for method in (self.client.get, self.client.post):
            with self.subTest(method=method.__name__):
                response = method(self.delete_url())

                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])
        self.assertTrue(orm_models.PennantWorld.objects.filter(id=self.world_a).exists())

    def test_another_user_gets_403_for_both_methods(self):
        self.client.force_login(self.other)

        for method in (self.client.get, self.client.post):
            with self.subTest(method=method.__name__):
                self.assertEqual(method(self.delete_url()).status_code, 403)
        self.assertTrue(orm_models.PennantWorld.objects.filter(id=self.world_a).exists())

    def test_a_superuser_who_is_not_the_owner_gets_403(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))

        self.assertEqual(self.client.post(self.delete_url()).status_code, 403)
        self.assertTrue(orm_models.PennantWorld.objects.filter(id=self.world_a).exists())

    def test_the_confirmation_says_what_will_be_lost(self):
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.WEEK)
        self.client.force_login(self.owner)

        content = self.client.get(self.delete_url()).content.decode()

        self.assertIn("世界A", content)
        self.assertIn("1シーズン・6試合", content, "同じ年の試合は1シーズンと数える")
        self.assertIn("元に戻せません", content)
        self.assertIn("実データには影響しません", content)
        self.assertIn("やめる", content)
        self.assertTrue(orm_models.PennantWorld.objects.filter(id=self.world_a).exists(), "GET では消さない")

    def test_a_post_deletes_only_that_world_and_goes_back_to_the_list(self):
        self.client.force_login(self.owner)
        real_teams = orm_models.Team.objects.filter(league__world__isnull=True).count()

        response = self.client.post(self.delete_url(), follow=True)

        self.assertRedirects(response, reverse("pennant_index"))
        self.assertIn("削除しました", response.content.decode())
        self.assertFalse(orm_models.PennantWorld.objects.filter(id=self.world_a).exists())
        self.assertTrue(orm_models.PennantWorld.objects.filter(id=self.world_b).exists())
        self.assertEqual(orm_models.Team.objects.filter(league__world__isnull=True).count(), real_teams)

    def test_deleting_twice_is_a_404(self):
        self.client.force_login(self.owner)
        self.client.post(self.delete_url())

        self.assertEqual(self.client.post(self.delete_url()).status_code, 404)


class CreateFailureTest(HomeCase):
    def test_a_world_whose_schedule_cannot_be_made_is_not_left_behind(self):
        """日程が組めない世界は進められない。作りかけを残さず、理由を出す。"""
        self.client.force_login(self.other)
        before = orm_models.PennantWorld.objects.count()
        form = {
            "name": "日程の組めない世界",
            "leagues": [str(self.league_id)],
            "managed_team": str(self.home.id),
            "start_year": "2040",
            "seed": "",
        }

        with mock.patch.object(
            PennantSeasonService, "ensure_schedule", side_effect=InvalidSchedule("日程が組めませんでした。")
        ):
            response = self.client.post(reverse("pennant_index"), form)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)
        self.assertIn("日程が組めませんでした。", response.content.decode())


class WorldCountTest(HomeCase):
    def test_the_count_is_per_owner_and_does_not_build_worlds(self):
        from myapp.infrastructure.repositories import DjangoWorldRepository

        repository = DjangoWorldRepository()

        self.assertEqual(repository.count_by_owner(self.owner.id), 1, "世界Aだけ")
        self.assertEqual(repository.count_by_owner(self.other.id), 0)
