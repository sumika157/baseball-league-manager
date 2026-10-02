"""試合シミュレーションを回して、水準を確かめる。**DB には一切書かない。**

ペナントモードの試合シミュレーションエンジン（`myapp/domain/simulation/`）の確認と調整の道具。

- 既定: 能力50の2球団で N 試合を回し、1試合ぶんのボックススコアを文字で出して、水準の表を
  NPB の目標帯と並べて出す。基準値（`LeagueBaseline`）の調整に使う。
- `--season`: 能力を散らした12球団（2リーグ6球団ずつ）で、ペナントと同じ日程（`generate_schedule`。
  1球団143試合・全858試合）の1シーズンを回し、水準の表に加えて
  首位打者・本塁打王などのタイトルの水準を出す。β と能力の分布の調整に使う。

同じ `--seed` なら同じ結果になる。
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from myapp.domain.entities import Game
from myapp.domain.pennant.schedule import NPB_TEAMS_PER_LEAGUE, ScheduleRules, generate_schedule
from myapp.domain.simulation.engine import SimulatedGame, simulate_game
from myapp.domain.simulation.levels import LevelRow, LevelTally, SeasonTally
from myapp.domain.simulation.manager import ClubRoster, PitchingHistory, choose_active_roster
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.simulation.samples import average_club, spread_league

DEFAULT_GAMES = 2000
SAMPLE_YEAR = 2026
SEASON_OPENING = date(SAMPLE_YEAR, 3, 29)
# 外国人の登録枠（1軍に登録できる人数）。サンプルの選手は全員が日本人なので効かないが、選択の経路は通す
FOREIGN_ROSTER_LIMIT = 4
# `--season` の日程の規則。既定値は NPB と同じ（1球団143試合）
SEASON_RULES = ScheduleRules()


def format_box_score(simulated: SimulatedGame, home: ClubRoster, away: ClubRoster) -> str:
    """1試合を文字のボックススコアにする。ビジター（先攻）が先。"""
    game = simulated.game
    lines = [
        f"{game.played_on}  {away.name} {game.away_score} - {game.home_score} {home.name}"
        f"（{simulated.innings_played}回）",
        "",
        _line_score(game, home, away),
        "",
    ]
    for club in (away, home):
        lines.extend(_batting_table(simulated, club))
        lines.append("")
    for club in (away, home):
        lines.extend(_pitching_table(simulated, club))
        lines.append("")
    return "\n".join(lines).rstrip()


def _pad(text: str, width: int) -> str:
    """全角を2桁として右に空白を足す（日本語の表を揃える）。"""
    used = sum(2 if ord(char) > 0x7F else 1 for char in text)
    return text + " " * max(0, width - used)


def _line_score(game: Game, home: ClubRoster, away: ClubRoster) -> str:
    innings = game.line_score.innings
    header = _pad("", 14) + "".join(f"{i:>3}" for i in range(1, innings + 1)) + "   R   H   E"
    rows = [header]
    for club, runs, is_home in ((away, game.line_score.away, False), (home, game.line_score.home, True)):
        cells = "".join(f"{runs[i]:>3}" if i < len(runs) else "  x" for i in range(innings))
        hits = sum(1 for p in game.plate_appearances if p.is_bottom == is_home and p.result.is_hit)
        errors = sum(len(p.errors) for p in game.plate_appearances if p.is_bottom != is_home)
        rows.append(_pad(club.name, 14) + cells + f"{sum(runs):>4}{hits:>4}{errors:>4}")
    return "\n".join(rows)


def _batting_table(simulated: SimulatedGame, club: ClubRoster) -> list[str]:
    game = simulated.game
    rows = [f"【{club.name} 打撃】  打数 安打 打点 四球 三振"]
    for entry in game.batting_in_order():
        if entry.team_id != club.team_id:
            continue
        mark = "  " if entry.is_starter else "└ "
        position = entry.fielding_position.label if entry.fielding_position else "-"
        line = entry.line
        name = simulated.names.get(entry.player_id, str(entry.player_id))
        rows.append(
            f"{entry.batting_order} {mark}{position} {_pad(name, 22)}"
            f"{line.at_bats:>4}{line.hits:>5}{line.runs_batted_in:>5}{line.walks:>5}{line.strikeouts:>5}"
        )
    return rows


def _pitching_table(simulated: SimulatedGame, club: ClubRoster) -> list[str]:
    game = simulated.game
    rows = [f"【{club.name} 投手】  投球回 被安打 失点 自責 四球 三振"]
    team_pitchers = {p.player_id for p in club.pitchers}
    for entry in game.pitching_in_order():
        if entry.player_id not in team_pitchers:
            continue
        line = entry.line
        decision = "".join(
            label
            for label, count in (("勝", line.wins), ("敗", line.losses), ("S", line.saves), ("H", line.holds))
            if count
        )
        name = simulated.names.get(entry.player_id, str(entry.player_id))
        rows.append(
            f"{entry.appearance_order} {_pad(name, 22)}{line.innings!s:>6}{line.hits_allowed:>6}"
            f"{line.runs_allowed:>5}{line.earned_runs:>5}{line.walks_allowed:>5}{line.strikeouts:>5}  {decision}"
        )
    return rows


def format_levels(title: str, rows: list[LevelRow]) -> str:
    """水準の表。目標帯と並べ、帯の外には印を付ける。"""
    lines = [title]
    for row in rows:
        digits = 3 if row.target.high < 10 else 1
        mark = "  " if row.within else "! "
        lines.append(
            f"  {mark}{_pad(row.label, 28)}{row.value:>9.{digits}f}   （目標 {row.target.low:g}〜{row.target.high:g}）"
        )
    return "\n".join(lines)


class Command(BaseCommand):
    help = "試合シミュレーションを回して水準を確かめる（DB には書かない）"

    def add_arguments(self, parser):
        parser.add_argument("--games", type=int, default=DEFAULT_GAMES, help=f"回す試合数（既定 {DEFAULT_GAMES}）")
        parser.add_argument("--seed", type=int, default=1, help="乱数シード（再現用。既定 1）")
        parser.add_argument(
            "--season",
            action="store_true",
            help=(
                f"能力を散らした{2 * NPB_TEAMS_PER_LEAGUE}球団で1シーズン"
                f"（1球団{SEASON_RULES.games_per_team(NPB_TEAMS_PER_LEAGUE)}試合）を回し、"
                "タイトルの水準も出す（--games は無視）"
            ),
        )

    def handle(self, *args, **options):
        seed = options["seed"]
        if options["season"]:
            self._run_season(seed)
            return
        games = options["games"]
        if games < 1:
            raise CommandError("--games は1以上を指定してください。")
        self._run_average(seed, games)

    def _say(self, message=""):
        self.stdout.write(message)

    def _run_average(self, seed: int, games: int) -> None:
        clubs = [
            choose_active_roster(average_club(team_id, f"平均{team_id}"), FOREIGN_ROSTER_LIMIT) for team_id in (1, 2)
        ]
        history = PitchingHistory()
        tally = LevelTally()
        first: SimulatedGame | None = None
        started = time.perf_counter()
        for index in range(games):
            # ホームとビジターを入れ替えて、1日1試合ずつ進める（先発の登板間隔を空けるため）
            home, away = (clubs[0], clubs[1]) if index % 2 == 0 else (clubs[1], clubs[0])
            rng = make_random(game_seed(seed, SAMPLE_YEAR, f"average-{index}"))
            played = simulate_game(rng, home, away, played_on=SEASON_OPENING + timedelta(days=index), history=history)
            tally.add(played.game)
            if first is None:
                first = played
                self._say(format_box_score(played, home, away))
                self._say()
        elapsed = time.perf_counter() - started
        self._say(f"{games}試合（能力50の2球団・シード{seed}）  {elapsed / games * 1000:.1f} ms/試合")
        self._say(format_levels("リーグ全体の水準:", tally.rows()))

    def _run_season(self, seed: int) -> None:
        rng = make_random(game_seed(seed, SAMPLE_YEAR, "league"))
        clubs = spread_league(rng, 2 * NPB_TEAMS_PER_LEAGUE)
        rosters = {club.team_id: choose_active_roster(club, FOREIGN_ROSTER_LIMIT) for club in clubs}
        team_ids = list(rosters)
        leagues = {1: team_ids[:NPB_TEAMS_PER_LEAGUE], 2: team_ids[NPB_TEAMS_PER_LEAGUE:]}
        schedule_rng = make_random(game_seed(seed, SAMPLE_YEAR, "schedule"))
        fixtures = generate_schedule(leagues, SEASON_RULES, SEASON_OPENING, schedule_rng)
        history = PitchingHistory()
        level = LevelTally()
        titles = SeasonTally(team_games=SEASON_RULES.games_per_team(NPB_TEAMS_PER_LEAGUE))
        started = time.perf_counter()
        for fixture in fixtures:
            game_rng = make_random(game_seed(seed, SAMPLE_YEAR, f"season-{fixture.date}-{fixture.home_team_id}"))
            played = simulate_game(
                game_rng,
                rosters[fixture.home_team_id],
                rosters[fixture.visitor_team_id],
                played_on=fixture.date,
                history=history,
            )
            level.add(played.game)
            titles.add(played.game)
        count = len(fixtures)
        elapsed = time.perf_counter() - started
        per_game = elapsed / count * 1000
        self._say(f"1シーズン {count}試合（能力を散らした{len(rosters)}球団・シード{seed}）  {per_game:.1f} ms/試合")
        self._say(format_levels("リーグ全体の水準:", level.rows()))
        self._say()
        self._say(format_levels("タイトル争いの水準:", titles.rows()))
