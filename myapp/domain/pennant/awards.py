"""ペナントの表彰（締めたシーズンのタイトル受賞者）。**純粋な値**で、DB にも Django にも触れない。

シーズンを締めるとき、その年のタイトル（`Title`。部門の1位。同率は全員）の受賞者を確定した記録として保存する
（2026-10-15 ユーザー決定。#248）。CLAUDE.md の「同じ事実の出典を2つ作らない」の**例外**で、範囲は
**締めたシーズンのタイトル受賞者だけ**（通算成績・順位・勝敗には広げない）。

- 受賞者は、締める処理がタイトルの画面と同じ集計（`get_league_titles`）で導いて書く。締めたシーズンの試合は
  書き換えないので、保存した受賞者が後から導いた受賞者とずれることは無い
- 締めた年の受賞者を読む画面（歴代の記録・球団史・選手ページ）は表彰を読み、終わったが締めていない年
  （`season.unclosed_completed_year`）だけを導く。同じ年を両方から読まない
- 実データのタイトルは保存しない（実データの試合は後から入力・修正されるため）
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from ..services.rankings import Title


@dataclass(frozen=True)
class Award:
    """表彰1つ。ある年・あるリーグの、ある部門の受賞者（同率の1位は1人ずつ別の表彰）。

    `team_id` は受賞した年に所属していた球団。`value` は受賞時の記録の表記
    （「.345」「42」。タイトルの画面と同じ書式）で、受賞の記録の一部として持つ
    （後から書式を変えても、過去の表彰の表記は変えない）。
    過去の表彰は受賞時の規定（部門・規定打席などの条件・書式）で確定し、書き換えない（後から規定を変えても導き直さない）。
    """

    year: int
    league_id: int
    title: Title
    player_id: int
    team_id: int
    value: str


def title_counts(awards: Iterable[Award]) -> Counter[int]:
    """選手ごとの表彰の数（同じ年の別の部門も、同率の受賞も1つずつ数える）。"""
    return Counter(award.player_id for award in awards)
