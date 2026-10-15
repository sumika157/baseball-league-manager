"""ペナントの進行と投手の疲労（#97）。

疲労は保存せず、世界の直近の登板（`SimulationContextQuery` の最後の先発）から導く。手動の5人ローテーション
（毎日の試合で中4日の先発が出る形）でも、1日ずつ進めても2週間まとめて進めても同じ試合になること、
疲労が進行の経路まで届いていること（段階1）、編成の投手陣タブに次の試合日の疲労の区分が出ること（段階3）を確かめる。
"""

import re
from collections import defaultdict
from datetime import timedelta
from unittest import mock

from django.db import transaction

from myapp.application.dto import LastStart, PitchingOuting, SimulationContext
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.simulation.fatigue import (
    FATIGUE_WINDOW_DAYS,
    HIGH_STAMINA_RELIEVER,
    MIN_DAYS_BETWEEN_STARTS,
    FatigueLevel,
    PitchingHistory,
    fatigued,
    pregame_fatigue_points,
    starter_rest_points,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoSimulationContextQuery
from myapp.presentation.views import build_club_service

from .test_club_screen import ClubScreenCase
from .test_pennant_advance import SeasonCase


class FiveManRotationTest(SeasonCase):
    """自軍だけ手動の5人ローテーションにした世界。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        home = orm_models.Team.objects.get(league__world_id=cls.world_a, name=cls.home.name)
        service = build_club_service(cls.world_a)
        service.set_rotation(home.id, list(service.propose(home.id).rotation_ids[:5]))

    @classmethod
    def replay(cls, run):
        """`run` で世界を進めた結果を取り、世界を元に戻す。"""
        savepoint = transaction.savepoint()
        try:
            run()
            return cls.snapshot(cls.world_a)
        finally:
            transaction.savepoint_rollback(savepoint)

    def two_weeks(self):
        self.advance(self.world_a, AdvanceTarget.WEEK)
        self.advance(self.world_a, AdvanceTarget.WEEK)

    def test_the_five_man_rotation_starts_on_four_days_of_rest(self):
        """毎日の試合で5人を回すと中4日（5日後）の先発が出る（疲労が起きる形になっている）。"""
        self.two_weeks()
        starts = defaultdict(list)
        for line in orm_models.GamePitchingLine.objects.filter(
            game__home_team__league__world_id=self.world_a, appearance_order=1
        ).select_related("game"):
            starts[line.player_id].append(line.game.played_on)
        gaps = set()
        for days in starts.values():
            days.sort()
            gaps.update((later - earlier).days for earlier, later in zip(days, days[1:], strict=False))
        # 相手（AI の6人ローテ）は中5日を割らないので、中4日は自軍の5人ローテから
        self.assertIn(MIN_DAYS_BETWEEN_STARTS - 1, gaps)

    def test_days_one_by_one_equal_two_weeks_at_once(self):
        """中4日の疲労は、進めるの区切りをまたいでも同じに効く（最後の先発は世界の全試合から読む）。"""
        weeks = self.replay(self.two_weeks)
        played = len({row[0] for row in weeks[0]})

        one_by_one = self.replay(lambda: [self.advance(self.world_a, AdvanceTarget.DAY) for _ in range(played)])

        self.assertEqual(weeks, one_by_one)

    def test_the_fatigue_reaches_the_engine_through_the_advance(self):
        """進行の経路でも、中4日の先発は疲労を背負って投げる（能力の目減りまで届いている）。"""
        points: list[int] = []

        def spy(ratings, fatigue):
            points.append(fatigue)
            return fatigued(ratings, fatigue)

        with mock.patch("myapp.domain.simulation.engine.fatigued", side_effect=spy):
            self.two_weeks()

        self.assertIn(starter_rest_points(MIN_DAYS_BETWEEN_STARTS - 1), points)


class FatigueWindowQueryTest(SeasonCase):
    """直近の登板は疲労の窓（`FATIGUE_WINDOW_DAYS`）の日数ぶんだけ読む。窓の端を含み、その前日を含まない。"""

    def test_the_query_reads_exactly_the_window(self):
        self.advance(self.world_a, AdvanceTarget.WEEK)
        self.advance(self.world_a, AdvanceTarget.WEEK)
        query = DjangoSimulationContextQuery(WorldScope.pennant(self.world_a))
        last = query.last_played_on()
        assert last is not None
        before = last + timedelta(days=1)

        days = {outing.played_on for outing in query.load(before=before).recent_outings}

        played = set(
            orm_models.Game.objects.filter(home_team__league__world_id=self.world_a).values_list(
                "played_on", flat=True
            )
        )
        window = {before - timedelta(days=ago) for ago in range(1, FATIGUE_WINDOW_DAYS + 1)}
        self.assertEqual(days, played & window)
        self.assertTrue(played - window, "窓より前にも試合がある（読まないことを確かめられる）")


class FatigueColumnTest(ClubScreenCase):
    """編成の投手陣タブの疲労の区分（#97 段階3）。次の試合日に投げたときの区分を、エンジンと同じ式で出す。

    ローテーションは先発したら（休養の点が付く）、抑えと救援陣は救援で投げたら。直近の登板は読む処理を差し替えて与える。
    """

    LOAD = "myapp.infrastructure.queries.DjangoSimulationContextQuery.load"

    def setUp(self):
        super().setUp()
        self.start_season()
        next_game_on = self.service.pitching_usage(self.view()).next_game_on
        assert next_game_on is not None
        self.next_game_on = next_game_on

    def set_stamina(self, player_id, stamina):
        orm_models.PennantPlayerRatings.objects.filter(player_id=player_id).update(stamina=stamina)

    def usage_with(self, *, starts=(), outings=()):
        """`starts` は (投手, 何日前) の先発、`outings` は (投手, 何日前) の救援登板。画面の HTML も返す。"""
        day = self.next_game_on
        context = SimulationContext(
            teams=(),
            last_starts=tuple(LastStart(pid, day - timedelta(days=ago)) for pid, ago in starts),
            recent_outings=tuple(PitchingOuting(day - timedelta(days=ago), (pid,)) for pid, ago in outings),
        )
        with mock.patch(self.LOAD, return_value=context):
            usage = self.service.pitching_usage(self.view())
            content = self.client.get(self.club_url("pitching")).content.decode()
        return usage, content

    @staticmethod
    def rows_of(usage):
        return {row.player_id: row for row in (*usage.rotation, *usage.bullpen, *filter(None, [usage.closer]))}

    def test_a_starter_is_judged_by_the_rest_since_the_last_start(self):
        """中5日以上（6日後）と先発の記録が無ければなし、中4日は軽い、中3日以下は重い。"""
        first, second, third, *_ = self.view().rotation_ids
        rested = MIN_DAYS_BETWEEN_STARTS

        usage, content = self.usage_with(starts=((first, rested), (second, rested - 1), (third, rested - 2)))

        levels = [row.fatigue for row in usage.rotation]
        self.assertEqual(levels[:3], [FatigueLevel.NONE, FatigueLevel.LIGHT, FatigueLevel.HEAVY])
        self.assertTrue(all(level is FatigueLevel.NONE for level in levels[3:]), "先発の記録が無ければなし")
        self.assertIn("に先発したら", content)
        self.assertIn('<span class="badge bg-warning text-dark">軽い</span>', content)
        self.assertIn('<span class="badge bg-danger">重い</span>', content)

    def test_a_reliever_does_not_carry_the_rest_of_a_starter(self):
        """同じ「4日前に先発した」記録でも、救援の行は救援で投げたらの区分（休養の点は付かない）。"""
        reliever = self.view().bullpen_ids[0]

        usage, _ = self.usage_with(starts=((reliever, MIN_DAYS_BETWEEN_STARTS - 2),))

        self.assertEqual(self.rows_of(usage)[reliever].fatigue, FatigueLevel.NONE)

    def test_a_reliever_is_judged_by_the_recent_appearances_and_the_stamina(self):
        """窓の中で3日投げたスタミナの低い救援は軽い。スタミナ55の3連投目に窓の中のもう1日が重なると重い。"""
        busy, worn, idle = self.view().bullpen_ids[:3]
        self.set_stamina(busy, 36)
        self.set_stamina(worn, HIGH_STAMINA_RELIEVER)

        usage, content = self.usage_with(outings=((busy, 1), (busy, 3), (busy, 5), (worn, 1), (worn, 2), (worn, 4)))

        rows = self.rows_of(usage)
        self.assertEqual(rows[idle].fatigue, FatigueLevel.NONE)
        self.assertEqual(rows[busy].fatigue, FatigueLevel.LIGHT)
        self.assertTrue(rows[worn].can_pitch, "スタミナ55以上は3連投目も投げられる")
        self.assertEqual(rows[worn].fatigue, FatigueLevel.HEAVY)
        self.assertIn("疲労（救援で投げたら）", content)

    def test_the_level_is_the_one_the_engine_would_use(self):
        """区分は domain の式（`pregame_fatigue_points` → `FatigueLevel.from_points`）と同じ答えになる。"""
        reliever = self.view().bullpen_ids[0]
        self.set_stamina(reliever, 40)
        ago = (1, 2, 4)

        usage, _ = self.usage_with(outings=tuple((reliever, back) for back in ago))

        history = PitchingHistory()
        for back in ago:
            history.record(self.next_game_on - timedelta(days=back), [reliever])
        points = pregame_fatigue_points(history.workload(reliever, self.next_game_on), as_starter=False, stamina=40)
        self.assertEqual(self.rows_of(usage)[reliever].fatigue, FatigueLevel.from_points(points))

    def test_a_pitcher_who_cannot_pitch_shows_the_rest_instead_of_a_level(self):
        """連投の上限で投げられない救援は「連投のため休み」で、疲労の区分を並べない。"""
        tired = self.view().bullpen_ids[0]
        self.set_stamina(tired, HIGH_STAMINA_RELIEVER - 1)

        usage, content = self.usage_with(outings=((tired, 1), (tired, 2)))

        self.assertFalse(self.rows_of(usage)[tired].can_pitch)
        # 能力を変えると AI が抑えに回すことがある。抑えの行も救援陣の表にあるので、表の中で探す
        bullpen = content.split('id="club-bullpen"')[1]
        row = re.search(rf'href="[^"]*/{tired}/"[^<]*</a>.*?</tr>', bullpen, re.DOTALL)
        assert row is not None
        self.assertIn("連投のため休み", row.group(0))
        self.assertNotIn("軽い", row.group(0))
        self.assertNotIn("重い", row.group(0))

    def test_the_owner_editing_a_manual_rotation_still_sees_the_fatigue(self):
        """手動のローテーションの編集欄の下にも、いま進めたときのローテーションの休養と疲労を出す。"""
        rotation = list(self.view().rotation_ids[:5])
        self.service.set_rotation(self.home_a.id, rotation)
        self.client.force_login(self.owner)

        _, content = self.usage_with(starts=((rotation[0], MIN_DAYS_BETWEEN_STARTS - 1),))

        self.assertIn('name="rotation_1"', content, "編集欄が出ている")
        self.assertIn('id="club-rotation-usage"', content)
        state = content.split('id="club-rotation-usage"')[1].split('id="club-closer"')[0]
        self.assertIn("に先発したら", state)
        self.assertIn('<span class="badge bg-warning text-dark">軽い</span>', state, "中4日の先発は軽い")


class FatigueColumnWithoutScheduleTest(ClubScreenCase):
    def test_without_a_schedule_there_is_no_level_and_no_column(self):
        usage = self.service.pitching_usage(self.view())
        content = self.client.get(self.club_url("pitching")).content.decode()

        rows = (*usage.rotation, *usage.bullpen, *filter(None, [usage.closer]))
        self.assertTrue(rows)
        self.assertTrue(all(row.fatigue is FatigueLevel.NONE for row in rows))
        self.assertNotIn("疲労（", content)
