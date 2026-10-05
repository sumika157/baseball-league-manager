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
from .dto import ActivePlayerStats, GameRow, PlayerFielding, TeamAnalysisFacts, TeamSummary


@runtime_checkable
class PlayerStatsQuery(Protocol):
    """在籍中の選手の成績の参照。ランキング・タイトルの材料。

    チーム（集約）を経由すると、順位づけに要らない経歴・主将歴・プロフィールまで
    組み立てる。ここでは成績を SQL で集計して、選手ごとの DTO にして返す。
    """

    def list_career(self) -> list[ActivePlayerStats]:
        """全チームの在籍中の選手と、その通算成績。チームの表示順、背番号順。"""
        ...

    def list_season(self, league_id: int, year: int) -> list[ActivePlayerStats]:
        """そのリーグで在籍中の選手と、そのシーズンの成績。

        成績に数えるのは、リーグのチームどうしの試合だけ（リーグをまたぐ対戦は数えない）。
        チームの表示順、背番号順。
        """
        ...


@runtime_checkable
class PlayerFieldingQuery(Protocol):
    """選手の守備成績の参照。"""

    def for_player(self, player_id: int, team_id: int) -> PlayerFielding | None:
        """通算と、そのチームでの年度別。守備に就いた試合が1つも無ければ None。"""
        ...


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

    def count_by_team(self, *, year: int | None = None) -> dict[int, int]:
        """チームid → 試合数。ホーム・ビジターの両方を数える。"""
        ...

    def list_months(
        self, *, year: int | None = None, team_id: int | None = None, league_id: int | None = None
    ) -> list[int]:
        """その絞り込みで試合がある月を昇順に返す。"""
        ...

    def latest_year(self) -> int | None:
        """最新シーズン。試合が1件も無ければ None。"""
        ...


@runtime_checkable
class TeamAnalysisQuery(Protocol):
    """戦力分析（球団×年度の編成）の参照。

    在籍と試合の明細を SQL で集計して材料だけを返す。区分けの規則は
    ドメイン層（`domain/services/roster_analysis.py`）にあり、ここは数えるだけ。
    """

    def list_years(self, team_id: int) -> list[int]:
        """そのチームの記録済みの試合がある年を新しい順に返す。"""
        ...

    def load(self, team_id: int, year: int) -> TeamAnalysisFacts:
        """その年にそのチームへ在籍していた選手と、その年の出場・登板の数、入退団の材料（在籍）。"""
        ...
