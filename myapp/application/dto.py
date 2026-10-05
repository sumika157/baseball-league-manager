"""プレゼンテーション層へ渡す読み取り専用のデータ構造。

ドメインオブジェクトをテンプレートへ直接渡すと、画面側からドメインの状態を
変更できてしまう。表示に必要な値だけを持つ DTO に詰め替えて渡す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..domain.pennant.club_plan import PlanSection
from ..domain.pennant.season import SeasonPhase
from ..domain.simulation.ratings import RatingEmphasis
from ..domain.value_objects import BattingLine, FieldingPosition, PitchingLine, Position


@dataclass(frozen=True)
class ActivePlayerStats:
    """在籍中の選手1人と、その成績。ランキング・タイトルの材料。

    チーム（集約）を組み立てずに、順位づけに要る値だけを読み出す。
    成績が通算かシーズンかは、読んだ参照クエリの側で決まる。
    """

    player_id: int
    name: str
    number: int
    position: Position
    team_id: int
    team_name: str
    batting: BattingLine
    pitching: PitchingLine


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
    winner_team_id: int | None
    # 未記録（打席も明細も無い）の試合は 0-0 でも引分ではない。判定の出典は `Game.is_recorded`
    is_recorded: bool = True

    @property
    def result(self) -> str:
        """'未記録'・'引分'・'<チーム名> の勝ち' のいずれか。

        持っている値から決まるので、作る側ごとに組み立てない
        （勝者そのものはドメインの winning_team_id が唯一の出典）。
        """
        if not self.is_recorded:
            return "未記録"
        if self.winner_team_id is None:
            return "引分"
        name = self.home_team_name if self.winner_team_id == self.home_team_id else self.away_team_name
        return f"{name} の勝ち"


@dataclass(frozen=True)
class PlayerIndexEntry:
    """試合詳細の組み立てで、選手 id から引く1人ぶんの情報。

    ボックススコアに並べる参考値として、1試合の率は読めないため通算の率を持つ。
    """

    name: str
    number: int
    team_id: int
    career_batting_average: float
    career_earned_run_average: float


@dataclass(frozen=True)
class GameEditPlayer:
    """試合の編集画面に並べる、ロスターの1選手。"""

    id: int
    name: str
    number: int
    position: str
    is_pitcher: bool


@dataclass(frozen=True)
class GameEditLineupSlot:
    """試合の編集画面に並べる、打順の1行。

    出場時点は「何回・表裏・その半回の何人目」で持つ（保存済みの値を入力と同じ形に戻したもの）。
    代打・代走・投手は出場時点を打席から導くので、ここは None・False・1 の既定にしてある。
    """

    player_id: int
    batting_order: int
    slot_sequence: int
    fielding_position: str  # 守備位置の表記。無ければ空
    entered_inning: int | None
    entered_is_bottom: bool
    entered_batter: int


@dataclass(frozen=True)
class GameEditRoster:
    """試合の編集画面に並べる、1チームぶんのロスターと、その試合の打順。"""

    team_id: int
    team_name: str
    is_home: bool
    players: list[GameEditPlayer]
    lineup: list[GameEditLineupSlot]


@dataclass(frozen=True)
class GameEditHeader:
    """試合の編集画面に出す試合の基本項目。得点は打席から導いた値で読み取り専用。"""

    id: int
    year: int
    played_on: date
    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int


@dataclass(frozen=True)
class GameEditRunnerAdvance:
    """編集画面の打席に付く、走者の進塁1つ。"""

    runner_id: int
    from_base: int
    to_base: int
    reason: str
    error_index: int | None


@dataclass(frozen=True)
class GameEditError:
    """編集画面の打席に付く、失策1つ。"""

    player_id: int
    position: str
    kind: str


@dataclass(frozen=True)
class GameEditPlateAppearance:
    """編集画面に並べる打席1つ。保存 API に送り返す形と同じ粒度。"""

    sequence: int
    inning: int
    is_bottom: bool
    batter_id: int
    pitcher_id: int
    batting_order: int
    slot_sequence: int
    result: str
    fielded_by: tuple[str, ...]  # 守備位置の表記を、打球を処理した順に
    advances: list[GameEditRunnerAdvance]
    errors: list[GameEditError]


@dataclass(frozen=True)
class GameEditData:
    """試合の編集画面に必要な材料。打順と打席は、表示用の値に詰め替えて持つ（集約は持たない）。"""

    header: GameEditHeader
    rosters: list[GameEditRoster]
    plate_appearances: list[GameEditPlateAppearance]


@dataclass(frozen=True)
class GameTeamIds:
    """試合の両チームの id。権限の確認のように、ロスターまでは要らない用途に使う。"""

    home_team_id: int
    away_team_id: int


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
    runs: int = 0
    sacrifice_bunts: int = 0
    stolen_bases: int = 0
    double_plays: int = 0
    # 打者の三振。投手の `strikeouts`（奪三振）とは別の事実なので名前を分ける
    # （1つの DTO を打撃行と投球行の両方に使っているため）
    strikeouts_batting: int = 0
    runs_allowed: int = 0
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
    # 失策は打席の記録から数える。打席の無い試合は数えられないので None（画面は「—」）
    away_errors: int | None = None
    home_errors: int | None = None

    @property
    def has_columns(self) -> bool:
        return bool(self.columns)


@dataclass(frozen=True)
class GameFieldingRow:
    """試合詳細に並べる、1選手ぶんの守備成績。守備に就いた選手は機会が無くても載る。"""

    player_id: int
    player_name: str
    number: int
    team_id: int
    position_label: str
    putouts: int = 0
    assists: int = 0
    errors: int = 0


@dataclass(frozen=True)
class ScorebookMark:
    """スコアブックのマスに書く1打席。"""

    result: str  # 結果の表記（`PlateAppearanceResult.label`）
    batter_name: str


@dataclass(frozen=True)
class ScorebookRow:
    """スコアブックの1行（打順1人ぶん）。cells は回の列と同じ順で、打席が無い回は空。

    同じ打順が同じ回に2打席立つ（打者一巡）と、1つのマスに複数入る。
    """

    batting_order: int
    cells: list[list[ScorebookMark]]


@dataclass(frozen=True)
class ScorebookGrid:
    """1チームぶんのスコアブック（打順 × 回）。読み取り専用の表示用。"""

    team_name: str
    innings: list[int]
    rows: list[ScorebookRow]


@dataclass(frozen=True)
class GameTeamBox:
    """1チームぶんのボックススコア。"""

    team_id: int
    team_name: str
    score: int
    batting: list[GamePlayerRow]
    pitching: list[GamePlayerRow]
    # 守備成績は打席から導く値なので、打席の記録が無い試合では空
    fielding: list[GameFieldingRow] = field(default_factory=list)


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
    # ビジター → ホームの順。打席の記録が無い試合は空
    scorebook: list[ScorebookGrid] = field(default_factory=list)

    @property
    def boxes(self) -> list[GameTeamBox]:
        """ビジター → ホームの順。スコアボードと同じ並びにする。"""
        return [box for box in (self.away_box, self.home_box) if box is not None]


@dataclass(frozen=True)
class PlayerGameRow:
    """選手個人ページの、試合ごとの成績1行。

    チームの勝敗は持たない。個人ページはその選手の働きを見る場所なので、
    投手には本人に付いた記録（勝・敗・Ｓ・Ｈ）を出し、野手には出さない。
    """

    game_id: int
    played_on: object
    opponent_name: str
    # 打撃
    at_bats: int = 0
    hits: int = 0
    home_runs: int = 0
    runs_batted_in: int = 0
    runs: int = 0
    stolen_bases: int = 0
    caught_stealing: int = 0
    sacrifice_bunts: int = 0
    intentional_walks: int = 0
    strikeouts_batting: int = 0
    double_plays: int = 0
    # 投球
    innings_pitched: str = "0.0"
    runs_allowed: int = 0
    earned_runs: int = 0
    strikeouts: int = 0
    decision: str = ""  # 本人に付いた記録。ボックススコアと同じ印


@dataclass(frozen=True)
class YearlyRow:
    """選手の年度別成績1行。

    キャリア通算（`PlayerDetail`）が積み上げ全体を表すのに対し、こちらは
    シーズンごとの働きを表す。率は年ごとに合算した実数から計算し直した値。
    """

    label: str  # '2026年'
    appearances: int
    # 打撃
    plate_appearances: int = 0
    at_bats: int = 0
    hits: int = 0
    doubles: int = 0
    triples: int = 0
    home_runs: int = 0
    runs_batted_in: int = 0
    walks: int = 0
    hit_by_pitch: int = 0
    sacrifice_flies: int = 0
    runs: int = 0
    stolen_bases: int = 0
    caught_stealing: int = 0
    sacrifice_bunts: int = 0
    intentional_walks: int = 0
    strikeouts_batting: int = 0
    double_plays: int = 0
    batting_average: float = 0.0
    on_base_percentage: float = 0.0
    slugging_percentage: float = 0.0
    ops: float = 0.0
    # 投球
    starts: int = 0
    innings_pitched: str = "0.0"
    wins: int = 0
    losses: int = 0
    saves: int = 0
    holds: int = 0
    hold_points: int = 0
    hits_allowed: int = 0
    home_runs_allowed: int = 0
    walks_allowed: int = 0
    hit_by_pitch_allowed: int = 0
    strikeouts: int = 0
    runs_allowed: int = 0
    earned_runs: int = 0
    earned_run_average: float = 0.0
    whip: float = 0.0
    strikeouts_per_nine: float = 0.0


@dataclass(frozen=True)
class MonthlyRow:
    """選手の月別成績1行。

    率は月ごとに合算した実数から計算し直したもの（月々の率を平均しても
    正しい率にはならない）。
    """

    label: str  # '2026年4月'
    appearances: int
    # 月のタブ（試合ごとの成績の絞り込み）に使う。ラベルから月を切り出さない
    year: int = 0
    month: int = 0
    # 打撃
    at_bats: int = 0
    hits: int = 0
    home_runs: int = 0
    runs_batted_in: int = 0
    runs: int = 0
    stolen_bases: int = 0
    caught_stealing: int = 0
    sacrifice_bunts: int = 0
    intentional_walks: int = 0
    strikeouts_batting: int = 0
    double_plays: int = 0
    batting_average: float = 0.0
    ops: float = 0.0
    # 投球
    innings_pitched: str = "0.0"
    runs_allowed: int = 0
    earned_runs: int = 0
    strikeouts: int = 0
    earned_run_average: float = 0.0
    whip: float = 0.0

    @property
    def key(self) -> str:
        """月のタブの値。'2026-04' のように年と月を1つの値にまとめたもの。

        年と月を別の引数にすると、片方だけ指定された組み合わせを画面側で
        繕うことになる。選べるのは「出場した月」だけなので1つの値で表す。
        """
        return f"{self.year}-{self.month:02d}"


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
class FieldingRow:
    """選手の守備成績1行（通算または1年度）。守備率は足した実数から計算し直した値。"""

    label: str  # '通算' / '2026年'
    games: int  # 守備に就いた試合数
    total_chances: int
    putouts: int
    assists: int
    errors: int
    double_plays_turned: int
    fielding_percentage: float


@dataclass(frozen=True)
class PlayerFielding:
    """選手個人ページの守備成績。通算と年度別。"""

    career: FieldingRow
    years: list[FieldingRow]


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
    # 試合ごとの成績。**選択された月のぶんだけ**が入る（全期間を並べると
    # 1シーズンで140行を超え、読む場所ではなくなる）。全期間の出場試合数は
    # appearances、月の一覧は months が持つ。
    games: list[PlayerGameRow]
    career: list[CareerRow] | None = None
    # 年度別成績。キャリア通算（detail）とシーズンごとの働きを分けて見るため
    years: list[YearlyRow] | None = None
    # 月別成績。調子の波は通算値では見えないため、期間で区切って並べる
    months: list[MonthlyRow] | None = None
    # 守備成績。守備に就いた試合が1つも無ければ None（画面に出さない）
    fielding: PlayerFielding | None = None
    # 選択されている月（MonthlyRow.key と同じ形式）と、その表示名
    selected_month: str = ""
    selected_month_label: str = ""
    # 全期間の出場試合数。games は月で絞られるため、数はここから出す
    appearances: int = 0
    # プロフィール
    age: int | None = None
    name_kana: str = ""  # 氏名のよみがな。名前に添えて出す
    back_name: str = ""  # ユニフォーム背面の表記（例: T.YAMADA）
    throws_bats: str = ""
    height_cm: int | None = None
    weight_kg: int | None = None
    birthplace: str = ""
    nationality: str = ""
    # 外国人枠の対象か。国籍（事実）とは別の概念なので独立して持つ
    is_foreign_player: bool = False
    debut_year: int | None = None
    # プロ入り前の経歴。(区分, 名称) を通った順に並べたもの
    amateur_career: list | None = None
    has_profile: bool = False


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
class LeagueOption:
    """絞り込みの選択肢としてのリーグ。表示に要る最小限だけ持つ。"""

    id: int
    name: str


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
    # 規定（規定打席・規定投球回）に到達した選手だけに絞っているか。
    # 到達した人数と全体の人数を添えて、切り替えの前に規模が分かるようにする
    qualified: bool = False
    qualified_count: int = 0
    total_count: int = 0


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

    ランキングと順位表を1本のタブで切り替えるため、リーグ単位でまとめて持つ。
    タブバーを内容ごとに分けると、左右で別のリーグが表示される状態が
    生まれてしまう。順位表は最新シーズンのもの。
    """

    league_id: int
    league_name: str
    rankings: LeagueRankings
    standings: list[StandingRow]
    standings_year: int | None
    # 順位表と同じチームの並びなので画面には並べない。1試合も行われておらず
    # 順位表を作れないリーグで、順位表の代わりに出すためだけに持つ
    teams: list[TeamSummary]
    # 直近の試合（新しい順）。順位表が「どこが強いか」を示すのに対して、
    # 「いま何が起きているか」を示す。さかのぼるのは試合一覧が受け持つ
    recent_games: list[GameRow]


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
    # 打席と安打は単打などから導ける値だが、導出の出典は BattingLine 側にあり、
    # 画面ごとに足し算を書き直さないためここに持たせる
    plate_appearances: int
    hits: int
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
    # 打席の記録から導く項目。打者の三振は投手の strikeouts（奪三振）と別の事実なので名前を分ける
    runs: int = 0
    stolen_bases: int = 0
    caught_stealing: int = 0
    sacrifice_bunts: int = 0
    intentional_walks: int = 0
    strikeouts_batting: int = 0
    double_plays: int = 0
    runs_allowed: int = 0


@dataclass(frozen=True)
class LineupSlot:
    """打順の1枠。誰が何番でどこを守ったか。

    スコアブックの保存でプレゼンテーション層から受け取る。成績は含めない
    （打席から導くため）。同じ打順に複数の選手が並ぶ場合は slot_sequence で区別する。
    """

    team_id: int
    player_id: int
    batting_order: int
    slot_sequence: int
    fielding_position: FieldingPosition | None
    # 途中出場の行だけ。守備固めなど打席から導けない出場を、入った半回で指す
    # （回と、その回の表か裏か）。応用層が半回の最初の打席の番号に読み替える。
    entered_inning: int | None = None
    entered_is_bottom: bool = False
    # その半回で何人目の打者から（1始まり）。守備固めが半回の途中で入る場合に指す
    entered_batter: int = 1


@dataclass(frozen=True)
class PennantWorldRow:
    """ペナントの世界ひとつ。一覧や詳細に出す、作ったときに決まった事実。"""

    id: int
    name: str
    seed: int
    start_year: int
    owner_id: int | None
    managed_team_id: int | None


@dataclass(frozen=True)
class PennantWorldCreated:
    """世界を作った結果。分岐で写した件数を添える。"""

    world: PennantWorldRow
    league_count: int
    team_count: int
    player_count: int
    # 初期能力を保存した選手の数（分岐した選手全員ぶん）
    rating_count: int


@dataclass(frozen=True)
class SimulationPlayer:
    """シミュレーションに出る候補の選手1人（球団に現在在籍している選手）。能力は含まない。"""

    player_id: int
    name: str
    position: Position
    is_foreign: bool


@dataclass(frozen=True)
class SimulationTeam:
    """シミュレーションに出る球団ひとつ。外国人の枠は所属リーグの値。"""

    team_id: int
    name: str
    foreign_roster_limit: int | None
    foreign_game_limit: int | None
    players: tuple[SimulationPlayer, ...]


@dataclass(frozen=True)
class LastStart:
    """投手の、直近の先発の日。先発の間隔（中5日）を導く材料。"""

    pitcher_id: int
    played_on: date


@dataclass(frozen=True)
class PitchingOuting:
    """1試合の登板の記録。投げた投手を登板順に並べる（先頭が先発）。連投を導く材料。"""

    played_on: date
    pitcher_ids: tuple[int, ...]


@dataclass(frozen=True)
class SimulationContext:
    """日を進めるのに要る、世界の今の状態（集約を組み立てずに読んだもの）。

    疲労は保存しない。直近の登板（`last_starts`・`recent_outings`）から導く。
    """

    teams: tuple[SimulationTeam, ...]
    last_starts: tuple[LastStart, ...]
    recent_outings: tuple[PitchingOuting, ...]


@dataclass(frozen=True)
class PlanNotice:
    """編成の上書きが使えず、その区画を自動で進めたことの知らせ（ホームで出す）。"""

    team_id: int
    team_name: str
    # 自動に落ちた最初の日（この「進める」の中では、その球団の最初の試合日）
    on: date
    section: PlanSection
    reason: str
    # False なら区画は自動に落とさず、効かない理由を知らせるだけ
    falls_back: bool = True


@dataclass(frozen=True)
class AdvanceReport:
    """世界を進めた結果。"""

    # 今回の「進める」で試合をした日（日付順）
    played_dates: tuple[date, ...]
    games: int
    # 進めた後の「今日」（消化した最後の試合日）。まだ1試合もしていなければ None
    today: date | None
    # 進めた後に残っている未消化の対戦の数
    remaining_fixtures: int
    # この呼び出しで、その年の日程を初めて作ったか
    schedule_created: bool
    # 編成の上書きが使えず自動で進めた区画（球団ごと・区画ごとに1つ。空なら上書きはすべて効いた）
    plan_notices: tuple[PlanNotice, ...] = ()

    @property
    def season_finished(self) -> bool:
        """日程をすべて消化したか。日程を作った直後（試合のない開幕前）は終わっていない。"""
        return self.remaining_fixtures == 0 and self.today is not None


@dataclass(frozen=True)
class WorldContext:
    """世界ひとつの「いま」。世界バーの表示と、共有テンプレートの URL の引き方（`scoped_url`）に使う。

    今日・局面は保存した値ではなく、試合と日程から導いた値（`domain/pennant/season.py`）。
    """

    world_id: int
    name: str
    phase: SeasonPhase
    # いまの年度（次の対戦の年、無ければ最後に試合をした年、どちらも無ければ開幕年。domain の `season_year`）。
    # 締めた直後は、まだ試合の無い翌年（today は前年の最終日のまま）
    season_year: int
    # 最後に試合をした日。まだ試合が無ければ None
    today: date | None
    # 受け持つ球団（まだ決めていなければ None と空の名前）
    managed_team_id: int | None
    managed_team_name: str
    # 世界バーの「成績」「タイトル」の行き先のリーグ。受け持つ球団のリーグ、無ければ先頭のリーグ
    default_league_id: int | None
    # 世界のオーナー（書き込みを許す人）。画面には名前を出さず、操作の権限の判定だけに使う
    owner_id: int | None = None


@dataclass(frozen=True)
class WorldSummary:
    """世界ひとつの見出しの材料。世界の台帳・試合・日程から、まとめて読んだ値（集約は組み立てない）。"""

    world_id: int
    name: str
    start_year: int
    managed_team_id: int | None
    managed_team_name: str
    # 受け持つ球団のリーグ、無ければ世界の先頭のリーグ（リーグが無ければ None）
    default_league_id: int | None
    # 最後に試合をした日。まだ無ければ None
    last_played_on: date | None
    # 次の対戦の日（未消化の対戦の最小の日）。残っていなければ None
    next_fixture_on: date | None
    owner_id: int | None = None


@dataclass(frozen=True)
class OwnStanding:
    """受け持つ球団の、いまの順位と成績（世界の一覧の1行に出す）。"""

    rank: int
    wins: int
    losses: int
    ties: int
    games_behind: str


@dataclass(frozen=True)
class PennantWorldListRow:
    """世界の一覧の1行。オーナーは載せない（一覧は誰にでも見せるため）。"""

    context: WorldContext
    # 受け持つ球団が決まっていて、順位表に載っているときだけ
    own_standing: OwnStanding | None


@dataclass(frozen=True)
class ClubPlayerRow:
    """編成の画面に並べる、球団の選手1人（能力のある選手だけ）。"""

    player_id: int
    name: str
    position: Position
    is_foreign: bool
    is_active: bool


@dataclass(frozen=True)
class ClubLineupRow:
    """オーダーの1枠。"""

    batting_order: int
    player_id: int
    name: str
    position: FieldingPosition


@dataclass(frozen=True)
class ClubPlanView:
    """球団の編成。**いま進めたときに使われる形**（手動が有効なら手動、無効なら自動）と、区画ごとの手動かどうか。

    `*_is_manual` が False の区画は、AI 監督の自動編成（提案）をそのまま映している。手動の区画で
    使えなくなっているものは区画が手動のまま（GM が自動に戻すまで）で、`notices` に理由が入り、
    中身はその日に使われる自動に落ちた形になる（設計書 6.3 の「無効になった上書き」）。
    自動のスタメンは先発の外国人を出場枠に数えない目安で、外国人の投手が先発する日は実際の試合と違いうる
    （先発は日ごとの疲労で決まるため、画面では決めない）。出場枠は世界のリーグで最も厳しい値で数えるので、
    自リーグの枠が緩い世界では、自リーグの試合の実際のスタメンと外国人の人数が違いうる。
    """

    team_id: int
    team_name: str
    year: int
    players: tuple[ClubPlayerRow, ...]
    active_ids: tuple[int, ...]
    lineup: tuple[ClubLineupRow, ...]
    rotation_ids: tuple[int, ...]
    closer_id: int | None
    # 抑えとローテーションを除く救援陣（セットアップ、中継ぎの順）
    bullpen_ids: tuple[int, ...]
    limits: ClubLimitsView
    counts: ClubRosterCounts
    # 保存済みの上書き（None は自動）。上の欄は「いま進めたときに使われる形」で、使えない上書きは自動に落ちた値になる。
    # 手動の区画の編集欄の初期値は、こちらから作る
    # （解決後の値から作ると、保存したときに GM の上書きが AI の形で置き換わる）
    saved_active_ids: tuple[int, ...] | None
    saved_lineup: tuple[ClubLineupRow, ...] | None
    saved_rotation_ids: tuple[int, ...] | None
    saved_closer_id: int | None
    active_is_manual: bool
    lineup_is_manual: bool
    rotation_is_manual: bool
    closer_is_manual: bool
    notices: tuple[ClubPlanNotice, ...] = ()


@dataclass(frozen=True)
class ClubLimitsView:
    """編成の枠（画面に出す上限と、選択肢。数は domain が出典で、テンプレートには書かない）。None は制限なし。"""

    active_size: int
    foreign_roster_limit: int | None
    foreign_game_limit: int | None
    lineup_size: int
    rotation_size: int
    min_rotation_size: int
    min_active_pitchers: int
    # オーダーで選べる守備位置（画面に出す順）
    lineup_positions: tuple[FieldingPosition, ...]


@dataclass(frozen=True)
class ClubRosterCounts:
    """いまの 1軍の内訳（登録の画面の上部に出す）。"""

    total: int
    batters: int
    pitchers: int
    catchers: int
    foreign: int


@dataclass(frozen=True)
class ClubPlanNotice:
    """編成の上書きが使えない理由（`ClubPlanView` に添える。日付は持たない）。"""

    section: PlanSection
    reason: str
    # False なら区画は自動に落とさず、効かない理由を知らせるだけ
    falls_back: bool = True


# --- ペナントの編成画面（P5b） ---


@dataclass(frozen=True)
class ClubPitcherUsage:
    """投手1人の、次の試合日に向けた状態。保存せず、直近の登板から導く。"""

    player_id: int
    name: str
    is_foreign: bool
    # 次の試合日の直近3日に投げたか（古い日から）
    pitched_recently: tuple[bool, ...]
    # 前回の先発から何日経っているか（次の試合日の時点。先発の記録が無ければ None）
    days_since_start: int | None
    # 次の試合日に投げられるか（連投の上限に達していないか）
    can_pitch: bool


@dataclass(frozen=True)
class ClubPitchingUsage:
    """投手陣の状態（編成の「投手陣」タブに出す）。次の試合日が無ければ、状態は既定（投げられる）で返す。"""

    # 受け持つ球団の次の試合日（日程が残っていなければ None）
    next_game_on: date | None
    # `pitched_recently` の日付（古い日から）
    recent_days: tuple[date, ...]
    rotation: tuple[ClubPitcherUsage, ...]
    closer: ClubPitcherUsage | None
    bullpen: tuple[ClubPitcherUsage, ...]


# --- ペナントの GM ホーム（P4b） ---


@dataclass(frozen=True)
class PennantWorldList:
    """世界の一覧。ログインしている人の世界と、ほかの人の世界に分ける。オーナーの名前は載せない。"""

    mine: list[PennantWorldListRow]
    others: list[PennantWorldListRow]
    # 1人が持てる世界の数（上限に達したら作成フォームを出さない）
    world_limit: int

    @property
    def at_limit(self) -> bool:
        return len(self.mine) >= self.world_limit


@dataclass(frozen=True)
class OwnTeamSummary:
    """自軍の帯。順位・成績・勢い・残り試合。"""

    team_id: int
    team_name: str
    league_name: str
    phase: SeasonPhase
    # 試合がまだ無ければ順位は付かない
    rank: int | None
    wins: int
    losses: int
    ties: int
    winning_percentage: str
    # 首位は「—」
    games_behind: str
    games_played: int
    remaining_games: int
    # 直近10試合の成績（「6勝4敗」）と連続（「3連勝」）。試合が無ければ空
    last_ten: str
    streak: str
    # 開幕前だけ。日程の最初の日（日程がまだ無ければ既定の開幕日）
    opening_date: date | None

    @property
    def is_leader(self) -> bool:
        """首位か。domain の `StandingRow.is_leader` と同じ定義（1位）で、表示用の「ゲーム差」の表記には頼らない。"""
        return self.rank == 1

    @property
    def is_before_opening(self) -> bool:
        return self.phase is SeasonPhase.BEFORE_OPENING

    @property
    def is_finished(self) -> bool:
        return self.phase is SeasonPhase.FINISHED


@dataclass(frozen=True)
class UpcomingGame:
    """自軍の未消化の試合。"""

    played_on: date
    opponent_team_id: int
    opponent_name: str
    is_home: bool


@dataclass(frozen=True)
class AdvanceOption:
    """「進める」のボタン1つ。終わる日付と試合数は日程から事前に導く（日程がまだ無ければ None）。"""

    # `AdvanceTarget` の値（フォームで送る文字列）
    target: str
    label: str
    end_date: date | None
    games: int | None


@dataclass(frozen=True)
class GameNote:
    """1試合の見どころ。責任投手と本塁打。"""

    game_id: int
    winning_pitcher: str
    losing_pitcher: str
    save_pitcher: str
    home_runs: tuple[str, ...]


@dataclass(frozen=True)
class PeriodBatting:
    """ある期間の、選手1人の打撃の合計。"""

    player_id: int
    name: str
    # 期間の合計。安打・打率などは BattingLine が出典（内訳から導く）
    batting: BattingLine


@dataclass(frozen=True)
class PeriodPitching:
    """ある期間の、選手1人の投球の合計。"""

    player_id: int
    name: str
    wins: int
    losses: int
    saves: int


@dataclass(frozen=True)
class OwnGameResult:
    """自軍の1試合の結果（自軍から見た表記）。"""

    game_id: int
    played_on: date
    opponent_name: str
    is_home: bool
    # 「○」「●」「△」
    outcome: str
    own_score: int
    opponent_score: int
    winning_pitcher: str = ""
    losing_pitcher: str = ""
    save_pitcher: str = ""
    home_runs: tuple[str, ...] = ()


@dataclass(frozen=True)
class PeriodBatter:
    """期間の活躍（打者）。"""

    player_id: int
    name: str
    batting_average: str
    hits: int
    at_bats: int
    home_runs: int
    runs_batted_in: int


@dataclass(frozen=True)
class AdvanceSummary:
    """「進めた結果」のまとめ。`since` より後から `until` までの試合。"""

    since: date
    until: date
    wins: int
    losses: int
    ties: int
    # 期間の前と後の順位・首位との差。期間の前に試合が無ければ順位は None
    rank_before: int | None
    rank_after: int | None
    games_behind_before: str
    games_behind_after: str
    own_games: list[OwnGameResult]
    # 自軍の試合がちょうど1つのときだけ、そのスコアボード
    line_score_game: GameRow | None
    line_score: GameLineScore | None
    batters: list[PeriodBatter]
    pitchers: list[PeriodPitching]
    # 自軍以外の試合（新しい順。多いときは先頭から `other_games_omitted` を除いた分だけ）
    other_games: list[GameRow]
    other_games_omitted: int


@dataclass(frozen=True)
class KeyBatterRow:
    """主力打者の1行。"""

    player_id: int
    name: str
    batting_average: float
    home_runs: int
    runs_batted_in: int
    ops: float
    # 直近15日の打率。打数が無ければ空
    recent_average: str


@dataclass(frozen=True)
class KeyPitcherRow:
    """投手陣の1行。"""

    player_id: int
    name: str
    wins: int
    losses: int
    saves: int
    innings_pitched: str
    earned_run_average: float
    starts: int


@dataclass(frozen=True)
class TitleRaceRow:
    """タイトル争い。自軍の選手が上位にいる部門。"""

    department: str
    rank: int
    player_id: int
    player_name: str
    value: str
    leader_name: str
    leader_value: str


@dataclass(frozen=True)
class HomeStandings:
    """ホームの順位表。自軍のリーグを最初に出し、ほかのリーグは選んで切り替える。"""

    year: int
    # 切り替えの選択肢（試合のあるリーグ。自軍のリーグが先頭）
    leagues: list[LeagueOption]
    selected: LeagueStandings | None


@dataclass(frozen=True)
class PennantHome:
    """GM ホームの材料。状態は保存せず、日程と試合から導く。"""

    world: WorldContext
    own: OwnTeamSummary | None
    # 自軍の今後の日程（先頭が次の試合）
    upcoming: list[UpcomingGame]
    advance_options: list[AdvanceOption]
    # 二重送信の検出に使う「いまの今日」（日付、まだ試合が無ければ空）
    expected_today: str
    summary: AdvanceSummary | None
    standings: HomeStandings
    batters: list[KeyBatterRow]
    pitchers: list[KeyPitcherRow]
    titles: list[TitleRaceRow]

    @property
    def season_finished(self) -> bool:
        return self.world.phase is SeasonPhase.FINISHED


@dataclass(frozen=True)
class WorldDeletion:
    """世界の削除の確認に出す、消えるものの規模。"""

    world: WorldContext
    season_count: int
    game_count: int


# --- ペナントの能力の表示（P4c） ---


@dataclass(frozen=True)
class RatingCell:
    """能力ひとつぶんの表示。「B 74」の区分と数値。

    区分の境目と目立たせ方は domain の `RatingGrade` が出典で、ここは写すだけ。
    """

    key: str  # 項目の識別子（並べ替えのキーと同じ）
    label: str  # ミート・球威など
    value: int
    grade: str  # S〜G
    emphasis: RatingEmphasis


@dataclass(frozen=True)
class PlayerRatingsCard:
    """選手ページの「能力（年度）」カード。成長型は隠し値なので持たない。"""

    year: int
    is_pitcher: bool
    cells: tuple[RatingCell, ...]


@dataclass(frozen=True)
class RatingColumn:
    """能力の表の1列。key は並べ替えのキー。"""

    key: str
    label: str


@dataclass(frozen=True)
class RatingsRow:
    """球団の能力の表の1行。能力がまだ無い選手は cells が空（画面では「—」）。"""

    id: int
    number: int
    name: str
    position: str
    is_foreign_player: bool
    age: int | None
    cells: tuple[RatingCell, ...]
    # 野手: 打率・OPS。投手: 防御率（average の位置）・投球回
    batting_average: float = 0.0
    ops: float = 0.0
    earned_run_average: float = 0.0
    innings_pitched: str = ""


@dataclass(frozen=True)
class RatingsTable:
    """球団の能力の表（野手か投手のどちらか）。sort / descending は実際に使った並び。"""

    is_pitcher: bool
    year: int  # 能力の年度（試合・編成と同じ規則で決めた年）
    columns: tuple[RatingColumn, ...]
    rows: tuple[RatingsRow, ...]
    sort: str
    descending: bool


@dataclass(frozen=True)
class SeasonClosed:
    """シーズンを締めた結果。"""

    # 締めた年と、これから始まる年
    year: int
    next_year: int
    retired_count: int
    # 新人の人数と、そのうち外国人の人数
    draftee_count: int
    foreign_draftee_count: int
    # 翌年の能力を作らなかった選手の数（締める年の能力が無かった選手）
    without_ratings_count: int
    # 引退した選手を含んでいたため、自動に戻した編成の区画。受け持つ球団のぶん（無ければ空）
    released_sections: tuple[PlanSection, ...]
    # 自動に戻した球団の数（受け持ち以外も含む）
    released_club_count: int
    # 翌年の日程の試合数
    fixture_count: int


@dataclass(frozen=True)
class SeasonCloseOption:
    """「シーズンを締める」の出せる状態。画面が、ボタンを出すか理由を出すかを決める材料。"""

    # 締める年（最後に試合をした年）。まだ試合をしていなければ None
    year: int | None
    # 締めた後の年。締められないときも、年が分かれば入れる
    next_year: int | None
    can_close: bool
    # 締められない理由（締められるときは空）
    reason: str = ""
