"""ペナントの試合を1つずつ作る共通部分（公式戦の進行とポストシーズンの進行が同じものを使う）。

1回の「進める」の間に要る材料（その年の能力から作った登録候補・GM の編成の上書き・直近の登板）を持ち、
対戦を渡されるたびに1試合を作る。試合の結果は次の3つだけで決まるので、1日ずつ進めてもまとめて進めても同じになる。

- 試合ごとの乱数は、世界のシード・年・試合の識別（`game_key`。区分・日付・ホーム・ビジター）から作る
- 疲労（先発の間隔・連投）は保存せず、直近の登板から導く。作った試合の登板はその場で足していく
- 1軍登録・スタメン・継投は、能力と GM の編成の上書き（`ClubPlan`）だけから決まる。1回の「進める」の間は
  球団ごとに1回だけ決める（上書きが使えない区画は自動に落とし、理由を `notices` で返す）

保存はしない（呼ぶ側が、自分の検査とロックの中で保存する）。
"""

from __future__ import annotations

from datetime import date

from ..domain.entities import Game
from ..domain.exceptions import InvalidRoster
from ..domain.pennant.club_plan import ClubLimits, ClubPlan, ResolvedClub, resolve_club, strictest_game_limit
from ..domain.pennant.world import World
from ..domain.repositories import ClubPlanRepository, RatingsRepository
from ..domain.simulation.engine import MAX_INNINGS, simulate_game
from ..domain.simulation.fatigue import PitchingHistory
from ..domain.simulation.game_day import simulated_attendance, simulated_times
from ..domain.simulation.manager import ClubRoster, SimBatter, SimPitcher
from ..domain.simulation.randomness import game_seed, make_random
from ..domain.simulation.ratings import BatterRatings, PitcherRatings
from ..domain.value_objects import GameStage
from .dto import PlanNotice, SimulationContext, SimulationTeam
from .queries import SimulationContextQuery

# ポストシーズンの試合の識別の接頭辞（公式戦の識別と混ざらない）
_POSTSEASON_KEY_PREFIX = "post"


def game_key(stage: GameStage, day: date, home_team_id: int, away_team_id: int) -> str:
    """試合ごとの乱数の識別。**公式戦は切り出す前と同じ形**（`<日付>:<ホーム>:<ビジター>`）で、
    同じ世界の公式戦の結果を変えない。ポストシーズンは接頭辞を付け、同じ日・同じ対戦でも公式戦と別の乱数にする。"""
    key = f"{day.isoformat()}:{home_team_id}:{away_team_id}"
    if stage is GameStage.POSTSEASON:
        return f"{_POSTSEASON_KEY_PREFIX}:{key}"
    return key


class DaySimulator:
    """1回の「進める」の間の試合の作り方。`load` で材料を読み、`play` で1試合ずつ作る（保存はしない）。"""

    def __init__(
        self,
        *,
        world_seed: int,
        context: SimulationContext,
        pools: dict[int, ClubRoster],
        plans: dict[int, ClubPlan],
    ) -> None:
        self._world_seed = world_seed
        self._teams = {team.team_id: team for team in context.teams}
        self._pools = pools
        self._plans = plans
        self._history = pitching_history(context)
        self._clubs: dict[int, ResolvedClub] = {}
        self._notices: list[PlanNotice] = []
        self._game_limit = strictest_game_limit(team.foreign_game_limit for team in context.teams)

    @classmethod
    def load(
        cls,
        *,
        world: World,
        first_day: date,
        context_query: SimulationContextQuery,
        ratings: RatingsRepository,
        plans: ClubPlanRepository,
    ) -> DaySimulator:
        """`first_day`（今回の最初の試合の日）より前の試合から導ける状態と、その年の能力と編成を読む。

        能力は `first_day` の年のもの（公式戦は日程の最初の日の年、ポストシーズンは同じ年の続き。
        domain の `ratings_year` と一致する）。
        """
        context = context_query.load(before=first_day)
        abilities = {item.player_id: item.ratings for item in ratings.find_by_year(first_day.year)}
        return cls(
            world_seed=world.seed,
            context=context,
            pools={team.team_id: pool_of(team, abilities) for team in context.teams},
            plans={plan.team_id: plan for plan in plans.find_all()},
        )

    @property
    def notices(self) -> tuple[PlanNotice, ...]:
        """編成の上書きが使えず自動に落ちた区画（球団ごと・区画ごとに、最初の日の1回だけ）。"""
        return tuple(self._notices)

    def play(
        self,
        day: date,
        home_team_id: int,
        away_team_id: int,
        *,
        stage: GameStage,
        max_innings: int | None = MAX_INNINGS,
    ) -> Game:
        """1試合を作る（時刻と観客数も）。作った試合の登板は、この後の試合の疲労に数える。

        試合を組めない球団（打順を組む野手がいないなど）は、日付と対戦を添えた InvalidRoster。
        """
        home_club = self._club_of(home_team_id, day)
        away_club = self._club_of(away_team_id, day)
        home, away = home_club.roster, away_club.roster
        key = game_key(stage, day, home_team_id, away_team_id)
        rng = make_random(game_seed(self._world_seed, day.year, key))
        try:
            simulated = simulate_game(
                rng,
                home,
                away,
                played_on=day,
                stage=stage,
                history=self._history,
                # リーグをまたぐ交流戦（とポストシーズンの日本シリーズ）は、ホーム球団のリーグの枠で行う
                foreign_game_limit=self._teams[home_team_id].foreign_game_limit,
                home_orders=home_club.orders,
                away_orders=away_club.orders,
                max_innings=max_innings,
            )
        except InvalidRoster as error:
            raise InvalidRoster(f"{day} {home.name} 対 {away.name}: {error}") from error
        # 時刻と観客数は、打席を進めた乱数とは別の乱数源（用途の接尾辞付き）で作る。
        # 試合の結果（得点・打席）と「まとめて進める」「1日ずつ進める」の一致を変えない
        day_rng = make_random(game_seed(self._world_seed, day.year, f"{key}:game-day"))
        simulated.game.times = simulated_times(simulated.game, day_rng)
        simulated.game.attendance = simulated_attendance(self._teams[home_team_id].stadium_capacity, day, day_rng)
        return simulated.game

    def _club_of(self, team_id: int, day: date) -> ResolvedClub:
        # 1軍登録・オーダー・投手陣は、能力と GM の上書きだけから決まるので、1回の「進める」の間は
        # 変わらない。日ごとに選び直さない（上書きが使えない理由も、球団ごとに最初の日の1回だけ知らせる）
        if team_id not in self._clubs:
            team = self._teams[team_id]
            limits = ClubLimits(foreign_roster_limit=team.foreign_roster_limit, foreign_game_limit=self._game_limit)
            self._clubs[team_id] = resolve_club(self._plans.get(team_id), self._pools[team_id], limits)
            self._notices.extend(
                PlanNotice(team_id, team.name, day, fallback.section, fallback.reason, fallback.falls_back)
                for fallback in self._clubs[team_id].fallbacks
            )
        return self._clubs[team_id]


def pool_of(team: SimulationTeam, ratings: dict[int, BatterRatings | PitcherRatings]) -> ClubRoster:
    """球団の登録候補。能力（その年のもの）が無い選手は出られない。"""
    batters: list[SimBatter] = []
    pitchers: list[SimPitcher] = []
    for player in team.players:
        ability = ratings.get(player.player_id)
        # 登録位置と能力の種類の食い違いは、能力の保存のときに弾いている
        if isinstance(ability, PitcherRatings):
            pitchers.append(SimPitcher(player.player_id, player.name, ability, player.is_foreign))
        elif isinstance(ability, BatterRatings):
            batters.append(
                SimBatter(
                    player.player_id, player.name, player.position, ability, player.is_foreign, throws=player.throws
                )
            )
    return ClubRoster(team_id=team.team_id, name=team.name, batters=tuple(batters), pitchers=tuple(pitchers))


def pitching_history(context: SimulationContext) -> PitchingHistory:
    """読んだ直近の登板から、疲労を導くための記録を作る。"""
    history = PitchingHistory()
    for start in context.last_starts:
        history.record(start.played_on, [start.pitcher_id])
    for outing in context.recent_outings:
        history.record(outing.played_on, list(outing.pitcher_ids))
    return history
