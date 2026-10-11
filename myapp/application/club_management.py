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

from ..domain.exceptions import InvalidClubPlan, InvalidRoster, TeamNotFound
from ..domain.pennant.club_plan import (
    LINEUP_POSITION_ORDER,
    ClubLimits,
    ClubMember,
    ClubPlan,
    LineupChoice,
    PlanSection,
    ResolvedClub,
    arrange_lineup,
    members_of,
    resolve_club,
    strictest_game_limit,
    unfilled_position,
)
from ..domain.pennant.season import ratings_year
from ..domain.repositories import ClubPlanRepository, FixtureRepository, RatingsRepository, WorldRepository
from ..domain.simulation.manager import (
    LINEUP_SIZE,
    MIN_ACTIVE_PITCHERS,
    MIN_ROTATION_SIZE,
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

    def reserve_player_ids(self, team_id: int) -> frozenset[int]:
        """いま在籍していて、進めたときに1軍に使われない選手の id（球団の画面の「2軍」の印の出典）。

        1軍は `view()` と同じ（`resolve_club` の結果）。受け持たない球団でも、その球団の（AI が決める）1軍で判定する。
        能力の無い選手は1軍に使われないので含まれる。退団・引退した選手（もう在籍していない）は含まれない。
        """
        situation = self._situation(team_id)
        return situation.on_team_ids - situation.active_ids(self._plans.find_by_team(team_id))

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

    def start_manual_active(self, team_id: int) -> ClubPlanView:
        """いま映している自動の1軍登録を初期値にして、1軍登録を手動にする。

        この球団の在籍選手では守備位置（捕1・内4・外3）を埋められないときは、選手層の不足として理由を返す
        （AI の選び方の問題とは分ける）。
        """
        situation = self._situation(team_id)
        plan = self._plans.find_by_team(team_id)
        self._ensure_squad_covers_field(situation)
        resolved = resolve_club(plan, situation.pool, situation.limits)
        view = situation.view(plan, resolved)
        plan.set_active(view.active_ids, roster=situation.members, limits=situation.limits)
        self._plans.save(plan)
        return situation.view(plan)

    def start_manual_lineup(self, team_id: int) -> ClubPlanView:
        """いま映している自動のオーダーを初期値にして、オーダーを手動にする。

        AI のオーダーは空いた枠を位置を問わず埋めるので、手動の規則（登録位置が就ける位置だけ）を
        満たさないことがある。選手と打順はそのままに守備位置を割り直し、それでも足りなければ1軍の控えから
        入れ替える（外国人は出場枠まで）。組めないときは理由を返す（汎用の検査エラーにしない）。
        """
        situation = self._situation(team_id)
        plan = self._plans.find_by_team(team_id)
        self._ensure_squad_covers_field(situation)
        resolved = resolve_club(plan, situation.pool, situation.limits)
        proposed = [LineupChoice(row.player_id, row.position) for row in situation.view(plan, resolved).lineup]
        bench = [situation.members[b.player_id] for b in resolved.roster.batters]
        fitted = arrange_lineup(
            proposed, situation.members, bench=bench, foreign_game_limit=situation.limits.foreign_game_limit
        )
        if fitted is None:
            gap = unfilled_position(bench)
            if gap is not None:
                raise InvalidClubPlan(
                    f"1軍に{gap.full_name}を守れる野手が足りないため、手動のオーダーを組めません。1軍登録を見直してください。"
                )
            if arrange_lineup(proposed, situation.members, bench=bench) is not None:
                raise InvalidClubPlan(
                    "外国人選手の出場枠の範囲では、守備位置を満たすオーダーを組めません。1軍登録を見直してください。"
                )
            raise InvalidClubPlan(
                "自動のオーダーと1軍の控えでは、守備位置を満たすオーダーを組めません。1軍登録を見直してください。"
            )
        active_ids = {b.player_id for b in resolved.roster.batters} | {p.player_id for p in resolved.roster.pitchers}
        plan.set_lineup(fitted, roster=situation.members, active_ids=active_ids, limits=situation.limits)
        self._plans.save(plan)
        return situation.view(plan)

    @staticmethod
    def _ensure_squad_covers_field(situation: _Situation) -> None:
        gap = unfilled_position(situation.members.values())
        if gap is not None:
            raise InvalidClubPlan(
                f"この球団には{gap.full_name}を守れる野手がいません（選手層が足りないため、手動の編成は組めません）。"
            )

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
        return _Situation(
            team.team_id, team.name, year, pool, limits, frozenset(player.player_id for player in team.players)
        )

    def _current_year(self) -> int:
        """次に試合をする年（能力を引く年）。規則は domain の `ratings_year`（表示・日を進める処理と共通）。"""
        return ratings_year(
            next_game_on=self._fixtures.first_date(),
            last_played_on=self._context_query.last_played_on(),
            start_year=self._worlds.find_by_id(self._world_id).start_year,
        )


class _Situation:
    """ある球団の、いまの登録候補と枠。編成の検査と、画面の材料の組み立てに使う。"""

    def __init__(
        self,
        team_id: int,
        team_name: str,
        year: int,
        pool: ClubRoster,
        limits: ClubLimits,
        on_team_ids: frozenset[int],
    ) -> None:
        self.team_id = team_id
        self.team_name = team_name
        self.year = year
        self.pool = pool
        self.limits = limits
        self.members: dict[int, ClubMember] = members_of(pool)
        # いま在籍している選手（能力の有無によらない）。1軍に使われるのはこのうち能力のある人だけ
        self.on_team_ids = on_team_ids

    @staticmethod
    def _ids_of(roster: ClubRoster) -> set[int]:
        return {b.player_id for b in roster.batters} | {p.player_id for p in roster.pitchers}

    def active_ids(self, plan: ClubPlan) -> set[int]:
        """いま 1軍にいる選手（1軍登録が自動か、使えない手動なら AI が選んだ人）。"""
        return self._ids_of(resolve_club(plan, self.pool, self.limits).roster)

    def view(self, plan: ClubPlan, resolved: ResolvedClub | None = None) -> ClubPlanView:
        """`resolved` は、同じ `plan` を `resolve_club` した結果（渡せば解決し直さない）。"""
        if resolved is None:
            resolved = resolve_club(plan, self.pool, self.limits)
        roster = resolved.roster
        active_ids = self._ids_of(roster)

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
                min_rotation_size=MIN_ROTATION_SIZE,
                min_active_pitchers=MIN_ACTIVE_PITCHERS,
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
            notices=tuple(ClubPlanNotice(f.section, f.reason, f.falls_back) for f in resolved.fallbacks),
        )
