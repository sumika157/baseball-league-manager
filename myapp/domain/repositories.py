"""リポジトリのインターフェース。

ドメイン層は「永続化できる」ことだけを知り、それが Django ORM なのか
他の手段なのかは知らない。実装は infrastructure 層に置く。

**`TeamRepository` / `GameRepository` / `LeagueRepository` / `RatingsRepository` は、組み立てるときに
`WorldScope` を必須で受け取る**（実装のコンストラクタの話で、このインターフェースには
現れない）。読み出しはすべてその範囲に絞られ、範囲の外の id は「見つからない」になる。
`WorldRepository` だけは世界そのものの台帳なので範囲を持たない。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .entities import Game, League, Team
from .pennant.ratings import PlayerRatings
from .pennant.world import World


@runtime_checkable
class TeamRepository(Protocol):
    """Team 集約の永続化。読み書きは常に集約単位で行う。"""

    def find_by_id(self, team_id: int) -> Team:
        """ロスター込みでチームを取得する。存在しなければ TeamNotFound。"""
        ...

    def find_all(self) -> list[Team]:
        """全チームを取得する（ロスターは含めない軽量版）。"""
        ...

    def find_by_league_with_roster(self, league_id: int) -> list[Team]:
        """そのリーグのチームを、ロスター込みで取得する。

        リーグ平均（FIP 定数・OPS+/ERA+ の基準）やリーグ内のランキングに使う。
        全チームを読んでから Python で絞ると、他リーグの選手の通算成績まで
        組み立てることになる。
        """
        ...

    def find_all_with_roster(self) -> list[Team]:
        """全チームをロスター込みで取得する。リーグ全体の順位づけに使う。"""
        ...

    def save(self, team: Team) -> Team:
        """集約の変更内容を永続化する。範囲の外のリーグ・チームには書けない。"""
        ...


@runtime_checkable
class GameRepository(Protocol):
    """Game 集約の永続化。試合は2チームにまたがるため Team とは別の集約。"""

    def find_by_id(self, game_id: int) -> Game:
        """打撃・投球・イニングスコア・打席の記録込みで取得する。無ければ GameNotFound。

        打席まで読むのはこれだけ。1試合で約280行あり、まとめて読む用途に付けると
        数十万行になるため、下の3つは打席を省いて読む（省いた集約は
        `plate_appearances_loaded` が False になり、保存しても打席に触れない）。
        """
        ...

    def find_all(self, year: int | None = None) -> list[Game]:
        """全試合を取得する。年を渡すとそのシーズンだけ。打席は含まない。"""
        ...

    def find_by_team(self, team_id: int, year: int | None = None) -> list[Game]:
        """そのチームが出場した試合を取得する（ホーム・ビジターの別を問わない）。打席は含まない。"""
        ...

    def find_between_teams(self, team_ids: set[int], year: int | None = None) -> list[Game]:
        """渡したチームどうしの試合だけを取得する（両チームが対象に含まれるもの）。

        リーグ内の試合を集めるのに使う。全試合を読んでから Python で捨てると、
        使わない試合の明細まで組み立てることになる。
        """
        ...

    def save(self, game: Game) -> Game:
        """集約の変更内容を永続化する。範囲の外のチームの試合には書けない。"""
        ...


@runtime_checkable
class LeagueRepository(Protocol):
    """リーグの永続化。"""

    def find_by_id(self, league_id: int) -> League: ...

    def find_all(self) -> list[League]: ...

    def save(self, league: League) -> League:
        """リーグを保存する。新しいリーグは範囲の世界に属する形で作られる。"""
        ...


@runtime_checkable
class WorldRepository(Protocol):
    """世界の台帳。世界そのものの出入りだけを扱い、範囲は持たない。"""

    def find_by_id(self, world_id: int) -> World:
        """無ければ WorldNotFound。"""
        ...

    def find_all(self) -> list[World]:
        """新しく作った世界から順に。"""
        ...

    def save(self, world: World) -> World:
        """世界を保存する。"""
        ...

    def delete(self, world_id: int) -> None:
        """世界と、その世界に属するリーグ・球団・選手・試合・能力をすべて消す。無ければ WorldNotFound。"""
        ...


@runtime_checkable
class RatingsRepository(Protocol):
    """世界の中の選手の能力。選手 × 年で1組。"""

    def add_all(self, ratings: Sequence[PlayerRatings]) -> None:
        """能力を一括で保存する（ある年の能力を足す）。

        範囲の外の選手には書けない（PlayerNotFound）。次は InvalidRatings で、**何も書かない**:
        登録位置と能力の種類が合わない（投手に野手の能力、またはその逆）、同じ選手 × 年が
        渡した中で重複している・すでに保存済みである。
        書き込みはまとめて行う（選手ごとに1回ずつ書かない）。ただし1回の SQL に載せる行数には
        データベースの変数の上限があるので、約1,600人ぶんは数百行ずつの数回に分かれる。
        """
        ...

    def find_by_year(self, year: int) -> list[PlayerRatings]:
        """その年の、範囲の全選手の能力。選手の id の順。"""
        ...

    def find_by_player(self, player_id: int) -> list[PlayerRatings]:
        """ひとりの選手の能力を、年の順に。範囲の外の選手や能力の無い選手は空。"""
        ...
