"""AI の期中の補強（#170 P3b）。日を進める処理（`PennantSeasonService.advance`）が、判定日をまたぐときに呼ぶ協力者。

判定日（`ReinforcementWindow.review_dates`。6月1日・7月1日）は、進める処理が日程を判定日の前で区切り
（`reinforcement.split_at_reviews`）、判定日以降の最初の試合日の前に1回だけ来る。ここでは:

1. **外で決める**: 判定日の材料（球団ごとの支配下の登録候補・育成選手と能力・支配下の人数と上限・その年の期中の登録）を
   読み、domain の規則（`development.decide_midseason_promotions`）で上げる選手を決める。上げる球団だけを
   ロスター込みで読み（`find_by_ids_with_roster`）、集約で昇格させる（上限・背番号の検査も外）
2. **中で確かめて書く**: トランザクションの中で、世界の最後の試合日と在籍の目印（`roster_signature`）が決めた時と
   同じかを確かめ（違えば `AlreadyAdvanced`。別の操作で世界が進んだか、GM が登録した）、球団を保存して
   登録日を記録する。
   外で読んだ集約を書いて安全なのは、この確かめがあるから（締める処理の「外で読んで中で書く」と同じ。目印は背番号の
   付け替えを見ないので、世界の中で背番号だけを書き換える操作を足すなら、ここの確かめにも含めること）

**昇格を先に決める。** #169 F3 の期中の外国人の獲得も同じ判定日に乗せる予定で、そのときは昇格の後の球団で決め、
同じトランザクションで書く（支配下の上限の1人ぶんは F3 のために残してある。`development.MIDSEASON_RESERVED_SLOTS`）。

登録日は判定日以降の最初の公式戦の日（上げた選手はその日の試合から出られる。GM の期中の昇格と同じ）。
昇格は登録日の年で書く（**年は必ず渡す**。省くと現実の今日の年になる）。
"""

from __future__ import annotations

from datetime import date

from ..domain.exceptions import AlreadyAdvanced
from ..domain.pennant.development import MidseasonClub, MidseasonProspect, Promotion, decide_midseason_promotions
from ..domain.pennant.reinforcement import MidseasonRegistration
from ..domain.pennant.world import World
from ..domain.repositories import (
    LeagueRepository,
    MidseasonRegistrationRepository,
    RatingsRepository,
    TeamRepository,
)
from ..domain.value_objects import JerseyNumber
from .ids import saved_id
from .pennant_season import AtomicBlock
from .pennant_simulation import DaySimulator
from .queries import DevelopmentRosterQuery, SimulationContextQuery


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
        context_query: SimulationContextQuery,
        atomic: AtomicBlock,
    ) -> None:
        self._leagues = leagues
        self._teams = teams
        self._ratings = ratings
        self._registrations = registrations
        self._squads = squads
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
    ) -> tuple[Promotion, ...]:
        """判定日の AI の昇格を決めて書き、上げた選手を返す（いなければ空で、何も書かない）。

        `on` は登録日（判定日以降の最初の公式戦の日）、`simulator` はその日から進めるために読んだもの
        （支配下の登録候補を1軍の基準に使う）、`last_played` と `signature` は `simulator` を読む前の世界の
        最後の試合日と在籍の目印。
        """
        promotions = decide_midseason_promotions(self._clubs(world, on, simulator), on=on, world_seed=world.seed)
        if not promotions:
            return ()
        # 集約の読み込みと昇格（検査）も外で済ませる（全 AI 球団を組み立てると約0.5秒。中で持つと、その間
        # ほかの書き込みが止まる）。外で読んだ集約を中で書いて安全なのは、中で在籍の目印を確かめるから
        # （締める処理と同じ「外で読んで中で書く」）
        by_team = {promotion.team_id: promotion for promotion in promotions}
        teams = self._teams.find_by_ids_with_roster(by_team)
        limits = {league.id: league.roster_limits for league in self._leagues.find_all()}
        for team in teams:
            promotion = by_team[saved_id(team.id)]
            team.promote_player(
                promotion.player_id, JerseyNumber(promotion.number), on.year, limits=limits[team.league_id]
            )
        with self._atomic():
            # 決めた後に世界が進んだ・名簿が変わった（GM の登録など）なら、古い材料で書かない
            if self._context_query.last_played_on() != last_played or self._teams.roster_signature() != signature:
                raise AlreadyAdvanced("進めている間に世界が変わりました。最新の状態からもう一度進めてください。")
            for team in teams:
                promotion = by_team[saved_id(team.id)]
                self._teams.save(team)
                self._registrations.add(
                    MidseasonRegistration(team_id=promotion.team_id, player_id=promotion.player_id, registered_on=on)
                )
        return promotions

    def _clubs(self, world: World, on: date, simulator: DaySimulator) -> list[MidseasonClub]:
        """判定日の材料。育成の能力は登録日の年のもの（試合と同じ年）。"""
        year = on.year
        squads = self._squads.squads(year)
        ratings = {
            item.player_id: item.ratings
            for item in self._ratings.find_by_players(
                [player.player_id for squad in squads for player in squad.players], year
            )
        }
        registered_on: dict[int, list[date]] = {}
        for registration in self._registrations.find_in_year(year):
            registered_on.setdefault(registration.team_id, []).append(registration.registered_on)
        return [
            MidseasonClub(
                team_id=squad.team_id,
                is_managed=squad.team_id == world.managed_team_id,
                pool=simulator.pool(squad.team_id),
                foreign_rules=simulator.foreign_rules(squad.team_id),
                development=tuple(
                    MidseasonProspect(player.player_id, player.position, ratings.get(player.player_id))
                    for player in squad.players
                ),
                registered_count=squad.registered_count,
                registered_limit=squad.registered_limit,
                used_numbers=squad.numbers,
                registrations=tuple(registered_on.get(squad.team_id, ())),
            )
            for squad in squads
        ]
