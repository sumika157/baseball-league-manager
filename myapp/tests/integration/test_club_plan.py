"""球団の編成（`ClubPlan`）の永続化・世界の範囲・「進める」への反映の検査。

世界は `test_pennant_advance.py` と同じ（球団2つ・1リーグ。同じシードの世界を2つ作る）。
核は次の3つ。

- 手動のオーダー・ローテーションで進めると、試合の打順・先発がそのとおりになる
- 登録を外した選手がオーダーにいると、その日は自動で進み、理由が `AdvanceReport` に返る（例外にしない）
- 1日ずつ進めても1週間まとめて進めても、上書きがあっても同じ結果になる
"""

from django.db import transaction

from myapp.domain.exceptions import (
    ForeignPlayerQuotaExceeded,
    InvalidClubPlan,
    InvalidWorld,
    PlayerNotFound,
    TeamNotFound,
)
from myapp.domain.pennant.club_plan import ClubPlan, LineupChoice, PlanSection
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoClubPlanRepository
from myapp.presentation.views import build_club_service

from .test_pennant_advance import SeasonCase


def team_of(world_id: int, name: str) -> orm_models.Team:
    return orm_models.Team.objects.get(league__world_id=world_id, name=name)


def players_of(team: orm_models.Team) -> list[orm_models.Player]:
    stints = orm_models.PlayerStint.objects.filter(team=team).select_related("player").order_by("number")
    return [stint.player for stint in stints]


def starting_lineup(world_id: int, team: orm_models.Team, game_index: int = 0) -> list[tuple[int | None, str, str]]:
    """その球団の `game_index` 試合目（日付順）のスタメン。(打順, 選手名, 守備位置)。"""
    games = orm_models.Game.objects.filter(home_team__league__world_id=world_id).order_by("played_on", "id")
    game = games[game_index]
    lines = orm_models.GameBattingLine.objects.filter(game=game, team=team, slot_sequence=0).order_by("batting_order")
    return [(line.batting_order, line.player.name, line.fielding_position) for line in lines]


def starting_pitcher(world_id: int, team: orm_models.Team, game_index: int = 0) -> str:
    games = orm_models.Game.objects.filter(home_team__league__world_id=world_id).order_by("played_on", "id")
    game = games[game_index]
    line = orm_models.GamePitchingLine.objects.get(game=game, player__stints__team=team, appearance_order=1)
    return line.player.name


class ClubPlanCase(SeasonCase):
    def setUp(self):
        self.home_a = team_of(self.world_a, self.home.name)
        self.home_b = team_of(self.world_b, self.home.name)
        self.service_a = build_club_service(self.world_a)
        self.service_b = build_club_service(self.world_b)

    @staticmethod
    def lineup_of(view) -> list[LineupChoice]:
        return [LineupChoice(row.player_id, row.position) for row in view.lineup]

    @staticmethod
    def _name(player_id: int) -> str:
        return orm_models.Player.objects.get(id=player_id).name

    @staticmethod
    def _is_pitcher(player_id: int) -> bool:
        return orm_models.Player.objects.get(id=player_id).position == "投手"


class RepositoryTest(ClubPlanCase):
    def test_a_plan_round_trips_through_the_repository(self):
        service = self.service_a
        proposal = service.propose(self.home_a.id)
        lineup = list(reversed(self.lineup_of(proposal)))
        service.set_lineup(self.home_a.id, lineup)
        service.set_rotation(self.home_a.id, list(proposal.rotation_ids[:5]))
        pitcher_ids = [pid for pid in proposal.active_ids if pid not in proposal.rotation_ids]
        service.set_closer(self.home_a.id, pitcher_ids[-1])
        service.set_active_roster(self.home_a.id, list(proposal.active_ids))

        plan = DjangoClubPlanRepository(WorldScope.pennant(self.world_a)).find_by_team(self.home_a.id)

        self.assertEqual(plan.lineup, tuple(lineup))
        self.assertEqual(plan.rotation, tuple(proposal.rotation_ids[:5]))
        self.assertEqual(plan.closer_id, pitcher_ids[-1])
        self.assertEqual(set(plan.active_ids or ()), set(proposal.active_ids))

    def test_a_team_without_a_plan_is_all_automatic(self):
        repository = DjangoClubPlanRepository(WorldScope.pennant(self.world_a))

        plan = repository.find_by_team(self.home_a.id)

        self.assertTrue(plan.is_empty)
        self.assertEqual(repository.find_all(), [])

    def test_setting_every_section_back_to_automatic_removes_the_rows(self):
        service = self.service_a
        proposal = service.propose(self.home_a.id)
        service.set_lineup(self.home_a.id, self.lineup_of(proposal))
        service.set_rotation(self.home_a.id, list(proposal.rotation_ids[:5]))
        self.assertEqual(orm_models.PennantClubPlan.objects.count(), 1)

        for section in PlanSection:
            service.reset(self.home_a.id, section)

        self.assertEqual(orm_models.PennantClubPlan.objects.count(), 0)
        self.assertEqual(orm_models.PennantClubPlanEntry.objects.count(), 0)

    def test_saving_again_replaces_the_sections(self):
        service = self.service_a
        proposal = service.propose(self.home_a.id)
        service.set_rotation(self.home_a.id, list(proposal.rotation_ids[:6]))
        service.set_rotation(self.home_a.id, list(proposal.rotation_ids[:5]))

        plan = DjangoClubPlanRepository(WorldScope.pennant(self.world_a)).find_by_team(self.home_a.id)

        self.assertEqual(plan.rotation, tuple(proposal.rotation_ids[:5]))
        self.assertEqual(orm_models.PennantClubPlanEntry.objects.filter(section="rotation").count(), 5)


class ScopeTest(ClubPlanCase):
    def test_another_world_cannot_see_the_plan(self):
        proposal = self.service_a.propose(self.home_a.id)
        self.service_a.set_lineup(self.home_a.id, self.lineup_of(proposal))

        repository_b = DjangoClubPlanRepository(WorldScope.pennant(self.world_b))

        self.assertEqual(repository_b.find_all(), [])
        with self.assertRaises(TeamNotFound):
            repository_b.find_by_team(self.home_a.id)
        with self.assertRaises(TeamNotFound):
            self.service_b.view(self.home_a.id)

    def test_another_world_cannot_write_the_plan(self):
        repository_b = DjangoClubPlanRepository(WorldScope.pennant(self.world_b))
        with self.assertRaises(TeamNotFound):
            repository_b.save(ClubPlan(team_id=self.home_a.id, closer_id=players_of(self.home_b)[-1].id))
        with self.assertRaises(TeamNotFound):
            self.service_b.set_closer(self.home_a.id, players_of(self.home_a)[-1].id)
        self.assertEqual(orm_models.PennantClubPlan.objects.count(), 0)

    def test_a_player_of_another_world_cannot_be_put_in_a_plan(self):
        foreign_player = players_of(self.home_b)[-1].id
        repository_a = DjangoClubPlanRepository(WorldScope.pennant(self.world_a))
        with self.assertRaises(PlayerNotFound):
            repository_a.save(ClubPlan(team_id=self.home_a.id, closer_id=foreign_player))
        self.assertEqual(orm_models.PennantClubPlan.objects.count(), 0)

    def test_the_real_data_has_no_plan(self):
        repository = DjangoClubPlanRepository(WorldScope.real())
        with self.assertRaises(InvalidWorld):
            repository.save(ClubPlan(team_id=self.home.id, closer_id=1))
        with self.assertRaises(TeamNotFound):
            repository.find_by_team(self.home_a.id)

    def test_a_plan_is_removed_with_its_world(self):
        from myapp.infrastructure.repositories import DjangoWorldRepository

        proposal = self.service_a.propose(self.home_a.id)
        self.service_a.set_lineup(self.home_a.id, self.lineup_of(proposal))
        self.service_a.set_closer(self.home_a.id, proposal.closer_id)
        self.service_b.set_rotation(self.home_b.id, list(self.service_b.propose(self.home_b.id).rotation_ids[:5]))

        DjangoWorldRepository().delete(self.world_a)

        self.assertEqual(orm_models.PennantClubPlan.objects.filter(team__league__world_id=self.world_a).count(), 0)
        self.assertEqual(orm_models.PennantClubPlan.objects.count(), 1, "他の世界の編成は残る")
        self.assertEqual(orm_models.PennantClubPlanEntry.objects.filter(section="lineup").count(), 0)


class ServiceTest(ClubPlanCase):
    def test_the_proposal_shows_the_automatic_plan_and_marks_nothing_manual(self):
        proposal = self.service_a.propose(self.home_a.id)

        self.assertEqual(len(proposal.lineup), 9)
        self.assertEqual({row.position.value for row in proposal.lineup}, set("捕一二三遊左中右指"))
        self.assertGreaterEqual(len(proposal.rotation_ids), 1)
        self.assertIsNotNone(proposal.closer_id)
        self.assertEqual(
            (
                proposal.active_is_manual,
                proposal.lineup_is_manual,
                proposal.rotation_is_manual,
                proposal.closer_is_manual,
            ),
            (False, False, False, False),
        )
        self.assertEqual(proposal.year, 2026)
        self.assertEqual(proposal.notices, ())

    def test_the_proposal_does_not_save_anything(self):
        self.service_a.propose(self.home_a.id)
        self.assertEqual(orm_models.PennantClubPlan.objects.count(), 0)

    def test_the_view_marks_manual_sections(self):
        proposal = self.service_a.propose(self.home_a.id)

        view = self.service_a.set_lineup(self.home_a.id, list(reversed(self.lineup_of(proposal))))

        self.assertTrue(view.lineup_is_manual)
        self.assertFalse(view.rotation_is_manual)
        self.assertEqual([row.player_id for row in view.lineup], [row.player_id for row in reversed(proposal.lineup)])
        self.assertEqual(self.service_a.view(self.home_a.id), view)

    def test_a_refused_override_is_not_saved(self):
        proposal = self.service_a.propose(self.home_a.id)
        with self.assertRaises(InvalidClubPlan):
            self.service_a.set_lineup(self.home_a.id, self.lineup_of(proposal)[:8])
        with self.assertRaises(InvalidClubPlan):
            self.service_a.set_active_roster(self.home_a.id, list(range(1, 40)))
        self.assertEqual(orm_models.PennantClubPlan.objects.count(), 0)

    def test_the_foreign_roster_limit_of_the_league_is_enforced(self):
        league = self.home_a.league
        league.foreign_player_roster_limit = 1
        league.save()
        foreign = [p for p in players_of(self.home_a) if p.position != "投手"][:2]
        for player in foreign:
            player.is_foreign_player = True
            player.save()
        proposal = self.service_a.propose(self.home_a.id)

        self.assertLessEqual(
            sum(row.is_foreign and row.is_active for row in proposal.players), 1, "自動編成は枠に収める"
        )
        everyone = sorted({row.player_id for row in proposal.players})
        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.service_a.set_active_roster(self.home_a.id, everyone)


class AdvanceWithPlanTest(ClubPlanCase):
    def week(self, world_id=None):
        return build_season(world_id or self.world_a).advance(AdvanceTarget.WEEK)

    def starters_of(self, team, count):
        return [starting_pitcher(self.world_a, team, i) for i in range(count)]

    def test_a_manual_lineup_bats_as_ordered_on_the_next_days(self):
        proposal = self.service_a.propose(self.home_a.id)
        manual = list(reversed(self.lineup_of(proposal)))
        self.service_a.set_lineup(self.home_a.id, manual)
        expected = [(order, self._name(c.player_id), c.position.value) for order, c in enumerate(manual, start=1)]

        report = self.week()

        self.assertEqual(report.plan_notices, (), "上書きは効いている")
        self.assertGreater(len(report.played_dates), 3, "1週間で数日ぶんの試合ができている")
        for game_index in range(report.games):
            with self.subTest(game=game_index):
                self.assertEqual(starting_lineup(self.world_a, self.home_a, game_index), expected)

    def test_without_a_plan_the_proposed_lineup_is_used(self):
        proposal = self.service_a.propose(self.home_a.id)
        automatic = [(row.batting_order, row.name, row.position.value) for row in proposal.lineup]

        build_season(self.world_a).advance(AdvanceTarget.DAY)

        self.assertEqual(starting_lineup(self.world_a, self.home_a), automatic, "提案は、そのまま使われる編成")

    def test_a_manual_rotation_decides_the_starter(self):
        proposal = self.service_a.propose(self.home_a.id)
        pair = list(reversed(proposal.rotation_ids))[:5]  # 自動なら先発しない順の投手
        self.service_a.set_rotation(self.home_a.id, pair)

        report = self.week()

        starters = self.starters_of(self.home_a, report.games)
        names = [self._name(pid) for pid in pair]
        self.assertEqual(set(starters), set(names), "手動のローテーションの投手だけが、全員先発する")
        self.assertEqual(starters[0], names[0])

    def test_a_manual_closer_is_used(self):
        proposal = self.service_a.propose(self.home_a.id)
        other = next(
            pid
            for pid in proposal.active_ids
            if pid not in proposal.rotation_ids and pid != proposal.closer_id and self._is_pitcher(pid)
        )
        self.service_a.set_closer(self.home_a.id, other)

        view = self.service_a.view(self.home_a.id)

        self.assertEqual(view.closer_id, other)
        self.assertTrue(view.closer_is_manual)
        build_season(self.world_a).advance(AdvanceTarget.MONTH_END)
        saves = orm_models.GamePitchingLine.objects.filter(
            game__home_team__league__world_id=self.world_a, player__stints__team=self.home_a, saves__gt=0
        )
        self.assertTrue(saves.exists())
        self.assertGreater(saves.filter(player_id=other).count(), saves.exclude(player_id=other).count())

    def test_a_lineup_with_a_player_dropped_from_the_roster_falls_back_for_that_advance(self):
        proposal = self.service_a.propose(self.home_a.id)
        self.service_a.set_lineup(self.home_a.id, self.lineup_of(proposal))
        dropped = proposal.lineup[3]
        # 1軍登録を手動にして、オーダーにいる選手を外す（オーダーの検査は決めたときだけなので、保存はできる）
        kept = [pid for pid in proposal.active_ids if pid != dropped.player_id]
        self.service_a.set_active_roster(self.home_a.id, kept)

        view = self.service_a.view(self.home_a.id)
        self.assertEqual([n.section for n in view.notices], [PlanSection.LINEUP])
        self.assertTrue(view.lineup_is_manual, "使えなくなっても区画は手動のまま（その日だけ自動に落ちる）")
        self.assertNotIn(dropped.player_id, [row.player_id for row in view.lineup])

        report = self.week()

        self.assertGreater(report.games, 3, "例外で止まらず、その日は自動で進む")
        (notice,) = report.plan_notices
        self.assertEqual((notice.team_id, notice.team_name), (self.home_a.id, self.home.name))
        self.assertEqual(notice.section, PlanSection.LINEUP)
        self.assertIn("1軍に登録されていない", notice.reason)
        self.assertEqual(notice.on, report.played_dates[0])
        names = {name for _, name, _ in starting_lineup(self.world_a, self.home_a)}
        self.assertNotIn(dropped.name, names)
        self.assertEqual(len(names), 9)

    def test_the_sections_that_are_still_valid_keep_working_when_one_falls_back(self):
        proposal = self.service_a.propose(self.home_a.id)
        self.service_a.set_lineup(self.home_a.id, self.lineup_of(proposal))
        pair = list(reversed(proposal.rotation_ids))[:5]
        self.service_a.set_rotation(self.home_a.id, pair)
        kept = [pid for pid in proposal.active_ids if pid != proposal.lineup[3].player_id]
        self.service_a.set_active_roster(self.home_a.id, kept)

        report = self.week()

        self.assertEqual([n.section for n in report.plan_notices], [PlanSection.LINEUP])
        self.assertEqual(set(self.starters_of(self.home_a, report.games)), {self._name(pid) for pid in pair})


class ReplayWithPlanTest(ClubPlanCase):
    """球団の id は世界ごとに違い、試合の乱数に入る。**同じ世界**を巻き戻して突き合わせる。"""

    def replay(self, world_id, run):
        savepoint = transaction.savepoint()
        try:
            run()
            return self.snapshot(world_id)
        finally:
            transaction.savepoint_rollback(savepoint)

    def manual_plan(self):
        proposal = self.service_a.propose(self.home_a.id)
        self.service_a.set_lineup(self.home_a.id, list(reversed(self.lineup_of(proposal))))
        self.service_a.set_rotation(self.home_a.id, list(reversed(proposal.rotation_ids))[:5])

    def test_a_day_at_a_time_and_a_week_at_once_give_the_same_games_with_a_plan(self):
        """上書きがあっても、まとめて進めるか1日ずつ進めるかで結果は変わらない（編成は世界の状態で決まる）。"""
        self.manual_plan()
        week = self.replay(self.world_a, lambda: self.advance(self.world_a, AdvanceTarget.WEEK))
        played = len({row[0] for row in week[0]})
        self.assertGreater(played, 3)

        one_by_one = self.replay(
            self.world_a, lambda: [self.advance(self.world_a, AdvanceTarget.DAY) for _ in range(played)]
        )

        self.assertEqual(week, one_by_one)

    def test_a_plan_changes_the_games_from_the_automatic_ones(self):
        """同じ世界でも、オーダーを上書きすると打席の列が変わる（上書きが効いている確認）。"""
        automatic = self.replay(self.world_a, lambda: self.advance(self.world_a, AdvanceTarget.WEEK))
        self.manual_plan()

        manual = self.replay(self.world_a, lambda: self.advance(self.world_a, AdvanceTarget.WEEK))

        self.assertNotEqual(automatic, manual)

    def test_another_worlds_plan_does_not_change_this_world(self):
        """世界 A に上書きを入れても、世界 B の試合は上書きの無いときと同じ。"""
        before = self.replay(self.world_b, lambda: self.advance(self.world_b, AdvanceTarget.WEEK))
        self.manual_plan()

        report = self.advance(self.world_b, AdvanceTarget.WEEK)

        self.assertEqual(report.plan_notices, ())
        self.assertEqual(self.snapshot(self.world_b), before)


def build_season(world_id):
    from myapp.presentation.views import build_pennant_season_service

    return build_pennant_season_service(world_id)


class StrictestGameLimitTest(ClubPlanCase):
    """出場枠の違うリーグがある世界では、編成を最も厳しい枠で検査する（再発防止）。

    交流戦はホーム球団のリーグの枠で行うので、自リーグの枠で検査すると、枠の厳しいリーグとの交流戦で
    スタメンを組めず「進める」が止まる。決めるとき（`ClubManagementService`）と進めるとき
    （`PennantSeasonService`）の両方が、世界のリーグで最も厳しい枠を使っていることを確かめる。
    """

    def setUp(self):
        super().setUp()
        # 日程を作ってから、出場枠の厳しいリーグを世界に足す（日程には入らず、枠の値だけが効く）
        build_season(self.world_a).advance(AdvanceTarget.WEEK)
        strict = orm_models.League.objects.create(
            name="枠の厳しいリーグ", world_id=self.world_a, foreign_player_game_limit=1
        )
        orm_models.Team.objects.create(league=strict, name="枠の厳しい球団")
        self.lineup = self.lineup_of(self.service_a.propose(self.home_a.id))
        # 自リーグの枠（3）には収まり、厳しい枠（1）には収まらない
        foreign = [choice.player_id for choice in self.lineup[:2]]
        orm_models.Player.objects.filter(id__in=foreign).update(is_foreign_player=True)

    def test_deciding_a_lineup_uses_the_strictest_limit_in_the_world(self):
        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.service_a.set_lineup(self.home_a.id, self.lineup)

    def test_advancing_uses_the_strictest_limit_in_the_world(self):
        # 検査を通さずに保存した（枠の厳しいリーグが後から増えた）オーダーは、進めるときに自動に落ちる
        repository = DjangoClubPlanRepository(WorldScope.pennant(self.world_a))
        repository.save(ClubPlan(team_id=self.home_a.id, lineup=tuple(self.lineup)))

        report = build_season(self.world_a).advance(AdvanceTarget.WEEK)

        sections = [(n.team_id, n.section) for n in report.plan_notices]
        self.assertIn((self.home_a.id, PlanSection.LINEUP), sections)
