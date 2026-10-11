"""オフの画面の検査（P6c）: 「シーズンを締める」・オフの結果・開幕前のホーム・選手ページの能力の推移。

世界は球団2つ（1リーグ）。シーズン終了まで進めた世界Aをクラスで1度だけ作り、各テストが締める
（テストごとに巻き戻る）。世界Bは同じ構成で、1試合も進めていない（開幕前・シーズン中の画面を確かめる）。
引退は乱数で決まるので、特定の選手が引退する前提のテストは `decide_retirements` を差し替える。
"""

from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.pennant.club_plan import ClubPlan, LineupChoice, PlanSection
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import FieldingPosition
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoClubPlanRepository
from myapp.presentation.views import build_pennant_offseason_service, build_pennant_season_service

from .test_pennant_advance import YEAR
from .test_pennant_home import HomeCase

DECIDE_RETIREMENTS = "myapp.domain.pennant.offseason.decide_retirements"
IS_FINAL_SEASON = "myapp.application.pennant_offseason.is_final_season"


class ScreenCase(HomeCase):
    """世界A（オーナー `owner`）はシーズン終了。世界B（同じオーナー）は開幕前。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        orm_models.PennantWorld.objects.filter(id=cls.world_b).update(owner_id=cls.owner.id)
        build_pennant_season_service(cls.world_a).advance(AdvanceTarget.SEASON_END)

    def close_url(self, world_id=None) -> str:
        return reverse("pennant_close_season", args=[self.world_a if world_id is None else world_id])

    def offseason_url(self, year=YEAR, world_id=None) -> str:
        return reverse("pennant_offseason", args=[self.world_a if world_id is None else world_id, year])

    def post_close(self, expected="2026", world_id=None):
        return self.client.post(self.close_url(world_id), {"expected_year": expected})

    def managed_players(self, count: int) -> list[int]:
        team_id = orm_models.PennantWorld.objects.get(id=self.world_a).managed_team_id
        assert team_id is not None, "テストの世界には受け持つ球団がある"
        return list(
            orm_models.PlayerStint.objects.filter(team_id=team_id, to_year=None)
            .order_by("number")
            .values_list("player_id", flat=True)[:count]
        )

    def close_with(self, retiring: list[int]):
        with mock.patch(DECIDE_RETIREMENTS, return_value=tuple(retiring)):
            return build_pennant_offseason_service(self.world_a).close_season(expected_year=YEAR)


class CloseCardTest(ScreenCase):
    def test_the_card_is_for_the_owner_at_the_end_of_the_season(self):
        self.client.force_login(self.owner)
        content = self.client.get(self.home_url()).content.decode()

        self.assertIn(self.close_url(), content)
        self.assertIn('name="expected_year" value="2026"', content)
        self.assertIn("元に戻せません", content)
        self.assertIn("2027年の日程を作ります", content)
        self.assertNotIn(self.advance_url(), content, "締めるカードと進めるカードは同時に出さない")

    def test_the_card_is_hidden_from_anonymous_and_other_users(self):
        for user in (None, self.other):
            with self.subTest(user=user):
                self.client.logout()
                if user is not None:
                    self.client.force_login(user)
                self.assertNotIn(self.close_url(), self.client.get(self.home_url()).content.decode())

    def test_the_card_is_hidden_before_the_opening_and_in_the_middle_of_the_season(self):
        self.client.force_login(self.owner)
        self.assertNotIn(self.close_url(self.world_b), self.client.get(self.home_url(self.world_b)).content.decode())

        build_pennant_season_service(self.world_b).advance(AdvanceTarget.DAY)
        content = self.client.get(self.home_url(self.world_b)).content.decode()
        self.assertNotIn(self.close_url(self.world_b), content)
        self.assertIn(self.advance_url(self.world_b), content)

    def test_the_final_season_shows_the_reason_instead_of_the_button(self):
        self.client.force_login(self.owner)
        with mock.patch(IS_FINAL_SEASON, return_value=True):
            content = self.client.get(self.home_url()).content.decode()

        self.assertNotIn(self.close_url(), content)
        self.assertIn("alert-secondary", content)
        self.assertIn("上限に達している", content)


class CloseSeasonPostTest(ScreenCase):
    def test_anonymous_goes_to_the_login_and_others_get_403(self):
        response = self.post_close()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

        self.client.force_login(self.other)
        self.assertEqual(self.post_close().status_code, 403)
        self.assertEqual(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).count(), 0, "何も締めていない")

    def test_get_goes_back_to_the_home_without_closing(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.close_url())

        self.assertRedirects(response, self.home_url(), fetch_redirect_response=False)
        self.assertEqual(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).count(), 0)

    def test_the_owner_closes_and_lands_on_the_result(self):
        self.client.force_login(self.owner)
        response = self.post_close()

        self.assertRedirects(response, self.offseason_url(), fetch_redirect_response=False)
        self.assertRegex(self.last_message(response), r"2026年のシーズンを締めました（引退 \d+人・新人 \d+人）。")
        self.assertGreater(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).count(), 0)

    def test_a_second_post_with_the_same_year_changes_nothing_and_goes_to_the_result(self):
        self.client.force_login(self.owner)
        self.post_close()
        before = orm_models.PennantPlayerRatings.objects.count()

        response = self.post_close()

        self.assertRedirects(response, self.offseason_url(), fetch_redirect_response=False)
        self.assertIn("既に締めています", self.last_message(response))
        self.assertEqual(orm_models.PennantPlayerRatings.objects.count(), before)

    def test_a_missing_or_unreadable_year_is_rejected_not_treated_as_no_check(self):
        self.client.force_login(self.owner)
        for sent in (None, "", "abc", "-1"):
            with self.subTest(sent=sent):
                data = {} if sent is None else {"expected_year": sent}
                response = self.client.post(self.close_url(), data)

                self.assertRedirects(response, self.home_url(), fetch_redirect_response=False)
                self.assertEqual(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).count(), 0)

    def test_a_stale_year_in_the_future_is_rejected(self):
        self.client.force_login(self.owner)
        response = self.post_close(expected="2030")

        self.assertRedirects(response, self.home_url(), fetch_redirect_response=False)
        self.assertEqual(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).count(), 0)

    def test_a_made_up_year_goes_home_never_to_a_result_page(self):
        """行き先は「締めていると確かめられた年」だけ。作り物の値（範囲外・開幕年より前・未来）はホーム。"""
        self.client.force_login(self.owner)
        for sent in ("1", str(YEAR - 1), "2030"):
            with self.subTest(sent=sent):
                response = self.post_close(expected=sent)

                self.assertRedirects(response, self.home_url(), fetch_redirect_response=False)
        self.assertEqual(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).count(), 0)

    def test_once_closed_any_resend_goes_to_the_year_that_was_closed(self):
        self.client.force_login(self.owner)
        self.post_close()
        for sent in ("1", "2026", "2030"):
            with self.subTest(sent=sent):
                response = self.post_close(expected=sent)

                self.assertRedirects(response, self.offseason_url(YEAR), fetch_redirect_response=False)

    def test_an_admin_who_is_not_the_owner_gets_403(self):
        admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(admin)

        self.assertEqual(self.post_close().status_code, 403)
        self.assertEqual(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).count(), 0)

    def test_a_season_that_is_not_finished_is_a_message_not_an_error_page(self):
        self.client.force_login(self.owner)
        response = self.post_close(world_id=self.world_b)

        self.assertRedirects(response, self.home_url(self.world_b), fetch_redirect_response=False)
        self.assertTrue(self.last_message(response))

    def test_the_final_season_cannot_be_closed(self):
        self.client.force_login(self.owner)
        with mock.patch(IS_FINAL_SEASON, return_value=True):
            response = self.post_close()

        self.assertRedirects(response, self.home_url(), fetch_redirect_response=False)
        self.assertIn("上限", self.last_message(response))
        self.assertEqual(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).count(), 0)

    def test_released_sections_are_told(self):
        self.client.force_login(self.owner)
        team_id = orm_models.PennantWorld.objects.get(id=self.world_a).managed_team_id
        assert team_id is not None
        players = self.managed_players(12)
        retiring, others = players[0], players[1:]
        DjangoClubPlanRepository(WorldScope.pennant(self.world_a)).save(
            ClubPlan(
                team_id,
                active_ids=tuple(players),
                lineup=tuple(LineupChoice(player_id, FieldingPosition.DESIGNATED_HITTER) for player_id in others[:9]),
                rotation=tuple(others[:2]),
                closer_id=others[2],
            )
        )
        with mock.patch(DECIDE_RETIREMENTS, return_value=(retiring,)):
            response = self.post_close()

        told = " ".join(str(message) for message in get_messages(response.wsgi_request))
        self.assertIn(PlanSection.ACTIVE.value, told)
        self.assertIn("自動に戻しました", told)


class OffseasonResultTest(ScreenCase):
    def test_a_year_that_is_not_closed_is_404_for_everyone(self):
        for year in (YEAR, YEAR + 1, YEAR - 1, 1, 0, 10**20):
            with self.subTest(year=year):
                self.assertEqual(self.client.get(self.offseason_url(year)).status_code, 404)

    def test_a_closed_result_is_never_opened_for_the_year_before_the_opening_year(self):
        """世界の作成は開幕年の能力を作る。開幕年の前の年を「締めた年」として開けない（分岐した全選手が新人に並ぶ）。"""
        self.close_with([])

        self.assertEqual(self.client.get(self.offseason_url(YEAR - 1)).status_code, 404)

    def test_anyone_can_read_the_result_of_a_closed_year(self):
        retiring = self.managed_players(2)
        self.close_with(retiring)

        for user in (None, self.other, self.owner):
            with self.subTest(user=user):
                self.client.logout()
                if user is not None:
                    self.client.force_login(user)
                response = self.client.get(self.offseason_url())
                self.assertEqual(response.status_code, 200)
                self.assertIn("2026年オフ（2027年シーズンへ）", response.content.decode())

    def test_retired_players_and_rookies_are_listed_for_the_own_team(self):
        retiring = self.managed_players(2)
        names = list(orm_models.Player.objects.filter(id__in=retiring).values_list("name", flat=True))
        result = self.close_with(retiring)

        content = self.client.get(self.offseason_url()).content.decode()

        for name in names:
            self.assertIn(name, content)
        self.assertIn("引退・退団（2人）", content)
        self.assertIn(f"新人（{result.draftee_count}人）", content)
        self.assertIn("能力の変化", content)

    def test_links_to_a_retired_player_open_the_players_page(self):
        retiring = self.managed_players(1)
        self.close_with(retiring)
        team_id = orm_models.PennantWorld.objects.get(id=self.world_a).managed_team_id
        link = reverse("pennant_player_detail", args=[self.world_a, team_id, retiring[0]])

        self.assertIn(link, self.client.get(self.offseason_url()).content.decode())
        self.assertEqual(self.client.get(link).status_code, 200)

    def test_an_unknown_league_falls_back_and_other_worlds_are_not_mixed_in(self):
        retiring = self.managed_players(1)
        self.close_with(retiring)

        response = self.client.get(self.offseason_url() + "?league=999999")
        self.assertEqual(response.status_code, 200)
        other_names = set(
            orm_models.Player.objects.filter(stints__team__league__world_id=self.world_b).values_list(
                "name", flat=True
            )
        )
        content = response.content.decode()
        self.assertTrue(other_names)
        # 世界Bの選手は、名前が世界Aと同じ（分岐元が同じ）。ここでは世界Bの id のリンクが出ないことで見る
        self.assertNotIn(f"/pennant/{self.world_b}/", content)

    def test_the_other_world_and_a_missing_world_are_404(self):
        self.close_with(self.managed_players(1))

        self.assertEqual(self.client.get(self.offseason_url(world_id=self.world_b)).status_code, 404)
        self.assertEqual(self.client.get(self.offseason_url(world_id=999999)).status_code, 404)


class HomeAfterCloseTest(ScreenCase):
    def setUp(self):
        super().setUp()
        self.close_with(self.managed_players(1))

    def test_the_home_before_the_next_opening_shows_last_years_rank_and_the_result_link(self):
        content = self.client.get(self.home_url()).content.decode()

        self.assertIn("2026年の最終順位", content)
        self.assertIn(self.offseason_url(), content)
        self.assertIn("順位表（2026年）", content, "順位表は前年の最終順位")
        self.assertIn("2026年のタイトル", content)
        world_league = orm_models.League.objects.get(world_id=self.world_a)
        titles = reverse("pennant_league_titles_by_year", args=[self.world_a, world_league.id, YEAR])
        self.assertIn(titles, content, "タイトル一覧は前年を指す")
        self.assertNotIn(self.close_url(), content)

    def test_the_owner_can_advance_again_and_the_current_season_takes_over(self):
        self.client.force_login(self.owner)
        content = self.client.get(self.home_url()).content.decode()
        self.assertIn(self.advance_url(), content)

        build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)
        after = self.client.get(self.home_url()).content.decode()
        self.assertIn("順位表（2027年）", after)
        self.assertNotIn(self.offseason_url(), after)


class RatingsHistoryTest(ScreenCase):
    def history_url(self, player_id: int) -> str:
        stint = orm_models.PlayerStint.objects.filter(player_id=player_id).order_by("-from_year").first()
        assert stint is not None, "選手には在籍がある"
        return reverse("pennant_player_detail", args=[self.world_a, stint.team_id, player_id])

    def test_the_first_season_shows_a_single_row(self):
        player = self.managed_players(1)[0]
        content = self.client.get(self.history_url(player)).content.decode()
        self.assertTrue("能力の推移" in content and "2026年" in content)
        self.assertNotIn("（最終年）", content)

    def test_after_closing_the_history_lists_each_year_and_marks_the_last_year_of_a_retired_player(self):
        staying, retiring = self.managed_players(2)[1], self.managed_players(1)[0]
        self.close_with([retiring])

        content = self.client.get(self.history_url(staying)).content.decode()
        self.assertIn("能力の推移", content)
        self.assertIn("2026年", content)
        self.assertIn("2027年", content)
        self.assertNotIn("（最終年）", content)

        retired_page = self.client.get(self.history_url(retiring)).content.decode()
        self.assertIn('2026年<span class="stat-muted">（最終年）</span>', retired_page)
        self.assertNotIn("2027年", retired_page.split("能力の推移")[1].split("プロフィール")[0])

    def test_the_history_adds_a_fixed_number_of_queries(self):
        """能力の推移は、球団の集約を読み直さない小さなクエリで作る（年を重ねても増えない）。"""
        player = self.managed_players(2)[1]
        url = self.history_url(player)
        with CaptureQueriesContext(connection) as before:
            self.client.get(url)
        self.close_with([])
        with CaptureQueriesContext(connection) as after:
            self.client.get(url)

        self.assertLessEqual(len(after) - len(before), 3)

    def test_real_data_players_have_no_history(self):

        team = self.home
        stint = orm_models.PlayerStint.objects.filter(team=team).first()
        assert stint is not None
        response = self.client.get(reverse("player_detail", args=[team.id, stint.player_id]))
        self.assertNotIn("能力の推移", response.content.decode())


class WorldIsolationTest(ScreenCase):
    def test_closing_one_world_does_not_show_in_the_other(self):
        self.close_with(self.managed_players(1))

        other_year_ratings = orm_models.PennantPlayerRatings.objects.filter(
            player__stints__team__league__world_id=self.world_b, year=YEAR + 1
        )
        self.assertFalse(other_year_ratings.exists())
        self.assertNotIn("2026年のオフの結果", self.client.get(self.home_url(self.world_b)).content.decode())
