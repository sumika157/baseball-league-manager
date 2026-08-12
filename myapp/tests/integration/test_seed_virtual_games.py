"""仮想の試合データの投入。打席から導いた成績が、保存された明細と一致すること。

投入コマンドは ORM へ直接 bulk_create で書くため、集約の検査を素通りする。
ここで「投入したデータが集約として成立しているか」を確かめる
（成立していなくても例外にはならず、画面の数字が静かにずれるだけになる）。
"""

from dataclasses import fields

from django.core.management import call_command

from myapp.domain import services as domain_services
from myapp.domain.value_objects import BattingLine, PitchingLine
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository

from .base import BaseCase

YEAR = 2026
GAMES_PER_PAIR = 3


class SeedVirtualGamesTest(BaseCase):
    """2チームで数試合だけ投入し、記録の整合を隅まで確かめる。"""

    def setUp(self):
        super().setUp()
        for team in (self.team, self.rival):
            self._fill_roster(team)
        call_command(
            "seed_virtual_games",
            year=YEAR,
            games_per_pair=GAMES_PER_PAIR,
            seed=20260812,
            verbosity=0,
        )
        self.games = [DjangoGameRepository().find_by_id(row.id) for row in orm_models.Game.objects.all()]

    def _fill_roster(self, team):
        """レギュラー9人＋控えとローテーション5人＋救援が組める人数を入れる。"""
        number = 1
        for label, count in (("捕手", 2), ("内野手", 6), ("外野手", 5), ("指名打者", 1), ("投手", 9)):
            for index in range(count):
                self.service.register_player(team.id, f"{team.name}{label}{index}", number, label)
                number += 1

    def test_every_game_holds_together_as_a_scorebook(self):
        """打順の巡回・塁の再生・得点の一致。集約自身の検算にかける。"""
        self.assertEqual(len(self.games), GAMES_PER_PAIR)
        for game in self.games:
            self.assertTrue(game.plate_appearances, "打席が記録されていません")
            game.ensure_plate_appearances_consistent()
            game.ensure_line_score_matches()

    def test_batting_lines_match_the_plate_appearances(self):
        """保存された打撃成績が、打席から数え直した値と一致すること。

        ずれても例外にはならず、ボックススコアと経過が食い違うだけになる。
        """
        for game in self.games:
            stored = orm_models.GameBattingLine.objects.filter(game_id=game.id)
            self.assertTrue(stored.exists())
            for row in stored:
                counted = domain_services.batting_line_for(game.plate_appearances, row.player_id)
                # **項目を1つずつ並べず、値オブジェクトの全フィールドを突き合わせる。**
                # 一部だけ見ていると、増やした項目が 0 のまま保存されても気づけない
                # （実際に起きた。投入コマンドが項目を独自に列挙していた）
                self.assertEqual(
                    {f.name: getattr(row, f.name) for f in fields(BattingLine)},
                    {f.name: getattr(counted, f.name) for f in fields(BattingLine)},
                    f"打撃成績が打席と食い違っています（試合 {game.id} / 選手 {row.player_id}）",
                )

    def test_pitching_lines_match_the_plate_appearances(self):
        for game in self.games:
            for row in orm_models.GamePitchingLine.objects.filter(game_id=game.id):
                counted = domain_services.pitching_line_for(game.plate_appearances, row.player_id)
                self.assertEqual(row.innings_pitched, float(counted.innings.to_notation()))
                # 勝敗・セーブ・先発登板は打席からは決まらないので突き合わせない
                derived = {"innings", "wins", "losses", "saves", "holds", "starts", "relief_wins"}
                self.assertEqual(
                    {f.name: getattr(row, f.name) for f in fields(PitchingLine) if f.name not in derived},
                    {f.name: getattr(counted, f.name) for f in fields(PitchingLine) if f.name not in derived},
                    f"投球成績が打席と食い違っています（試合 {game.id} / 選手 {row.player_id}）",
                )

    def test_every_out_belongs_to_a_pitcher(self):
        """アウトの合計と投球回の合計が一致すること。

        合わないと、誰も投げていない回が生まれて失点の帰属先が無くなる。
        """
        for game in self.games:
            recorded = sum(entry.outs_recorded for entry in game.plate_appearances)
            pitched = sum(row.outs for row in (p.line.innings for p in game.pitching))
            self.assertEqual(recorded, pitched)

    def test_runs_batted_in_never_exceed_the_runs(self):
        """打点は還った走者の内数。失策や野選で還った得点には付かない。"""
        for game in self.games:
            runs = sum(entry.runs_scored for entry in game.plate_appearances)
            batted_in = sum(entry.runs_batted_in for entry in game.plate_appearances)
            self.assertLessEqual(batted_in, runs)
            self.assertEqual(runs, game.home_score + game.away_score)

    def test_the_game_ends_the_way_the_rules_say(self):
        """9回以降のホームの攻撃。リードしていれば行わず、逆転すれば打ち切る。"""
        for game in self.games:
            score = game.derived_line_score()
            if game.home_score > game.away_score and len(score.home) >= len(score.away):
                # サヨナラか、9回裏を戦って勝った試合。裏の得点が最後に入っている
                self.assertGreater(len(score.home), 0)
            if game.home_score < game.away_score:
                # 負けているホームは最後まで攻撃する
                self.assertEqual(len(score.home), len(score.away))

    def test_advances_and_errors_are_persisted(self):
        """進塁が1件も保存されないと、得点も打点も導けなくなる。"""
        advances = orm_models.GameRunnerAdvance.objects.count()
        self.assertGreaterEqual(advances, sum(len(g.plate_appearances) for g in self.games))
        for game in self.games:
            for entry in game.plate_appearances:
                self.assertTrue(entry.advances, f"打席 {entry.sequence} に進塁がありません")
