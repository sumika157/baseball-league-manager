"""期中の外国人の獲得（#169 F3）: 編成の「外国人」タブの期中の形・GM の獲得・補強の窓・登録日の記録・オフの結果。

期待値の根拠: #169 のユーザーの決定 Q3 A・Q4 A（2026-10-25。期中の獲得は1球団1年1人・保有は6人まで・候補は見立て
だけを見せる）・Q5 B（前所属はプロフィール）、補強の窓は締めた後（開幕前）から7月31日まで（#170 Q4 A・野球協約）、
期限の後は導線を出さない（#207・CLAUDE.md「押しても弾かれるだけの導線を見せない」）、見るのは誰でも・書くのはオーナー
（Wiki「ペナントモード」）、POST のキーの欠落はエラー（CLAUDE.md「JSON API の部分更新」と同じ考え）。
AI の規則そのものは `tests/domain/test_midseason_foreign_signings.py`、AI の獲得は `test_pennant_midseason_review.py`。

世界は `test_pennant_midseason.py` の `MidseasonCase`（球団2つ・各23人。世界Aは開幕前でオーナーは `owner`。
分岐元に外国人はいない）。オフの結果との関係だけは、シーズンを終えた世界（`ScreenCase`）で確かめる。
"""

from unittest import mock

from django.urls import reverse

from myapp.domain.exceptions import InvalidSigning, RegisteredPlayerLimitExceeded, ReinforcementClosed
from myapp.domain.pennant.foreign_market import MAX_FOREIGN_HELD
from myapp.domain.pennant.reinforcement import RegistrationKind
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoOffseasonQuery, DjangoSimulationContextQuery
from myapp.infrastructure.repositories import DjangoRatingsRepository, DjangoTeamRepository
from myapp.presentation.club_input import REOPEN
from myapp.presentation.views import build_foreign_signing_service, build_pennant_offseason_service

from .test_pennant_advance import YEAR
from .test_pennant_midseason import MidseasonCase, first_fixture_on, managed_team_id
from .test_pennant_offseason_screens import DECIDE_RELEASES, DECIDE_RETIREMENTS, ScreenCase


class ForeignSigningCase(MidseasonCase):
    def foreign_url(self, tab: str | None = "foreign") -> str:
        return self.club_url(tab=tab)

    def post_signing(self, index, *, expected: str | None = None):
        data = {"section": "foreign_midseason", "candidate": str(index)}
        data["expected_next_game_on"] = first_fixture_on(self.world_a).isoformat() if expected is None else expected
        if expected == "":
            del data["expected_next_game_on"]
        return self.client.post(self.club_url(tab=None), data)

    def signed_stints(self):
        return orm_models.PlayerStint.objects.filter(
            team_id=managed_team_id(self.world_a), acquired_via=AcquisitionRoute.NEW_FOREIGN.value
        )

    def signings(self):
        return [r for r in self.registrations() if r.kind is RegistrationKind.SIGNING]

    def make_foreign(self, count: int) -> None:
        """受け持つ球団の選手 `count` 人を外国人にする（保有の上限を確かめる）。"""
        ids = list(
            orm_models.PlayerStint.objects.filter(team_id=managed_team_id(self.world_a), to_year=None)
            .order_by("player_id")
            .values_list("player_id", flat=True)[:count]
        )
        orm_models.Player.objects.filter(id__in=ids).update(is_foreign_player=True)


class ForeignTabTest(ForeignSigningCase):
    def test_everyone_sees_the_tab_in_the_window_and_only_the_owner_gets_the_buttons(self):
        """GET は誰でも（候補の表は出す）。「獲得する」はオーナーにだけ出す（確認つき・次の公式戦の日を送る）。"""
        content = self.client.get(self.foreign_url()).content.decode()
        self.assertIn('id="club-foreign-midseason"', content)
        self.assertNotIn('id="club-foreign"', content, "オフの形は出さない")
        self.assertEqual(content.count('data-testid="foreign-candidate-row"'), 8)
        self.assertNotIn("獲得する</button>", content)
        self.assertIn("保有 0人／上限 6人", content)

        self.client.force_login(self.owner)
        content = self.client.get(self.foreign_url()).content.decode()
        self.assertEqual(content.count("獲得する</button>"), 8)
        self.assertIn(f'name="expected_next_game_on" value="{first_fixture_on(self.world_a).isoformat()}"', content)
        self.assertIn("confirm(", content, "獲得は取り消せないので確認する")
        self.assertIn("7月31日", content)

    def test_other_tabs_link_to_the_foreign_tab_in_the_window(self):
        self.assertIn("?tab=foreign", self.client.get(self.club_url(tab="active")).content.decode())
        self.assertIn("?tab=foreign", self.client.get(self.club_url(tab="development")).content.decode())

    def test_after_july_the_tab_is_not_shown(self):
        """8月1日から公式戦の終わりまではタブを出さない（期限の後は導線を出さない）。"""
        self.move_the_schedule_past_july()

        self.assertNotIn("?tab=foreign", self.client.get(self.club_url(tab="active")).content.decode())
        response = self.client.get(self.foreign_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tab"], "active", "出せないタブは既定のタブに落とす")
        self.assertNotIn('data-testid="foreign-candidate-row"', response.content.decode())

    def test_at_the_held_limit_the_reason_replaces_the_buttons(self):
        self.make_foreign(MAX_FOREIGN_HELD)
        self.client.force_login(self.owner)

        content = self.client.get(self.foreign_url()).content.decode()
        self.assertIn('data-testid="foreign-midseason-blocked"', content)
        self.assertIn("保有は6人まで", content)
        self.assertNotIn("獲得する</button>", content)
        self.assertEqual(content.count('data-testid="foreign-held-row"'), MAX_FOREIGN_HELD)

    def test_at_the_registered_limit_the_reason_replaces_the_buttons(self):
        self.set_the_limit(23)
        self.client.force_login(self.owner)

        content = self.client.get(self.foreign_url()).content.decode()
        self.assertIn('data-testid="foreign-midseason-blocked"', content)
        self.assertIn("上限（23人）", content)
        self.assertNotIn("獲得する</button>", content)


class SigningTest(ForeignSigningCase):
    def test_the_owner_signs_a_candidate_who_can_play_from_the_next_game(self):
        self.client.force_login(self.owner)
        next_game = first_fixture_on(self.world_a)
        candidate = build_foreign_signing_service(self.world_a).view().candidates[2]

        response = self.post_signing(candidate.index)

        self.assertRedirects(response, self.foreign_url(), fetch_redirect_response=False)
        self.assertIn("を獲得しました", self.last_message(response))
        (stint,) = self.signed_stints()
        self.assertEqual(stint.from_year, YEAR, "期中の獲得は登録日の年（年を渡し忘れると今日の年になる）")
        self.assertEqual(stint.signed_as, ContractStatus.REGISTERED.value)
        self.assertEqual(stint.player.position, candidate.position.label)
        self.assertTrue(stint.player.is_foreign_player)
        self.assertEqual(stint.player.foreign_background, candidate.background.value, "前所属はプロフィール（Q5 B）")
        numbers = orm_models.PlayerStint.objects.filter(team_id=stint.team_id, to_year=None).values_list(
            "number", flat=True
        )
        self.assertEqual(len(numbers), len(set(numbers)), "背番号は重ならない")
        ratings = DjangoRatingsRepository(WorldScope.pennant(self.world_a)).find_by_players([stint.player_id], YEAR)
        self.assertEqual(len(ratings), 1, "獲得した年の能力の行")
        (registration,) = self.signings()
        self.assertEqual((registration.player_id, registration.registered_on), (stint.player_id, next_game))
        # 次の試合の候補に入る
        candidates = {
            player.player_id
            for team in DjangoSimulationContextQuery(WorldScope.pennant(self.world_a)).teams()
            for player in team.players
        }
        self.assertIn(stint.player_id, candidates)

    def test_one_a_year_and_the_tab_says_so(self):
        """1年1人まで。獲得した後は候補の表とボタンを出さず、済んだことを出す。"""
        service = build_foreign_signing_service(self.world_a)
        next_game = first_fixture_on(self.world_a)
        service.sign(0, expected_next_game_on=next_game)

        with self.assertRaisesRegex(InvalidSigning, "1年に1人まで"):
            service.sign(1, expected_next_game_on=next_game)
        self.assertEqual(self.signed_stints().count(), 1)
        self.client.force_login(self.owner)
        content = self.client.get(self.foreign_url()).content.decode()
        self.assertIn('data-testid="foreign-midseason-done"', content)
        self.assertNotIn('data-testid="foreign-candidate-row"', content)
        self.assertIn("今季の獲得 1人／上限 1人", content)
        self.assertEqual(content.count('data-testid="foreign-held-row"'), 1)

    def test_the_held_and_registered_limits_refuse_the_signing(self):
        next_game = first_fixture_on(self.world_a)
        self.make_foreign(MAX_FOREIGN_HELD)
        with self.assertRaisesRegex(InvalidSigning, "保有は6人まで"):
            build_foreign_signing_service(self.world_a).sign(0, expected_next_game_on=next_game)
        orm_models.Player.objects.update(is_foreign_player=False)
        self.set_the_limit(23)
        with self.assertRaisesRegex(RegisteredPlayerLimitExceeded, "上限（23人）"):
            build_foreign_signing_service(self.world_a).sign(0, expected_next_game_on=next_game)
        self.assertFalse(self.signed_stints().exists())
        self.assertEqual(self.signings(), [])

    def test_an_unknown_candidate_is_refused(self):
        with self.assertRaisesRegex(InvalidSigning, "候補にいません"):
            build_foreign_signing_service(self.world_a).sign(8, expected_next_game_on=first_fixture_on(self.world_a))
        self.assertFalse(self.signed_stints().exists())

    def test_anonymous_goes_to_the_login_and_others_get_403(self):
        response = self.post_signing(0)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

        self.client.force_login(self.other)
        self.assertEqual(self.post_signing(0).status_code, 403)
        self.assertFalse(self.signed_stints().exists())

    def test_a_missing_or_stale_form_writes_nothing(self):
        """画面を開いたときの次の公式戦の日が無い・読めない・今と違う、候補が無いなら、何も書かずに理由を出す。"""
        self.client.force_login(self.owner)
        for expected, message in (("", REOPEN), ("x", REOPEN), (f"{YEAR}-01-01", "画面を開いた後に世界が進みました")):
            with self.subTest(expected=expected):
                response = self.post_signing(0, expected=expected)
                self.assertRedirects(response, self.foreign_url(), fetch_redirect_response=False)
                self.assertIn(message, self.last_message(response))
        response = self.client.post(
            self.club_url(tab=None),
            {"section": "foreign_midseason", "expected_next_game_on": first_fixture_on(self.world_a).isoformat()},
        )
        self.assertIn(REOPEN, self.last_message(response))
        self.assertFalse(self.signed_stints().exists())

    def test_after_july_the_signing_is_refused(self):
        self.move_the_schedule_past_july()
        with self.assertRaisesRegex(ReinforcementClosed, "7月31日"):
            build_foreign_signing_service(self.world_a).sign(0, expected_next_game_on=first_fixture_on(self.world_a))
        self.assertFalse(self.signed_stints().exists())

    def test_the_roster_signature_changes_with_a_signing(self):
        """獲得は在籍を足すので、進めている間に獲得したら、進める処理は古い名簿で書かない（目印が変わる）。"""
        teams = DjangoTeamRepository(WorldScope.pennant(self.world_a))
        before = teams.roster_signature()
        build_foreign_signing_service(self.world_a).sign(0, expected_next_game_on=first_fixture_on(self.world_a))
        self.assertNotEqual(teams.roster_signature(), before)


class AfterTheSeasonTest(ScreenCase):
    """シーズンを終えた世界A（公式戦の対戦が残っていない）。"""

    def test_the_window_is_closed_while_the_season_can_be_closed(self):
        """シーズン終了〜締めるまでは窓の外。「外国人」タブはオフの形（#169 F2）で出し、期中の形は出さない。"""
        self.client.force_login(self.owner)
        content = self.client.get(reverse("pennant_club", args=[self.world_a]) + "?tab=foreign").content.decode()
        self.assertIn('id="club-foreign"', content)
        self.assertNotIn('id="club-foreign-midseason"', content)
        self.assertNotIn("獲得する</button>", content)
        with self.assertRaises(ReinforcementClosed):
            build_foreign_signing_service(self.world_a).sign(0, expected_next_game_on=None)

    def test_a_midseason_signing_is_not_an_offseason_rookie(self):
        """締めた後（翌年の開幕前）は窓の中。期中に獲得した外国人は、加入年が翌年でもオフの結果の新人に載せない。"""
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, return_value=()):
            build_pennant_offseason_service(self.world_a).close_season(expected_year=YEAR)
        query = DjangoOffseasonQuery(WorldScope.pennant(self.world_a))
        rookies_before = {fact.player_id for fact in query.facts(YEAR).rookies}

        service = build_foreign_signing_service(self.world_a)
        self.assertTrue(service.is_open())
        opening = first_fixture_on(self.world_a)
        self.assertEqual(opening.year, YEAR + 1)
        signed = service.sign(0, expected_next_game_on=opening)

        rookies = {fact.player_id for fact in query.facts(YEAR).rookies}
        self.assertNotIn(signed.player_id, rookies)
        self.assertEqual(rookies, rookies_before, "オフの新人と獲得した外国人はそのまま")
