"""ペナントの期中の外国人の獲得（#169 F3）。受け持つ球団の GM が、シーズン中に候補から外国人を獲得する。

補強の窓（`domain/pennant/reinforcement.py` の `ReinforcementWindow`。シーズンを締めた後〜7月31日。#207）の中だけで
動く。窓の判定は世界の「次の公式戦の日」で行い、獲得した選手はその日の試合から出られる（登録日＝次の公式戦の日）。
育成の期中の登録（`MidseasonRegistrationService`）とは別のサービスで、登録日の表とリポジトリだけを共有する
（2026-11-13 ユーザー決定）。

- 候補はその年の期中の候補（`foreign_market.draw_candidates(phase=MIDSEASON, nth=その年に獲得した人数)`）。見せるのは
  受け持つ球団のスカウトの見立てだけ（2026-10-25 ユーザー決定 Q4 A）
- 上限は domain の `foreign_market.ensure_signable`（1年 `MAX_MIDSEASON_SIGNINGS` 人・保有 `MAX_FOREIGN_HELD` 人）と、
  支配下の上限（集約が検査する）
- 獲得は `Team.add_player` に登録日の年を渡して書く（**年は必ず渡す**。省くと現実の今日の年になる）。入団の経路は
  新外国人、前所属はプロフィール（#169 Q5）。その年の能力の行を書き、登録日を区分＝獲得で記録する

**書くのはトランザクションの中で、次の公式戦の日を読み直してから。** 画面を開いたときの日
（`expected_next_game_on`）と違えば（別の画面で日を進めた）書かずに `AlreadyAdvanced`。窓の外なら
`ReinforcementClosed`。球団は中で読む（1球団だけなので短い）。

AI の期中の獲得は日を進める処理の協力者（`pennant_midseason_review.MidseasonReview`）が、判定日に昇格の後で決める。

画面の材料（`view`）は参照クエリ（`SimulationContextQuery.teams()` と `DevelopmentRosterQuery.for_team`）から
作り、集約を組み立てない。保有の人数の出典は `pennant_foreign_rows.held_foreign_rows`（獲得の検査も同じ行で数える）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from ..domain.entities import ensure_room_for_registered
from ..domain.exceptions import AlreadyAdvanced, InvalidSigning, RegisteredPlayerLimitExceeded, TeamNotFound
from ..domain.pennant.foreign_market import (
    MAX_FOREIGN_HELD,
    MAX_MIDSEASON_SIGNINGS,
    ForeignCandidate,
    SigningPhase,
    draw_candidates,
    ensure_signable,
    midseason_key,
    sign_midseason,
)
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.reinforcement import MidseasonRegistration, RegistrationKind, ReinforcementWindow
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
from .dto import DevelopmentRoster, ForeignHeldRow, MidseasonForeignView, MidseasonSigning, SimulationTeam
from .foreign_quota import ForeignQuotaExemptions
from .pennant_foreign_rows import foreign_candidate_row, held_foreign_rows, surname_counts, used_numbers
from .pennant_season import AtomicBlock
from .queries import DevelopmentRosterQuery, PlayerNameQuery, SimulationContextQuery


class ForeignSigningService:
    """ペナントの世界ひとつの、受け持つ球団の期中の外国人の獲得。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

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
        context_query: SimulationContextQuery,
        development_roster: DevelopmentRosterQuery,
        names: PlayerNameQuery,
        fa_exemptions: ForeignQuotaExemptions,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._leagues = leagues
        self._teams = teams
        self._fixtures = fixtures
        self._ratings = ratings
        self._registrations = registrations
        # 画面の材料と保有の人数（支配下は球団の現在の選手、育成は育成の一覧。集約を組み立てない）
        self._context_query = context_query
        self._development_roster = development_roster
        # 候補の名前を世界の選手と重ねないための名前の一覧
        self._names = names
        # FA 資格の取得による外国人枠の対象外（協約 第82条 (4)）。今の外国人の枠の数え方を出すのに使う
        self._fa_exemptions = fa_exemptions
        self._atomic = atomic

    def is_open(self) -> bool:
        """期中の形の「外国人」タブを出せるか（受け持つ球団があり、補強の窓の中）。候補までは引かない。"""
        world = self._worlds.find_by_id(self._world_id)
        return world.managed_team_id is not None and ReinforcementWindow.is_open(
            next_regular_game_on=self._fixtures.first_date()
        )

    def view(self) -> MidseasonForeignView:
        """期中の形の「外国人」タブの材料。窓の外は `ReinforcementClosed`、受け持つ球団が無ければ `TeamNotFound`。"""
        world = self._worlds.find_by_id(self._world_id)
        team_id = _managed_team_id(world)
        next_on = self._fixtures.first_date()
        ReinforcementWindow.ensure_open(next_regular_game_on=next_on)
        assert next_on is not None, "窓が開いていれば次の公式戦がある"
        year = next_on.year
        team = self._team(team_id)
        development = self._development_roster.for_team(team_id, year)
        held = held_foreign_rows(team, development)
        exemptions = self._fa_exemptions.for_year([row.player_id for row in held], year)
        signed = self._signed_this_season(team_id, year)
        candidates = self._candidates(world, team_id, year, signed)
        return MidseasonForeignView(
            team_id=team_id,
            team_name=team.name,
            year=year,
            next_game_on=next_on,
            deadline=ReinforcementWindow.deadline(year),
            players=tuple(
                replace(row, counts_toward_quota=exemptions.counts(row.player_id, is_foreign_player=True))
                for row in held
            ),
            held_limit=MAX_FOREIGN_HELD,
            active_rules=team.foreign_rules,
            signed_this_season=signed,
            signing_limit=MAX_MIDSEASON_SIGNINGS,
            registered_count=development.registered_count,
            registered_limit=development.registered_limit,
            candidates=tuple(
                foreign_candidate_row(candidate, world_seed=world.seed, is_chosen=False) for candidate in candidates
            ),
            blocked_reason=_blocker(development, candidates, held=held, signed=signed),
        )

    def sign(self, index: int, *, expected_next_game_on: date) -> MidseasonSigning:
        """受け持つ球団が、その年の期中の候補（添字 `index`）を次の公式戦の日から支配下で獲得する。

        - 画面を開いた後に日が進んだ（次の公式戦の日が `expected_next_game_on` と違う） → `AlreadyAdvanced`
        - 補強の窓の外 → `ReinforcementClosed`
        - 候補にいない・1年の上限・保有の上限 → `InvalidSigning`、支配下の上限 → 集約の例外

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
            year = next_on.year
            held = held_foreign_rows(self._team(team_id), self._development_roster.for_team(team_id, year))
            signed = self._signed_this_season(team_id, year)
            candidates = self._candidates(world, team_id, year, signed)
            key = midseason_key(year, signed, index)
            ensure_signable(
                [key], candidates, held_foreign=len(held), phase=SigningPhase.MIDSEASON, signed_this_season=signed
            )
            candidate = next(candidate for candidate in candidates if candidate.key == key)
            team = self._teams.find_by_id(team_id)
            assert team.league_id is not None, "世界の球団はリーグに属する"
            limits = self._leagues.find_by_id(team.league_id).roster_limits
            signing = sign_midseason(
                candidate,
                world_seed=world.seed,
                used_names=set(self._names.names()),
                used_numbers=used_numbers(team),
                surname_counts=surname_counts(team),
            )
            player = team.add_player(
                signing.name,
                JerseyNumber(signing.number),
                signing.position,
                from_year=year,
                contract=signing.contract,
                acquired_via=signing.acquisition_route,
                limits=limits,
            )
            player.profile = signing.profile
            self._teams.save(team)
            assert player.id is not None, "保存すると獲得した選手に id が入る"
            self._ratings.add_all([PlayerRatings(player_id=player.id, year=year, ratings=signing.ratings)])
            self._registrations.add(
                MidseasonRegistration(
                    team_id=team_id, player_id=player.id, registered_on=next_on, kind=RegistrationKind.SIGNING
                )
            )
        return MidseasonSigning(
            player_id=player.id,
            name=player.name,
            number=signing.number,
            position=signing.position,
            registered_on=next_on,
        )

    def _team(self, team_id: int) -> SimulationTeam:
        """受け持つ球団と、いま在籍している支配下の選手（参照クエリ。育成は含まない）。"""
        team = next((team for team in self._context_query.teams() if team.team_id == team_id), None)
        if team is None:
            raise TeamNotFound(f"球団が見つかりません（id={team_id}）。")
        return team

    def _signed_this_season(self, team_id: int, year: int) -> int:
        """受け持つ球団が `year` 年に期中に獲得した人数（登録日の表の区分＝獲得で数える）。"""
        return sum(
            1
            for registration in self._registrations.find_by_team(team_id)
            if registration.kind is RegistrationKind.SIGNING and registration.registered_on.year == year
        )

    def _candidates(self, world: World, team_id: int, year: int, signed: int) -> tuple[ForeignCandidate, ...]:
        """受け持つ球団の、`year` 年の期中の候補（AI と同じ引き方。名前は世界の全選手と重ならない）。"""
        return draw_candidates(
            team_id,
            year=year,
            phase=SigningPhase.MIDSEASON,
            world_seed=world.seed,
            used_names=self._names.names(),
            nth=signed,
        )


def _managed_team_id(world: World) -> int:
    if world.managed_team_id is None:
        raise TeamNotFound("この世界には、受け持つ球団が決まっていません。")
    return world.managed_team_id


def _blocker(
    development: DevelopmentRoster, candidates: Sequence[ForeignCandidate], *, held: list[ForeignHeldRow], signed: int
) -> str | None:
    """獲得できない理由（1年の上限・保有の上限は `ensure_signable`、支配下の上限は `ensure_room_for_registered` の
    文言）。獲得できるなら None。"""
    try:
        ensure_signable(
            [candidates[0].key],
            candidates,
            held_foreign=len(held),
            phase=SigningPhase.MIDSEASON,
            signed_this_season=signed,
        )
        ensure_room_for_registered(development.team_name, development.registered_count, development.registered_limit)
    except (InvalidSigning, RegisteredPlayerLimitExceeded) as error:
        return str(error)
    return None
