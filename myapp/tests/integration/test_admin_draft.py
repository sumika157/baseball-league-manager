"""管理画面のドラフトの入力（#108）。値の検査と、最初の在籍の経路（ドラフト／育成ドラフト）との整合。"""

from django.contrib.auth.models import User

from myapp.infrastructure import orm_models

from .base import BaseCase

_EMPTY_INLINES = {
    f"{prefix}-{key}": value
    for prefix in ("fa_declarations", "captaincies")
    for key, value in (("TOTAL_FORMS", "0"), ("INITIAL_FORMS", "0"), ("MIN_NUM_FORMS", "0"), ("MAX_NUM_FORMS", "1000"))
}


class AdminDraftTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def _post(self, *, draft_year="", draft_round="", development=False, stint=None):
        """選手の新規登録。`stint` は (背番号, 加入年, 区分, 経路)。"""
        payload = {
            "name": "新人太郎",
            "position": "投手",
            "birth_date": "",
            "throws": "",
            "bats": "",
            "height_cm": "",
            "weight_kg": "",
            "birthplace": "",
            "draft_year": draft_year,
            "draft_round": draft_round,
            "high_school": "",
            "university": "",
            "corporate_team": "",
            "stints-TOTAL_FORMS": "1" if stint else "0",
            "stints-INITIAL_FORMS": "0",
            "stints-MIN_NUM_FORMS": "0",
            "stints-MAX_NUM_FORMS": "1000",
            **_EMPTY_INLINES,
        }
        if development:
            payload["is_development_draft"] = "on"
        if stint:
            number, from_year, signed_as, acquired_via = stint
            payload.update(
                {
                    "stints-0-team": str(self.team.id),
                    "stints-0-number": number,
                    "stints-0-from_year": from_year,
                    "stints-0-to_year": "",
                    "stints-0-signed_as": signed_as,
                    "stints-0-acquired_via": acquired_via,
                    "stints-0-id": "",
                    "stints-0-player": "",
                }
            )
        return self.client.post("/admin/myapp/player/add/", payload)

    def _created(self):
        return orm_models.Player.objects.filter(name="新人太郎").first()

    def test_developmental_pick_is_saved(self):
        response = self._post(
            draft_year="2020", draft_round="3", development=True, stint=("120", "", "育成", "育成ドラフト")
        )

        self.assertEqual(response.status_code, 302)
        player = self._created()
        self.assertEqual((player.draft_year, player.draft_round, player.is_development_draft), (2020, 3, True))
        self.assertEqual(player.stints.get().from_year, 2021)

    def test_round_zero_is_rejected(self):
        response = self._post(draft_year="2020", draft_round="0")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ドラフトの順位は1以上で入力してください")
        self.assertIsNone(self._created())

    def test_round_or_development_without_a_year_is_rejected(self):
        for kwargs in ({"draft_round": "1"}, {"development": True}):
            with self.subTest(**kwargs):
                response = self._post(**kwargs)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "ドラフト年を入力してください")
                self.assertIsNone(self._created())

    def test_regular_pick_with_a_developmental_first_stint_is_rejected(self):
        """育成ドラフトで入団した選手のドラフトを本指名にはできない（選手の画面。送信後の在籍の全体で見る）。"""
        response = self._post(draft_year="2020", draft_round="1", stint=("120", "2021", "育成", "育成ドラフト"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "育成ドラフトで入団した選手は、ドラフトも育成にしてください")
        self.assertIsNone(self._created())

    def test_developmental_pick_with_a_regular_draft_stint_is_rejected(self):
        response = self._post(
            draft_year="2020", draft_round="1", development=True, stint=("18", "2021", "支配下", "ドラフト")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ドラフトで入団した選手は、ドラフトを育成にできません")
        self.assertIsNone(self._created())

    def test_stint_page_checks_the_saved_draft(self):
        """在籍だけの画面では、保存済みのドラフトと突き合わせる。"""
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(pk=player.id).update(draft_year=2017, draft_round=2)
        past = orm_models.Team.objects.create(league=self.league, name="前所属")

        response = self.client.post(
            "/admin/myapp/playerstint/add/",
            {
                "player": player.id,
                "team": past.id,
                "number": "120",
                "from_year": "2018",
                "to_year": "2020",
                "signed_as": "育成",
                "acquired_via": "育成ドラフト",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "育成ドラフトで入団した選手は、ドラフトも育成にしてください")
        self.assertFalse(orm_models.PlayerStint.objects.filter(team=past).exists())

    def test_stint_page_does_not_check_a_later_stint(self):
        """最初の在籍でない在籍の経路は見ない（ここでは不自然な「育成ドラフト」の2つ目の在籍でも通る）。"""
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(pk=player.id).update(draft_year=2017, draft_round=2)
        orm_models.PlayerStint.objects.filter(player_id=player.id).update(from_year=2018)
        other = orm_models.Team.objects.create(league=self.league, name="移籍先")

        response = self.client.post(
            "/admin/myapp/playerstint/add/",
            {
                "player": player.id,
                "team": other.id,
                "number": "130",
                "from_year": "2030",
                "to_year": "",
                "signed_as": "育成",
                "acquired_via": "育成ドラフト",
            },
        )

        self.assertEqual(response.status_code, 302)
