"""プレゼンテーション層へ渡す読み取り専用のデータ構造。

ドメインオブジェクトをテンプレートへ直接渡すと、画面側からドメインの状態を
変更できてしまう。表示に必要な値だけを持つ DTO に詰め替えて渡す。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TeamSummary:
    """チーム一覧の1行。集約を組み立てず、表示に必要な値だけを読み出す。"""

    id: int
    name: str
    city: str
    league_name: str
    player_count: int


@dataclass(frozen=True)
class Listing:
    """並べ替えた一覧と、実際に採用された並び順。

    URL の指定が不正だった場合は既定に落とすため、要求された値ではなく
    採用された値を返す。画面の見出しの矢印をこれに合わせる。
    """

    rows: list
    sort: str
    descending: bool


@dataclass(frozen=True)
class StandingRow:
    """順位表の1行。順位と勝率は勝敗から算出した結果。"""

    rank: int
    team_id: int
    team_name: str
    wins: int
    losses: int
    ties: int
    games_played: int
    winning_percentage: str
    games_behind: str


@dataclass(frozen=True)
class Standings:
    """指定シーズンの順位表。"""

    year: int
    rows: list[StandingRow]
    available_years: list[int]
    sort: str = 'rank'
    descending: bool = False


@dataclass(frozen=True)
class AdminOverview:
    """管理画面トップに出す概況。

    「いま何件あるか」と「手当てが必要なデータはどれか」に絞る。
    成績のランキングはサイト側のダッシュボードの役割なので、ここには置かない。
    """

    league_count: int
    team_count: int
    player_count: int
    batter_count: int
    pitcher_count: int
    # 手当てが必要なもの
    players_without_stats: int
    retired_count: int
    teams_without_players: int


@dataclass(frozen=True)
class RankingEntry:
    """ランキングの1行。"""

    rank: int
    player_id: int
    player_name: str
    team_id: int
    team_name: str
    value: str


@dataclass(frozen=True)
class Dashboard:
    """ホーム画面（ダッシュボード）に表示する内容。"""

    league_count: int
    team_count: int
    batter_count: int
    pitcher_count: int
    ops_leaders: list[RankingEntry]
    home_run_leaders: list[RankingEntry]
    era_leaders: list[RankingEntry]
    strikeout_leaders: list[RankingEntry]
    teams: list['TeamSummary']

    @property
    def player_count(self) -> int:
        return self.batter_count + self.pitcher_count


@dataclass(frozen=True)
class BatterRow:
    """野手成績の1行。"""

    id: int
    name: str
    number: int
    position: str
    at_bats: int
    hits: int
    doubles: int
    triples: int
    home_runs: int
    runs_batted_in: int
    batting_average: float
    on_base_percentage: float
    ops: float


@dataclass(frozen=True)
class PitcherRow:
    """投手成績の1行。"""

    id: int
    name: str
    number: int
    position: str
    innings_pitched: str
    wins: int
    losses: int
    strikeouts: int
    earned_run_average: float
    whip: float
    strikeouts_per_nine: float


@dataclass(frozen=True)
class PlayerDetail:
    """選手編集画面で使う詳細。"""

    id: int
    team_id: int
    name: str
    number: int
    position: str
    is_pitcher: bool
    # 打撃
    at_bats: int
    singles: int
    doubles: int
    triples: int
    home_runs: int
    runs_batted_in: int
    walks: int
    hit_by_pitch: int
    sacrifice_flies: int
    # 投球
    innings_pitched: str
    wins: int
    losses: int
    saves: int
    earned_runs: int
    strikeouts: int
    hits_allowed: int
    walks_allowed: int
    # 算出済みの指標（編集中の入力がどう効くかを画面で確認できるように）
    batting_average: float
    on_base_percentage: float
    slugging_percentage: float
    ops: float
    earned_run_average: float
    whip: float
    strikeouts_per_nine: float
