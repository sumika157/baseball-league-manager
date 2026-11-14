"""GM の外国人のオフの指名（#169 F2）: 編成の「外国人」タブ・保存と往復・締める処理・締めるカードの1行・
計算中の変更・世界の分離。

期待値の根拠: 候補は球団ごとに8人・見えるのはスカウトの見立てだけ・オフは外国人の保有がゲームの上限6人まで
（2026-10-25 ユーザー決定 Q4 A）、前所属をプロフィールに保存する（Q5 B）。指名は入力なので保存し、締めた時点で消す
（戦力外の指名と同じ）。見るのは誰でも・書くのはオーナー、POST のキーの欠落は0人と扱わない（CLAUDE.md
「JSON API の部分更新」と同じ考え）、締める年（`expected_year`）で古い画面を弾く。
会議を始めた後に変えられないことと、会議と締める処理で人数の幅が食い違わないことは `test_pennant_draft_service.py`。

世界は `test_pennant_offseason_screens.py` の `ScreenCase`（世界Aはシーズン終了）。受け持つ球団の支配下の1人を外国人に
し（`foreigner`）、外国人の育成を1人足して（`foreign_prospect`）から確かめる。
"""

from unittest import mock

from myapp.domain.exceptions import AlreadyClosed, InvalidSigning, InvalidWorld, TeamNotFound
from myapp.domain.pennant.foreign_market import MAX_FOREIGN_HELD, ForeignSigningPlan, offseason_key
from myapp.domain.pennant.release import ReleasePlan
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoForeignSigningPlanRepository, DjangoReleasePlanRepository
from myapp.presentation.club_input import REOPEN
from myapp.presentation.views import build_pennant_offseason_service, build_pennant_world_service

from ..helpers import default_workspace, real_scope, world_scope, ws_url
from .test_pennant_advance import YEAR
from .test_pennant_midseason import add_development, managed_team_id
from .test_pennant_offseason_screens import DECIDE_RELEASES, DECIDE_RETIREMENTS, IS_FINAL_SEASON, ScreenCase


class ForeignPlanCase(ScreenCase):
    """世界Aはシーズン終了。受け持つ球団の支配下の1人（`foreigner`）を外国人にし、外国人の育成（`foreign_prospect`）を足す。"""

    foreigner: int
    foreign_prospect: int

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        team_id = managed_team_id(cls.world_a)
        cls.foreigner = (
            orm_models.PlayerStint.objects.filter(team_id=team_id, to_year=None, player__is_foreign_player=False)
            .order_by("number")
            .values_list("player_id", flat=True)
            .first()
        )
        orm_models.Player.objects.filter(id=cls.foreigner).update(is_foreign_player=True)
        cls.foreign_prospect = add_development(cls.world_a, "育成外国", "150", draft_year=YEAR - 1, year=YEAR)
        orm_models.Player.objects.filter(id=cls.foreign_prospect).update(is_foreign_player=True)

    def team_id(self) -> int:
        return managed_team_id(self.world_a)

    def held(self) -> int:
        """受け持つ球団にいま在籍する外国人（育成を含む）。"""
        return orm_models.PlayerStint.objects.filter(
            team_id=self.team_id(), to_year=None, player__is_foreign_player=True
        ).count()

    def club_url(self, tab: str | None = "foreign", world_id=None) -> str:
        url = ws_url("pennant_club", self.world_a if world_id is None else world_id)
        return url if tab is None else f"{url}?tab={tab}"

    def repository(self, world_id=None) -> DjangoForeignSigningPlanRepository:
        return DjangoForeignSigningPlanRepository(world_scope(self.world_a if world_id is None else world_id))

    def plan(self) -> ForeignSigningPlan | None:
        return self.repository().find_by_team(self.team_id())

    def save_plan(self, indices, *, year: int = YEAR) -> None:
        keys = frozenset(offseason_key(year, index) for index in indices)
        self.repository().save(ForeignSigningPlan(team_id=self.team_id(), year=year, keys=keys))

    def post(self, action: str, *, sign=(), shown=None, expected: str | None = str(YEAR)):
        data: dict = {"section": "foreign", "action": action}
        data["shown"] = [str(i) for i in (range(8) if shown is None else shown)]
        data["sign"] = [str(i) for i in sign]
        if expected is not None:
            data["expected_year"] = expected
        return self.client.post(self.club_url(tab=None), data)

    def view(self):
        return build_pennant_offseason_service(world_scope(self.world_a)).foreign_view()

    def close(self):
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, return_value=()):
            return build_pennant_offseason_service(world_scope(self.world_a)).close_season(expected_year=YEAR)

    def new_foreigners(self) -> set[str]:
        return set(
            orm_models.PlayerStint.objects.filter(
                team_id=self.team_id(), from_year=YEAR + 1, acquired_via=AcquisitionRoute.NEW_FOREIGN.value
            ).values_list("player__name", flat=True)
        )


class ForeignTabTest(ForeignPlanCase):
    def test_everyone_sees_the_foreigners_and_the_candidates_and_only_the_owner_gets_the_buttons(self):
        """GET は誰でも（今の外国人・8人の候補・お任せの説明）。「手動で選ぶ」はオーナーにだけ。"""
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn('id="club-foreign" data-mode="auto"', content)
        self.assertEqual(content.count('data-testid="foreign-candidate-row"'), 8)
        self.assertEqual(content.count('data-testid="foreign-held-row"'), self.held())
        self.assertIn(f"保有 {self.held()}人／上限 {MAX_FOREIGN_HELD}人", content)
        self.assertIn("計4人・投手3人・野手3人", content)
        self.assertIn("ほかの球団と同じ規則", content)
        self.assertIn("見立て", content)
        self.assertNotIn('value="manual"', content)
        self.assertNotIn('type="checkbox"', content)

        self.client.force_login(self.owner)
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn("手動で選ぶ", content)
        self.assertIn(f'name="expected_year" value="{YEAR}"', content)

    def test_a_creator_who_lost_the_edit_right_only_reads(self):
        """外国人の指名も世界の書き込み: 作成者で今もワークスペースの全体の編集権を持つ人だけ（W4 の決定1）。"""
        orm_models.WorkspaceEditor.objects.filter(workspace=default_workspace(), user=self.owner).delete()
        self.client.force_login(self.owner)

        content = self.client.get(self.club_url()).content.decode()
        self.assertEqual(content.count('data-testid="foreign-candidate-row"'), 8, "表は読める")
        self.assertNotIn("手動で選ぶ", content)
        for action, sign in (("manual", ()), ("save", (0,)), ("auto", ())):
            with self.subTest(action=action):
                self.assertEqual(self.post(action, sign=sign).status_code, 403)
        self.assertFalse(orm_models.PennantForeignSigningPlan.objects.exists(), "何も書かない")

    def test_the_view_shows_the_estimates_and_the_backgrounds_but_not_the_true_ratings(self):
        view = self.view()
        self.assertEqual([row.index for row in view.candidates], list(range(8)))
        self.assertEqual({row.player_id for row in view.players}, set(self.foreign_ids()))
        prospect = next(row for row in view.players if row.player_id == self.foreign_prospect)
        self.assertTrue(prospect.is_development)
        for row in view.candidates:
            with self.subTest(index=row.index):
                self.assertTrue(row.background.label)
                self.assertTrue(all(cell.value % 5 == 0 for cell in row.cells), "見立ての項目は5刻み")

    def foreign_ids(self) -> list[int]:
        return list(
            orm_models.PlayerStint.objects.filter(
                team_id=self.team_id(), to_year=None, player__is_foreign_player=True
            ).values_list("player_id", flat=True)
        )

    def test_other_tabs_link_to_the_foreign_tab(self):
        self.assertIn("?tab=foreign", self.client.get(self.club_url(tab="active")).content.decode())

    def test_the_tab_is_hidden_before_the_end_of_the_season_and_in_the_final_season(self):
        content = self.client.get(self.club_url(world_id=self.world_b)).content.decode()
        self.assertNotIn('id="club-foreign"', content)
        self.assertNotIn("?tab=foreign", content)
        with mock.patch(IS_FINAL_SEASON, return_value=True):
            response = self.client.get(self.club_url())
        self.assertEqual(response.context["tab"], "active", "出せないタブは既定のタブに落とす")
        self.assertNotIn('id="club-foreign"', response.content.decode())

    def test_the_manual_plan_is_shown_read_only_to_others(self):
        self.save_plan([1, 5])
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn('data-mode="manual"', content)
        self.assertIn("手動（2人）", content)
        self.assertEqual(content.count('<span class="badge bg-primary">獲得</span>'), 2)
        self.assertNotIn('type="checkbox"', content, "選ぶ欄はオーナーにだけ")
        self.assertNotIn('value="auto"', content)


class ForeignPostTest(ForeignPlanCase):
    def test_anonymous_goes_to_the_login_and_others_get_403(self):
        response = self.post("manual")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

        self.client.force_login(self.other)
        self.assertEqual(self.post("manual").status_code, 403)
        self.assertIsNone(self.plan())

    def test_manual_starts_with_nobody_then_save_and_back_to_auto(self):
        self.client.force_login(self.owner)

        response = self.post("manual")
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertEqual(self.plan(), ForeignSigningPlan(self.team_id(), YEAR, frozenset()), "手動は0人から選ぶ")
        content = self.client.get(self.club_url()).content.decode()
        self.assertEqual(content.count('name="sign"'), 8)
        self.assertNotIn("手動で選ぶ", content, "手動のあいだは「手動で選ぶ」を並べない")

        response = self.post("save", sign=[0, 6])
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertEqual(self.plan().indices, {0, 6})
        self.assertIn("（2人）", self.last_message(response))
        self.assertIn("外国人: 手動（2人）", self.client.get(self.home_url()).content.decode())

        response = self.post("save", sign=[])
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertEqual(self.plan().indices, frozenset(), "0人も保存できる（お任せには戻らない）")

        response = self.post("auto")
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertIsNone(self.plan())

    def test_missing_keys_are_not_treated_as_nobody(self):
        """`shown`・締める年が欠けた送信、今の候補と違う `shown`、表示していない候補は保存せず、指名を変えない。"""
        self.client.force_login(self.owner)
        self.save_plan([2, 3])
        cases = {
            "shown が無い": {"shown": ()},
            "shown が今の候補と違う": {"shown": range(7)},
            "締める年が無い": {"expected": None},
            "表示していない候補": {"sign": [8]},
            "読めない値": {"sign": ["x"]},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                options: dict = {"sign": [0]}
                options.update(overrides)
                response = self.post("save", **options)
                self.assertContains(response, REOPEN, status_code=400)
        self.assertEqual(self.plan().indices, {2, 3})

    def test_the_held_limit_is_shown_again_with_the_checks_kept(self):
        """保有の上限（6人）を超える指名は保存せず、理由と、チェックを残して出す。"""
        self.client.force_login(self.owner)
        self.save_plan([])
        over = list(range(MAX_FOREIGN_HELD - self.held() + 1))

        response = self.post("save", sign=over)

        self.assertContains(response, f"{MAX_FOREIGN_HELD}人まで", status_code=400)
        content = response.content.decode()
        for index in over:
            self.assertRegex(content, rf'name="sign" value="{index}"[^>]*checked')
        self.assertEqual(self.plan().indices, frozenset())

    def test_foreigners_chosen_to_leave_make_room(self):
        """手動で戦力外に選んだ外国人は保有に数えない（締めるときは引退・戦力外の後の人数で数え直す）。"""
        service = build_pennant_offseason_service(world_scope(self.world_a))
        limit = MAX_FOREIGN_HELD - self.held() + 1
        with self.assertRaises(InvalidSigning):
            service.set_foreign_signings(list(range(limit)), expected_year=YEAR)
        DjangoReleasePlanRepository(world_scope(self.world_a)).save(
            ReleasePlan(team_id=self.team_id(), year=YEAR, player_ids=frozenset({self.foreigner}))
        )

        service.set_foreign_signings(list(range(limit)), expected_year=YEAR)

        self.assertEqual(len(self.plan().indices), limit)

    def test_a_stale_year_is_refused(self):
        self.client.force_login(self.owner)

        response = self.post("manual", expected=str(YEAR - 1))

        self.assertRedirects(response, self.club_url(tab=None), fetch_redirect_response=False)
        self.assertIsNone(self.plan())


class CloseWithManualForeignTest(ForeignPlanCase):
    def test_the_chosen_candidates_are_signed_and_the_plan_is_cleared(self):
        """指名した候補が翌年から支配下・新外国人で入り、前所属がプロフィールに残る。指名は締めると消える。"""
        chosen = [row for row in self.view().candidates if row.index in (0, 4)]
        self.save_plan([0, 4])

        result = self.close()

        self.assertEqual(self.new_foreigners(), {row.name for row in chosen})
        self.assertGreaterEqual(result.signed_foreign_count, 2)
        for row in chosen:
            stint = orm_models.PlayerStint.objects.get(team_id=self.team_id(), player__name=row.name)
            self.assertEqual(stint.signed_as, ContractStatus.REGISTERED.value)
            self.assertEqual(stint.player.foreign_background, row.background.value)
        self.assertIsNone(self.plan(), "使い終わった指名は消す")
        self.assertFalse(orm_models.PennantForeignSigningPlanEntry.objects.exists())

    def test_nobody_is_signed_when_the_gm_chose_nobody(self):
        self.save_plan([])

        self.close()

        self.assertEqual(self.new_foreigners(), set())

    def test_a_plan_for_another_year_is_not_used_but_cleared(self):
        self.save_plan([], year=YEAR - 1)
        auto = self.new_foreigners()

        self.close()

        self.assertGreaterEqual(len(self.new_foreigners()), len(auto), "お任せ（AI）で締める")
        self.assertFalse(orm_models.PennantForeignSigningPlan.objects.exists())

    def test_the_close_card_shows_auto_or_manual_with_the_count(self):
        """締めるカードに「外国人: お任せ／手動（N人）」の1行。締める操作はボタン1つのまま。"""
        self.assertIsNone(
            build_pennant_offseason_service(world_scope(self.world_a)).close_option().foreign_signing_count
        )
        self.client.force_login(self.owner)
        content = self.client.get(self.home_url()).content.decode()
        self.assertIn("外国人: お任せ", content)
        self.assertIn(f'href="{self.club_url()}"', content)

        self.save_plan([3])
        content = self.client.get(self.home_url()).content.decode()
        self.assertIn("外国人: 手動（1人）", content)
        self.assertEqual(content.count(f'action="{self.close_url()}"'), 1)

    def test_a_plan_saved_during_the_calculation_stops_the_close(self):
        """外で計算している間に GM が外国人の指名を変えたら、古い計算を書かずに断る（状態の目印に指名が入る）。"""
        service = build_pennant_offseason_service(world_scope(self.world_a))
        original = service._prepare

        def prepare_then_save(world, year):
            prepared = original(world, year)
            self.save_plan([7])
            return prepared

        service._prepare = prepare_then_save  # type: ignore[method-assign]
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), self.assertRaises(AlreadyClosed):
            service.close_season(expected_year=YEAR)

        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).exists(), "何も書かない")
        self.assertIsNotNone(self.plan(), "GM の指名は残る")

    def test_after_closing_the_tab_is_gone_and_posts_are_refused(self):
        self.client.force_login(self.owner)
        self.close()

        response = self.post("manual")

        self.assertRedirects(response, self.club_url(tab=None), fetch_redirect_response=False)
        self.assertIsNone(self.plan())
        self.assertNotIn('id="club-foreign"', self.client.get(self.club_url()).content.decode())


class ForeignPlanRepositoryTest(ForeignPlanCase):
    def test_round_trip_and_auto_deletes_the_rows(self):
        self.save_plan([1, 7])
        self.assertEqual(
            self.plan(), ForeignSigningPlan(self.team_id(), YEAR, frozenset(offseason_key(YEAR, i) for i in (1, 7)))
        )

        self.repository().save(ForeignSigningPlan(self.team_id(), YEAR, None))
        self.assertIsNone(self.plan())
        self.assertFalse(orm_models.PennantForeignSigningPlanEntry.objects.exists())

    def test_the_real_data_and_other_worlds_cannot_be_written(self):
        plan = ForeignSigningPlan(self.team_id(), YEAR, frozenset())
        with self.assertRaises(InvalidWorld):
            DjangoForeignSigningPlanRepository(real_scope()).save(plan)
        with self.assertRaises(TeamNotFound):
            self.repository(self.world_b).save(plan)
        with self.assertRaises(TeamNotFound):
            self.repository(self.world_b).find_by_team(self.team_id())

    def test_deleting_the_world_deletes_the_plan(self):
        self.save_plan([0, 1])

        build_pennant_world_service(real_scope()).delete_world(self.world_a)

        self.assertFalse(orm_models.PennantForeignSigningPlan.objects.exists())
        self.assertFalse(orm_models.PennantForeignSigningPlanEntry.objects.exists())
