"""ドラフトの指名順の規則（#198 D1）: 世界の作成・設定の画面・シーズンを締める処理の会議・オフの結果の「指名」。

土台の世界（2リーグ×3球団・公式戦を終えた NPB 方式と方式「なし」）は `world_case.PostseasonWorldCase`。
締める処理は方式「なし」の世界で行う（ポストシーズンを経ずに締められる）。
"""

from __future__ import annotations

from collections import defaultdict
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse

from myapp.domain.exceptions import AlreadyClosed
from myapp.domain.pennant.draft_order import DraftOrderRule
from myapp.domain.pennant.offseason import plan_offseason
from myapp.domain.pennant.world import league_priority
from myapp.domain.value_objects import AcquisitionRoute
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_offseason_service, build_pennant_settings_service

from ..helpers import default_workspace, make_editor, world_ledger, world_scope, ws_url
from .world_case import YEAR, PostseasonWorldCase

DECIDE_RETIREMENTS = "myapp.domain.pennant.offseason.decide_retirements"
PLAN_OFFSEASON = "myapp.application.pennant_offseason.plan_offseason"


class DraftOrderCase(PostseasonWorldCase):
    owner: User

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = make_editor(User.objects.create_user("gm", password="x"))
        orm_models.PennantWorld.objects.filter(id__in=[cls.npb_world, cls.none_world]).update(owner_id=cls.owner.id)

    @staticmethod
    def rule_of(world_id) -> DraftOrderRule:
        return world_ledger().find_by_id(world_id).draft_order_rule

    def settings_page(self, world_id=None):
        return self.client.get(ws_url("pennant_settings", world_id or self.npb_world))

    def post_rule(self, value, world_id=None):
        return self.client.post(
            ws_url("pennant_draft_order_settings", world_id or self.npb_world), {"draft_order_rule": value}
        )

    def close(self, world_id):
        with mock.patch(DECIDE_RETIREMENTS, return_value=()):
            return build_pennant_offseason_service(world_scope(world_id)).close_season(expected_year=YEAR)

    @staticmethod
    def rookies(world_id):
        """締めた年のドラフト・新外国人で入った在籍（球団 id ごと）。"""
        stints = orm_models.PlayerStint.objects.filter(
            team__league__world_id=world_id, from_year=YEAR + 1
        ).select_related("player")
        by_team = defaultdict(list)
        for stint in stints:
            by_team[stint.team_id].append(stint)
        return by_team


class SettingsTest(DraftOrderCase):
    def test_existing_and_new_worlds_use_the_npb_rule_by_default(self):
        self.assertIs(self.rule_of(self.npb_world), DraftOrderRule.NPB)

    def test_anyone_can_read_the_rule_but_only_the_owner_gets_the_form(self):
        response = self.settings_page()
        self.assertContains(response, f'data-testid="draft-order-rule">{DraftOrderRule.NPB.label}<')
        self.assertNotContains(response, 'name="draft_order_rule"')

        self.client.force_login(User.objects.create_user("other", password="x"))
        self.assertNotContains(self.settings_page(), 'name="draft_order_rule"')

        self.client.force_login(self.owner)
        content = self.settings_page().content.decode()
        self.assertIn(ws_url("pennant_draft_order_settings", self.npb_world), content)
        self.assertRegex(content, r'value="NPB"[^>]*checked')
        for rule in DraftOrderRule:
            self.assertIn(f'value="{rule.name}"', content)
            self.assertIn(rule.description, content)

    def test_guests_are_sent_to_log_in_and_others_are_refused(self):
        response = self.post_rule("WAIVER")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        self.client.force_login(User.objects.create_user("other", password="x"))
        self.assertEqual(self.post_rule("WAIVER").status_code, 403)
        self.assertIs(self.rule_of(self.npb_world), DraftOrderRule.NPB)

    def test_the_owner_changes_the_rule(self):
        self.client.force_login(self.owner)
        response = self.post_rule("WAIVER")

        self.assertRedirects(response, ws_url("pennant_settings", self.npb_world), fetch_redirect_response=False)
        self.assertIs(self.rule_of(self.npb_world), DraftOrderRule.WAIVER)
        self.assertContains(self.client.get(response["Location"]), "ドラフトの指名順を「完全ウェーバー」にしました。")
        self.assertContains(self.client.get(response["Location"]), "ドラフトの指名順は変わっていません。", count=0)
        self.post_rule("WAIVER")
        self.assertContains(self.settings_page(), "ドラフトの指名順は変わっていません。")

    def test_a_bad_value_changes_nothing(self):
        self.client.force_login(self.owner)
        for value in ("", "npb", "なし"):
            with self.subTest(value=value):
                response = self.post_rule(value)
                self.assertRedirects(
                    response, ws_url("pennant_settings", self.npb_world), fetch_redirect_response=False
                )
        self.assertIs(self.rule_of(self.npb_world), DraftOrderRule.NPB)

    def test_get_goes_back_to_the_settings(self):
        self.client.force_login(self.owner)
        response = self.client.get(ws_url("pennant_draft_order_settings", self.npb_world))
        self.assertRedirects(response, ws_url("pennant_settings", self.npb_world), fetch_redirect_response=False)


class CreateFormTest(DraftOrderCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def _post(self, **overrides):
        data = {
            "name": "指名順の世界",
            "leagues": [
                str(league)
                for league in orm_models.League.objects.filter(world__isnull=True).values_list("id", flat=True)
            ],
            "managed_team": str(self.managed_source_id),
            "start_year": str(YEAR),
            "postseason_format": "NONE",
            "draft_order_rule": "RANDOM_SNAKE",
        }
        data.update(overrides)
        return self.client.post(ws_url("pennant_index"), data)

    def created_rule(self) -> str:
        return orm_models.PennantWorld.objects.get(name="指名順の世界").draft_order_rule

    def test_the_form_offers_every_rule_with_the_npb_rule_first(self):
        response = self.client.get(ws_url("pennant_index"))
        self.assertEqual(response.context["form"]["draft_order_rule"].value(), "NPB")
        for rule in DraftOrderRule:
            self.assertContains(response, rule.description)

    def test_the_chosen_rule_is_kept(self):
        self._post()
        self.assertEqual(self.created_rule(), DraftOrderRule.RANDOM_SNAKE.value)

    def test_an_omitted_rule_is_the_npb_rule(self):
        data = self._post(draft_order_rule="")
        self.assertEqual(data.status_code, 302)
        self.assertEqual(self.created_rule(), DraftOrderRule.NPB.value)

    def test_a_bad_rule_is_refused(self):
        response = self._post(draft_order_rule="npb")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors["draft_order_rule"])
        self.assertFalse(orm_models.PennantWorld.objects.filter(name="指名順の世界").exists())


class CloseSeasonDraftTest(DraftOrderCase):
    """締める処理が会議を全自動で行い、新人に本物の巡を付ける。"""

    def test_rookies_carry_real_rounds_and_enter_the_year_after_the_draft(self):
        result = self.close(self.none_world)

        rookies = self.rookies(self.none_world)
        self.assertEqual(sum(len(stints) for stints in rookies.values()), result.draftee_count)
        for stints in rookies.values():
            japanese = [s for s in stints if s.player.draft_year is not None]
            foreign = [s for s in stints if s.player.draft_year is None]
            self.assertEqual(sorted(s.player.draft_round for s in japanese), list(range(1, len(japanese) + 1)))
            for stint in japanese:
                self.assertEqual(stint.player.draft_year, YEAR)
                self.assertFalse(stint.player.is_development_draft)
                self.assertEqual(stint.acquired_via, AcquisitionRoute.DRAFT.value)
            for stint in foreign:
                self.assertTrue(stint.player.is_foreign_player)
                self.assertEqual(stint.acquired_via, AcquisitionRoute.NEW_FOREIGN.value)
        self.assertGreaterEqual(result.first_round_lottery_count, 0)

    def test_the_meeting_uses_the_world_rule_and_the_regular_season_order(self):
        """指名順は世界の規則と、締める年の公式戦の最終順位（リーグはその年の優先順）。"""
        build_pennant_settings_service(world_scope(self.none_world)).change_draft_order_rule(DraftOrderRule.WAIVER)
        with mock.patch(PLAN_OFFSEASON, wraps=plan_offseason) as planned:
            self.close(self.none_world)

        draft = planned.call_args.kwargs["draft"]
        self.assertIs(draft.rule, DraftOrderRule.WAIVER)
        leagues = list(
            orm_models.League.objects.filter(world_id=self.none_world).order_by("id").values_list("id", flat=True)
        )
        priority = league_priority(leagues, year=YEAR, start_year=YEAR)
        teams = {
            league: set(orm_models.Team.objects.filter(league_id=league).values_list("id", flat=True))
            for league in leagues
        }
        self.assertEqual([set(league) for league in draft.standings.leagues], [teams[league] for league in priority])
        self.assertEqual(draft.standings.unranked, ())

    def test_a_rule_changed_while_closing_closes_nothing(self):
        """締める計算の間に規則が変わったら、古い規則の新人を書かずに断る（`_state()` に規則を入れた）。"""
        service = build_pennant_offseason_service(world_scope(self.none_world))
        atomic = service._atomic

        def racing():
            build_pennant_settings_service(world_scope(self.none_world)).change_draft_order_rule(
                DraftOrderRule.ALL_LOTTERY
            )
            return atomic()

        service._atomic = racing
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), self.assertRaises(AlreadyClosed):
            service.close_season(expected_year=YEAR)
        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).exists())
        self.assertEqual(self.rookies(self.none_world), {})

    def test_the_offseason_screen_shows_the_pick(self):
        self.close(self.none_world)
        content = self.client.get(ws_url("pennant_offseason", self.none_world, YEAR)).content.decode()
        self.assertIn("<th>指名</th>", content)
        self.assertIn('data-testid="rookie-pick">1位<', content)


class CreateCommandTest(DraftOrderCase):
    def test_the_command_takes_the_rule(self):
        out = StringIO()
        leagues = orm_models.League.objects.filter(world__isnull=True).values_list("id", flat=True)
        call_command(
            "pennant_create",
            "--workspace",
            default_workspace().key,
            "--name",
            "コマンドの世界",
            *[arg for league in leagues for arg in ("--league", str(league))],
            "--year",
            str(YEAR),
            "--seed",
            "3",
            "--managed-team",
            str(self.managed_source_id),
            "--draft-order",
            "waiver",
            stdout=out,
        )
        self.assertEqual(
            orm_models.PennantWorld.objects.get(name="コマンドの世界").draft_order_rule, DraftOrderRule.WAIVER.value
        )
        self.assertIn("ドラフトの指名順 完全ウェーバー", out.getvalue())
