"""AI の期中の昇格（#170 P3b）と外国人の獲得（#169 F3）: 日を進める処理が判定日（6月1日・7月1日）をまたぐときに、
AI の球団の育成を支配下に上げ、昇格の後の球団で1軍の外国人枠に空きがあれば外国人を獲得する。

期待値の根拠: #170 のユーザーの決定 Q2 A（2026-11-12。判定日は6月1日・7月1日、1軍の同じ位置の基準以上の育成、
1判定日1球団1人・1年2人まで、受け持つ球団は対象にしない）、P3a の申し送り（登録日は判定日以降の最初の公式戦の日で、
上げた選手はその日の試合から出られる。登録日は `MidseasonRegistration` に書く。進める処理が名簿を読んだ後に
GM が登録したら、
古い名簿で試合を書かない）、ペナントの進行の不変条件「1日ずつ進めてもまとめて進めても同じ結果」（Wiki「ペナントモードの仕組み」）。
規則そのもの（基準・上限・空ける枠・窓）は `tests/domain/test_midseason_promotions.py`。

世界は `test_pennant_advance.py` の `SeasonCase`（球団2つ・各23人。受け持つのはホーム球団）。分岐元に育成がいないので、
世界Aのライバル球団（AI）と受け持つ球団に育成の投手を足してから進める。
"""

from datetime import date
from unittest import mock

from django.db import transaction

from myapp.application import pennant_midseason_review as review_module
from myapp.application.pennant_simulation import DaySimulator
from myapp.domain.exceptions import AlreadyAdvanced, TeamNotFound
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.reinforcement import RegistrationKind
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.simulation.ratings import PitcherRatings
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus, Position
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoDevelopmentRosterQuery
from myapp.infrastructure.repositories import (
    DjangoMidseasonRegistrationRepository,
    DjangoRatingsRepository,
    DjangoTeamRepository,
)
from myapp.presentation.views import build_midseason_registration_service, build_pennant_season_service

from .test_pennant_advance import YEAR, SeasonCase

JUNE_1 = date(YEAR, 6, 1)
JULY_1 = date(YEAR, 7, 1)


def add_development_pitcher(world_id: int, team_id: int, name: str, number: str, value: int) -> int:
    """球団に、育成ドラフトで入った育成の投手（25歳）を足し、`YEAR` 年の能力（抑える力 `value`）を付ける。"""
    player = orm_models.Player.objects.create(
        name=name,
        position=Position.PITCHER.label,
        birth_date=date(YEAR - 25, 1, 1),
        draft_year=YEAR - 2,
        draft_round=1,
        is_development_draft=True,
    )
    orm_models.PlayerStint.objects.create(
        player=player,
        team_id=team_id,
        number=number,
        from_year=YEAR - 1,
        signed_as=ContractStatus.DEVELOPMENTAL.value,
        acquired_via=AcquisitionRoute.DEVELOPMENTAL_DRAFT.value,
    )
    DjangoRatingsRepository(WorldScope.pennant(world_id)).add_all(
        [PlayerRatings(player.id, YEAR, PitcherRatings(value, value, value, 60))]
    )
    return player.id


class MidseasonReviewCase(SeasonCase):
    """世界Aのライバル球団（AI）に強い育成3人と弱い育成1人、受け持つ球団に強い育成1人。

    比べる基準の「シーズン終了まで一度に進めた結果」（試合と昇格）は、クラスで1回だけ作る（`whole_season`）。
    """

    whole_season: tuple
    first_game_from: dict[date, date]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        world = cls.world_a
        rival = orm_models.Team.objects.get(league__world_id=world, name=cls.rival.name).id
        managed = orm_models.Team.objects.get(league__world_id=world, name=cls.home.name).id
        cls.strong = [
            add_development_pitcher(world, rival, f"ラ育成{n}", f"1{n:02d}", value)
            for n, value in enumerate((95, 90, 85), start=1)
        ]
        cls.weak = add_development_pitcher(world, rival, "ラ育成弱", "199", 1)
        cls.managed_prospect = add_development_pitcher(world, managed, "ホ育成", "101", 95)
        build_pennant_season_service(world).ensure_schedule()
        dates = sorted({fixture.date for fixture in cls.fixtures_of(world)})
        cls.first_game_from = {review: min(d for d in dates if d >= review) for review in (JUNE_1, JULY_1)}
        cls.whole_season = cls.replay(world, lambda: cls.advance(world, AdvanceTarget.SEASON_END))

    @classmethod
    def replay(cls, world_id, run):
        """`run` で世界を進めた結果（試合と期中の登録・在籍の昇格）を取り、世界を元に戻す。"""
        savepoint = transaction.savepoint()
        try:
            run()
            return cls.snapshot(world_id), cls.promotions(world_id)
        finally:
            transaction.savepoint_rollback(savepoint)

    @staticmethod
    def promotions(world_id):
        registrations = tuple(
            orm_models.PennantMidseasonRegistration.objects.filter(team__league__world_id=world_id)
            .order_by("registered_on", "player__name")
            .values_list("player__name", "team__name", "registered_on", "kind")
        )
        stints = tuple(
            orm_models.PlayerStint.objects.filter(team__league__world_id=world_id, promoted_year__isnull=False)
            .order_by("player__name")
            .values_list("player__name", "promoted_year", "number", "number_before_promotion")
        )
        # 期中に獲得した外国人（#169 F3）の在籍（名前・球団・加入年・背番号・区分）
        signed = tuple(
            orm_models.PlayerStint.objects.filter(
                team__league__world_id=world_id, from_year=YEAR, acquired_via=AcquisitionRoute.NEW_FOREIGN.value
            )
            .order_by("player__name")
            .values_list("player__name", "team__name", "from_year", "number", "signed_as")
        )
        return registrations, stints, signed

    def advance_until(self, target, last_day):
        """`target` の単位で、世界の今日が `last_day` 以降になるまで進める。"""
        while self.advance(self.world_a, target).today < last_day:
            pass


class MidseasonReviewTest(MidseasonReviewCase):
    def test_the_ai_promotes_one_prospect_on_each_review_date(self):
        """6月1日・7月1日に1人ずつ（強い順）。登録日は判定日以降の最初の試合日。3人目は1年2人までで上げない。"""
        registrations, stints, _ = self.whole_season[1]
        self.assertEqual(
            tuple(row[:3] for row in registrations if row[3] == RegistrationKind.PROMOTION.value),
            (
                ("ラ育成1", self.rival.name, self.first_game_from[JUNE_1]),
                ("ラ育成2", self.rival.name, self.first_game_from[JULY_1]),
            ),
        )
        self.assertEqual([(name, year) for name, year, _, _ in stints], [("ラ育成1", YEAR), ("ラ育成2", YEAR)])
        for name, _, number, before in stints:
            with self.subTest(player=name):
                self.assertNotEqual(number, before, "背番号は支配下の番号に付け替える")

    def test_the_managed_club_and_a_weak_prospect_are_not_promoted(self):
        """受け持つ球団の育成は GM が決める（AI は触れない）。1軍の基準に届かない育成は上げない。"""
        names = {name for name, *_ in self.whole_season[1][0]}
        self.assertNotIn("ホ育成", names)
        self.assertNotIn("ラ育成弱", names)

    def test_a_promoted_player_pitches_only_from_the_registration_date(self):
        """上げた選手は登録日の試合から出られ、それより前には出ない。"""
        pitched = sorted({row[0] for row in self.whole_season[0][1] if row[2] == "ラ育成1"})
        self.assertTrue(pitched, "強い投手は1軍に入って投げる")
        self.assertGreaterEqual(pitched[0], self.first_game_from[JUNE_1])

    def test_days_one_by_one_around_the_review_dates_equal_the_whole_season_at_once(self):
        """判定日の前後を1日ずつ、ほかを1週間ずつ進めても、試合と昇格はシーズンを一度に進めたのと同じ。"""

        def mixed():
            self.advance_until(AdvanceTarget.WEEK, date(YEAR, 5, 27))
            self.advance_until(AdvanceTarget.DAY, date(YEAR, 6, 4))
            self.advance_until(AdvanceTarget.WEEK, date(YEAR, 6, 27))
            self.advance_until(AdvanceTarget.DAY, date(YEAR, 7, 3))
            self.advance(self.world_a, AdvanceTarget.SEASON_END)

        self.assertEqual(self.whole_season, self.replay(self.world_a, mixed))

    def test_a_month_ending_after_the_review_date_equals_the_whole_season(self):
        """判定日をまたぐ範囲（月の終わりまで・1週間）で進めても同じ。"""

        def months():
            for _ in range(12):
                if self.advance(self.world_a, AdvanceTarget.MONTH_END).season_finished:
                    return

        self.assertEqual(self.whole_season, self.replay(self.world_a, months))


class MidseasonForeignSigningReviewTest(MidseasonReviewCase):
    """AI の期中の外国人の獲得（#169 F3）。SeasonCase の球団は外国人が0人なので、AI の球団は1軍の外国人枠に空きがあり、
    最初の判定日（6月1日）に昇格の後で1人獲得する。1年1人までなので7月1日には獲得しない。"""

    def signed_name(self) -> str:
        (row,) = self.whole_season[1][2]
        return row[0]

    def test_the_ai_signs_one_foreign_player_on_the_first_review_date(self):
        registrations, _, signed = self.whole_season[1]
        (row,) = signed
        name, team, from_year, number, signed_as = row
        self.assertEqual((team, from_year, signed_as), (self.rival.name, YEAR, ContractStatus.REGISTERED.value))
        self.assertEqual(
            [r[:3] for r in registrations if r[3] == RegistrationKind.SIGNING.value],
            [(name, self.rival.name, self.first_game_from[JUNE_1])],
            "登録日は判定日以降の最初の試合日。受け持つ球団は獲得しない（GM が決める）",
        )
        self.assertTrue(number)

    def test_the_signed_player_has_ratings_of_the_year_and_plays_only_from_the_registration_date(self):
        def run():
            self.advance(self.world_a, AdvanceTarget.SEASON_END)
            player = orm_models.Player.objects.get(
                name=self.signed_name(), stints__team__league__world_id=self.world_a
            )
            self.assertTrue(player.is_foreign_player)
            ratings = DjangoRatingsRepository(WorldScope.pennant(self.world_a)).find_by_players([player.id], YEAR)
            self.assertEqual(len(ratings), 1, "獲得した年の能力の行がある")

        self.replay(self.world_a, run)
        plate_appearances = self.whole_season[0][0]
        name = self.signed_name()
        played = sorted({row[0] for row in plate_appearances if name in (row[8], row[9])})
        self.assertTrue(played, "外国人枠の空きを埋める選手なので1軍で出る")
        self.assertGreaterEqual(played[0], self.first_game_from[JUNE_1])


class RosterChangedWhileAdvancingTest(MidseasonReviewCase):
    def test_a_gm_registration_during_an_advance_stops_it_before_writing_stale_games(self):
        """進める処理が登録候補を読んだ後に GM が期中の登録をしたら、古い名簿の試合を書かずに AlreadyAdvanced
        （登録した選手が出られるはずの試合を、その選手抜きで作らない。P3a の申し送り）。書き出し済みの日は残る。"""
        self.advance(self.world_a, AdvanceTarget.WEEK)
        before = self.snapshot(self.world_a)
        today = self.advance(self.world_a, AdvanceTarget.DAY).today
        registration = build_midseason_registration_service(self.world_a)
        next_on = registration.view().next_game_on
        real_play = DaySimulator.play
        promoted = []

        def play_and_register(simulator, *args, **kwargs):
            if not promoted:
                promoted.append(registration.promote(self.managed_prospect, expected_next_game_on=next_on))
            return real_play(simulator, *args, **kwargs)

        with mock.patch.object(DaySimulator, "play", play_and_register), self.assertRaises(AlreadyAdvanced):
            self.advance(self.world_a, AdvanceTarget.WEEK)

        self.assertTrue(promoted)
        self.assertEqual(build_pennant_season_service(self.world_a)._context_query.last_played_on(), today)
        self.assertGreater(len(self.snapshot(self.world_a)[0]), len(before[0]))

    def test_a_gm_registration_after_the_ai_decided_stops_the_review_before_writing(self):
        """AI が判定日の昇格を決めた後（球団は外で読む）に GM が期中の登録をしたら、AI の昇格を書かずに
        AlreadyAdvanced（外で読んだ古い集約で上書きしない）。判定日の前の日までの試合は残る。"""
        while build_pennant_season_service(self.world_a)._fixtures.first_date() < JUNE_1:
            today = self.advance(self.world_a, AdvanceTarget.DAY).today
        registration = build_midseason_registration_service(self.world_a)
        next_on = registration.view().next_game_on
        real_decide = review_module.decide_midseason_promotions
        promoted = []

        def decide_and_register(*args, **kwargs):
            decided = real_decide(*args, **kwargs)
            promoted.append(registration.promote(self.managed_prospect, expected_next_game_on=next_on))
            return decided

        with (
            mock.patch.object(review_module, "decide_midseason_promotions", decide_and_register),
            self.assertRaises(AlreadyAdvanced),
        ):
            self.advance(self.world_a, AdvanceTarget.DAY)

        self.assertTrue(promoted)
        self.assertEqual(
            list(
                orm_models.PennantMidseasonRegistration.objects.filter(
                    team__league__world_id=self.world_a
                ).values_list("player__name", flat=True)
            ),
            ["ホ育成"],
            "GM の登録だけが残り、AI の昇格・獲得は書かない",
        )
        self.assertFalse(
            orm_models.PlayerStint.objects.filter(
                team__league__world_id=self.world_a, acquired_via=AcquisitionRoute.NEW_FOREIGN.value
            ).exists()
        )
        self.assertFalse(
            orm_models.PlayerStint.objects.filter(player_id__in=self.strong, promoted_year__isnull=False).exists()
        )
        self.assertEqual(build_pennant_season_service(self.world_a)._context_query.last_played_on(), today)


class ReviewMaterialsTest(MidseasonReviewCase):
    """判定日の材料の参照クエリとリポジトリ（範囲の中だけを読む）。"""

    def test_squads_list_the_development_players_and_the_registered_count_of_every_club(self):
        squads = {
            squad.team_id: squad
            for squad in DjangoDevelopmentRosterQuery(WorldScope.pennant(self.world_a)).squads(YEAR)
        }
        rival = orm_models.Team.objects.get(league__world_id=self.world_a, name=self.rival.name).id
        self.assertEqual(len(squads), 2, "世界Aの球団だけ")
        squad = squads[rival]
        self.assertEqual(squad.registered_count, 23)
        self.assertEqual(
            squad.registered_limit, orm_models.League.objects.get(world_id=self.world_a).registered_player_limit
        )
        self.assertEqual(
            [(player.player_id, player.position) for player in squad.players],
            [(player_id, Position.PITCHER) for player_id in sorted([*self.strong, self.weak])],
        )
        self.assertTrue({"101", "199", "1"} <= squad.numbers, "支配下と育成の両方の背番号")
        self.assertEqual(squad.foreign_count, 0, "外国人の保有の人数（#169 F3。分岐元に外国人はいない）")
        self.assertFalse(any(player.is_foreign for player in squad.players))

    def test_only_the_given_clubs_of_the_world_are_read_with_their_roster(self):
        teams = DjangoTeamRepository(WorldScope.pennant(self.world_a))
        rival = orm_models.Team.objects.get(league__world_id=self.world_a, name=self.rival.name).id
        (team,) = teams.find_by_ids_with_roster([rival])
        self.assertEqual(team.id, rival)
        self.assertEqual(len(team.active_players), 23 + 4)
        other = orm_models.Team.objects.get(league__world_id=self.world_b, name=self.rival.name).id
        with self.assertRaises(TeamNotFound):
            teams.find_by_ids_with_roster([rival, other])

    def test_registrations_of_a_year_are_read_only_inside_the_world(self):
        def run():
            self.advance(self.world_a, AdvanceTarget.SEASON_END)
            mine = DjangoMidseasonRegistrationRepository(WorldScope.pennant(self.world_a))
            self.assertEqual(
                [
                    (r.player_id, r.registered_on)
                    for r in mine.find_in_year(YEAR)
                    if r.kind is RegistrationKind.PROMOTION
                ],
                [(self.strong[0], self.first_game_from[JUNE_1]), (self.strong[1], self.first_game_from[JULY_1])],
            )
            # 期中の外国人の獲得（#169 F3）も区分つきで読める
            self.assertEqual(
                [(r.registered_on, r.kind) for r in mine.find_in_year(YEAR) if r.kind is RegistrationKind.SIGNING],
                [(self.first_game_from[JUNE_1], RegistrationKind.SIGNING)],
            )
            self.assertEqual(mine.find_in_year(YEAR + 1), [])
            other = DjangoMidseasonRegistrationRepository(WorldScope.pennant(self.world_b))
            self.assertEqual(other.find_in_year(YEAR), [])

        self.replay(self.world_a, run)
