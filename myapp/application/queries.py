"""参照クエリのインターフェース。

一覧表示のような参照は集約を組み立てず、リードモデルが直接 DTO を作る
（実装は infrastructure/queries.py）。アプリケーション層はその実装ではなく、
ここで宣言した形だけに依存する。

永続化のインターフェース（domain/repositories.py）と分けているのは、
参照クエリの戻り値が画面向けの DTO（application/dto.py）で、ドメイン層から
参照できないため。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from ..domain.entities import Game
from ..domain.pennant.retirement import PlayingTime
from ..domain.value_objects import FieldingLine
from .dto import (
    ActivePlayerStats,
    GameNote,
    GameRow,
    PeriodBatting,
    PeriodPitching,
    PlayerFielding,
    SimulationContext,
    SimulationTeam,
    TeamSummary,
    WorldSummary,
)


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
class FieldingTotalsQuery(Protocol):
    """選手の通算守備成績の参照（世界の作成時に、守備力の推定に使う）。"""

    def totals_for(self, player_ids: Sequence[int]) -> dict[int, FieldingLine]:
        """選手 id → 通算の守備成績。守備に就いた試合が無い選手は含めない。"""
        ...


@runtime_checkable
class SimulationContextQuery(Protocol):
    """日を進めるのに要る世界の状態の参照（ペナントの進行が使う）。

    球団・選手・直近の登板を、集約（`Team` / `Game`）を組み立てずにまとめて読む。
    **1回の「進める」で1度だけ読み**、その後の登板は呼び出し側がメモリで足していく。
    能力はここに含めない（`RatingsRepository` が出典）。
    """

    def last_played_on(self) -> date | None:
        """世界の「今日」。消化した最後の試合日で、まだ1試合も無ければ None。"""
        ...

    def teams(self) -> tuple[SimulationTeam, ...]:
        """球団と、その現在の選手と、リーグの外国人の枠だけ（直近の登板は読まない。編成の画面が使う）。"""
        ...

    def load(self, *, before: date) -> SimulationContext:
        """`before` より前の試合から導ける状態を読む。

        - 球団と、その現在の選手（登録位置・外国人かどうか）と、リーグの外国人の枠
        - 投手ごとの直近の先発の日（`before` より前のすべての試合から。期間を切ると、
          まとめて進めるか1日ずつ進めるかで「中何日か」の答えが変わる）
        - `before` の直前の数日の登板（連投の判断。数日あれば足りる）
        """
        ...


@runtime_checkable
class WorldSummaryQuery(Protocol):
    """世界の見出し（今日・局面の材料・受け持つ球団）の参照。世界の台帳そのものなので範囲は持たない。

    **世界の数にかかわらず、一定のクエリ数でまとめて読む**（世界ごとにサービスを組み立てて読み直さない）。
    """

    def get(self, world_id: int) -> WorldSummary:
        """無ければ WorldNotFound。"""
        ...

    def list_all(self) -> list[WorldSummary]:
        """新しく作った世界から順に。"""
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
        after: date | None = None,
        through: date | None = None,
    ) -> list[GameRow]:
        """絞り込んだ試合を新しい順に返す。`after` より後、`through` 以前の試合に絞れる。"""
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
class PennantActivityQuery(Protocol):
    """GM ホームの「進めた結果」に要る、期間の見どころの参照（責任投手・本塁打・期間の成績）。

    試合の明細（打撃・投球）を SQL で集計するだけで、集約（`Game`）は組み立てない。
    """

    def game_notes(self, game_ids: Sequence[int]) -> dict[int, GameNote]:
        """試合ごとの責任投手（勝・敗・セーブ）と本塁打を打った選手。明細の無い試合は含めない。"""
        ...

    def batting_between(self, team_id: int, *, after: date, through: date) -> list[PeriodBatting]:
        """`after` より後、`through` 以前の、球団の打者ごとの打撃の合計。出場の無い選手は含めない。"""
        ...

    def pitching_between(self, team_id: int, *, after: date, through: date) -> list[PeriodPitching]:
        """同じ期間の、球団の投手ごとの勝・敗・セーブ。登板の無い選手は含めない。"""
        ...


@runtime_checkable
class SeasonPlayingTimeQuery(Protocol):
    """世界のその年の出場機会の参照。シーズンを締めるときの引退の材料。

    打席数と投球のアウト数を、選手ごとに SQL で集計する（集約は組み立てない）。
    """

    def for_year(self, year: int) -> Mapping[int, PlayingTime]:
        """その年に打席に立った・投げた選手の出場機会。どちらも無い選手は含めない。"""
        ...
