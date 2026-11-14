"""AI の期中の補強（昇格は #170 P3b、外国人の獲得は #169 F3）。日を進める処理（`PennantSeasonService.advance`）が、
判定日をまたぐときに呼ぶ協力者。

判定日（`ReinforcementWindow.review_dates`。6月1日・7月1日）は、進める処理が日程を判定日の前で区切り
（`reinforcement.split_at_reviews`）、判定日以降の最初の試合日の前に1回だけ来る。ここでは:

1. **外で決める**: 判定日の材料（球団ごとの支配下の登録候補・育成選手と能力・支配下の人数と上限・外国人の人数・
   その年の期中の登録）を読み、domain の規則で、まず昇格（`development.decide_midseason_promotions`）を、続けて
   **昇格の後の球団**で外国人の獲得（`foreign_market.decide_midseason_signings`。昇格した選手を登録候補と支配下の人数に
   入れる）を決める。決めた球団だけをロスター込みで読み（`find_by_ids_with_roster`）、集約で昇格させ、獲得した外国人を
   加える（上限・背番号の検査も外。獲得した外国人の名前・背番号・背ネームは `foreign_market.sign_midseason`）
2. **中で確かめて書く**: 1つのトランザクションの中で、世界の最後の試合日と在籍の目印（`roster_signature`）が決めた時と
   同じかを確かめ（違えば `AlreadyAdvanced`。別の操作で世界が進んだか、GM が登録・獲得した）、球団を保存して、
   獲得した外国人のその年の能力の行と、登録日（区分は昇格・獲得）を記録する。
   外で読んだ集約を書いて安全なのは、この確かめがあるから（締める処理の「外で読んで中で書く」と同じ。目印は背番号の
   付け替えを見ないので、世界の中で背番号だけを書き換える操作を足すなら、ここの確かめにも含めること）

**昇格を先に決める**（支配下の上限の1人ぶんは獲得のために残してある。`development.MIDSEASON_RESERVED_SLOTS`）。

登録日は判定日以降の最初の公式戦の日（上げた・獲得した選手はその日の試合から出られる。GM の期中の操作と同じ）。
昇格と獲得は登録日の年で書く（**年は必ず渡す**。省くと現実の今日の年になる）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from ..domain.exceptions import AlreadyAdvanced
from ..domain.pennant.development import MidseasonClub, MidseasonProspect, Promotion, decide_midseason_promotions
from ..domain.pennant.foreign_market import (
    ForeignCandidate,
    ForeignSigning,
    MidseasonForeignClub,
    decide_midseason_signings,
    sign_midseason,
)
from ..domain.pennant.offseason_player import Ratings
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.reinforcement import MidseasonRegistration, RegistrationKind
from ..domain.pennant.world import World
from ..domain.repositories import (
    LeagueRepository,
    MidseasonRegistrationRepository,
    RatingsRepository,
    TeamRepository,
)
from ..domain.simulation.manager import ClubRoster, SimBatter, SimPitcher
from ..domain.simulation.ratings import PitcherRatings
from ..domain.value_objects import JerseyNumber, Position
from .dto import DevelopmentSquad
from .ids import saved_id
from .pennant_foreign_rows import surname_counts, used_numbers
from .pennant_season import AtomicBlock
from .pennant_simulation import DaySimulator
from .queries import DevelopmentRosterQuery, PlayerNameQuery, SimulationContextQuery


@dataclass(frozen=True)
class ReviewOutcome:
    """判定日に AI が書いた期中の補強（昇格と、獲得した外国人）。どちらも無ければ何も書いていない。"""

    promotions: tuple[Promotion, ...] = ()
    signings: tuple[ForeignSigning, ...] = ()

    @property
    def changed(self) -> bool:
        """名簿が変わったか（進める処理が登録候補を読み直す）。"""
        return bool(self.promotions or self.signings)


class MidseasonReview:
    """ペナントの世界ひとつの、判定日の AI の期中の補強。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        leagues: LeagueRepository,
        teams: TeamRepository,
        ratings: RatingsRepository,
        registrations: MidseasonRegistrationRepository,
        squads: DevelopmentRosterQuery,
        names: PlayerNameQuery,
        context_query: SimulationContextQuery,
        atomic: AtomicBlock,
    ) -> None:
        self._leagues = leagues
        self._teams = teams
        self._ratings = ratings
        self._registrations = registrations
        self._squads = squads
        # 外国人の候補の名前を世界の選手と重ねないための名前の一覧
        self._names = names
        self._context_query = context_query
        self._atomic = atomic

    def run(
        self,
        *,
        world: World,
        on: date,
        simulator: DaySimulator,
        last_played: date | None,
        signature: tuple[int, int, int, int],
    ) -> ReviewOutcome:
        """判定日の AI の昇格と外国人の獲得を決めて書き、書いたものを返す（無ければ空で、何も書かない）。

        `on` は登録日（判定日以降の最初の公式戦の日）、`simulator` はその日から進めるために読んだもの
        （支配下の登録候補を1軍の基準と外国人枠の空きに使う）、`last_played` と `signature` は `simulator` を読む前の
        世界の最後の試合日と在籍の目印。
        """
        year = on.year
        squads = self._squads.squads(year)
        prospects = self._prospect_ratings(squads, year)
        registrations: dict[int, list[MidseasonRegistration]] = {}
        for registration in self._registrations.find_in_year(year):
            registrations.setdefault(registration.team_id, []).append(registration)
        promotions = decide_midseason_promotions(
            self._clubs(world, squads, prospects, registrations, simulator), on=on, world_seed=world.seed
        )
        names = set(self._names.names())
        chosen = decide_midseason_signings(
            self._foreign_clubs(world, squads, prospects, registrations, simulator, promotions),
            on=on,
            world_seed=world.seed,
            used_names=names,
        )
        if not promotions and not chosen:
            return ReviewOutcome()
        # 集約の読み込みと昇格・獲得（検査）も外で済ませる（全 AI 球団を組み立てると約0.5秒。中で持つと、その間
        # ほかの書き込みが止まる）。外で読んだ集約を中で書いて安全なのは、中で在籍の目印を確かめるから
        # （締める処理と同じ「外で読んで中で書く」）
        promoted = {promotion.team_id: promotion for promotion in promotions}
        signing_of: dict[int, ForeignCandidate] = {candidate.team_id: candidate for candidate in chosen}
        teams = self._teams.find_by_ids_with_roster(promoted.keys() | signing_of.keys())
        limits = {league.id: league.roster_limits for league in self._leagues.find_all()}
        signed = []
        for team in teams:
            team_id = saved_id(team.id)
            team_limits = limits[team.league_id]
            if (promotion := promoted.get(team_id)) is not None:
                team.promote_player(promotion.player_id, JerseyNumber(promotion.number), year, limits=team_limits)
            if (candidate := signing_of.get(team_id)) is not None:
                # 名前の重なりは球団 id の順に解消する（`teams` は id の順）。背番号は昇格の後の球団で引く
                signing = sign_midseason(
                    candidate,
                    world_seed=world.seed,
                    used_names=names,
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
                    limits=team_limits,
                )
                player.profile = signing.profile
                signed.append((player, signing))
        with self._atomic():
            # 決めた後に世界が進んだ・名簿が変わった（GM の登録・獲得など）なら、古い材料で書かない
            if self._context_query.last_played_on() != last_played or self._teams.roster_signature() != signature:
                raise AlreadyAdvanced("進めている間に世界が変わりました。最新の状態からもう一度進めてください。")
            for team in teams:
                self._teams.save(team)
            for promotion in promotions:
                self._registrations.add(
                    MidseasonRegistration(
                        team_id=promotion.team_id,
                        player_id=promotion.player_id,
                        registered_on=on,
                        kind=RegistrationKind.PROMOTION,
                    )
                )
            # 保存すると獲得した外国人に id が入る。その id でその年の能力と登録日を結びつける
            self._ratings.add_all(
                [PlayerRatings(player_id=saved_id(player.id), year=year, ratings=s.ratings) for player, s in signed]
            )
            for player, signing in signed:
                self._registrations.add(
                    MidseasonRegistration(
                        team_id=signing.team_id,
                        player_id=saved_id(player.id),
                        registered_on=on,
                        kind=RegistrationKind.SIGNING,
                    )
                )
        return ReviewOutcome(promotions=promotions, signings=tuple(signing for _, signing in signed))

    def _prospect_ratings(self, squads: Sequence[DevelopmentSquad], year: int) -> dict[int, Ratings]:
        """育成選手の、登録日の年の能力（試合と同じ年）。"""
        return {
            item.player_id: item.ratings
            for item in self._ratings.find_by_players(
                [player.player_id for squad in squads for player in squad.players], year
            )
        }

    @staticmethod
    def _clubs(
        world: World,
        squads: Sequence[DevelopmentSquad],
        prospects: Mapping[int, Ratings],
        registrations: Mapping[int, Sequence[MidseasonRegistration]],
        simulator: DaySimulator,
    ) -> list[MidseasonClub]:
        """昇格の材料。"""
        return [
            MidseasonClub(
                team_id=squad.team_id,
                is_managed=squad.team_id == world.managed_team_id,
                pool=simulator.pool(squad.team_id),
                foreign_rules=simulator.foreign_rules(squad.team_id),
                development=tuple(
                    MidseasonProspect(player.player_id, player.position, prospects.get(player.player_id))
                    for player in squad.players
                ),
                registered_count=squad.registered_count,
                registered_limit=squad.registered_limit,
                used_numbers=squad.numbers,
                registrations=tuple(registrations.get(squad.team_id, ())),
            )
            for squad in squads
        ]

    @staticmethod
    def _foreign_clubs(
        world: World,
        squads: Sequence[DevelopmentSquad],
        prospects: Mapping[int, Ratings],
        registrations: Mapping[int, Sequence[MidseasonRegistration]],
        simulator: DaySimulator,
        promotions: Sequence[Promotion],
    ) -> list[MidseasonForeignClub]:
        """外国人の獲得の材料。**昇格の後の球団**（昇格した選手を登録候補と支配下の人数に入れる）。"""
        promoted = {promotion.team_id: promotion.player_id for promotion in promotions}
        clubs = []
        for squad in squads:
            pool = simulator.pool(squad.team_id)
            player_id = promoted.get(squad.team_id)
            if player_id is not None:
                player = next(player for player in squad.players if player.player_id == player_id)
                pool = _with_promoted(
                    pool, player_id, player.position, prospects[player_id], is_foreign=player.is_foreign
                )
            clubs.append(
                MidseasonForeignClub(
                    team_id=squad.team_id,
                    is_managed=squad.team_id == world.managed_team_id,
                    pool=pool,
                    foreign_rules=simulator.foreign_rules(squad.team_id),
                    held_foreign=squad.foreign_count,
                    registered_count=squad.registered_count + (1 if player_id is not None else 0),
                    registered_limit=squad.registered_limit,
                    signed_on=tuple(
                        registration.registered_on
                        for registration in registrations.get(squad.team_id, ())
                        if registration.kind is RegistrationKind.SIGNING
                    ),
                )
            )
        return clubs


def _with_promoted(
    pool: ClubRoster, player_id: int, position: Position, ratings: Ratings, *, is_foreign: bool
) -> ClubRoster:
    """昇格した育成選手を足した登録候補（外国人の獲得の、1軍の外国人枠の空きと戦力の判定にだけ使う）。

    名前と投げる手は判定に使わないので持たない。外国人枠は外国人選手なら数える（育成から上がった外国人が FA 資格を
    取っていることは無いので、対象外は導かない）。
    """
    if isinstance(ratings, PitcherRatings):
        pitcher = SimPitcher(player_id, "", ratings, counts_as_foreign=is_foreign)
        return ClubRoster(pool.team_id, pool.name, pool.batters, (*pool.pitchers, pitcher))
    batter = SimBatter(player_id, "", position, ratings, counts_as_foreign=is_foreign, throws=None)
    return ClubRoster(pool.team_id, pool.name, (*pool.batters, batter), pool.pitchers)
