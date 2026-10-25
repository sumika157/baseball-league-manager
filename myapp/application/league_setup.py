"""リーグ・チーム・球場の登録（作成・編集・削除）を担うアプリケーションサービス（W3。#79）。

**更新だけを持つ。** 登録画面の一覧・編集フォームの初期値・消せない理由は参照クエリ（`application/queries.py` の
`SetupQuery`）が読む（更新と参照を分ける）。実データの範囲だけで組み立てる（`build_league_setup_service`）。

規則の置き場所:

- 名前の検査（空でない・長さの上限）は集約・エンティティ（`League`・`Team`・`Stadium` の作成と `rename()`）。
- 名前の重複（ワークスペースの中で一意）と、消してよいか・所属リーグを変えてよいかの判定の材料（数）を読むのは
  リポジトリ。判定そのものは domain の規則（`domain/setup.py`）で、リポジトリがそれを通す。
- 範囲の外のリーグ・チーム・球場の id は、範囲で絞ったリポジトリが `*NotFound` にする（ほかのワークスペースの
  id を混ぜた書き込みは届かない）。
- 表示順は一覧の並べ替え（`reorder_leagues`・`reorder_teams`）だけで決める（W3 T8。2026-10-25 決定。編集の
  `update_*` は表示順を取らない）。新しい行は末尾に入る。並べ替えは識別子の並びで受け取り、今の行の集まりと
  ちょうど一致しなければ domain の `apply_order` が `InvalidOrder` にする。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ..domain.entities import League, Stadium, Team
from ..domain.repositories import LeagueRepository, StadiumRepository, TeamRepository
from ..domain.setup import apply_order
from ..domain.value_objects import ForeignPlayerRules, StadiumProfile
from .ids import saved_id
from .pennant_season import AtomicBlock


def _next_display_order(orders: Iterable[int]) -> int:
    """新しい行の表示順。今の最大＋1（末尾）。行が無ければ 0。"""
    return max(orders, default=-1) + 1


class LeagueSetupService:
    def __init__(
        self,
        leagues: LeagueRepository,
        teams: TeamRepository,
        stadiums: StadiumRepository,
        atomic: AtomicBlock,
    ) -> None:
        self._leagues = leagues
        self._teams = teams
        self._stadiums = stadiums
        # 並べ替えの「今の行を読む → 集まりを照らす → 書く」を1つのトランザクションにする（`transaction.atomic`）
        self._atomic = atomic

    # --- リーグ ---

    def create_league(
        self, *, name: str, foreign_rules: ForeignPlayerRules, registered_player_limit: int | None
    ) -> int:
        """リーグを作り、id を返す。表示順は末尾。"""
        order = _next_display_order(league.display_order for league in self._leagues.find_all())
        league = League(
            name=name,
            foreign_rules=foreign_rules,
            display_order=order,
            registered_player_limit=registered_player_limit,
        )
        return saved_id(self._leagues.save(league).id)

    def update_league(
        self,
        league_id: int,
        *,
        name: str,
        foreign_rules: ForeignPlayerRules,
        registered_player_limit: int | None,
    ) -> None:
        """名前・外国人枠・支配下選手の登録枠を変える（表示順は `reorder_leagues`）。"""
        league = self._leagues.find_by_id(league_id)
        league.rename(name)
        league.change_rules(foreign_rules, registered_player_limit)
        self._leagues.save(league)

    def reorder_leagues(self, league_ids: Sequence[int]) -> None:
        """範囲のリーグの表示順を、`league_ids` の並び（0, 1, 2 …）にする。

        `league_ids` が範囲のリーグの集まりとちょうど一致しない（欠け・余り・重複・ほかのワークスペースの id）なら
        `InvalidOrder` で、何も書かない。読むのも書くのも行数によらないクエリ数。
        """
        with self._atomic():
            leagues = apply_order(self._leagues.find_all(), league_ids)
            self._leagues.save_display_orders(leagues)

    def delete_league(self, league_id: int) -> None:
        """チームが1つも無いリーグを消す（あれば `NotDeletable`）。"""
        self._leagues.delete(league_id)

    # --- チーム ---

    def create_team(self, *, name: str, league_id: int, home_stadium_id: int | None) -> int:
        """チームを作り、id を返す。表示順はそのリーグの末尾。"""
        orders = (team.display_order for team in self._teams.find_all() if team.league_id == league_id)
        team = Team(
            name=name,
            league_id=league_id,
            home_stadium_id=home_stadium_id,
            display_order=_next_display_order(orders),
        )
        return saved_id(self._teams.save(team).id)

    def update_team(self, team_id: int, *, name: str, league_id: int, home_stadium_id: int | None) -> None:
        """名前・所属リーグ・本拠地を変える（表示順は `reorder_teams`）。

        所属リーグは試合が1つも無いときだけ変えられる（`LeagueChangeLocked`。2026-10-25 決定）。検査は、今の所属と
        比べて変えたときにリポジトリが試合を数えて domain の規則で行う。
        移す先のリーグの支配下選手の登録枠を超えるチームは移せない（`RegisteredPlayerLimitExceeded`。W3 T3）。
        移す先のリーグは範囲で引く（範囲の外なら `LeagueNotFound`）。所属を変えないときはリーグを読まない。
        移したチームは、移す先のリーグの末尾に入る（作成と同じ。表示順はリーグの中でしか意味が無い）。
        """
        team = self._teams.find_by_id(team_id)
        team.rename(name)
        if league_id != team.league_id:
            team.move_to_league(league_id, limits=self._leagues.find_by_id(league_id).roster_limits)
            team.reorder(
                _next_display_order(t.display_order for t in self._teams.find_all() if t.league_id == league_id)
            )
        team.change_home_stadium(home_stadium_id)
        self._teams.save(team)

    def reorder_teams(self, league_id: int, team_ids: Sequence[int]) -> None:
        """そのリーグのチームの表示順を、`team_ids` の並び（0, 1, 2 …）にする。

        リーグは範囲で引く（範囲の外なら `LeagueNotFound`）。`team_ids` がそのリーグのチームの集まりとちょうど一致
        しない（欠け・余り・重複・ほかのリーグやワークスペースの id）なら `InvalidOrder` で、何も書かない。
        読むのも書くのも行数によらないクエリ数。
        """
        with self._atomic():
            self._leagues.find_by_id(league_id)
            teams = [team for team in self._teams.find_all() if team.league_id == league_id]
            self._teams.save_display_orders(apply_order(teams, team_ids))

    def delete_team(self, team_id: int) -> None:
        """在籍・主将の在任・試合が1つも無いチームを消す（あれば `NotDeletable`）。担当者の割り当ても外れる。"""
        self._teams.delete(team_id)

    # --- 球場 ---

    def create_stadium(self, *, name: str, profile: StadiumProfile) -> int:
        """球場を作り、id を返す。球場はワークスペースのペナントの世界と共有する。"""
        return saved_id(self._stadiums.save(Stadium(name=name, profile=profile)).id)

    def update_stadium(self, stadium_id: int, *, name: str, profile: StadiumProfile) -> None:
        """名前と属性を変える。世界の球団の本拠地でもあれば、世界にも映る（観客数の上限など。2026-10-25 決定）。"""
        stadium = self._stadiums.find_by_id(stadium_id)
        stadium.rename(name)
        stadium.change_profile(profile)
        self._stadiums.save(stadium)

    def delete_stadium(self, stadium_id: int) -> None:
        """どのチーム（ペナントの世界の球団を含む）も本拠地にしていない球場を消す（していれば `NotDeletable`）。"""
        self._stadiums.delete(stadium_id)
