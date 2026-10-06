"""ペナントの GM ホームの材料を作る。世界の「いま」を読むだけで、書き込みはしない。

材料は次の3つに分けて読み、**世界の全試合を集約として組み立てない**（`.claude/rules/performance.md`）。

- 順位・主力・タイトル: 世界の範囲で組み立てた `TeamApplicationService`（既存の参照）
- 自軍の試合・期間の試合: `GameListQuery`（試合の一覧の SQL。明細は読まない）
- 責任投手・本塁打・期間の成績: `PennantActivityQuery`（明細の SQL 集計）

「進める」の選択肢（終わる日付と試合数）は未消化の対戦から導く（`dates_to_play`）。
状態（今日・局面・連続・順位の変動）は保存せず、毎回、日程と試合から導く。
"""

from __future__ import annotations

from datetime import date, timedelta

from ..domain.pennant.form import Outcome, outcome_for, recent_form
from ..domain.pennant.schedule import AdvanceTarget, Fixture, ScheduleRules, dates_to_play, default_opening_day
from ..domain.pennant.season import MAX_GAMES_PER_ADVANCE, SCREEN_ADVANCE_TARGETS, SeasonPhase, summary_period
from ..domain.pennant.season import stats_year as stats_year_of
from ..domain.repositories import FixtureRepository, GameRepository, LeagueRepository, TeamRepository
from ..domain.value_objects import InningsPitched, format_average
from .dto import (
    AdvanceOption,
    AdvanceSummary,
    FinaleSeason,
    GameRow,
    HomeStandings,
    KeyBatterRow,
    KeyPitcherRow,
    LeagueOption,
    LeagueStandings,
    OwnGameResult,
    OwnTeamSummary,
    PennantHome,
    PeriodBatter,
    PeriodBatting,
    PeriodPitching,
    StandingRow,
    TitleRaceRow,
    UpcomingGame,
    WorldContext,
    WorldDeletion,
    WorldFinale,
)
from .pennant_race import own_race, remaining_by_team, tiebreak_for
from .queries import GameListQuery, PennantActivityQuery
from .scorebook_view import build_line_score
from .services import TeamApplicationService
from .standings import league_standings

# 今後の日程に出す件数
UPCOMING_GAMES = 5
# 主力打者・投手陣・期間の活躍・タイトル争いに出す人数（部門）
KEY_PLAYERS = 5
PITCHING_STAFF = 7
# 直近の打率に数える日数
RECENT_DAYS = 15
# 「ほかの試合」に載せる件数の上限（残りは件数だけ出す）
MAX_OTHER_GAMES = 100

# ボタンの文言。どの範囲を出すかと順序は `SCREEN_ADVANCE_TARGETS`（domain）が出典で、ここは文言だけ持つ
_LABELS = {
    AdvanceTarget.DAY: "1日進める",
    AdvanceTarget.NEXT_MANAGED_GAME: "次の自軍の試合まで進める",
    AdvanceTarget.WEEK: "1週間進める",
    AdvanceTarget.MONTH_END: "月末まで進める",
    AdvanceTarget.LIMIT: "上限まで進める",
}


def _record_label(wins: int, losses: int, ties: int) -> str:
    """「80勝60敗3分」（引分が無ければ「80勝60敗」）。"""
    return f"{wins}勝{losses}敗" + (f"{ties}分" if ties else "")


def _saved_id(value: int | None) -> int:
    assert value is not None, "保存済みのものには id がある"
    return value


class PennantHomeService:
    """GM ホームと世界の削除の確認の材料を作る。`teams` は世界の範囲で組み立てたサービス。"""

    def __init__(
        self,
        *,
        teams: TeamApplicationService,
        games: GameListQuery,
        game_records: GameRepository,
        fixtures: FixtureRepository,
        activity: PennantActivityQuery,
        team_records: TeamRepository,
        leagues: LeagueRepository,
    ) -> None:
        self._teams = teams
        # 世界の終了の通算が、年ごとの順位を作るために読む（順位の規則は domain の `standings`）
        self._team_records = team_records
        self._leagues = leagues
        self._games = games
        # 自軍の1試合のスコアボードを作るときだけ、試合を集約として1つ読む
        self._game_records = game_records
        self._fixtures = fixtures
        self._activity = activity

    def get_home(
        self, world: WorldContext, *, since: date | None = None, league_id: int | None = None, include_advance: bool
    ) -> PennantHome:
        """GM ホームの材料。`since` は「進める前の今日」（URL の値なので信用しない）。

        `include_advance` は「進める」の選択肢を作るか（オーナーにしか出さないので、他の人には読まない）。
        受け持つ球団が無い世界でも開ける（自軍に関する区画が空になる）。
        """
        year = world.season_year
        pending = self._fixtures.find_all()
        own_id = world.managed_team_id
        own_games = self._games.list_rows(year=year, team_id=own_id) if own_id is not None else []
        # 締めた後の開幕前は、今季の成績がまだ無い。順位表・主力・タイトルは前年の最終のものを見せる
        stats_year = stats_year_of(phase=world.phase, last_played_on=world.today, current_year=world.season_year)
        standings = self._teams.get_league_standings(stats_year)
        team_rows = self._teams.list_teams().rows
        team_leagues = {team.id: (team.league_id, team.league_name) for team in team_rows}
        names = {team.id: team.name for team in team_rows}

        own_league_id = team_leagues[own_id][0] if own_id is not None and own_id in team_leagues else None
        own = self._own_summary(world, standings, own_games, pending, team_leagues, year, stats_year)
        return PennantHome(
            world=world,
            own=own,
            upcoming=self._upcoming(pending, own_id, names),
            advance_options=self._advance_options(world, pending) if include_advance else [],
            expected_today=world.today.isoformat() if world.today is not None else "",
            summary=self._summary(world, since, year, standings, own_games, names),
            standings=self._home_standings(standings, stats_year, own_league_id, league_id, world.default_league_id),
            batters=self._key_batters(world, stats_year) if own_id is not None else [],
            pitchers=self._key_pitchers(own_id, stats_year) if own_id is not None else [],
            titles=self._title_race(own_id, own_league_id, stats_year),
            stats_year=stats_year,
            shows_previous_season=stats_year != year,
            finale=self._finale(world, stats_year, standings),
        )

    def get_deletion(self, world: WorldContext) -> WorldDeletion:
        """世界の削除の確認に出す規模。試合数は両チームの1試合として数えた総数。"""
        return WorldDeletion(
            world=world,
            season_count=len(self._games.list_seasons()),
            game_count=sum(self._games.count_by_team().values()) // 2,
        )

    # --- 自軍の帯 ---

    def _own_summary(
        self,
        world: WorldContext,
        standings: list[LeagueStandings],
        own_games: list[GameRow],
        pending: list[Fixture],
        team_leagues: dict[int, tuple[int, str]],
        year: int,
        stats_year: int,
    ) -> OwnTeamSummary | None:
        own_id = world.managed_team_id
        if own_id is None or own_id not in team_leagues:
            return None
        row = _find_row(standings, own_id)
        # `standings` は stats_year の順位。前年のものなら、いまの年度の欄には使わず「前年の順位」に回す
        previous = row if stats_year != year else None
        current = None if stats_year != year else row
        form = recent_form([_outcome(own_id, game) for game in own_games if game.is_recorded])
        opening: date | None = None
        if world.phase is SeasonPhase.BEFORE_OPENING:
            opening = min((f.date for f in pending), default=None) or default_opening_day(year, ScheduleRules())
        return OwnTeamSummary(
            team_id=own_id,
            team_name=world.managed_team_name,
            league_name=team_leagues[own_id][1],
            phase=world.phase,
            rank=current.rank if current is not None else None,
            wins=current.wins if current is not None else 0,
            losses=current.losses if current is not None else 0,
            ties=current.ties if current is not None else 0,
            winning_percentage=current.winning_percentage if current is not None else "",
            games_behind=current.games_behind if current is not None else "",
            games_played=current.games_played if current is not None else 0,
            remaining_games=sum(1 for fixture in pending if fixture.involves(own_id)),
            last_ten=form.record if own_games else "",
            streak=form.streak_label,
            opening_date=opening,
            previous_year=stats_year if previous is not None else None,
            previous_rank=previous.rank if previous is not None else None,
            previous_record=(
                _record_label(previous.wins, previous.losses, previous.ties) if previous is not None else ""
            ),
            race=(
                own_race(
                    standings,
                    remaining_by_team(pending),
                    own_id,
                    tiebreak_for(year, games=self._games, teams=self._teams),
                )
                if world.phase is not SeasonPhase.BEFORE_OPENING
                else None
            ),
        )

    def _finale(self, world: WorldContext, latest_year: int, latest: list[LeagueStandings]) -> WorldFinale | None:
        """最終シーズンを終えた世界の、自軍の年度ごとの順位と通算。終えていない世界・受け持つ球団が無い世界は None。

        `latest` は最後の年（`latest_year`）の順位で、ホームが読んだものを使う（二重に読まない）。
        ほかの年は `_standings_by_year` が球団とリーグを1回だけ読み、年ごとに試合だけを読む。
        終えた世界だけが読む（最大 `MAX_SEASONS_PER_WORLD` 年ぶん）。
        優勝は、その年の最終順位で単独の首位か、同率なら規定（domain の `season_champion`）で決まった球団。
        自軍が載らない年（その年に試合の無い球団）は読み飛ばす。
        """
        own_id = world.managed_team_id
        if own_id is None or not world.is_over:
            return None
        years = sorted(self._games.list_seasons(recorded_only=True))
        by_year = self._standings_by_year([year for year in years if year != latest_year])
        by_year[latest_year] = latest
        seasons: list[FinaleSeason] = []
        wins = losses = ties = 0
        for year in years:
            standings = by_year[year]
            row = _find_row(standings, own_id)
            if row is None:
                continue
            race = own_race(standings, {}, own_id, tiebreak_for(year, games=self._games, teams=self._teams))
            seasons.append(
                FinaleSeason(
                    year=year,
                    rank=row.rank,
                    record=_record_label(row.wins, row.losses, row.ties),
                    is_champion=race is not None and race.clinched,
                )
            )
            wins += row.wins
            losses += row.losses
            ties += row.ties
        return WorldFinale(
            seasons=seasons,
            wins=wins,
            losses=losses,
            ties=ties,
            titles=sum(1 for s in seasons if s.is_champion),
            record=_record_label(wins, losses, ties),
        )

    def _standings_by_year(self, years: list[int]) -> dict[int, list[LeagueStandings]]:
        """複数の年のリーグ別の順位。球団とリーグは1回だけ読み、年ごとにその年の試合だけを読む。

        順位表の組み立ては `application/standings.py` の `league_standings`（順位の規則は domain の `standings`）。
        行の写しも `application/standings.py` の `standing_row_of`（`TeamApplicationService` と同じ関数）。
        """
        teams = self._team_records.find_all()
        leagues = self._leagues.find_all()
        result: dict[int, list[LeagueStandings]] = {}
        for year in years:
            games = self._games.list_for_standings(year=year)
            result[year] = league_standings(teams, leagues, games)
        return result

    # --- 次の試合・進める ---

    @staticmethod
    def _upcoming(pending: list[Fixture], own_id: int | None, names: dict[int, str]) -> list[UpcomingGame]:
        if own_id is None:
            return []
        mine = sorted((f for f in pending if f.involves(own_id)), key=lambda f: f.date)
        return [
            UpcomingGame(
                played_on=fixture.date,
                opponent_team_id=opponent,
                opponent_name=names.get(opponent, ""),
                is_home=fixture.home_team_id == own_id,
            )
            for fixture in mine[:UPCOMING_GAMES]
            for opponent in [fixture.visitor_team_id if fixture.home_team_id == own_id else fixture.home_team_id]
        ]

    @staticmethod
    def _advance_options(world: WorldContext, pending: list[Fixture]) -> list[AdvanceOption]:
        """進める選択肢。終わる日付と試合数は日程から導く。

        シーズンが終わっていれば無い。同じ範囲になる選択肢は1つにまとめ、1回の上限
        （`MAX_GAMES_PER_ADVANCE`）を超える範囲は出さない（押しても弾かれるだけの導線を見せない）。
        日程がまだ無い世界（作った直後にコマンドで作った世界など）は、最初の1日だけを出す
        （日程は最初に進めたときに作られる）。
        """
        if not pending:
            if world.today is None:
                return [
                    AdvanceOption(
                        target=AdvanceTarget.DAY.value, label=_LABELS[AdvanceTarget.DAY], end_date=None, games=None
                    )
                ]
            return []
        own_id = world.managed_team_id
        today = world.today if world.today is not None else date.min
        options: list[AdvanceOption] = []
        offered: list[list[date]] = []
        for target in SCREEN_ADVANCE_TARGETS:
            if target is AdvanceTarget.NEXT_MANAGED_GAME and (
                own_id is None or not any(f.involves(own_id) and f.date > today for f in pending)
            ):
                continue
            dates = dates_to_play(pending, today, target, own_id)
            if not dates or dates in offered:
                continue
            days = set(dates)
            games = sum(1 for fixture in pending if fixture.date in days)
            if games > MAX_GAMES_PER_ADVANCE:
                continue
            offered.append(dates)
            options.append(AdvanceOption(target=target.value, label=_LABELS[target], end_date=dates[-1], games=games))
        return options

    # --- 結果のまとめ ---

    def _summary(
        self,
        world: WorldContext,
        since: date | None,
        year: int,
        standings: list[LeagueStandings],
        own_games: list[GameRow],
        names: dict[int, str],
    ) -> AdvanceSummary | None:
        own_id = world.managed_team_id
        period = summary_period(since, world.today)
        if own_id is None or period is None:
            return None
        after, through = period
        in_period = [game for game in own_games if after < _day(game) <= through]
        results = self._own_results(own_id, in_period)
        outcomes = [result.outcome for result in results]
        before = _find_row(self._teams.get_league_standings(year, through=after), own_id)
        after_row = _find_row(standings, own_id)
        own_ids = {game.id for game in in_period}
        everyone = self._games.list_rows(year=year, after=after, through=through)
        others = [game for game in everyone if game.id not in own_ids]
        single = in_period[0] if len(in_period) == 1 else None
        return AdvanceSummary(
            since=after,
            until=through,
            wins=outcomes.count(Outcome.WIN.value),
            losses=outcomes.count(Outcome.LOSS.value),
            ties=outcomes.count(Outcome.TIE.value),
            rank_before=before.rank if before is not None else None,
            rank_after=after_row.rank if after_row is not None else None,
            games_behind_before=before.games_behind if before is not None else "",
            games_behind_after=after_row.games_behind if after_row is not None else "",
            own_games=results,
            line_score_game=single,
            line_score=(
                # 世界の試合は必ず打席から作るので、安打の補助（打席の無い古い記録用）は使わない
                build_line_score(self._game_records.find_by_id(single.id), lambda team_id: 0)
                if single is not None
                else None
            ),
            batters=self._period_batters(own_id, after, through),
            pitchers=self._period_pitchers(own_id, after, through),
            other_games=others[:MAX_OTHER_GAMES],
            other_games_omitted=max(len(others) - MAX_OTHER_GAMES, 0),
        )

    def _own_results(self, own_id: int, games: list[GameRow]) -> list[OwnGameResult]:
        notes = self._activity.game_notes([game.id for game in games])
        results = []
        for game in games:
            is_home = game.home_team_id == own_id
            note = notes.get(game.id)
            results.append(
                OwnGameResult(
                    game_id=game.id,
                    played_on=_day(game),
                    opponent_name=game.away_team_name if is_home else game.home_team_name,
                    is_home=is_home,
                    outcome=_outcome(own_id, game).value,
                    own_score=game.home_score if is_home else game.away_score,
                    opponent_score=game.away_score if is_home else game.home_score,
                    winning_pitcher=note.winning_pitcher if note else "",
                    losing_pitcher=note.losing_pitcher if note else "",
                    save_pitcher=note.save_pitcher if note else "",
                    home_runs=note.home_runs if note else (),
                )
            )
        # 新しい順で受け取るので、日付の古い順に並べ直して読ませる
        return sorted(results, key=lambda result: (result.played_on, result.game_id))

    def _period_batters(self, own_id: int, after: date, through: date) -> list[PeriodBatter]:
        rows = [
            row for row in self._activity.batting_between(own_id, after=after, through=through) if row.batting.at_bats
        ]
        rows.sort(
            key=lambda row: (-row.batting.hits, -row.batting.home_runs, -row.batting.runs_batted_in, row.player_id)
        )
        return [
            PeriodBatter(
                player_id=row.player_id,
                name=row.name,
                batting_average=format_average(row.batting.batting_average),
                hits=row.batting.hits,
                at_bats=row.batting.at_bats,
                home_runs=row.batting.home_runs,
                runs_batted_in=row.batting.runs_batted_in,
            )
            for row in rows[:KEY_PLAYERS]
        ]

    def _period_pitchers(self, own_id: int, after: date, through: date) -> list[PeriodPitching]:
        rows = [
            row
            for row in self._activity.pitching_between(own_id, after=after, through=through)
            if row.wins or row.saves
        ]
        rows.sort(key=lambda row: (-row.wins, -row.saves, row.player_id))
        return rows[:KEY_PLAYERS]

    # --- 順位表・主力・タイトル ---

    @staticmethod
    def _home_standings(
        standings: list[LeagueStandings],
        year: int,
        own_league_id: int | None,
        requested: int | None,
        default_league_id: int | None,
    ) -> HomeStandings:
        """自軍のリーグを先頭にし、選ばれたリーグを出す（無効な指定は自軍のリーグに落とす）。"""
        first = own_league_id if own_league_id is not None else default_league_id
        ordered = sorted(standings, key=lambda league: league.league_id != first)
        selected = next((league for league in ordered if league.league_id == requested), None)
        if selected is None and ordered:
            selected = ordered[0]
        return HomeStandings(
            year=year,
            leagues=[LeagueOption(id=league.league_id, name=league.league_name) for league in ordered],
            selected=selected,
        )

    def _key_batters(self, world: WorldContext, year: int) -> list[KeyBatterRow]:
        own_id = _saved_id(world.managed_team_id)
        rows = [
            row for row in self._teams.list_batters(own_id, sort="ops", descending=True, year=year).rows if row.at_bats
        ]
        recent: dict[int, PeriodBatting] = {}
        if world.today is not None:
            recent = {
                row.player_id: row
                for row in self._activity.batting_between(
                    own_id, after=world.today - timedelta(days=RECENT_DAYS), through=world.today
                )
            }
        return [
            KeyBatterRow(
                player_id=row.id,
                name=row.name,
                batting_average=row.batting_average,
                home_runs=row.home_runs,
                runs_batted_in=row.runs_batted_in,
                ops=row.ops,
                recent_average=_recent_average(recent.get(row.id)),
            )
            for row in rows[:KEY_PLAYERS]
        ]

    def _key_pitchers(self, own_id: int | None, year: int) -> list[KeyPitcherRow]:
        if own_id is None:
            return []
        rows = [
            row
            for row in self._teams.list_pitchers(own_id, sort="innings", descending=True, year=year).rows
            if row.starts or InningsPitched.from_notation(row.innings_pitched).outs
        ]
        return [
            KeyPitcherRow(
                player_id=row.id,
                name=row.name,
                wins=row.wins,
                losses=row.losses,
                saves=row.saves,
                innings_pitched=row.innings_pitched,
                earned_run_average=row.earned_run_average,
                starts=row.starts,
            )
            for row in rows[:PITCHING_STAFF]
        ]

    def _title_race(self, own_id: int | None, own_league_id: int | None, year: int) -> list[TitleRaceRow]:
        """自軍の選手が上位 `KEY_PLAYERS` 位以内にいる部門。"""
        if own_id is None or own_league_id is None:
            return []
        titles = self._teams.get_league_titles(own_league_id, year, leaders=KEY_PLAYERS)
        return [
            TitleRaceRow(
                department=department.label,
                rank=entry.rank,
                player_id=entry.player_id,
                player_name=entry.player_name,
                value=entry.value,
                leader_name=leader.player_name,
                leader_value=leader.value,
            )
            for department in titles.departments
            for leader in [department.leader]
            if leader is not None
            for entry in department.entries or []
            if entry.team_id == own_id
        ]


def _outcome(own_id: int, game: GameRow) -> Outcome:
    return outcome_for(own_id, game.home_team_id, game.away_team_id, game.home_score, game.away_score)


def _find_row(standings: list[LeagueStandings], team_id: int) -> StandingRow | None:
    for league in standings:
        for row in league.rows:
            if row.team_id == team_id:
                return row
    return None


def _recent_average(batting: PeriodBatting | None) -> str:
    if batting is None or not batting.batting.at_bats:
        return ""
    return format_average(batting.batting.batting_average)


def _day(game: GameRow) -> date:
    """試合日。`GameRow.played_on` は型が緩いので、日付であることをここで確かめる。"""
    assert isinstance(game.played_on, date), "試合日は日付"
    return game.played_on
