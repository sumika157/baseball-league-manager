"""既存の投球・守備の明細に、チーム（試合のホームかビジター）を入れる（#396）。

履歴のモデル（apps.get_model）だけを使う。現行のドメインのコードには依存しない。
決め方は打撃の明細の 0035 と同じで、打席を先に見る。

- 投球: その投手が投げた打席の、守っている側（表ならホーム、裏ならビジター）。
- 守備: 同じ試合の打撃の明細のチーム → 同じ試合の投球の明細のチーム → 失策を記録した打席の守っている側。
- どれからも決まらない行は、その年に在籍していたチームのうち、試合のホームかビジターに当たるものが
  1つだけのとき、それ。

それでも決まらない行があれば、推測で埋めずにここで止まる（試合と選手の id を示す）。
大半の行は SQL の UPDATE 1本ずつで埋める（ペナントの世界を含めると明細は数百万行になりうる）。
"""

from django.db import migrations
from django.db.models import Case, F, OuterRef, Subquery, When
from django.db.models.functions import Coalesce


def _team_by_stints(candidates, stints, year):
    """在籍したチームのうち、試合に出ているチームがちょうど1つならそれ。"""
    teams = {
        team_id for team_id, from_year, to_year in stints if team_id in candidates and from_year <= year <= (to_year or 9999)
    }
    return teams.pop() if len(teams) == 1 else None


def _fill_by_stints(model, apps):
    """SQL で決まらなかった行を、在籍から埋める。決まらない行の (試合, 選手) を返す。"""
    Stint = apps.get_model('myapp', 'PlayerStint')
    rows = list(model.objects.filter(team__isnull=True).select_related('game'))
    if not rows:
        return []
    stints = {}
    for player_id, team_id, from_year, to_year in Stint.objects.filter(
        player_id__in={row.player_id for row in rows}
    ).values_list('player_id', 'team_id', 'from_year', 'to_year'):
        stints.setdefault(player_id, []).append((team_id, from_year, to_year))
    unresolved = []
    for row in rows:
        game = row.game
        team_id = _team_by_stints({game.home_team_id, game.away_team_id}, stints.get(row.player_id, []), game.year)
        if team_id is None:
            unresolved.append((row.game_id, row.player_id))
        row.team_id = team_id
    model.objects.bulk_update([row for row in rows if row.team_id is not None], ['team'], batch_size=500)
    return unresolved


def backfill(apps, schema_editor):
    PitchingLine = apps.get_model('myapp', 'GamePitchingLine')
    FieldingLine = apps.get_model('myapp', 'GameFieldingLine')
    BattingLine = apps.get_model('myapp', 'GameBattingLine')
    PlateAppearance = apps.get_model('myapp', 'GamePlateAppearance')
    FieldingError = apps.get_model('myapp', 'GameFieldingError')

    # 守っている側: 表（is_bottom=False）はホーム、裏はビジター
    pitched_side = (
        PlateAppearance.objects.filter(game_id=OuterRef('game_id'), pitcher_id=OuterRef('player_id'))
        .annotate(side=Case(When(is_bottom=True, then=F('game__away_team_id')), default=F('game__home_team_id')))
        .values('side')[:1]
    )
    PitchingLine.objects.filter(team__isnull=True).update(team_id=Subquery(pitched_side))

    batted = BattingLine.objects.filter(game_id=OuterRef('game_id'), player_id=OuterRef('player_id')).values('team_id')[:1]
    pitched = PitchingLine.objects.filter(game_id=OuterRef('game_id'), player_id=OuterRef('player_id')).values('team_id')[:1]
    erred = (
        FieldingError.objects.filter(plate_appearance__game_id=OuterRef('game_id'), player_id=OuterRef('player_id'))
        .annotate(
            side=Case(
                When(plate_appearance__is_bottom=True, then=F('plate_appearance__game__away_team_id')),
                default=F('plate_appearance__game__home_team_id'),
            )
        )
        .values('side')[:1]
    )
    FieldingLine.objects.filter(team__isnull=True).update(
        team_id=Coalesce(Subquery(batted), Subquery(pitched), Subquery(erred))
    )

    unresolved = [('投球', *key) for key in _fill_by_stints(PitchingLine, apps)]
    unresolved += [('守備', *key) for key in _fill_by_stints(FieldingLine, apps)]
    if unresolved:
        shown = ', '.join(f'{kind}/試合{g}/選手{p}' for kind, g, p in unresolved[:20])
        raise RuntimeError(
            f'チームを決められない明細が {len(unresolved)} 行あります（{shown}）。'
            '推測では埋めません。在籍の記録を直してから、もう一度 migrate してください。'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0070_pitching_fielding_line_team'),
    ]

    operations = [
        # 戻すときは 0070 が列ごと消すので、ここでは何もしない
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
