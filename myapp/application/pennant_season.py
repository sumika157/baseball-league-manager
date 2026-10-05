"""ペナントのシーズン進行。日程を作り、日を進めて試合を作る。

**進行の状態は保存しない。** 「今日」は消化した最後の試合日、シーズン中かは未消化の対戦が残っているか、
で日程（`FixtureRepository`）と試合から導く。カーソルを持つと、日程・試合と食い違いうる。

1日ずつ進めても1週間まとめて進めても、**同じ結果になる**。次の3つがそろっているため。

- 試合ごとの乱数は、世界のシード・年・試合の識別（日付とホーム・ビジター）から作る
  （進めた順序や回数に依存しない。`randomness.game_seed`）
- 疲労（先発の間隔・連投）は保存せず、直近の登板から導く。まとめて進めるときは、読んだ登板に
  その場で作った試合の登板を足していく
- 1軍登録・スタメン・継投は、能力と GM の編成の上書き（`ClubPlan`）だけから決まる。上書きが使えない区画は
  その区画だけ自動に落とし、理由を `AdvanceReport.plan_notices` で返す（例外で止めない）

試合は日の区切りで、`FLUSH_EVERY_GAMES` 試合ごとにまとめて保存する（メモリに溜める量の上限）。
**日の途中では切らない**。残りの対戦が「今日」より前になり、進められなくなるため。
保存と消化済みの対戦の削除は同じトランザクションで行う。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from types import EllipsisType

from ..domain.entities import Game
from ..domain.exceptions import AlreadyAdvanced, InvalidRoster, InvalidSchedule
from ..domain.pennant.club_plan import ClubLimits, ResolvedClub, resolve_club, strictest_game_limit
from ..domain.pennant.schedule import (
    AdvanceTarget,
    Fixture,
    ScheduleRules,
    dates_to_play,
    default_opening_day,
    generate_schedule,
)
from ..domain.pennant.world import World
from ..domain.repositories import (
    ClubPlanRepository,
    FixtureRepository,
    GameRepository,
    LeagueRepository,
    RatingsRepository,
    TeamRepository,
    WorldRepository,
)
from ..domain.simulation.engine import simulate_game
from ..domain.simulation.manager import ClubRoster, PitchingHistory, SimBatter, SimPitcher
from ..domain.simulation.randomness import game_seed, make_random
from ..domain.simulation.ratings import BatterRatings, PitcherRatings
from .dto import AdvanceReport, PlanNotice, SimulationContext, SimulationTeam
from .queries import SimulationContextQuery

# メモリに溜める試合数の上限（`seed_virtual_games` の `FLUSH_EVERY_GAMES` に倣う）
FLUSH_EVERY_GAMES = 200

# 保存と消化済みの対戦の削除を1つにまとめる。実装は組み立ての1か所が渡す（`transaction.atomic`）
AtomicBlock = Callable[[], AbstractContextManager[object]]


def _saved_id(value: int | None) -> int:
    """保存済みの集約から取り出す id。永続化された後は必ず値がある。"""
    assert value is not None, "保存済みの集約には id がある"
    return value


class PennantSeasonService:
    """ペナントの世界ひとつのシーズン進行。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        leagues: LeagueRepository,
        teams: TeamRepository,
        games: GameRepository,
        fixtures: FixtureRepository,
        ratings: RatingsRepository,
        plans: ClubPlanRepository,
        context_query: SimulationContextQuery,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._leagues = leagues
        self._teams = teams
        self._games = games
        self._fixtures = fixtures
        self._ratings = ratings
        self._plans = plans
        self._context_query = context_query
        self._atomic = atomic

    def ensure_schedule(self) -> bool:
        """その年の日程が無ければ作って保存する。作ったかどうかを返す。

        作るのは世界の開幕年の日程だけで、**日程も試合もまだ無い世界**のときに限る（シーズンが終わって
        日程が空の世界に、日程を作り足さない。翌年の日程はシーズンを締めるときに作る）。
        同じ世界なら、いつ呼んでも同じ日程になる（乱数は世界のシードから作る）。
        """
        if self._fixtures.find_all() or self._context_query.last_played_on() is not None:
            return False
        world = self._worlds.find_by_id(self._world_id)
        teams = self._teams.find_all()
        leagues = {
            _saved_id(league.id): sorted(_saved_id(team.id) for team in teams if team.league_id == league.id)
            for league in self._leagues.find_all()
        }
        rules = ScheduleRules()
        schedule = generate_schedule(
            leagues,
            rules,
            default_opening_day(world.start_year, rules),
            make_random(game_seed(world.seed, world.start_year, "schedule")),
            season=world.start_year,
        )
        with self._atomic():
            self._fixtures.add_all(schedule)
        return True

    def advance(
        self,
        target: AdvanceTarget,
        *,
        max_games: int | None = None,
        expected_today: date | None | EllipsisType = ...,
    ) -> AdvanceReport:
        """日程を `target` の分だけ進める。未消化の対戦ごとに試合を作って保存し、その対戦を消す。

        日程がまだ無い世界は、先に開幕年の日程を作る。未消化が無ければ何もせず、空の結果を返す。
        試合ができなくなる世界（打順を組む野手がいない球団など）は InvalidRoster で、書き出し済みの日は
        そのまま残る（日の区切りで書き出すので、「今日」が中途半端にならない。まだ書き出していない
        直近の日（最大 `FLUSH_EVERY_GAMES` 試合ぶん）は捨てられ、次に進めるときに同じ結果で作り直される）。

        `max_games` を渡すと、今回の範囲の試合数がそれを超えるときは何も作らずに InvalidSchedule にする
        （画面から進めるときの上限。管理コマンドは渡さない）。

        `expected_today` を渡すと、世界の今日（消化した最後の試合日。まだ無ければ None）がそれと違うときは
        何も作らずに AlreadyAdvanced にする（画面を開いたあとに別の操作で進んだ世界を、さらに進めない。
        管理コマンドは渡さない）。
        """
        world = self._worlds.find_by_id(self._world_id)
        last_played = self._context_query.last_played_on()
        if expected_today is not ... and expected_today != last_played:
            raise AlreadyAdvanced("既に進んでいます。最新の状態を表示しました。")
        created = self.ensure_schedule()
        pending = self._fixtures.find_all()
        dates = dates_to_play(
            pending, last_played if last_played is not None else date.min, target, world.managed_team_id
        )
        if not dates:
            return AdvanceReport(
                played_dates=(), games=0, today=last_played, remaining_fixtures=len(pending), schedule_created=created
            )
        if max_games is not None:
            days = set(dates)
            wanted = sum(1 for fixture in pending if fixture.date in days)
            if wanted > max_games:
                # 1回で作る試合が多すぎると応答が返らない。何も作らずに断る
                raise InvalidSchedule(
                    f"一度に進められるのは{max_games}試合までです（この範囲は{wanted}試合あります）。"
                )

        played, notices = self._play(world, pending, dates)
        return AdvanceReport(
            played_dates=tuple(dates),
            games=played,
            today=dates[-1],
            remaining_fixtures=len(pending) - played,
            schedule_created=created,
            plan_notices=notices,
        )

    # --- 内部 ---

    def _play(self, world: World, pending: list[Fixture], dates: list[date]) -> tuple[int, tuple[PlanNotice, ...]]:
        """`dates` の日の対戦をシミュレーションして保存し、作った試合数と、自動に落ちた編成の知らせを返す。"""
        context = self._context_query.load(before=dates[0])
        history = _pitching_history(context)
        teams = {team.team_id: team for team in context.teams}
        # 能力を引く年。`dates[0]` は最後の試合日より後の未消化の最初の日。消化した日程は試合の保存と
        # 同じトランザクションで消えるので、日程の最初の日（`FixtureRepository.first_date`）と同じになり、
        # domain の `ratings_year`（編成・表示が使う規則）と一致する
        pools = self._pools(context, dates[0].year)
        plans = {plan.team_id: plan for plan in self._plans.find_all()}
        clubs: dict[int, ResolvedClub] = {}
        notices: list[PlanNotice] = []
        game_limit = strictest_game_limit(team.foreign_game_limit for team in context.teams)

        def club_of(team_id: int, day: date) -> ResolvedClub:
            # 1軍登録・オーダー・投手陣は、能力と GM の上書きだけから決まるので、1回の「進める」の間は
            # 変わらない。日ごとに選び直さない（上書きが使えない理由も、球団ごとに最初の日の1回だけ知らせる）
            if team_id not in clubs:
                team = teams[team_id]
                limits = ClubLimits(foreign_roster_limit=team.foreign_roster_limit, foreign_game_limit=game_limit)
                clubs[team_id] = resolve_club(plans.get(team_id), pools[team_id], limits)
                notices.extend(
                    PlanNotice(team_id, team.name, day, fallback.section, fallback.reason)
                    for fallback in clubs[team_id].fallbacks
                )
            return clubs[team_id]

        by_date: dict[date, list[Fixture]] = {}
        for fixture in pending:
            by_date.setdefault(fixture.date, []).append(fixture)

        games: list[Game] = []
        consumed: list[Fixture] = []
        total = 0
        for day in dates:
            for fixture in by_date[day]:
                home_club = club_of(fixture.home_team_id, day)
                away_club = club_of(fixture.visitor_team_id, day)
                home, away = home_club.roster, away_club.roster
                rng = make_random(
                    game_seed(
                        world.seed, day.year, f"{day.isoformat()}:{fixture.home_team_id}:{fixture.visitor_team_id}"
                    )
                )
                try:
                    simulated = simulate_game(
                        rng,
                        home,
                        away,
                        played_on=day,
                        history=history,
                        # リーグをまたぐ交流戦は、ホーム球団のリーグの枠で行う
                        foreign_game_limit=teams[fixture.home_team_id].foreign_game_limit,
                        home_orders=home_club.orders,
                        away_orders=away_club.orders,
                    )
                except InvalidRoster as error:
                    raise InvalidRoster(f"{day} {home.name} 対 {away.name}: {error}") from error
                games.append(simulated.game)
                consumed.append(fixture)
            # 日の途中では書き出さない（残りの対戦が「今日」より前になり、進められなくなる）
            if len(games) >= FLUSH_EVERY_GAMES:
                total += self._flush(games, consumed)
                games, consumed = [], []
        return total + self._flush(games, consumed), tuple(notices)

    def _flush(self, games: list[Game], consumed: list[Fixture]) -> int:
        """試合を保存し、消化した対戦を消す。同じトランザクションで行う。"""
        if not games:
            return 0
        with self._atomic():
            # 先に消す。すでに消化された対戦があれば InvalidSchedule で、試合を書かずに止まる
            # （同じ世界を同時に進めて、同じ対戦の試合が2つできるのを防ぐ）
            self._fixtures.remove(consumed)
            self._games.add_all(games)
        return len(games)

    def _pools(self, context: SimulationContext, year: int) -> dict[int, ClubRoster]:
        """球団ごとの登録候補。能力（その年のもの）が無い選手は出られない。"""
        ratings = {item.player_id: item.ratings for item in self._ratings.find_by_year(year)}
        return {team.team_id: pool_of(team, ratings) for team in context.teams}


def pool_of(team: SimulationTeam, ratings: dict[int, BatterRatings | PitcherRatings]) -> ClubRoster:
    batters: list[SimBatter] = []
    pitchers: list[SimPitcher] = []
    for player in team.players:
        ability = ratings.get(player.player_id)
        # 登録位置と能力の種類の食い違いは、能力の保存のときに弾いている
        if isinstance(ability, PitcherRatings):
            pitchers.append(SimPitcher(player.player_id, player.name, ability, player.is_foreign))
        elif isinstance(ability, BatterRatings):
            batters.append(SimBatter(player.player_id, player.name, player.position, ability, player.is_foreign))
    return ClubRoster(team_id=team.team_id, name=team.name, batters=tuple(batters), pitchers=tuple(pitchers))


def _pitching_history(context: SimulationContext) -> PitchingHistory:
    """読んだ直近の登板から、疲労を導くための記録を作る。"""
    history = PitchingHistory()
    for start in context.last_starts:
        history.record(start.played_on, [start.pitcher_id])
    for outing in context.recent_outings:
        history.record(outing.played_on, list(outing.pitcher_ids))
    return history
