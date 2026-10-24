"""保存済みの集約から取り出す id の扱いと、id から名前を引く索引。"""

from __future__ import annotations

from ..domain.repositories import TeamRepository


def saved_id(value: int | None) -> int:
    """保存済みの集約が持つ id を取り出す。

    集約は未保存のあいだ id を持たないため型は `int | None` だが、
    リポジトリ・参照クエリから読んだものは必ず保存済みで、所属先の id
    （チームのリーグなど）も埋まっている。内包表記の中など assert を
    置けない場所でも同じ書き方で済むよう、関数にしてある。
    """
    assert value is not None, "リポジトリから読んだ集約は保存済み"
    return value


def team_names(teams: TeamRepository) -> dict[int, str]:
    """チームの id から名前を引く索引。ロスターと成績を読まない一覧用の読み方（`find_all`）で作る。"""
    return {saved_id(team.id): team.name for team in teams.find_all()}
