"""球団の編成の管理。1軍登録・オーダー・ローテーション・抑えの上書きと、自動に戻すこと、自動編成の提案。

編成（`ClubPlan`）は**上書きだけ**を持ち、自動の区画は毎日 AI 監督が決める。このサービスは
上書きを決める／外すだけで、試合の進行には関わらない（進めるときに `PennantSeasonService` が
`resolve_club` で上書きを当てはめる）。

`TeamApplicationService` には足さない（あのクラスは既に大きい）。読むのは集約ではなく、
球団・選手の現在の状態（`SimulationContextQuery.teams()`）と能力（`RatingsRepository`）で、
`Team` 集約は組み立てない。リポジトリと参照クエリは、世界の範囲で組み立てて渡す。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from ..domain.exceptions import InvalidRoster, TeamNotFound
from ..domain.pennant.club_plan import (
    LINEUP_POSITION_ORDER,
    ClubLimits,
    ClubMember,
    ClubPlan,
    LineupChoice,
    PlanSection,
    members_of,
    resolve_club,
    strictest_game_limit,
)
from ..domain.pennant.season import ratings_year
from ..domain.repositories import ClubPlanRepository, FixtureRepository, RatingsRepository, WorldRepository
from ..domain.simulation.manager import (
    LINEUP_SIZE,
    RECENT_PITCHING_DAYS,
    ROTATION_SIZE,
    ClubRoster,
    ForeignQuota,
    PitchingHistory,
    choose_lineup,
    plan_pitching_staff,
)
from ..domain.value_objects import Position
from .dto import (
    ClubLimitsView,
    ClubLineupRow,
    ClubPitcherUsage,
    ClubPitchingUsage,
    ClubPlanNotice,
    ClubPlanView,
    ClubPlayerRow,
    ClubRosterCounts,
)
from .pennant_season import pitching_history, pool_of
from .queries import SimulationContextQuery


class ClubManagementService:
    """ペナントの世界ひとつの、球団の編成を管理する。世界の範囲で組み立てて渡す。

    変更は、球団ごとに1つの編成（`ClubPlan`）を読み、検査を通る上書きだけを入れて保存する。
    戻り値はどれも、変更後の画面の材料（`ClubPlanView`）。
    """

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        plans: ClubPlanRepository,
        ratings: RatingsRepository,
        fixtures: FixtureRepository,
        context_query: SimulationContextQuery,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._plans = plans
        self._ratings = ratings
        self._fixtures = fixtures
        self._context_query = context_query

    # --- 見る ---

    def view(self, team_id: int) -> ClubPlanView:
        """いま進めたときに使われる編成。手動が使えなくなっている区画は、自動に落ちた形と理由を添える。"""
        situation = self._situation(team_id)
        return situation.view(self._plans.find_by_team(team_id))

    def propose(self, team_id: int) -> ClubPlanView:
        """AI 監督の自動編成（上書きを無視した提案）。保存はしない。"""
        return self._situation(team_id).view(ClubPlan(team_id=team_id))

    def pitching_usage(self, view: ClubPlanView) -> ClubPitchingUsage:
        """投手陣の状態（直近の登板・前回の先発・次の試合日に投げられるか）。`view` は `view()` の結果。

        次の試合日は受け持つ球団の未消化の対戦の最初の日。疲労は保存せず、直近の登板から導く
        （進めるときと同じ `PitchingHistory`）。日程が残っていなければ、状態は既定で返す。
        """
        team_id = view.team_id
        next_game_on = self._fixtures.first_date(team_id)
        history = PitchingHistory()
        recent_days: tuple[date, ...] = ()
        if next_game_on is not None:
            # 進めるときと同じ手順で、直近の登板から疲労の記録を作る（手順を書き写さない）
            history = pitching_history(self._context_query.load(before=next_game_on))
            recent_days = tuple(next_game_on - timedelta(days=ago) for ago in range(RECENT_PITCHING_DAYS, 0, -1))
        players = {row.player_id: row for row in view.players}

        def usage_of(player_id: int) -> ClubPitcherUsage:
            row = players[player_id]
            if next_game_on is None:
                return ClubPitcherUsage(player_id, row.name, row.is_foreign, (), None, True)
            return ClubPitcherUsage(
                player_id=player_id,
                name=row.name,
                is_foreign=row.is_foreign,
                pitched_recently=tuple(history.pitched_on(player_id, day) for day in recent_days),
                days_since_start=history.days_since_start(player_id, next_game_on),
                can_pitch=history.can_pitch_on(player_id, next_game_on),
            )

        return ClubPitchingUsage(
            next_game_on=next_game_on,
            recent_days=recent_days,
            rotation=tuple(usage_of(player_id) for player_id in view.rotation_ids),
            closer=usage_of(view.closer_id) if view.closer_id is not None else None,
            bullpen=tuple(usage_of(player_id) for player_id in view.bullpen_ids),
        )

    # --- 上書きする ---

    def set_active_roster(self, team_id: int, player_ids: Sequence[int]) -> ClubPlanView:
        """1軍登録を手動にする。不変条件に反すれば DomainError。"""
        situation = self._situation(team_id)
        plan = self._plans.find_by_team(team_id)
        plan.set_active(player_ids, roster=situation.members, limits=situation.limits)
        self._plans.save(plan)
        return situation.view(plan)

    def set_lineup(self, team_id: int, choices: Sequence[LineupChoice]) -> ClubPlanView:
        """オーダー（打順と守備位置）を手動にする。1軍にいる野手9人でなければ DomainError。"""
        situation = self._situation(team_id)
        plan = self._plans.find_by_team(team_id)
        plan.set_lineup(
            choices, roster=situation.members, active_ids=situation.active_ids(plan), limits=situation.limits
        )
        self._plans.save(plan)
        return situation.view(plan)

    def set_rotation(self, team_id: int, player_ids: Sequence[int]) -> ClubPlanView:
        """ローテーションを手動にする（先発の序列順）。1軍の投手でなければ DomainError。"""
        situation = self._situation(team_id)
        plan = self._plans.find_by_team(team_id)
        plan.set_rotation(player_ids, roster=situation.members, active_ids=situation.active_ids(plan))
        self._plans.save(plan)
        return situation.view(plan)

    def set_closer(self, team_id: int, player_id: int) -> ClubPlanView:
        """抑えを手動にする。1軍の投手でなければ DomainError。"""
        situation = self._situation(team_id)
        plan = self._plans.find_by_team(team_id)
        plan.set_closer(player_id, roster=situation.members, active_ids=situation.active_ids(plan))
        self._plans.save(plan)
        return situation.view(plan)

    def reset(self, team_id: int, section: PlanSection) -> ClubPlanView:
        """区画を自動に戻す。すでに自動なら何も変わらない。"""
        situation = self._situation(team_id)
        plan = self._plans.find_by_team(team_id)
        if section is PlanSection.ACTIVE:
            plan.clear_active()
        elif section is PlanSection.LINEUP:
            plan.clear_lineup()
        elif section is PlanSection.ROTATION:
            plan.clear_rotation()
        else:
            plan.clear_closer()
        self._plans.save(plan)
        return situation.view(plan)

    # --- 内部 ---

    def _situation(self, team_id: int) -> _Situation:
        teams = self._context_query.teams()
        team = next((t for t in teams if t.team_id == team_id), None)
        if team is None:
            raise TeamNotFound(f"球団が見つかりません（id={team_id}）。")
        year = self._current_year()
        # 受け持つ球団の選手だけ読む（世界の全選手の能力は要らない）
        found = self._ratings.find_by_players([player.player_id for player in team.players], year)
        ratings = {item.player_id: item.ratings for item in found}
        pool = pool_of(team, ratings)
        # 出場枠は、進めるときと同じく世界のリーグで最も厳しい値（交流戦でも組めるように）
        limits = ClubLimits(
            foreign_roster_limit=team.foreign_roster_limit,
            foreign_game_limit=strictest_game_limit(t.foreign_game_limit for t in teams),
        )
        return _Situation(team.team_id, team.name, year, pool, limits)

    def _current_year(self) -> int:
        """次に試合をする年（能力を引く年）。規則は domain の `ratings_year`（表示・日を進める処理と共通）。"""
        return ratings_year(
            next_game_on=self._fixtures.first_date(),
            last_played_on=self._context_query.last_played_on(),
            start_year=self._worlds.find_by_id(self._world_id).start_year,
        )


class _Situation:
    """ある球団の、いまの登録候補と枠。編成の検査と、画面の材料の組み立てに使う。"""

    def __init__(self, team_id: int, team_name: str, year: int, pool: ClubRoster, limits: ClubLimits) -> None:
        self.team_id = team_id
        self.team_name = team_name
        self.year = year
        self.pool = pool
        self.limits = limits
        self.members: dict[int, ClubMember] = members_of(pool)

    def active_ids(self, plan: ClubPlan) -> set[int]:
        """いま 1軍にいる選手（1軍登録が自動か、使えない手動なら AI が選んだ人）。"""
        roster = resolve_club(plan, self.pool, self.limits).roster
        return {b.player_id for b in roster.batters} | {p.player_id for p in roster.pitchers}

    def view(self, plan: ClubPlan) -> ClubPlanView:
        resolved = resolve_club(plan, self.pool, self.limits)
        roster = resolved.roster
        active_ids = {b.player_id for b in roster.batters} | {p.player_id for p in roster.pitchers}

        orders = resolved.orders
        if orders is not None and orders.lineup is not None:
            lineup_slots = orders.lineup
        else:
            try:
                lineup_slots = tuple(choose_lineup(roster.batters, ForeignQuota(limit=self.limits.foreign_game_limit)))
            except InvalidRoster:
                lineup_slots = ()
        if orders is not None and orders.staff is not None:
            staff = orders.staff
        else:
            try:
                staff = plan_pitching_staff(roster.pitchers)
            except InvalidRoster:
                staff = None

        batter_count = len(roster.batters)
        return ClubPlanView(
            team_id=self.team_id,
            team_name=self.team_name,
            year=self.year,
            players=tuple(
                ClubPlayerRow(m.player_id, m.name, m.position, m.is_foreign, m.player_id in active_ids)
                for m in self.members.values()
            ),
            active_ids=tuple([b.player_id for b in roster.batters] + [p.player_id for p in roster.pitchers]),
            lineup=tuple(
                ClubLineupRow(order, slot.batter.player_id, slot.batter.name, slot.position)
                for order, slot in enumerate(lineup_slots, start=1)
            ),
            rotation_ids=tuple(p.player_id for p in staff.rotation) if staff is not None else (),
            closer_id=staff.closer.player_id if staff is not None and staff.closer is not None else None,
            bullpen_ids=tuple(p.player_id for p in staff.setup + staff.middle) if staff is not None else (),
            limits=ClubLimitsView(
                active_size=self.limits.active_size,
                foreign_roster_limit=self.limits.foreign_roster_limit,
                foreign_game_limit=self.limits.foreign_game_limit,
                lineup_size=LINEUP_SIZE,
                rotation_size=ROTATION_SIZE,
                lineup_positions=LINEUP_POSITION_ORDER,
            ),
            counts=ClubRosterCounts(
                total=len(active_ids),
                batters=batter_count,
                pitchers=len(roster.pitchers),
                catchers=sum(b.position is Position.CATCHER for b in roster.batters),
                foreign=sum(self.members[player_id].is_foreign for player_id in active_ids),
            ),
            saved_active_ids=plan.active_ids,
            saved_lineup=(
                tuple(
                    ClubLineupRow(
                        order,
                        choice.player_id,
                        self.members[choice.player_id].name if choice.player_id in self.members else "",
                        choice.position,
                    )
                    for order, choice in enumerate(plan.lineup, start=1)
                )
                if plan.lineup is not None
                else None
            ),
            saved_rotation_ids=plan.rotation,
            saved_closer_id=plan.closer_id,
            active_is_manual=plan.active_ids is not None,
            lineup_is_manual=plan.lineup is not None,
            rotation_is_manual=plan.rotation is not None,
            closer_is_manual=plan.closer_id is not None,
            notices=tuple(ClubPlanNotice(f.section, f.reason) for f in resolved.fallbacks),
        )
