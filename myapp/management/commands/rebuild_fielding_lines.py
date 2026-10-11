"""全試合の守備成績を、打席の記録から導き直して保存する。

守備成績（`GameFieldingLine`）は打席から導いた値で、通算成績の集計のために保存している。
導き方（経路の読み方・守備位置の解決）を直したときや、既存の試合に後から守備成績を
付けるとき（マイグレーションは既存の試合の守備行を作らない。migrate の後に流す）に使う。
何度流しても同じ結果になる。

打席の無い試合には行を作らない。記録が成立していない試合（組み立てられない）は飛ばし、
最後に件数と試合 id を出す。**更新するのは守備成績の行だけ**で、打席や打撃・投球には触れない
（集約を保存し直すと打席を消して入れ直すことになるため）。
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.domain import services as domain_services
from myapp.domain.exceptions import DomainError
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository
from myapp.infrastructure.scoping import games_in


class Command(BaseCommand):
    help = "全試合の守備成績を、打席の記録から導き直して保存する"

    def handle(self, *args, **options):
        # 対象は実データの試合だけ（ペナントの世界の試合は、その世界を進める側が導く）
        scope = WorldScope.real()
        repository = DjangoGameRepository(scope)
        game_ids = sorted(
            orm_models.GamePlateAppearance.objects.filter(game__in=games_in(scope).values("pk"))
            .order_by()
            .values_list("game_id", flat=True)
            .distinct()
        )

        rebuilt = 0
        skipped: list[int] = []
        for game_id in game_ids:
            try:
                game = repository.find_by_id(game_id)
            except (DomainError, ValueError):
                # 記録が成立していない試合。ここで止めると全体が使えなくなるので飛ばす
                skipped.append(game_id)
                continue

            rows = [
                orm_models.GameFieldingLine(
                    game_id=game_id,
                    player_id=player_id,
                    putouts=line.putouts,
                    assists=line.assists,
                    errors=line.errors,
                    double_plays_turned=line.double_plays_turned,
                )
                for player_id, line in domain_services.fielding_lines_for(game).items()
            ]
            with transaction.atomic():
                orm_models.GameFieldingLine.objects.filter(game_id=game_id).delete()
                orm_models.GameFieldingLine.objects.bulk_create(rows, batch_size=500)
            rebuilt += 1

        self.stdout.write(f"守備成績を導き直しました: {rebuilt}試合")
        if skipped:
            self.stderr.write(
                f"組み立てられず飛ばした試合: {len(skipped)}件（試合id={', '.join(map(str, skipped))}）。"
                "スコアブックの編集画面から保存し直すと揃います。"
            )
