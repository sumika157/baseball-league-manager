"""入団年（debut_year）をドラフト年に移す（#108）。2/3: データ移行。

- 日本人選手（`is_foreign_player=False`）で入団年がある選手は、ドラフト年 ＝ 入団年 − 1（入団はドラフトの翌年）。
  順位は分からないので空。育成ドラフトかどうかは、**最初の在籍の経路が「育成ドラフト」なら育成**にする（それ以外は本指名）。
  移さないと、育成ドラフトで入った選手（開発 DB の実測で909人。#175 の仮想選手）が経路とドラフトの整合
  （`ensure_draft_fits_career`）に反し、管理画面で保存できなくなる。
  移すとシーズンの範囲（1900〜2100）を外れる入団年（1900）は空のまま。
- 外国人選手はドラフトを経ていないので空のまま（プロ入りは最初の在籍の加入年で分かる。開発 DB の実測で、
  全員の入団年が最初の在籍の加入年と一致していた）。
- ペナントの世界の選手も同じ表なので同じ規則で移る（自動ドラフトの新人は 入団年 ＝ 締めた年 ＋ 1 なので、
  ドラフト年 ＝ 締めた年になる）。

逆方向は、ドラフト年のある選手を 入団年 ＝ ドラフト年 ＋ 1 に、外国人選手を最初の在籍の加入年に戻す。
"""

from django.db import migrations
from django.db.models import F, Min

# `AcquisitionRoute.DEVELOPMENTAL_DRAFT` の値（ドメイン）。マイグレーションはその時点の値で固定する
_DEVELOPMENTAL_DRAFT = "育成ドラフト"
# `Season.MIN_YEAR`（ドメイン）。マイグレーションはその時点の値で固定する（後で変わっても移行の結果を変えない）
_MIN_DRAFT_YEAR = 1900


def forward(apps, schema_editor):
    Player = apps.get_model("myapp", "Player")
    PlayerStint = apps.get_model("myapp", "PlayerStint")
    Player.objects.filter(
        is_foreign_player=False, debut_year__isnull=False, debut_year__gt=_MIN_DRAFT_YEAR
    ).update(draft_year=F("debut_year") - 1, draft_round=None, is_development_draft=False)
    # 最初の在籍（加入年が最も早い在籍）の経路が育成ドラフトの選手は、育成ドラフトの指名にする
    first_years = dict(
        PlayerStint.objects.values("player_id").annotate(first=Min("from_year")).order_by().values_list("player_id", "first")
    )
    developmental = sorted(
        {
            player_id
            for player_id, from_year in PlayerStint.objects.filter(acquired_via=_DEVELOPMENTAL_DRAFT).values_list(
                "player_id", "from_year"
            )
            if first_years.get(player_id) == from_year
        }
    )
    for start in range(0, len(developmental), 500):
        Player.objects.filter(pk__in=developmental[start : start + 500], draft_year__isnull=False).update(
            is_development_draft=True
        )


def backward(apps, schema_editor):
    Player = apps.get_model("myapp", "Player")
    PlayerStint = apps.get_model("myapp", "PlayerStint")
    Player.objects.filter(draft_year__isnull=False).update(debut_year=F("draft_year") + 1)
    # ドラフトの無い外国人選手は、最初の在籍の加入年に戻す（選手ごとの最小を1本で読む）
    first_years = dict(
        PlayerStint.objects.filter(player__is_foreign_player=True, player__draft_year__isnull=True)
        .values("player_id")
        .annotate(first=Min("from_year"))
        .order_by()
        .values_list("player_id", "first")
    )
    players = list(Player.objects.filter(pk__in=first_years))
    for player in players:
        player.debut_year = first_years[player.pk]
    Player.objects.bulk_update(players, ["debut_year"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0052_player_draft_fields"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
