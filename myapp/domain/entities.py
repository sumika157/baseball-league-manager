"""エンティティと集約。

集約ルートは Team。「同一チーム内で背番号は重複しない」という不変条件は
チーム全体を見ないと判定できないため、Team がロスターを保持して自ら保証する。
リポジトリはこの集約単位で読み書きする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .exceptions import (
    DuplicateJerseyNumber,
    InvalidGame,
    InvalidStint,
    PlayerNotFound,
)
from .value_objects import (
    BattingLine,
    JerseyNumber,
    PitchingLine,
    Position,
    Profile,
    Season,
    StadiumProfile,
)


@dataclass
class League:
    """リーグ。"""

    name: str
    id: int | None = None

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
    team_name: str = ''

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

    def overlaps(self, other: 'Stint') -> bool:
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

    def __str__(self) -> str:
        return f"{self.number} {self.name} ({self.position.label})"

    @property
    def is_pitcher(self) -> bool:
        return self.position.is_pitcher

    def rename(self, name: str) -> None:
        cleaned = (name or '').strip()
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

    def add_player(
        self, name: str, number: JerseyNumber, position: Position, from_year: int = None
    ) -> Player:
        """選手を加入させる。背番号が在籍中の選手と重複する場合は拒否する。"""
        self._ensure_number_is_available(number)

        player = Player(name=(name or '').strip(), number=number, position=position)
        if not player.name:
            from .exceptions import DomainError

            raise DomainError("選手名を入力してください。")

        player.career = [Stint(
            team_id=self.id,
            number=number,
            from_year=from_year if from_year is not None else date.today().year,
            team_name=self.name,
        )]
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

    def _ensure_number_is_available(
        self, number: JerseyNumber, excluding: Player | None = None
    ) -> None:
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
                    raise DuplicateJerseyNumber(
                        f"背番号 {number} は「{self.name}」で既に使用されています。"
                    )


@dataclass
class GameBatting:
    """1試合ぶんの、ある選手の打撃成績。"""

    player_id: int
    line: BattingLine
    id: int | None = None


@dataclass
class GamePitching:
    """1試合ぶんの、ある投手の投球成績。"""

    player_id: int
    line: PitchingLine
    id: int | None = None


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

    def __post_init__(self) -> None:
        if self.home_team_id == self.away_team_id:
            raise InvalidGame("同じチーム同士の試合は登録できません。")
        for label, value in (('ホームの得点', self.home_score), ('ビジターの得点', self.away_score)):
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
            return 'tie'
        return 'win' if self.winner_team_id == team_id else 'loss'

    def score_for(self, team_id: int) -> tuple[int, int]:
        """指定チームから見た (得点, 失点)。"""
        if not self.involves(team_id):
            raise InvalidGame("この試合に参加していないチームです。")
        if team_id == self.home_team_id:
            return self.home_score, self.away_score
        return self.away_score, self.home_score

    def record_batting(self, player_id: int, line: BattingLine) -> GameBatting:
        """選手の打撃成績を記録する。同じ選手が既にあれば上書きする。"""
        for entry in self.batting:
            if entry.player_id == player_id:
                entry.line = line
                return entry
        entry = GameBatting(player_id=player_id, line=line)
        self.batting.append(entry)
        return entry

    def record_pitching(self, player_id: int, line: PitchingLine) -> GamePitching:
        """投手の投球成績を記録する。同じ選手が既にあれば上書きする。"""
        for entry in self.pitching:
            if entry.player_id == player_id:
                entry.line = line
                return entry
        entry = GamePitching(player_id=player_id, line=line)
        self.pitching.append(entry)
        return entry

    # 成績の取り消しは、集約を組み直して保存することで表す。
    # 渡されなかった選手の行はリポジトリ側で消えるため、
    # 個別に取り消す操作は持たない（出典を1つに保つため）。
