"""リーグの水準を数える。NPB の目標帯と並べて、シミュレーション（と仮想データの投入）の現実味を見る。

数えるのは1試合ずつの `Game`（成績の揃った集約）から。率は試合ごとの率を平均せず、
合算した実数から計算し直す。

- `LevelTally`: リーグ全体の水準（得点・打率・防御率・K/9 など）
- `SeasonTally`: 選手ごとの1シーズンの合計から、首位打者・本塁打王などのタイトルの水準
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..entities import Game
from ..services.rankings import QUALIFYING_INNINGS_PER_GAME, QUALIFYING_PLATE_APPEARANCES_PER_GAME
from ..value_objects import BattingLine, InningsPitched, PitchingLine

OUTS_PER_INNING = InningsPitched.OUTS_PER_INNING
# 1シーズンの試合数（NPB）。引分の目標帯は「1チームが年に何試合引き分けるか」で持つ
SEASON_GAMES = 143


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
            # 引分は試合ごとの割合。1チームが143試合で何回引き分けるかに直す
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
