"""シーズンを締める前の局面の検査（`OffseasonPhase`）と、締める処理の入力の読み口（`OffseasonInputsReader`）。

シーズンを締める処理（`PennantOffseasonService`）とドラフト会議（`PennantDraftService`。#297 D3）が共有する。
会議を始めるときも締めるときと同じ入力から同じ順（引退・戦力外 → 外国人の獲得 → 人数の幅と候補。domain の
`offseason.prepare_meeting`）で計算するので、入力を読むのも同じ1か所にする（読み方が違うと、会議を始めたときに写した
人数の幅と、締めるときに計算し直す幅が食い違う）。

- 局面: 戦力外を指名できるのは公式戦を終えてから締めるまで（ポストシーズン中も選べる）。締められるのと
  会議を開けるのは、それに「その年のポストシーズンが終わった（または無い）」を足した条件（#297 Q1）
- 状態の目印（`OffseasonPhase.state`）: 締める処理と会議を始める処理は、外で読んで計算し、中で目印を読み直して
  変わっていたら書かずに断る（目印の中身は `state` の docstring）
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from ..domain.entities import Player, Team
from ..domain.exceptions import AlreadyClosed, PostseasonNotFinished, SeasonLimitReached, TeamNotFound
from ..domain.pennant.awards import Award, title_counts
from ..domain.pennant.development import DevelopmentDecision, DevelopmentPlan
from ..domain.pennant.draft_meeting import DraftContext
from ..domain.pennant.draft_order import DraftOrderRule, DraftOrderStrategy, DraftStandings, strategy_for
from ..domain.pennant.offseason import OffseasonClub, OffseasonPlayer
from ..domain.pennant.offseason_player import TrackRecord
from ..domain.pennant.postseason import PostseasonFormat, PostseasonStatus
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.release import ReleasePlan
from ..domain.pennant.retirement import PlayingTime
from ..domain.pennant.season import is_final_season, is_season_closed, regular_season_over_year
from ..domain.pennant.world import World, league_priority
from ..domain.repositories import (
    AwardRepository,
    DevelopmentPlanRepository,
    FixtureRepository,
    LeagueRepository,
    RatingsRepository,
    ReleasePlanRepository,
    TeamRepository,
    WorldRepository,
)
from ..domain.value_objects import ForeignPlayerRules, RosterLimits
from .ids import saved_id
from .league_view import LeagueViewService
from .pennant_awards import derive_awards
from .pennant_postseason import PostseasonBracketReader
from .pennant_race import LeagueFinishReader
from .queries import SeasonPlayingTimeQuery, SimulationContextQuery

# 締める処理と会議を始める処理が、外で読んだときと中で書く前とで比べる状態の目印（`OffseasonPhase.state`）
OffseasonState = tuple[
    date | None,
    date | None,
    tuple[int, int, int, int],
    ReleasePlan | None,
    DevelopmentPlan | None,
    PostseasonFormat,
    DraftOrderRule,
]


class OffseasonPhase:
    """世界ひとつの、シーズンを締める前の局面の検査。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        teams: TeamRepository,
        fixtures: FixtureRepository,
        ratings: RatingsRepository,
        releases: ReleasePlanRepository,
        developments: DevelopmentPlanRepository,
        context_query: SimulationContextQuery,
        postseason: PostseasonBracketReader,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._teams = teams
        self._fixtures = fixtures
        self._ratings = ratings
        self._releases = releases
        self._developments = developments
        self._context_query = context_query
        # ポストシーズンが終わるまで締めない・会議を開かない（その年の勝ち上がりを導いて状況を見る）
        self._postseason = postseason

    def season_over_year(self, world: World, *, expected_year: int | None) -> int:
        """その年の公式戦を終えて、まだ締めていない年 Y を返す（最終シーズンは除く）。そうでなければ例外。

        **戦力外を指名できる条件の唯一の出典**（ポストシーズン中も指名できる）。締められる条件と会議を開ける条件
        （`closable_year`）はこれにポストシーズンの検査を足したもの。
        「公式戦を終えた年」は domain の `regular_season_over_year` が出典（ここで書き直さない）。ここで足すのは
        画面を開いたときの年（`expected_year`）と最終シーズンの検査だけ。
        """
        year = regular_season_over_year(
            last_played_on=self._context_query.last_played_on(),
            next_fixture_on=self._fixtures.first_date(),
            start_year=world.start_year,
        )
        if expected_year is not None and expected_year != year:
            if world.start_year <= expected_year < year:
                raise AlreadyClosed(f"{expected_year}年のシーズンは既に締めています。", year=expected_year)
            raise AlreadyClosed("表示している年度と世界の年度が違います。開き直してください。")
        if is_final_season(current_year=year, start_year=world.start_year):
            raise SeasonLimitReached("この世界のシーズン数の上限に達しているので、これ以上は締められません。")
        return year

    def closable_year(
        self, world: World, *, expected_year: int | None, postseason_status: PostseasonStatus | None = None
    ) -> int:
        """締められる（ドラフト会議を開ける。#297 Q1）年 Y を返す。そうでなければ例外。

        `season_over_year` に、「その年のポストシーズンが終わった」を足したもの。`postseason_status` は呼び手が
        既に導いたその年のポストシーズンの状況（渡せば導き直さない。表示にだけ使う）。
        """
        year = self.season_over_year(world, expected_year=expected_year)
        status = self._postseason.status(world, year) if postseason_status is None else postseason_status
        ensure_postseason_settled(year, status)
        return year

    def ensure_not_closed(self, world: World, year: int) -> None:
        if is_season_closed(
            year=year,
            start_year=world.start_year,
            next_year_ratings_exist=bool(self._ratings.find_by_year(year + 1)),
        ):
            raise AlreadyClosed(f"{year}年のシーズンは既に締めています。", year=year)

    def state(self) -> OffseasonState:
        """締められるかと何を計算するかを決める事実（最後に試合をした日・次の対戦の日・在籍の目印・受け持つ球団の
        戦力外の指名・育成の指名・ポストシーズンの方式・ドラフトの指名順の規則）。

        読み込みの前後で比べる。戦力外の指名は GM が締める処理の計算中にも保存しうるので、ここに入れる（#100）。
        育成の指名（昇格・満了の自由契約。#170 P2）も同じ（会議の人数の幅を変える）。
        方式も設定の画面で計算中に変えうる（「なし」から変えたのに、その年のポストシーズンを行わずに締めない。#280）。
        指名順の規則も同じ（古い規則で指名した新人を書かない。#198）。
        """
        world = self._worlds.find_by_id(self._world_id)
        return (
            self._context_query.last_played_on(),
            self._fixtures.first_date(),
            self._teams.roster_signature(),
            release_plan_of(self._releases, world.managed_team_id),
            development_plan_of(self._developments, world.managed_team_id),
            world.postseason_format,
            world.draft_order_rule,
        )


@dataclass(frozen=True)
class OffseasonInputs:
    """締める年 Y の、締める処理（と会議を始める処理）の入力。DB から読んだ値だけで、計算はしていない。

    - `teams`: 世界の球団（在籍と通算成績つきの集約。締める処理はこれをメモリの上で書き換えて保存する）
    - `limits`: リーグごとの人数の上限
    - `clubs`: domain の計算に渡す球団（現役の選手と、締める年の能力・出場・実績）
    - `used_names`: 世界の全選手（引退した選手を含む）の名前
    - `manual_releases`: 受け持つ球団の、締める年の戦力外の手動の指名（自動なら空）
    - `manual_development`: 受け持つ球団の、締める年の育成の手動の指名（選手 id → 扱い。自動なら空。#170 P2）
    - `draft`: ドラフト会議の設定（世界の規則・締める年の公式戦の最終順位・受け持つ球団）
    - `awards`: 締める年のタイトル受賞者（締める処理が表彰として書く。実績の加点にも使った）
    """

    teams: list[Team]
    limits: Mapping[int | None, RosterLimits]
    clubs: tuple[OffseasonClub, ...]
    used_names: frozenset[str]
    manual_releases: Mapping[int, frozenset[int]]
    manual_development: Mapping[int, Mapping[int, DevelopmentDecision]]
    draft: DraftContext
    awards: list[Award]


class OffseasonInputsReader:
    """締める処理の入力の読み口（世界ひとつ）。**締める処理と会議を始める処理の唯一の読み方**。"""

    def __init__(
        self,
        *,
        teams: TeamRepository,
        leagues: LeagueRepository,
        ratings: RatingsRepository,
        releases: ReleasePlanRepository,
        developments: DevelopmentPlanRepository,
        awards: AwardRepository,
        titles: LeagueViewService,
        playing_time: SeasonPlayingTimeQuery,
        finishes: LeagueFinishReader,
    ) -> None:
        self._teams = teams
        self._leagues = leagues
        self._ratings = ratings
        self._releases = releases
        self._developments = developments
        self._awards = awards
        # タイトルの受賞者を導く（世界の範囲の、タイトルの画面と同じサービス）
        self._titles = titles
        self._playing_time = playing_time
        # 締める年の公式戦の最終順位（ドラフトの指名順の「球団順位」）
        self._finishes = finishes

    def read(self, world: World, year: int) -> OffseasonInputs:
        """締める年 `year` の入力を読む（8リーグで数秒。トランザクションの外で呼ぶ）。"""
        teams = self._teams.find_all_with_roster()
        leagues = self._leagues.find_all()
        limits = {league.id: league.roster_limits for league in leagues}
        foreign = {league.id: league.foreign_rules for league in leagues}
        ratings_by_player = {item.player_id: item for item in self._ratings.find_by_year(year)}
        playing = self._playing_time.for_year(year)
        # 締める年のタイトル受賞者（表彰として書く）。画面と同じ集計で、リーグごとに1回（8リーグで0.6〜2.9秒）
        awards = derive_awards(self._titles, year, [saved_id(league.id) for league in leagues])
        # 実績の加点の材料: 締めた年までの表彰の数と、いま締める年の受賞の数（過去の年を導き直さない）
        titles = title_counts(awards) + Counter(self._awards.count_by_player())
        clubs = tuple(
            OffseasonClub(
                team_id=saved_id(team.id),
                foreign_rules=foreign.get(team.league_id, ForeignPlayerRules.UNLIMITED),
                registered_player_limit=limits_of(team, limits).registered,
                players=tuple(
                    _offseason_player(team, player, ratings_by_player, playing, titles, year=year)
                    for player in team.active_players
                ),
            )
            for team in teams
        )
        return OffseasonInputs(
            teams=teams,
            limits=limits,
            clubs=clubs,
            used_names=frozenset(player.name for team in teams for player in team.players),
            manual_releases=self.manual_releases(world, year),
            manual_development=self.manual_development(world, year),
            draft=DraftContext(
                world.draft_order_rule,
                self.standings(world, year, {saved_id(team.id): team.league_id for team in teams}),
                managed_team_id=world.managed_team_id,
            ),
            awards=awards,
        )

    def strategy(self, world: World, year: int) -> DraftOrderStrategy:
        """締める年 `year` の会議の指名順の戦略（会議の途中に保存した行動を再生するときに使う。集約は組み立てない）。

        会議を始めた後は規則を変えられず（`draft_order_rule_change_blocker`）、最終順位は公式戦を終えた後は変わらないので、
        会議を始めたとき・締めるときと同じ戦略になる。
        """
        teams = {saved_id(team.id): team.league_id for team in self._teams.find_all()}
        return strategy_for(
            world.draft_order_rule, self.standings(world, year, teams), world_seed=world.seed, year=year
        )

    def standings(self, world: World, year: int, team_leagues: Mapping[int, int | None]) -> DraftStandings:
        """ドラフトの指名順に使う、締める年の公式戦の最終順位。リーグはその年の優先順（`league_priority`）に並べる。

        `team_leagues` は世界の球団（id → リーグの id）。公式戦の順位に無い球団（その年に試合をしていない。
        ふつうは無い）は、最下位の段より前に置く。
        """
        by_league = {
            finish.league_id: tuple(row.team_id for row in finish.standings)
            for finish in self._finishes.finishes(year)
        }
        league_ids = sorted({league_id for league_id in team_leagues.values() if league_id is not None})
        priority = league_priority(league_ids, year=year, start_year=world.start_year)
        # 世界の球団のリーグに載る順位だけを使う（順位の側にしか無いリーグの球団を、どちらにも入れ落とさない）
        ranked = {team_id for league_id in priority for team_id in by_league.get(league_id, ())}
        return DraftStandings(
            leagues=tuple(by_league.get(league_id, ()) for league_id in priority),
            unranked=tuple(team_id for team_id in team_leagues if team_id not in ranked),
        )

    def manual_releases(self, world: World, year: int) -> dict[int, frozenset[int]]:
        """締める処理が使う手動の指名（受け持つ球団の、締める年の指名だけ。AI 球団は常に自動）。"""
        if world.managed_team_id is None:
            return {}
        chosen = chosen_releases(self._releases.find_by_team(world.managed_team_id), year)
        return {} if chosen is None else {world.managed_team_id: chosen}

    def manual_development(self, world: World, year: int) -> dict[int, Mapping[int, DevelopmentDecision]]:
        """締める処理が使う育成の手動の指名（受け持つ球団の、締める年の指名だけ。AI 球団は常に自動。#170 P2）。"""
        if world.managed_team_id is None:
            return {}
        chosen = chosen_development(self._developments.find_by_team(world.managed_team_id), year)
        return {} if chosen is None else {world.managed_team_id: chosen}


def development_plan_of(developments: DevelopmentPlanRepository, team_id: int | None) -> DevelopmentPlan | None:
    """球団の育成の手動の指名（受け持ちが無い・自動なら None）。"""
    return None if team_id is None else developments.find_by_team(team_id)


def chosen_development(plan: DevelopmentPlan | None, year: int) -> Mapping[int, DevelopmentDecision] | None:
    """締める年 `year` に使う育成の手動の指名（選手 id → 扱い）。自動・締める年と違う指名なら None。"""
    if plan is None or plan.year != year:
        return None
    return plan.decisions


def release_plan_of(releases: ReleasePlanRepository, team_id: int | None) -> ReleasePlan | None:
    """球団の戦力外の手動の指名（受け持ちが無い・自動なら None）。"""
    return None if team_id is None else releases.find_by_team(team_id)


def chosen_releases(plan: ReleasePlan | None, year: int) -> frozenset[int] | None:
    """締める年 `year` に使う手動の指名。自動なら None。

    締める年と違う指名（保存した年のまま締めなかったもの。ふつうは締めるときに消えるので残らない）は使わない。
    """
    if plan is None or plan.year != year:
        return None
    return plan.player_ids


def managed_team_id(world: World) -> int:
    """受け持つ球団。決まっていなければ `TeamNotFound`。"""
    if world.managed_team_id is None:
        raise TeamNotFound("この世界には、受け持つ球団が決まっていません。")
    return world.managed_team_id


def ensure_postseason_settled(year: int, status: PostseasonStatus) -> None:
    """その年のポストシーズンが終わった（または無い）か。終わっていなければ `PostseasonNotFinished`（締めない）。"""
    if not status.is_settled:
        raise PostseasonNotFinished(f"{year}年のポストシーズンがまだ終わっていません。")


def limits_of(team: Team, limits: Mapping[int | None, RosterLimits]) -> RosterLimits:
    """球団のリーグの人数の上限。リーグが見つからなければ無制限（世界の球団はどれもリーグに属す）。"""
    return limits.get(team.league_id, RosterLimits.UNLIMITED)


def _offseason_player(
    team: Team,
    player: Player,
    ratings: Mapping[int, PlayerRatings],
    playing: Mapping[int, PlayingTime],
    titles: Mapping[int, int],
    *,
    year: int,
) -> OffseasonPlayer:
    """締める年の現役の選手。`joined_year` は現在の在籍の開始年（分岐した選手は一律に開幕年）。

    引退の確率の「入団2年以内」の保護は、ドラフトの翌年（`DraftPick.entry_year`）を優先して数える（`OffseasonPlayer.entered_year`）。
    """
    player_id = saved_id(player.id)
    stint = team.current_stint(player)
    joined = year if stint is None else stint.from_year
    return OffseasonPlayer(
        player_id=player_id,
        position=player.position,
        profile=player.profile,
        number=player.number.value,
        joined_year=joined,
        ratings=ratings.get(player_id),
        # 今の区分は集約の判定（`Team.contract_of`＝`Stint.contract_now`）をそのまま渡す。支配下の上限の検査
        # （`Team.registered_player_count`）と同じ判定なので、domain の指名数が上限を超えない
        contract=team.contract_of(player),
        playing_time=playing.get(player_id, PlayingTime()),
        # 実績の加点の材料。通算成績は `find_all_with_roster` が世界の試合から集計した値（保存しない）
        track_record=TrackRecord.of(
            team_id=saved_id(team.id),
            career=player.career,
            year=year,
            batting=player.batting,
            pitching=player.pitching,
            is_captain=team.current_captain is player,
            titles=titles.get(player_id, 0),
        ),
        # 昇格した年のオフは戦力外にしない（`release.py`。#170）
        promoted_year=None if stint is None else stint.promoted_year,
    )
