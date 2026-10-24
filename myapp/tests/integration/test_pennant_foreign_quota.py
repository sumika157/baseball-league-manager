"""ペナントの世界の外国人枠と FA 資格の取得（協約 第82条 (4)。#204 段階B）。

世界の FA は分岐後の記録（開幕年から）で数えるので、ドラフトを経ていない外国人が対象外になるのは早くても
開幕年+8年（9シーズン目）。それより前の「進める」と編成画面では、出場を読まずに済ませる（前段の絞り込み）。
"""

from unittest import mock

from django.test import SimpleTestCase

from myapp.application.dto import SimulationPlayer, SimulationTeam
from myapp.application.foreign_quota import ForeignQuotaExemptions, QuotaExemptions
from myapp.application.pennant_season import pool_of
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import BattingLine, ForeignPlayerRules, Position
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoServiceHistoryQuery
from myapp.presentation.views import build_pennant_offseason_service, build_pennant_season_service

from ..helpers import play_game
from .test_club_plan import players_of, team_of
from .test_club_screen import ClubScreenCase
from .test_pennant_advance import YEAR
from .test_pennant_home import HomeCase
from .test_pennant_offseason_screens import ScreenCase

ENSURE_RELEASABLE = "myapp.application.pennant_offseason.ensure_releasable"


def make_foreign(player_id: int) -> None:
    orm_models.Player.objects.filter(id=player_id).update(is_foreign_player=True)


class WorldExemptionTest(HomeCase):
    """世界の範囲の試合だけで取得を数え、9シーズン目（開幕年+8年）に初めて対象外になる。"""

    def setUp(self):
        self.scope = WorldScope.pennant(self.world_a)
        home, rival = team_of(self.world_a, self.home.name), team_of(self.world_a, self.rival.name)
        self.player_id = players_of(home)[0].id
        make_foreign(self.player_id)
        # 開幕年から8シーズン、毎年 4/1 と 9/30 に出場する（毎年1シーズン）
        for year in range(YEAR, YEAR + 8):
            for month, day in ((4, 1), (9, 30)):
                play_game(
                    home,
                    rival,
                    year=year,
                    month=month,
                    day=day,
                    batting={self.player_id: BattingLine()},
                    scope=self.scope,
                )

    def exemptions(self, year: int) -> QuotaExemptions:
        return ForeignQuotaExemptions(DjangoServiceHistoryQuery(self.scope)).for_year([self.player_id], year)

    def test_the_ninth_season_is_the_first_exempt_one(self):
        self.assertEqual(dict(self.exemptions(YEAR + 8).exempt_from), {self.player_id: YEAR + 8})
        self.assertEqual(dict(self.exemptions(YEAR + 7).exempt_from), {}, "8シーズン目はまだ数える")

    def test_through_the_eighth_season_the_appearances_are_not_read(self):
        """8シーズン目までは取得しえないので、出場（重い）を読まない。9シーズン目だけ読む。"""
        with mock.patch.object(
            DjangoServiceHistoryQuery,
            "load_service_history",
            autospec=True,
            side_effect=DjangoServiceHistoryQuery.load_service_history,
        ) as load:
            for year in range(YEAR, YEAR + 8):
                self.exemptions(year)
            self.assertEqual(load.call_count, 0)
            self.exemptions(YEAR + 8)
            self.assertEqual(load.call_count, 1)

    def test_the_real_data_does_not_see_the_worlds_players(self):
        subjects = DjangoServiceHistoryQuery(WorldScope.real()).load_fa_subjects([self.player_id], YEAR + 8)
        self.assertEqual(subjects.subjects, [])
        mine = DjangoServiceHistoryQuery(self.scope).load_fa_subjects([self.player_id], YEAR + 8)
        self.assertEqual([(s.player_id, s.first_stint_year) for s in mine.subjects], [(self.player_id, YEAR)])
        self.assertEqual(mine.records_from_year, YEAR)


class AdvanceTest(HomeCase):
    def test_one_advance_derives_the_exemptions_once_for_the_year_it_plays(self):
        self.start_season()
        original = ForeignQuotaExemptions.for_year

        with mock.patch.object(ForeignQuotaExemptions, "for_year", autospec=True, side_effect=original) as for_year:
            build_pennant_season_service(self.world_a).advance(AdvanceTarget.WEEK)

        self.assertEqual(for_year.call_count, 1)
        self.assertEqual(for_year.call_args.args[2], YEAR)


class PoolTest(SimpleTestCase):
    """`pool_of` が「その年の枠で数えるか」を決める唯一の場所（AI の1軍・試合の枠・GM の検査はこの値だけを見る）。"""

    def test_an_exempt_foreigner_does_not_count_and_the_flag_stays_for_others(self):
        players = (
            SimulationPlayer(1, "対象外の助っ人", Position.OUTFIELDER, True, None),
            SimulationPlayer(2, "助っ人", Position.PITCHER, True, None),
            SimulationPlayer(3, "日本人", Position.INFIELDER, False, None),
        )
        team = SimulationTeam(10, "球団", ForeignPlayerRules.NPB, players, None)
        ratings = {1: BatterRatings(), 2: PitcherRatings(), 3: BatterRatings()}

        pool = pool_of(team, ratings, exemptions=QuotaExemptions(YEAR, {1: YEAR, 3: YEAR}))

        counted = {p.player_id: p.counts_as_foreign for p in (*pool.batters, *pool.pitchers)}
        self.assertEqual(counted, {1: False, 2: True, 3: False})
        no_exemption = pool_of(team, ratings, exemptions=QuotaExemptions(YEAR))
        self.assertEqual(
            {p.player_id for p in (*no_exemption.batters, *no_exemption.pitchers) if p.counts_as_foreign}, {1, 2}
        )


class ClubScreenTest(ClubScreenCase):
    """編成画面: FA 宣言の翌年（開幕年）は枠の対象外。表に印を付け、外国人の人数から外す。"""

    def setUp(self):
        super().setUp()
        active = [row.player_id for row in self.view().players if row.is_active]
        self.exempt, self.counted = active[0], active[1]
        make_foreign(self.exempt)
        make_foreign(self.counted)
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.exempt, year=YEAR - 1, kind="国内")

    def test_the_exempt_foreigner_is_marked_and_not_counted(self):
        view = self.view()

        rows = {row.player_id: row for row in view.players}
        self.assertEqual((rows[self.exempt].is_foreign, rows[self.exempt].quota_exempt), (True, True))
        self.assertEqual((rows[self.counted].is_foreign, rows[self.counted].quota_exempt), (True, False))
        self.assertEqual((view.counts.foreign, view.counts.foreign_exempt), (1, 1))

    def test_the_screen_shows_the_mark_and_the_number_left_out(self):
        content = self.client.get(self.club_url("active")).content.decode()

        self.assertIn("FA取得で枠外", content)
        self.assertIn("FA取得で枠外の1人を除く", content)
        self.assertIn("1/4", content)

    def test_a_declaration_in_the_current_year_does_not_exempt(self):
        orm_models.PlayerFreeAgentDeclaration.objects.filter(player_id=self.exempt).update(year=YEAR)

        view = self.view()

        self.assertEqual((view.counts.foreign, view.counts.foreign_exempt), (2, 0))
        self.assertNotIn("FA取得で枠外", self.client.get(self.club_url("active")).content.decode())


class ReleaseTest(ScreenCase):
    """GM の戦力外（Y 年のオフ）: 残る選手が組むのは Y+1 年の名簿なので、Y+1 年の扱い（Y 年までの記録）で数える。"""

    def check_members(self):
        exempt, counted = self.managed_players(2)
        make_foreign(exempt)
        make_foreign(counted)
        with mock.patch(ENSURE_RELEASABLE) as ensure:
            build_pennant_offseason_service(self.world_a).set_releases([], expected_year=YEAR)
        members = {member.player_id: member.counts_as_foreign for member in ensure.call_args.args[1]}
        return exempt, counted, members

    def test_a_declaration_in_the_closing_year_exempts_the_next_roster(self):
        exempt = self.managed_players(1)[0]
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=exempt, year=YEAR, kind="国内")

        exempt, counted, members = self.check_members()

        self.assertEqual((members[exempt], members[counted]), (False, True))

    def test_a_declaration_after_the_closing_year_is_not_read(self):
        exempt = self.managed_players(1)[0]
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=exempt, year=YEAR + 1, kind="国内")

        exempt, counted, members = self.check_members()

        self.assertEqual((members[exempt], members[counted]), (True, True))
