"""ロスターの変更。移籍・外国人枠・主将の指名。"""

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.application.dto import LineupSlot
from myapp.application.foreign_quota import ForeignQuotaExemptions
from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
    ForeignPlayerQuotaExceeded,
)
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import (
    BattingLine,
    FieldingPosition,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoTeamRepository,
)

from ..helpers import (
    build_recording_service,
    build_scorebook,
    give_batting,
    login_as_manager,
    play_game,
    register_lineup,
    to_plate_appearances,
)
from .base import BaseCase


class TransferTest(BaseCase):
    """移籍と経歴。"""

    def setUp(self):
        super().setUp()
        self.player = self.roster.register_player(self.team.id, "山田", 10, "内野手")

    def test_transfer_closes_the_old_stint_and_opens_a_new_one(self):
        self.roster.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        stints = list(orm_models.PlayerStint.objects.filter(player_id=self.player.id).order_by("from_year"))
        self.assertEqual(len(stints), 2)
        self.assertEqual(stints[0].team_id, self.team.id)
        self.assertEqual(stints[0].to_year, 2026)
        self.assertEqual(stints[1].team_id, self.rival.id)
        self.assertEqual(stints[1].number, "7")
        self.assertIsNone(stints[1].to_year)

    def test_player_appears_on_the_new_roster_only(self):
        self.roster.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        self.assertEqual(self.team_view.list_batters(self.team.id).rows, [])
        self.assertEqual([r.name for r in self.team_view.list_batters(self.rival.id).rows], ["山田"])

    def test_stats_follow_the_player_not_the_team(self):
        """成績は選手に紐づくので、移籍しても失われない。"""
        give_batting(self.team, self.rival, self.player.id, BattingLine(at_bats=10, singles=3))

        self.roster.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        detail = self.team_view.get_player_detail(self.rival.id, self.player.id)
        self.assertEqual(detail.at_bats, 10)

    def test_the_old_number_becomes_available(self):
        self.roster.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        # 空いた10番を別の選手が使える
        self.roster.register_player(self.team.id, "田中", 10, "外野手")
        self.assertEqual([r.name for r in self.team_view.list_batters(self.team.id).rows], ["田中"])

    def test_number_in_use_at_the_destination_is_rejected(self):
        self.roster.register_player(self.rival.id, "先客", 7, "外野手")

        with self.assertRaises(DuplicateJerseyNumber):
            self.roster.transfer_player(
                self.player.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=7,
                year=2026,
            )

    def test_career_is_visible_on_the_team_aggregate(self):
        self.roster.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        team = DjangoTeamRepository(WorldScope.real()).find_by_id(self.rival.id)
        career = team.find_player(self.player.id).career

        self.assertEqual([s.team_name for s in career], ["相手チーム", "テストチーム"])

    def test_admin_shows_the_current_team(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        response = self.client.get("/admin/myapp/player/")

        self.assertContains(response, "テストチーム")
        self.assertContains(response, "field-current_number")

    def test_failed_transfer_leaves_the_source_team_untouched(self):
        """検査に失敗したら、元チームの退団も取り消されること（部分的な保存を残さない）。"""
        self.roster.register_player(self.rival.id, "先客", 7, "外野手")

        with self.assertRaises(DuplicateJerseyNumber):
            self.roster.transfer_player(
                self.player.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=7,
                year=2026,
            )

        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertIsNone(stint.to_year)
        self.assertEqual(stint.team_id, self.team.id)


class ForeignPlayerQuotaTest(BaseCase):
    """外国人枠（協約第82条の2。1軍登録は計4人・うち投手3人・野手3人）。保有は縛らず、実データは試合の出場者で検査する。"""

    def setUp(self):
        super().setUp()
        self.foreign = self.roster.register_player(self.team.id, "助っ人", 50, "外野手")
        orm_models.Player.objects.filter(id=self.foreign.id).update(is_foreign_player=True)

    def set_rules(self, active, pitchers, batters):
        orm_models.League.objects.filter(id=self.league.id).update(
            foreign_active_limit=active, foreign_active_pitcher_limit=pitchers, foreign_active_batter_limit=batters
        )

    def foreigners(self, team, positions, *, first_number):
        """外国人選手を登録する（`positions` は登録位置の並び）。"""
        ids = []
        for offset, position in enumerate(positions):
            number = first_number + offset
            player = self.roster.register_player(team.id, f"外国人{number}", number, position)
            orm_models.Player.objects.filter(id=player.id).update(is_foreign_player=True)
            ids.append(player.id)
        return ids

    def test_transfer_accepts_a_foreigner_however_many_the_destination_holds(self):
        """協約第82条の2: 外国人は任意の数を保有できる（移籍の受け入れでは数えない）。"""
        self.foreigners(self.rival, ["投手"] * 5, first_number=80)

        self.roster.transfer_player(
            self.foreign.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=99,
            year=2026,
        )

        stint = orm_models.PlayerStint.objects.get(player_id=self.foreign.id, to_year__isnull=True)
        self.assertEqual(stint.team_id, self.rival.id)

    def _record(self, game, *, home_batters=(), away_batters=(), home_pitchers=None):
        """1回表から、外国人を打順の先頭に置いたスコアブックを記録する。

        `home_pitchers` は {回: 投手id}（ホームの投手は表を投げる）。表の回数は投手の数に合わせる。
        """
        away = register_lineup(self.service, self.rival, prefix="ビジター", first_number=61)
        home = register_lineup(self.service, self.team, prefix="ホーム", first_number=71)
        away[: len(away_batters)] = away_batters
        home[: len(home_batters)] = home_batters
        if home_pitchers is None:
            home_pitchers = {1: self.roster.register_player(self.team.id, "投手", 91, "投手").id}
        away_pitcher = self.roster.register_player(self.rival.id, "ビジター投手", 92, "投手")

        return build_recording_service().record_scorebook(
            game.id,
            year=2026,
            played_on=game.played_on,
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            lineup=[
                LineupSlot(
                    team_id=team.id,
                    player_id=player_id,
                    batting_order=order,
                    slot_sequence=0,
                    fielding_position=FieldingPosition.DESIGNATED_HITTER,
                )
                for team, ids in ((self.rival, away), (self.team, home))
                for order, player_id in enumerate(ids, start=1)
            ],
            plate_appearances=to_plate_appearances(
                build_scorebook(
                    away=[0] * max(home_pitchers),
                    home=[],
                    away_batters=away,
                    home_batters=home,
                    away_pitchers={1: away_pitcher.id},
                    home_pitchers=home_pitchers,
                )
            ),
            times=None,
            attendance=None,
        )

    def test_four_foreigners_split_two_and_two_pass_the_agreement(self):
        game = play_game(self.team, self.rival, recorded=False)
        batters = [self.foreign.id, *self.foreigners(self.team, ["内野手"], first_number=80)]
        pitchers = self.foreigners(self.team, ["投手", "投手"], first_number=82)

        self._record(game, home_batters=batters, home_pitchers={1: pitchers[0], 2: pitchers[1]})  # 例外にならない

        self.assertTrue(orm_models.GameBattingLine.objects.filter(player_id=self.foreign.id).exists())

    def test_a_fifth_foreigner_is_refused(self):
        game = play_game(self.team, self.rival, recorded=False)
        batters = [self.foreign.id, *self.foreigners(self.team, ["内野手", "内野手"], first_number=80)]
        pitchers = self.foreigners(self.team, ["投手", "投手"], first_number=83)

        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, "計4人までです（5人）"):
            self._record(game, home_batters=batters, home_pitchers={1: pitchers[0], 2: pitchers[1]})
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)

    def test_a_fourth_foreign_pitcher_is_refused(self):
        game = play_game(self.team, self.rival, recorded=False)
        pitchers = self.foreigners(self.team, ["投手"] * 4, first_number=80)

        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, "投手3人までです（4人）"):
            self._record(game, home_pitchers=dict(enumerate(pitchers, start=1)))

    def test_a_fourth_foreign_batter_is_refused(self):
        game = play_game(self.team, self.rival, recorded=False)
        batters = [self.foreign.id, *self.foreigners(self.team, ["内野手"] * 3, first_number=80)]

        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, "野手3人までです（4人）"):
            self._record(game, home_batters=batters)

    def test_a_position_player_who_pitches_counts_as_a_batter(self):
        """投手か野手かは登録位置で数える（その試合で何をしたかでは数えない）。"""
        game = play_game(self.team, self.rival, recorded=False)
        batters = [self.foreign.id, *self.foreigners(self.team, ["内野手"] * 2, first_number=80)]
        [outfielder] = self.foreigners(self.team, ["外野手"], first_number=83)
        pitcher = self.roster.register_player(self.team.id, "投手", 91, "投手")

        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, "野手3人までです（4人）"):
            self._record(game, home_batters=batters, home_pitchers={1: pitcher.id, 2: outfielder})

    def test_recording_rejects_when_home_team_quota_exceeded(self):
        self.set_rules(0, None, None)
        game = play_game(self.team, self.rival, recorded=False)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self._record(game, home_batters=[self.foreign.id])
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)

    def test_recording_rejects_when_away_team_quota_exceeded(self):
        """ホーム・ビジターは独立に判定する。ホームに助っ人がいなくても、
        ビジター側の上限超過は検出される。"""
        [rival_foreign] = self.foreigners(self.rival, ["外野手"], first_number=51)
        self.set_rules(0, None, None)
        game = play_game(self.team, self.rival, recorded=False)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self._record(game, away_batters=[rival_foreign])

    def test_recording_allows_exactly_at_the_limit(self):
        self.set_rules(1, None, None)
        game = play_game(self.team, self.rival, recorded=False)

        self._record(game, home_batters=[self.foreign.id])  # 例外にならない

        self.assertTrue(orm_models.GameBattingLine.objects.filter(player_id=self.foreign.id).exists())

    def test_recording_with_no_limit_set_never_rejects(self):
        """空欄（無制限）はリーグの既定値（協約の 4／3／3）とは別に、明示的に検証しておく。"""
        self.set_rules(None, None, None)
        game = play_game(self.team, self.rival, recorded=False)
        batters = [self.foreign.id, *self.foreigners(self.team, ["内野手"] * 4, first_number=80)]

        self._record(game, home_batters=batters)  # 例外にならない

        self.assertTrue(orm_models.GameBattingLine.objects.filter(player_id=self.foreign.id).exists())

    # --- 協約第82条 (4): FA 資格を取得した外国人は、公示の翌年のシーズンから外国人枠の対象外 ---

    def serve_seasons(self, player_id, years):
        """毎年 4/1 と 9/30 に出場する（登録日数は初〜最終出場で推定するので、毎年1シーズン）。在籍は最初の年から。"""
        orm_models.PlayerStint.objects.filter(player_id=player_id).update(from_year=min(years))
        for year in years:
            for month, day in ((4, 1), (9, 30)):
                play_game(self.team, self.rival, year=year, month=month, day=day, batting={player_id: BattingLine()})

    def five_foreigners(self, *, pitchers=2):
        """2026年の未記録の試合と、外国人の打者3人（`self.foreign` を含む）・投手 `pitchers` 人。"""
        game = play_game(self.team, self.rival, recorded=False)
        batters = [self.foreign.id, *self.foreigners(self.team, ["内野手", "内野手"], first_number=80)]
        ids = self.foreigners(self.team, ["投手"] * pitchers, first_number=83)
        return game, batters, dict(enumerate(ids, start=1))

    def test_a_foreigner_who_acquired_domestic_fa_last_year_is_not_counted(self):
        """2018〜2025年の8シーズンで国内FAを取得（2025年）。2026年の試合では数えないので、5人目でも通る。"""
        self.serve_seasons(self.foreign.id, range(2018, 2026))
        game, batters, pitchers = self.five_foreigners()

        self._record(game, home_batters=batters, home_pitchers=pitchers)  # 例外にならない

        self.assertTrue(orm_models.GameBattingLine.objects.filter(game_id=game.id, player_id=self.foreign.id).exists())

    def test_a_foreigner_still_counts_in_the_year_of_the_acquisition(self):
        """2019〜2026年の8シーズン目は2026年。取得した年はまだ数える（対象外は公示の翌年から）。"""
        self.serve_seasons(self.foreign.id, range(2019, 2027))
        game, batters, pitchers = self.five_foreigners()

        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, r"の出場選手の外国人選手は計4人までです（5人）"):
            self._record(game, home_batters=batters, home_pitchers=pitchers)

    def test_a_foreigner_who_declared_fa_last_year_is_not_counted(self):
        """FA 宣言には資格が要るので、宣言の翌年からも対象外（記録が浅くて取得を数えられなくても）。"""
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.foreign.id, year=2025, kind="国内")
        game, batters, pitchers = self.five_foreigners()

        self._record(game, home_batters=batters, home_pitchers=pitchers)  # 例外にならない

    def test_a_declaration_in_the_same_year_does_not_exempt(self):
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.foreign.id, year=2026, kind="国内")
        game, batters, pitchers = self.five_foreigners()

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self._record(game, home_batters=batters, home_pitchers=pitchers)

    def test_the_message_tells_how_many_were_left_out(self):
        orm_models.PlayerFreeAgentDeclaration.objects.create(player_id=self.foreign.id, year=2025, kind="国内")
        game, batters, pitchers = self.five_foreigners(pitchers=3)

        with self.assertRaisesRegex(
            ForeignPlayerQuotaExceeded, r"（FA 取得で枠外の1人を除く）の外国人選手は計4人までです（5人）"
        ):
            self._record(game, home_batters=batters, home_pitchers=pitchers)

    def test_a_save_within_the_quota_does_not_read_the_fa_records(self):
        """枠に収まる保存では、対象外を導かない（ふだんの保存のクエリを増やさない）。"""
        game = play_game(self.team, self.rival, recorded=False)

        with mock.patch.object(ForeignQuotaExemptions, "for_year") as for_year:
            self._record(game, home_batters=[self.foreign.id])

        for_year.assert_not_called()


class CaptaincyApplicationTest(BaseCase):
    """主将の指名・解任。"""

    def setUp(self):
        super().setUp()
        self.player = self.roster.register_player(self.team.id, "山田", 10, "内野手")

    def test_appoint_and_remove_round_trip_through_the_database(self):
        self.roster.appoint_captain(self.team.id, self.player.id, 2026)

        team = DjangoTeamRepository(WorldScope.real()).find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, self.player.id)

        self.roster.remove_captain(self.team.id, self.player.id, 2027)

        team = DjangoTeamRepository(WorldScope.real()).find_by_id(self.team.id)
        self.assertIsNone(team.current_captain)

    def test_player_edit_appoints_a_captain(self):
        login_as_manager(self.client, self.team, username="u")

        response = self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"appoint_captain": "1"},
        )

        self.assertRedirects(response, reverse("player_edit", args=[self.team.id, self.player.id]))
        team = DjangoTeamRepository(WorldScope.real()).find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, self.player.id)

    def test_player_edit_shows_duplicate_captain_error(self):
        other = self.roster.register_player(self.team.id, "田中", 11, "外野手")
        self.roster.appoint_captain(self.team.id, other.id)
        login_as_manager(self.client, self.team, username="u")

        response = self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"appoint_captain": "1"},
            follow=True,
        )

        self.assertContains(response, "には既に主将")
        team = DjangoTeamRepository(WorldScope.real()).find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, other.id)
