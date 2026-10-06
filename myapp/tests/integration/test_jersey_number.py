"""背番号（00〜99・100〜999）の永続化と画面。「0」と「00」は別の番号で、表記のまま持ち回る。

値オブジェクトの規則（受け付ける表記・並び・区分）は `tests/domain/` が見る。
ここは、保存して読み戻しても「00」が「0」に化けないこと、入力欄・管理画面・並びが文字列の列で
崩れないこと（SQL の文字列順だと「10」が「2」より前に来る）、マイグレーションが既存の整数を文字列にすることを固定する。
"""

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse

from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import JerseyNumber, Position, RosterLimits
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoTeamRepository

from ..helpers import login_as_manager
from .base import BaseCase

REAL = WorldScope.real()


class JerseyNumberRoundTripTest(BaseCase):
    def test_double_zero_and_zero_survive_the_round_trip(self):
        repo = DjangoTeamRepository(REAL)
        team = repo.find_by_id(self.team.id)
        zero = team.add_player("零", JerseyNumber("0"), Position.INFIELDER, limits=RosterLimits.UNLIMITED)
        double_zero = team.add_player("双零", JerseyNumber("00"), Position.PITCHER, limits=RosterLimits.UNLIMITED)
        repo.save(team)

        saved = DjangoTeamRepository(REAL).find_by_id(self.team.id)

        self.assertEqual(saved.find_player(zero.id).number.value, "0")
        self.assertEqual(saved.find_player(double_zero.id).number.value, "00")
        self.assertEqual(
            sorted(orm_models.PlayerStint.objects.values_list("number", flat=True)),
            ["0", "00"],
        )

    def test_the_team_lists_players_in_jersey_order_not_text_order(self):
        for number in ["10", "2", "00", "0", "1", "100"]:
            self.service.register_player(
                self.team.id, f"選手{number}", number, "内野手", "育成" if number == "100" else "支配下"
            )

        saved = DjangoTeamRepository(REAL).find_by_id(self.team.id)

        self.assertEqual([p.number.value for p in saved.players], ["00", "0", "1", "2", "10", "100"])

    def test_the_service_refuses_a_leading_zero(self):
        from myapp.domain.exceptions import InvalidJerseyNumber

        with self.assertRaises(InvalidJerseyNumber):
            self.service.register_player(self.team.id, "山田", "01", "内野手")


class JerseyNumberScreenTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("player_list", args=[self.team.id])
        login_as_manager(self.client, self.team, username="editor")

    def test_registering_with_double_zero(self):
        self.client.post(self.url, {"name": "山田", "number": "00", "position": "内野手"})
        self.client.post(self.url, {"name": "田中", "number": "0", "position": "外野手"})

        self.assertEqual(
            sorted(orm_models.PlayerStint.objects.values_list("number", flat=True)),
            ["0", "00"],
        )

    def test_double_zero_is_shown_as_it_is(self):
        self.client.post(self.url, {"name": "山田", "number": "00", "position": "内野手"})

        response = self.client.get(f"{self.url}?pos=batter")

        self.assertContains(response, '<span class="jersey-number">00</span>', html=True)

    def test_a_leading_zero_is_rejected_with_a_japanese_message(self):
        response = self.client.post(self.url, {"name": "山田", "number": "01", "position": "内野手"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "前ゼロは使えません")
        self.assertEqual(orm_models.Player.objects.count(), 0)

    def test_the_list_orders_double_zero_zero_then_numerically(self):
        for number in ["10", "2", "0", "00"]:
            self.client.post(self.url, {"name": f"選手{number}", "number": number, "position": "内野手"})

        response = self.client.get(f"{self.url}?pos=batter")

        shown = [row.number for row in response.context["players"]]
        self.assertEqual(shown, ["00", "0", "2", "10"])

    def test_editing_to_double_zero_keeps_it(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.client.post(
            reverse("player_edit", args=[self.team.id, player.id]),
            {"name": "山田", "number": "00", "position": "内野手"},
        )

        self.assertEqual(self.service.get_player_detail(self.team.id, player.id).number, "00")

    def test_promoting_to_double_zero(self):
        developmental = self.service.register_player(self.team.id, "育成", 120, "投手", "育成")

        self.client.post(
            reverse("player_edit", args=[self.team.id, developmental.id]),
            {"promote": "1", "promote_number": "00"},
        )

        stint = orm_models.PlayerStint.objects.get(player_id=developmental.id)
        self.assertEqual((stint.number, stint.number_before_promotion), ("00", "120"))


class JerseyNumberAdminTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.other = self.service.register_player(self.team.id, "田中", 11, "外野手")

    def _add(self, number):
        return self.client.post(
            "/admin/myapp/playerstint/add/",
            {
                "player": self.other.id,
                "team": self.rival.id,
                "number": number,
                "from_year": "2020",
                "to_year": "2021",
            },
        )

    def test_double_zero_can_be_saved(self):
        self._add("00")

        self.assertTrue(orm_models.PlayerStint.objects.filter(team=self.rival, number="00").exists())

    def test_a_leading_zero_is_rejected(self):
        response = self._add("01")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "前ゼロは使えません")
        self.assertFalse(orm_models.PlayerStint.objects.filter(team=self.rival).exists())

    def test_the_stint_list_is_ordered_by_jersey_number_not_text(self):
        for number in ["10", "2", "0", "00"]:
            player = orm_models.Player.objects.create(name=f"選手{number}", position="内野手")
            orm_models.PlayerStint.objects.create(player=player, team=self.rival, number=number, from_year=2020)

        response = self.client.get(f"/admin/myapp/playerstint/?team__id__exact={self.rival.id}")

        shown = [s.number for s in response.context["cl"].result_list]
        self.assertEqual(shown, ["00", "0", "2", "10"])
        # 並びのキー（SQL 側）がドメインの `sort_key` と同じ並びになる
        self.assertEqual(shown, sorted(shown, key=lambda n: JerseyNumber(n).sort_key))


class MigrationToTextTest(TransactionTestCase):
    """0041: 背番号の列が整数から文字列に変わる。既存の値は文字列表記に直る。"""

    BEFORE = [("myapp", "0040_acquisition_route_and_fa_declaration")]
    AFTER = [("myapp", "0041_jersey_number_as_text")]

    def tearDown(self):
        # 後続のテストのために最新の状態へ戻す
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def test_existing_integers_become_text_and_double_zero_stays_possible(self):
        old = self._migrate(self.BEFORE)
        league = old.get_model("myapp", "League").objects.create(name="旧リーグ")
        team = old.get_model("myapp", "Team").objects.create(league=league, name="旧チーム")
        player = old.get_model("myapp", "Player").objects.create(name="旧選手", position="内野手")
        old.get_model("myapp", "PlayerStint").objects.create(
            player=player,
            team=team,
            number=7,
            from_year=2020,
            signed_as="育成",
            promoted_year=2024,
            number_before_promotion=120,
        )
        old.get_model("myapp", "PlayerStint").objects.create(
            player=player, team=team, number=0, from_year=2018, to_year=2019
        )

        new = self._migrate(self.AFTER)

        stints = new.get_model("myapp", "PlayerStint").objects.order_by("from_year")
        self.assertEqual([(s.number, s.number_before_promotion) for s in stints], [("0", None), ("7", "120")])
        # 「0」と「00」は別の値として入る
        new.get_model("myapp", "PlayerStint").objects.create(
            player_id=player.id, team_id=team.id, number="00", from_year=2015, to_year=2016
        )
        self.assertEqual(
            sorted(new.get_model("myapp", "PlayerStint").objects.values_list("number", flat=True)),
            ["0", "00", "7"],
        )
