"""エンティティと集約。

集約ルートは Team。「同一チーム内で背番号は重複しない」という不変条件は
チーム全体を見ないと判定できないため、Team がロスターを保持して自ら保証する。
リポジトリはこの集約単位で読み書きする。

Captaincy は Stint とロジック（is_current/overlaps/close）が同型だが、対象の異なる
別概念（在籍と主将在任は開始・終了のタイミングが一致しない）のため独立させている。
共通化の余地はあるが、Stint は既存テストの対象が多く触るリスクが大きいため見送った。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .exceptions import (
    DuplicateCaptain,
    DuplicateJerseyNumber,
    InvalidCaptaincy,
    InvalidGame,
    InvalidStint,
    PlayerNotEligibleForCaptaincy,
    PlayerNotFound,
)
from .value_objects import (
    BattingLine,
    FieldingPosition,
    JerseyNumber,
    LineScore,
    PitchingLine,
    Position,
    Profile,
    Season,
    StadiumProfile,
    ensure_quota_not_exceeded,
)


@dataclass
class League:
    """リーグ。"""

    name: str
    id: int | None = None
    # 外国人選手の枠。リーグごとにルールが異なりうるためここに持つ。None なら無制限
    foreign_player_roster_limit: int | None = None
    foreign_player_game_limit: int | None = None

    def __str__(self) -> str:
        return self.name


@dataclass
class Stadium:
    """球場。

    所在地はここが持つ。チーム側に本拠地の地名を別に持たせると、
    同じ事実の出典が2つになるため。
    """

    name: str
    id: int | None = None
    profile: StadiumProfile = field(default_factory=StadiumProfile)

    def __str__(self) -> str:
        return self.name

    @property
    def city(self) -> str:
        return self.profile.city


@dataclass
class Stint:
    """在籍。ある選手が、あるチームに、いつからいつまで在籍したか。

    背番号も在籍ごとに持つ。移籍で変わるため選手そのものには持たせない。
    """

    team_id: int
    number: JerseyNumber
    from_year: int
    to_year: int | None = None
    id: int | None = None
    team_name: str = ""

    def __post_init__(self) -> None:
        self.from_year = Season(self.from_year).year
        if self.to_year is not None:
            self.to_year = Season(self.to_year).year
            if self.to_year < self.from_year:
                raise InvalidStint("退団年が加入年より前になっています。")

    def __str__(self) -> str:
        return f"{self.from_year}〜{self.to_year or '現在'}"

    @property
    def is_current(self) -> bool:
        return self.to_year is None

    def covers(self, year: int) -> bool:
        return self.from_year <= year and (self.to_year is None or year <= self.to_year)

    def overlaps(self, other: Stint) -> bool:
        """期間が重なるか。片方でも在籍中（to_year が空）なら無限として扱う。"""
        end, other_end = self.to_year, other.to_year
        starts_before_other_ends = other_end is None or self.from_year <= other_end
        other_starts_before_end = end is None or other.from_year <= end
        return starts_before_other_ends and other_starts_before_end

    def close(self, year: int) -> None:
        """退団させる。"""
        season = Season(year).year
        if season < self.from_year:
            raise InvalidStint("加入年より前の年で退団にはできません。")
        self.to_year = season


@dataclass
class Captaincy:
    """主将在任。あるチームで、いつからいつまで主将だったか。

    在籍（Stint）とは開始・終了のタイミングが一致しない別軸の期間のため、
    Stint を再利用せず並列の型として持つ（ロジックは同型だが対象が異なる）。
    """

    team_id: int
    from_year: int
    to_year: int | None = None
    id: int | None = None
    team_name: str = ""

    def __post_init__(self) -> None:
        self.from_year = Season(self.from_year).year
        if self.to_year is not None:
            self.to_year = Season(self.to_year).year
            if self.to_year < self.from_year:
                raise InvalidCaptaincy("退任年が就任年より前になっています。")

    def __str__(self) -> str:
        return f"{self.from_year}〜{self.to_year or '現在'}"

    @property
    def is_current(self) -> bool:
        return self.to_year is None

    def overlaps(self, other: Captaincy) -> bool:
        """期間が重なるか。片方でも在任中（to_year が空）なら無限として扱う。"""
        end, other_end = self.to_year, other.to_year
        starts_before_other_ends = other_end is None or self.from_year <= other_end
        other_starts_before_end = end is None or other.from_year <= end
        return starts_before_other_ends and other_starts_before_end

    def close(self, year: int) -> None:
        """解任する。"""
        season = Season(year).year
        if season < self.from_year:
            raise InvalidCaptaincy("就任年より前の年で解任にはできません。")
        self.to_year = season


@dataclass
class Player:
    """選手。Team 集約の内部エンティティ。

    打撃成績と投球成績を常に両方保持する（未記録なら 0 の行）。
    こうすることで「投手に転向したら打撃成績のレコードが無い」といった
    欠損状態が構造的に発生しなくなる。
    """

    name: str
    number: JerseyNumber
    position: Position
    id: int | None = None
    is_active: bool = True
    profile: Profile = field(default_factory=Profile)
    batting: BattingLine = field(default_factory=BattingLine)
    pitching: PitchingLine = field(default_factory=PitchingLine)
    # 経歴。number と is_active は、このうち現在の在籍から導いた値
    career: list[Stint] = field(default_factory=list)
    # 主将在任歴。career と同様、生涯を通じた経歴として選手自身が持つ
    captaincies: list[Captaincy] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.number} {self.name} ({self.position.label})"

    @property
    def is_pitcher(self) -> bool:
        return self.position.is_pitcher

    def rename(self, name: str) -> None:
        cleaned = (name or "").strip()
        if not cleaned:
            from .exceptions import DomainError

            raise DomainError("選手名を入力してください。")
        self.name = cleaned

    def change_position(self, position: Position) -> None:
        self.position = position

    def record_batting(self, line: BattingLine) -> None:
        self.batting = line

    def record_pitching(self, line: PitchingLine) -> None:
        self.pitching = line

    def retire(self) -> None:
        self.is_active = False


@dataclass
class Team:
    """チーム。集約ルート。

    ロスター（選手一覧）を内部に持ち、背番号の一意性を保証する。
    勝敗やシーズン成績は保持しない。試合（Game）から集計して求める。
    """

    name: str
    league_id: int | None = None
    # 本拠地。所在地は球場が持つので、チーム側に地名は持たない
    home_stadium_id: int | None = None
    id: int | None = None
    # リーグ内での表示順。管理画面から手動で並べ替える
    display_order: int = 0
    players: list[Player] = field(default_factory=list)

    def __str__(self) -> str:
        return self.name

    # --- ロスターの参照 ---

    @property
    def active_players(self) -> list[Player]:
        return [p for p in self.players if p.is_active]

    def find_player(self, player_id: int) -> Player:
        for player in self.players:
            if player.id == player_id:
                return player
        raise PlayerNotFound(f"選手が見つかりません（id={player_id}）。")

    def batters_by_ops(self) -> list[Player]:
        """野手を OPS の高い順（同率なら打率順）に並べる。"""
        batters = [p for p in self.active_players if not p.is_pitcher]
        return sorted(
            batters,
            key=lambda p: (p.batting.ops, p.batting.batting_average),
            reverse=True,
        )

    def pitchers_by_era(self) -> list[Player]:
        """投手を防御率の低い順に並べる。

        未登板（投球回0）は防御率0となり不当に上位へ来るため末尾に回す。
        """
        pitchers = [p for p in self.active_players if p.is_pitcher]
        return sorted(
            pitchers,
            key=lambda p: (p.pitching.innings.outs == 0, p.pitching.earned_run_average),
        )

    # --- ロスターの変更（不変条件を守る） ---

    def add_player(self, name: str, number: JerseyNumber, position: Position, from_year: int = None) -> Player:
        """選手を加入させる。背番号が在籍中の選手と重複する場合は拒否する。"""
        self._ensure_number_is_available(number)

        player = Player(name=(name or "").strip(), number=number, position=position)
        if not player.name:
            from .exceptions import DomainError

            raise DomainError("選手名を入力してください。")

        player.career = [
            Stint(
                team_id=self.id,
                number=number,
                from_year=from_year if from_year is not None else date.today().year,
                team_name=self.name,
            )
        ]
        self.players.append(player)
        return player

    def change_player_number(self, player: Player, number: JerseyNumber) -> None:
        """背番号を変更する。在籍中の他の選手と重複する場合は拒否する。"""
        if player.number == number:
            return
        self._ensure_number_is_available(number, excluding=player)
        player.number = number
        current = self.current_stint(player)
        if current is not None:
            current.number = number

    def current_stint(self, player: Player) -> Stint | None:
        """このチームでの現在の在籍。"""
        for stint in player.career:
            if stint.team_id == self.id and stint.is_current:
                return stint
        return None

    def retire_player(self, player: Player, year: int = None) -> None:
        """退団させる。在籍期間を閉じることで、背番号が空く。"""
        player.retire()
        current = self.current_stint(player)
        if current is not None:
            current.close(year if year is not None else date.today().year)
        # 在籍していないのに主将、という両立しない状態を残さない
        self.remove_captain(player, year)

    def _ensure_number_is_available(self, number: JerseyNumber, excluding: Player | None = None) -> None:
        """在籍期間が重なる選手どうしで背番号が重複しないことを確かめる。

        過去に同じ番号を付けた選手がいても、期間が重なっていなければ問題ない。
        """
        for player in self.players:
            if player is excluding:
                continue
            for stint in player.career:
                if stint.team_id != self.id or not stint.is_current:
                    continue
                if stint.number == number:
                    raise DuplicateJerseyNumber(f"背番号 {number} は「{self.name}」で既に使用されています。")

    # --- 主将の指名・解任（不変条件を守る） ---

    def appoint_captain(self, player: Player, year: int = None) -> None:
        """主将に指名する。在籍していない選手や、既に他の選手が主将の場合は拒否する。"""
        if self.current_stint(player) is None:
            raise PlayerNotEligibleForCaptaincy(f"「{self.name}」に在籍していない選手を主将にはできません。")
        if self.current_captain is player:
            return
        self._ensure_no_current_captain(excluding=player)
        player.captaincies.append(
            Captaincy(
                team_id=self.id,
                team_name=self.name,
                from_year=year if year is not None else date.today().year,
            )
        )

    def remove_captain(self, player: Player, year: int = None) -> None:
        """主将を解任する。主将でなければ何もしない。"""
        current = self._current_captaincy(player)
        if current is not None:
            current.close(year if year is not None else date.today().year)

    @property
    def current_captain(self) -> Player | None:
        return next((p for p in self.players if self._current_captaincy(p) is not None), None)

    def _current_captaincy(self, player: Player) -> Captaincy | None:
        return next(
            (c for c in player.captaincies if c.team_id == self.id and c.is_current),
            None,
        )

    def _ensure_no_current_captain(self, excluding: Player | None = None) -> None:
        """このチームに同時に主将は1人まで。ロスター全体を見て検査する。"""
        for player in self.players:
            if player is excluding:
                continue
            if self._current_captaincy(player) is not None:
                raise DuplicateCaptain(f"「{self.name}」には既に主将（{player.name}）がいます。")

    # --- 外国人枠 ---

    @property
    def foreign_player_count(self) -> int:
        return sum(1 for p in self.active_players if p.profile.is_foreign_player)

    def ensure_foreign_player_quota(self, limit: int | None) -> None:
        """現在のロスターが外国人枠の上限を超えていないか確認する。

        呼び出し側は、検査したい変更（選手追加・移籍受け入れ・国籍フラグ変更）を
        保存前の roster に反映してから呼ぶ。超えていれば例外を投げ、
        呼び出し元のサービスがそれ以降の保存処理を止める。
        """
        ensure_quota_not_exceeded(
            self.foreign_player_count,
            limit,
            f"「{self.name}」の外国人選手登録数が上限（{limit}人）を超えています。",
        )


@dataclass
class GameBatting:
    """1試合ぶんの、ある選手の打撃成績と、打線での位置づけ。

    打順のどこに入り、その打順の何番目に出て、どこを守ったか。ボックススコアは
    この3つで並びが決まる（1番から順に、同じ打順は交代の順に並べる）。
    """

    player_id: int
    line: BattingLine
    id: int | None = None
    # 打順（1〜9）。記録しない試合もあるため未設定を許す
    batting_order: int | None = None
    # 同じ打順の何番目か。0 がスタメンで、1以上は途中出場
    slot_sequence: int = 0
    fielding_position: FieldingPosition | None = None

    def __post_init__(self) -> None:
        if self.batting_order is not None and not (1 <= self.batting_order <= 9):
            raise InvalidGame("打順は1〜9で入力してください。")
        if self.slot_sequence < 0:
            raise InvalidGame("交代の順に負の値は指定できません。")

    @property
    def is_starter(self) -> bool:
        """スタメンか。打順の先頭に入っていればスタメン。

        別に旗を持たせると「交代なのにスタメン」という食い違いが起こりうるため、
        交代の順から導く。
        """
        return self.slot_sequence == 0

    @property
    def position_label(self) -> str:
        return self.fielding_position.label if self.fielding_position else ""


@dataclass
class GamePitching:
    """1試合ぶんの、ある投手の投球成績と、登板の順番。

    登板順は先発を1とする。ボックススコアは投げた順に並べるため、
    順番を持たないと先発と抑えの区別が付かない。
    """

    player_id: int
    line: PitchingLine
    id: int | None = None
    appearance_order: int = 1
    # 何回から投げたか。勝敗・セーブ・ホールドは登板した時点のスコアで決まるため、
    # 登板順だけでは足りない（何回のスコアを見るかが決まらない）
    entered_inning: int = 1

    def __post_init__(self) -> None:
        if self.appearance_order < 1:
            raise InvalidGame("登板順は1以上で入力してください。")
        if self.entered_inning < 1:
            raise InvalidGame("登板した回は1以上で入力してください。")

    @property
    def is_starter(self) -> bool:
        return self.appearance_order == 1


@dataclass
class Game:
    """試合。集約ルート。

    2つのチームにまたがるため Team の内部には置けず、独立した集約とする。
    チームの勝敗も選手の通算成績も、すべてここから集計して求める。
    試合が唯一の出典であり、手入力の勝敗や通算値は持たない。
    """

    season: Season
    played_on: date
    home_team_id: int
    away_team_id: int
    home_score: int = 0
    away_score: int = 0
    id: int | None = None
    batting: list[GameBatting] = field(default_factory=list)
    pitching: list[GamePitching] = field(default_factory=list)
    # 回ごとの得点。勝敗・セーブ・ホールドの判定に使う。空でも試合は成立する
    # （経過を記録しない試合では、勝敗も導けないだけ）
    line_score: LineScore = field(default_factory=LineScore)

    def __post_init__(self) -> None:
        if self.home_team_id == self.away_team_id:
            raise InvalidGame("同じチーム同士の試合は登録できません。")
        for label, value in (("ホームの得点", self.home_score), ("ビジターの得点", self.away_score)):
            try:
                score = int(value)
            except (TypeError, ValueError):
                raise InvalidGame(f"{label}は数値で入力してください。") from None
            if score < 0:
                raise InvalidGame(f"{label}に負の値は入力できません。")

    def __str__(self) -> str:
        return f"{self.played_on} {self.home_score}-{self.away_score}"

    @property
    def is_tie(self) -> bool:
        return self.home_score == self.away_score

    @property
    def winner_team_id(self) -> int | None:
        """勝ったチーム。引分なら None。"""
        if self.is_tie:
            return None
        return self.home_team_id if self.home_score > self.away_score else self.away_team_id

    def involves(self, team_id: int) -> bool:
        return team_id in (self.home_team_id, self.away_team_id)

    def result_for(self, team_id: int) -> str:
        """指定チームから見た結果。'win' / 'loss' / 'tie'。"""
        if not self.involves(team_id):
            raise InvalidGame("この試合に参加していないチームです。")
        if self.is_tie:
            return "tie"
        return "win" if self.winner_team_id == team_id else "loss"

    def score_for(self, team_id: int) -> tuple[int, int]:
        """指定チームから見た (得点, 失点)。"""
        if not self.involves(team_id):
            raise InvalidGame("この試合に参加していないチームです。")
        if team_id == self.home_team_id:
            return self.home_score, self.away_score
        return self.away_score, self.home_score

    def record_batting(
        self,
        player_id: int,
        line: BattingLine,
        *,
        batting_order: int | None = None,
        slot_sequence: int = 0,
        fielding_position: FieldingPosition | None = None,
    ) -> GameBatting:
        """選手の打撃成績を記録する。同じ選手が既にあれば上書きする。"""
        for entry in self.batting:
            if entry.player_id == player_id:
                entry.line = line
                entry.batting_order = batting_order
                entry.slot_sequence = slot_sequence
                entry.fielding_position = fielding_position
                return entry
        entry = GameBatting(
            player_id=player_id,
            line=line,
            batting_order=batting_order,
            slot_sequence=slot_sequence,
            fielding_position=fielding_position,
        )
        self.batting.append(entry)
        return entry

    def record_pitching(
        self,
        player_id: int,
        line: PitchingLine,
        *,
        appearance_order: int = 1,
        entered_inning: int = 1,
    ) -> GamePitching:
        """投手の投球成績を記録する。同じ選手が既にあれば上書きする。"""
        for entry in self.pitching:
            if entry.player_id == player_id:
                entry.line = line
                entry.appearance_order = appearance_order
                entry.entered_inning = entered_inning
                return entry
        entry = GamePitching(
            player_id=player_id,
            line=line,
            appearance_order=appearance_order,
            entered_inning=entered_inning,
        )
        self.pitching.append(entry)
        return entry

    def ensure_line_score_matches(self) -> None:
        """イニングスコアの合計が最終得点と一致することを確かめる。

        両方を持つと食い違いうるが、イニングスコアは記録されない試合もあるため
        最終得点を残している。空でない場合だけ照合する。
        """
        if self.line_score.is_empty:
            return
        if not self.line_score.matches(self.away_score, self.home_score):
            raise InvalidGame(
                "イニングスコアの合計が得点と一致しません"
                f"（ビジター {self.line_score.away_total}/{self.away_score}、"
                f"ホーム {self.line_score.home_total}/{self.home_score}）。"
            )

    def batting_in_order(self) -> list[GameBatting]:
        """ボックススコアの並び。打順の順に、同じ打順は交代の順に並べる。

        打順が未記録の行は末尾に回す（記録の無いものを先頭に置くと、
        1番打者が誰なのか読めなくなる）。
        """
        return sorted(
            self.batting,
            key=lambda e: (e.batting_order is None, e.batting_order or 0, e.slot_sequence),
        )

    def pitching_in_order(self) -> list[GamePitching]:
        """ボックススコアの並び。投げた順に並べる。"""
        return sorted(self.pitching, key=lambda e: e.appearance_order)

    # 成績の取り消しは、集約を組み直して保存することで表す。
    # 渡されなかった選手の行はリポジトリ側で消えるため、
    # 個別に取り消す操作は持たない（出典を1つに保つため）。
