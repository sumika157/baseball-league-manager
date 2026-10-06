"""ペナントのシーズンを締める（オフ）。引退・ドラフト・成長と衰え・編成の後始末・翌年の日程を、1つの操作にまとめる。

**進行（`PennantSeasonService`）には足さない。** 進行は試合を作って保存する処理で、こちらは集約（`Team`）の
書き換えと能力・日程の一括保存で、依存も検査も違う。

締めたかどうかは保存しない。「次の対戦が翌年にある」「翌年の能力がある」という事実から導く
（`PennantSeasonService` の進行と同じく、状態を持たない）。

処理は「検査 → 読む → 計算する（`plan_offseason`。DB に触れない）→ 書く」。検査と読み書きは**すべて
1つのトランザクションの中**で行う。同時に2回呼ばれても、翌年の能力の一意制約（選手 × 年）で後から来た方が
失敗し、新人や在籍の変更ごと巻き戻る（`add_all` が保存前に既存の能力を検査して `InvalidRatings` を出し、
それもすり抜けたら DB の一意制約）。**この2段が最後の砦**。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..domain.entities import Player, Team
from ..domain.exceptions import AlreadyClosed, SeasonLimitReached, SeasonNotFinished
from ..domain.pennant.club_plan import PlanSection
from ..domain.pennant.offseason import OffseasonClub, OffseasonPlan, OffseasonPlayer, plan_offseason
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.retirement import PlayingTime
from ..domain.pennant.schedule import season_schedule
from ..domain.pennant.season import SeasonPhase, is_final_season, is_season_closed, season_phase, season_year
from ..domain.pennant.world import World
from ..domain.repositories import (
    ClubPlanRepository,
    FixtureRepository,
    LeagueRepository,
    RatingsRepository,
    TeamRepository,
    WorldRepository,
)
from ..domain.value_objects import JerseyNumber
from .dto import SeasonClosed, SeasonCloseOption
from .pennant_season import AtomicBlock
from .queries import SeasonPlayingTimeQuery, SimulationContextQuery


def _saved_id(value: int | None) -> int:
    """保存済みの集約から取り出す id。永続化された後は必ず値がある。"""
    assert value is not None, "保存済みの集約には id がある"
    return value


class PennantOffseasonService:
    """ペナントの世界ひとつのオフ。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        leagues: LeagueRepository,
        teams: TeamRepository,
        fixtures: FixtureRepository,
        ratings: RatingsRepository,
        plans: ClubPlanRepository,
        context_query: SimulationContextQuery,
        playing_time: SeasonPlayingTimeQuery,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._leagues = leagues
        self._teams = teams
        self._fixtures = fixtures
        self._ratings = ratings
        self._plans = plans
        self._context_query = context_query
        self._playing_time = playing_time
        self._atomic = atomic

    def close_option(self) -> SeasonCloseOption:
        """いま締められるか。締められないときは理由を返す（例外にしない。画面のボタンの出し分けの材料）。"""
        world = self._worlds.find_by_id(self._world_id)
        try:
            year = self._check(world, expected_year=None)
        except (SeasonNotFinished, AlreadyClosed, SeasonLimitReached) as error:
            last_played = self._context_query.last_played_on()
            blocked_year = None if last_played is None else last_played.year
            return SeasonCloseOption(
                year=blocked_year,
                next_year=None if blocked_year is None else blocked_year + 1,
                can_close=False,
                reason=str(error),
            )
        return SeasonCloseOption(year=year, next_year=year + 1, can_close=True)

    def close_season(self, *, expected_year: int | None = None) -> SeasonClosed:
        """シーズンを締める。締める年は最後に試合をした年 Y で、翌年 Y+1 の在籍・能力・日程を作る。

        - 未消化の対戦が残っている、または試合を一度もしていない → `SeasonNotFinished`
        - 既に締めている、または `expected_year`（画面を開いたときの年）が Y と違う → `AlreadyClosed`
        - 世界の最後のシーズン → `SeasonLimitReached`

        どれも何も書かない。書き込みの途中で失敗したときも何も残さない（1つのトランザクション）。
        引退した選手の在籍は Y で閉じ、新人は Y+1 から加入する。**年を省略した在籍の操作は呼ばない**
        （省くと現実の今日の年が入る）。
        """
        with self._atomic():
            world = self._worlds.find_by_id(self._world_id)
            year = self._check(world, expected_year=expected_year)
            if is_season_closed(
                year=year,
                start_year=world.start_year,
                next_year_ratings_exist=bool(self._ratings.find_by_year(year + 1)),
            ):
                raise AlreadyClosed(f"{year}年のシーズンは既に締めています。", year=year)
            return self._close(world, year)

    # --- 検査 ---

    def _check(self, world: World, *, expected_year: int | None) -> int:
        """締められるかを調べ、締める年 Y を返す。締められなければ例外。

        局面の規則は domain の `season_phase` / `season_year` が出典（ここで書き直さない）。
        """
        last_played = self._context_query.last_played_on()
        next_fixture = self._fixtures.first_date()
        phase = season_phase(last_played_on=last_played, next_fixture_on=next_fixture)
        if last_played is None:
            raise SeasonNotFinished("まだ試合をしていないので、シーズンを締められません。")
        if phase is SeasonPhase.BEFORE_OPENING:
            raise AlreadyClosed(f"{last_played.year}年のシーズンは既に締めています。", year=last_played.year)
        if phase is SeasonPhase.IN_SEASON:
            raise SeasonNotFinished(
                f"{last_played.year}年のシーズンはまだ終わっていません（未消化の試合があります）。"
            )
        year = season_year(next_fixture_on=next_fixture, last_played_on=last_played, start_year=world.start_year)
        if expected_year is not None and expected_year != year:
            if world.start_year <= expected_year < year:
                raise AlreadyClosed(f"{expected_year}年のシーズンは既に締めています。", year=expected_year)
            raise AlreadyClosed("表示している年度と世界の年度が違います。開き直してください。")
        if is_final_season(current_year=year, start_year=world.start_year):
            raise SeasonLimitReached("この世界のシーズン数の上限に達しているので、これ以上は締められません。")
        return year

    # --- 読む・計算する・書く ---

    def _close(self, world: World, year: int) -> SeasonClosed:
        teams = self._teams.find_all_with_roster()
        limits = {league.id: league.foreign_player_roster_limit for league in self._leagues.find_all()}
        ratings_by_player = {item.player_id: item for item in self._ratings.find_by_year(year)}
        playing = self._playing_time.for_year(year)

        clubs = [
            OffseasonClub(
                team_id=_saved_id(team.id),
                foreign_roster_limit=limits.get(team.league_id),
                players=tuple(
                    _offseason_player(team, player, ratings_by_player, playing, year=year)
                    for player in team.active_players
                ),
            )
            for team in teams
        ]
        plan = plan_offseason(
            clubs,
            year=year,
            world_seed=world.seed,
            start_year=world.start_year,
            used_names={player.name for team in teams for player in team.players},
        )

        rookie_ratings = self._write_rosters(teams, plan, limits, year)
        retained = [change.after for change in plan.retained]
        self._ratings.add_all([*retained, *rookie_ratings])

        released, released_clubs = self._release_plans(teams, plan, world)

        next_year = year + 1
        schedule = season_schedule(
            {
                league_id: sorted(_saved_id(team.id) for team in teams if team.league_id == league_id)
                for league_id in sorted({team.league_id for team in teams if team.league_id is not None})
            },
            world_seed=world.seed,
            year=next_year,
        )
        self._fixtures.add_all(schedule)

        return SeasonClosed(
            year=year,
            next_year=next_year,
            retired_count=len(plan.retired),
            draftee_count=len(plan.draftees),
            foreign_draftee_count=sum(1 for draftee in plan.draftees if draftee.is_foreign),
            without_ratings_count=len(plan.without_ratings),
            released_sections=released,
            released_club_count=released_clubs,
            fixture_count=len(schedule),
        )

    def _write_rosters(
        self, teams: list[Team], plan: OffseasonPlan, limits: dict[int | None, int | None], year: int
    ) -> list[PlayerRatings]:
        """引退と新人を集約の操作で書く。保存後の id で、新人と能力を結びつけて返す（翌年の能力）。"""
        retired = set(plan.retired)
        next_year = year + 1
        rookie_ratings: list[PlayerRatings] = []
        for team in teams:
            team_id = _saved_id(team.id)
            leaving = [player for player in team.active_players if player.id in retired]
            draftees = [draftee for draftee in plan.draftees if draftee.team_id == team_id]
            if not leaving and not draftees:
                continue
            # 引退を先に済ませ、在籍を Y で閉じる（引退した選手の背番号を新人へ渡せる）
            for player in leaving:
                team.retire_player(player, year=year)
            added: list[Player] = []
            for draftee in draftees:
                player = team.add_player(
                    draftee.name, JerseyNumber(draftee.number), draftee.position, from_year=next_year
                )
                player.profile = draftee.profile
                added.append(player)
            if any(draftee.is_foreign for draftee in draftees):
                team.ensure_foreign_player_quota(limits.get(team.league_id))
            self._teams.save(team)
            rookie_ratings.extend(
                PlayerRatings(player_id=_saved_id(player.id), year=next_year, ratings=draftee.ratings)
                for player, draftee in zip(added, draftees, strict=True)
            )
        return rookie_ratings

    def _release_plans(
        self, teams: list[Team], plan: OffseasonPlan, world: World
    ) -> tuple[tuple[PlanSection, ...], int]:
        """引退した選手を含む編成の区画を自動に戻して保存する（判断26）。受け持つ球団のぶんの区画と、戻した球団の数。"""
        retired = set(plan.retired)
        own_sections: tuple[PlanSection, ...] = ()
        clubs = 0
        for club_plan in self._plans.find_all():
            released_plan, sections = club_plan.release(retired)
            if not sections:
                continue
            self._plans.save(released_plan)
            clubs += 1
            if club_plan.team_id == world.managed_team_id:
                own_sections = sections
        return own_sections, clubs


def _offseason_player(
    team: Team, player: Player, ratings: Mapping[int, PlayerRatings], playing: Mapping[int, PlayingTime], *, year: int
) -> OffseasonPlayer:
    """締める年の現役の選手。`joined_year` は現在の在籍の開始年（分岐した選手は一律に開幕年）。

    引退の確率の「入団2年以内」の保護は、`profile.debut_year`（入団年）を優先して数える（`OffseasonPlayer.entered_year`）。
    """
    player_id = _saved_id(player.id)
    stint = team.current_stint(player)
    joined = year if stint is None else stint.from_year
    return OffseasonPlayer(
        player_id=player_id,
        position=player.position,
        profile=player.profile,
        number=player.number.value,
        joined_year=joined,
        ratings=ratings.get(player_id),
        playing_time=playing.get(player_id, PlayingTime()),
    )
