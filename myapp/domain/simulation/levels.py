"""リーグの水準を数える。NPB の目標帯と並べて、シミュレーション（と仮想データの投入）の現実味を見る。

数えるのは1試合ずつの `Game`（成績の揃った集約）から。率は試合ごとの率を平均せず、
合算した実数から計算し直す。

- `LevelTally`: リーグ全体の水準（得点・打率・防御率・K/9 など）
- `SeasonTally`: 選手ごとの1シーズンの合計から、首位打者・本塁打王などのタイトルの水準
- `UsageTally`: 投手の起用と疲労（中4日以下の先発・3連投・疲労のある打席など）。
  目標帯は付けない（#97 の前後を並べる道具）
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from ..entities import Game
from ..pennant.schedule import ScheduleRules
from ..services.rankings import QUALIFYING_INNINGS_PER_GAME, QUALIFYING_PLATE_APPEARANCES_PER_GAME
from ..value_objects import BattingLine, InningsPitched, PitchingLine
from .fatigue import FatigueLevel, in_game_fatigue_points, starter_rest_points

if TYPE_CHECKING:
    # 型注釈だけに使う（エンジンへの実行時の依存を作らない）
    from .engine import SimulatedGame

OUTS_PER_INNING = InningsPitched.OUTS_PER_INNING
# 1シーズンの試合数（NPB）。出典は日程の規則。引分の目標帯は「1チームが年に何試合引き分けるか」で持つ
SEASON_GAMES = ScheduleRules().games_per_team


@dataclass(frozen=True)
class LevelTarget:
    """目標帯。low 以上 high 以下なら帯の内側。"""

    low: float
    high: float

    @property
    def center(self) -> float:
        return (self.low + self.high) / 2

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high


@dataclass(frozen=True)
class LevelRow:
    """水準の1行。"""

    key: str
    label: str
    value: float
    target: LevelTarget

    @property
    def within(self) -> bool:
        return self.target.contains(self.value)


# リーグ水準の目標帯（NPB の近年の水準。1チーム1試合あたりで見るものが多い）
LEVEL_TARGETS: dict[str, tuple[str, LevelTarget]] = {
    "runs": ("1チーム1試合の得点", LevelTarget(3.8, 4.0)),
    "batting_average": ("打率", LevelTarget(0.250, 0.260)),
    "era": ("防御率", LevelTarget(3.40, 3.60)),
    "strikeouts_per_9": ("K/9", LevelTarget(7.0, 8.0)),
    "walks_per_9": ("BB/9", LevelTarget(2.5, 3.0)),
    "home_runs_per_9": ("HR/9", LevelTarget(0.8, 1.0)),
    "errors": ("1試合の失策（両チーム）", LevelTarget(1.1, 1.3)),
    "sacrifice_bunts": ("犠打（1チーム1試合）", LevelTarget(0.5, 0.7)),
    "stolen_bases": ("盗塁（1チーム1試合）", LevelTarget(0.45, 0.65)),
    "caught_stealing": ("盗塁刺（1チーム1試合）", LevelTarget(0.15, 0.25)),
    "double_plays": ("併殺打（1チーム1試合）", LevelTarget(0.6, 0.8)),
    "runs_allowed": ("失点（1チーム1試合）", LevelTarget(3.8, 4.0)),
    "ties": ("引分（1チーム年あたり）", LevelTarget(2.0, 5.0)),
}

# タイトル争いの目標帯
TITLE_TARGETS: dict[str, tuple[str, LevelTarget]] = {
    "batting_title": ("首位打者の打率", LevelTarget(0.320, 0.350)),
    "home_run_title": ("本塁打王の本数", LevelTarget(30, 45)),
    "save_title": ("最多セーブ", LevelTarget(30, 40)),
    "innings_leader": ("最多投球回", LevelTarget(160, 180)),
    "era_title": ("最優秀防御率", LevelTarget(1.8, 2.3)),
}


class LevelTally:
    """試合を足し込んで、リーグ全体の水準を数える。"""

    def __init__(self) -> None:
        self.games = 0
        self.ties = 0
        self.plate_appearances = 0
        self.advances = 0
        self.errors = 0
        self.runs = 0
        self.runs_allowed = 0
        self.at_bats = 0
        self.hits = 0
        self.home_runs = 0
        self.walks = 0
        self.sacrifice_bunts = 0
        self.stolen_bases = 0
        self.caught_stealing = 0
        self.double_plays = 0
        self.outs = 0
        self.earned_runs = 0
        self.strikeouts = 0

    def add(self, game: Game) -> None:
        """1試合ぶんを足す。"""
        self.games += 1
        self.ties += 1 if game.is_tie else 0
        self.runs += game.home_score + game.away_score
        self.plate_appearances += len(game.plate_appearances)
        self.advances += sum(len(entry.advances) for entry in game.plate_appearances)
        self.errors += sum(len(entry.errors) for entry in game.plate_appearances)
        for batting in game.batting:
            line = batting.line
            self.at_bats += line.at_bats
            self.hits += line.hits
            self.home_runs += line.home_runs
            self.walks += line.walks
            self.sacrifice_bunts += line.sacrifice_bunts
            self.stolen_bases += line.stolen_bases
            self.caught_stealing += line.caught_stealing
            self.double_plays += line.double_plays
        for pitching in game.pitching:
            pitched = pitching.line
            self.outs += pitched.innings.outs
            self.earned_runs += pitched.earned_runs
            self.runs_allowed += pitched.runs_allowed
            self.strikeouts += pitched.strikeouts

    def rows(self) -> list[LevelRow]:
        """目標帯と並べた水準の表。試合が無ければ 0 で返す。"""
        games = max(1, self.games)
        team_games = games * 2
        at_bats = max(1, self.at_bats)
        innings = max(1.0, self.outs / OUTS_PER_INNING)
        values = {
            "runs": self.runs / team_games,
            "batting_average": self.hits / at_bats,
            "era": self.earned_runs * 9 / innings,
            "strikeouts_per_9": self.strikeouts * 9 / innings,
            "walks_per_9": self.walks * 9 / innings,
            "home_runs_per_9": self.home_runs * 9 / innings,
            "errors": self.errors / games,
            "sacrifice_bunts": self.sacrifice_bunts / team_games,
            "stolen_bases": self.stolen_bases / team_games,
            "caught_stealing": self.caught_stealing / team_games,
            "double_plays": self.double_plays / team_games,
            "runs_allowed": self.runs_allowed / team_games,
            # 引分は試合ごとの割合。1チームが1シーズン（SEASON_GAMES 試合）で何回引き分けるかに直す
            "ties": self.ties / games * SEASON_GAMES,
        }
        return [LevelRow(key, LEVEL_TARGETS[key][0], value, LEVEL_TARGETS[key][1]) for key, value in values.items()]


class SeasonTally:
    """選手ごとの合計を持ち、タイトル争いの水準（首位打者・本塁打王など）を数える。"""

    def __init__(self, team_games: int = SEASON_GAMES) -> None:
        self.team_games = team_games
        self.batting: dict[int, BattingLine] = {}
        self.pitching: dict[int, PitchingLine] = {}

    def add(self, game: Game) -> None:
        for batting in game.batting:
            self.batting[batting.player_id] = self.batting.get(batting.player_id, BattingLine()) + batting.line
        for pitching in game.pitching:
            self.pitching[pitching.player_id] = self.pitching.get(pitching.player_id, PitchingLine()) + pitching.line

    def add_all(self, games: Iterable[Game]) -> None:
        for game in games:
            self.add(game)

    def rows(self) -> list[LevelRow]:
        """タイトル争いの水準。規定打席・規定投球回に達した選手を対象にする。"""
        required_pa = self.team_games * QUALIFYING_PLATE_APPEARANCES_PER_GAME
        required_outs = self.team_games * QUALIFYING_INNINGS_PER_GAME * OUTS_PER_INNING
        qualified_batters = [
            line for line in self.batting.values() if line.plate_appearances >= required_pa and line.at_bats > 0
        ]
        qualified_pitchers = [line for line in self.pitching.values() if line.innings.outs >= required_outs]
        values = {
            "batting_title": max((line.batting_average for line in qualified_batters), default=0.0),
            "home_run_title": float(max((line.home_runs for line in self.batting.values()), default=0)),
            "save_title": float(max((line.saves for line in self.pitching.values()), default=0)),
            "innings_leader": max((line.innings.as_innings for line in self.pitching.values()), default=0.0),
            "era_title": min((line.earned_run_average for line in qualified_pitchers), default=0.0),
        }
        return [LevelRow(key, TITLE_TARGETS[key][0], value, TITLE_TARGETS[key][1]) for key, value in values.items()]


@dataclass(frozen=True)
class UsageRow:
    """起用の1行。目標帯は持たない（前後を並べて読む）。"""

    key: str
    label: str
    value: float


USAGE_LABELS: dict[str, str] = {
    "short_rest_starts": "中4日以下の先発の割合",
    "starter_innings": "先発の平均投球回",
    "relief_appearances": "救援の登板（1チーム1試合）",
    "most_relief_appearances": "救援の最多登板（1人）",
    "three_day_streaks": "3連投（延べ）",
    "light_fatigue": "疲労が軽い打席の割合",
    "heavy_fatigue": "疲労が重い打席の割合",
    "overrun_batters": "目安を超えた続投の打者（1試合）",
}


class UsageTally:
    """投手の起用と疲労を数える（#97）。シミュレーションの出力（`SimulatedGame`）から数え、保存はしない。

    AI だけの世界では、中4日以下の先発・疲労のある打席・目安を超えた続投がほぼ0になるはず
    （疲労は酷使にだけ効かせる）。手動の編成で酷使したときにどれだけ起きるかを見る。
    """

    def __init__(self) -> None:
        self.games = 0
        self.starts = 0
        self.short_rest_starts = 0
        self.start_outs = 0
        self.relief_appearances = 0
        self.relief_by_pitcher: dict[int, int] = {}
        self.pitched_days: dict[int, set[date]] = {}
        self.plate_appearances = 0
        self.light = 0
        self.heavy = 0
        self.overrun_batters = 0

    def add(self, simulated: SimulatedGame) -> None:
        game = simulated.game
        self.games += 1
        outs = {pitching.player_id: pitching.line.innings.outs for pitching in game.pitching}
        for outing in simulated.outings:
            self.pitched_days.setdefault(outing.pitcher_id, set()).add(game.played_on)
            # 目安を超えた続投の打者数 = 最後の打者（faced − 1 人と対戦した後）の試合中の疲労点（1人ごとに1点）
            self.overrun_batters += in_game_fatigue_points(outing.faced - 1, outing.planned_target)
            if outing.is_starter:
                self.starts += 1
                self.start_outs += outs.get(outing.pitcher_id, 0)
                # 中4日以下 = 休養の疲労点が付く先発
                if starter_rest_points(outing.workload.days_since_start) > 0:
                    self.short_rest_starts += 1
            else:
                self.relief_appearances += 1
                self.relief_by_pitcher[outing.pitcher_id] = self.relief_by_pitcher.get(outing.pitcher_id, 0) + 1
        for points in simulated.pitcher_fatigue:
            self.plate_appearances += 1
            level = FatigueLevel.from_points(points)
            self.light += level is FatigueLevel.LIGHT
            self.heavy += level is FatigueLevel.HEAVY

    @property
    def three_day_streaks(self) -> int:
        """3日続けて投げた登板の延べ数（3日目を1つと数える。4連投なら2）。"""
        one_day = timedelta(days=1)
        return sum(
            1
            for days in self.pitched_days.values()
            for day in days
            if day - one_day in days and day - 2 * one_day in days
        )

    def rows(self) -> list[UsageRow]:
        team_games = max(1, self.games * 2)
        starts = max(1, self.starts)
        plate_appearances = max(1, self.plate_appearances)
        values = {
            "short_rest_starts": self.short_rest_starts / starts,
            "starter_innings": self.start_outs / starts / OUTS_PER_INNING,
            "relief_appearances": self.relief_appearances / team_games,
            "most_relief_appearances": float(max(self.relief_by_pitcher.values(), default=0)),
            "three_day_streaks": float(self.three_day_streaks),
            "light_fatigue": self.light / plate_appearances,
            "heavy_fatigue": self.heavy / plate_appearances,
            "overrun_batters": self.overrun_batters / max(1, self.games),
        }
        return [UsageRow(key, USAGE_LABELS[key], value) for key, value in values.items()]
