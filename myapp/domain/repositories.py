"""リポジトリのインターフェース。

ドメイン層は「永続化できる」ことだけを知り、それが Django ORM なのか
他の手段なのかは知らない。実装は infrastructure 層に置く。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .entities import Game, League, Team


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
        """集約の変更内容を永続化する。"""
        ...


@runtime_checkable
class GameRepository(Protocol):
    """Game 集約の永続化。試合は2チームにまたがるため Team とは別の集約。"""

    def find_by_id(self, game_id: int) -> Game:
        """打撃・投球・イニングスコアの明細込みで取得する。無ければ GameNotFound。"""
        ...

    def find_all(self, year: int | None = None) -> list[Game]:
        """全試合を取得する。年を渡すとそのシーズンだけ。"""
        ...

    def find_by_team(self, team_id: int, year: int | None = None) -> list[Game]:
        """そのチームが出場した試合を取得する（ホーム・ビジターの別を問わない）。"""
        ...

    def find_between_teams(self, team_ids: set[int], year: int | None = None) -> list[Game]:
        """渡したチームどうしの試合だけを取得する（両チームが対象に含まれるもの）。

        リーグ内の試合を集めるのに使う。全試合を読んでから Python で捨てると、
        使わない試合の明細まで組み立てることになる。
        """
        ...

    def save(self, game: Game) -> Game:
        """集約の変更内容を永続化する。"""
        ...


@runtime_checkable
class LeagueRepository(Protocol):
    """リーグの永続化。"""

    def find_by_id(self, league_id: int) -> League: ...

    def find_all(self) -> list[League]: ...
