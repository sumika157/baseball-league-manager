"""ペナントの世界の作成・一覧・削除（P4b）。

作成と削除は書き込みなので権限を確かめる（未ログインはログインへ、ほかの人は 403）。作成できるのは
ワークスペースの持ち主・編集者だけ（W2 T4。#78）。削除・進めるは世界の作成者。
上限（ワークスペースの世界の数と試合の枠・元にするリーグの数）と、フォームの検証エラー（日本語）も。
GM ホームと進める操作は `test_pennant_home.py`。
"""

from contextlib import contextmanager
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.test import override_settings

from myapp.application.pennant_season import PennantSeasonService
from myapp.domain.exceptions import InvalidSchedule, InvalidWorld, WorkspaceLimitReached
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldUsage, world_game_reserve
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_season_service, build_pennant_world_service, build_roster_service

from ..helpers import (
    default_workspace,
    login_as_manager,
    make_editor,
    make_league,
    play_game,
    real_scope,
    world_ledger,
    world_scope,
    ws_url,
)
from .test_pennant_advance import register_club
from .test_pennant_home import HomeCase


class CreateWorldTest(HomeCase):
    """世界の作成。作る人（`other`・`owner`）は既定のワークスペースの編集者にしておく。"""

    def setUp(self):
        super().setUp()
        make_editor(self.other)
        make_editor(self.owner)

    def form(self, **overrides) -> dict:
        data = {
            "name": "新しい世界",
            "leagues": [str(self.league_id)],
            "managed_team": str(self.home.id),
            "postseason_format": "NPB",
            "start_year": "2040",
            "seed": "5",
        }
        data.update(overrides)
        return data

    def test_the_list_is_open_to_anyone_and_has_no_form_for_anonymous(self):
        content = self.client.get(ws_url("pennant_index")).content.decode()

        self.assertIn("世界A", content)
        self.assertNotIn('name="leagues"', content)

    def test_the_form_is_shown_to_a_logged_in_user(self):
        self.client.force_login(self.other)

        content = self.client.get(ws_url("pennant_index")).content.decode()

        self.assertIn('name="leagues"', content)
        self.assertIn("<optgroup", content, "受け持つ球団はリーグごとの optgroup")
        self.assertIn("作成に数秒かかります", content)
        self.assertIn("<details", content, "シードは詳細設定の中")

    def test_a_logged_in_user_who_is_not_an_editor_gets_no_form_and_cannot_create(self):
        """公開ワークスペースを見に来ただけの人は、他人のワークスペースに世界を作れない（作成はワークスペースへの書き込み）。"""
        self.client.force_login(User.objects.create_superuser("visitor", password="x"))
        before = orm_models.PennantWorld.objects.count()

        content = self.client.get(ws_url("pennant_index")).content.decode()
        response = self.client.post(ws_url("pennant_index"), self.form(name="見に来た人の世界"))

        self.assertIn("世界A", content, "一覧は読める")
        self.assertNotIn('name="leagues"', content)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_the_default_year_is_the_year_after_the_latest_real_season(self):
        play_game(self.home, self.rival, home_score=1, away_score=0, year=2031)
        self.client.force_login(self.other)

        content = self.client.get(ws_url("pennant_index")).content.decode()

        self.assertIn('name="start_year" value="2032"', content)

    def test_an_anonymous_post_creates_nothing(self):
        before = orm_models.PennantWorld.objects.count()

        response = self.client.post(ws_url("pennant_index"), self.form())

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_a_valid_post_creates_the_world_and_goes_to_its_home(self):
        self.client.force_login(self.other)

        response = self.client.post(ws_url("pennant_index"), self.form())

        world = orm_models.PennantWorld.objects.get(name="新しい世界")
        self.assertRedirects(response, ws_url("pennant_world", world.id), fetch_redirect_response=False)
        self.assertEqual(world.owner_id, self.other.id)
        self.assertEqual(world.seed, 5)
        self.assertEqual(world.start_year, 2040)
        self.assertEqual(world.managed_team.name, self.home.name)
        self.assertTrue(
            orm_models.PennantFixture.objects.filter(home_team__league__world=world).exists(), "日程も作る"
        )

    def test_the_new_world_home_opens_for_the_creator_with_the_advance_form(self):
        self.client.force_login(self.other)
        response = self.client.post(ws_url("pennant_index"), self.form(), follow=True)

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("新しい世界", content)
        self.assertIn("1日進める", content)
        self.assertRegex(content, r"\d+月\d+日（.）まで · 1試合")

    def test_without_a_seed_one_is_chosen(self):
        self.client.force_login(self.other)

        self.client.post(ws_url("pennant_index"), self.form(seed=""))

        self.assertTrue(orm_models.PennantWorld.objects.filter(name="新しい世界").exists())

    def test_the_form_errors_are_shown_in_japanese_and_nothing_is_created(self):
        self.client.force_login(self.other)
        before = orm_models.PennantWorld.objects.count()
        other_league = make_league(name="別リーグ")
        other_team = orm_models.Team.objects.create(league=other_league, name="別球団")
        register_club(build_roster_service(real_scope()), other_team, "別")
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
                response = self.client.post(ws_url("pennant_index"), data)

                self.assertEqual(response.status_code, 200)
                self.assertIn(error, response.content.decode())
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_the_entered_values_stay_in_the_form_on_an_error(self):
        self.client.force_login(self.other)

        content = self.client.post(ws_url("pennant_index"), self.form(managed_team="")).content.decode()

        self.assertIn('value="新しい世界"', content)
        self.assertIn('value="2040"', content)

    def test_more_than_eight_leagues_are_refused(self):
        with self.assertRaises(InvalidWorld) as raised:
            build_pennant_world_service(real_scope()).create_world(
                name="多すぎ", owner_id=None, source_league_ids=list(range(1, 10)), start_year=2040, seed=1
            )

        self.assertIn("8つまで", str(raised.exception))

    def test_the_list_splits_my_worlds_from_others_and_hides_the_owner(self):
        self.client.force_login(self.owner)

        content = self.client.get(ws_url("pennant_index")).content.decode()

        self.assertIn("あなたの世界", content)
        self.assertIn("ほかの人の世界", content)
        self.assertLess(content.index("世界A"), content.index("ほかの人の世界"))
        self.assertGreater(content.index("世界B"), content.index("ほかの人の世界"))
        self.assertNotIn("owner", content.split("<main")[1], "オーナーの名前は出さない（ヘッダーのログイン名は除く）")

    def test_anonymous_sees_every_world_as_someone_elses(self):
        content = self.client.get(ws_url("pennant_index")).content.decode()

        self.assertNotIn("あなたの世界", content)
        self.assertIn("ほかの人の世界", content)

    def test_the_empty_list_says_login_is_needed(self):
        orm_models.PennantWorld.objects.all().delete()

        content = self.client.get(ws_url("pennant_index")).content.decode()

        self.assertIn("まだ世界がありません。世界を作れるのは、このワークスペースの持ち主と編集者です", content)


class DeleteWorldTest(HomeCase):
    def delete_url(self, world_id=None) -> str:
        return ws_url("pennant_delete", self.world_a if world_id is None else world_id)

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

    def test_an_editor_who_is_not_the_owner_gets_403(self):
        """ワークスペースの編集者でも、世界の作成者でなければ消せない（持ち主による削除は W6）。"""
        self.client.force_login(make_editor(User.objects.create_user("editor", password="x")))

        self.assertEqual(self.client.post(self.delete_url()).status_code, 403)
        self.assertTrue(orm_models.PennantWorld.objects.filter(id=self.world_a).exists())

    def test_the_confirmation_says_what_will_be_lost(self):
        build_pennant_season_service(world_scope(self.world_a)).advance(AdvanceTarget.WEEK)
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

        self.assertRedirects(response, ws_url("pennant_index"))
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
        self.client.force_login(make_editor(self.other))
        before = orm_models.PennantWorld.objects.count()
        form = {
            "name": "日程の組めない世界",
            "leagues": [str(self.league_id)],
            "managed_team": str(self.home.id),
            "postseason_format": "NPB",
            "start_year": "2040",
            "seed": "",
        }

        with mock.patch.object(
            PennantSeasonService, "ensure_schedule", side_effect=InvalidSchedule("日程が組めませんでした。")
        ):
            response = self.client.post(ws_url("pennant_index"), form)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)
        self.assertIn("日程が組めませんでした。", response.content.decode())


class WorkspaceQuotaTest(HomeCase):
    """世界の数と試合の枠の上限は**ワークスペースごと**（W6 #82）。

    既存の世界は3つ（世界A＝`owner` の世界、世界B・C＝作成者のいない世界）で、どれも球団2つ。
    作る人（`other`・`owner`）は既定のワークスペースの編集者にしておく。
    """

    def setUp(self):
        super().setUp()
        make_editor(self.other)
        make_editor(self.owner)
        self.reserve = world_game_reserve(2)

    def form(self, **overrides) -> dict:
        data = {
            "name": "新しい世界",
            "leagues": [str(self.league_id)],
            "managed_team": str(self.home.id),
            "postseason_format": "NPB",
            "start_year": "2040",
            "seed": "5",
        }
        data.update(overrides)
        return data

    def messages_of(self, response) -> list[str]:
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_the_usage_counts_every_world_of_the_workspace(self):
        """作成者を問わず、作成者のいない世界も数える。"""
        self.assertEqual(world_ledger().usage(), WorldUsage(world_count=3, reserved_games=3 * self.reserve))

    @override_settings(PENNANT_MAX_WORLDS=4)
    def test_worlds_of_other_editors_and_ownerless_worlds_count_toward_the_limit(self):
        self.client.force_login(self.other)
        content = self.client.get(ws_url("pennant_index")).content.decode()
        self.assertIn("このワークスペースの世界 3 / 4", content)
        self.assertEqual(self.client.post(ws_url("pennant_index"), self.form()).status_code, 302)

        # 別の編集者から見ても、ほかの人の世界と作成者のいない世界で上限に達している
        self.client.force_login(self.owner)
        content = self.client.get(ws_url("pennant_index")).content.decode()
        self.assertIn("このワークスペースの世界 4 / 4", content)
        self.assertNotIn('name="leagues"', content, "上限ならフォームを出さない")
        self.assertIn("このワークスペースの世界は4つまでです。", content)

        # フォームを出さなくても、POST は弾いて何も作らない
        before = orm_models.PennantWorld.objects.count()
        response = self.client.post(ws_url("pennant_index"), self.form(name="あふれた世界"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.PennantWorld.objects.count(), before)
        self.assertIn("このワークスペースの世界は4つまでです。", self.messages_of(response)[0])

    def test_a_world_that_exactly_fills_the_game_quota_is_made_and_then_none_fits(self):
        with override_settings(PENNANT_MAX_GAMES=4 * self.reserve):
            self.client.force_login(self.other)
            response = self.client.post(ws_url("pennant_index"), self.form())
            self.assertEqual(response.status_code, 302, "ちょうど収まる")

            content = self.client.get(ws_url("pennant_index")).content.decode()
            self.assertNotIn('name="leagues"', content)
            self.assertIn("収まるリーグがありません", content)

            before = orm_models.PennantWorld.objects.count()
            response = self.client.post(ws_url("pennant_index"), self.form(name="あふれた世界"))
            self.assertEqual(orm_models.PennantWorld.objects.count(), before)
            self.assertIn("このワークスペースの残りは0試合です", self.messages_of(response)[0])

    def test_a_combination_over_the_remainder_is_refused_when_sent(self):
        """リーグ1つずつなら収まるのでフォームは出し、超える組み合わせは送ったときに理由を出して何も作らない。"""
        second = make_league(name="第二リーグ")
        roster = build_roster_service(real_scope())
        for name, prefix in (("甲球団", "甲"), ("乙球団", "乙")):
            register_club(roster, orm_models.Team.objects.create(league=second, name=name), prefix)
        with override_settings(PENNANT_MAX_GAMES=4 * self.reserve):
            self.client.force_login(self.other)
            content = self.client.get(ws_url("pennant_index")).content.decode()
            self.assertIn('name="leagues"', content)
            self.assertIn(f"（10シーズンで約{self.reserve:,}試合）", content, "リーグの横に見込みを出す")

            before = orm_models.PennantWorld.objects.count()
            response = self.client.post(
                ws_url("pennant_index"), self.form(leagues=[str(self.league_id), str(second.id)])
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(orm_models.PennantWorld.objects.count(), before)
            self.assertIn(
                f"この世界には10シーズンで約{2 * self.reserve:,}試合の枠が要りますが、"
                f"このワークスペースの残りは{self.reserve:,}試合です。",
                self.messages_of(response)[0],
            )

    def test_the_usage_is_counted_inside_the_creation_transaction(self):
        """数える・検査する・保存するは1つのトランザクション（数えてから保存までに、別の作成が割り込まない）。"""
        service = build_pennant_world_service(real_scope())
        real_atomic = service._atomic
        depth = [0]
        counted_inside: list[bool] = []

        @contextmanager
        def tracking_atomic():
            depth[0] += 1
            try:
                with real_atomic():
                    yield
            finally:
                depth[0] -= 1

        real_usage = service._worlds.usage

        def tracking_usage() -> WorldUsage:
            counted_inside.append(depth[0] > 0)
            return real_usage()

        with (
            mock.patch.object(service, "_atomic", tracking_atomic),
            mock.patch.object(service._worlds, "usage", side_effect=tracking_usage),
        ):
            service.create_world_with_schedule(
                name="数える世界", owner_id=None, source_league_ids=[self.league_id], start_year=2040, seed=1
            )

        self.assertEqual(counted_inside, [True])

    @override_settings(PENNANT_MAX_WORLDS=3)
    def test_deleting_a_world_gives_its_room_back(self):
        self.client.force_login(self.owner)
        self.assertNotIn('name="leagues"', self.client.get(ws_url("pennant_index")).content.decode())

        build_pennant_world_service(real_scope()).delete_world(self.world_b)

        content = self.client.get(ws_url("pennant_index")).content.decode()
        self.assertIn("このワークスペースの世界 2 / 3", content)
        self.assertIn(f"試合の枠 {2 * self.reserve:,} / {settings.PENNANT_MAX_GAMES:,}", content)
        self.assertIn('name="leagues"', content)

    def test_people_who_cannot_create_see_neither_the_usage_nor_the_form(self):
        def assignee() -> None:
            login_as_manager(self.client, self.home)

        def visitor() -> None:
            self.client.force_login(User.objects.create_user("visitor", password="x"))

        for label, login in {"anonymous": None, "assignee": assignee, "visitor": visitor}.items():
            with self.subTest(label):
                self.client.logout()
                if login is not None:
                    login()
                response = self.client.get(ws_url("pennant_index"))

                self.assertNotContains(response, 'data-testid="pennant-quota"')
                self.assertNotContains(response, 'name="leagues"')
                self.assertIsNone(response.context["quota"])

    @override_settings(PENNANT_MAX_WORLDS=0, PENNANT_MAX_GAMES=0)
    def test_the_command_ignores_the_limit(self):
        """管理コマンドは運営者の道具なので上限を見ない（画面の入口だけが見る）。"""
        with self.assertRaises(WorkspaceLimitReached):
            build_pennant_world_service(real_scope()).create_world_with_schedule(
                name="画面の世界", owner_id=None, source_league_ids=[self.league_id], start_year=2040, seed=1
            )

        call_command(
            "pennant_create",
            "--workspace",
            default_workspace().key,
            "--name",
            "コマンドの世界",
            "--league",
            str(self.league_id),
            "--year",
            "2040",
            "--seed",
            "3",
            "--owner",
            self.owner.username,
            "--managed-team",
            str(self.home.id),
            stdout=StringIO(),
        )

        self.assertTrue(orm_models.PennantWorld.objects.filter(name="コマンドの世界").exists())
        self.assertFalse(orm_models.PennantWorld.objects.filter(name="画面の世界").exists())
