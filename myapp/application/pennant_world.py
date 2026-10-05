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

from ..domain.entities import League, Player, Team
from ..domain.exceptions import InvalidWorld, TeamNotFound
from ..domain.pennant.fork import fork_league, fork_roster, fork_team
from ..domain.pennant.initial_ratings import career_record, estimate_initial_ratings
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.world import (
    MAX_SEASONS_PER_WORLD,
    MAX_SOURCE_LEAGUES,
    MAX_START_YEAR,
    MAX_WORLDS_PER_OWNER,
    World,
    WorldScope,
    earliest_start_year,
)
from ..domain.repositories import LeagueRepository, RatingsRepository, TeamRepository, WorldRepository
from .dto import PennantWorldCreated, PennantWorldRow
from .pennant_season import AtomicBlock
from .queries import FieldingTotalsQuery


@dataclass(frozen=True)
class ForkSource:
    """分岐元として読んだリーグと名簿（`rosters` は `leagues` と同じ順）。

    分岐する側（世界の作成）が集約をそのまま使うので、集約を包んでいる。これを手本に DTO へ
    集約を入れる形を増やさない（参照用の DTO ではなく、更新の材料）。
    """

    leagues: list[League]
    rosters: list[list[Team]]

    @property
    def active_players(self) -> list[Player]:
        """世界に写される選手（球団に現在在籍している選手）。写す選手と同じ範囲。"""
        return [player for teams in self.rosters for team in teams for player in team.active_players]


@dataclass(frozen=True)
class WorldRepositories:
    """ある世界の範囲で組み立てたリポジトリの組。"""

    leagues: LeagueRepository
    teams: TeamRepository
    ratings: RatingsRepository


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
        real_fielding: FieldingTotalsQuery,
        worlds: WorldRepository,
        repositories_for: WorldRepositoryFactory,
        atomic: AtomicBlock,
        ensure_schedule: Callable[[int], bool],
    ) -> None:
        # 実データ範囲のリポジトリと参照クエリ。分岐元を読むだけで、書き込みには使わない
        self._real_leagues = real_leagues
        self._real_teams = real_teams
        self._real_fielding = real_fielding
        self._worlds = worlds
        self._repositories_for = repositories_for
        # 世界の作成と日程の生成を1つにまとめるトランザクションと、世界の id から日程を作る関数（組み立て口が渡す）
        self._atomic = atomic
        self._ensure_schedule = ensure_schedule

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
        if len(league_ids) > MAX_SOURCE_LEAGUES:
            raise InvalidWorld(f"分岐元のリーグは{MAX_SOURCE_LEAGUES}つまで選べます。")
        if owner_id is not None and self._worlds.count_by_owner(owner_id) >= MAX_WORLDS_PER_OWNER:
            raise InvalidWorld(
                f"作れる世界は1人{MAX_WORLDS_PER_OWNER}つまでです。不要な世界を削除してから作ってください。"
            )
        # 検査を先に済ませる（世界を作ってから失敗すると、消す手間が増える）
        world = World(name=name, seed=seed, start_year=start_year, owner_id=owner_id)
        source = self.load_source(league_ids)
        if world.start_year > MAX_START_YEAR:
            raise InvalidWorld(
                f"開幕年は{MAX_START_YEAR}年までにしてください（{MAX_SEASONS_PER_WORLD}シーズン遊べる年が必要です）。"
            )
        earliest = earliest_start_year(player.profile.birth_date for player in source.active_players)
        if world.start_year < earliest:
            raise InvalidWorld(
                f"開幕年は{earliest}年以降にしてください（選んだリーグに、それより後に生まれた選手がいます）。"
            )
        if managed_source_team_id is not None and not any(
            team.id == managed_source_team_id for teams in source.rosters for team in teams
        ):
            raise TeamNotFound("受け持つ球団は、分岐元のリーグの球団から選んでください。")

        self._worlds.save(world)
        try:
            return self._fork(world, source.leagues, source.rosters, managed_source_team_id)
        except Exception:
            # 後片付けの失敗で、元の例外を隠さない
            with suppress(Exception):
                self._worlds.delete(_saved_id(world.id))
            raise

    def create_world_with_schedule(
        self,
        *,
        name: str,
        owner_id: int | None,
        source_league_ids: Sequence[int],
        start_year: int,
        seed: int,
        managed_source_team_id: int | None = None,
    ) -> PennantWorldCreated:
        """世界を作り、開幕年の日程まで作る。**同じトランザクション**で行い、日程が組めなければ世界も残さない。

        日程まで揃えるのは、開幕前の GM ホームが「進める範囲と試合数」を出せるようにするため
        （画面から作る世界はこちらを使う。コマンドは `create_world` で、日程は最初の「進める」で作る）。
        """
        with self._atomic():
            created = self.create_world(
                name=name,
                owner_id=owner_id,
                source_league_ids=source_league_ids,
                start_year=start_year,
                seed=seed,
                managed_source_team_id=managed_source_team_id,
            )
            self._ensure_schedule(_saved_id(created.world.id))
        return created

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
        # (分岐元の選手, 写した選手)。能力の推定が、元の成績と写した先を結びつけるのに使う
        forked_players: list[tuple[Player, Player]] = []

        for order, (source, source_teams) in enumerate(zip(sources, source_rosters, strict=True)):
            league = repositories.leagues.save(fork_league(source, display_order=order))
            for source_team in source_teams:
                # 球団を先に保存して id を得てから、集約の操作で選手を加入させる
                team = repositories.teams.save(fork_team(source_team, league_id=_saved_id(league.id)))
                pairs = fork_roster(source_team, team, start_year=world.start_year)
                team = repositories.teams.save(team)
                forked_players.extend(pairs)
                forked_team_ids[_saved_id(source_team.id)] = _saved_id(team.id)
                team_count += 1
                player_count += len(team.active_players)

        rating_count = self._save_initial_ratings(world, repositories, forked_players)

        if managed_source_team_id is not None:
            world.managed_team_id = forked_team_ids[managed_source_team_id]
            self._worlds.save(world)

        return PennantWorldCreated(
            world=_row(world),
            league_count=len(sources),
            team_count=team_count,
            player_count=player_count,
            rating_count=rating_count,
        )

    def load_source(self, league_ids: Sequence[int]) -> ForkSource:
        """分岐元のリーグと、球団ごとの名簿（実データの範囲）を読む。無ければ LeagueNotFound。

        世界の作成と、能力の推定の確認（`simulate_sample --from-real-leagues`）が同じ読み方をする。
        """
        leagues = [self._real_leagues.find_by_id(league_id) for league_id in league_ids]
        rosters = [self._real_teams.find_by_league_with_roster(_saved_id(league.id)) for league in leagues]
        return ForkSource(leagues=leagues, rosters=rosters)

    def estimate_ratings(
        self, source_players: Sequence[Player], *, seed: int, year: int, player_ids: Sequence[int] | None = None
    ) -> list[PlayerRatings]:
        """分岐元の選手たちの初期能力を、実成績から推定する。`source_players` と同じ順に返す。

        **世界の作成と確認用のコマンドが通る、推定の入口はここだけ。** 成績は分岐元の選手のもの
        （写した選手には成績が無い。試合は写さない）。推定は選手全員を見渡してから行う（盗塁や失策の
        リーグ全体の実測・能力の散らばりの調整の母集団にするため）ので、世界に写す選手全員を一度に渡す。
        `player_ids` は能力を結びつける選手（写した先）。省略は分岐元の選手そのもの。
        """
        fielding = self._real_fielding.totals_for([_saved_id(player.id) for player in source_players])
        ids: list[int | None] = [None] * len(source_players) if player_ids is None else list(player_ids)
        records = [
            career_record(player, fielding, year=year, player_id=player_id)
            for player, player_id in zip(source_players, ids, strict=True)
        ]
        return estimate_initial_ratings(records, seed=seed, year=year)

    def _save_initial_ratings(
        self, world: World, repositories: WorldRepositories, forked_players: list[tuple[Player, Player]]
    ) -> int:
        """分岐した選手の初期能力を、実成績から推定して**1回の一括**で保存する。"""
        ratings = self.estimate_ratings(
            [source for source, _ in forked_players],
            seed=world.seed,
            year=world.start_year,
            player_ids=[_saved_id(copy.id) for _, copy in forked_players],
        )
        repositories.ratings.add_all(ratings)
        return len(ratings)

    def delete_world(self, world_id: int) -> None:
        """世界と、属するリーグ・球団・選手・試合をすべて消す。無ければ WorldNotFound。"""
        self._worlds.delete(world_id)
