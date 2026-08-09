"""エンティティと集約。

集約ルートは Team。「同一チーム内で背番号は重複しない」という不変条件は
チーム全体を見ないと判定できないため、Team がロスターを保持して自ら保証する。
リポジトリはこの集約単位で読み書きする。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .exceptions import (
    DuplicateJerseyNumber,
    PlayerNotFound,
)
from .value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)


@dataclass
class League:
    """リーグ。"""

    name: str
    id: int | None = None

    def __str__(self) -> str:
        return self.name


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
    batting: BattingLine = field(default_factory=BattingLine)
    pitching: PitchingLine = field(default_factory=PitchingLine)

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
    """

    name: str
    league_id: int | None = None
    city: str = ''
    id: int | None = None
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
        self, name: str, number: JerseyNumber, position: Position
    ) -> Player:
        """選手を登録する。背番号が現役選手と重複する場合は拒否する。"""
        self._ensure_number_is_available(number)

        player = Player(name=(name or '').strip(), number=number, position=position)
        if not player.name:
            from .exceptions import DomainError

            raise DomainError("選手名を入力してください。")

        self.players.append(player)
        return player

    def change_player_number(self, player: Player, number: JerseyNumber) -> None:
        """背番号を変更する。他の現役選手と重複する場合は拒否する。"""
        if player.number == number:
            return
        self._ensure_number_is_available(number, excluding=player)
        player.number = number

    def _ensure_number_is_available(
        self, number: JerseyNumber, excluding: Player | None = None
    ) -> None:
        for player in self.active_players:
            if player is excluding:
                continue
            if player.number == number:
                raise DuplicateJerseyNumber(
                    f"背番号 {number} は「{self.name}」で既に使用されています。"
                )
