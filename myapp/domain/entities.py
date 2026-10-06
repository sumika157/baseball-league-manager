"""エンティティと集約。

集約ルートは Team。「同一チーム内で背番号は重複しない」という不変条件は
チーム全体を見ないと判定できないため、Team がロスターを保持して自ら保証する。
リポジトリはこの集約単位で読み書きする。

Captaincy は Stint とロジック（is_current/overlaps/close）が同型だが、対象の異なる
別概念（在籍と主将在任は開始・終了のタイミングが一致しない）のため独立させている。
共通化の余地はあるが、Stint は既存テストの対象が多く触るリスクが大きいため見送った。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from .exceptions import (
    DuplicateCaptain,
    DuplicateJerseyNumber,
    InvalidAcquisition,
    InvalidCaptaincy,
    InvalidContract,
    InvalidFreeAgentDeclaration,
    InvalidGame,
    InvalidPlateAppearance,
    InvalidStint,
    PlayerNotEligibleForCaptaincy,
    PlayerNotFound,
    RegisteredPlayerLimitExceeded,
)
from .value_objects import (
    DEFAULT_REGISTERED_PLAYER_LIMIT,
    AcquisitionRoute,
    AdvanceReason,
    Base,
    BattingLine,
    ContractStatus,
    ErrorKind,
    FieldingLine,
    FieldingPosition,
    FreeAgencyKind,
    FreeAgencyOutcome,
    JerseyNumber,
    LineScore,
    PitchingLine,
    PlateAppearanceResult,
    Position,
    Profile,
    Season,
    StadiumProfile,
    StintPeriod,
    ensure_quota_not_exceeded,
    fa_destinations,
    free_agency_outcome,
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
    # 支配下選手の登録上限。NPB は70人。None なら無制限
    registered_player_limit: int | None = DEFAULT_REGISTERED_PLAYER_LIMIT

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


def jersey_number_in(
    number: JerseyNumber, promoted_year: int | None, number_before_promotion: JerseyNumber | None, year: int
) -> JerseyNumber:
    """その年の背番号。**出典はここだけ**（`Stint.number_in` も参照クエリの材料もこれを呼ぶ）。

    昇格より前の年は昇格前の番号、それ以降は今の番号。
    """
    if promoted_year is not None and number_before_promotion is not None and year < promoted_year:
        return number_before_promotion
    return number


@dataclass
class Stint:
    """在籍。ある選手が、あるチームに、いつからいつまで在籍したか。

    背番号も在籍ごとに持つ。移籍で変わるため選手そのものには持たせない。

    契約区分（支配下／育成）も在籍ごとの事実で、試合からは導けない入力の値。
    加入時の区分（signed_as）と、育成から支配下に上がった年（promoted_year）で持ち、
    ある年の区分は contract_in(year) で導く。背番号は昇格で変わるため、
    number は常に最新の区分に合う番号で、昇格前（育成だった間）の番号は
    number_before_promotion に残す（年ごとの番号は number_in(year)）。
    """

    team_id: int
    number: JerseyNumber
    from_year: int
    to_year: int | None = None
    id: int | None = None
    team_name: str = ""
    signed_as: ContractStatus = ContractStatus.REGISTERED
    promoted_year: int | None = None
    # 昇格する前（育成だった間）の背番号。昇格していなければ None。
    # 昇格で number が支配下の番号に変わるので、育成だった年の番号はここに残す
    number_before_promotion: JerseyNumber | None = None
    # 入団の経路。None は不明（推測で埋めない）。既定値つきなので、経路を知らない呼び出し元は壊れない
    acquired_via: AcquisitionRoute | None = None

    def __post_init__(self) -> None:
        self.from_year = Season(self.from_year).year
        if self.acquired_via is not None:
            # 経路と加入時の区分の食い違い（育成ドラフトなのに支配下など）を弾く
            self.acquired_via.ensure_fits_contract(self.signed_as)
        if self.to_year is not None:
            self.to_year = Season(self.to_year).year
            if self.to_year < self.from_year:
                raise InvalidStint("退団年が加入年より前になっています。")
        if self.promoted_year is not None:
            self.promoted_year = Season(self.promoted_year).year
            self._ensure_promotion_year_is_valid(self.promoted_year)
            # 昇格した記録には、昇格前の番号を必ず添える（過去の昇格を手入力する場合も同じ）。
            # 無いと、育成だった年に誰がどの番号を着けていたか分からなくなる
            if self.number_before_promotion is None:
                raise InvalidContract("支配下登録の年を入れるときは、昇格前の背番号も入力してください。")
        elif self.number_before_promotion is not None:
            raise InvalidContract("昇格前の背番号は、支配下登録の年があるときだけ設定できます。")

    def __str__(self) -> str:
        return f"{self.from_year}〜{self.to_year or '現在'}"

    @property
    def is_current(self) -> bool:
        return self.to_year is None

    def _ensure_promotion_year_is_valid(self, year: int) -> None:
        if self.signed_as is not ContractStatus.DEVELOPMENTAL:
            raise InvalidContract("支配下で加入した選手に、支配下登録の年は設定できません。")
        if year < self.from_year:
            raise InvalidContract("支配下登録の年が加入年より前になっています。")
        if self.to_year is not None and year > self.to_year:
            raise InvalidContract("支配下登録の年が退団年より後になっています。")

    def contract_in(self, year: int) -> ContractStatus:
        """その年の契約区分。育成で加入し、その年までに昇格していれば支配下。"""
        return ContractStatus.in_year(self.signed_as, self.promoted_year, year)

    @property
    def contract_now(self) -> ContractStatus:
        """今（在籍の最後の時点）の契約区分。昇格していれば支配下。"""
        if self.signed_as is ContractStatus.DEVELOPMENTAL and self.promoted_year is None:
            return ContractStatus.DEVELOPMENTAL
        return ContractStatus.REGISTERED

    def ensure_number_matches_contract(self) -> None:
        """背番号が区分に合うか。管理画面など、集約を通さない書き込みからも呼ぶ。

        今の番号は今の区分に、昇格前の番号は育成に合っていること。
        """
        self.contract_now.ensure_number_fits(self.number)
        if self.number_before_promotion is not None:
            ContractStatus.DEVELOPMENTAL.ensure_number_fits(self.number_before_promotion)

    def number_in(self, year: int) -> JerseyNumber:
        """その年の背番号。昇格より前の年は昇格前の番号、それ以降は今の番号。"""
        return jersey_number_in(self.number, self.promoted_year, self.number_before_promotion, year)

    def number_periods(self) -> list[tuple[JerseyNumber, int, int | None]]:
        """背番号ごとの期間（番号・開始年・終了年）。終了年が None なら現在も。

        昇格した在籍は「昇格前の期間×昇格前の番号」と「昇格後の期間×今の番号」の2つに分かれる。
        昇格の年に加入した（昇格前の期間が空の）ときは後者だけ。
        """
        if self.promoted_year is None or self.number_before_promotion is None:
            return [(self.number, self.from_year, self.to_year)]
        periods = [(self.number, self.promoted_year, self.to_year)]
        if self.promoted_year > self.from_year:
            periods.insert(0, (self.number_before_promotion, self.from_year, self.promoted_year - 1))
        return periods

    def shared_number(self, other: Stint) -> JerseyNumber | None:
        """期間が重なる同じ背番号があれば、その番号。他人の在籍との照合（同じチーム内）に使う。"""
        for number, start, end in self.number_periods():
            for other_number, other_start, other_end in other.number_periods():
                if number != other_number:
                    continue
                if (other_end is None or start <= other_end) and (end is None or other_start <= end):
                    return number
        return None

    def ensure_promotable(self) -> None:
        """今が育成か。昇格できるかの判定と文言はここが唯一の出典。"""
        if self.contract_now is not ContractStatus.DEVELOPMENTAL:
            raise InvalidContract("育成選手ではないため、支配下登録にはできません。")

    def promote(self, year: int, number: JerseyNumber) -> None:
        """育成から支配下に上げる。背番号は支配下の番号に変わる。"""
        self.ensure_promotable()
        season = Season(year).year
        self._ensure_promotion_year_is_valid(season)
        ContractStatus.REGISTERED.ensure_number_fits(number)
        self.promoted_year = season
        self.number_before_promotion = self.number
        self.number = number

    def covers(self, year: int) -> bool:
        return self.from_year <= year and (self.to_year is None or year <= self.to_year)

    @property
    def period(self) -> StintPeriod:
        """期間だけを取り出したもの（FA の結果の導出に使う）。"""
        return StintPeriod(
            team_id=self.team_id, from_year=self.from_year, to_year=self.to_year, acquired_via=self.acquired_via
        )

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
class FreeAgentDeclaration:
    """FA 宣言。ある年に、国内か海外かで FA の権利を行使すると宣言した記録。

    **宣言の結果（残留か移籍か）は持たない。** 在籍から導ける事実なので、
    `declaration_outcome` が毎回導く（保存すると在籍と食い違いうる）。
    宣言が `Player`（`Team` 集約の内部）の持ち物なのは、生涯を通じた選手の事実で、
    移籍しても付いて回るため。
    """

    year: int
    kind: FreeAgencyKind
    id: int | None = None

    def __post_init__(self) -> None:
        self.year = Season(self.year).year

    def __str__(self) -> str:
        return f"{self.year}年 {self.kind.label}FA"


def declaration_outcome(declaration: FreeAgentDeclaration, career: Iterable[Stint]) -> FreeAgencyOutcome:
    """宣言の結果（残留か移籍か）を、選手の在籍から導く。規則の出典は `fa_destinations`。"""
    return free_agency_outcome(declaration.year, (stint.period for stint in career))


def fa_destination_stints(declaration: FreeAgentDeclaration, career: Iterable[Stint]) -> list[Stint]:
    """宣言の移籍先の在籍（加入年の早い順）。判定の出典は `fa_destinations`（ここは在籍に引き直すだけ）。"""
    pairs = [(stint.period, stint) for stint in career]
    found = fa_destinations(declaration.year, (period for period, _ in pairs))
    # 期間は在籍から作った別の値なので、同じものを同一性で引く
    return [stint for period in found for candidate, stint in pairs if candidate is period]


def ensure_free_agent_acquisitions(
    career: Iterable[Stint], declarations: Iterable[FreeAgentDeclaration], name: str = ""
) -> None:
    """経路が FA の在籍は、どれかの FA 宣言の**移籍先として導かれる在籍**であること。

    宣言の翌年までの別の球団への加入で、経路が FA か不明のものが移籍先になる（`fa_destinations`）。
    経路が不明の在籍は検査しない（宣言して移籍したのに経路が不明、は許す。不明は推測しない）。
    同じ球団で在籍を結び直しただけの在籍や、宣言の年に移籍先の在籍しか無い選手は、FA 入団にできない。
    """
    stints, declared = list(career), list(declarations)
    destinations = [stint for d in declared for stint in fa_destination_stints(d, stints)]
    for stint in stints:
        if stint.acquired_via is AcquisitionRoute.FREE_AGENT and not any(stint is d for d in destinations):
            raise InvalidAcquisition(
                f"{name + 'は' if name else ''}{stint.from_year}年に FA で入団したことになっていますが、"
                f"その年の前年に、別の球団から FA を宣言した記録がありません"
                f"（FA 宣言と、宣言した球団の在籍が先に要ります）。"
            )


def ensure_declarations_valid(
    career: Iterable[Stint], declarations: Iterable[FreeAgentDeclaration], name: str = ""
) -> None:
    """FA 宣言の不変条件。同じ年に1回だけ、宣言した年にどこかの球団に在籍していること。

    宣言を足すときだけでなく、在籍の削除・期間の短縮で後から破れないかを見るときにも使う
    （管理画面が、送信後の在籍と宣言の全体に対して呼ぶ）。
    """
    stints = list(career)
    prefix = f"{name}は" if name else ""
    seen: set[int] = set()
    for declaration in declarations:
        if declaration.year in seen:
            raise InvalidFreeAgentDeclaration(f"{prefix}{declaration.year}年にすでに FA を宣言しています。")
        seen.add(declaration.year)
        # 「今年まで」のような時計による制限は置かない（ペナントの世界は実際の年より先へ進むため）。
        # 未来の年の宣言は在籍が続いている限り認め、退団・移籍で在籍を閉じるときに
        # Player.ensure_free_agency_consistent が「どの在籍にも覆われない宣言」を拒否する
        if not any(stint.covers(declaration.year) for stint in stints):
            raise InvalidFreeAgentDeclaration(
                f"{prefix}{declaration.year}年にどの球団にも在籍していないため、FA を宣言できません。"
            )


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
    # FA 宣言。移籍しても付いて回る選手の事実なので在籍（球団ごと）ではなく選手が持つ。
    # 結果（残留・移籍）は持たず、在籍から導く（outcome_of）
    fa_declarations: list[FreeAgentDeclaration] = field(default_factory=list)
    # 取り消した（保存済みの）宣言の id。リポジトリはこれだけを消す。
    # 宣言を読み込まずに組み立てた Player で保存しても、既存の宣言は消えない（在籍・主将と同じ upsert だけの保存）
    removed_declaration_ids: list[int] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.number} {self.name} ({self.position.label})"

    # --- FA 宣言 ---

    def declaration_in(self, year: int) -> FreeAgentDeclaration | None:
        return next((d for d in self.fa_declarations if d.year == year), None)

    def outcome_of(self, declaration: FreeAgentDeclaration) -> FreeAgencyOutcome:
        """宣言の結果。在籍から導く（保存しない）。"""
        return declaration_outcome(declaration, self.career)

    def declare_free_agency(self, year: int, kind: FreeAgencyKind) -> FreeAgentDeclaration:
        """FA を宣言する。同じ年に宣言できるのは1回だけで、その年にどこかの球団に在籍している必要がある。

        宣言した年に権利を取得している見込みか（登録日数）は、ここでは見ない（FA 権の計算を足すときに入れる）。
        """
        declaration = FreeAgentDeclaration(year=year, kind=kind)
        ensure_declarations_valid(self.career, [*self.fa_declarations, declaration], f"「{self.name}」")
        self.fa_declarations.append(declaration)
        return declaration

    def remove_free_agency_declaration(self, year: int) -> None:
        """FA 宣言を取り消す。その宣言を根拠にした「FA で入団」の在籍があれば、取り消せない。"""
        declaration = self.declaration_in(year)
        if declaration is None:
            raise InvalidFreeAgentDeclaration(f"「{self.name}」に{year}年の FA 宣言はありません。")
        self.fa_declarations.remove(declaration)
        try:
            self.ensure_acquisitions_declared()
        except InvalidAcquisition:
            self.fa_declarations.append(declaration)
            raise InvalidFreeAgentDeclaration(
                f"{year}年の FA 宣言は、FA での入団（経路が FA の在籍）の根拠になっているため取り消せません。"
            ) from None
        # 保存済みの宣言なら、リポジトリが消すのはこの id だけ（持っていない宣言は消さない）
        if declaration.id is not None:
            self.removed_declaration_ids.append(declaration.id)

    def ensure_acquisitions_declared(self) -> None:
        """経路が FA の在籍は、どれかの宣言の移籍先として導かれる在籍か（`ensure_free_agent_acquisitions`）。

        在籍を足す・宣言を取り消すときに、集約や application から呼ぶ。
        """
        ensure_free_agent_acquisitions(self.career, self.fa_declarations, f"「{self.name}」")

    def ensure_free_agency_consistent(self) -> None:
        """宣言の不変条件（同じ年1回・宣言した年の在籍）と、経路 FA の整合の両方。

        在籍を閉じる・足す操作のあとに呼ぶ（退団や、宣言より前の年を指定した移籍で、どの在籍にも覆われない宣言が残らないように）。
        """
        ensure_declarations_valid(self.career, self.fa_declarations, f"「{self.name}」")
        self.ensure_acquisitions_declared()

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

    def add_player(
        self,
        name: str,
        number: JerseyNumber,
        position: Position,
        from_year: int | None = None,
        contract: ContractStatus = ContractStatus.REGISTERED,
        acquired_via: AcquisitionRoute | None = None,
    ) -> Player:
        """選手を加入させる。背番号が在籍中の選手と重複する場合や、契約区分に合わない場合は拒否する。

        入団の経路は任意（None は不明）。経路が契約区分と食い違うときや、FA での入団（新しい選手には
        FA 宣言が無い）は拒否する。

        支配下の上限は集約からは見えない（リーグが持つ）ので、呼び出し側が
        支配下で加えるなら、加える前に ensure_room_for_registered で検査する。
        """
        assert self.id is not None, "ロスターの変更は保存済みのチームに対して行う"
        contract.ensure_number_fits(number)
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
                signed_as=contract,
                acquired_via=acquired_via,
            )
        ]
        player.ensure_acquisitions_declared()
        self.players.append(player)
        return player

    def declare_free_agency(self, player_id: int, year: int, kind: FreeAgencyKind) -> FreeAgentDeclaration:
        """選手の FA 宣言を記録する。同じ年に2回・在籍の無い年の宣言は拒否する（検査は `Player` が行う）。"""
        return self.find_player(player_id).declare_free_agency(year, kind)

    def remove_free_agency_declaration(self, player_id: int, year: int) -> None:
        """選手の FA 宣言を取り消す。FA での入団の根拠になっている宣言は取り消せない。"""
        self.find_player(player_id).remove_free_agency_declaration(year)

    def change_player_number(self, player: Player, number: JerseyNumber) -> None:
        """背番号を変更する。在籍中の他の選手と重複する場合や、今の契約区分に合わない場合は拒否する。"""
        if player.number == number:
            return
        current = self.current_stint(player)
        if current is not None:
            current.contract_now.ensure_number_fits(number)
        self._ensure_number_is_available(number, excluding=player)
        player.number = number
        if current is not None:
            current.number = number

    def ensure_promotable(self, player_id: int) -> None:
        """在籍中の育成選手か。昇格できない選手なら、その理由の例外を投げる。

        支配下の上限など他の検査より先に呼ぶ（理由を取り違えて案内しないため）。
        """
        self._promotable_stint(player_id)

    def _promotable_stint(self, player_id: int) -> Stint:
        """昇格の対象の在籍（在籍中の育成選手）。そうでなければ理由の例外を投げる。"""
        player = self.find_player(player_id)
        current = self.current_stint(player)
        if current is None:
            raise InvalidContract(f"「{self.name}」に在籍していない選手は支配下登録にできません。")
        current.ensure_promotable()
        return current

    def promote_player(self, player_id: int, number: JerseyNumber, year: int | None = None) -> Player:
        """育成選手を支配下に上げる。背番号は支配下の番号（99以下）に変わる。

        在籍中の育成選手だけが対象。支配下の上限は呼び出し側が
        ensure_room_for_registered で昇格の前に検査する（add_player と同じ）。
        その前に ensure_promotable で、昇格できる選手かを見ておく。
        """
        current = self._promotable_stint(player_id)
        player = self.find_player(player_id)
        # 新しい背番号が支配下の番号か・昇格の年が妥当かは Stint.promote が検査する。重複はここで見る
        self._ensure_number_is_available(number, excluding=player)
        current.promote(year if year is not None else date.today().year, number)
        player.number = number
        return player

    def contract_of(self, player: Player) -> ContractStatus | None:
        """このチームでの今の契約区分。在籍していなければ None。"""
        current = self.current_stint(player)
        return current.contract_now if current is not None else None

    def contract_in(self, player: Player, year: int) -> ContractStatus | None:
        """このチームでのその年の契約区分。その年にこのチームに在籍していなければ None。

        同じチームの在籍は年が重ならないよう集約が検査するので、通常は1つ。検査を素通りしたデータ（bulk_create など）で
        同じ年に在籍が複数あるときへの備えとして、1つでも支配下なら支配下とする
        （出場を止めるのは、その年ずっと育成だった選手だけにするため）。
        """
        statuses = [
            stint.contract_in(year) for stint in player.career if stint.team_id == self.id and stint.covers(year)
        ]
        if not statuses:
            return None
        return ContractStatus.REGISTERED if ContractStatus.REGISTERED in statuses else ContractStatus.DEVELOPMENTAL

    def ensure_not_developmental_in(self, player_ids: Iterable[int], year: int) -> None:
        """指定した選手のうち、その年に育成の選手がいれば拒否する（育成選手は試合に出られない）。

        在籍の無い選手 id（別チームの選手・データの不整合）は対象にしない。
        """
        wanted = set(player_ids)
        for player in self.players:
            if player.id in wanted and self.contract_in(player, year) is ContractStatus.DEVELOPMENTAL:
                raise InvalidContract(
                    f"育成選手の「{player.name}」は{year}年の試合に出場できません"
                    "（支配下に登録されている選手だけが出場できます）。"
                )

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
        # 閉じたことで、宣言した年にどの在籍にも覆われなくなる宣言が無いか
        player.ensure_free_agency_consistent()
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

    # --- 支配下の上限 ---

    @property
    def registered_player_count(self) -> int:
        """在籍中で、今の区分が支配下の選手の数。育成は数えない。"""
        return sum(1 for p in self.active_players if self.contract_of(p) is ContractStatus.REGISTERED)

    def ensure_room_for_registered(self, limit: int | None) -> None:
        """支配下の選手が1人増えても上限を超えないか確認する。

        支配下が増える操作（支配下での選手追加・支配下としての移籍受け入れ・昇格）の
        **前**に呼ぶ。育成の追加は支配下を増やさないので呼ばない。
        メッセージの出典はここだけ（管理画面も application もこれを呼ぶ）。
        """
        ensure_quota_not_exceeded(
            self.registered_player_count + 1,
            limit,
            f"「{self.name}」の支配下選手登録数が上限（{limit}人）を超えています。",
            RegisteredPlayerLimitExceeded,
        )


@dataclass
class GameBatting:
    """1試合ぶんの、ある選手の打撃成績と、打線での位置づけ。

    打順のどこに入り、その打順の何番目に出て、どこを守ったか。ボックススコアは
    この3つで並びが決まる（1番から順に、同じ打順は交代の順に並べる）。
    """

    player_id: int
    line: BattingLine
    # どちらのチームの選手か（試合のホームかビジター）。ラインアップの行にチームが無いと、
    # 守備位置を選手に引くときに打席から推測するしかなくなる
    team_id: int
    id: int | None = None
    # 打順（1〜9）。記録しない試合もあるため未設定を許す
    batting_order: int | None = None
    # 同じ打順の何番目か。0 がスタメンで、1以上は途中出場
    slot_sequence: int = 0
    fielding_position: FieldingPosition | None = None
    # 試合に入った最初の打席の通し番号（`PlateAppearance.sequence`）。スタメンは試合開始からなので
    # None。**途中出場の None は「不明」**で、守備の解決ではその選手もその位置も飛ばす（推測しない）。
    entered_sequence: int | None = None

    def __post_init__(self) -> None:
        if self.batting_order is not None and not (1 <= self.batting_order <= 9):
            raise InvalidGame("打順は1〜9で入力してください。")
        if self.slot_sequence < 0:
            raise InvalidGame("交代の順に負の値は指定できません。")
        if self.entered_sequence is not None:
            if self.entered_sequence < 1:
                raise InvalidGame("出場した打席の番号は1以上で入力してください。")
            if self.slot_sequence == 0:
                raise InvalidGame("スタメンに途中出場の打席は持てません（試合開始から出ています）。")

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
class GameFielding:
    """1試合ぶんの、ある選手の守備成績。

    打撃・投球と違い、**打席の記録からしか作れない**（刺殺・補殺は打球の処理経路、
    失策は失策の記録から導く）。通算成績の集計のために保存するが、出典は打席で、
    集約が保存前に照合する（`ensure_lines_match_plate_appearances`）。
    """

    player_id: int
    line: FieldingLine
    id: int | None = None


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
    def runs_voided_if_third_out(self) -> int:
        """この打席のアウトが半回の3つ目になる場合に、規則 5.08(a) で無効になる得点の数。

        打席の中のアウトの前後は記録していないため、アウトがすべて「打者の打球での
        アウト・封殺」のときだけ無効とみなす。走塁死・盗塁刺・牽制死（タイムプレー）が
        1つでも混じるなら、それが3つ目のアウトだった可能性があり、得点を認める側に倒す
        （誤って正しい記録を弾かないため）。盗塁・暴投・捕逸・ボークで還った走者は
        別のプレイなので数えない。
        """
        reasons = [advance.reason for advance in self.advances if advance.is_out]
        if not reasons or not all(reason.cancels_runs_when_third_out for reason in reasons):
            return 0
        return sum(1 for advance in self.advances if advance.has_scored and not advance.reason.happens_between_pitches)

    @property
    def runs_scored(self) -> int:
        """この打席で本塁に達した走者の数。"""
        return sum(1 for advance in self.advances if advance.has_scored)

    @property
    def is_double_play(self) -> bool:
        """併殺（以上）か。**打者への守備でアウトが2つ以上取られた**かで判断する。

        盗塁刺・牽制死は同じ打席に記録されていても併殺ではない（打者の打球とは
        別に起きた走塁のアウト）。単純に `outs_recorded >= 2` とすると、三振と
        盗塁刺が重なった打席が併殺打として数えられ、打点まで消えてしまう。
        """
        return sum(1 for advance in self.advances if advance.is_out and not advance.reason.is_baserunning_out) >= 2

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
    # 守備成績。打席から導く値で、打席と一緒に読む（`plate_appearances_loaded` が False の
    # 集約では空であって「守備が無い」ではない。保存しても触れない）
    fielding: list[GameFielding] = field(default_factory=list)
    # 打席の記録を伴って読み込んだか。1試合あたり約280行になるため、一覧のために
    # まとめて読むときは打席を省く。**「省いた」と「記録が無い」は区別しなければならない**
    # （区別しないと、省いて読んだ集約を保存したときに記録済みの打席が全部消える）。
    # 記録は打席入力の画面だけが書き換えるので、省いて読んだ集約の保存は打席に触れない。
    plate_appearances_loaded: bool = True
    # 記録済みかを外から教える値。明細を読まずに作った集約（順位表用の一覧）は、
    # 明細が空でも「未記録」とは限らない。その場合に限り、参照クエリが答えを持ち込む。
    # None なら `is_recorded` が明細から判定する
    recorded_hint: bool | None = None

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
    def is_recorded(self) -> bool:
        """記録済みの試合か。**未記録かどうかの唯一の出典。**

        未記録とは、打席も打撃・投球の明細も無い試合（登録しただけで、スコアブックを
        まだ保存していない）。得点は0-0で入っているが、0-0で終わった試合ではなく
        「まだ何も分かっていない」ので、勝敗・引分・試合数・順位に数えない。

        打席の記録を持たない古い試合（明細だけがある）は記録済みとして数える。
        参照クエリは SQL で同じ意味の条件を書く
        （`infrastructure/queries.py` の `recorded_games_filter`。突き合わせるテストがある）。
        """
        if self.recorded_hint is not None:
            return self.recorded_hint
        return bool(self.plate_appearances or self.batting or self.pitching)

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
        team_id: int,
        batting_order: int | None = None,
        slot_sequence: int = 0,
        fielding_position: FieldingPosition | None = None,
        entered_sequence: int | None = None,
    ) -> GameBatting:
        """選手の打撃成績を記録する。同じ選手が既にあれば上書きする。"""
        if team_id not in (self.home_team_id, self.away_team_id):
            raise InvalidGame("打撃成績のチームは、試合のホームかビジターでなければなりません。")
        candidate = GameBatting(
            player_id=player_id,
            line=line,
            team_id=team_id,
            batting_order=batting_order,
            slot_sequence=slot_sequence,
            fielding_position=fielding_position,
            entered_sequence=entered_sequence,
        )
        for index, entry in enumerate(self.batting):
            if entry.player_id == player_id:
                candidate.id = entry.id
                self.batting[index] = candidate
                return candidate
        self.batting.append(candidate)
        return candidate

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

    def record_fielding(self, player_id: int, line: FieldingLine) -> GameFielding:
        """選手の守備成績を記録する。同じ選手が既にあれば上書きする。"""
        for entry in self.fielding:
            if entry.player_id == player_id:
                entry.line = line
                return entry
        entry = GameFielding(player_id=player_id, line=line)
        self.fielding.append(entry)
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

    def ensure_lineup_consistent(self) -> None:
        """ラインアップの出場時刻が、打席の記録と噛み合っていることを確かめる。

        - 出場した打席は、記録された打席の範囲内にある。
        - 同じ打順の枠では、後の選手ほど遅く入る（交代の順と時刻が逆転しない）。
        """
        last = max((entry.sequence for entry in self.plate_appearances), default=0)
        slots: dict[tuple[int, int], list[GameBatting]] = {}
        for entry in self.batting:
            if entry.entered_sequence is not None and entry.entered_sequence > last:
                raise InvalidGame(
                    f"出場した打席（{entry.entered_sequence}）が記録された打席の範囲を超えています（選手id={entry.player_id}）。"
                )
            if entry.batting_order is not None:
                slots.setdefault((entry.team_id, entry.batting_order), []).append(entry)

        # 選手が打者・走者・投手・失策の守備者として最後に現れた打席
        last_seen: dict[int, int] = {}
        for plate_appearance in self.plate_appearances:
            appeared = {plate_appearance.batter_id, plate_appearance.pitcher_id}
            appeared.update(advance.runner_id for advance in plate_appearance.advances)
            appeared.update(error.player_id for error in plate_appearance.errors)
            for player_id in appeared:
                last_seen[player_id] = max(last_seen.get(player_id, 0), plate_appearance.sequence)

        for group in slots.values():
            ordered = sorted(group, key=lambda e: e.slot_sequence)
            known = [e.entered_sequence for e in ordered if e.entered_sequence]
            if any(later <= earlier for earlier, later in zip(known, known[1:], strict=False)):
                raise InvalidGame(
                    f"{group[0].batting_order}番の途中出場の順序と、出場した打席の順序が食い違っています。"
                )
            # 後任が入った後に、前任者が現れる記録は成立しない（交代したのに打席や守備に残っている）
            for index, former in enumerate(ordered):
                later_entries = [e.entered_sequence for e in ordered[index + 1 :] if e.entered_sequence]
                if later_entries and last_seen.get(former.player_id, 0) >= min(later_entries):
                    raise InvalidGame(
                        f"{group[0].batting_order}番の交代で、退いたはずの選手（選手id={former.player_id}）が"
                        "後任の出場後にも打席や守備に現れています。"
                    )

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

        検査するのは4点。同じ塁に2人の走者がいないこと、その塁にいない走者が
        進んでいないこと、1つの半回のアウトが3を超えないこと、3つ目のアウトが
        打者の打球でのアウトか封殺のときに、その打席で得点が入っていないこと。
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
            if outs == OUTS_PER_HALF_INNING and entry.runs_voided_if_third_out > 0:
                raise InvalidPlateAppearance(
                    f"{where}: 3つ目のアウトが打者のアウトか封殺なので、この打席で本塁に達した走者の得点は"
                    "記録できません（公認野球規則 5.08）。"
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
