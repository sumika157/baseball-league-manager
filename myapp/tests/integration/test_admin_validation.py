"""管理画面の入力検証。ORM を直接触る画面でも、集約と同じ業務ルールが効くこと。"""

from django.contrib.auth.models import User

from myapp.infrastructure import orm_models

from .base import BaseCase


class AdminStintValidationTest(BaseCase):
    """管理画面から過去の経歴を登録するときの検証。

    管理画面はドメインを経由しないため、判定を素通しにすると
    「同じチームで同じ背番号の選手が同時に2人」を作れてしまう。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.other = self.service.register_player(self.team.id, "田中", 11, "外野手")

    def _add(self, **overrides):
        payload = {
            "player": self.other.id,
            "team": self.team.id,
            "number": "10",
            "from_year": "2020",
            "to_year": "",
        }
        payload.update(overrides)
        return self.client.post("/admin/myapp/playerstint/add/", payload)

    def test_past_stint_can_be_registered(self):
        """別チームでの過去の在籍は普通に登録できる。"""
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        self.client.post(
            "/admin/myapp/playerstint/add/",
            {
                "player": self.player.id,
                "team": past.id,
                "number": "55",
                "from_year": "2020",
                "to_year": "2023",
            },
        )

        stints = orm_models.PlayerStint.objects.filter(player_id=self.player.id)
        self.assertEqual(stints.count(), 2)
        self.assertTrue(stints.filter(team=past, number=55, to_year=2023).exists())

    def test_overlapping_number_is_rejected(self):
        response = self._add()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "期間が重なる同じ背番号は登録できません")
        self.assertFalse(orm_models.PlayerStint.objects.filter(player_id=self.other.id, number=10).exists())

    def test_same_number_is_allowed_when_periods_do_not_overlap(self):
        """期間が重ならなければ同じ背番号を使える。"""
        # 山田は10番を2024〜2025で使い終えている
        orm_models.PlayerStint.objects.filter(player_id=self.player.id).update(from_year=2024, to_year=2025)
        # 田中の既存の在籍は別の年にしておく（同じ年の二重加入を避けるため）
        orm_models.PlayerStint.objects.filter(player_id=self.other.id).update(from_year=2020, to_year=2021)

        self._add(from_year="2026", to_year="")

        self.assertTrue(
            orm_models.PlayerStint.objects.filter(player_id=self.other.id, number=10, from_year=2026).exists()
        )

    def test_joining_the_same_team_twice_in_a_year_is_rejected(self):
        """同じチームに同じ年から二重に加入することはない。"""
        response = self._add(number="99", from_year="2026")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "既に存在します")

    def test_leaving_before_joining_is_rejected(self):
        response = self._add(number="99", from_year="2026", to_year="2020")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "退団年が加入年より前")

    def test_blank_joining_year_falls_back_to_the_debut_year(self):
        """最初の在籍では加入年＝入団年になることがほとんど。

        同じ年を二度入力させる意味が無いので、空欄なら入団年で埋める。
        """
        orm_models.Player.objects.filter(pk=self.other.id).update(debut_year=2019)
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        self._add(team=past.id, number="55", from_year="")

        stint = orm_models.PlayerStint.objects.get(player_id=self.other.id, team=past)
        self.assertEqual(stint.from_year, 2019)

    def test_explicit_joining_year_wins_over_the_debut_year(self):
        orm_models.Player.objects.filter(pk=self.other.id).update(debut_year=2019)
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        self._add(team=past.id, number="55", from_year="2022")

        stint = orm_models.PlayerStint.objects.get(player_id=self.other.id, team=past)
        self.assertEqual(stint.from_year, 2022)

    def test_blank_joining_year_is_rejected_without_a_debut_year(self):
        """埋める材料が無いときだけ入力を求める。"""
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        response = self._add(team=past.id, number="55", from_year="")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "加入年を入力してください")
        self.assertFalse(orm_models.PlayerStint.objects.filter(team=past).exists())

    def test_editing_a_stint_does_not_conflict_with_itself(self):
        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)

        response = self.client.post(
            f"/admin/myapp/playerstint/{stint.id}/change/",
            {
                "player": self.player.id,
                "team": self.team.id,
                "number": "10",
                "from_year": "2024",
                "to_year": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        stint.refresh_from_db()
        self.assertEqual(stint.from_year, 2024)


class AdminCaptaincyValidationTest(BaseCase):
    """管理画面から主将を登録するときの検証。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.other = self.service.register_player(self.team.id, "田中", 11, "外野手")

    def _change_payload(self, player, **captaincy_overrides):
        payload = {
            "name": player.name,
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
            "stints-TOTAL_FORMS": "0",
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "captaincies-TOTAL_FORMS": "1",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
            "captaincies-0-team": str(self.team.id),
            "captaincies-0-from_year": "2026",
            "captaincies-0-to_year": "",
            "captaincies-0-id": "",
        }
        payload.update(captaincy_overrides)
        return payload

    def test_appointing_a_captain_via_admin(self):
        response = self.client.post(f"/admin/myapp/player/{self.player.id}/change/", self._change_payload(self.player))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            orm_models.Captaincy.objects.filter(
                player_id=self.player.id, team_id=self.team.id, to_year__isnull=True
            ).exists()
        )

    def test_duplicate_captain_is_rejected(self):
        orm_models.Captaincy.objects.create(player_id=self.other.id, team_id=self.team.id, from_year=2025)

        response = self.client.post(f"/admin/myapp/player/{self.player.id}/change/", self._change_payload(self.player))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "に主将です")

    def test_appointing_a_player_not_on_the_roster_is_rejected(self):
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        response = self.client.post(
            f"/admin/myapp/player/{self.player.id}/change/",
            self._change_payload(self.player, **{"captaincies-0-team": str(past.id)}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "在籍していないため主将にできません")


class AdminForeignPlayerQuotaTest(BaseCase):
    """管理画面から外国人選手を登録・移籍するときの、枠の検証。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_roster_limit=1)
        self.existing_foreign = self.service.register_player(self.team.id, "既存助っ人", 50, "外野手")
        orm_models.Player.objects.filter(id=self.existing_foreign.id).update(is_foreign_player=True)
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")

    def _player_change_payload(self, player, **overrides):
        payload = {
            "name": player.name,
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
            "stints-TOTAL_FORMS": "0",
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "captaincies-TOTAL_FORMS": "0",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
        }
        payload.update(overrides)
        return payload

    def test_marking_a_player_as_foreign_is_rejected_over_the_roster_limit(self):
        response = self.client.post(
            f"/admin/myapp/player/{self.player.id}/change/",
            self._player_change_payload(self.player, is_foreign_player="on"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "外国人選手登録数が上限")
        self.assertFalse(orm_models.Player.objects.get(id=self.player.id).is_foreign_player)

    def test_transferring_a_foreign_player_is_rejected_over_the_destination_limit(self):
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_roster_limit=0)

        response = self.client.post(
            "/admin/myapp/playerstint/add/",
            {
                "player": self.existing_foreign.id,
                "team": self.rival.id,
                "number": "77",
                "from_year": "2026",
                "to_year": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "外国人選手登録数が上限")
        self.assertFalse(
            orm_models.PlayerStint.objects.filter(player_id=self.existing_foreign.id, team_id=self.rival.id).exists()
        )


class AdminUsesDomainRulesTest(BaseCase):
    """管理画面から保存できる値と、ドメインが許す値をそろえる。

    管理画面はドメインを経由しないため、繋いでおかないと画面からだけ
    現実的でない値を保存できてしまう。在籍だけが検証されていて、
    球場とプロフィールは素通りだった。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def _player_payload(self, **overrides):
        payload = {
            "name": "検証太郎",
            "position": "投手",
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
            "stints-TOTAL_FORMS": "0",
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "captaincies-TOTAL_FORMS": "0",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
        }
        payload.update(overrides)
        return payload

    def test_unrealistic_height_is_rejected(self):
        response = self.client.post("/admin/myapp/player/add/", self._player_payload(height_cm="400"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "身長の値が現実的ではありません")
        self.assertFalse(orm_models.Player.objects.filter(name="検証太郎").exists())

    def test_debut_year_outside_the_season_range_is_rejected(self):
        response = self.client.post("/admin/myapp/player/add/", self._player_payload(debut_year="1800"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "シーズンは")
        self.assertFalse(orm_models.Player.objects.filter(name="検証太郎").exists())

    def test_realistic_profile_is_accepted(self):
        response = self.client.post(
            "/admin/myapp/player/add/",
            self._player_payload(
                height_cm="180",
                weight_kg="78",
                debut_year="2021",
                birthplace="大阪府",
            ),
        )

        self.assertEqual(response.status_code, 302)
        player = orm_models.Player.objects.get(name="検証太郎")
        self.assertEqual((player.height_cm, player.debut_year), (180, 2021))

    def test_stadium_opened_year_outside_the_season_range_is_rejected(self):
        response = self.client.post(
            "/admin/myapp/stadium/add/",
            {
                "name": "検証球場",
                "city": "",
                "capacity": "",
                "surface": "",
                "opened_year": "1800",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "シーズンは")
        self.assertFalse(orm_models.Stadium.objects.filter(name="検証球場").exists())

    def test_valid_stadium_is_accepted(self):
        response = self.client.post(
            "/admin/myapp/stadium/add/",
            {
                "name": "検証球場",
                "city": "仙台市",
                "capacity": "30000",
                "surface": "人工芝",
                "opened_year": "1950",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(orm_models.Stadium.objects.filter(name="検証球場").exists())


class AdminPlayerWithStintsTest(BaseCase):
    """選手登録画面から、在籍（経歴）を一緒に登録できること。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.past = orm_models.Team.objects.create(league=self.league, name="前所属")

    def _post(self, stints, debut_year=""):
        payload = {
            "name": "新人太郎",
            "position": "投手",
            "birth_date": "",
            "throws": "",
            "bats": "",
            "height_cm": "",
            "weight_kg": "",
            "birthplace": "",
            "debut_year": debut_year,
            "high_school": "",
            "university": "",
            "corporate_team": "",
            "stints-TOTAL_FORMS": str(len(stints)),
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            "captaincies-TOTAL_FORMS": "0",
            "captaincies-INITIAL_FORMS": "0",
            "captaincies-MIN_NUM_FORMS": "0",
            "captaincies-MAX_NUM_FORMS": "1000",
        }
        for i, (team, number, from_year, to_year) in enumerate(stints):
            payload.update(
                {
                    f"stints-{i}-team": str(team.id),
                    f"stints-{i}-number": str(number),
                    f"stints-{i}-from_year": str(from_year),
                    f"stints-{i}-to_year": str(to_year),
                    f"stints-{i}-id": "",
                    f"stints-{i}-player": "",
                }
            )
        return self.client.post("/admin/myapp/player/add/", payload)

    def _created(self):
        return orm_models.Player.objects.filter(name="新人太郎").first()

    def test_new_player_can_be_registered_with_a_stint(self):
        """新規登録では選手がまだ保存されていない。

        その状態で既存の在籍と突き合わせようとして落ちていた。
        """
        response = self._post([(self.team, 18, 2024, "")])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._created().stints.count(), 1)

    def test_new_player_can_be_registered_with_a_transfer_history(self):
        """経歴を複数まとめて登録できる。"""
        self._post([(self.past, 18, 2018, 2021), (self.team, 11, 2022, "")])

        stints = self._created().stints.order_by("from_year")
        self.assertEqual(
            [(s.team_id, s.number, s.from_year, s.to_year) for s in stints],
            [(self.past.id, 18, 2018, 2021), (self.team.id, 11, 2022, None)],
        )

    def test_mid_season_transfer_is_allowed(self):
        """移籍元と移籍先が同じ年を共有するのは普通のこと。"""
        response = self._post([(self.past, 18, 2020, 2022), (self.team, 11, 2022, "")])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._created().stints.count(), 2)

    def test_rejoining_the_same_team_later_is_allowed(self):
        response = self._post([(self.team, 18, 2018, 2021), (self.team, 99, 2024, "")])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._created().stints.count(), 2)

    def test_overlapping_stints_at_the_same_team_are_rejected(self):
        """1行ずつの検証では、同時に送られた行どうしの矛盾に気づけない。"""
        response = self._post([(self.team, 18, 2018, 2021), (self.team, 11, 2020, "")])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じチームに同時に2度在籍することはできません")
        self.assertIsNone(self._created())

    def test_stint_takes_the_debut_year_entered_on_the_same_page(self):
        """入団年はまだ保存されていないが、同じ画面で入力されている。"""
        response = self._post([(self.team, 18, "", "")], debut_year="2021")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._created().stints.get().from_year, 2021)

    def test_number_taken_by_another_player_is_still_rejected(self):
        """新規登録でも、他の選手との背番号の重なりは弾く。"""
        self.service.register_player(self.team.id, "山田", 10, "内野手")

        response = self._post([(self.team, 10, 2026, "")])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "期間が重なる同じ背番号は登録できません")
        self.assertIsNone(self._created())
