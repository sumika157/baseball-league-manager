"""ロスターの更新（選手の登録・更新・退団・移籍・主将、契約区分の昇格、FA 宣言）を担うアプリケーションサービス。

更新だけを持ち、参照（ロスターや成績の画面）は `TeamViewService`・`LeagueViewService` が担う。世界の範囲に固定した
参照用の組み立て（`build_team_view_service(世界の範囲)` など）から更新を呼べないよう、更新はこのサービスに分けてある。
"""

from __future__ import annotations

from datetime import date

from django.db import transaction

from ..domain.entities import Player
from ..domain.repositories import LeagueRepository, TeamRepository
from ..domain.value_objects import AcquisitionRoute, ContractStatus, FreeAgencyKind, JerseyNumber, Position
from .dto import ProfileInput
from .ids import saved_id
from .player_profile import profile_from_input


class RosterService:
    def __init__(self, teams: TeamRepository, leagues: LeagueRepository) -> None:
        self._teams = teams
        self._leagues = leagues

    def promote_player(self, team_id: int, player_id: int, number: str, year: int | None = None) -> None:
        """育成選手を支配下に上げ、背番号を新しい番号に変える。

        昇格は支配下を1人増やすので、常に上限を検査する。検査に落ちたら保存しない。
        """
        team = self._teams.find_by_id(team_id)
        assert team.league_id is not None, "リポジトリから読んだチームはリーグに属する"
        league = self._leagues.find_by_id(team.league_id)
        # 昇格できる選手か・上限（この順。理由を取り違えて案内しない）・背番号の検査は集約が行う
        team.promote_player(
            player_id,
            JerseyNumber(number),
            year if year is not None else date.today().year,
            limits=league.roster_limits,
        )
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

    # --- 選手の登録・更新・退団・移籍・主将 ---

    def register_player(
        self,
        team_id: int,
        name: str,
        number: str,
        position_label: str,
        contract_label: str = ContractStatus.REGISTERED.value,
        acquired_via_label: str | None = None,
    ) -> Player:
        """新しい選手をロスターに加える。契約区分は既定で支配下。入団の経路は既定で不明。"""
        team = self._teams.find_by_id(team_id)
        contract = ContractStatus.from_label(contract_label)
        acquired_via = AcquisitionRoute.from_label(acquired_via_label) if acquired_via_label else None
        league = self._leagues.find_by_id(saved_id(team.league_id))
        # 支配下の上限は集約（add_player）が検査する
        player = team.add_player(
            name=name,
            number=JerseyNumber(number),
            position=Position.from_label(position_label),
            contract=contract,
            acquired_via=acquired_via,
            limits=league.roster_limits,
        )
        self._teams.save(team)
        return player

    def update_player(
        self,
        team_id: int,
        player_id: int,
        *,
        name: str,
        number: str,
        position_label: str,
        profile: ProfileInput | None = None,
    ) -> Player:
        """選手の基本情報（とプロフィール）を更新する。名前・背番号・守備位置・プロフィールを1回の集約の保存で書く。

        `profile` が None ならプロフィールに触れない（欄が送られてこなかった要求で、空のプロフィールで
        上書きしない）。どれかの検査に落ちたら何も保存しない。成績は試合から集計するため、ここでは扱わない。
        """
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)

        player.rename(name)
        team.change_player_number(player, JerseyNumber(number))
        player.change_position(Position.from_label(position_label))
        if profile is not None:
            team.change_player_profile(player_id, profile_from_input(profile))

        self._teams.save(team)
        return player

    def retire_player(self, team_id: int, player_id: int, year: int | None = None) -> Player:
        """選手を退団させる。成績は残り、背番号は再利用できるようになる。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        team.retire_player(player, year)
        self._teams.save(team)
        return player

    @transaction.atomic
    def transfer_player(
        self,
        player_id: int,
        *,
        from_team_id: int,
        to_team_id: int,
        number: str,
        year: int | None = None,
        contract_label: str | None = None,
        acquired_via_label: str | None = None,
    ) -> None:
        """選手を移籍させる。元の在籍を閉じ、移籍先で新しい在籍を開く。

        入団の経路（移籍先の在籍に付く）は既定で不明。FA を指定するときは、加入年の前年の
        FA 宣言が先に要る（オフの FA 移籍は、宣言の翌年を加入年として記録する）。

        成績は選手に紐づくため移籍しても失われない。経歴として
        「いつどのチームに居たか」が残る。

        移籍先での契約区分は、指定が無ければ移籍元での今の区分を引き継ぐ
        （育成選手は育成のまま移る）。背番号は新しい区分に合うものにする。

        検査がすべて終わってから保存する。途中で拒否された場合に、元チームだけ
        退団済みで移籍先には入らない、という中途半端な状態を残さないため。
        """
        season = year if year is not None else date.today().year

        source = self._teams.find_by_id(from_team_id)
        player = source.find_player(player_id)
        contract = (
            ContractStatus.from_label(contract_label)
            if contract_label is not None
            else source.contract_of(player) or ContractStatus.REGISTERED
        )
        source.retire_player(player, season)

        destination = self._teams.find_by_id(to_team_id)
        league = self._leagues.find_by_id(saved_id(destination.league_id))
        # 背番号・区分・支配下の上限・経路と FA 宣言の検査は、移籍先の集約が行う（外国人の保有は縛らない）
        destination.accept_transfer(
            player,
            JerseyNumber(number),
            season,
            contract,
            AcquisitionRoute.from_label(acquired_via_label) if acquired_via_label else None,
            limits=league.roster_limits,
        )

        self._teams.save(source)
        self._teams.save(destination)

    def appoint_captain(self, team_id: int, player_id: int, year: int | None = None) -> Player:
        """選手をチームの主将に指名する。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        team.appoint_captain(player, year)
        self._teams.save(team)
        return player

    def remove_captain(self, team_id: int, player_id: int, year: int | None = None) -> Player:
        """選手の主将を解任する。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        team.remove_captain(player, year)
        self._teams.save(team)
        return player
