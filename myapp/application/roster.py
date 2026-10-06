"""ロスターの契約（支配下／育成）と FA 宣言に関するアプリケーションサービス。

`TeamApplicationService` は既に約50メソッドを抱えているため、契約区分まわりの新しい操作
（昇格・FA 宣言）は対象ごとの別サービスに置く。選手の登録・移籍に区分の引数を足す変更は、
既存のメソッドのほうで受ける。
"""

from __future__ import annotations

from datetime import date

from ..domain.repositories import LeagueRepository, TeamRepository
from ..domain.value_objects import FreeAgencyKind, JerseyNumber


class RosterService:
    def __init__(self, teams: TeamRepository, leagues: LeagueRepository) -> None:
        self._teams = teams
        self._leagues = leagues

    def promote_player(self, team_id: int, player_id: int, number: int, year: int | None = None) -> None:
        """育成選手を支配下に上げ、背番号を新しい番号に変える。

        昇格は支配下を1人増やすので、常に上限を検査する。検査に落ちたら保存しない。
        """
        team = self._teams.find_by_id(team_id)
        # 上限より先に、昇格できる選手か（在籍中の育成選手か）を見る。理由を取り違えて案内しない
        team.ensure_promotable(player_id)
        assert team.league_id is not None, "リポジトリから読んだチームはリーグに属する"
        league = self._leagues.find_by_id(team.league_id)
        team.ensure_room_for_registered(league.registered_player_limit)
        team.promote_player(player_id, JerseyNumber(number), year if year is not None else date.today().year)
        self._teams.save(team)

    def declare_free_agency(self, team_id: int, player_id: int, year: int, kind_label: str) -> None:
        """選手の FA 宣言を記録する。残留か移籍かは保存しない（在籍から導く）。

        同じ年に2回、宣言した年にどこにも在籍していない選手は拒否する（ドメインの検査）。
        """
        team = self._teams.find_by_id(team_id)
        team.declare_free_agency(player_id, year, FreeAgencyKind.from_label(kind_label))
        self._teams.save(team)

    def remove_free_agency_declaration(self, team_id: int, player_id: int, year: int) -> None:
        """選手の FA 宣言を取り消す。FA での入団の根拠になっている宣言は取り消せない。"""
        team = self._teams.find_by_id(team_id)
        team.remove_free_agency_declaration(player_id, year)
        self._teams.save(team)
