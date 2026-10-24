"""ペナントの日本一（ポストシーズンの優勝）と、球団ごとのポストシーズンの結果。歴代の記録・球団史・世界の終了の材料。

- **日本一**: 締めた年は記録（`ChampionshipQuery`。締める処理が保存した優勝球団。#242）を1本で読み、
  **終わったが締めていない年**（domain の `unclosed_completed_year`。シーズン終了のいまの年度。最終シーズンも）だけを
  試合から導く。同じ年を両方から読まない（表彰の `pennant_history._holders` と同じ形）
- **到達段**（「CS ファイナルステージ敗退」「出場なし」など。球団史）: 保存しない。その年の試合から導く
  （ユーザー決定 2026-10-24）。締めた年の日本一は記録から取り、導いた勝ち上がりの優勝が記録と食い違えば
  `InvalidPostseason`（どちらかに黙って寄せない）

勝ち上がりは `final_standings` が年ごとに作った最終順位（`SeasonFinal.finishes`。勝ち上がりの読み取りと同じ
`league_finishes`）と、ポストシーズンの試合（全部の年を1クエリ）から `derive_bracket` で導く。年ごとに順位を
読み直さない（クエリ数が年の数に比例して増えない）。`InvalidPostseason` はどこでも握りつぶさない。
ポストシーズンの無い世界（いまの方式が「なし」で、試合も無い。`WorldContext.shows_postseason`）は何も読まない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.exceptions import InvalidPostseason
from ..domain.pennant.postseason import PostseasonBracket, PostseasonFormat, PostseasonResult, champion_title
from ..domain.pennant.season import unclosed_completed_year
from ..domain.repositories import WorldRepository
from .dto import ChampionshipRow, WorldContext
from .pennant_postseason import derive_bracket
from .pennant_race import SeasonFinal
from .queries import ChampionshipQuery, PostseasonQuery

# 終わっていないポストシーズン（いまの年度）の、球団史の結果の表記
IN_PROGRESS = "進行中"


@dataclass(frozen=True)
class YearChampion:
    """ある年の日本一。

    `team_id` が None なら、その年はポストシーズンが無かったか、まだ終わっていない（`in_progress`）。
    """

    year: int
    team_id: int | None
    team_name: str
    in_progress: bool


@dataclass(frozen=True)
class ClubPostseason:
    """ある球団のある年のポストシーズンの結果（球団史の1マス）。`label` が空なら、その年はポストシーズンが無かった。"""

    year: int
    label: str
    won: bool


class PostseasonHistoryReader:
    """日本一と球団ごとのポストシーズンの結果を読む。読むだけで、何も書かない。

    リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。
    """

    def __init__(
        self,
        *,
        worlds: WorldRepository,
        postseason: PostseasonQuery,
        championships: ChampionshipQuery,
    ) -> None:
        self._worlds = worlds
        self._postseason = postseason
        self._championships = championships
        # 開幕年（リーグの優先順に使う）。要るときに1回だけ読む
        self._start_year: int | None = None

    @staticmethod
    def title(world: WorldContext, league_ids: Sequence[int]) -> str:
        """日本一の列の見出し（「日本一」「CS 優勝」）。ポストシーズンの無い世界は空（列を出さない）。"""
        return champion_title(len(league_ids)) if world.shows_postseason else ""

    def champions(
        self,
        world: WorldContext,
        finals: Sequence[SeasonFinal],
        league_ids: Sequence[int],
        *,
        open_year: YearChampion | None = None,
    ) -> dict[int, YearChampion]:
        """終わった年ごとの日本一（歴代の記録・世界の終了）。ポストシーズンの無い世界は空（何も読まない）。

        締めた年は記録だけを読み、締めていない年（いまの年度）だけを導く（その年の試合だけを読む）。
        `open_year` は呼び手（GM ホーム）が既に導いたいまの年度の日本一。渡せば導き直さない。
        """
        if not world.shows_postseason:
            return {}
        current = unclosed_completed_year(phase=world.phase, current_year=world.season_year)
        recorded = self._recorded()
        champions: dict[int, YearChampion] = {}
        for final in finals:
            if final.year == current and open_year is not None and open_year.year == current:
                champions[final.year] = open_year
            elif final.year == current:
                bracket = self._derive(world, final, self._postseason.results(final.year), league_ids)
                champions[final.year] = _open_champion(final, bracket)
            else:
                champions[final.year] = _recorded_champion(final.year, recorded.get(final.year))
        return champions

    def club_results(
        self, world: WorldContext, finals: Sequence[SeasonFinal], league_ids: Sequence[int], team_id: int
    ) -> dict[int, ClubPostseason]:
        """球団の年ごとのポストシーズンの結果（球団史）。ポストシーズンの無い世界は空（何も読まない）。

        日本一は `champions` と同じ出典（締めた年は記録、いまの年度は導いた値）。それ以外の到達段は、その年の試合から
        導いた勝ち上がりから。ポストシーズンの試合は全部の年を1クエリで読む。締めた年で試合の無い年は「無かった」（空）。
        """
        if not world.shows_postseason:
            return {}
        open_year = unclosed_completed_year(phase=world.phase, current_year=world.season_year)
        recorded = self._recorded()
        played_by_year = self._postseason.results_by_year([final.year for final in finals])
        title = champion_title(len(league_ids))
        club: dict[int, ClubPostseason] = {}
        for final in finals:
            if final.result_of(team_id) is None:
                continue
            played = played_by_year.get(final.year, [])
            if final.year == open_year:
                bracket = self._derive(world, final, played, league_ids)
                champion = _open_champion(final, bracket)
            else:
                champion = _recorded_champion(final.year, recorded.get(final.year))
                if not played:
                    if champion.team_id is not None:
                        raise InvalidPostseason(
                            f"{final.year}年の日本一の記録がありますが、その年のポストシーズンの試合がありません。"
                        )
                    club[final.year] = ClubPostseason(year=final.year, label="", won=False)
                    continue
                bracket = self._derive(world, final, played, league_ids, unplayed=PostseasonFormat.NONE)
                if bracket.champion != champion.team_id:
                    raise InvalidPostseason(
                        f"{final.year}年の日本一の記録と、その年の試合から導いた優勝球団が食い違います。"
                    )
            club[final.year] = _club_result(final.year, bracket, champion, team_id, title)
        return club

    # --- 読む・導く ---

    def _recorded(self) -> dict[int, ChampionshipRow]:
        return {row.year: row for row in self._championships.list_championships()}

    def _derive(
        self,
        world: WorldContext,
        final: SeasonFinal,
        results: Sequence[PostseasonResult],
        league_ids: Sequence[int],
        *,
        unplayed: PostseasonFormat | None = None,
    ) -> PostseasonBracket:
        """その年の勝ち上がり。試合の無い年の方式は `unplayed`（省くと世界のいまの方式＝いまの年度）。"""
        if self._start_year is None:
            self._start_year = self._worlds.find_by_id(world.world_id).start_year
        return derive_bracket(
            final.year,
            finishes=final.finishes,
            last_regular=final.last_regular_date,
            results=results,
            unplayed_format=world.postseason_format if unplayed is None else unplayed,
            league_ids=league_ids,
            start_year=self._start_year,
        )


def _recorded_champion(year: int, row: ChampionshipRow | None) -> YearChampion:
    """締めた年の日本一（記録が無ければ、その年はポストシーズンが無かった）。"""
    return YearChampion(
        year=year,
        team_id=row.team_id if row is not None else None,
        team_name=row.team_name if row is not None else "",
        in_progress=False,
    )


def _open_champion(final: SeasonFinal, bracket: PostseasonBracket) -> YearChampion:
    """締めていない年（いまの年度）の日本一。導いた勝ち上がりから。"""
    champion = bracket.champion
    name = (
        next((row.team_name for league in final.standings for row in league.rows if row.team_id == champion), "")
        if champion is not None
        else ""
    )
    return YearChampion(year=final.year, team_id=champion, team_name=name, in_progress=not bracket.status.is_settled)


def _club_result(
    year: int, bracket: PostseasonBracket, champion: YearChampion, team_id: int, title: str
) -> ClubPostseason:
    """導いた勝ち上がりと日本一から、球団のその年の結果。日本一は `champion`（締めた年は記録）から取る。"""
    if champion.team_id == team_id:
        return ClubPostseason(year=year, label=title, won=True)
    outcome = bracket.outcome_of(team_id)
    if outcome is not None:
        return ClubPostseason(year=year, label=outcome.label, won=False)
    if champion.in_progress:
        return ClubPostseason(year=year, label=IN_PROGRESS, won=False)
    # ポストシーズンが無い（方式「なし」・対戦が組めない）
    return ClubPostseason(year=year, label="", won=False)
