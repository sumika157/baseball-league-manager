"""ペナントの世界の設定（ポストシーズンの方式）。

方式は世界を作るときに選び、後からこの画面で変えられる（#39 のユーザーの決定）。**方式は年ごとに保存しない**:
過去の年の勝ち上がりは、その年の試合から方式を導く（domain の `played_format`。#280 のユーザーの決定）。
そのため、変えられるのは、いまの年度のポストシーズンがまだ始まっていない間だけ（始まった年の方式が変わると、
その年の勝ち上がりが導けなくなる）。終了した世界は変えられない（変えても使われない）。条件の出典は domain の
`world.postseason_format_change_blocker`。

**ロック（#100）**: 書き込みのトランザクションの中で、いまの年度のポストシーズンの試合を読み直してから変える。
進める側（`PennantPostseasonService._save`）も書く前に方式を読み直すので、計算と変更が重なっても、
古い方式の試合を書かない（変更が先なら進める側が `AlreadyAdvanced`、進める側が先なら変更が `PostseasonFormatLocked`）。
"""

from __future__ import annotations

from ..domain.exceptions import PostseasonFormatLocked
from ..domain.pennant.postseason import PostseasonFormat
from ..domain.pennant.season import SeasonPhase, is_final_season, season_phase, season_year
from ..domain.pennant.world import World, postseason_format_change_blocker
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
        """設定の画面の材料（いまの方式と、変えられない理由）。"""
        world = self._worlds.find_by_id(self._world_id)
        return PennantSettingsView(postseason_format=world.postseason_format, blocker=self._blocker(world) or "")

    def change_postseason_format(self, postseason_format: PostseasonFormat) -> bool:
        """方式を変える。変えたら True、いまと同じなら何もせず False。

        変えられないとき（いまの年度のポストシーズンが始まっている・世界が終了している）は `PostseasonFormatLocked`
        で、何も書かない。検査はトランザクションの中で読み直す。
        """
        with self._atomic():
            world = self._worlds.find_by_id(self._world_id)
            blocker = self._blocker(world)
            if blocker is not None:
                raise PostseasonFormatLocked(blocker)
            if world.postseason_format is postseason_format:
                return False
            world.postseason_format = postseason_format
            self._worlds.save(world)
            return True

    def _blocker(self, world: World) -> str | None:
        last_played = self._context_query.last_played_on()
        next_fixture = self._fixtures.first_date()
        year = season_year(next_fixture_on=next_fixture, last_played_on=last_played, start_year=world.start_year)
        started = bool(self._postseason.results(year))
        # 終了した世界: 最終シーズンを終え、ポストシーズンも終わった（または無い）。`WorldContext.is_over` と同じ条件
        world_over = (
            is_final_season(current_year=year, start_year=world.start_year)
            and season_phase(last_played_on=last_played, next_fixture_on=next_fixture) is SeasonPhase.FINISHED
            and self._brackets.status(world, year).is_settled
        )
        return postseason_format_change_blocker(started=started, world_over=world_over)
