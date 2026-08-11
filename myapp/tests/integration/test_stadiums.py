"""球場。本拠地の割り当て・表示順・屋根の種別。"""

import re

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.value_objects import (
    StadiumProfile,
)
from myapp.infrastructure import orm_models

from .base import BaseCase


class StadiumTest(BaseCase):
    """球場と本拠地。"""

    def test_team_summary_uses_the_stadium(self):
        summary = self.service.list_teams().rows[0]

        self.assertEqual(summary.stadium_name, "テスト球場")
        self.assertEqual(summary.city, "東京")

    def test_team_without_a_stadium(self):
        orm_models.Team.objects.filter(id=self.team.id).update(home_stadium=None)
        summary = {s.id: s for s in self.service.list_teams().rows}[self.team.id]

        self.assertEqual(summary.stadium_name, "")
        self.assertEqual(summary.city, "")

    def test_deleting_a_stadium_keeps_the_team(self):
        """球場を消してもチームは残る（本拠地が未設定になるだけ）。"""
        self.stadium.delete()

        self.assertTrue(orm_models.Team.objects.filter(id=self.team.id).exists())
        self.assertIsNone(orm_models.Team.objects.get(id=self.team.id).home_stadium)

    def test_page_shows_the_stadium(self):
        response = self.client.get(reverse("team_list"))
        self.assertContains(response, "テスト球場")

    def test_can_be_sorted_by_stadium(self):
        other = orm_models.Stadium.objects.create(name="あ球場")
        orm_models.Team.objects.filter(id=self.rival.id).update(home_stadium=other)

        rows = self.service.list_teams(sort="stadium", descending=False).rows
        self.assertEqual(rows[0].stadium_name, "あ球場")

    def test_teams_without_a_stadium_sort_last(self):
        rows = self.service.list_teams(sort="stadium", descending=False).rows
        self.assertEqual(rows[-1].stadium_name, "")

    def test_admin_page(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        response = self.client.get("/admin/myapp/stadium/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テスト球場")


class StadiumHomeTeamAssignmentTest(BaseCase):
    """球場の編集画面から本拠地を決められること。

    所属の出典は Team.home_stadium の1か所のまま。球場の側から
    編めるようにするだけで、関係を二重には持たない。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.dome = orm_models.Stadium.objects.create(name="新ドーム")
        self.url = f"/admin/myapp/stadium/{self.dome.id}/change/"

    def _save(self, team_ids, **overrides):
        payload = {
            "name": self.dome.name,
            "city": "",
            "capacity": "",
            "surface": "",
            "roof": "",
            "opened_year": "",
            "home_teams": [str(i) for i in team_ids],
        }
        payload.update(overrides)
        return self.client.post(self.url, payload)

    def _home_of(self, team):
        return orm_models.Team.objects.get(pk=team.id).home_stadium

    def test_form_offers_the_home_team_field(self):
        response = self.client.get(self.url)

        self.assertContains(response, "home_teams")
        self.assertContains(response, "本拠地とするチーム")

    def test_assigning_teams_moves_their_home(self):
        response = self._save([self.team.id, self.rival.id])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._home_of(self.team), self.dome)
        self.assertEqual(self._home_of(self.rival), self.dome)

    def test_removing_a_team_clears_its_home(self):
        self._save([self.team.id, self.rival.id])

        self._save([self.team.id])

        self.assertEqual(self._home_of(self.team), self.dome)
        self.assertIsNone(self._home_of(self.rival))

    def test_teams_of_other_stadiums_are_left_alone(self):
        """外すのはこの球場を本拠地にしていたチームだけ。

        テストチームはテスト球場が本拠地。新ドームの画面で相手チームだけを
        選んでも、テストチームの本拠地は動かない。
        """
        self._save([self.rival.id])

        self.assertEqual(self._home_of(self.team), self.stadium)
        self.assertEqual(self._home_of(self.rival), self.dome)

    def test_existing_assignment_is_shown_when_reopening(self):
        self._save([self.team.id])

        response = self.client.get(self.url)

        self.assertContains(response, f'value="{self.team.id}" selected')

    def test_saving_with_no_team_selected_clears_the_stadium(self):
        self._save([self.team.id])

        self._save([])

        self.assertIsNone(self._home_of(self.team))


class StadiumOrderingTest(BaseCase):
    """球場一覧の既定の並び。

    球場名順よりも、本拠地とするチームの並びをたどるほうが目的の球場に
    行き着きやすい。使われていない球場は末尾へ回す。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        # テストチーム(表示順1)・相手チーム(表示順2) の順に並ぶようにする
        orm_models.Team.objects.filter(pk=self.team.id).update(display_order=1)
        orm_models.Team.objects.filter(pk=self.rival.id).update(display_order=2)
        # 球場名の順（あ→た→わ）と、チームの並び順をわざと食い違わせる
        orm_models.Stadium.objects.filter(pk=self.stadium.id).update(name="わ球場")
        self.rival_stadium = orm_models.Stadium.objects.create(name="た球場")
        orm_models.Team.objects.filter(pk=self.rival.id).update(home_stadium=self.rival_stadium)
        self.unused = orm_models.Stadium.objects.create(name="あ球場")

    def _listed(self):
        body = self.client.get("/admin/myapp/stadium/").content.decode()
        return re.findall(r'<th class="field-name"><a[^>]*>([^<]+)</a>', body)

    def test_ordered_by_the_home_team_order(self):
        self.assertEqual(self._listed()[:2], ["わ球場", "た球場"])

    def test_stadiums_without_a_home_team_come_last(self):
        self.assertEqual(self._listed()[-1], "あ球場")

    def test_unused_stadiums_are_ordered_by_name_among_themselves(self):
        orm_models.Stadium.objects.create(name="い球場")

        listed = self._listed()

        self.assertEqual(listed[-2:], ["あ球場", "い球場"])

    def test_leagues_are_followed_before_teams(self):
        """リーグの表示順が先に効く。

        球場名でもチームの表示順でも最後に来る球場が、リーグを先に置いた
        ことで先頭へ来る。
        """
        orm_models.League.objects.filter(pk=self.league.id).update(display_order=1)
        other = orm_models.League.objects.create(name="別リーグ", display_order=0)
        far = orm_models.Team.objects.create(league=other, name="別リーグのチーム", display_order=99)
        first = orm_models.Stadium.objects.create(name="ん球場")
        orm_models.Team.objects.filter(pk=far.id).update(home_stadium=first)

        self.assertEqual(self._listed()[0], "ん球場")

    def test_columns_can_still_be_sorted(self):
        """既定を変えても、列を押しての並べ替えは残る。"""
        body = self.client.get("/admin/myapp/stadium/?o=1").content.decode()
        listed = re.findall(r'<th class="field-name"><a[^>]*>([^<]+)</a>', body)

        self.assertEqual(listed, sorted(listed))


class StadiumRoofTest(BaseCase):
    """屋根の種類。雨天中止があり得るかを分ける属性。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def test_roof_is_saved(self):
        response = self.client.post(
            "/admin/myapp/stadium/add/",
            {
                "name": "屋根つき球場",
                "city": "",
                "capacity": "",
                "surface": "",
                "roof": "ドーム",
                "opened_year": "",
                "home_teams": [],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(orm_models.Stadium.objects.get(name="屋根つき球場").roof, "ドーム")

    def test_choices_come_from_the_domain(self):
        """選択肢を画面側に書き足せないようにしておく（出典はドメイン）。"""
        self.assertEqual(
            [value for value, _ in orm_models.Stadium.ROOF_CHOICES],
            list(StadiumProfile.ROOFS),
        )

    def test_unknown_roof_is_rejected(self):
        response = self.client.post(
            "/admin/myapp/stadium/add/",
            {
                "name": "ガラス球場",
                "city": "",
                "capacity": "",
                "surface": "",
                "roof": "ガラス張り",
                "opened_year": "",
                "home_teams": [],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(orm_models.Stadium.objects.filter(name="ガラス球場").exists())

    def test_roof_appears_in_the_changelist(self):
        orm_models.Stadium.objects.filter(pk=self.stadium.id).update(roof="開閉式屋根")

        response = self.client.get("/admin/myapp/stadium/")

        self.assertContains(response, "開閉式屋根")
