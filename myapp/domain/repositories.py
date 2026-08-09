"""リポジトリのインターフェース。

ドメイン層は「永続化できる」ことだけを知り、それが Django ORM なのか
他の手段なのかは知らない。実装は infrastructure 層に置く。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .entities import League, Team


@runtime_checkable
class TeamRepository(Protocol):
    """Team 集約の永続化。読み書きは常に集約単位で行う。"""

    def find_by_id(self, team_id: int) -> Team:
        """ロスター込みでチームを取得する。存在しなければ TeamNotFound。"""
        ...

    def find_all(self) -> list[Team]:
        """全チームを取得する（ロスターは含めない軽量版）。"""
        ...

    def find_all_with_roster(self) -> list[Team]:
        """全チームをロスター込みで取得する。リーグ全体の順位づけに使う。"""
        ...

    def save(self, team: Team) -> Team:
        """集約の変更内容を永続化する。"""
        ...


@runtime_checkable
class LeagueRepository(Protocol):
    """リーグの永続化。"""

    def find_by_id(self, league_id: int) -> League:
        ...

    def find_all(self) -> list[League]:
        ...
