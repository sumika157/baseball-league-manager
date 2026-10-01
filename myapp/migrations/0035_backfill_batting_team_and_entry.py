"""既存のラインアップの行に、チームと出場した打席を入れる。

履歴のモデル（apps.get_model）だけを使う。現行のドメインのコードには依存しない。

チーム（必須にする前に全行を埋める）は、次の順で決める。
1. 打席に現れた側（打者・投手・走者・代走・失策の守備者）。
2. 打席に現れない選手は、その年に在籍していたチームのうち、試合のホームかビジターに当たる
   ものが1つだけのとき、それ。
打席に現れた側が在籍の記録と食い違うとき、どちらからも決まらないときは、推測で埋めずに
ここで止まる（試合と選手の id を示す）。

出場した打席は、途中出場（交代の順が1以上）のうち、代打は初めて打席に立った打席、
代走は代走を出した打席、投手は初めて投げた打席。**それ以外の途中出場は不明のまま（空）**にする。
スタメンは試合開始からなので空。
"""

from django.db import migrations

CHUNK = 200
PINCH_HITTER = '打'
PINCH_RUNNER = '走'
PITCHER = '投'


def _team_by_stints(candidates, stints, year):
    """在籍したチームのうち、試合に出ているチームがちょうど1つならそれ。"""
    teams = {team_id for team_id, from_year, to_year in stints if team_id in candidates and from_year <= year <= (to_year or 9999)}
    return teams.pop() if len(teams) == 1 else None


def backfill(apps, schema_editor):
    Game = apps.get_model('myapp', 'Game')
    BattingLine = apps.get_model('myapp', 'GameBattingLine')
    PlateAppearance = apps.get_model('myapp', 'GamePlateAppearance')
    Advance = apps.get_model('myapp', 'GameRunnerAdvance')
    Substitution = apps.get_model('myapp', 'GameRunnerSubstitution')
    FieldingError = apps.get_model('myapp', 'GameFieldingError')
    Stint = apps.get_model('myapp', 'PlayerStint')

    unresolved = []
    conflicts = []
    game_ids = list(Game.objects.values_list('id', flat=True).order_by('id'))
    for start in range(0, len(game_ids), CHUNK):
        chunk = game_ids[start:start + CHUNK]
        games = {g['id']: g for g in Game.objects.filter(id__in=chunk).values('id', 'year', 'home_team_id', 'away_team_id')}
        lines = list(BattingLine.objects.filter(game_id__in=chunk))
        if not lines:
            continue

        # 打席に現れた側。値は「そのチームが裏に攻めるか」ではなく、チーム id にして持つ
        evidence = {}
        first_bat = {}
        first_pitch = {}
        for game_id, sequence, is_bottom, batter_id, pitcher_id in PlateAppearance.objects.filter(
            game_id__in=chunk
        ).values_list('game_id', 'sequence', 'is_bottom', 'batter_id', 'pitcher_id').order_by('sequence'):
            game = games[game_id]
            batting, fielding = (
                (game['home_team_id'], game['away_team_id']) if is_bottom else (game['away_team_id'], game['home_team_id'])
            )
            evidence.setdefault((game_id, batter_id), batting)
            evidence.setdefault((game_id, pitcher_id), fielding)
            first_bat.setdefault((game_id, batter_id), sequence)
            first_pitch.setdefault((game_id, pitcher_id), sequence)
        for game_id, runner_id, is_bottom in Advance.objects.filter(
            plate_appearance__game_id__in=chunk
        ).values_list('plate_appearance__game_id', 'runner_id', 'plate_appearance__is_bottom'):
            game = games[game_id]
            evidence.setdefault((game_id, runner_id), game['home_team_id'] if is_bottom else game['away_team_id'])
        for game_id, player_id, is_bottom in FieldingError.objects.filter(
            plate_appearance__game_id__in=chunk
        ).values_list('plate_appearance__game_id', 'player_id', 'plate_appearance__is_bottom'):
            game = games[game_id]
            evidence.setdefault((game_id, player_id), game['away_team_id'] if is_bottom else game['home_team_id'])

        substituted = {}
        for game_id, entering_id, sequence, is_bottom in Substitution.objects.filter(
            plate_appearance__game_id__in=chunk
        ).values_list('plate_appearance__game_id', 'entering_runner_id', 'plate_appearance__sequence', 'plate_appearance__is_bottom').order_by('plate_appearance__sequence'):
            substituted.setdefault((game_id, entering_id), sequence)
            game = games[game_id]
            evidence.setdefault((game_id, entering_id), game['home_team_id'] if is_bottom else game['away_team_id'])

        stints = {}
        for player_id, team_id, from_year, to_year in Stint.objects.filter(
            player_id__in={line.player_id for line in lines}
        ).values_list('player_id', 'team_id', 'from_year', 'to_year'):
            stints.setdefault(player_id, []).append((team_id, from_year, to_year))

        for line in lines:
            game = games[line.game_id]
            candidates = {game['home_team_id'], game['away_team_id']}
            player_stints = stints.get(line.player_id, [])
            seen = evidence.get((line.game_id, line.player_id))
            by_stint = _team_by_stints(candidates, player_stints, game['year'])
            if seen is not None and by_stint is not None and seen != by_stint:
                conflicts.append((line.game_id, line.player_id))
                continue
            team_id = seen if seen is not None else by_stint
            if team_id is None:
                unresolved.append((line.game_id, line.player_id))
                continue
            line.team_id = team_id

            entered = None
            if line.slot_sequence >= 1:
                if line.fielding_position == PINCH_HITTER:
                    entered = first_bat.get((line.game_id, line.player_id))
                elif line.fielding_position == PITCHER:
                    entered = first_pitch.get((line.game_id, line.player_id))
                elif line.fielding_position == PINCH_RUNNER:
                    entered = substituted.get((line.game_id, line.player_id))
            line.entered_sequence = entered

        BattingLine.objects.bulk_update(
            [line for line in lines if line.team_id is not None], ['team', 'entered_sequence'], batch_size=500
        )

    if unresolved or conflicts:
        parts = []
        if conflicts:
            shown = ', '.join(f'試合{g}/選手{p}' for g, p in conflicts[:20])
            parts.append(f'打席に現れた側が在籍の記録と食い違う行が {len(conflicts)} 行あります（{shown}）。')
        if unresolved:
            shown = ', '.join(f'試合{g}/選手{p}' for g, p in unresolved[:20])
            parts.append(f'チームを決められない行が {len(unresolved)} 行あります（{shown}）。')
        raise RuntimeError(''.join(parts) + '推測では埋めません。在籍の記録を直してから、もう一度 migrate してください。')


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0034_gamefieldingline_and_batting_entry'),
    ]

    operations = [
        # 戻すときは 0034 が列ごと消すので、ここでは何もしない
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
