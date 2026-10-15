"""ワークスペースの制約（0051）。名前の一意はワークスペースの中だけ、帰属は必須・排他。"""

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import make_league, make_pennant_world, make_stadium, make_workspace
from .base import BaseCase


class WorkspaceConstraintsTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.other = make_workspace(name="別のワークスペース")

    def test_league_name_is_unique_within_a_workspace(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_league("テストリーグ", workspace=self.workspace)

    def test_another_workspace_may_reuse_a_league_name(self):
        league = make_league("テストリーグ", workspace=self.other)

        self.assertEqual(league.workspace_id, self.other.id)

    def test_stadium_name_is_unique_within_a_workspace(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_stadium("テスト球場", workspace=self.workspace)

    def test_another_workspace_may_reuse_a_stadium_name(self):
        stadium = make_stadium("テスト球場", workspace=self.other)

        self.assertEqual(stadium.workspace_id, self.other.id)

    def test_a_pennant_league_may_share_a_name_with_real_data_in_any_workspace(self):
        world = make_pennant_world("世界", seed=1, start_year=2026)
        make_league("テストリーグ", world=world)
        other_world = make_pennant_world("別の世界", workspace=self.other, seed=2, start_year=2026)
        make_league("テストリーグ", world=other_world)

        self.assertEqual(orm_models.League.objects.filter(name="テストリーグ").count(), 3)

    def test_league_name_is_still_unique_within_a_world(self):
        world = make_pennant_world("世界", seed=1, start_year=2026)
        make_league("セ", world=world)

        with self.assertRaises(IntegrityError), transaction.atomic():
            make_league("セ", world=world)

    def test_a_league_with_both_workspace_and_world_cannot_be_created(self):
        world = make_pennant_world("世界", seed=1, start_year=2026)

        with self.assertRaises(IntegrityError), transaction.atomic():
            orm_models.League.objects.create(name="両方", workspace=self.workspace, world=world)

    def test_a_league_with_neither_workspace_nor_world_cannot_be_created(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            orm_models.League.objects.create(name="どちらも無い")

    def test_a_stadium_without_a_workspace_cannot_be_saved(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            orm_models.Stadium.objects.create(name="帰属なし")

    def test_a_pennant_world_without_a_workspace_cannot_be_saved(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            orm_models.PennantWorld.objects.create(name="帰属なし", seed=1, start_year=2026)


class AdminDuplicateNameTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def _league_data(self, **overrides):
        return {
            "name": "テストリーグ",
            "workspace": self.workspace.id,
            "display_order": 0,
            "foreign_player_roster_limit": 5,
            "foreign_player_game_limit": 3,
            "registered_player_limit": 70,
            "teams-TOTAL_FORMS": 0,
            "teams-INITIAL_FORMS": 0,
            "teams-MIN_NUM_FORMS": 0,
            "teams-MAX_NUM_FORMS": 1000,
        } | overrides

    def test_a_duplicate_league_name_in_the_same_workspace_is_a_form_error(self):
        response = self.client.post(reverse("admin:myapp_league_add"), self._league_data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じワークスペースに同じ名前のリーグがあります。")
        self.assertEqual(orm_models.League.objects.filter(name="テストリーグ").count(), 1)

    def test_the_same_league_name_in_another_workspace_is_created(self):
        other = make_workspace(name="別のワークスペース")

        response = self.client.post(reverse("admin:myapp_league_add"), self._league_data(workspace=other.id))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.League.objects.filter(name="テストリーグ").count(), 2)

    def test_a_duplicate_stadium_name_in_the_same_workspace_is_a_form_error(self):
        response = self.client.post(
            reverse("admin:myapp_stadium_add"), {"name": "テスト球場", "workspace": self.workspace.id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じワークスペースに同じ名前の球場があります。")
        self.assertEqual(orm_models.Stadium.objects.filter(name="テスト球場").count(), 1)

    def test_saving_a_league_change_form_without_renaming_does_not_collide_with_itself(self):
        url = reverse("admin:myapp_league_change", args=[self.league.id])

        response = self.client.post(url, self._league_data(display_order=3))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.League.objects.get(id=self.league.id).display_order, 3)

    def test_a_duplicate_name_in_the_stadium_change_form_is_a_form_error(self):
        other = make_stadium("もう一つの球場", workspace=self.workspace)

        response = self.client.post(
            reverse("admin:myapp_stadium_change", args=[other.id]),
            {"name": "テスト球場", "workspace": self.workspace.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じワークスペースに同じ名前の球場があります。")
        self.assertEqual(orm_models.Stadium.objects.get(id=other.id).name, "もう一つの球場")
