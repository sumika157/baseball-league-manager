"""ペナントの世界の設定（ポストシーズンの方式・ドラフトの指名順の規則）。

方式は世界を作るときに選び、後からこの画面で変えられる（#39 のユーザーの決定）。**方式は年ごとに保存しない**:
過去の年の勝ち上がりは、その年の試合から方式を導く（domain の `played_format`。#280 のユーザーの決定）。
そのため、変えられるのは、いまの年度のポストシーズンがまだ始まっていない間だけ（始まった年の方式が変わると、
その年の勝ち上がりが導けなくなる）。終了した世界は変えられない（変えても使われない）。条件の出典は domain の
`world.postseason_format_change_blocker`。

**ロック（#100）**: 書き込みのトランザクションの中で、いまの年度のポストシーズンの試合を読み直してから変える。
進める側（`PennantPostseasonService._save`）も書く前に方式を読み直すので、計算と変更が重なっても、
古い方式の試合を書かない（変更が先なら進める側が `AlreadyAdvanced`、進める側が先なら変更が `PostseasonFormatLocked`）。

**ドラフトの指名順の規則**（#198）も世界を作るときに選び、後からこの画面で変えられる。規則は年ごとに保存しない
（締めた年の新人の巡は `Profile.draft` に残る）。会議はシーズンを締める処理の中で全自動で行うので、締める前ならいつでも
変えられ、その年のオフの会議から新しい規則になる（条件の出典は domain の `world.draft_order_rule_change_blocker`）。
締める処理は規則を `_state()` に入れて計算の前後で比べるので、計算中に変えたら古い規則の新人を書かずに断る。
"""

from __future__ import annotations

from ..domain.exceptions import DraftOrderRuleLocked, PostseasonFormatLocked
from ..domain.pennant.draft_order import DraftOrderRule
from ..domain.pennant.postseason import PostseasonFormat
from ..domain.pennant.season import SeasonPhase, is_final_season, season_phase, season_year
from ..domain.pennant.world import World, draft_order_rule_change_blocker, postseason_format_change_blocker
from ..domain.repositories import FixtureRepository, WorldRepository
from .dto import PennantSettingsView
from .pennant_postseason import PostseasonBracketReader
from .pennant_season import AtomicBlock
from .queries import PostseasonQuery, SimulationContextQuery


class PennantSettingsService:
    """世界ひとつの設定。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す（世界の台帳は範囲を持たない）。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        fixtures: FixtureRepository,
        context_query: SimulationContextQuery,
        postseason: PostseasonQuery,
        brackets: PostseasonBracketReader,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._fixtures = fixtures
        self._context_query = context_query
        self._postseason = postseason
        self._brackets = brackets
        self._atomic = atomic

    def view(self) -> PennantSettingsView:
        """設定の画面の材料（いまの方式・規則と、変えられない理由）。"""
        world = self._worlds.find_by_id(self._world_id)
        year, world_over = self._season(world)
        return PennantSettingsView(
            postseason_format=world.postseason_format,
            blocker=self._format_blocker(year, world_over) or "",
            draft_order_rule=world.draft_order_rule,
            draft_order_blocker=draft_order_rule_change_blocker(world_over=world_over) or "",
        )

    def change_postseason_format(self, postseason_format: PostseasonFormat) -> bool:
        """方式を変える。変えたら True、いまと同じなら何もせず False。

        変えられないとき（いまの年度のポストシーズンが始まっている・世界が終了している）は `PostseasonFormatLocked`
        で、何も書かない。検査はトランザクションの中で読み直す。
        """
        with self._atomic():
            world = self._worlds.find_by_id(self._world_id)
            blocker = self._format_blocker(*self._season(world))
            if blocker is not None:
                raise PostseasonFormatLocked(blocker)
            if world.postseason_format is postseason_format:
                return False
            world.postseason_format = postseason_format
            self._worlds.save(world)
            return True

    def change_draft_order_rule(self, rule: DraftOrderRule) -> bool:
        """ドラフトの指名順の規則を変える。変えたら True、いまと同じなら何もせず False。

        変えられないとき（世界が終了している）は `DraftOrderRuleLocked` で、何も書かない。
        検査はトランザクションの中で読み直す。
        """
        with self._atomic():
            world = self._worlds.find_by_id(self._world_id)
            _, world_over = self._season(world)
            blocker = draft_order_rule_change_blocker(world_over=world_over)
            if blocker is not None:
                raise DraftOrderRuleLocked(blocker)
            if world.draft_order_rule is rule:
                return False
            world.draft_order_rule = rule
            self._worlds.save(world)
            return True

    def _season(self, world: World) -> tuple[int, bool]:
        """いまの年度と、終了した世界か（最終シーズンを終え、ポストシーズンも終わった（または無い）。
        `WorldContext.is_over` と同じ条件）。"""
        last_played = self._context_query.last_played_on()
        next_fixture = self._fixtures.first_date()
        year = season_year(next_fixture_on=next_fixture, last_played_on=last_played, start_year=world.start_year)
        world_over = (
            is_final_season(current_year=year, start_year=world.start_year)
            and season_phase(last_played_on=last_played, next_fixture_on=next_fixture) is SeasonPhase.FINISHED
            and self._brackets.status(world, year).is_settled
        )
        return year, world_over

    def _format_blocker(self, year: int, world_over: bool) -> str | None:
        started = bool(self._postseason.results(year))
        return postseason_format_change_blocker(started=started, world_over=world_over)
