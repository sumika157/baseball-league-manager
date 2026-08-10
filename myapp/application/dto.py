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
    stadium_name: str = ""
    city: str = ""


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
    result: str  # '引分' または '<チーム名> の勝ち'
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
    innings_pitched: str = "0.0"
    earned_runs: int = 0
    strikeouts: int = 0
    hits_allowed: int = 0
    earned_run_average: float = 0.0
    # 打線での位置づけ。ボックススコアの並びと表示に使う
    batting_order: int | None = None
    slot_sequence: int = 0
    position_label: str = ""
    # 投手の登板順と、その試合で付いた記録
    appearance_order: int = 1
    walks_allowed: int = 0
    hit_by_pitch_allowed: int = 0
    home_runs_allowed: int = 0
    decision: str = ""  # '勝' / '敗' / 'Ｓ' / 'Ｈ'。付かなければ空
    # その試合ぶんの内訳
    doubles: int = 0
    triples: int = 0
    hit_by_pitch: int = 0
    sacrifice_flies: int = 0
    # 通算の率。ボックススコアの「打率」「防御率」は、その試合の率ではなく
    # 積み上がった率を参考として並べる（1試合の率は標本が小さすぎて読めない）
    career_batting_average: float = 0.0
    career_earned_run_average: float = 0.0

    @property
    def is_starter(self) -> bool:
        """スタメンか。打順の先頭に入っていればスタメン。"""
        return self.slot_sequence == 0

    @property
    def order_label(self) -> str:
        """打順の表示。交代で入った選手は打順を繰り返さず空にする。"""
        if self.batting_order is None or not self.is_starter:
            return ""
        return str(self.batting_order)


@dataclass(frozen=True)
class InningScoreColumn:
    """イニングスコアの1列。"""

    inning: int
    away: str  # 得点。ホームが攻めていない回は 'X'
    home: str


@dataclass(frozen=True)
class GameLineScore:
    """イニングスコア（スコアボード）。

    ビジターが表、ホームが裏。回数は延長も含めて記録されているぶんだけ出す。
    """

    columns: list[InningScoreColumn]
    away_total: int
    home_total: int
    away_hits: int
    home_hits: int

    @property
    def has_columns(self) -> bool:
        return bool(self.columns)


@dataclass(frozen=True)
class GameTeamBox:
    """1チームぶんのボックススコア。"""

    team_id: int
    team_name: str
    score: int
    batting: list[GamePlayerRow]
    pitching: list[GamePlayerRow]


@dataclass(frozen=True)
class GameDetail:
    """試合詳細。

    打撃・投球はチームごとに分けて持つ。ボックススコアはチーム単位で
    読むものなので、両チームを1つの表に混ぜると打順が追えない。
    """

    game: GameRow
    batting: list[GamePlayerRow]
    pitching: list[GamePlayerRow]
    line_score: GameLineScore | None = None
    away_box: GameTeamBox | None = None
    home_box: GameTeamBox | None = None

    @property
    def boxes(self) -> list[GameTeamBox]:
        """ビジター → ホームの順。スコアボードと同じ並びにする。"""
        return [box for box in (self.away_box, self.home_box) if box is not None]


@dataclass(frozen=True)
class PlayerGameRow:
    """選手個人ページの、試合ごとの成績1行。"""

    game_id: int
    played_on: object
    opponent_name: str
    result: str  # '勝' / '敗' / '分'
    # 打撃
    at_bats: int = 0
    hits: int = 0
    home_runs: int = 0
    runs_batted_in: int = 0
    # 投球
    innings_pitched: str = "0.0"
    earned_runs: int = 0
    strikeouts: int = 0


@dataclass(frozen=True)
class MonthlyRow:
    """選手の月別成績1行。

    率は月ごとに合算した実数から計算し直したもの（月々の率を平均しても
    正しい率にはならない）。
    """

    label: str  # '2026年4月'
    appearances: int
    # 打撃
    at_bats: int = 0
    hits: int = 0
    home_runs: int = 0
    runs_batted_in: int = 0
    batting_average: float = 0.0
    ops: float = 0.0
    # 投球
    innings_pitched: str = "0.0"
    earned_runs: int = 0
    strikeouts: int = 0
    earned_run_average: float = 0.0
    whip: float = 0.0


@dataclass(frozen=True)
class TeamMonthlyRow:
    """チームの月別成績1行。

    選手の MonthlyRow と対になるが、束ねる対象がチームなので勝敗を持ち、
    打撃と投球を同時に並べる（チームは常に攻守どちらも行う）。
    """

    label: str  # '2026年4月'
    games_played: int
    record_label: str  # '8-4-1'（勝-敗-分）
    winning_percentage: str
    # 打撃
    batting_average: float = 0.0
    ops: float = 0.0
    home_runs: int = 0
    # 投球
    earned_run_average: float = 0.0
    whip: float = 0.0
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

    detail: PlayerDetail
    games: list[PlayerGameRow]
    career: list[CareerRow] | None = None
    # 月別成績。調子の波は通算値では見えないため、期間で区切って並べる
    months: list[MonthlyRow] | None = None
    # プロフィール
    age: int | None = None
    throws_bats: str = ""
    height_cm: int | None = None
    weight_kg: int | None = None
    birthplace: str = ""
    debut_year: int | None = None
    # プロ入り前の経歴。(区分, 名称) を通った順に並べたもの
    amateur_career: list | None = None
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
class PlayerSearchRow:
    """選手検索の1行。所属が分からなくても名前でたどり着けるようにする。"""

    id: int
    name: str
    position: str
    team_id: int | None
    team_name: str
    league_name: str
    number: int | None
    is_active: bool


@dataclass(frozen=True)
class LeagueRankings:
    """1リーグぶんの各種ランキング。

    打撃・投手のタイトルはリーグの中で争われるため、リーグをまたいで
    1つの表にはしない。部門は NPB の個人成績ページにならい、
    打者は打率・本塁打・打点、投手は防御率・勝利・セーブを出す。
    """

    average_leaders: list[RankingEntry]
    home_run_leaders: list[RankingEntry]
    rbi_leaders: list[RankingEntry]
    era_leaders: list[RankingEntry]
    win_leaders: list[RankingEntry]
    save_leaders: list[RankingEntry]

    @property
    def has_any(self) -> bool:
        return bool(
            self.average_leaders
            or self.home_run_leaders
            or self.rbi_leaders
            or self.era_leaders
            or self.win_leaders
            or self.save_leaders
        )


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
    # 守備に左右されない投球内容。チーム防御率との差が守備・運の寄与を示す
    fip: float = 0.0


@dataclass(frozen=True)
class LeagueTeams:
    """1リーグぶんの所属チーム。"""

    league_id: int
    league_name: str
    teams: list[TeamSummary]


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
    sort: str = "rank"
    descending: bool = False

    @property
    def rows(self) -> list[StandingRow]:
        """全リーグを平坦に並べたもの。件数の判定などに使う。"""
        return [row for league in self.leagues for row in league.rows]


@dataclass(frozen=True)
class MatchupColumn:
    """対戦成績表の列見出し。"""

    team_id: int
    team_name: str

    @property
    def short_name(self) -> str:
        """列幅を抑えるための略称。先頭2文字。

        チーム名をそのまま並べると、チーム数ぶんの列で表が横に伸びる。
        行の見出しには正式名を出すので、対応は付く。
        """
        return self.team_name[:2]


@dataclass(frozen=True)
class MatchupCell:
    """対戦成績表の1マス。行のチームから見た、列のチームとの成績。"""

    opponent_id: int | None
    label: str  # '3-1-1'（勝-敗-分）。自分自身の列は '—'
    is_self: bool = False
    is_winning: bool = False  # 勝ち越している
    is_losing: bool = False  # 負けている


@dataclass(frozen=True)
class MatchupRow:
    """対戦成績表の1行。"""

    team_id: int
    team_name: str
    cells: list[MatchupCell]
    total_label: str


@dataclass(frozen=True)
class MatchupTable:
    """チーム間の対戦成績表。

    列の並びは行と同じ（順位表の順）。行と列で同じ順に並べることで、
    対角線が自分自身になり、表として読めるようになる。
    """

    columns: list[MatchupColumn]
    rows: list[MatchupRow]

    @property
    def has_rows(self) -> bool:
        return bool(self.rows)


@dataclass(frozen=True)
class TitleDepartment:
    """タイトル1部門。打率・本塁打などの部門ごとの上位者。"""

    key: str
    label: str
    note: str = ""  # '規定打席以上' など。率の部門だけ付く
    entries: list[RankingEntry] | None = None

    @property
    def leader(self) -> RankingEntry | None:
        """首位者。同率なら先頭の1人（順位そのものは entry.rank が持つ）。"""
        return self.entries[0] if self.entries else None


@dataclass(frozen=True)
class LeagueTitles:
    """リーグのタイトル一覧。

    ダッシュボードのランキングは全体の概況で、通算成績を並べる。こちらは
    シーズンで区切り、部門を掘り下げて確認する場所として役割を分ける。
    """

    league_id: int
    league_name: str
    year: int | None
    available_years: list[int]
    departments: list[TitleDepartment]

    @property
    def has_any(self) -> bool:
        return any(d.entries for d in self.departments)


@dataclass(frozen=True)
class LeaguePlayerRow:
    """リーグの成績一覧の1行。チームをまたいで並べるため所属を添える。"""

    team_id: int
    team_name: str
    player: BatterRow | PitcherRow


@dataclass(frozen=True)
class LeagueStats:
    """リーグの成績一覧。所属する全選手の通算成績を1つの表で見る。

    ダッシュボードのランキングは通算の上位だけを出す。ここはその続きとして
    全員を並べる場所。シーズンで区切った部門別の上位者はタイトル一覧が担う。
    """

    league_id: int
    league_name: str
    listing: Listing


@dataclass(frozen=True)
class LeagueDetail:
    """リーグ画面。"""

    id: int
    name: str
    year: int | None
    available_years: list[int]
    teams: list[TeamSummary]
    standings: list[StandingRow]
    recent_games: list[GameRow]
    # チーム間の相性。順位表の背景として同じ画面に置く
    matchups: MatchupTable | None = None


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
class DashboardLeague:
    """ダッシュボードの1リーグぶんのまとまり。

    ランキング・順位表・チームを1本のタブで切り替えるため、リーグ単位で
    まとめて持つ。タブバーを内容ごとに分けると、左右で別のリーグが
    表示される状態が生まれてしまう。順位表は最新シーズンのもの。
    """

    league_id: int
    league_name: str
    rankings: LeagueRankings
    standings: list[StandingRow]
    standings_year: int | None
    teams: list[TeamSummary]


@dataclass(frozen=True)
class Dashboard:
    """ホーム画面（ダッシュボード）に表示する内容。"""

    league_count: int
    team_count: int
    batter_count: int
    pitcher_count: int
    # タイトルも順位もリーグの中で争われるので、内容はリーグごとに持つ
    leagues: list[DashboardLeague]

    @property
    def player_count(self) -> int:
        return self.batter_count + self.pitcher_count

    @property
    def teams(self) -> list[TeamSummary]:
        """全リーグを平坦に並べたもの。件数の判定などに使う。"""
        return [team for league in self.leagues for team in league.teams]


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
    isolated_power: float = 0.0
    walks: int = 0
    sacrifice_flies: int = 0
    slugging_percentage: float = 0.0
    # リーグ平均を100とした指数。得点環境の違うリーグ・シーズンでも比べられる
    ops_plus: float = 0.0
    is_captain: bool = False
    is_foreign_player: bool = False
    throws_bats: str = ""
    height_cm: int | None = None
    weight_kg: int | None = None
    age: int | None = None


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
    walks_per_nine: float = 0.0
    # FIP はリーグの定数を足して仕上げるため、リーグを知る側で計算して渡す
    fip: float = 0.0
    # リーグ平均防御率を100とした指数。FIP と逆で、大きいほど良い
    era_plus: float = 0.0
    saves: int = 0
    # 救援投手の指標。HP（ホールドポイント）はホールド＋救援勝利
    holds: int = 0
    hold_points: int = 0
    starts: int = 0
    home_runs_allowed: int = 0
    hit_by_pitch_allowed: int = 0
    is_captain: bool = False
    is_foreign_player: bool = False
    throws_bats: str = ""
    height_cm: int | None = None
    weight_kg: int | None = None
    age: int | None = None


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
    home_runs_allowed: int = 0
    hit_by_pitch_allowed: int = 0
    isolated_power: float = 0.0
    walks_per_nine: float = 0.0
    fip: float = 0.0
    ops_plus: float = 0.0
    era_plus: float = 0.0
    holds: int = 0
    hold_points: int = 0
    starts: int = 0
    is_captain: bool = False
