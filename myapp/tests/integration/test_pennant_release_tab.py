"""編成画面の「戦力外」タブ（#168 段階B）: 出す条件・権限・手動にする／保存する／自動に戻す・古い画面・GM ホームの1行。

世界は `test_pennant_offseason_screens.py` と同じ（球団2つ・1リーグ。世界Aはシーズン終了、世界Bは開幕前）。
受け持つ球団は野手12人・投手11人で、位置の最低人数（`draft.MIN_BY_POSITION`。投手15人など）を最初から下回る。
指名が成り立つことを確かめるテストは最低人数を0に差し替える。
"""

from unittest import mock

from django.contrib.messages import get_messages

from myapp.domain.pennant.draft import MIN_BY_POSITION
from myapp.domain.pennant.release import ReleasePlan
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoReleasePlanRepository
from myapp.presentation.club_input import REOPEN
from myapp.presentation.views import build_pennant_season_service

from ..helpers import world_scope, ws_url
from .test_pennant_advance import YEAR
from .test_pennant_offseason_screens import IS_FINAL_SEASON, ScreenCase

NO_POSITION_MINIMUM = dict.fromkeys(MIN_BY_POSITION, 0)


class ReleaseTabCase(ScreenCase):
    def club_url(self, tab: str | None = "release", world_id=None) -> str:
        url = ws_url("pennant_club", self.world_a if world_id is None else world_id)
        return url if tab is None else f"{url}?tab={tab}"

    def team_id(self) -> int:
        team_id = orm_models.PennantWorld.objects.get(id=self.world_a).managed_team_id
        assert team_id is not None
        return team_id

    def registered_ids(self) -> list[int]:
        return sorted(
            orm_models.PlayerStint.objects.filter(team_id=self.team_id(), to_year=None).values_list(
                "player_id", flat=True
            )
        )

    def pitcher_ids(self) -> list[int]:
        return sorted(
            orm_models.PlayerStint.objects.filter(
                team_id=self.team_id(), to_year=None, player__position="投手"
            ).values_list("player_id", flat=True)
        )

    def plan(self) -> ReleasePlan | None:
        return DjangoReleasePlanRepository(world_scope(self.world_a)).find_by_team(self.team_id())

    def save_plan(self, player_ids) -> None:
        DjangoReleasePlanRepository(world_scope(self.world_a)).save(
            ReleasePlan(team_id=self.team_id(), year=YEAR, player_ids=frozenset(player_ids))
        )

    def post(self, action: str, *, chosen=(), shown=None, expected: str | None = str(YEAR)):
        data: dict = {"section": "release", "action": action, "release": [str(i) for i in chosen]}
        data["shown"] = [str(i) for i in (self.registered_ids() if shown is None else shown)]
        if expected is not None:
            data["expected_year"] = expected
        return self.client.post(self.club_url(tab=None), data)


class ReleaseTabVisibilityTest(ReleaseTabCase):
    def test_the_tab_is_shown_to_everyone_at_the_end_of_the_season(self):
        """GET は誰でも。書き込みの導線（手動で選ぶ）はオーナーにだけ出す。"""
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn('href="?tab=release"', content)
        self.assertIn('id="club-release" data-mode="auto"', content)
        self.assertNotIn('value="manual"', content, "未ログインには書き込みの導線を出さない")

        self.client.force_login(self.owner)
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn("手動で選ぶ", content)
        self.assertIn(f'name="expected_year" value="{YEAR}"', content)

    def test_the_tab_is_hidden_before_the_opening_and_in_the_season(self):
        """締められないときはタブを出さず、`?tab=release` は既定のタブ（1軍登録）に落とす。"""
        orm_models.PennantWorld.objects.filter(id=self.world_b).update(owner_id=self.owner.id)
        self.client.force_login(self.owner)
        for advance in (False, True):
            if advance:
                build_pennant_season_service(world_scope(self.world_b)).advance(AdvanceTarget.DAY)
            with self.subTest(in_season=advance):
                response = self.client.get(self.club_url(world_id=self.world_b))
                content = response.content.decode()
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('href="?tab=release"', content)
                self.assertNotIn('id="club-release"', content)
                self.assertIn('id="club-active"', content)

    def test_the_tab_is_hidden_in_the_final_season(self):
        with mock.patch(IS_FINAL_SEASON, return_value=True):
            content = self.client.get(self.club_url()).content.decode()
        self.assertNotIn('id="club-release"', content)

    def test_the_manual_plan_is_shown_read_only_to_others(self):
        self.save_plan(self.pitcher_ids()[:1])
        for user in (None, self.other):
            with self.subTest(user=user):
                self.client.logout()
                if user is not None:
                    self.client.force_login(user)
                content = self.client.get(self.club_url()).content.decode()
                self.assertIn('data-mode="manual"', content)
                self.assertIn("手動（1人）", content)
                self.assertNotIn('name="release"', content, "チェックボックスはオーナーにだけ")
                self.assertNotIn('value="auto"', content)


class ReleaseTabPostTest(ReleaseTabCase):
    def test_anonymous_goes_to_the_login_and_others_get_403(self):
        response = self.post("manual")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

        self.client.force_login(self.other)
        self.assertEqual(self.post("manual").status_code, 403)
        self.assertIsNone(self.plan())

    def test_manual_save_and_back_to_auto(self):
        self.client.force_login(self.owner)

        response = self.post("manual")
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertEqual(self.plan(), ReleasePlan(self.team_id(), YEAR, frozenset()), "手動は0人から選ぶ")
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn('name="release"', content)
        self.assertIn('value="save"', content)
        self.assertNotIn("手動で選ぶ", content, "手動のあいだは「手動で選ぶ」を並べない")

        chosen = self.pitcher_ids()[:2]
        with mock.patch.dict(MIN_BY_POSITION, NO_POSITION_MINIMUM):
            response = self.post("save", chosen=chosen)
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertEqual(self.plan().player_ids, frozenset(chosen))
        self.assertIn("2人", [str(m) for m in get_messages(response.wsgi_request)][-1])

        response = self.post("auto")
        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertIsNone(self.plan())

    def test_saving_zero_players_is_manual_zero(self):
        self.client.force_login(self.owner)
        self.save_plan(self.pitcher_ids()[:1])

        self.post("save", chosen=())

        self.assertEqual(self.plan().player_ids, frozenset(), "チェックを全部外すと手動で0人（自動には戻らない）")

    def test_missing_keys_are_not_read_as_zero_players(self):
        """`shown` が欠けた・食い違う・締める年が欠けた送信は、古い画面として弾き、保存済みの指名を消さない。"""
        self.client.force_login(self.owner)
        chosen = self.pitcher_ids()[:1]
        self.save_plan(chosen)
        cases = {
            "shown が無い": {"shown": []},
            "shown が今の支配下と違う": {"shown": self.registered_ids()[1:]},
            "締める年が無い": {"expected": None},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                response = self.post("save", chosen=(), **overrides)
                self.assertContains(response, REOPEN, status_code=400)
                self.assertEqual(self.plan().player_ids, frozenset(chosen))

    def test_an_invalid_plan_is_shown_again_with_the_checks_kept(self):
        """位置の最低人数を割る指名は保存せず、チェックを残して理由を出す。"""
        self.client.force_login(self.owner)
        self.save_plan(())
        chosen = self.pitcher_ids()[0]

        response = self.post("save", chosen=[chosen])

        self.assertContains(response, "投手は支配下に15人以上", status_code=400)
        self.assertRegex(response.content.decode(), rf'value="{chosen}" aria-label="[^"]*" checked')
        self.assertEqual(self.plan().player_ids, frozenset())

    def test_a_stale_year_is_refused(self):
        """別の年の画面からの送信は、指名を作らずに理由を出して編成へ戻す。"""
        self.client.force_login(self.owner)

        response = self.post("manual", expected=str(YEAR - 1))

        self.assertRedirects(response, self.club_url(tab=None), fetch_redirect_response=False)
        self.assertIsNone(self.plan())

    def test_after_closing_the_tab_is_gone_and_posts_are_refused(self):
        self.client.force_login(self.owner)
        self.close_with([])

        response = self.post("manual")

        self.assertRedirects(response, self.club_url(tab=None), fetch_redirect_response=False)
        self.assertIsNone(self.plan())
        self.assertNotIn('id="club-release"', self.client.get(self.club_url()).content.decode())


class SeasonCloseCardReleaseLineTest(ReleaseTabCase):
    def test_the_card_shows_auto_or_manual_with_the_count(self):
        """締めるカードに「戦力外: 自動／手動（N人）」の1行。締める操作はボタン1つのまま。"""
        self.client.force_login(self.owner)
        content = self.client.get(self.home_url()).content.decode()
        self.assertIn("戦力外: 自動", content)
        self.assertIn(f'href="{self.club_url()}"', content)

        self.save_plan(self.pitcher_ids()[:2])
        content = self.client.get(self.home_url()).content.decode()
        self.assertIn("戦力外: 手動（2人）", content)
        self.assertEqual(content.count(f'action="{self.close_url()}"'), 1)
