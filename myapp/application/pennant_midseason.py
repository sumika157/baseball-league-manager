"""ペナントの期中の支配下登録（#170 P3a）。受け持つ球団の育成選手を、シーズン中に GM が支配下に上げる。

補強の窓（`domain/pennant/reinforcement.py` の `ReinforcementWindow`。シーズンを締めた後〜7月31日。#207）の中だけで
動く。窓の判定は世界の「次の公式戦の日」で行い、上げた選手はその日の試合から出られる（登録日＝次の公式戦の日）。

- 背番号は GM が選ばない（空いている支配下の番号から引く。`development.midseason_promotion_number`）
- 昇格は `Team.promote_player` に登録日の年を渡して書く（**年は必ず渡す**。省くと現実の今日の年になる）。
  支配下の上限・昇格できる選手か・背番号の重なりは集約が検査する
- 登録日は記録する（`MidseasonRegistrationRepository`）。在籍は年単位で、オフの昇格（締めた年 Y に Y+1 で書く）と
  Y+1 年の期中の昇格を区別できないため。登録日の年と在籍の昇格の年の照合はリポジトリが書く前に行う

**書くのはトランザクションの中で、次の公式戦の日を読み直してから。** 画面を開いたときの日
（`expected_next_game_on`）と違えば（別の画面で日を進めた）書かずに `AlreadyAdvanced`。窓の外なら
`ReinforcementClosed`。球団は中で読む（1球団だけなので短い。締める処理の「外で読んで中で書く」と違い、
古い集約を書く心配が無い）。

締める処理（`PennantOffseasonService`）とは時期が重ならない（締める処理は公式戦の対戦が残っていないときだけ動き、
こちらは残っているときだけ動く）。締める処理は念のため在籍の目印（`roster_signature`。昇格した在籍の数を含む）を
前後で比べる。

AI の期中の昇格（P3b）と期中の外国人の獲得（#169 F3）はまだ無い。F3 の獲得もこの記録の表に乗せる予定
（同じサービスにするかは #170 の P3a のコメントで選択肢として示した）。
"""

from __future__ import annotations

from datetime import date

from ..domain.entities import ensure_room_for_registered
from ..domain.exceptions import AlreadyAdvanced, RegisteredPlayerLimitExceeded, TeamNotFound
from ..domain.pennant.development import (
    contract_season,
    contract_start_year,
    is_contract_expiring,
    midseason_promotion_number,
)
from ..domain.pennant.offseason_player import Ratings, player_age
from ..domain.pennant.reinforcement import MidseasonRegistration, ReinforcementWindow
from ..domain.pennant.world import World
from ..domain.repositories import (
    FixtureRepository,
    LeagueRepository,
    MidseasonRegistrationRepository,
    RatingsRepository,
    TeamRepository,
    WorldRepository,
)
from ..domain.value_objects import JerseyNumber
from .dto import DevelopmentPlayerFacts, DevelopmentPlayerRow, MidseasonPromotion, MidseasonRegistrationView
from .pennant_outlook import outlook_cell
from .pennant_ratings import overall_cell, rating_cells
from .pennant_season import AtomicBlock
from .queries import DevelopmentRosterQuery


class MidseasonRegistrationService:
    """ペナントの世界ひとつの、受け持つ球団の期中の支配下登録。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        leagues: LeagueRepository,
        teams: TeamRepository,
        fixtures: FixtureRepository,
        ratings: RatingsRepository,
        registrations: MidseasonRegistrationRepository,
        roster: DevelopmentRosterQuery,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._leagues = leagues
        self._teams = teams
        self._fixtures = fixtures
        self._ratings = ratings
        self._registrations = registrations
        self._roster = roster
        self._atomic = atomic

    def is_open(self) -> bool:
        """「育成」タブを出せるか（受け持つ球団があり、補強の窓の中）。育成の一覧までは読まない。"""
        world = self._worlds.find_by_id(self._world_id)
        return world.managed_team_id is not None and ReinforcementWindow.is_open(
            next_regular_game_on=self._fixtures.first_date()
        )

    def view(self) -> MidseasonRegistrationView:
        """「育成」タブの材料。窓の外は `ReinforcementClosed`、受け持つ球団が無ければ `TeamNotFound`。"""
        world = self._worlds.find_by_id(self._world_id)
        team_id = _managed_team_id(world)
        next_on = self._open_next_game_on()
        year = next_on.year
        roster = self._roster.for_team(team_id, year)
        ratings = {
            item.player_id: item.ratings
            for item in self._ratings.find_by_players([player.player_id for player in roster.players], year)
        }
        try:
            ensure_room_for_registered(roster.team_name, roster.registered_count, roster.registered_limit)
            blocked = None
        except RegisteredPlayerLimitExceeded as error:
            blocked = str(error)
        return MidseasonRegistrationView(
            team_id=team_id,
            team_name=roster.team_name,
            year=year,
            next_game_on=next_on,
            deadline=ReinforcementWindow.deadline(year),
            registered_count=roster.registered_count,
            registered_limit=roster.registered_limit,
            blocked_reason=blocked,
            players=tuple(
                _row(player, ratings.get(player.player_id), world=world, team_id=team_id, year=year)
                for player in roster.players
            ),
        )

    def promote(self, player_id: int, *, expected_next_game_on: date) -> MidseasonPromotion:
        """受け持つ球団の育成選手を、次の公式戦の日から支配下にする。

        - 画面を開いた後に日が進んだ（次の公式戦の日が `expected_next_game_on` と違う） → `AlreadyAdvanced`
        - 補強の窓の外 → `ReinforcementClosed`
        - 育成でない・球団にいない・支配下の上限 → 集約の例外（`InvalidContract` など）

        どれも何も書かない。
        """
        world = self._worlds.find_by_id(self._world_id)
        team_id = _managed_team_id(world)
        with self._atomic():
            next_on = self._fixtures.first_date()
            if next_on != expected_next_game_on:
                raise AlreadyAdvanced("画面を開いた後に世界が進みました。開き直してからもう一度お試しください。")
            ReinforcementWindow.ensure_open(next_regular_game_on=next_on)
            assert next_on is not None, "窓が開いていれば次の公式戦がある"
            team = self._teams.find_by_id(team_id)
            assert team.league_id is not None, "世界の球団はリーグに属する"
            limits = self._leagues.find_by_id(team.league_id).roster_limits
            used = {player.number.value for player in team.active_players}
            number = midseason_promotion_number(
                used, world_seed=world.seed, year=next_on.year, team_id=team_id, player_id=player_id
            )
            player = team.promote_player(player_id, JerseyNumber(number), next_on.year, limits=limits)
            self._teams.save(team)
            self._registrations.add(MidseasonRegistration(team_id=team_id, player_id=player_id, registered_on=next_on))
        return MidseasonPromotion(player_id=player_id, name=player.name, number=number, registered_on=next_on)

    def _open_next_game_on(self) -> date:
        next_on = self._fixtures.first_date()
        ReinforcementWindow.ensure_open(next_regular_game_on=next_on)
        assert next_on is not None, "窓が開いていれば次の公式戦がある"
        return next_on


def _managed_team_id(world: World) -> int:
    if world.managed_team_id is None:
        raise TeamNotFound("この世界には、受け持つ球団が決まっていません。")
    return world.managed_team_id


def _row(
    player: DevelopmentPlayerFacts, found: Ratings | None, *, world: World, team_id: int, year: int
) -> DevelopmentPlayerRow:
    tenure = player.tenure
    start = contract_start_year(tenure.profile.draft, joined_year=player.joined_year)
    return DevelopmentPlayerRow(
        player_id=player.player_id,
        name=player.name,
        number=player.number,
        position=tenure.position,
        age=player_age(tenure.profile, year, start_year=world.start_year),
        overall=overall_cell(found) if found is not None else None,
        cells=rating_cells(found) if found is not None else (),
        outlook=(
            outlook_cell(found, tenure=tenure, world=world, team_id=team_id, player_id=player.player_id, year=year)
            if found is not None
            else None
        ),
        contract_season=contract_season(start, year),
        is_expiring=is_contract_expiring(start, year),
    )
