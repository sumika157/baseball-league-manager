"""参照クエリのインターフェース。

一覧表示のような参照は集約を組み立てず、リードモデルが直接 DTO を作る
（実装は infrastructure/queries.py）。アプリケーション層はその実装ではなく、
ここで宣言した形だけに依存する。

永続化のインターフェース（domain/repositories.py）と分けているのは、
参照クエリの戻り値が画面向けの DTO（application/dto.py）で、ドメイン層から
参照できないため。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.entities import Game
from .dto import GameRow, TeamSummary


@runtime_checkable
class TeamListQuery(Protocol):
    """チーム一覧の参照。"""

    def list_summaries(self) -> list[TeamSummary]:
        """全チームの概要を表示順で返す。"""
        ...


@runtime_checkable
class GameListQuery(Protocol):
    """試合一覧の参照。"""

    def list_rows(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
    ) -> list[GameRow]:
        """絞り込んだ試合を新しい順に返す。"""
        ...

    def list_for_standings(self, *, year: int | None = None) -> list[Game]:
        """順位表の計算に渡す試合。成績の明細は持たない。"""
        ...

    def list_seasons(self) -> list[int]:
        """試合のある年を新しい順に返す。"""
        ...

    def list_months(
        self, *, year: int | None = None, team_id: int | None = None, league_id: int | None = None
    ) -> list[int]:
        """その絞り込みで試合がある月を昇順に返す。"""
        ...

    def latest_year(self) -> int | None:
        """最新シーズン。試合が1件も無ければ None。"""
        ...
