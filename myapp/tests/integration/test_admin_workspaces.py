"""管理画面とワークスペース。登録・一覧の絞り込み・変更画面の選択肢・範囲をまたぐ組み合わせの拒否。

選択肢を絞るのは導線で、本体は `clean()` の「同じワークスペースか」の検査（追加画面は行の
ワークスペースがまだ決まっておらず、選択肢を絞れない）。
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.admin import CaptaincyForm, GameForm
from myapp.domain.workspace import KEY_LENGTH, validate_workspace_key
from myapp.infrastructure import orm_models

from ..helpers import make_league, make_stadium, make_workspace
from .base import BaseCase

CROSS = "別のワークスペースに属しています"
GAME = {"year": 2024, "played_on": "2024-04-01", "home_score": 0, "away_score": 0}


class TwoWorkspaceCase(BaseCase):
    """ワークスペースを2つ（既定の A と別の B）持つ土台。A には BaseCase のリーグ・球場・チームがある。"""

    workspace_b: orm_models.Workspace
    league_b: orm_models.League
    stadium_b: orm_models.Stadium
    team_b: orm_models.Team

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.workspace_b = make_workspace(name="別ワークスペース")
        cls.league_b = make_league("Bリーグ", workspace=cls.workspace_b)
        cls.stadium_b = make_stadium("B球場", workspace=cls.workspace_b)
        cls.team_b = orm_models.Team.objects.create(league=cls.league_b, name="Bチーム")
        cls.player = orm_models.Player.objects.create(name="山田")
        cls.stint = orm_models.PlayerStint.objects.create(
            player=cls.player, team=cls.team, number="10", from_year=2020
        )

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def _choices(self, response, field):
        """変更画面のフォームの、その欄の選択肢（pk の集合）。"""
        return set(response.context["adminform"].form.fields[field].queryset.values_list("pk", flat=True))


class WorkspaceRegistrationTest(BaseCase):
    """空の DB で、運営者が最初のワークスペースを作り、続けてリーグ・球場を作れる。"""

    def setUp(self):
        super().setUp()
        self.operator = User.objects.create_superuser(username="root", password="x")
        self.client.force_login(self.operator)
        orm_models.Stadium.objects.all().delete()
        orm_models.League.objects.all().delete()
        orm_models.Workspace.objects.all().delete()

    def _workspace_data(self, **overrides):
        return {
            "name": "最初のワークスペース",
            "key": "",
            "owner": self.operator.id,
            "visibility": "public",
            "editors-TOTAL_FORMS": 0,
            "editors-INITIAL_FORMS": 0,
            "editors-MIN_NUM_FORMS": 0,
            "editors-MAX_NUM_FORMS": 1000,
        } | overrides

    def test_the_first_workspace_is_created_with_a_generated_key_and_then_a_league_and_a_stadium(self):
        response = self.client.post(reverse("admin:myapp_workspace_add"), self._workspace_data())

        self.assertEqual(response.status_code, 302)
        workspace = orm_models.Workspace.objects.get()
        self.assertEqual(len(workspace.key), KEY_LENGTH)
        self.assertEqual(validate_workspace_key(workspace.key), workspace.key)
        self.assertEqual(workspace.owner, self.operator)

        league_response = self.client.post(
            reverse("admin:myapp_league_add"),
            {
                "name": "新リーグ",
                "workspace": workspace.id,
                "display_order": 0,
                "teams-TOTAL_FORMS": 0,
                "teams-INITIAL_FORMS": 0,
                "teams-MIN_NUM_FORMS": 0,
                "teams-MAX_NUM_FORMS": 1000,
            },
        )
        self.assertEqual(league_response.status_code, 302)
        stadium_response = self.client.post(
            reverse("admin:myapp_stadium_add"), {"name": "新球場", "workspace": workspace.id}
        )
        self.assertEqual(stadium_response.status_code, 302)
        self.assertEqual(orm_models.League.objects.get().workspace_id, workspace.id)
        self.assertEqual(orm_models.Stadium.objects.get().workspace_id, workspace.id)

    def test_the_visibility_choices_come_from_the_domain(self):
        response = self.client.get(reverse("admin:myapp_workspace_add"))

        self.assertContains(response, "公開")
        self.assertContains(response, "非公開")

    def test_a_key_that_is_not_the_domain_form_is_a_form_error(self):
        response = self.client.post(reverse("admin:myapp_workspace_add"), self._workspace_data(key="Bad Key"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "英小文字と数字")
        self.assertFalse(orm_models.Workspace.objects.exists())

    def test_a_given_key_is_kept_and_cannot_be_changed_afterwards(self):
        self.client.post(reverse("admin:myapp_workspace_add"), self._workspace_data(key="abcdefghij"))
        workspace = orm_models.Workspace.objects.get()
        self.assertEqual(workspace.key, "abcdefghij")

        response = self.client.get(reverse("admin:myapp_workspace_change", args=[workspace.id]))
        self.assertNotIn("key", response.context["adminform"].form.fields)
        self.client.post(
            reverse("admin:myapp_workspace_change", args=[workspace.id]),
            self._workspace_data(key="zzzzzzzzzz", name="改名", **{"editors-INITIAL_FORMS": 0}),
        )
        workspace.refresh_from_db()
        self.assertEqual((workspace.key, workspace.name), ("abcdefghij", "改名"))

    def test_the_workspace_is_listed_first_in_the_admin_index(self):
        response = self.client.get(reverse("admin:index"))

        myapp = next(app for app in response.context["app_list"] if app["app_label"] == "myapp")
        self.assertEqual(myapp["models"][0]["object_name"], "Workspace")


class WorkspaceReadOnlyTest(TwoWorkspaceCase):
    """登録済みのリーグ・球場はワークスペースを変えられない（追加では選ぶ）。"""

    def test_the_workspace_is_a_field_on_the_add_screens_and_read_only_on_the_change_screens(self):
        for model, row in [("league", self.league), ("stadium", self.stadium)]:
            with self.subTest(model=model):
                add = self.client.get(reverse(f"admin:myapp_{model}_add"))
                self.assertIn("workspace", add.context["adminform"].form.fields)
                change = self.client.get(reverse(f"admin:myapp_{model}_change", args=[row.id]))
                self.assertNotIn("workspace", change.context["adminform"].form.fields)
                self.assertIn("workspace", change.context["adminform"].readonly_fields)

    def test_a_posted_workspace_does_not_move_an_existing_league(self):
        response = self.client.post(
            reverse("admin:myapp_league_change", args=[self.league.id]),
            {
                "name": "改名リーグ",
                "workspace": self.workspace_b.id,
                "display_order": 0,
                "teams-TOTAL_FORMS": 0,
                "teams-INITIAL_FORMS": 0,
                "teams-MIN_NUM_FORMS": 0,
                "teams-MAX_NUM_FORMS": 1000,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.league.refresh_from_db()
        self.assertEqual((self.league.name, self.league.workspace_id), ("改名リーグ", self.workspace.id))

    def test_a_posted_workspace_does_not_move_an_existing_stadium(self):
        self.client.post(
            reverse("admin:myapp_stadium_change", args=[self.stadium.id]),
            {"name": "改名球場", "workspace": self.workspace_b.id},
        )

        self.stadium.refresh_from_db()
        self.assertEqual((self.stadium.name, self.stadium.workspace_id), ("改名球場", self.workspace.id))


class WorkspaceListFilterTest(TwoWorkspaceCase):
    """一覧にワークスペースの列があり、フィルタで絞れる。"""

    def setUp(self):
        super().setUp()
        orm_models.PlayerStint.objects.create(
            player=orm_models.Player.objects.create(name="鈴木"), team=self.team_b, number="7", from_year=2020
        )
        self.game_a = orm_models.Game.objects.create(
            year=2024, played_on="2024-04-01", home_team=self.team, away_team=self.rival
        )
        rival_b = orm_models.Team.objects.create(league=self.league_b, name="B相手")
        self.game_b = orm_models.Game.objects.create(
            year=2024, played_on="2024-04-02", home_team=self.team_b, away_team=rival_b
        )

    def _rows(self, model, lookup):
        url = reverse(f"admin:myapp_{model}_changelist")
        response = self.client.get(url, {lookup: self.workspace_b.id})
        self.assertEqual(response.status_code, 200)
        pks = list(response.context["cl"].queryset.values_list("pk", flat=True))
        # 件数も見る（結合で同じ行が重複して並ぶと、集合にすると見えなくなる）
        self.assertEqual(len(pks), len(set(pks)))
        self.assertEqual(response.context["cl"].result_count, len(pks))
        return set(pks)

    def test_league_and_stadium_filter_directly(self):
        self.assertEqual(self._rows("league", "workspace__id__exact"), {self.league_b.id})
        self.assertEqual(self._rows("stadium", "workspace__id__exact"), {self.stadium_b.id})

    def test_team_stint_and_game_filter_through_the_league(self):
        self.assertEqual(
            self._rows("team", "league__workspace__id__exact"),
            set(orm_models.Team.objects.filter(league=self.league_b).values_list("pk", flat=True)),
        )
        self.assertEqual(
            self._rows("playerstint", "team__league__workspace__id__exact"),
            set(orm_models.PlayerStint.objects.filter(team=self.team_b).values_list("pk", flat=True)),
        )
        self.assertEqual(self._rows("game", "home_team__league__workspace__id__exact"), {self.game_b.id})

    def test_player_filters_through_its_stints(self):
        rows = self._rows("player", "stints__team__league__workspace__id__exact")

        self.assertEqual(rows, set(orm_models.Player.objects.filter(name="鈴木").values_list("pk", flat=True)))

    def test_the_lists_show_the_workspace_column_and_filter(self):
        for model in ["league", "team", "stadium", "playerstint", "game", "player"]:
            with self.subTest(model=model):
                response = self.client.get(reverse(f"admin:myapp_{model}_changelist"))
                self.assertContains(response, "別ワークスペース")  # フィルタの選択肢
                if model != "player":
                    self.assertContains(response, "既定のワークスペース")  # 列


class ChangeScreenChoicesTest(TwoWorkspaceCase):
    """変更画面の選択肢に、ほかのワークスペースのリーグ・球場・チームが出ない。"""

    def test_team_change_offers_only_the_leagues_and_stadiums_of_its_workspace(self):
        response = self.client.get(reverse("admin:myapp_team_change", args=[self.team.id]))

        self.assertEqual(self._choices(response, "league"), {self.league.id})
        self.assertEqual(self._choices(response, "home_stadium"), {self.stadium.id})

    def test_league_change_offers_only_the_stadiums_of_its_workspace_for_its_teams(self):
        response = self.client.get(reverse("admin:myapp_league_change", args=[self.league.id]))

        formset = response.context["inline_admin_formsets"][0].formset
        for form in [*formset.forms, formset.empty_form]:
            stadiums = set(form.fields["home_stadium"].queryset.values_list("pk", flat=True))
            self.assertEqual(stadiums, {self.stadium.id})

    def test_stadium_change_offers_only_the_teams_of_its_workspace(self):
        response = self.client.get(reverse("admin:myapp_stadium_change", args=[self.stadium.id]))

        teams = self._choices(response, "home_teams")
        self.assertEqual(teams, {self.team.id, self.rival.id})
        self.assertNotIn(self.team_b.id, teams)

    def test_stint_change_offers_only_the_teams_of_its_workspace(self):
        response = self.client.get(reverse("admin:myapp_playerstint_change", args=[self.stint.id]))

        teams = self._choices(response, "team")
        self.assertEqual(teams, {self.team.id, self.rival.id})

    def test_player_change_narrows_the_team_of_the_stint_and_captaincy_rows(self):
        response = self.client.get(reverse("admin:myapp_player_change", args=[self.player.id]))

        narrowed = 0
        for inline in response.context["inline_admin_formsets"]:
            formset = inline.formset
            if "team" not in formset.empty_form.fields:
                continue
            for form in [*formset.forms, formset.empty_form]:
                teams = set(form.fields["team"].queryset.values_list("pk", flat=True))
                self.assertNotIn(self.team_b.id, teams)
                narrowed += 1
        self.assertEqual(narrowed, 3)  # 在籍の既存の1行と空の行、主将在任の空の行


class CrossWorkspaceFormErrorTest(TwoWorkspaceCase):
    """範囲をまたぐ組み合わせは、フォームのエラーになって何も保存されない。"""

    def test_a_team_add_with_a_stadium_of_another_workspace_is_rejected(self):
        response = self.client.post(
            reverse("admin:myapp_team_add"),
            {"league": self.league.id, "name": "新チーム", "display_order": 0, "home_stadium": self.stadium_b.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertFalse(orm_models.Team.objects.filter(name="新チーム").exists())

    def test_a_team_add_in_the_same_workspace_is_saved(self):
        response = self.client.post(
            reverse("admin:myapp_team_add"),
            {"league": self.league_b.id, "name": "新チーム", "display_order": 0, "home_stadium": self.stadium_b.id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(orm_models.Team.objects.filter(name="新チーム", league=self.league_b).exists())

    def test_a_team_change_cannot_move_to_a_league_of_another_workspace(self):
        response = self.client.post(
            reverse("admin:myapp_team_change", args=[self.team.id]),
            {"league": self.league_b.id, "name": self.team.name, "display_order": 0},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じワークスペースの実データだけです")
        self.team.refresh_from_db()
        self.assertEqual(self.team.league_id, self.league.id)

    def test_a_league_add_with_a_team_stadium_of_another_workspace_is_rejected(self):
        response = self.client.post(
            reverse("admin:myapp_league_add"),
            {
                "name": "新リーグ",
                "workspace": self.workspace.id,
                "display_order": 0,
                "teams-TOTAL_FORMS": 1,
                "teams-INITIAL_FORMS": 0,
                "teams-MIN_NUM_FORMS": 0,
                "teams-MAX_NUM_FORMS": 1000,
                "teams-0-name": "新チーム",
                "teams-0-display_order": 0,
                "teams-0-home_stadium": self.stadium_b.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じワークスペースの実データだけです")
        self.assertFalse(orm_models.League.objects.filter(name="新リーグ").exists())
        self.assertFalse(orm_models.Team.objects.filter(name="新チーム").exists())

    def test_a_stadium_add_with_a_team_of_another_workspace_is_rejected(self):
        response = self.client.post(
            reverse("admin:myapp_stadium_add"),
            {"name": "新球場", "workspace": self.workspace.id, "home_teams": [self.team_b.id]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertFalse(orm_models.Stadium.objects.filter(name="新球場").exists())
        self.team_b.refresh_from_db()
        self.assertIsNone(self.team_b.home_stadium_id)

    def test_a_stadium_change_cannot_take_a_team_of_another_workspace(self):
        response = self.client.post(
            reverse("admin:myapp_stadium_change", args=[self.stadium.id]),
            {"name": self.stadium.name, "home_teams": [self.team_b.id]},
        )

        self.assertEqual(response.status_code, 200)
        self.team_b.refresh_from_db()
        self.assertIsNone(self.team_b.home_stadium_id)

    def test_a_stint_add_to_a_team_of_another_workspace_than_the_players_is_rejected(self):
        response = self.client.post(
            reverse("admin:myapp_playerstint_add"),
            {"player": self.player.id, "team": self.team_b.id, "number": "55", "from_year": "2021"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertEqual(orm_models.PlayerStint.objects.filter(player=self.player).count(), 1)

    def test_the_stints_of_one_player_submitted_together_must_share_a_workspace(self):
        new_player = orm_models.Player.objects.create(name="新人")
        data = {
            "name": new_player.name,
            "position": new_player.position,
            "stints-TOTAL_FORMS": 2,
            "stints-INITIAL_FORMS": 0,
            "stints-MIN_NUM_FORMS": 0,
            "stints-MAX_NUM_FORMS": 1000,
            "stints-0-team": self.team.id,
            "stints-0-number": "20",
            "stints-0-from_year": "2020",
            "stints-1-team": self.team_b.id,
            "stints-1-number": "21",
            "stints-1-from_year": "2021",
            "fa_declarations-TOTAL_FORMS": 0,
            "fa_declarations-INITIAL_FORMS": 0,
            "fa_declarations-MIN_NUM_FORMS": 0,
            "fa_declarations-MAX_NUM_FORMS": 1000,
            "captaincies-TOTAL_FORMS": 0,
            "captaincies-INITIAL_FORMS": 0,
            "captaincies-MIN_NUM_FORMS": 0,
            "captaincies-MAX_NUM_FORMS": 1000,
        }

        response = self.client.post(reverse("admin:myapp_player_change", args=[new_player.id]), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertFalse(orm_models.PlayerStint.objects.filter(player=new_player).exists())

    def test_a_captaincy_for_a_team_of_another_workspace_than_the_players_is_rejected(self):
        form = CaptaincyForm({"player": self.player.id, "team": self.team_b.id, "from_year": 2024, "to_year": 2024})

        self.assertFalse(form.is_valid())
        self.assertIn(CROSS, form.errors["team"][0])

    def test_a_game_between_teams_of_different_workspaces_is_rejected(self):
        form = GameForm({**GAME, "home_team": self.team.id, "away_team": self.team_b.id})

        self.assertFalse(form.is_valid())
        self.assertIn(CROSS, form.errors["away_team"][0])

    def test_a_game_between_teams_of_one_workspace_is_valid(self):
        form = GameForm({**GAME, "home_team": self.team.id, "away_team": self.rival.id})

        self.assertTrue(form.is_valid(), form.errors)


class ReadOnlyWorkspaceUniqueNameTest(TwoWorkspaceCase):
    """ワークスペースが読み取り専用の変更画面でも、同名の重複は 500 ではなくフォームのエラーになる。"""

    def test_a_duplicate_league_name_in_the_change_form_is_a_form_error(self):
        other = make_league("もう一つのリーグ", workspace=self.workspace)

        response = self.client.post(
            reverse("admin:myapp_league_change", args=[other.id]),
            {
                "name": self.league.name,
                "display_order": 0,
                "teams-TOTAL_FORMS": 0,
                "teams-INITIAL_FORMS": 0,
                "teams-MIN_NUM_FORMS": 0,
                "teams-MAX_NUM_FORMS": 1000,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じワークスペースに同じ名前のリーグがあります。")
        self.assertEqual(orm_models.League.objects.get(id=other.id).name, "もう一つのリーグ")

    def test_a_duplicate_stadium_name_in_the_change_form_is_a_form_error(self):
        other = make_stadium("もう一つの球場", workspace=self.workspace)

        response = self.client.post(
            reverse("admin:myapp_stadium_change", args=[other.id]), {"name": self.stadium.name}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じワークスペースに同じ名前の球場があります。")
        self.assertEqual(orm_models.Stadium.objects.get(id=other.id).name, "もう一つの球場")

    def test_the_same_name_in_another_workspace_is_allowed(self):
        response = self.client.post(
            reverse("admin:myapp_stadium_change", args=[self.stadium_b.id]),
            {"name": self.stadium.name},
        )

        self.assertEqual(response.status_code, 302)


def _player_data(name, *, stints=(), captaincies=()):
    """選手の追加・変更画面の送信内容（在籍・FA 宣言・主将在任のインライン付き）。"""
    data = {
        "name": name,
        "position": "内野手",
        "stints-TOTAL_FORMS": len(stints),
        "stints-INITIAL_FORMS": 0,
        "stints-MIN_NUM_FORMS": 0,
        "stints-MAX_NUM_FORMS": 1000,
        "fa_declarations-TOTAL_FORMS": 0,
        "fa_declarations-INITIAL_FORMS": 0,
        "fa_declarations-MIN_NUM_FORMS": 0,
        "fa_declarations-MAX_NUM_FORMS": 1000,
        "captaincies-TOTAL_FORMS": len(captaincies),
        "captaincies-INITIAL_FORMS": 0,
        "captaincies-MIN_NUM_FORMS": 0,
        "captaincies-MAX_NUM_FORMS": 1000,
    }
    for i, (team, number) in enumerate(stints):
        data |= {f"stints-{i}-team": team.id, f"stints-{i}-number": number, f"stints-{i}-from_year": "2020"}
    for i, team in enumerate(captaincies):
        data |= {
            f"captaincies-{i}-team": team.id,
            f"captaincies-{i}-from_year": "2020",
            f"captaincies-{i}-to_year": "2021",
        }
    return data


class CaptaincyAcrossWorkspacesTest(TwoWorkspaceCase):
    """主将在任のチームは、同じ送信で足す在籍とも同じワークスペースでなければならない（在籍が未保存でも）。"""

    def test_the_player_add_screen_rejects_a_captaincy_in_another_workspace_than_the_new_stint(self):
        data = _player_data("新人", stints=[(self.team, "20")], captaincies=[self.team_b])

        response = self.client.post(reverse("admin:myapp_player_add"), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertFalse(orm_models.Player.objects.filter(name="新人").exists())
        self.assertFalse(orm_models.Captaincy.objects.exists())

    def test_the_change_screen_of_a_player_without_stints_rejects_it_too(self):
        bare = orm_models.Player.objects.create(name="無所属")
        data = _player_data("無所属", stints=[(self.team, "20")], captaincies=[self.team_b])

        response = self.client.post(reverse("admin:myapp_player_change", args=[bare.id]), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertFalse(orm_models.PlayerStint.objects.filter(player=bare).exists())
        self.assertFalse(orm_models.Captaincy.objects.exists())

    def test_a_captaincy_in_the_same_workspace_as_the_new_stint_is_saved(self):
        data = _player_data("新人", stints=[(self.team_b, "20")], captaincies=[self.team_b])

        response = self.client.post(reverse("admin:myapp_player_add"), data)

        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["errors"])
        self.assertEqual(orm_models.Captaincy.objects.get().team_id, self.team_b.id)


class StintPlayerChangeTest(TwoWorkspaceCase):
    def test_re_pointing_a_stint_to_a_player_of_another_workspace_is_rejected(self):
        player_b = orm_models.Player.objects.create(name="Bの選手")
        orm_models.PlayerStint.objects.create(player=player_b, team=self.team_b, number="9", from_year=2020)

        response = self.client.post(
            reverse("admin:myapp_playerstint_change", args=[self.stint.id]),
            {"player": player_b.id, "team": self.team.id, "number": "10", "from_year": "2020"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.stint.refresh_from_db()
        self.assertEqual(self.stint.player_id, self.player.id)


class WorkspaceEditorTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.operator = User.objects.create_superuser(username="root", password="x")
        self.client.force_login(self.operator)

    def _change(self, user):
        return self.client.post(
            reverse("admin:myapp_workspace_change", args=[self.workspace.id]),
            {
                "name": self.workspace.name,
                "owner": self.workspace.owner_id,
                "visibility": "public",
                "editors-TOTAL_FORMS": 1,
                "editors-INITIAL_FORMS": 0,
                "editors-MIN_NUM_FORMS": 0,
                "editors-MAX_NUM_FORMS": 1000,
                "editors-0-user": user.id,
            },
        )

    def test_the_owner_cannot_be_added_as_an_editor(self):
        response = self._change(self.workspace.owner)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "持ち主は編集者に加えられません")
        self.assertFalse(orm_models.WorkspaceEditor.objects.exists())

    def test_another_user_can_be_added_as_an_editor(self):
        response = self._change(self.operator)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.WorkspaceEditor.objects.get().user, self.operator)


class PlayerWorkspaceFromCaptaincyTest(TwoWorkspaceCase):
    """選手のワークスペースは、在籍だけでなく主将在任からも決まる。"""

    def test_a_stint_in_another_workspace_than_a_past_captaincy_is_rejected(self):
        bare = orm_models.Player.objects.create(name="無所属")
        orm_models.Captaincy.objects.create(player=bare, team=self.team_b, from_year=2019, to_year=2020)

        response = self.client.post(
            reverse("admin:myapp_playerstint_add"),
            {"player": bare.id, "team": self.team.id, "number": "20", "from_year": "2021"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertFalse(orm_models.PlayerStint.objects.filter(player=bare).exists())

    def test_deleting_the_only_stint_keeps_the_workspace_of_the_captaincy(self):
        orm_models.Captaincy.objects.create(player=self.player, team=self.team, from_year=2019, to_year=2020)
        captaincy = orm_models.Captaincy.objects.get()
        data = _player_data("山田", stints=[(self.team_b, "30")], captaincies=[])
        data |= {
            "stints-TOTAL_FORMS": 2,
            "stints-INITIAL_FORMS": 1,
            "stints-0-id": self.stint.id,
            "stints-0-player": self.player.id,
            "stints-0-team": self.team.id,
            "stints-0-number": "10",
            "stints-0-from_year": "2020",
            "stints-0-DELETE": "on",
            "stints-1-team": self.team_b.id,
            "stints-1-number": "30",
            "stints-1-from_year": "2021",
            "captaincies-TOTAL_FORMS": 1,
            "captaincies-INITIAL_FORMS": 1,
            "captaincies-0-id": captaincy.id,
            "captaincies-0-player": self.player.id,
            "captaincies-0-team": self.team.id,
            "captaincies-0-from_year": "2019",
            "captaincies-0-to_year": "2020",
        }

        with patch("myapp.admin._narrow_choices"):
            response = self.client.post(reverse("admin:myapp_player_change", args=[self.player.id]), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertTrue(orm_models.PlayerStint.objects.filter(pk=self.stint.id).exists())
        self.assertFalse(orm_models.PlayerStint.objects.filter(team=self.team_b).exists())


class CleanIsTheRealGuardTest(TwoWorkspaceCase):
    """選択肢を絞る処理が無くても、clean() が範囲をまたぐ組み合わせを弾く。"""

    def _existing_stint_row(self):
        return {
            "stints-0-id": self.stint.id,
            "stints-0-player": self.player.id,
            "stints-0-team": self.team.id,
            "stints-0-number": "10",
            "stints-0-from_year": "2020",
        }

    def test_a_new_stint_of_another_workspace_is_rejected_without_narrowing(self):
        data = _player_data("山田", stints=[(self.team, "10"), (self.team_b, "30")])
        data |= {"stints-INITIAL_FORMS": 1} | self._existing_stint_row()

        with patch("myapp.admin._narrow_choices"):
            response = self.client.post(reverse("admin:myapp_player_change", args=[self.player.id]), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertEqual(orm_models.PlayerStint.objects.filter(player=self.player).count(), 1)

    def test_a_saved_stint_left_out_of_the_post_still_counts(self):
        """POST から保存済みの在籍の行を落としても、DB に残る在籍のワークスペースと比べる。"""
        data = _player_data("山田", stints=[(self.team_b, "30")])

        with patch("myapp.admin._narrow_choices"):
            response = self.client.post(reverse("admin:myapp_player_change", args=[self.player.id]), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertFalse(orm_models.PlayerStint.objects.filter(team=self.team_b).exists())

    def test_a_saved_captaincy_left_out_of_the_post_still_counts_for_a_new_stint(self):
        bare = orm_models.Player.objects.create(name="無所属")
        orm_models.Captaincy.objects.create(player=bare, team=self.team_b, from_year=2019, to_year=2020)
        data = _player_data("無所属", stints=[(self.team, "20")])

        with patch("myapp.admin._narrow_choices"):
            response = self.client.post(reverse("admin:myapp_player_change", args=[bare.id]), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CROSS)
        self.assertFalse(orm_models.PlayerStint.objects.filter(player=bare).exists())


class WorkspaceEditorOwnerTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.operator = User.objects.create_superuser(username="root", password="x")
        self.client.force_login(self.operator)

    def test_the_owner_cannot_be_an_editor_on_the_add_screen(self):
        response = self.client.post(
            reverse("admin:myapp_workspace_add"),
            {
                "name": "新ワークスペース",
                "key": "",
                "owner": self.operator.id,
                "visibility": "public",
                "editors-TOTAL_FORMS": 1,
                "editors-INITIAL_FORMS": 0,
                "editors-MIN_NUM_FORMS": 0,
                "editors-MAX_NUM_FORMS": 1000,
                "editors-0-user": self.operator.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "持ち主は編集者に加えられません")
        self.assertFalse(orm_models.Workspace.objects.filter(name="新ワークスペース").exists())

    def test_changing_the_owner_to_an_existing_editor_is_rejected(self):
        editor = orm_models.WorkspaceEditor.objects.create(workspace=self.workspace, user=self.operator)

        response = self.client.post(
            reverse("admin:myapp_workspace_change", args=[self.workspace.id]),
            {
                "name": self.workspace.name,
                "owner": self.operator.id,
                "visibility": "public",
                "editors-TOTAL_FORMS": 1,
                "editors-INITIAL_FORMS": 1,
                "editors-MIN_NUM_FORMS": 0,
                "editors-MAX_NUM_FORMS": 1000,
                "editors-0-id": editor.id,
                "editors-0-workspace": self.workspace.id,
                "editors-0-user": self.operator.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "持ち主は編集者に加えられません")
        self.workspace.refresh_from_db()
        self.assertNotEqual(self.workspace.owner_id, self.operator.id)
