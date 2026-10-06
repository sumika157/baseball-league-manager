"""入団の経路と FA 宣言。リポジトリの往復・管理画面の検査・編集画面の追加と削除・個人ページ・戦力分析の入退団。"""

from datetime import date

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.admin import FreeAgentDeclarationForm
from myapp.domain.exceptions import InvalidAcquisition, InvalidFreeAgentDeclaration
from myapp.domain.value_objects import AcquisitionRoute, FreeAgencyKind
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoTeamRepository

from ..helpers import build_roster_service, build_team_analysis_service, login_as_manager
from .base import BaseCase

THIS_YEAR = date.today().year
DEVELOPMENTAL = "育成"


class RouteRepositoryTest(BaseCase):
    def test_route_round_trips(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手", acquired_via_label="ドラフト")

        stint = DjangoTeamRepository().find_by_id(self.team.id).current_stint(player)

        self.assertIs(stint.acquired_via, AcquisitionRoute.DRAFT)
        self.assertEqual(orm_models.PlayerStint.objects.get(player_id=player.id).acquired_via, "ドラフト")

    def test_unknown_route_is_stored_as_null_and_read_back_as_none(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        stint = DjangoTeamRepository().find_by_id(self.team.id).current_stint(player)

        self.assertIsNone(stint.acquired_via)
        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=player.id).acquired_via)

    def test_a_route_that_conflicts_with_the_contract_is_rejected_and_not_saved(self):
        with self.assertRaises(InvalidAcquisition):
            self.service.register_player(self.team.id, "山田", 10, "内野手", acquired_via_label="育成ドラフト")
        self.assertFalse(orm_models.Player.objects.exists())

    def test_developmental_draft_with_a_developmental_contract(self):
        player = self.service.register_player(
            self.team.id, "育成", 120, "内野手", contract_label=DEVELOPMENTAL, acquired_via_label="育成ドラフト"
        )

        self.assertEqual(orm_models.PlayerStint.objects.get(player_id=player.id).acquired_via, "育成ドラフト")

    def test_a_free_agent_signing_needs_a_declaration(self):
        with self.assertRaises(InvalidAcquisition):
            self.service.register_player(self.team.id, "山田", 10, "内野手", acquired_via_label="FA")

    def test_transfer_carries_the_route_to_the_new_stint(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.service.transfer_player(
            player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=THIS_YEAR,
            acquired_via_label="トレード",
        )

        self.assertEqual(
            orm_models.PlayerStint.objects.get(player_id=player.id, team=self.rival).acquired_via, "トレード"
        )
        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=player.id, team=self.team).acquired_via)

    def test_transfer_as_a_free_agent_without_a_declaration_is_rejected_and_not_saved(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        with self.assertRaises(InvalidAcquisition):
            self.service.transfer_player(
                player.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=7,
                year=THIS_YEAR,
                acquired_via_label="FA",
            )

        self.assertIsNone(orm_models.PlayerStint.objects.get(player_id=player.id, team=self.team).to_year)
        self.assertFalse(orm_models.PlayerStint.objects.filter(team=self.rival).exists())

    def test_transfer_as_a_free_agent_after_declaring(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        build_roster_service().declare_free_agency(self.team.id, player.id, THIS_YEAR, "国内")

        self.service.transfer_player(
            player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=THIS_YEAR + 1,
            acquired_via_label="FA",
        )

        self.assertEqual(orm_models.PlayerStint.objects.get(player_id=player.id, team=self.rival).acquired_via, "FA")
        # 宣言は選手が持つので、移籍したあとの球団から読んでも残っている
        moved = DjangoTeamRepository().find_by_id(self.rival.id).find_player(player.id)
        self.assertEqual([d.year for d in moved.fa_declarations], [THIS_YEAR])


class DeclarationRepositoryTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.roster = build_roster_service()

    def _declarations(self):
        return list(orm_models.PlayerFreeAgentDeclaration.objects.filter(player_id=self.player.id))

    def test_declaration_round_trips(self):
        self.roster.declare_free_agency(self.team.id, self.player.id, THIS_YEAR, "海外")

        rows = self._declarations()
        self.assertEqual([(r.year, r.kind) for r in rows], [(THIS_YEAR, "海外")])
        loaded = DjangoTeamRepository().find_by_id(self.team.id).find_player(self.player.id)
        self.assertEqual([(d.year, d.kind) for d in loaded.fa_declarations], [(THIS_YEAR, FreeAgencyKind.OVERSEAS)])

    def test_saving_again_does_not_duplicate(self):
        self.roster.declare_free_agency(self.team.id, self.player.id, THIS_YEAR, "国内")

        self.service.update_player(self.team.id, self.player.id, name="山田", number=10, position_label="内野手")

        self.assertEqual(len(self._declarations()), 1)

    def test_removing_deletes_the_row(self):
        self.roster.declare_free_agency(self.team.id, self.player.id, THIS_YEAR, "国内")

        self.roster.remove_free_agency_declaration(self.team.id, self.player.id, THIS_YEAR)

        self.assertEqual(self._declarations(), [])

    def test_the_same_year_twice_is_rejected(self):
        self.roster.declare_free_agency(self.team.id, self.player.id, THIS_YEAR, "国内")

        with self.assertRaises(InvalidFreeAgentDeclaration):
            self.roster.declare_free_agency(self.team.id, self.player.id, THIS_YEAR, "海外")
        self.assertEqual([r.kind for r in self._declarations()], ["国内"])

    def test_a_year_without_a_stint_is_rejected(self):
        with self.assertRaises(InvalidFreeAgentDeclaration):
            self.roster.declare_free_agency(self.team.id, self.player.id, THIS_YEAR - 30, "国内")
        self.assertEqual(self._declarations(), [])

    def test_an_unknown_kind_is_rejected(self):
        with self.assertRaises(InvalidFreeAgentDeclaration):
            self.roster.declare_free_agency(self.team.id, self.player.id, THIS_YEAR, "月面")

    def test_the_database_allows_one_declaration_per_player_and_year(self):
        from django.db import IntegrityError, transaction

        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.player.id, year=2024, kind="国内")

        with self.assertRaises(IntegrityError), transaction.atomic():
            orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.player.id, year=2024, kind="海外")

    def test_loading_a_roster_does_not_query_per_player(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                DjangoTeamRepository().find_by_id(self.team.id)
            return len(captured)

        before = count()
        for number in range(11, 31):
            extra = self.service.register_player(self.team.id, f"選手{number}", number, "内野手")
            orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=extra.id, year=2024, kind="国内")

        self.assertEqual(count(), before)


class AdminAcquisitionTest(BaseCase):
    """管理画面もドメインの規則で、経路と区分・FA 宣言との食い違いを弾く。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.other = self.service.register_player(self.team.id, "田中", 11, "外野手")

    def _add_stint(self, **overrides):
        payload = {
            "player": self.other.id,
            "team": self.rival.id,
            "number": "55",
            "from_year": "2020",
            "to_year": "2022",
            "signed_as": "支配下",
            "promoted_year": "",
            "number_before_promotion": "",
            "acquired_via": "",
        }
        payload.update(overrides)
        return self.client.post("/admin/myapp/playerstint/add/", payload)

    def _count(self):
        return orm_models.PlayerStint.objects.filter(team=self.rival).count()

    def test_a_blank_route_is_unknown(self):
        response = self._add_stint()

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(orm_models.PlayerStint.objects.get(team=self.rival).acquired_via)

    def test_the_route_is_saved(self):
        self._add_stint(acquired_via="ドラフト")

        self.assertEqual(orm_models.PlayerStint.objects.get(team=self.rival).acquired_via, "ドラフト")

    def test_developmental_draft_with_a_registered_contract_is_rejected(self):
        response = self._add_stint(acquired_via="育成ドラフト")

        self.assertContains(response, "育成ドラフトで入団した選手は")
        self.assertEqual(self._count(), 0)

    def test_draft_with_a_developmental_contract_is_rejected(self):
        response = self._add_stint(acquired_via="ドラフト", signed_as="育成", number="150")

        self.assertContains(response, "ドラフトで入団した選手は")
        self.assertEqual(self._count(), 0)

    def test_a_free_agent_signing_without_a_declaration_is_rejected(self):
        response = self._add_stint(acquired_via="FA")

        self.assertContains(response, "FA を宣言した記録がありません")
        self.assertEqual(self._count(), 0)

    def test_a_free_agent_signing_with_a_saved_declaration_is_accepted(self):
        # 宣言した球団（team）の在籍を2019年までにして、2019年の宣言のあと rival に2020年に加入する
        orm_models.PlayerStint.objects.filter(player_id=self.other.id).update(from_year=2015, to_year=2019)
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.other.id, year=2019, kind="国内")

        response = self._add_stint(acquired_via="FA", from_year="2020", to_year="2022")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.PlayerStint.objects.get(team=self.rival).acquired_via, "FA")

    def test_the_player_page_has_the_declaration_inline_and_the_route_column(self):
        response = self.client.get(f"/admin/myapp/player/{self.player.id}/change/")

        self.assertContains(response, "FA 宣言")
        self.assertContains(response, "入団の経路")

    def _declaration_form(self, **overrides):
        data = {"player": self.player.id, "year": THIS_YEAR, "kind": "国内"}
        data.update(overrides)
        return FreeAgentDeclarationForm(data=data)

    def test_a_declaration_in_a_year_with_a_stint_is_valid(self):
        self.assertTrue(self._declaration_form().is_valid())

    def test_a_declaration_without_a_stint_in_that_year_is_invalid(self):
        form = self._declaration_form(year=THIS_YEAR - 30)

        self.assertFalse(form.is_valid())
        self.assertIn("どの球団にも在籍していない", str(form.errors))

    def test_a_second_declaration_in_the_same_year_is_invalid(self):
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.player.id, year=THIS_YEAR, kind="国内")

        form = self._declaration_form(kind="海外")

        self.assertFalse(form.is_valid())
        self.assertIn("すでに FA を宣言", str(form.errors))

    def test_editing_a_saved_declaration_does_not_collide_with_itself(self):
        saved = orm_models.PlayerFreeAgentDeclaration.objects.create(
            player_id=self.player.id, year=THIS_YEAR, kind="国内"
        )

        form = FreeAgentDeclarationForm(
            data={"player": self.player.id, "year": THIS_YEAR, "kind": "海外"}, instance=saved
        )

        self.assertTrue(form.is_valid())


class DeclarationScreenTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.edit_url = reverse("player_edit", args=[self.team.id, self.player.id])

    def _declarations(self):
        return [
            (r.year, r.kind) for r in orm_models.PlayerFreeAgentDeclaration.objects.filter(player_id=self.player.id)
        ]

    def test_the_manager_sees_the_form_with_kinds_from_the_domain(self):
        login_as_manager(self.client, self.team)

        response = self.client.get(self.edit_url)

        self.assertContains(response, "FA を宣言する")
        for label in FreeAgencyKind.labels():
            self.assertContains(response, f'<option value="{label}">{label}FA</option>')

    def test_declaring_from_the_edit_screen(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(self.edit_url, {"declare_fa": "1", "fa_year": str(THIS_YEAR), "fa_kind": "海外"})

        self.assertRedirects(response, self.edit_url)
        self.assertEqual(self._declarations(), [(THIS_YEAR, "海外")])
        self.assertContains(self.client.get(self.edit_url), f"{THIS_YEAR}年 海外FA")

    def test_removing_from_the_edit_screen(self):
        login_as_manager(self.client, self.team)
        build_roster_service().declare_free_agency(self.team.id, self.player.id, THIS_YEAR, "国内")

        response = self.client.post(self.edit_url, {"remove_fa": str(THIS_YEAR)})

        self.assertRedirects(response, self.edit_url)
        self.assertEqual(self._declarations(), [])

    def test_a_domain_violation_shows_an_error_and_saves_nothing(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(
            self.edit_url, {"declare_fa": "1", "fa_year": str(THIS_YEAR - 30), "fa_kind": "国内"}, follow=True
        )

        self.assertContains(response, "どの球団にも在籍していない")
        self.assertEqual(self._declarations(), [])

    def test_a_non_numeric_year_shows_an_error(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(self.edit_url, {"declare_fa": "1", "fa_year": "", "fa_kind": "国内"}, follow=True)

        self.assertContains(response, "数値で入力してください")
        self.assertEqual(self._declarations(), [])

    def test_the_other_teams_manager_gets_403(self):
        login_as_manager(self.client, self.rival)

        response = self.client.post(self.edit_url, {"declare_fa": "1", "fa_year": str(THIS_YEAR), "fa_kind": "国内"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._declarations(), [])

    def test_anonymous_is_sent_to_login_and_saves_nothing(self):
        response = self.client.post(self.edit_url, {"declare_fa": "1", "fa_year": str(THIS_YEAR), "fa_kind": "国内"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(self._declarations(), [])

    def test_anonymous_sees_no_declaration_controls_on_the_profile(self):
        build_roster_service().declare_free_agency(self.team.id, self.player.id, THIS_YEAR, "国内")

        response = self.client.get(reverse("player_detail", args=[self.team.id, self.player.id]))

        self.assertNotContains(response, "FA を宣言する")
        self.assertNotContains(response, "取り消す")


class ProfileDisplayTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.roster = build_roster_service()

    def _page(self, player):
        return self.client.get(reverse("player_detail", args=[self.team.id, player.id]))

    def test_the_career_row_shows_the_route(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手", acquired_via_label="ドラフト")

        self.assertContains(self._page(player), "ドラフトで入団")

    def test_an_unknown_route_shows_nothing(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.assertNotContains(self._page(player), "で入団")

    def test_a_declaration_without_a_move_is_shown_as_a_stay(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.roster.declare_free_agency(self.team.id, player.id, THIS_YEAR, "国内")

        response = self._page(player)

        self.assertContains(response, f"{THIS_YEAR}年 国内FA")
        self.assertContains(response, "残留")
        self.assertNotContains(response, ">移籍</span>")

    def test_a_declaration_followed_by_a_move_is_shown_as_a_move(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.roster.declare_free_agency(self.team.id, player.id, THIS_YEAR, "海外")
        self.service.transfer_player(
            player.id, from_team_id=self.team.id, to_team_id=self.rival.id, number=7, year=THIS_YEAR + 1
        )

        response = self.client.get(reverse("player_detail", args=[self.rival.id, player.id]))

        self.assertContains(response, f"{THIS_YEAR}年 海外FA")
        self.assertContains(response, ">移籍</span>")

    def test_no_declaration_means_no_section(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.assertNotContains(self._page(player), "FA 宣言")


class MovesDisplayTest(BaseCase):
    """戦力分析の入退団。加入の表に経路、退団の表に FA 宣言して移籍した印。"""

    YEAR = 2025

    def setUp(self):
        super().setUp()
        self.roster = build_roster_service()
        # 年を選べるよう、その年に記録済みの試合（先発1人）を作っておく
        self._with_a_recorded_game()

    def _player(self, team, name, number, from_year, to_year=None, route=None):
        created = self.service.register_player(team.id, name, number, "内野手")
        orm_models.PlayerStint.objects.filter(player_id=created.id).update(
            from_year=from_year, to_year=to_year, acquired_via=route
        )
        return created

    def _analysis(self, team=None):
        return build_team_analysis_service().get_analysis((team or self.team).id, year=self.YEAR)

    def _with_a_recorded_game(self):
        from myapp.domain.entities import Game
        from myapp.domain.value_objects import PitchingLine, Season
        from myapp.infrastructure.repositories import DjangoGameRepository

        starter = self._player(self.team, "先発", 1, 2020)
        game = Game(
            season=Season(self.YEAR),
            played_on=date(self.YEAR, 4, 1),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=1,
            away_score=0,
        )
        game.record_pitching(starter.id, PitchingLine(), appearance_order=1)
        DjangoGameRepository().save(game)

    def test_joiners_carry_the_route_and_unknown_is_empty(self):
        self._player(self.team, "ドラフト組", 20, self.YEAR, route="ドラフト")
        self._player(self.team, "経路不明", 21, self.YEAR)

        rows = {r.name: r.acquired_via_label for r in self._analysis().joiners}

        self.assertEqual(rows, {"ドラフト組": "ドラフト", "経路不明": ""})

    def test_the_joiners_table_shows_a_dash_for_an_unknown_route(self):
        self._player(self.team, "ドラフト組", 20, self.YEAR, route="ドラフト")
        self._player(self.team, "経路不明", 21, self.YEAR)

        response = self.client.get(reverse("team_analysis", args=[self.team.id]), {"year": self.YEAR})

        self.assertContains(response, "<th>経路</th>")
        self.assertContains(response, "<td>ドラフト</td>")
        self.assertContains(response, "<td>—</td>")

    def _leave_via_fa(self, kind):
        leaver = self._player(self.team, "FA移籍", 30, 2020, self.YEAR)
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=leaver.id, year=self.YEAR, kind=kind)
        orm_models.PlayerStint.objects.create(
            player_id=leaver.id, team=self.rival, number=7, from_year=self.YEAR + 1, acquired_via="FA"
        )
        return leaver

    def test_a_leaver_who_declared_and_moved_is_marked(self):
        self._leave_via_fa("国内")

        [row] = self._analysis().leavers

        self.assertEqual((row.kind_label, row.fa_label), ("移籍", "国内FA"))

    def test_a_trade_after_a_declared_stay_is_not_marked_as_free_agency(self):
        # 宣言して残留したが、翌年に経路がトレードの在籍が別の球団で始まった。宣言の結果とは見なさない
        leaver = self._player(self.team, "宣言残留後にトレード", 35, 2020, self.YEAR)
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=leaver.id, year=self.YEAR, kind="国内")
        orm_models.PlayerStint.objects.create(
            player_id=leaver.id, team=self.rival, number=7, from_year=self.YEAR + 1, acquired_via="トレード"
        )

        [row] = self._analysis().leavers

        self.assertEqual((row.kind_label, row.fa_label), ("移籍", ""))

    def test_the_overseas_kind_is_told_apart(self):
        self._leave_via_fa("海外")

        self.assertEqual(self._analysis().leavers[0].fa_label, "海外FA")

    def test_a_departure_without_a_declaration_is_not_marked(self):
        self._player(self.team, "ただの退団", 31, 2020, self.YEAR)
        mover = self._player(self.team, "ただの移籍", 32, 2020, self.YEAR)
        orm_models.PlayerStint.objects.create(player_id=mover.id, team=self.rival, number=7, from_year=self.YEAR)

        self.assertEqual({r.fa_label for r in self._analysis().leavers}, {""})

    def test_a_declaration_followed_by_a_departure_without_a_move_is_not_marked(self):
        leaver = self._player(self.team, "宣言して退団", 33, 2020, self.YEAR)
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=leaver.id, year=self.YEAR, kind="国内")

        [row] = self._analysis().leavers

        self.assertEqual((row.kind_label, row.fa_label), ("退団", ""))

    def test_the_departures_table_shows_the_mark(self):
        self._leave_via_fa("国内")

        response = self.client.get(reverse("team_analysis", args=[self.team.id]), {"year": self.YEAR})

        self.assertContains(response, ">国内FA</span>")

    def test_a_stay_after_declaring_is_not_listed_as_a_departure(self):
        stayer = self._player(self.team, "宣言残留", 34, 2020)
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=stayer.id, year=self.YEAR, kind="国内")

        analysis = self._analysis()

        self.assertEqual([r.name for r in analysis.leavers], [])

    def test_query_count_does_not_grow_with_declarations(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self._analysis()
            return len(captured)

        self._leave_via_fa("国内")
        before = count()
        for number in range(40, 60):
            leaver = self._player(self.team, f"退団{number}", number, 2020, self.YEAR)
            orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=leaver.id, year=self.YEAR, kind="国内")
            orm_models.PlayerStint.objects.create(
                player_id=leaver.id, team=self.rival, number=number, from_year=self.YEAR + 1, acquired_via="FA"
            )

        self.assertEqual(count(), before)
