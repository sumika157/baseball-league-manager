"""FA 宣言と経路 FA の整合。リポジトリの保存（消すのは取り消した宣言だけ）と、管理画面が送信後の全体で見る検査。"""

from datetime import date

from django.contrib.auth.models import User

from myapp.domain.exceptions import InvalidFreeAgentDeclaration
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoTeamRepository

from ..helpers import build_roster_service
from .base import BaseCase

THIS_YEAR = date.today().year


class RepositoryDoesNotDeleteUnknownDeclarationsTest(BaseCase):
    """保存は upsert だけ。消すのは、選手が取り消したと記録した宣言だけ。"""

    def test_saving_a_player_that_never_loaded_the_declarations_keeps_them(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        build_roster_service().declare_free_agency(self.team.id, player.id, THIS_YEAR, "国内")
        team = DjangoTeamRepository().find_by_id(self.team.id)
        # 宣言を読み込まずに組み立てた選手（fa_declarations が空）で保存する
        team.find_player(player.id).fa_declarations = []

        DjangoTeamRepository().save(team)

        self.assertEqual(orm_models.PlayerFreeAgentDeclaration.objects.filter(player_id=player.id).count(), 1)

    def test_the_removed_ids_are_cleared_after_saving(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        build_roster_service().declare_free_agency(self.team.id, player.id, THIS_YEAR, "国内")
        team = DjangoTeamRepository().find_by_id(self.team.id)
        team.remove_free_agency_declaration(player.id, THIS_YEAR)
        self.assertEqual(len(team.find_player(player.id).removed_declaration_ids), 1)

        DjangoTeamRepository().save(team)

        self.assertEqual(team.find_player(player.id).removed_declaration_ids, [])
        self.assertFalse(orm_models.PlayerFreeAgentDeclaration.objects.exists())


class AdminSubmittedStateTest(BaseCase):
    """選手の管理画面は、送信後の在籍と FA 宣言の全体で整合を見る（削除・年の変更・在籍の削除や短縮）。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        # team に2024年まで在籍して FA を宣言し、2025年に rival へ FA で加入した選手
        orm_models.PlayerStint.objects.filter(player_id=self.player.id).update(from_year=2015, to_year=2024)
        orm_models.PlayerStint.objects.create(
            player_id=self.player.id, team=self.rival, number=7, from_year=2025, acquired_via="FA"
        )
        self.declaration = orm_models.PlayerFreeAgentDeclaration.objects.create(
            player_id=self.player.id, year=2024, kind="国内"
        )
        self.url = f"/admin/myapp/player/{self.player.id}/change/"

    def _payload(self, stint_overrides=None, declaration_overrides=None):
        payload = {
            "name": self.player.name,
            "position": "内野手",
            "birth_date": "",
            "throws": "",
            "bats": "",
            "height_cm": "",
            "weight_kg": "",
            "birthplace": "",
            "debut_year": "",
            "high_school": "",
            "university": "",
            "corporate_team": "",
            "nationality": "",
            "is_foreign_player": "",
            "captaincies-TOTAL_FORMS": "0",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
            "stints-TOTAL_FORMS": "2",
            "stints-INITIAL_FORMS": "2",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "fa_declarations-TOTAL_FORMS": "1",
            "fa_declarations-INITIAL_FORMS": "1",
            "fa_declarations-MIN_NUM_FORMS": "0",
            "fa_declarations-MAX_NUM_FORMS": "1000",
            "fa_declarations-0-id": str(self.declaration.id),
            "fa_declarations-0-player": str(self.player.id),
            "fa_declarations-0-year": "2024",
            "fa_declarations-0-kind": "国内",
        }
        # インラインの並びは在籍の新しい順。0 番目が rival（2025〜）、1 番目が team（2015〜2024）
        for index, stint in enumerate(
            orm_models.PlayerStint.objects.filter(player_id=self.player.id).order_by("-from_year")
        ):
            payload.update(
                {
                    f"stints-{index}-id": str(stint.id),
                    f"stints-{index}-player": str(self.player.id),
                    f"stints-{index}-team": str(stint.team_id),
                    f"stints-{index}-number": str(stint.number),
                    f"stints-{index}-from_year": str(stint.from_year),
                    f"stints-{index}-to_year": "" if stint.to_year is None else str(stint.to_year),
                    f"stints-{index}-signed_as": stint.signed_as,
                    f"stints-{index}-promoted_year": "",
                    f"stints-{index}-number_before_promotion": "",
                    f"stints-{index}-acquired_via": stint.acquired_via or "",
                }
            )
        payload.update({f"stints-{key}": value for key, value in (stint_overrides or {}).items()})
        payload.update({f"fa_declarations-{key}": value for key, value in (declaration_overrides or {}).items()})
        return payload

    def _declarations(self):
        return orm_models.PlayerFreeAgentDeclaration.objects.filter(player_id=self.player.id).count()

    def test_an_unchanged_submission_is_accepted(self):
        response = self.client.post(self.url, self._payload())

        self.assertEqual(response.status_code, 302)

    def test_deleting_the_declaration_behind_a_free_agent_signing_is_an_input_error(self):
        response = self.client.post(self.url, self._payload(declaration_overrides={"0-DELETE": "on"}))

        self.assertContains(response, "FA を宣言した記録がありません")
        self.assertEqual(self._declarations(), 1)

    def test_changing_the_declaration_year_away_from_the_signing_is_an_input_error(self):
        response = self.client.post(self.url, self._payload(declaration_overrides={"0-year": "2022"}))

        self.assertContains(response, "FA を宣言した記録がありません")
        self.assertEqual(orm_models.PlayerFreeAgentDeclaration.objects.get(id=self.declaration.id).year, 2024)

    def test_deleting_the_stint_that_covers_the_declaration_year_is_an_input_error(self):
        response = self.client.post(self.url, self._payload(stint_overrides={"1-DELETE": "on"}))

        self.assertContains(response, "どの球団にも在籍していない")
        self.assertEqual(orm_models.PlayerStint.objects.filter(player_id=self.player.id).count(), 2)

    def test_shortening_the_stint_so_it_no_longer_covers_the_year_is_an_input_error(self):
        response = self.client.post(self.url, self._payload(stint_overrides={"1-to_year": "2022"}))

        self.assertContains(response, "どの球団にも在籍していない")
        self.assertEqual(orm_models.PlayerStint.objects.get(team=self.team, player_id=self.player.id).to_year, 2024)

    def test_removing_both_the_signing_and_its_declaration_is_accepted(self):
        response = self.client.post(
            self.url, self._payload(stint_overrides={"0-DELETE": "on"}, declaration_overrides={"0-DELETE": "on"})
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._declarations(), 0)

    def test_the_stint_admin_refuses_to_delete_a_stint_a_declaration_depends_on(self):
        stint = orm_models.PlayerStint.objects.get(team=self.team, player_id=self.player.id)

        response = self.client.post(f"/admin/myapp/playerstint/{stint.id}/delete/", {"post": "yes"})

        # 削除の導線ごと出さない（403）。「削除しました」とも出ない
        self.assertEqual(response.status_code, 403)
        self.assertTrue(orm_models.PlayerStint.objects.filter(id=stint.id).exists())

    def test_the_stint_admin_hides_the_delete_button_for_such_a_stint(self):
        stint = orm_models.PlayerStint.objects.get(team=self.team, player_id=self.player.id)
        free = orm_models.PlayerStint.objects.get(team=self.rival, player_id=self.player.id)
        # rival の在籍（FA 入団）は、消しても宣言（2024年の在籍が team にある）は崩れない
        self.assertNotContains(self.client.get(f"/admin/myapp/playerstint/{stint.id}/change/"), "/delete/")
        self.assertEqual(self.client.get(f"/admin/myapp/playerstint/{free.id}/delete/").status_code, 200)

    def test_bulk_delete_with_a_blocked_stint_deletes_nothing_and_says_why(self):
        stint = orm_models.PlayerStint.objects.get(team=self.team, player_id=self.player.id)
        free = orm_models.PlayerStint.objects.get(team=self.rival, player_id=self.player.id)

        response = self.client.post(
            "/admin/myapp/playerstint/",
            {"action": "delete_selected", "_selected_action": [stint.id, free.id], "post": "yes"},
            follow=True,
        )

        self.assertContains(response, "何も削除しませんでした")
        self.assertNotContains(response, "削除されました")
        self.assertEqual(orm_models.PlayerStint.objects.filter(player_id=self.player.id).count(), 2)

    def test_retiring_before_the_declaration_year_is_refused_and_saves_nothing(self):
        # 宣言（今年）の年に在籍が無くなる退団（前の年を指定）は拒否する
        player = self.service.register_player(self.team.id, "新人", 20, "投手")
        orm_models.PlayerStint.objects.filter(player_id=player.id).update(from_year=THIS_YEAR - 3)
        build_roster_service().declare_free_agency(self.team.id, player.id, THIS_YEAR, "国内")

        with self.assertRaises(InvalidFreeAgentDeclaration):
            self.service.retire_player(self.team.id, player.id, year=THIS_YEAR - 1)
        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=player.id).to_year)

    def test_transferring_before_the_declaration_year_is_refused_and_saves_nothing(self):
        player = self.service.register_player(self.team.id, "新人", 20, "投手")
        orm_models.PlayerStint.objects.filter(player_id=player.id).update(from_year=THIS_YEAR - 3)
        build_roster_service().declare_free_agency(self.team.id, player.id, THIS_YEAR, "国内")

        with self.assertRaises(InvalidFreeAgentDeclaration):
            self.service.transfer_player(
                player.id, from_team_id=self.team.id, to_team_id=self.rival.id, number=7, year=THIS_YEAR - 1
            )
        self.assertFalse(orm_models.PlayerStint.objects.filter(player_id=player.id, team=self.rival).exists())
        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=player.id, team=self.team).to_year)

    def test_retiring_before_a_declaration_ahead_of_the_clock_is_refused(self):
        """先の年の宣言は在籍が続く限り認める（時計で制限しない）。その年より前に退団させる操作は拒否し、何も保存しない。"""
        player = self.service.register_player(self.team.id, "新人", 20, "投手")
        build_roster_service().declare_free_agency(self.team.id, player.id, THIS_YEAR + 1, "国内")

        with self.assertRaises(InvalidFreeAgentDeclaration):
            self.service.retire_player(self.team.id, player.id, year=THIS_YEAR)
        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=player.id, team=self.team).to_year)

    def test_the_stint_admin_refuses_to_shorten_the_stint(self):
        stint = orm_models.PlayerStint.objects.get(team=self.team, player_id=self.player.id)

        response = self.client.post(
            f"/admin/myapp/playerstint/{stint.id}/change/",
            {
                "player": self.player.id,
                "team": self.team.id,
                "number": stint.number,
                "from_year": 2015,
                "to_year": 2022,
                "signed_as": "支配下",
                "promoted_year": "",
                "number_before_promotion": "",
                "acquired_via": "",
            },
        )

        self.assertContains(response, "どの球団にも在籍していない")
        self.assertEqual(orm_models.PlayerStint.objects.get(id=stint.id).to_year, 2024)

    def test_the_stint_admin_rejects_re_signing_with_the_same_team_as_a_free_agent(self):
        # 同じ球団で在籍を結び直して経路 FA
        response = self.client.post(
            "/admin/myapp/playerstint/add/",
            {
                "player": self.player.id,
                "team": self.team.id,
                "number": 88,
                "from_year": 2026,
                "to_year": "",
                "signed_as": "支配下",
                "promoted_year": "",
                "number_before_promotion": "",
                "acquired_via": "FA",
            },
        )

        self.assertContains(response, "FA を宣言した記録がありません")
        self.assertFalse(orm_models.PlayerStint.objects.filter(player_id=self.player.id, from_year=2026).exists())
