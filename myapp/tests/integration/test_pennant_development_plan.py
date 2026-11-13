"""GM の育成選手のオフの指名（#170 P2）: 編成の「育成」タブのオフの形・保存と往復・締める処理・締めるカードの1行・
計算中の変更・世界の分離。

期待値の根拠: 行ごとに1択（育成のまま・支配下に登録・再契約・自由契約。満了の行だけ再契約・自由契約。2026-10-24
ユーザー決定 Q4 A）、満了の自由契約は退団の経路「戦力外」（Q3）、オフの昇格は翌年（Y+1）の昇格の年で書く（P1）。
指名は入力なので保存し、締めた時点で消す（戦力外の指名と同じ）。見るのは誰でも・書くのはオーナー、POST のキーの欠落は
エラー（CLAUDE.md「JSON API の部分更新」と同じ考え）、締める年（`expected_year`）で古い画面を弾く。
会議を始めた後に変えられないことは `test_pennant_draft_service.py` の `LocksTest`。

世界は `test_pennant_offseason_screens.py` の `ScreenCase`（世界Aはシーズン終了）。分岐元に育成がいないので、
受け持つ球団に育成を2人（1年目と、満了の年の3年目）足してから確かめる。
"""

from unittest import mock

from django.contrib.messages import get_messages
from django.urls import reverse

from myapp.domain.exceptions import AlreadyClosed, InvalidWorld, PlayerNotFound, TeamNotFound
from myapp.domain.pennant.development import DevelopmentDecision, DevelopmentPlan
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import ContractStatus, DepartureRoute
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoDevelopmentPlanRepository
from myapp.presentation.club_input import REOPEN
from myapp.presentation.views import build_pennant_offseason_service, build_pennant_world_service

from .test_pennant_advance import YEAR
from .test_pennant_midseason import add_development, managed_team_id
from .test_pennant_offseason_screens import DECIDE_RELEASES, DECIDE_RETIREMENTS, IS_FINAL_SEASON, ScreenCase

KEEP = DevelopmentDecision.KEEP
PROMOTE = DevelopmentDecision.PROMOTE
RESIGN = DevelopmentDecision.RESIGN
RELEASE = DevelopmentDecision.RELEASE


class DevelopmentPlanCase(ScreenCase):
    """世界Aはシーズン終了。受け持つ球団に育成を2人（1年目の `rookie` と、満了の年の3年目の `veteran`）。"""

    rookie: int
    veteran: int

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.rookie = add_development(cls.world_a, "育成一年", "150", draft_year=YEAR - 1, year=YEAR)
        cls.veteran = add_development(cls.world_a, "育成三年", "151", draft_year=YEAR - 3, year=YEAR)

    def team_id(self) -> int:
        return managed_team_id(self.world_a)

    def club_url(self, tab: str | None = "development", world_id=None) -> str:
        url = reverse("pennant_club", args=[self.world_a if world_id is None else world_id])
        return url if tab is None else f"{url}?tab={tab}"

    def repository(self, world_id=None) -> DjangoDevelopmentPlanRepository:
        return DjangoDevelopmentPlanRepository(WorldScope.pennant(self.world_a if world_id is None else world_id))

    def plan(self) -> DevelopmentPlan | None:
        return self.repository().find_by_team(self.team_id())

    def save_plan(self, decisions, *, year: int = YEAR) -> None:
        self.repository().save(DevelopmentPlan(team_id=self.team_id(), year=year, decisions=dict(decisions)))

    def post(self, action: str, *, decisions=None, shown=None, expected: str | None = str(YEAR)):
        data: dict = {"section": "development_plan", "action": action}
        data["shown"] = [str(i) for i in ((self.rookie, self.veteran) if shown is None else shown)]
        for player_id, decision in (decisions or {}).items():
            data[f"decision_{player_id}"] = decision.value
        if expected is not None:
            data["expected_year"] = expected
        return self.client.post(self.club_url(tab=None), data)

    def stint(self, player_id: int) -> orm_models.PlayerStint:
        return orm_models.PlayerStint.objects.get(player_id=player_id)

    def close(self):
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, return_value=()):
            return build_pennant_offseason_service(self.world_a).close_season(expected_year=YEAR)


class DevelopmentPlanTabTest(DevelopmentPlanCase):
    def test_everyone_sees_the_offseason_form_and_only_the_owner_gets_the_buttons(self):
        """GET は誰でも（育成の表と規則の説明）。「手動で選ぶ」はオーナーにだけ。期中の「支配下に登録」は出さない。"""
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn('id="club-development-plan" data-mode="auto"', content)
        self.assertEqual(content.count('data-testid="development-plan-row"'), 2)
        self.assertEqual(content.count('data-testid="expiring-badge"'), 1)
        self.assertIn("ほかの球団と同じ規則", content)
        self.assertNotIn('value="manual"', content)
        self.assertNotIn("支配下に登録</button>", content)

        self.client.force_login(self.owner)
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn("手動で選ぶ", content)
        self.assertIn(f'name="expected_year" value="{YEAR}"', content)

    def test_other_tabs_link_to_the_development_tab(self):
        self.assertIn("?tab=development", self.client.get(self.club_url(tab="active")).content.decode())

    def test_the_tab_is_hidden_before_the_opening_and_in_the_final_season(self):
        """世界Bは開幕前（補強の窓の外にするため日程を作らない）。最終シーズンは締められないので出さない。"""
        response = self.client.get(self.club_url(world_id=self.world_b))
        self.assertNotIn('id="club-development-plan"', response.content.decode())
        with mock.patch(IS_FINAL_SEASON, return_value=True):
            response = self.client.get(self.club_url())
        self.assertEqual(response.context["tab"], "active", "出せないタブは既定のタブに落とす")
        self.assertNotIn('id="club-development-plan"', response.content.decode())

    def test_the_manual_plan_is_shown_read_only_to_others(self):
        self.save_plan({self.rookie: PROMOTE, self.veteran: RELEASE})
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn('data-mode="manual"', content)
        self.assertIn("手動（支配下に登録 1人・自由契約 1人）", content)
        self.assertNotIn('type="radio"', content, "選ぶ欄はオーナーにだけ")
        self.assertNotIn('value="auto"', content)


class DevelopmentPlanPostTest(DevelopmentPlanCase):
    def test_anonymous_goes_to_the_login_and_others_get_403(self):
        response = self.post("manual")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

        self.client.force_login(self.other)
        self.assertEqual(self.post("manual").status_code, 403)
        self.assertIsNone(self.plan())

    def test_manual_starts_unchanged_then_save_and_back_to_auto(self):
        self.client.force_login(self.owner)

        response = self.post("manual")
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertEqual(
            self.plan(),
            DevelopmentPlan(self.team_id(), YEAR, {self.rookie: KEEP, self.veteran: RESIGN}),
            "手動は何も変えない扱い（育成のまま・再契約）から選ぶ",
        )
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn(f'name="decision_{self.veteran}"', content)
        self.assertNotIn("手動で選ぶ", content, "手動のあいだは「手動で選ぶ」を並べない")
        # 満了でない選手に「再契約」「自由契約」の選択肢を出さない（選択肢は domain の `choices_for`）
        self.assertNotIn(f'name="decision_{self.rookie}" id="decision-{self.rookie}-3"', content)

        response = self.post("save", decisions={self.rookie: PROMOTE, self.veteran: RELEASE})
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertEqual(self.plan().decisions, {self.rookie: PROMOTE, self.veteran: RELEASE})
        self.assertIn("支配下に登録 1人・自由契約 1人", [str(m) for m in get_messages(response.wsgi_request)][-1])

        response = self.post("auto")
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertIsNone(self.plan())

    def test_missing_keys_are_not_filled_in(self):
        """行の扱い・`shown`・締める年が欠けた送信は保存せず、保存済みの指名を変えない。"""
        self.client.force_login(self.owner)
        saved = {self.rookie: PROMOTE, self.veteran: RESIGN}
        self.save_plan(saved)
        response = self.post("save", decisions={self.rookie: KEEP})
        self.assertContains(response, "育成三年の扱いを選んでください", status_code=400)
        self.assertNotRegex(
            response.content.decode(),
            rf'name="decision_{self.veteran}" id="[^"]+" value="[^"]+" checked',
            "選んでいなかった行は、保存済みの扱いで埋めずに未選択のまま出す",
        )
        cases = {
            "shown が無い": {"shown": ()},
            "shown が今の育成と違う": {"shown": (self.rookie,)},
            "締める年が無い": {"expected": None},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                response = self.post("save", decisions={self.rookie: KEEP, self.veteran: RESIGN}, **overrides)
                self.assertContains(response, REOPEN, status_code=400)
        self.assertEqual(self.plan().decisions, saved)

    def test_an_invalid_choice_is_shown_again_with_the_choices_kept(self):
        """満了でない選手の「自由契約」は保存せず、理由と、選んだ扱いを残して出す。"""
        self.client.force_login(self.owner)
        self.save_plan({self.rookie: KEEP, self.veteran: RESIGN})

        response = self.post("save", decisions={self.rookie: RELEASE, self.veteran: PROMOTE})

        self.assertContains(response, "育成一年は今季で育成契約が満了しない", status_code=400)
        self.assertRegex(
            response.content.decode(), rf'name="decision_{self.veteran}" id="[^"]+" value="{PROMOTE.value}" checked'
        )
        self.assertEqual(self.plan().decisions, {self.rookie: KEEP, self.veteran: RESIGN})

    def test_an_unknown_value_is_a_stale_page(self):
        self.client.force_login(self.owner)
        data = {
            "section": "development_plan",
            "action": "save",
            "expected_year": str(YEAR),
            "shown": [str(self.rookie), str(self.veteran)],
            f"decision_{self.rookie}": "放出",
            f"decision_{self.veteran}": RESIGN.value,
        }
        self.assertContains(self.client.post(self.club_url(tab=None), data), REOPEN, status_code=400)
        self.assertIsNone(self.plan())

    def test_a_stale_year_is_refused(self):
        self.client.force_login(self.owner)

        response = self.post("manual", expected=str(YEAR - 1))

        self.assertRedirects(response, self.club_url(tab=None), fetch_redirect_response=False)
        self.assertIsNone(self.plan())


class CloseWithManualDevelopmentTest(DevelopmentPlanCase):
    def test_the_choices_are_written_and_the_plan_is_cleared(self):
        """支配下に登録した選手は翌年から支配下（昇格前の背番号を残す）、満了の自由契約は退団の経路「戦力外」。"""
        self.save_plan({self.rookie: PROMOTE, self.veteran: RELEASE})

        result = self.close()

        self.assertEqual((result.promoted_count, result.expired_count), (1, 1))
        promoted = self.stint(self.rookie)
        self.assertEqual(promoted.promoted_year, YEAR + 1)
        self.assertEqual(promoted.number_before_promotion, "150")
        self.assertEqual(promoted.signed_as, ContractStatus.DEVELOPMENTAL.value)
        released = self.stint(self.veteran)
        self.assertEqual((released.to_year, released.left_via), (YEAR, DepartureRoute.RELEASED.value))
        self.assertIsNone(self.plan(), "使い終わった指名は消す")
        self.assertFalse(orm_models.PennantDevelopmentPlanEntry.objects.exists())

    def test_resign_and_keep_write_nothing(self):
        self.save_plan({self.rookie: KEEP, self.veteran: RESIGN})

        result = self.close()

        self.assertEqual((result.promoted_count, result.expired_count), (0, 0))
        for player_id in (self.rookie, self.veteran):
            stint = self.stint(player_id)
            self.assertEqual((stint.to_year, stint.promoted_year), (None, None))

    def test_a_plan_for_another_year_is_not_used_but_cleared(self):
        self.save_plan({self.rookie: PROMOTE, self.veteran: RELEASE}, year=YEAR - 1)

        self.close()

        self.assertIsNone(self.stint(self.rookie).promoted_year)
        self.assertFalse(orm_models.PennantDevelopmentPlan.objects.exists())

    def test_the_close_card_shows_auto_or_manual_with_the_counts(self):
        """締めるカードに「育成: 自動／手動（昇格 N人・自由契約 N人）」の1行。締める操作はボタン1つのまま。"""
        service = build_pennant_offseason_service(self.world_a)
        option = service.close_option()
        self.assertEqual((option.development_promote_count, option.development_release_count), (None, None))
        self.client.force_login(self.owner)
        content = self.client.get(self.home_url()).content.decode()
        self.assertIn("育成: 自動", content)
        self.assertIn(f'href="{self.club_url()}"', content)

        self.save_plan({self.rookie: PROMOTE, self.veteran: RELEASE})
        content = self.client.get(self.home_url()).content.decode()
        self.assertIn("育成: 手動（昇格 1人・自由契約 1人）", content)
        self.assertEqual(content.count(f'action="{self.close_url()}"'), 1)

    def test_a_plan_saved_during_the_calculation_stops_the_close(self):
        """外で計算している間に GM が育成の指名を変えたら、古い計算を書かずに断る（状態の目印に指名が入る）。"""
        service = build_pennant_offseason_service(self.world_a)
        original = service._prepare

        def prepare_then_save(world, year):
            prepared = original(world, year)
            self.save_plan({self.rookie: PROMOTE, self.veteran: RESIGN})
            return prepared

        service._prepare = prepare_then_save  # type: ignore[method-assign]
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), self.assertRaises(AlreadyClosed):
            service.close_season(expected_year=YEAR)

        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).exists(), "何も書かない")
        self.assertIsNone(self.stint(self.rookie).promoted_year)
        self.assertIsNotNone(self.plan(), "GM の指名は残る")

    def test_after_closing_the_offseason_form_is_gone_and_posts_are_refused(self):
        self.client.force_login(self.owner)
        self.close()

        response = self.post("manual")

        self.assertRedirects(response, self.club_url(tab=None), fetch_redirect_response=False)
        self.assertIsNone(self.plan())
        self.assertNotIn('id="club-development-plan"', self.client.get(self.club_url()).content.decode())


class DevelopmentPlanRepositoryTest(DevelopmentPlanCase):
    def test_round_trip_and_auto_deletes_the_rows(self):
        decisions = {self.rookie: PROMOTE, self.veteran: RELEASE}
        self.save_plan(decisions)
        self.assertEqual(self.plan(), DevelopmentPlan(self.team_id(), YEAR, decisions))

        self.repository().save(DevelopmentPlan(self.team_id(), YEAR, None))
        self.assertIsNone(self.plan())
        self.assertFalse(orm_models.PennantDevelopmentPlanEntry.objects.exists())

    def test_the_real_data_and_other_worlds_cannot_be_written(self):
        plan = DevelopmentPlan(self.team_id(), YEAR, {})
        with self.assertRaises(InvalidWorld):
            DjangoDevelopmentPlanRepository(WorldScope.real()).save(plan)
        with self.assertRaises(TeamNotFound):
            self.repository(self.world_b).save(plan)
        with self.assertRaises(TeamNotFound):
            self.repository(self.world_b).find_by_team(self.team_id())

    def test_players_outside_the_world_cannot_be_chosen(self):
        outsider = orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_b).first().player_id
        with self.assertRaises(PlayerNotFound):
            self.save_plan({outsider: PROMOTE})

    def test_deleting_the_world_deletes_the_plan(self):
        self.save_plan({self.rookie: PROMOTE, self.veteran: RESIGN})

        build_pennant_world_service().delete_world(self.world_a)

        self.assertFalse(orm_models.PennantDevelopmentPlan.objects.exists())
        self.assertFalse(orm_models.PennantDevelopmentPlanEntry.objects.exists())
