"""ペナントの世界（セーブデータ）の作成・一覧・削除。

世界は実データのリーグを**分岐**して作る。実データは読むだけで、世界の側へ集約経由で
書き込む（bulk で直接書くと、背番号の一意性などの検査を素通りするため）。
世界を作った後の進行・編成は別のサービスが受け持つ（この段階には無い）。

実データを読むリポジトリと、世界の範囲で書くリポジトリは別物で、どちらも
`presentation/views.py` の組み立て関数が渡す。ここは具体的な実装を知らない。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass

from ..domain.entities import League, Team
from ..domain.exceptions import InvalidWorld, TeamNotFound
from ..domain.pennant.fork import fork_league, fork_roster, fork_team
from ..domain.pennant.world import World, WorldScope
from ..domain.repositories import LeagueRepository, TeamRepository, WorldRepository
from .dto import PennantWorldCreated, PennantWorldRow


@dataclass(frozen=True)
class WorldRepositories:
    """ある世界の範囲で組み立てたリポジトリの組。"""

    leagues: LeagueRepository
    teams: TeamRepository


# 範囲を渡すと、その範囲のリポジトリを返す生成器。実装は組み立ての1か所が渡す
WorldRepositoryFactory = Callable[[WorldScope], WorldRepositories]


def _saved_id(value: int | None) -> int:
    """保存済みの集約から取り出す id。永続化された後は必ず値がある。"""
    assert value is not None, "保存済みの集約には id がある"
    return value


def _row(world: World) -> PennantWorldRow:
    return PennantWorldRow(
        id=_saved_id(world.id),
        name=world.name,
        seed=world.seed,
        start_year=world.start_year,
        owner_id=world.owner_id,
        managed_team_id=world.managed_team_id,
    )


class PennantWorldService:
    """世界の作成（分岐）・一覧・削除。"""

    def __init__(
        self,
        *,
        real_leagues: LeagueRepository,
        real_teams: TeamRepository,
        worlds: WorldRepository,
        repositories_for: WorldRepositoryFactory,
    ) -> None:
        # 実データ範囲のリポジトリ。分岐元を読むだけで、書き込みには使わない
        self._real_leagues = real_leagues
        self._real_teams = real_teams
        self._worlds = worlds
        self._repositories_for = repositories_for

    def list_worlds(self) -> list[PennantWorldRow]:
        return [_row(world) for world in self._worlds.find_all()]

    def get_world(self, world_id: int) -> PennantWorldRow:
        """無ければ WorldNotFound。"""
        return _row(self._worlds.find_by_id(world_id))

    def create_world(
        self,
        *,
        name: str,
        owner_id: int | None,
        source_league_ids: Sequence[int],
        start_year: int,
        seed: int,
        managed_source_team_id: int | None = None,
    ) -> PennantWorldCreated:
        """実データのリーグを分岐して世界を作る。

        複製するのはリーグ・球団・選手・現在の在籍だけ（加入年は開幕年）。試合と過去の在籍は
        写さず、球場は共有する。分岐元に使えるのは実データのリーグだけで、ペナントの
        リーグを渡すと LeagueNotFound になる。

        `managed_source_team_id` は受け持つ球団を**分岐元の球団の id**で指す（任意。
        世界を作ったあとで決めてもよい）。写した先の球団が受け持ちになる。

        途中で失敗したら、作りかけの世界を消してから例外を投げる（半端な世界を残さない）。
        """
        league_ids = list(dict.fromkeys(source_league_ids))
        if not league_ids:
            raise InvalidWorld("分岐元のリーグを1つ以上選んでください。")
        # 検査を先に済ませる（世界を作ってから失敗すると、消す手間が増える）
        world = World(name=name, seed=seed, start_year=start_year, owner_id=owner_id)
        sources = [self._real_leagues.find_by_id(league_id) for league_id in league_ids]
        source_rosters = [self._real_teams.find_by_league_with_roster(_saved_id(league.id)) for league in sources]
        if managed_source_team_id is not None and not any(
            team.id == managed_source_team_id for teams in source_rosters for team in teams
        ):
            raise TeamNotFound("受け持つ球団は、分岐元のリーグの球団から選んでください。")

        self._worlds.save(world)
        try:
            return self._fork(world, sources, source_rosters, managed_source_team_id)
        except Exception:
            # 後片付けの失敗で、元の例外を隠さない
            with suppress(Exception):
                self._worlds.delete(_saved_id(world.id))
            raise

    def _fork(
        self,
        world: World,
        sources: list[League],
        source_rosters: list[list[Team]],
        managed_source_team_id: int | None,
    ) -> PennantWorldCreated:
        repositories = self._repositories_for(world.scope)
        team_count = player_count = 0
        forked_team_ids: dict[int, int] = {}

        for order, (source, source_teams) in enumerate(zip(sources, source_rosters, strict=True)):
            league = repositories.leagues.save(fork_league(source, display_order=order))
            for source_team in source_teams:
                # 球団を先に保存して id を得てから、集約の操作で選手を加入させる
                team = repositories.teams.save(fork_team(source_team, league_id=_saved_id(league.id)))
                fork_roster(source_team, team, start_year=world.start_year)
                team = repositories.teams.save(team)
                forked_team_ids[_saved_id(source_team.id)] = _saved_id(team.id)
                team_count += 1
                player_count += len(team.active_players)

        if managed_source_team_id is not None:
            world.managed_team_id = forked_team_ids[managed_source_team_id]
            self._worlds.save(world)

        return PennantWorldCreated(
            world=_row(world),
            league_count=len(sources),
            team_count=team_count,
            player_count=player_count,
        )

    def delete_world(self, world_id: int) -> None:
        """世界と、属するリーグ・球団・選手・試合をすべて消す。無ければ WorldNotFound。"""
        self._worlds.delete(world_id)
