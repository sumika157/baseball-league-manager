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
    league_id: int
    league_name: str
    player_count: int
    # 本拠地球場。所在地は球場が持つので、チーム側に地名は持たない
    stadium_name: str = ''
    city: str = ''


@dataclass(frozen=True)
class GameRow:
    """試合一覧の1行。"""

    id: int
    year: int
    played_on: object
    home_team_id: int
    home_team_name: str
    away_team_id: int
    away_team_name: str
    home_score: int
    away_score: int
    result: str          # '引分' または '<チーム名> の勝ち'
    winner_team_id: int | None


@dataclass(frozen=True)
class GamePlayerRow:
    """試合詳細に並べる、1選手ぶんの成績。"""

    player_id: int
    player_name: str
    number: int
    team_id: int
    team_name: str
    # 打撃
    at_bats: int = 0
    hits: int = 0
    home_runs: int = 0
    runs_batted_in: int = 0
    walks: int = 0
    batting_average: float = 0.0
    # 投球
    innings_pitched: str = '0.0'
    earned_runs: int = 0
    strikeouts: int = 0
    hits_allowed: int = 0
    earned_run_average: float = 0.0


@dataclass(frozen=True)
class GameDetail:
    """試合詳細。"""

    game: GameRow
    batting: list[GamePlayerRow]
    pitching: list[GamePlayerRow]


@dataclass(frozen=True)
class PlayerGameRow:
    """選手個人ページの、試合ごとの成績1行。"""

    game_id: int
    played_on: object
    opponent_name: str
    result: str          # '勝' / '敗' / '分'
    # 打撃
    at_bats: int = 0
    hits: int = 0
    home_runs: int = 0
    runs_batted_in: int = 0
    # 投球
    innings_pitched: str = '0.0'
    earned_runs: int = 0
    strikeouts: int = 0


@dataclass(frozen=True)
class CareerRow:
    """経歴の1行。どのチームにいつ在籍したか。"""

    team_id: int
    team_name: str
    number: int
    from_year: int
    to_year: int | None
    is_current: bool

    @property
    def period(self) -> str:
        return f"{self.from_year}〜{self.to_year or '現在'}"


@dataclass(frozen=True)
class PlayerProfile:
    """選手個人ページ。プロフィール・経歴・通算成績・試合ごとの記録。"""

    detail: 'PlayerDetail'
    games: list[PlayerGameRow]
    career: list[CareerRow] = None
    # プロフィール
    age: int | None = None
    throws_bats: str = ''
    height_cm: int | None = None
    weight_kg: int | None = None
    birthplace: str = ''
    debut_year: int | None = None
    # プロ入り前の経歴。(区分, 名称) を通った順に並べたもの
    amateur_career: list = None
    has_profile: bool = False

    @property
    def appearances(self) -> int:
        return len(self.games)


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
class TeamTotals:
    """チームの合計成績と、そこから求めた指標。

    率は選手ごとの率を平均せず、合算した実数から計算し直したもの。
    """

    games: int
    # 打撃
    batting_average: float
    on_base_percentage: float
    slugging_percentage: float
    ops: float
    home_runs: int
    runs_batted_in: int
    # 投球
    earned_run_average: float
    whip: float
    strikeouts: int
    innings_pitched: str
    # タイトルの対象になる目安
    required_plate_appearances: int
    required_innings: str


@dataclass(frozen=True)
class LeagueTeams:
    """1リーグぶんの所属チーム。"""

    league_id: int
    league_name: str
    teams: list['TeamSummary']


@dataclass(frozen=True)
class LeagueStandings:
    """1リーグぶんの順位表。"""

    league_id: int
    league_name: str
    rows: list[StandingRow]


@dataclass(frozen=True)
class Standings:
    """指定シーズンの順位表。

    順位はリーグの中で決まるので、リーグごとに分けて持つ。
    リーグをまたいで1つの表にすると、別々に戦っているチームが
    同じ土俵で並んでしまう。
    """

    year: int
    leagues: list[LeagueStandings]
    available_years: list[int]
    sort: str = 'rank'
    descending: bool = False

    @property
    def rows(self) -> list[StandingRow]:
        """全リーグを平坦に並べたもの。件数の判定などに使う。"""
        return [row for league in self.leagues for row in league.rows]


@dataclass(frozen=True)
class LeagueDetail:
    """リーグ画面。"""

    id: int
    name: str
    year: int | None
    available_years: list[int]
    teams: list[TeamSummary]
    standings: list[StandingRow]
    recent_games: list['GameRow']


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
    # チームはリーグごとに分けて持つ。数が増えると1つの並びでは読みにくいため
    league_teams: list['LeagueTeams']

    @property
    def player_count(self) -> int:
        return self.batter_count + self.pitcher_count

    @property
    def teams(self) -> list['TeamSummary']:
        """全リーグを平坦に並べたもの。件数の判定などに使う。"""
        return [team for group in self.league_teams for team in group.teams]


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
