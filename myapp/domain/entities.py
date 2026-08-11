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
    InvalidPlateAppearance,
    InvalidStint,
    PlayerNotEligibleForCaptaincy,
    PlayerNotFound,
)
from .value_objects import (
    AdvanceReason,
    Base,
    BattingLine,
    ErrorKind,
    FieldingPosition,
    JerseyNumber,
    LineScore,
    PitchingLine,
    PlateAppearanceResult,
    Position,
    Profile,
    Season,
    StadiumProfile,
    ensure_quota_not_exceeded,
)

OUTS_PER_HALF_INNING = 3
BATTING_ORDER_SIZE = 9


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

    def add_player(self, name: str, number: JerseyNumber, position: Position, from_year: int | None = None) -> Player:
        """選手を加入させる。背番号が在籍中の選手と重複する場合は拒否する。"""
        assert self.id is not None, "ロスターの変更は保存済みのチームに対して行う"
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

    def retire_player(self, player: Player, year: int | None = None) -> None:
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

    def appoint_captain(self, player: Player, year: int | None = None) -> None:
        """主将に指名する。在籍していない選手や、既に他の選手が主将の場合は拒否する。"""
        assert self.id is not None, "主将の指名は保存済みのチームに対して行う"
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

    def remove_captain(self, player: Player, year: int | None = None) -> None:
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


@dataclass(frozen=True)
class RunnerAdvance:
    """1人の走者が、この打席の中でどこからどこへ動いたか。

    打者自身も走者として記録する（`from_base` は `Base.BATTER`）。得点・打点・盗塁・
    残塁・自責点はすべてこの記録から導く。動かなかった走者は記録しない。
    """

    runner_id: int
    from_base: Base
    to_base: Base
    reason: AdvanceReason
    # 失策に起因する進塁なら、同じ打席の errors の位置。自責点の判定に使う
    error_index: int | None = None

    def __post_init__(self) -> None:
        if self.from_base in (Base.OUT, Base.HOME):
            raise InvalidPlateAppearance(f"{self.from_base.label}の走者は進塁できません。")
        if self.reason.is_out != self.to_base.is_out:
            raise InvalidPlateAppearance(
                f"進塁の理由（{self.reason.label}）と到達（{self.to_base.label}）が食い違っています。"
            )
        if not self.to_base.is_out and self.to_base.value <= self.from_base.value:
            raise InvalidPlateAppearance(f"走者は{self.from_base.label}から{self.to_base.label}へは進めません。")
        if self.error_index is not None and self.error_index < 0:
            raise InvalidPlateAppearance("失策の位置に負の値は指定できません。")

    @property
    def is_out(self) -> bool:
        return self.to_base.is_out

    @property
    def has_scored(self) -> bool:
        return self.to_base.has_scored

    @property
    def is_batter(self) -> bool:
        """打者自身の進塁か。"""
        return self.from_base is Base.BATTER


@dataclass(frozen=True)
class RunnerSubstitution:
    """代走。塁上の走者を別の選手に入れ替える。

    交代は進塁ではないため `RunnerAdvance` では表せない。塁の状態を再生するときに
    走者が入れ替わっていないと「その塁にいない走者が進んだ」と誤って弾いてしまう。
    失点の責任投手は交代前の走者から引き継ぐ。
    """

    base: Base
    leaving_runner_id: int
    entering_runner_id: int

    def __post_init__(self) -> None:
        if not self.base.occupies_base:
            raise InvalidPlateAppearance("代走は塁上の走者にだけ出せます。")
        if self.leaving_runner_id == self.entering_runner_id:
            raise InvalidPlateAppearance("同じ選手に代走は出せません。")


@dataclass(frozen=True)
class FieldingError:
    """失策。誰がどこで何をしたか。

    自責点の判定（規則 9.16 の「失策が無かったものと仮定した再構成」）と、
    守備成績の出典。
    """

    player_id: int
    position: FieldingPosition
    kind: ErrorKind


@dataclass
class PlateAppearance:
    """1打席。スコアブックのマス目1つにあたる。

    打撃成績・投球成績・守備成績・イニングスコアはすべてここから導出する
    （導出は `domain.services.scoring`）。`sequence` が試合内の時系列の唯一の出典で、
    アウトの数も塁の状態も並び順を再生して求めるため、ここには持たない。
    """

    sequence: int
    inning: int
    is_bottom: bool
    batter_id: int
    pitcher_id: int
    batting_order: int
    result: PlateAppearanceResult
    slot_sequence: int = 0
    # 打球の処理経路（6-3 なら (遊, 一)）。刺殺・補殺の出典
    fielded_by: tuple[FieldingPosition, ...] = ()
    advances: list[RunnerAdvance] = field(default_factory=list)
    substitutions: list[RunnerSubstitution] = field(default_factory=list)
    errors: list[FieldingError] = field(default_factory=list)
    id: int | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise InvalidPlateAppearance("打席の通し番号は1以上で入力してください。")
        if self.inning < 1:
            raise InvalidPlateAppearance("回は1以上で入力してください。")
        if not (1 <= self.batting_order <= BATTING_ORDER_SIZE):
            raise InvalidPlateAppearance(f"打順は1〜{BATTING_ORDER_SIZE}で入力してください。")
        if self.slot_sequence < 0:
            raise InvalidPlateAppearance("交代の順に負の値は指定できません。")
        self._ensure_batter_advance_matches_result()
        self._ensure_result_requirements()

    def __str__(self) -> str:
        half = "裏" if self.is_bottom else "表"
        return f"{self.inning}回{half} {self.batting_order}番 {self.result.label}"

    def _ensure_batter_advance_matches_result(self) -> None:
        """打者の進塁が1つだけあり、打席の結果と噛み合っていることを確かめる。"""
        moves = [advance for advance in self.advances if advance.is_batter]
        if not moves:
            raise InvalidPlateAppearance("打者の進塁が記録されていません。")
        if len(moves) > 1:
            raise InvalidPlateAppearance("1打席に打者の進塁を2つ以上は記録できません。")

        moved = moves[0]
        if moved.runner_id != self.batter_id:
            raise InvalidPlateAppearance("打者の進塁の選手が打者と一致していません。")
        if self.result.retires_batter and not moved.is_out:
            raise InvalidPlateAppearance(f"{self.result.label}なのに打者が{moved.to_base.label}に達しています。")
        if self.result is PlateAppearanceResult.HOME_RUN and not moved.has_scored:
            raise InvalidPlateAppearance("本塁打なのに打者が本塁に達していません。")
        # 単打で二塁を陥れることはあるので下限だけを見る。走塁死は結果と両立する
        if self.result.is_hit and not moved.is_out and moved.to_base.value < self.result.bases:
            raise InvalidPlateAppearance(
                f"{self.result.label}なのに打者が{moved.to_base.label}までしか進んでいません。"
            )

    def _ensure_result_requirements(self) -> None:
        """結果の種別が要求する条件を確かめる。"""
        for advance in self.advances:
            if advance.error_index is not None and advance.error_index >= len(self.errors):
                raise InvalidPlateAppearance("進塁が参照している失策が記録されていません。")

        if self.result is PlateAppearanceResult.REACHED_ON_ERROR and not self.errors:
            raise InvalidPlateAppearance("失策出塁には失策の記録が必要です。")
        # 犠飛は走者が還って初めて成立し、犠打は走者が進んで初めて成立する（規則 9.08）
        if self.result is PlateAppearanceResult.SACRIFICE_FLY and self.runs_scored == 0:
            raise InvalidPlateAppearance("犠飛は走者が本塁に達していないと記録できません。")
        if self.result is PlateAppearanceResult.SACRIFICE_BUNT and not self._runners_advanced:
            raise InvalidPlateAppearance("犠打は走者が進んでいないと記録できません。")

    @property
    def _runners_advanced(self) -> bool:
        return any(not advance.is_batter and not advance.is_out for advance in self.advances)

    @property
    def half_inning(self) -> tuple[int, bool]:
        """どの半回か。回と表裏の組。"""
        return (self.inning, self.is_bottom)

    @property
    def batter_advance(self) -> RunnerAdvance:
        """打者自身の進塁。生成時に1つだけあることを保証している。"""
        return next(advance for advance in self.advances if advance.is_batter)

    @property
    def outs_recorded(self) -> int:
        """この打席で記録されたアウトの数。投球回と半回の終わりの出典。"""
        return sum(1 for advance in self.advances if advance.is_out)

    @property
    def runs_scored(self) -> int:
        """この打席で本塁に達した走者の数。"""
        return sum(1 for advance in self.advances if advance.has_scored)

    @property
    def is_double_play(self) -> bool:
        """併殺（以上）か。アウトが2つ以上記録されたかで判断する。"""
        return self.outs_recorded >= 2

    @property
    def runs_batted_in(self) -> int:
        """打点。

        打者の打撃行為の結果として還った得点だけを数える。失策・野選・暴投・捕逸・
        盗塁で還った得点には付かない。併殺の間に還った得点にも付かない（規則 9.04）。
        """
        if self.is_double_play:
            return 0
        return sum(1 for advance in self.advances if advance.has_scored and advance.reason.earns_run_batted_in)

    def scoring_advances(self) -> list[RunnerAdvance]:
        """本塁に達した進塁。失点・自責点の判定に使う。"""
        return [advance for advance in self.advances if advance.has_scored]

    def advances_lead_runner_first(self) -> list[RunnerAdvance]:
        """先の塁の走者から順に並べた進塁。

        塁の状態を再生するときは、前を走る走者が塁を空けてから後続が入る順で
        適用しなければならない（一塁走者を先に二塁へ動かすと、二塁走者がまだ
        居るために衝突と誤判定される）。
        """
        return sorted(self.advances, key=lambda advance: -advance.from_base.value)


def winning_team_id(home_team_id: int, away_team_id: int, home_score: int, away_score: int) -> int | None:
    """得点から勝ったチームを決める。同点なら None（引分）。

    「どちらが勝ちか」の唯一の出典。集約（Game.winner_team_id）と、集約を
    組み立てずに一覧を作る参照クエリの両方がここを通る。
    """
    if home_score == away_score:
        return None
    return home_team_id if home_score > away_score else away_team_id


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
    # 打席ごとの記録。スコアブックのマス目にあたり、記録があれば打撃・投球・守備成績と
    # イニングスコアはすべてここから導出できる。イニングスコアと同じく空でも試合は成立する
    plate_appearances: list[PlateAppearance] = field(default_factory=list)

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
        return winning_team_id(self.home_team_id, self.away_team_id, self.home_score, self.away_score)

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

    def plate_appearances_in_order(self) -> list[PlateAppearance]:
        """打席を試合の進行順に並べる。通し番号が時系列の出典。"""
        return sorted(self.plate_appearances, key=lambda entry: entry.sequence)

    def derived_line_score(self) -> LineScore:
        """打席の記録から回ごとの得点を組み立てる。

        イニングスコアの出典を打席に一本化するためのもの。得点が無かった半回も
        「行われた」なら 0 として残し、ホームが攻めずに終わった最終回は記録しない
        （`LineScore` が表と裏で長さの違いを許すのはこのため）。
        打席の記録が無い試合では空を返す（手入力の `line_score` をそのまま使う）。
        """
        if not self.plate_appearances:
            return LineScore()

        away: dict[int, int] = {}
        home: dict[int, int] = {}
        for entry in self.plate_appearances:
            half = home if entry.is_bottom else away
            half[entry.inning] = half.get(entry.inning, 0) + entry.runs_scored

        def to_tuple(half: dict[int, int]) -> tuple[int, ...]:
            if not half:
                return ()
            return tuple(half.get(inning, 0) for inning in range(1, max(half) + 1))

        return LineScore(away=to_tuple(away), home=to_tuple(home))

    def ensure_plate_appearances_consistent(self) -> None:
        """打席の記録がスコアブックとして成立していることを確かめる。

        紙のスコアブックで縦計・横計を取って検算する作業にあたる。記録が無い試合では
        何もしない（経過を記録しない試合もあるため）。
        """
        if not self.plate_appearances:
            return
        ordered = self.plate_appearances_in_order()
        self._ensure_sequences_are_contiguous(ordered)
        self._ensure_batting_order_cycles(ordered)
        self._replay_bases(ordered)
        self._ensure_derived_line_score_matches()

    @staticmethod
    def _ensure_sequences_are_contiguous(ordered: list[PlateAppearance]) -> None:
        """通し番号が1から欠けも重複もなく続いていることを確かめる。

        番号が飛ぶと打席が抜け落ちていることになり、塁の状態を再生できない。
        """
        for expected, entry in enumerate(ordered, start=1):
            if entry.sequence != expected:
                raise InvalidPlateAppearance(
                    f"打席の通し番号が連続していません（{expected} のはずが {entry.sequence}）。"
                )

    @staticmethod
    def _ensure_batting_order_cycles(ordered: list[PlateAppearance]) -> None:
        """打順が1〜9を巡回していることを確かめる。

        スコアブックを横に読む性質そのもので、打席の抜け・重複を強く捕まえる。
        攻守が入れ替わっても打線は続くため、チーム（表裏）ごとに追う。
        """
        previous: dict[bool, int] = {}
        for entry in ordered:
            last = previous.get(entry.is_bottom)
            if last is not None:
                expected = last % BATTING_ORDER_SIZE + 1
                if entry.batting_order != expected:
                    half = "裏" if entry.is_bottom else "表"
                    raise InvalidPlateAppearance(
                        f"{entry.inning}回{half}の打順が飛んでいます（{expected}番のはずが{entry.batting_order}番）。"
                    )
            previous[entry.is_bottom] = entry.batting_order

    @staticmethod
    def _replay_bases(ordered: list[PlateAppearance]) -> None:
        """塁の状態を打席順に再生し、走者とアウトの整合を確かめる。

        検査するのは3点。同じ塁に2人の走者がいないこと、その塁にいない走者が
        進んでいないこと、1つの半回のアウトが3を超えないこと。
        """
        occupied: dict[Base, int] = {}
        outs = 0
        current: tuple[int, bool] | None = None

        for entry in ordered:
            if entry.half_inning != current:
                current = entry.half_inning
                occupied = {}
                outs = 0

            half = "裏" if entry.is_bottom else "表"
            where = f"{entry.inning}回{half}{entry.batting_order}番"

            for substitution in entry.substitutions:
                if occupied.get(substitution.base) != substitution.leaving_runner_id:
                    raise InvalidPlateAppearance(f"{where}: 代走を出す走者が{substitution.base.label}にいません。")
                occupied[substitution.base] = substitution.entering_runner_id

            for advance in entry.advances_lead_runner_first():
                if advance.is_batter:
                    pass
                elif occupied.get(advance.from_base) != advance.runner_id:
                    raise InvalidPlateAppearance(f"{where}: 進塁した走者が{advance.from_base.label}にいません。")
                else:
                    del occupied[advance.from_base]

                if advance.to_base.occupies_base:
                    if advance.to_base in occupied:
                        raise InvalidPlateAppearance(f"{where}: {advance.to_base.label}に走者が2人います。")
                    occupied[advance.to_base] = advance.runner_id

            outs += entry.outs_recorded
            if outs > OUTS_PER_HALF_INNING:
                raise InvalidPlateAppearance(
                    f"{entry.inning}回{half}のアウトが{outs}になっています（1つの半回は{OUTS_PER_HALF_INNING}まで）。"
                )

    def _ensure_derived_line_score_matches(self) -> None:
        """打席から導いた得点が最終得点と一致することを確かめる。"""
        derived = self.derived_line_score()
        if not derived.matches(self.away_score, self.home_score):
            raise InvalidPlateAppearance(
                "打席の記録から導いた得点が最終得点と一致しません"
                f"（ビジター {derived.away_total}/{self.away_score}、"
                f"ホーム {derived.home_total}/{self.home_score}）。"
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
