"""ペナントの世界のポストシーズンを読む（勝ち上がりの表と、GM ホームの「ポストシーズン」のカード）。

読むだけで、書き込みはしない。進める処理（`PennantPostseasonService`）とは分ける（`OffseasonViewService` の前例）。

- **勝ち上がりは保存しない。** `PostseasonBracketReader` が、その年の順位（公式戦だけ）と試合の結果から導く
- その年の試合は `PostseasonQuery.games` で1回だけ読み、表の試合の行と導出の両方に使う
- 同じサービスの中では、年ごとに1回だけ導く（世界の終了の判定 `status` とカード・表が共有する。勝ち上がりは
  不変の値なので覚えてよい。サービスは要求ごとに組み立てるので、要求をまたいでは覚えない）
- 予定日は `next_day()` からだけ出す（`Series.date_of`・`scheduled_last_day` は年をまたぐ日付を返しうるので
  画面に出さない）
"""

from __future__ import annotations

from datetime import date

from ..domain import services as domain_services
from ..domain.exceptions import InvalidPostseason
from ..domain.pennant.postseason import (
    PostseasonBracket,
    PostseasonDay,
    PostseasonFormat,
    PostseasonStatus,
    Series,
    SeriesRules,
)
from ..domain.pennant.season import SeasonPhase
from ..domain.pennant.world import World
from ..domain.repositories import WorldRepository
from .dto import (
    LeaguePlayerRow,
    Listing,
    PostseasonCard,
    PostseasonGameRow,
    PostseasonMatchup,
    PostseasonRound,
    PostseasonStats,
    PostseasonView,
    SeriesGameView,
    SeriesView,
    WorldContext,
)
from .pennant_home import postseason_advance_options
from .pennant_postseason import PostseasonBracketReader
from .pennant_race import StandingsSource
from .player_stats_view import played_counts, stats_player
from .queries import PostseasonQuery, PostseasonStatsQuery
from .stat_rows import to_batter_row, to_pitcher_row

# 自軍がまだ決まっていないときの結果の名前（始まる前は「出場」、始まった後は「勝ち残り」）
_QUALIFIED = "出場"
_ALIVE = "勝ち残り"

# 試合を引く鍵（日付・ホーム・ビジター。同じ日に同じ2球団の試合は1つしかない）
type _GameKey = tuple[date, int, int]


class PennantPostseasonViewService:
    """ポストシーズンの表とカードの材料を作る。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。

    `teams` は世界の範囲の順位表の読み取り（球団とリーグの名前に使う。勝ち上がりの導出と同じもの）。
    """

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        teams: StandingsSource,
        postseason: PostseasonQuery,
        brackets: PostseasonBracketReader,
        stats: PostseasonStatsQuery,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._teams = teams
        self._postseason = postseason
        self._brackets = brackets
        self._stats = stats
        self._world_cache: list[World] = []
        self._derived: dict[int, tuple[PostseasonBracket, list[PostseasonGameRow]]] = {}

    # --- 状況 ---

    def status(self, year: int) -> PostseasonStatus:
        """その年のポストシーズンの状況（世界の終了の判定に使う）。方式が「なし」なら何も読まずに `NONE`。"""
        if self._world().postseason_format is PostseasonFormat.NONE:
            return PostseasonStatus.NONE
        return self._derive(year)[0].status

    # --- 勝ち上がりの表 ---

    def years(self, world: WorldContext) -> list[int]:
        """表を開ける年（新しい順）。ポストシーズンの試合のある年と、公式戦を終えたいまの年（方式が「なし」でなければ）。"""
        years = set(self._postseason.years())
        if world.phase is SeasonPhase.FINISHED and world.has_postseason:
            years.add(world.season_year)
        return sorted(years, reverse=True)

    def view(self, world: WorldContext, year: int | None) -> PostseasonView | None:
        """Y年の勝ち上がりの表。開けない年（`years` に無い・対戦が組めない）なら None。

        `year` を省くと、開ける年のうち最も新しい年（無ければ None）。
        """
        years = self.years(world)
        if year is None and years:
            year = years[0]
        if year is None or year not in years:
            return None
        bracket, games = self._derive(year)
        if bracket.status is PostseasonStatus.NONE:
            return None
        names, leagues = self._names()
        by_key = _games_by_key(games)
        next_day = _next_day(bracket)
        return PostseasonView(
            year=year,
            status=bracket.status,
            format_label=bracket.postseason_format.label,
            champion_label=bracket.champion_label,
            champion_team_id=bracket.champion,
            champion_name=_name(names, bracket.champion),
            rounds=tuple(
                PostseasonRound(
                    name=bracket.plan.stage_name(index),
                    series=tuple(_series_view(series, by_key, names, leagues) for series in round_),
                )
                for index, round_ in enumerate(bracket.rounds)
            ),
            pending_stage_names=tuple(bracket.pending_stage_names()),
            next_day=next_day.played_on if next_day is not None else None,
            next_games=_matchups(next_day, names),
            own_outcome=_own_outcome(bracket, world.managed_team_id),
            years=tuple(years),
        )

    # --- 個人成績の表 ---

    def stats(
        self,
        world: WorldContext,
        year: int,
        *,
        pitchers: bool,
        sort: str | None = None,
        descending: bool | None = None,
    ) -> PostseasonStats | None:
        """Y年のポストシーズンの個人成績（出場した選手だけ）。開けない年（`years` に無い）なら None。

        並べ替えの規則は domain（キーは公式戦の成績と同じ、既定は打撃が安打・投手が投球回）。不正なキーは既定に落ちる。
        率は試合ごとの率を平均せず、合算した実数から計算し直す（成績の値オブジェクトが出典）。
        """
        years = self.years(world)
        if year not in years:
            return None
        rows = [
            row
            for row in self._stats.list_players(year)
            if row.position.is_pitcher == pitchers and (row.appearances if pitchers else row.games)
        ]
        played = played_counts(rows, pitchers=pitchers)
        members = [stats_player(row) for row in rows]
        home_of = {row.player_id: (row.team_id, row.team_name) for row in rows}
        if pitchers:
            ordered, key, desc = domain_services.sort_pitchers(
                members, sort, descending, domain_services.DEFAULT_POSTSEASON_PITCHER_SORT, appearances=played
            )
        else:
            ordered, key, desc = domain_services.sort_batters(
                members, sort, descending, domain_services.DEFAULT_POSTSEASON_BATTER_SORT, games=played
            )
        to_row = to_pitcher_row if pitchers else to_batter_row
        listed = []
        for player in ordered:
            player_id = player.id or 0
            team_id, team_name = home_of[player_id]
            # 年齢は出さない（プロフィールを読まない）ので、基準日はその年の終わりでよい
            row = to_row(player, played=played[player_id], today=date(year, 12, 31))
            listed.append(LeaguePlayerRow(team_id=team_id, team_name=team_name, player=row))
        return PostseasonStats(
            year=year, pitchers=pitchers, listing=Listing(rows=listed, sort=key, descending=desc), years=tuple(years)
        )

    # --- GM ホームのカード ---

    def card(self, world: WorldContext, *, include_advance: bool) -> PostseasonCard | None:
        """GM ホームの「ポストシーズン」のカード。公式戦を終えた年で、ポストシーズンがあるときだけ（ほかは None）。

        `include_advance` は「進める」のボタンを作るか（オーナーにだけ出す）。
        """
        if world.phase is not SeasonPhase.FINISHED or not world.has_postseason:
            return None
        bracket, games = self._derive(world.season_year)
        if bracket.status is PostseasonStatus.NONE:
            return None
        names, leagues = self._names()
        own_id = world.managed_team_id
        own_series = bracket.series_of(own_id) if own_id is not None else []
        next_day = _next_day(bracket)
        finished = bracket.status is PostseasonStatus.FINISHED
        own_alive = own_id is not None and bracket.outcome_of(own_id) is None
        return PostseasonCard(
            year=world.season_year,
            status=bracket.status,
            champion_label=bracket.champion_label,
            champion_team_id=bracket.champion,
            champion_name=_name(names, bracket.champion),
            own_outcome=_own_outcome(bracket, own_id),
            own_series=_series_view(own_series[-1], _games_by_key(games), names, leagues) if own_series else None,
            next_day=next_day.played_on if next_day is not None else None,
            next_games=_matchups(next_day, names),
            advance_options=(
                tuple(postseason_advance_options(own_alive=own_alive)) if include_advance and not finished else ()
            ),
            expected_today=world.today.isoformat() if world.today is not None else "",
        )

    # --- 内部 ---

    def _world(self) -> World:
        if not self._world_cache:
            self._world_cache.append(self._worlds.find_by_id(self._world_id))
        return self._world_cache[0]

    def _derive(self, year: int) -> tuple[PostseasonBracket, list[PostseasonGameRow]]:
        """その年の勝ち上がりと試合。試合は1回だけ読み、導出にも渡す。"""
        if year not in self._derived:
            games = self._postseason.games(year)
            bracket = self._brackets.bracket(self._world(), year, results=[game.result for game in games])
            self._derived[year] = (bracket, games)
        return self._derived[year]

    def _names(self) -> tuple[dict[int, str], dict[int, str]]:
        """球団の名前とリーグの名前。"""
        rows = self._teams.list_teams().rows
        return (
            {team.id: team.name for team in rows},
            {team.league_id: team.league_name for team in rows},
        )


def _name(names: dict[int, str], team_id: int | None) -> str:
    return "" if team_id is None else names.get(team_id, "")


def _next_day(bracket: PostseasonBracket) -> PostseasonDay | None:
    """次に試合をする日。年をまたぐ日付を導いたら（進める処理が止まる状態）、表には出さない。"""
    try:
        return bracket.next_day()
    except InvalidPostseason:
        return None


def _matchups(day: PostseasonDay | None, names: dict[int, str]) -> tuple[PostseasonMatchup, ...]:
    if day is None:
        return ()
    return tuple(
        PostseasonMatchup(
            series_name=game.series.name,
            number=game.number,
            home_team_name=_name(names, game.home_team_id),
            away_team_name=_name(names, game.away_team_id),
        )
        for game in day.games
    )


def _own_outcome(bracket: PostseasonBracket, own_id: int | None) -> str:
    """自軍の結果の名前。決まっていなければ、始まる前は「出場」、始まった後は「勝ち残り」。"""
    if own_id is None:
        return ""
    outcome = bracket.outcome_of(own_id)
    if outcome is not None:
        return outcome.label
    return _QUALIFIED if bracket.status is PostseasonStatus.NOT_STARTED else _ALIVE


def _games_by_key(games: list[PostseasonGameRow]) -> dict[_GameKey, PostseasonGameRow]:
    return {(game.played_on, game.home_team_id, game.away_team_id): game for game in games}


def _series_view(
    series: Series, games: dict[_GameKey, PostseasonGameRow], names: dict[int, str], leagues: dict[int, str]
) -> SeriesView:
    rows = []
    for number, result in enumerate(series.games, start=1):
        game = games[(result.played_on, result.home_team_id, result.away_team_id)]
        rows.append(
            SeriesGameView(
                number=number,
                game_id=game.game_id,
                played_on=game.played_on,
                home_team_name=_name(names, game.home_team_id),
                away_team_name=_name(names, game.away_team_id),
                home_score=game.home_score,
                away_score=game.away_score,
                winner_name=_name(names, result.winner_team_id),
            )
        )
    return SeriesView(
        name=series.name,
        league_name="" if series.league_id is None else leagues.get(series.league_id, ""),
        upper_team_id=series.upper_team_id,
        upper_team_name=_name(names, series.upper_team_id),
        lower_team_id=series.lower_team_id,
        lower_team_name=_name(names, series.lower_team_id),
        upper_wins=series.upper_wins,
        lower_wins=series.lower_wins,
        ties=series.ties,
        advantage=series.rules.advantage,
        rules_label=_rules_label(series.rules),
        winner_team_id=series.winner,
        winner_name=_name(names, series.winner),
        games=tuple(rows),
    )


def _rules_label(rules: SeriesRules) -> str:
    """シリーズの規則の1行（値の出典は domain の `SeriesRules`）。"""
    parts = [f"先に{rules.wins_needed}勝"]
    if rules.advantage:
        parts[0] += f"（上位はアドバンテージの{rules.advantage}勝を含む）"
    if rules.max_games is not None:
        parts.append(f"{rules.max_games}試合まで")
    if rules.unlimited_from is not None:
        parts.append(f"第{rules.unlimited_from}戦からは延長の制限なし")
    return "・".join(parts)
