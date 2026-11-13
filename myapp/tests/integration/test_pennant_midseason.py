"""期中の支配下登録（#170 P3a）: 編成の「育成」タブ・支配下に登録・補強の窓（#207）・登録日の記録・締める処理との関係。

期待値の根拠: 補強の窓は締めた後（開幕前）から7月31日まで（#170 ユーザーの決定 Q4 A。野球協約）、
期限の後は導線を出さない（#207・CLAUDE.md「押しても弾かれるだけの導線を見せない」）、見るのは誰でも・書くのはオーナー
（Wiki「ペナントモード」）、POST のキーの欠落はエラー（CLAUDE.md「JSON API の部分更新」と同じ考え）、オフの結果の
「支配下登録（昇格）」は登録日の無い選手だけ（P1 の申し送り）。
窓の規則そのものは `tests/domain/test_reinforcement.py`。

世界は `test_pennant_home.py` の `HomeCase`（球団2つ・各23人。世界Aは開幕前でオーナーは `owner`）。分岐元に育成が
いないので、受け持つ球団に育成を足してから確かめる。締める処理との関係だけは、シーズンを終えた世界
（`test_pennant_offseason_screens.py` の `ScreenCase`）で確かめる。
"""

from datetime import date
from unittest import mock

from django.urls import reverse

from myapp.domain.exceptions import (
    InvalidContract,
    InvalidWorld,
    PlayerNotFound,
    RegisteredPlayerLimitExceeded,
    ReinforcementClosed,
    SeasonNotFinished,
    TeamNotFound,
)
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.reinforcement import MidseasonRegistration
from myapp.domain.pennant.world import WorldScope
from myapp.domain.simulation.ratings import PitcherRatings
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus, Position
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoOffseasonQuery, DjangoSimulationContextQuery
from myapp.infrastructure.repositories import (
    DjangoMidseasonRegistrationRepository,
    DjangoRatingsRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
)
from myapp.presentation.club_input import REOPEN
from myapp.presentation.views import (
    build_midseason_registration_service,
    build_pennant_offseason_service,
    build_pennant_season_service,
)

from .test_pennant_advance import YEAR
from .test_pennant_home import HomeCase
from .test_pennant_offseason_screens import DECIDE_RELEASES, DECIDE_RETIREMENTS, ScreenCase


def managed_team_id(world_id: int) -> int:
    team_id = DjangoWorldRepository().find_by_id(world_id).managed_team_id
    assert team_id is not None, "テストの世界には受け持つ球団がある"
    return team_id


def add_development(world_id: int, name: str, number: str, *, draft_year: int, year: int, value: int = 40) -> int:
    """受け持つ球団に、育成ドラフトの翌年から在籍する育成の投手（30歳）を足し、`year` 年の能力を付ける。"""
    player = orm_models.Player.objects.create(
        name=name,
        position=Position.PITCHER.label,
        birth_date=date(year - 30, 1, 1),
        draft_year=draft_year,
        draft_round=1,
        is_development_draft=True,
    )
    orm_models.PlayerStint.objects.create(
        player=player,
        team_id=managed_team_id(world_id),
        number=number,
        from_year=draft_year + 1,
        signed_as=ContractStatus.DEVELOPMENTAL.value,
        acquired_via=AcquisitionRoute.DEVELOPMENTAL_DRAFT.value,
    )
    DjangoRatingsRepository(WorldScope.pennant(world_id)).add_all(
        [PlayerRatings(player.id, year, PitcherRatings(value, value, value, 50))]
    )
    return player.id


def first_fixture_on(world_id: int) -> date:
    first = orm_models.PennantFixture.objects.filter(home_team__league__world_id=world_id).order_by("date").first()
    assert first is not None, "日程がある"
    return first.date


class MidseasonCase(HomeCase):
    """世界Aは開幕前（補強の窓の中）。受け持つ球団に育成を2人（1年目と、満了の年の3年目）。"""

    rookie: int
    veteran: int

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # 画面から作る世界と同じく、開幕年の日程まで作っておく（次の公式戦の日で窓を判定する）
        build_pennant_season_service(cls.world_a).ensure_schedule()
        cls.rookie = add_development(cls.world_a, "育成一年", "150", draft_year=YEAR - 1, year=YEAR)
        cls.veteran = add_development(cls.world_a, "育成三年", "151", draft_year=YEAR - 3, year=YEAR)

    def club_url(self, tab: str | None = "development") -> str:
        url = reverse("pennant_club", args=[self.world_a])
        return url if tab is None else f"{url}?tab={tab}"

    def post_promotion(self, player_id, *, expected: str | None = None):
        data = {"section": "development", "player": str(player_id)}
        data["expected_next_game_on"] = first_fixture_on(self.world_a).isoformat() if expected is None else expected
        if expected == "":
            del data["expected_next_game_on"]
        return self.client.post(self.club_url(tab=None), data)

    def stint(self, player_id: int) -> orm_models.PlayerStint:
        return orm_models.PlayerStint.objects.get(player_id=player_id)

    def registrations(self) -> list[MidseasonRegistration]:
        return DjangoMidseasonRegistrationRepository(WorldScope.pennant(self.world_a)).find_by_team(
            managed_team_id(self.world_a)
        )

    def move_the_schedule_past_july(self) -> None:
        """7月31日までの対戦を消し、次の公式戦を8月以降にする（期限の後の世界を作る）。"""
        orm_models.PennantFixture.objects.filter(
            home_team__league__world_id=self.world_a, date__lte=date(YEAR, 7, 31)
        ).delete()

    def set_the_limit(self, limit: int) -> None:
        orm_models.League.objects.filter(world_id=self.world_a).update(registered_player_limit=limit)


class DevelopmentTabTest(MidseasonCase):
    def test_everyone_sees_the_tab_in_the_window_and_only_the_owner_gets_the_buttons(self):
        """GET は誰でも（育成の表は出す）。「支配下に登録」はオーナーにだけ出す。"""
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn("?tab=development", content)
        self.assertEqual(content.count('data-testid="development-row"'), 2)
        self.assertNotIn("支配下に登録</button>", content)

        self.client.force_login(self.owner)
        content = self.client.get(self.club_url()).content.decode()
        self.assertEqual(content.count("支配下に登録</button>"), 2)
        self.assertIn(f'name="expected_next_game_on" value="{first_fixture_on(self.world_a).isoformat()}"', content)
        self.assertIn("confirm(", content, "登録は取り消せないので確認する")
        self.assertIn("7月31日", content)

    def test_the_rows_show_the_contract_season_and_the_expiring_mark(self):
        """1年目は満了の印なし、3年目（`DEVELOPMENT_CONTRACT_YEARS`）は「今季で満了」。"""
        content = self.client.get(self.club_url()).content.decode()
        self.assertIn("1年目", content)
        self.assertIn("3年目", content)
        self.assertEqual(content.count('data-testid="expiring-badge"'), 1)

    def test_other_tabs_link_to_the_development_tab_in_the_window(self):
        self.assertIn("?tab=development", self.client.get(self.club_url(tab="active")).content.decode())

    def test_after_july_the_tab_is_not_shown(self):
        """8月1日から公式戦の終わりまではタブを出さない（期限の後は導線を出さない）。"""
        self.move_the_schedule_past_july()

        self.assertNotIn("?tab=development", self.client.get(self.club_url(tab="active")).content.decode())
        response = self.client.get(self.club_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tab"], "active", "出せないタブは既定のタブに落とす")
        self.assertNotIn('data-testid="development-row"', response.content.decode())

    def test_at_the_limit_the_reason_replaces_the_buttons(self):
        """支配下が上限に達していれば、ボタンの代わりに理由を出す（押しても弾かれるだけの導線を見せない）。"""
        self.set_the_limit(23)
        self.client.force_login(self.owner)

        content = self.client.get(self.club_url()).content.decode()
        self.assertIn('data-testid="development-blocked"', content)
        self.assertIn("上限（23人）", content)
        self.assertNotIn("支配下に登録</button>", content)


class PromotionTest(MidseasonCase):
    def test_the_owner_registers_a_prospect_who_can_play_from_the_next_game(self):
        self.client.force_login(self.owner)
        next_game = first_fixture_on(self.world_a)

        response = self.post_promotion(self.rookie)

        self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
        self.assertIn("を支配下に登録しました", self.last_message(response))
        stint = self.stint(self.rookie)
        self.assertEqual(stint.promoted_year, YEAR, "期中の昇格は登録日の年（年を渡し忘れると今日の年になる）")
        self.assertEqual(stint.number_before_promotion, "150")
        self.assertLessEqual(int(stint.number), 99)
        self.assertEqual(
            self.registrations(),
            [
                MidseasonRegistration(
                    team_id=managed_team_id(self.world_a), player_id=self.rookie, registered_on=next_game
                )
            ],
        )
        # 次の試合の候補に入る（育成は入らない）
        candidates = {
            player.player_id
            for team in DjangoSimulationContextQuery(WorldScope.pennant(self.world_a)).teams()
            for player in team.players
        }
        self.assertIn(self.rookie, candidates)
        self.assertNotIn(self.veteran, candidates)
        numbers = orm_models.PlayerStint.objects.filter(team_id=stint.team_id, to_year=None).values_list(
            "number", flat=True
        )
        self.assertEqual(len(numbers), len(set(numbers)), "背番号は重ならない")

    def test_anonymous_goes_to_the_login_and_others_get_403(self):
        response = self.post_promotion(self.rookie)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

        self.client.force_login(self.other)
        self.assertEqual(self.post_promotion(self.rookie).status_code, 403)
        self.assertIsNone(self.stint(self.rookie).promoted_year)
        self.assertEqual(self.registrations(), [])

    def test_a_missing_or_stale_next_game_date_writes_nothing(self):
        """画面を開いたときの次の公式戦の日が無い・読めない・今と違うなら、何も書かずに理由を出す。"""
        self.client.force_login(self.owner)
        for expected, message in (("", REOPEN), ("x", REOPEN), (f"{YEAR}-01-01", "画面を開いた後に世界が進みました")):
            with self.subTest(expected=expected):
                response = self.post_promotion(self.rookie, expected=expected)
                self.assertRedirects(response, self.club_url(), fetch_redirect_response=False)
                self.assertIn(message, self.last_message(response))
        self.assertIsNone(self.stint(self.rookie).promoted_year)
        self.assertEqual(self.registrations(), [])

    def test_a_missing_player_is_an_error(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.club_url(tab=None),
            {"section": "development", "expected_next_game_on": first_fixture_on(self.world_a).isoformat()},
        )
        self.assertIn(REOPEN, self.last_message(response))

    def test_after_july_the_registration_is_refused(self):
        self.move_the_schedule_past_july()
        service = build_midseason_registration_service(self.world_a)

        with self.assertRaisesRegex(ReinforcementClosed, "7月31日"):
            service.promote(self.rookie, expected_next_game_on=first_fixture_on(self.world_a))
        self.assertIsNone(self.stint(self.rookie).promoted_year)
        self.assertEqual(self.registrations(), [])

    def test_july_31_is_the_last_day(self):
        """次の公式戦が7月31日なら登録できる（その日の試合から出られる）。"""
        fixtures = orm_models.PennantFixture.objects.filter(home_team__league__world_id=self.world_a)
        fixtures.filter(date__lt=date(YEAR, 7, 31)).delete()
        # 7月31日に試合が無い日程なら、次の対戦を7月31日に動かす（次の公式戦が期限の日の世界を作る）
        fixtures.filter(date=first_fixture_on(self.world_a)).update(date=date(YEAR, 7, 31))
        build_midseason_registration_service(self.world_a).promote(
            self.rookie, expected_next_game_on=date(YEAR, 7, 31)
        )
        self.assertEqual(self.stint(self.rookie).promoted_year, YEAR)

    def test_a_registered_player_and_a_player_of_another_club_cannot_be_registered(self):
        service = build_midseason_registration_service(self.world_a)
        next_game = first_fixture_on(self.world_a)
        service.promote(self.rookie, expected_next_game_on=next_game)

        with self.assertRaisesRegex(InvalidContract, "育成選手ではない"):
            service.promote(self.rookie, expected_next_game_on=next_game)
        rival_player = (
            orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_a, to_year=None)
            .exclude(team_id=managed_team_id(self.world_a))
            .values_list("player_id", flat=True)
            .first()
        )
        with self.assertRaises(PlayerNotFound):
            service.promote(rival_player, expected_next_game_on=next_game)
        self.assertEqual(len(self.registrations()), 1)

    def test_the_limit_refuses_the_registration(self):
        self.set_the_limit(23)
        with self.assertRaisesRegex(RegisteredPlayerLimitExceeded, "上限（23人）"):
            build_midseason_registration_service(self.world_a).promote(
                self.rookie, expected_next_game_on=first_fixture_on(self.world_a)
            )
        self.assertIsNone(self.stint(self.rookie).promoted_year)

    def test_the_roster_signature_changes_with_a_promotion(self):
        """昇格は在籍の行を書き換えるだけなので、目印に昇格した在籍の数を足してある（締める処理が前後で比べる）。"""
        teams = DjangoTeamRepository(WorldScope.pennant(self.world_a))
        before = teams.roster_signature()
        build_midseason_registration_service(self.world_a).promote(
            self.rookie, expected_next_game_on=first_fixture_on(self.world_a)
        )
        self.assertNotEqual(teams.roster_signature(), before)

    def test_the_window_and_the_close_do_not_overlap(self):
        """窓の中（公式戦の対戦が残っている）では締められない。"""
        with self.assertRaises(SeasonNotFinished):
            build_pennant_offseason_service(self.world_a).close_season()


class RegistrationRepositoryTest(MidseasonCase):
    def test_the_registration_year_must_match_the_promotion_year(self):
        """登録日の年と在籍の昇格の年は、書く前に照合する（同じ事実が2か所にある）。"""
        repository = DjangoMidseasonRegistrationRepository(WorldScope.pennant(self.world_a))
        with self.assertRaises(InvalidContract):
            repository.add(MidseasonRegistration(managed_team_id(self.world_a), self.rookie, date(YEAR, 5, 1)))
        self.assertEqual(self.registrations(), [])

    def test_the_real_data_and_other_worlds_cannot_be_written(self):
        registration = MidseasonRegistration(managed_team_id(self.world_a), self.rookie, date(YEAR, 5, 1))
        with self.assertRaises(InvalidWorld):
            DjangoMidseasonRegistrationRepository(WorldScope.real()).add(registration)
        with self.assertRaises(TeamNotFound):
            DjangoMidseasonRegistrationRepository(WorldScope.pennant(self.world_b)).add(registration)
        with self.assertRaises(TeamNotFound):
            DjangoMidseasonRegistrationRepository(WorldScope.pennant(self.world_b)).find_by_team(
                managed_team_id(self.world_a)
            )

    def test_deleting_the_world_deletes_the_registrations(self):
        build_midseason_registration_service(self.world_a).promote(
            self.rookie, expected_next_game_on=first_fixture_on(self.world_a)
        )
        DjangoWorldRepository().delete(self.world_a)
        self.assertFalse(orm_models.PennantMidseasonRegistration.objects.exists())


class AfterTheSeasonTest(ScreenCase):
    """シーズンを終えた世界A（公式戦の対戦が残っていない）。"""

    def test_the_window_is_closed_while_the_season_can_be_closed(self):
        """シーズン終了〜締めるまで（ポストシーズン中も）は窓の外。「戦力外」タブと「育成」タブは同時に出ない。"""
        prospect = add_development(self.world_a, "育成期末", "150", draft_year=YEAR - 1, year=YEAR)
        self.client.force_login(self.owner)

        content = self.client.get(reverse("pennant_club", args=[self.world_a])).content.decode()
        self.assertIn("?tab=release", content)
        self.assertNotIn("?tab=development", content)
        with self.assertRaises(ReinforcementClosed):
            build_midseason_registration_service(self.world_a).promote(prospect, expected_next_game_on=None)

    def test_after_the_close_the_window_opens_and_a_midseason_promotion_is_not_an_offseason_promotion(self):
        """締めた後（翌年の開幕前）は窓の中。期中に上げた選手は、昇格の年が翌年でもオフの結果の昇格に載せない。"""
        prospect = add_development(self.world_a, "育成期中", "150", draft_year=YEAR - 1, year=YEAR, value=10)
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, return_value=()):
            build_pennant_offseason_service(self.world_a).close_season(expected_year=YEAR)
        self.assertIsNone(self.stint_of(prospect).promoted_year, "見込みの低い育成はオフに上がらない")

        service = build_midseason_registration_service(self.world_a)
        self.assertTrue(service.is_open())
        opening = first_fixture_on(self.world_a)
        self.assertEqual(opening.year, YEAR + 1)
        service.promote(prospect, expected_next_game_on=opening)

        self.assertEqual(self.stint_of(prospect).promoted_year, YEAR + 1)
        facts = DjangoOffseasonQuery(WorldScope.pennant(self.world_a)).facts(YEAR)
        self.assertNotIn(prospect, {fact.player_id for fact in facts.promoted})

    @staticmethod
    def stint_of(player_id: int) -> orm_models.PlayerStint:
        return orm_models.PlayerStint.objects.get(player_id=player_id)
