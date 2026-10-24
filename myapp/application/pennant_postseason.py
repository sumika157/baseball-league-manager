"""ペナントのポストシーズン（CS と日本シリーズ）の進行。

**勝ち上がりは保存しない。** その年のリーグの最終順位（公式戦だけ）と、その年のポストシーズンの試合の結果から、
domain の `PostseasonBracket` が毎回導く（いまのステージ・次の対戦・第何戦かは持たない）。
保存するのは試合（`Game`。区分は `GameStage.POSTSEASON`）と、世界の方式（`World.postseason_format`）だけ。

**公式戦の進行（`PennantSeasonService`）には足さない。** 公式戦は保存した日程を消化する進め方で、ポストシーズンは
結果から次の対戦を導く進め方なので、二重の試合の防ぎ方も検査も違う。試合を1つずつ作る部分
（能力・編成・疲労・乱数）は `pennant_simulation.DaySimulator` を共有する。

- 試合ごとの乱数の識別は公式戦と別の接頭辞（`game_key`）。1日ずつ進めても終わりまで進めても同じ結果になる
- **ロック（#100）**: 計算はトランザクションの外。中では「世界の最後の試合日（全区分）と次の対戦の日が
  読み込みの前と同じ」ことを読み直してから書く（公式戦の「対戦を先に消す」の代わり。ポストシーズンには消す対戦が無い）。
  変わっていれば書かずに `AlreadyAdvanced`
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from types import EllipsisType

from ..domain.entities import Game
from ..domain.exceptions import AlreadyAdvanced, AlreadyClosed, InvalidSchedule, SeasonNotFinished
from ..domain.pennant.postseason import (
    FinalStanding,
    LeagueFinish,
    PostseasonBracket,
    PostseasonFormat,
    PostseasonResult,
    PostseasonStatus,
    ScheduledGame,
)
from ..domain.pennant.race import final_order
from ..domain.pennant.schedule import MAX_GAMES_PER_ADVANCE, AdvanceTarget
from ..domain.pennant.season import POSTSEASON_SCREEN_TARGETS, regular_season_over_year
from ..domain.pennant.world import World, league_priority
from ..domain.repositories import (
    ClubPlanRepository,
    FixtureRepository,
    GameRepository,
    LeagueRepository,
    RatingsRepository,
    WorldRepository,
)
from ..domain.value_objects import GameStage, TeamRecord
from .dto import LeagueStandings, PlanNotice, PostseasonAdvanceReport
from .pennant_race import ChampionTiebreak, StandingsSource, tiebreak_for
from .pennant_season import AtomicBlock
from .pennant_simulation import DaySimulator
from .queries import GameListQuery, PostseasonQuery, SimulationContextQuery


def _saved_id(value: int | None) -> int:
    assert value is not None, "保存済みのものには id がある"
    return value


class PostseasonBracketReader:
    """その年の勝ち上がりを導く読み取り。進める処理と、シーズンを締める処理の検査が共有する。

    `teams` は世界の範囲で組み立てた順位表の読み取り（`TeamApplicationService`。順位表の出典で、公式戦だけ）。
    """

    def __init__(
        self,
        *,
        teams: StandingsSource,
        games: GameListQuery,
        leagues: LeagueRepository,
        postseason: PostseasonQuery,
    ) -> None:
        self._teams = teams
        # 同率の決着（当該球団間の対戦勝率など）の材料。公式戦だけを読む
        self._games = games
        self._leagues = leagues
        self._postseason = postseason

    def status(self, world: World, year: int) -> PostseasonStatus:
        """その年のポストシーズンの状況。方式が「なし」の世界は何も読まずに `NONE`。"""
        return self.bracket(world, year).status

    def bracket(
        self, world: World, year: int, *, results: Sequence[PostseasonResult] | None = None
    ) -> PostseasonBracket:
        """その年の勝ち上がり。結果が導いた対戦と食い違えば `InvalidPostseason`。

        方式が「なし」・その年の公式戦が無いときは、順位を読まずに状況 `NONE` の勝ち上がりを返す
        （方式「なし」の世界の締める処理に、読み込みを足さない）。
        `results` はその年のポストシーズンの結果。既に読んだ呼び手（勝ち上がりの表）が渡し、省けばここで読む。
        """
        if world.postseason_format is PostseasonFormat.NONE:
            return _no_postseason(year)
        last_regular = self._postseason.last_regular_date(year)
        if last_regular is None:
            return _no_postseason(year)
        standings = self._teams.get_league_standings(year)
        priority = league_priority(
            (_saved_id(league.id) for league in self._leagues.find_all()), year=year, start_year=world.start_year
        )
        return PostseasonBracket.derive(
            world.postseason_format,
            _league_finishes(standings, tiebreak_for(year, games=self._games, teams=self._teams)),
            priority,
            last_regular,
            self._postseason.results(year) if results is None else results,
        )

    def team_names(self) -> dict[int, str]:
        """球団の名前（優勝の球団を知らせるのに使う）。"""
        return {team.id: team.name for team in self._teams.list_teams().rows}


def _no_postseason(year: int) -> PostseasonBracket:
    # 方式「なし」は回が無いので、日付も結果も使わない（状況は `NONE`）
    return PostseasonBracket.derive(PostseasonFormat.NONE, (), (), date(year, 1, 1))


def _league_finishes(standings: Sequence[LeagueStandings], tiebreak: ChampionTiebreak) -> list[LeagueFinish]:
    """リーグ別の順位表を、ポストシーズンの出場を決める**全順序**の最終順位にする。

    同率のあるリーグだけ、優勝と同じ規定（domain の `final_order`。首位は `league_champion` と同じ）で並べる。
    同率の材料（対戦結果・前年の順位・球団の並び）は、要るときに1回だけ読む。
    """
    results = _once(lambda: list(tiebreak.results()))
    previous = _once(lambda: dict(tiebreak.previous_rank()))
    order = _once(lambda: list(tiebreak.order()))
    finishes = []
    for league in standings:
        ranks = {row.team_id: row.rank for row in league.rows}
        if len(set(ranks.values())) == len(ranks):
            ordered = sorted(ranks, key=ranks.__getitem__)
        else:
            ordered = final_order(ranks, results(), previous(), order())
        rows = {row.team_id: row for row in league.rows}
        finishes.append(
            LeagueFinish(
                league_id=league.league_id,
                standings=tuple(
                    FinalStanding(
                        team_id=team_id,
                        record=TeamRecord(
                            wins=rows[team_id].wins, losses=rows[team_id].losses, ties=rows[team_id].ties
                        ),
                    )
                    for team_id in ordered
                ),
            )
        )
    return finishes


def _once[T](read: Callable[[], T]) -> Callable[[], T]:
    """最初に呼ばれたときだけ読み、以後は同じ値を返す（同率のリーグが複数あっても読み直さない）。"""
    cache: list[T] = []

    def value() -> T:
        if not cache:
            cache.append(read())
        return cache[0]

    return value


class PennantPostseasonService:
    """ペナントの世界ひとつのポストシーズンの進行。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        games: GameRepository,
        fixtures: FixtureRepository,
        ratings: RatingsRepository,
        plans: ClubPlanRepository,
        context_query: SimulationContextQuery,
        brackets: PostseasonBracketReader,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._games = games
        self._fixtures = fixtures
        self._ratings = ratings
        self._plans = plans
        self._context_query = context_query
        self._brackets = brackets
        self._atomic = atomic

    def has_postseason_left(self) -> bool:
        """公式戦を終え、その年のポストシーズンがまだ終わっていないか（進める余地があるか）。

        公式戦の途中・締めた後・方式「なし」・終わったポストシーズンは False（管理コマンドの案内に使う）。
        """
        world = self._worlds.find_by_id(self._world_id)
        try:
            year = regular_season_over_year(
                last_played_on=self._context_query.last_played_on(),
                next_fixture_on=self._fixtures.first_date(),
                start_year=world.start_year,
            )
        except (SeasonNotFinished, AlreadyClosed):
            return False
        return not self._brackets.status(world, year).is_settled

    def advance(
        self, target: AdvanceTarget, *, expected_today: date | None | EllipsisType = ...
    ) -> PostseasonAdvanceReport:
        """ポストシーズンを `target` の分だけ進め、作った試合を保存する。

        - `DAY`: 次に試合がある1日（自軍が休みの日も、次の試合の日まで進む）
        - `NEXT_MANAGED_GAME`: 自軍が試合をした日まで。自軍が出ない（敗退した・出場していない）なら終わりまで
        - `POSTSEASON_END`: 終わり（優勝が決まる）まで

        どれも、試合数が `MAX_GAMES_PER_ADVANCE` を超える手前の日で止める（少なくとも1日は進める）。
        それ以外の範囲は `InvalidSchedule`。公式戦が終わっていなければ `SeasonNotFinished`、締めた後なら
        `AlreadyClosed`。方式が「なし」・終わったポストシーズンは何もせず空の結果を返す。

        `expected_today` を渡すと、世界の今日がそれと違うときは何も作らずに `AlreadyAdvanced`
        （画面の二重送信。管理コマンドは渡さない）。
        """
        if target not in POSTSEASON_SCREEN_TARGETS:
            raise InvalidSchedule("ポストシーズンでは選べない範囲です。")
        world = self._worlds.find_by_id(self._world_id)
        last_played = self._context_query.last_played_on()
        if expected_today is not ... and expected_today != last_played:
            raise AlreadyAdvanced("既に進んでいます。最新の状態を表示しました。")
        next_fixture = self._fixtures.first_date()
        year = regular_season_over_year(
            last_played_on=last_played, next_fixture_on=next_fixture, start_year=world.start_year
        )
        bracket = self._brackets.bracket(world, year)
        day = bracket.next_day()
        if day is None:
            return self._report(bracket, (), [], last_played, ())

        simulator = DaySimulator.load(
            world=world,
            first_day=day.played_on,
            context_query=self._context_query,
            ratings=self._ratings,
            plans=self._plans,
        )
        games: list[Game] = []
        dates: list[date] = []
        while day is not None:
            if dates and len(games) + len(day.games) > MAX_GAMES_PER_ADVANCE:
                break
            played = [
                simulator.play(
                    day.played_on,
                    scheduled.home_team_id,
                    scheduled.away_team_id,
                    stage=GameStage.POSTSEASON,
                    max_innings=scheduled.max_innings,
                )
                for scheduled in day.games
            ]
            games.extend(played)
            dates.append(day.played_on)
            # 結果をメモリで足して、次の日の対戦を導く（導いた対戦と食い違えば InvalidPostseason）
            bracket = bracket.played(_results_of(played))
            if target is AdvanceTarget.DAY or (
                target is AdvanceTarget.NEXT_MANAGED_GAME and _involves(day.games, world.managed_team_id)
            ):
                break
            day = bracket.next_day()

        self._save(games, before=(last_played, next_fixture))
        return self._report(bracket, tuple(dates), games, dates[-1], simulator.notices)

    # --- 内部 ---

    def _save(self, games: list[Game], *, before: tuple[date | None, date | None]) -> None:
        """試合を保存する。検査と行の組み立てはトランザクションの外、中では状態の読み直しと書き込みだけ。"""
        write = self._games.prepare_add_all(games)
        with self._atomic():
            if (self._context_query.last_played_on(), self._fixtures.first_date()) != before:
                # 計算している間に、別のリクエストが進めた（締めた）。同じ日の試合を2つ作らない
                raise AlreadyAdvanced("計算している間に世界が進みました。最新の状態を表示しました。")
            write()

    def _report(
        self,
        bracket: PostseasonBracket,
        dates: tuple[date, ...],
        games: list[Game],
        today: date | None,
        notices: tuple[PlanNotice, ...],
    ) -> PostseasonAdvanceReport:
        champion = bracket.champion
        return PostseasonAdvanceReport(
            played_dates=dates,
            games=len(games),
            today=today,
            status=bracket.status,
            champion_team_id=champion,
            champion_name="" if champion is None else self._brackets.team_names().get(champion, ""),
            plan_notices=notices,
        )


def _results_of(games: Iterable[Game]) -> list[PostseasonResult]:
    return [
        PostseasonResult(
            played_on=game.played_on,
            home_team_id=game.home_team_id,
            away_team_id=game.away_team_id,
            winner_team_id=game.winner_team_id,
        )
        for game in games
    ]


def _involves(games: Iterable[ScheduledGame], team_id: int | None) -> bool:
    if team_id is None:
        return False
    return any(team_id in (game.home_team_id, game.away_team_id) for game in games)
