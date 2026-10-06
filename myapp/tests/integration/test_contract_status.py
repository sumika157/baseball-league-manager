"""契約区分（支配下／育成）。リポジトリの往復・登録と昇格の画面・管理画面の検査・育成のバッジ。"""

from importlib import import_module

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.exceptions import InvalidContract, RegisteredPlayerLimitExceeded
from myapp.domain.value_objects import ContractStatus
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoLeagueRepository, DjangoTeamRepository

from ..helpers import build_roster_service, login_as_manager
from .base import BaseCase

DEVELOPMENTAL = ContractStatus.DEVELOPMENTAL.value


class ContractRepositoryTest(BaseCase):
    def test_signing_status_and_promotion_year_round_trip(self):
        player = self.service.register_player(self.team.id, "育成", 120, "投手", contract_label=DEVELOPMENTAL)
        build_roster_service().promote_player(self.team.id, player.id, 30, year=2026)

        team = DjangoTeamRepository().find_by_id(self.team.id)
        stint = team.current_stint(team.find_player(player.id))
        self.assertIs(stint.signed_as, ContractStatus.DEVELOPMENTAL)
        self.assertEqual(stint.promoted_year, 2026)
        self.assertEqual(stint.number_before_promotion.value, "120")
        self.assertEqual(stint.number.value, "30")
        row = orm_models.PlayerStint.objects.get(id=stint.id)
        self.assertEqual((row.signed_as, row.promoted_year), ("育成", 2026))
        self.assertEqual(row.number_before_promotion, "120")

    def test_league_limit_round_trips(self):
        self.assertEqual(DjangoLeagueRepository().find_by_id(self.league.id).registered_player_limit, 70)

        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=None)

        self.assertIsNone(DjangoLeagueRepository().find_by_id(self.league.id).registered_player_limit)


class ContractBackfillTest(BaseCase):
    """データマイグレーション 0039: 背番号が100以上の既存の在籍を育成にする。"""

    def test_three_digit_numbers_become_developmental(self):
        two_digit = self.service.register_player(self.team.id, "支配下", 10, "内野手")
        three_digit = self.service.register_player(self.team.id, "育成", 100, "内野手", contract_label=DEVELOPMENTAL)
        # 区分を持つ前のデータ（すべて既定の支配下）に戻す
        orm_models.PlayerStint.objects.update(signed_as="支配下")

        import_module("myapp.migrations.0039_backfill_stint_contract_status").backfill(django_apps, None)

        signed = {s.player_id: s.signed_as for s in orm_models.PlayerStint.objects.all()}
        self.assertEqual(signed[two_digit.id], "支配下")
        self.assertEqual(signed[three_digit.id], "育成")


class ContractServiceTest(BaseCase):
    def test_developmental_player_does_not_count_towards_the_limit(self):
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=1)
        self.service.register_player(self.team.id, "支配下", 10, "内野手")

        self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            self.service.register_player(self.team.id, "もう一人", 11, "内野手")
        self.assertFalse(orm_models.Player.objects.filter(name="もう一人").exists())

    def test_a_number_that_does_not_match_the_contract_is_rejected(self):
        with self.assertRaises(InvalidContract):
            self.service.register_player(self.team.id, "育成", 50, "内野手", contract_label=DEVELOPMENTAL)

    def test_transfer_receives_into_the_limit(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=1)
        self.service.register_player(self.rival.id, "先客", 5, "内野手")

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            self.service.transfer_player(player.id, from_team_id=self.team.id, to_team_id=self.rival.id, number=7)

        # 検査に落ちたら元の在籍も閉じない
        self.assertTrue(orm_models.PlayerStint.objects.get(player_id=player.id, team=self.team).to_year is None)

    def test_transfer_keeps_a_developmental_contract(self):
        player = self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)

        self.service.transfer_player(
            player.id, from_team_id=self.team.id, to_team_id=self.rival.id, number=130, year=2026
        )

        stint = orm_models.PlayerStint.objects.get(player_id=player.id, team=self.rival)
        self.assertEqual(stint.signed_as, "育成")

    def test_transfer_rejects_a_number_that_does_not_match_the_contract(self):
        player = self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)

        with self.assertRaises(InvalidContract):
            self.service.transfer_player(player.id, from_team_id=self.team.id, to_team_id=self.rival.id, number=7)

    def test_promotion_over_the_limit_is_rejected_and_not_saved(self):
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=1)
        self.service.register_player(self.team.id, "支配下", 10, "内野手")
        player = self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            build_roster_service().promote_player(self.team.id, player.id, 30)

        stint = orm_models.PlayerStint.objects.get(player_id=player.id)
        self.assertEqual((stint.number, stint.promoted_year), ("120", None))

    def _fill_the_limit(self, team):
        """上限ちょうどまで支配下を入れる（上限は1）。"""
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=1)
        self.service.register_player(team.id, "先客", 5, "内野手")

    def test_a_developmental_player_can_be_added_to_a_full_team(self):
        self._fill_the_limit(self.team)

        self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)

        self.assertTrue(orm_models.Player.objects.filter(name="育成").exists())

    def test_a_developmental_player_can_transfer_into_a_full_team(self):
        player = self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)
        self._fill_the_limit(self.rival)

        self.service.transfer_player(
            player.id, from_team_id=self.team.id, to_team_id=self.rival.id, number=130, year=2026
        )

        self.assertTrue(orm_models.PlayerStint.objects.filter(player_id=player.id, team=self.rival).exists())

    def test_a_registered_player_cannot_be_added_to_a_full_team(self):
        self._fill_the_limit(self.team)

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            self.service.register_player(self.team.id, "もう一人", 11, "内野手")

    def test_promotion_into_a_full_team_is_rejected(self):
        player = self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)
        self._fill_the_limit(self.team)

        with self.assertRaises(RegisteredPlayerLimitExceeded):
            build_roster_service().promote_player(self.team.id, player.id, 30)

    def test_promotion_keeps_the_number_worn_before(self):
        player = self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)

        build_roster_service().promote_player(self.team.id, player.id, 30, year=2026)

        row = orm_models.PlayerStint.objects.get(player_id=player.id)
        self.assertEqual((row.number, row.number_before_promotion), ("30", "120"))


class ContractScreenTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.developmental = self.service.register_player(
            self.team.id, "育成太郎", 120, "投手", contract_label=DEVELOPMENTAL
        )
        self.registered = self.service.register_player(self.team.id, "支配下次郎", 10, "内野手")
        self.edit_url = reverse("player_edit", args=[self.team.id, self.developmental.id])

    def test_registration_form_offers_both_contracts_from_the_domain(self):
        login_as_manager(self.client, self.team)

        response = self.client.get(reverse("player_list", args=[self.team.id]))

        for label in ContractStatus.labels():
            self.assertContains(response, f'<option value="{label}"')

    def test_registering_a_developmental_player_from_the_form(self):
        login_as_manager(self.client, self.team)

        self.client.post(
            reverse("player_list", args=[self.team.id]),
            {"name": "新人育成", "number": "130", "position": "投手", "contract": DEVELOPMENTAL},
        )

        stint = orm_models.PlayerStint.objects.get(player__name="新人育成")
        self.assertEqual((stint.signed_as, stint.number), ("育成", "130"))

    def test_registering_without_a_contract_means_registered(self):
        login_as_manager(self.client, self.team)

        self.client.post(
            reverse("player_list", args=[self.team.id]),
            {"name": "新人", "number": "40", "position": "投手"},
        )

        self.assertEqual(orm_models.PlayerStint.objects.get(player__name="新人").signed_as, "支配下")

    def test_a_mismatched_number_shows_an_error(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(
            reverse("player_list", args=[self.team.id]),
            {"name": "新人育成", "number": "40", "position": "投手", "contract": DEVELOPMENTAL},
            follow=True,
        )

        self.assertContains(response, "育成選手の背番号は100以上")
        self.assertFalse(orm_models.Player.objects.filter(name="新人育成").exists())

    def test_badge_marks_developmental_players_in_the_list_and_on_their_page(self):
        list_page = self.client.get(reverse("player_list", args=[self.team.id]) + "?pos=pitcher")
        detail = self.client.get(reverse("player_detail", args=[self.team.id, self.developmental.id]))
        registered_detail = self.client.get(reverse("player_detail", args=[self.team.id, self.registered.id]))

        self.assertContains(list_page, 'class="badge bg-primary ms-1">育成</span>', count=1)
        self.assertContains(detail, 'class="badge bg-primary ms-1">育成</span>')
        self.assertNotContains(registered_detail, ">育成</span>")

    def test_promotion_button_is_for_developmental_players_of_the_managers_team_only(self):
        login_as_manager(self.client, self.team)

        shown = self.client.get(self.edit_url)
        hidden = self.client.get(reverse("player_edit", args=[self.team.id, self.registered.id]))

        self.assertContains(shown, "支配下登録する")
        self.assertNotContains(hidden, "支配下登録する")

    def test_promoting_from_the_edit_screen(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(self.edit_url, {"promote": "1", "promote_number": "35"})

        self.assertRedirects(response, self.edit_url)
        stint = orm_models.PlayerStint.objects.get(player_id=self.developmental.id)
        self.assertEqual(stint.number, "35")
        self.assertIsNotNone(stint.promoted_year)
        self.assertNotContains(self.client.get(self.edit_url), "支配下登録する")

    def test_promoting_to_a_three_digit_number_shows_an_error(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(self.edit_url, {"promote": "1", "promote_number": "135"}, follow=True)

        self.assertContains(response, "支配下選手の背番号は00・0〜99")
        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=self.developmental.id).promoted_year)

    def test_promoting_without_a_number_shows_an_error(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(self.edit_url, {"promote": "1", "promote_number": ""}, follow=True)

        self.assertContains(response, "背番号は 0・00・1〜999 の数字で入力してください")

    def test_other_teams_manager_cannot_promote(self):
        login_as_manager(self.client, self.rival)

        response = self.client.post(self.edit_url, {"promote": "1", "promote_number": "35"})

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=self.developmental.id).promoted_year)

    def test_anonymous_cannot_promote(self):
        response = self.client.post(self.edit_url, {"promote": "1", "promote_number": "35"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=self.developmental.id).promoted_year)


class AdminContractValidationTest(BaseCase):
    """管理画面も、区分と背番号・昇格の年・支配下の上限をドメインの規則で検査する。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.other = self.service.register_player(self.team.id, "田中", 11, "外野手")

    def _add(self, **overrides):
        payload = {
            "player": self.other.id,
            "team": self.rival.id,
            "number": "55",
            "from_year": "2020",
            "to_year": "2022",
            "signed_as": "支配下",
            "promoted_year": "",
            "number_before_promotion": "",
        }
        payload.update(overrides)
        return self.client.post("/admin/myapp/playerstint/add/", payload)

    def _count(self):
        return orm_models.PlayerStint.objects.filter(team=self.rival).count()

    def test_developmental_with_a_two_digit_number_is_rejected(self):
        response = self._add(signed_as="育成")

        self.assertContains(response, "育成選手の背番号は100以上")
        self.assertEqual(self._count(), 0)

    def test_registered_with_a_three_digit_number_is_rejected(self):
        response = self._add(number="150")

        self.assertContains(response, "支配下選手の背番号は00・0〜99")
        self.assertEqual(self._count(), 0)

    def test_matching_developmental_stint_is_saved(self):
        self._add(signed_as="育成", number="150")

        self.assertEqual(orm_models.PlayerStint.objects.get(team=self.rival).signed_as, "育成")

    def test_a_blank_contract_means_registered(self):
        response = self._add(signed_as="")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.PlayerStint.objects.get(team=self.rival).signed_as, "支配下")

    def test_promotion_year_outside_the_stint_is_rejected(self):
        response = self._add(signed_as="育成", number="30", promoted_year="2025")

        self.assertContains(response, "支配下登録の年が退団年より後")
        self.assertEqual(self._count(), 0)

    def test_promotion_year_on_a_registered_signing_is_rejected(self):
        response = self._add(promoted_year="2021")

        self.assertContains(response, "支配下で加入した選手に")
        self.assertEqual(self._count(), 0)

    def test_current_registered_stint_over_the_limit_is_rejected(self):
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=1)
        self.service.register_player(self.rival.id, "先客", 5, "内野手")

        response = self._add(to_year="")

        self.assertContains(response, "支配下選手登録数が上限")
        self.assertEqual(self._count(), 1)

    def test_current_developmental_stint_is_not_counted_against_the_limit(self):
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=1)
        self.service.register_player(self.rival.id, "先客", 5, "内野手")

        self._add(to_year="", signed_as="育成", number="150")

        self.assertEqual(self._count(), 2)

    def test_editing_an_already_counted_stint_is_not_over_the_limit(self):
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=2)
        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)

        response = self.client.post(
            f"/admin/myapp/playerstint/{stint.id}/change/",
            {
                "player": self.player.id,
                "team": self.team.id,
                "number": "12",
                "from_year": stint.from_year,
                "to_year": "",
                "signed_as": "支配下",
                "promoted_year": "",
                "number_before_promotion": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.PlayerStint.objects.get(id=stint.id).number, "12")

    def test_promoting_in_the_admin_over_the_limit_is_rejected(self):
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=2)
        developmental = self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)
        stint = orm_models.PlayerStint.objects.get(player_id=developmental.id)

        response = self.client.post(
            f"/admin/myapp/playerstint/{stint.id}/change/",
            {
                "player": developmental.id,
                "team": self.team.id,
                "number": "30",
                "from_year": stint.from_year,
                "to_year": "",
                "signed_as": "育成",
                "promoted_year": stint.from_year,
                "number_before_promotion": "120",
            },
        )

        self.assertContains(response, "支配下選手登録数が上限")
        self.assertIsNone(orm_models.PlayerStint.objects.get(id=stint.id).promoted_year)

    def test_league_admin_edits_the_limit(self):
        response = self.client.get(f"/admin/myapp/league/{self.league.id}/change/")

        self.assertContains(response, "registered_player_limit")

    def test_a_number_worn_before_promotion_can_be_reused_by_the_promoted_player(self):
        """2015〜2023 に 50 を着けた人がいても、2025 に 50 で昇格した選手を保存し直せる。"""
        earlier = orm_models.Player.objects.create(name="先代", position="内野手")
        orm_models.PlayerStint.objects.create(player=earlier, team=self.rival, number=50, from_year=2015, to_year=2023)
        developmental = self.service.register_player(
            self.rival.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL
        )
        orm_models.PlayerStint.objects.filter(player_id=developmental.id).update(from_year=2020)
        build_roster_service().promote_player(self.rival.id, developmental.id, 50, year=2025)
        stint = orm_models.PlayerStint.objects.get(player_id=developmental.id)

        response = self.client.post(
            f"/admin/myapp/playerstint/{stint.id}/change/",
            {
                "player": developmental.id,
                "team": self.rival.id,
                "number": "50",
                "from_year": stint.from_year,
                "to_year": "",
                "signed_as": "育成",
                "promoted_year": "2025",
                "number_before_promotion": "120",
            },
        )

        self.assertEqual(response.status_code, 302)

    def test_the_number_worn_before_promotion_is_checked_against_others(self):
        orm_models.PlayerStint.objects.create(
            player=orm_models.Player.objects.create(name="同期", position="内野手"),
            team=self.rival,
            number=120,
            from_year=2020,
            signed_as="育成",
        )

        response = self._add(
            signed_as="育成",
            number="30",
            from_year="2020",
            to_year="",
            promoted_year="2022",
            number_before_promotion="120",
        )

        self.assertContains(response, "期間が重なる同じ背番号は登録できません")
        self.assertEqual(self._count(), 1)

    def test_a_promotion_year_needs_the_number_before(self):
        response = self._add(signed_as="育成", number="30", promoted_year="2021")

        self.assertContains(response, "昇格前の背番号も入力してください")
        self.assertEqual(self._count(), 0)

    def test_the_number_before_must_be_three_digits(self):
        response = self._add(signed_as="育成", number="30", promoted_year="2021", number_before_promotion="60")

        self.assertContains(response, "育成選手の背番号は100以上")
        self.assertEqual(self._count(), 0)

    def test_an_edit_form_does_not_change_the_contract(self):
        from myapp.presentation.forms import PlayerUpdateForm

        self.assertNotIn("contract", PlayerUpdateForm().fields)

    def test_a_corrupt_stored_row_is_a_form_error_not_a_server_error(self):
        # 管理画面を通さずに書かれた、支配下加入なのに昇格の記録がある不正な行
        orm_models.PlayerStint.objects.create(
            player=orm_models.Player.objects.create(name="不正", position="内野手"),
            team=self.rival,
            number=55,
            from_year=2020,
            to_year=2022,
            promoted_year=2021,
        )

        response = self._add()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "保存済みの在籍")


class PromotionReasonTest(BaseCase):
    """昇格できない選手は、上限ちょうどでも「上限超過」ではなく本当の理由で断る。"""

    def setUp(self):
        super().setUp()
        orm_models.League.objects.filter(id=self.league.id).update(registered_player_limit=1)

    def test_a_registered_player_is_told_he_is_not_developmental(self):
        player = self.service.register_player(self.team.id, "支配下", 10, "内野手")

        with self.assertRaisesMessage(InvalidContract, "育成選手ではない"):
            build_roster_service().promote_player(self.team.id, player.id, 11)

    def test_a_player_who_left_is_told_he_is_not_on_the_team(self):
        player = self.service.register_player(self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL)
        self.service.retire_player(self.team.id, player.id)
        self.service.register_player(self.team.id, "先客", 5, "内野手")

        with self.assertRaisesMessage(InvalidContract, "在籍していない"):
            build_roster_service().promote_player(self.team.id, player.id, 30)
