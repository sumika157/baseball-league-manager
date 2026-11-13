"""新しいワークスペースに見本のリーグを入れるアプリケーションサービス（W5。#81）。

**更新だけを持つ。** 範囲は1つのワークスペースの実データ（組み立て口 `build_workspace_sample_service(scope)` が
`WorldScope.real(ws)` で依存を作る）。見本の中身（名簿・日程・試合）は domain の `domain/sample/league.py` が作り、
ここは集約とリポジトリを通して保存するだけ（ORM への直接の書き込みはしない。背番号の一意性・支配下の上限は `Team`
集約が、打席と明細の照合と範囲の検査は `GameRepository.add_all` が通す）。

- 呼ぶのはワークスペースの作成（`WorkspaceService.create` に、組み立て口から呼び出しの関数として渡す。2026-11-13
  ユーザー決定 2）だけ。後から見本を入れる・見本だけを消す導線は無い。
- **トランザクションは持たない。** 呼ぶ側（作成）のトランザクションの中で動く（決定 1。途中で失敗したら
  ワークスペースも見本も残らない）。
- 能力（`SamplePlayer.ratings`）は試合を作るのに使うだけで保存しない。「見本かどうか」も保存しない
  （利用者が入れた行と同じ実データ）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ..domain.entities import League, Stadium, Team
from ..domain.exceptions import InvalidWorkspace
from ..domain.repositories import GameRepository, LeagueRepository, StadiumRepository, TeamRepository
from ..domain.sample.league import (
    SampleLeague,
    draw_sample_league,
    play_sample_games,
    sample_club,
    sample_fixtures,
    sample_year,
)
from ..domain.simulation.manager import ClubRoster
from ..domain.value_objects import JerseyNumber
from .ids import saved_id

NOT_EMPTY_MESSAGE = "見本のリーグは空のワークスペースにだけ入れられます。"


class WorkspaceSampleService:
    """見本のリーグ（1リーグ・6球団・球場6・各25人・36試合）を、範囲のワークスペースの実データとして保存する。"""

    def __init__(
        self,
        leagues: LeagueRepository,
        stadiums: StadiumRepository,
        teams: TeamRepository,
        games: GameRepository,
        today: Callable[[], date],
    ) -> None:
        self._leagues = leagues
        self._stadiums = stadiums
        self._teams = teams
        self._games = games
        self._today = today

    def seed(self, *, key: str) -> None:
        """見本のリーグを入れる。`key` は範囲のワークスペースの key（見本の乱数の元。同じ key と年なら同じ見本）。

        範囲にリーグかチームがあれば `InvalidWorkspace`（何も書かない）。年は試合がすべて今日より前の日付になる年
        （`sample_year`）。球場 → リーグ → チーム → 名簿（加入 → プロフィール）→ 試合の順に保存する。
        途中の例外はそのまま投げる（書いた行を戻すのは呼ぶ側のトランザクション）。
        """
        if self._leagues.find_all() or self._teams.find_all():
            raise InvalidWorkspace(NOT_EMPTY_MESSAGE)
        sample = draw_sample_league(key, sample_year(self._today()))
        clubs = self._save_clubs(sample)
        capacities = {
            club.team_id: team.stadium.profile.capacity for club, team in zip(clubs, sample.teams, strict=True)
        }
        fixtures = sample_fixtures([club.team_id for club in clubs], key=key, year=sample.year)
        games = play_sample_games(
            clubs,
            fixtures,
            key=key,
            year=sample.year,
            foreign_rules=sample.foreign_rules,
            capacities=capacities,
        )
        self._games.add_all(games)

    def _save_clubs(self, sample: SampleLeague) -> list[ClubRoster]:
        """球場・リーグ・チームと名簿を保存し、保存後の id で試合を回す型（`ClubRoster`）にして返す（球団の順）。"""
        league = self._leagues.save(
            League(
                name=sample.name,
                foreign_rules=sample.foreign_rules,
                registered_player_limit=sample.registered_player_limit,
            )
        )
        limits = league.roster_limits
        clubs = []
        for order, sample_team in enumerate(sample.teams):
            stadium = self._stadiums.save(Stadium(name=sample_team.stadium.name, profile=sample_team.stadium.profile))
            team = self._teams.save(
                Team(
                    name=sample_team.name,
                    league_id=saved_id(league.id),
                    home_stadium_id=saved_id(stadium.id),
                    display_order=order,
                )
            )
            # 加入（`from_year` は必ず渡す。省くと現実の今日の年になる）→ 保存で選手の id が決まる → プロフィール
            players = [
                team.add_player(
                    p.name,
                    JerseyNumber(p.number),
                    p.position,
                    from_year=p.from_year,
                    contract=p.contract,
                    acquired_via=p.acquired_via,
                    limits=limits,
                )
                for p in sample_team.players
            ]
            self._teams.save(team)
            player_ids = [saved_id(player.id) for player in players]
            for player_id, p in zip(player_ids, sample_team.players, strict=True):
                team.change_player_profile(player_id, p.profile)
            self._teams.save(team)
            clubs.append(sample_club(sample_team, team_id=saved_id(team.id), player_ids=player_ids))
        return clubs
