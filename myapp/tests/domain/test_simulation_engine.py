"""試合シミュレーションエンジンの単体テスト。Django も DB も使わない。

守りたいこと:
- 同じシードなら同じ打席列になる（再現できる）
- 生成した試合が、スコアブックとして成立し、成績が打席と一致する
- 延長は12回まで・引分、ホームがリードなら9回裏を省く、サヨナラは決勝点で終わる
- 守備位置が空かない（代打を出したら次の守備から誰かがその位置に就く）、入った打席が正しい
- `fielded_by` は結果ごとの固定経路ではなく散らばる。併殺は3人、三振は空
- 先発は中5日、救援は3連投しない、外国人の出場枠を守る
"""

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta
from unittest import TestCase

from myapp.domain.services import (
    ensure_lines_match_plate_appearances,
    fielders_by_plate_appearance,
)
from myapp.domain.simulation.engine import MAX_INNINGS, SimulatedGame, simulate_game
from myapp.domain.simulation.manager import PitchingHistory, choose_active_roster
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.simulation.samples import average_club, round_robin_days, spread_league
from myapp.domain.value_objects import FieldingPosition, PlateAppearanceResult

FP = FieldingPosition
P = PlateAppearanceResult
OPENING_DAY = date(2026, 4, 1)


class ScriptedRandom:
    """決めた列を繰り返す乱数源。"""

    def __init__(self, *values: float) -> None:
        self.values = values
        self.index = 0

    def random(self) -> float:
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return value


def _play(seed: int, home, away, *, day: date = OPENING_DAY, history=None, **kwargs) -> SimulatedGame:
    return simulate_game(make_random(seed), home, away, played_on=day, history=history, **kwargs)


class SameSeedTest(TestCase):
    def setUp(self):
        self.home = choose_active_roster(average_club(1))
        self.away = choose_active_roster(average_club(2))

    def test_the_same_seed_gives_the_same_plate_appearances(self):
        first = _play(42, self.home, self.away)
        second = _play(42, self.home, self.away)
        self.assertEqual(first.plate_appearances, second.plate_appearances)
        self.assertEqual(first.lineup, second.lineup)
        self.assertEqual(
            (first.game.home_score, first.game.away_score), (second.game.home_score, second.game.away_score)
        )

    def test_a_different_seed_gives_a_different_game(self):
        outcomes = {tuple(p.result for p in _play(seed, self.home, self.away).plate_appearances) for seed in range(5)}
        self.assertGreater(len(outcomes), 1)

    def test_the_game_seed_makes_a_game_independent_of_the_order_it_is_played_in(self):
        """試合ごとのシードは試合の識別から作る。何試合目に回しても同じ試合になる。"""

        def play(key):
            return _play(game_seed(7, 2026, key), self.home, self.away).plate_appearances

        order_a = {key: play(key) for key in ("a", "b", "c")}
        order_b = {key: play(key) for key in ("c", "b", "a")}
        self.assertEqual(order_a, order_b)


class ScriptedGameTest(TestCase):
    def test_a_game_without_a_runner_is_a_twelve_inning_scoreless_tie(self):
        """打者が一人も出塁しなければ、12回まで行って引き分ける（NPB の規定）。"""
        home = choose_active_roster(average_club(1))
        away = choose_active_roster(average_club(2))
        played = simulate_game(ScriptedRandom(0.99), home, away, played_on=OPENING_DAY)
        game = played.game
        self.assertEqual(game.line_score.innings, MAX_INNINGS)
        self.assertEqual(len(game.line_score.home), MAX_INNINGS)
        self.assertEqual((game.home_score, game.away_score), (0, 0))
        self.assertTrue(game.is_tie)
        self.assertIsNone(game.winner_team_id)
        self.assertEqual(len(played.plate_appearances), MAX_INNINGS * 2 * 3)
        # 引分では勝敗もセーブも付かない
        self.assertTrue(all(o.line.wins == o.line.losses == o.line.saves == 0 for o in game.pitching))
        ensure_lines_match_plate_appearances(game)

    def test_a_broken_random_source_cannot_loop_forever(self):
        """全打席が死球になる乱数源でも、半回が終わらないままにはならない。"""
        from myapp.domain.exceptions import InvalidGame

        home = choose_active_roster(average_club(1))
        away = choose_active_roster(average_club(2))
        with self.assertRaises(InvalidGame):
            simulate_game(ScriptedRandom(0.0), home, away, played_on=OPENING_DAY)


class SimulatedGamesTest(TestCase):
    """能力を散らした6球団で、毎日1試合ずつ数十日回した試合の集まり。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        rng = make_random(20260801)
        cls.rosters = {club.team_id: choose_active_roster(club, 4) for club in spread_league(rng, 6)}
        cls.history = PitchingHistory()
        cls.games: list[SimulatedGame] = []
        cls.days: list[date] = []
        for day_index, day in enumerate(round_robin_days(list(cls.rosters), 28)):
            played_on = OPENING_DAY + timedelta(days=day_index)
            for home_id, away_id in day:
                played = _play(
                    game_seed(1, 2026, f"{day_index}-{home_id}"),
                    cls.rosters[home_id],
                    cls.rosters[away_id],
                    day=played_on,
                    history=cls.history,
                )
                cls.games.append(played)
                cls.days.append(played_on)

    def test_every_game_holds_together_as_a_scorebook(self):
        for played in self.games:
            game = played.game
            game.ensure_plate_appearances_consistent()
            game.ensure_line_score_matches()
            ensure_lines_match_plate_appearances(game)

    def test_scores_are_derived_from_the_plate_appearances(self):
        for played in self.games:
            game = played.game
            runs = [
                sum(p.runs_scored for p in played.plate_appearances if p.is_bottom == home) for home in (False, True)
            ]
            self.assertEqual([game.away_score, game.home_score], runs)

    def test_no_game_runs_past_twelve_innings_and_only_twelve_inning_games_tie(self):
        for played in self.games:
            game = played.game
            self.assertLessEqual(game.line_score.innings, MAX_INNINGS)
            self.assertGreaterEqual(game.line_score.innings, 8)
            if game.is_tie:
                self.assertEqual(game.line_score.innings, MAX_INNINGS)
                self.assertEqual(len(game.line_score.home), MAX_INNINGS)
        self.assertTrue(any(p.game.is_tie for p in self.games), "引分が1試合も無い（この検査が空振りしている）")

    def test_extra_innings_are_played_only_when_tied_after_nine(self):
        for played in self.games:
            game = played.game
            if game.line_score.innings <= 9:
                continue
            after_nine = game.line_score.score_after(9, bottom=True)
            self.assertEqual(after_nine[0], after_nine[1], "9回を終えて決着していた試合が延長に入っている")

    def test_the_bottom_of_the_ninth_is_skipped_when_the_home_team_leads(self):
        skipped = 0
        for played in self.games:
            game = played.game
            home_innings = len(game.line_score.home)
            if game.line_score.innings == 9 and home_innings == 8:
                skipped += 1
                self.assertGreater(game.home_score, game.away_score)
                self.assertFalse([p for p in played.plate_appearances if p.is_bottom and p.inning == 9])
        self.assertGreater(skipped, 0, "9回裏を省いた試合が無い（この検査が空振りしている）")

    def test_a_home_team_that_trails_after_the_top_of_the_ninth_still_bats(self):
        for played in self.games:
            game = played.game
            if game.line_score.innings >= 9 and len(game.line_score.home) < game.line_score.innings:
                before_bottom = game.line_score.score_after(game.line_score.innings, bottom=False)
                self.assertGreater(before_bottom[1], before_bottom[0], "リードしていないホームが裏を省かれている")

    def test_a_walk_off_ends_the_game_on_the_deciding_run(self):
        walk_offs = 0
        for played in self.games:
            game = played.game
            last = played.plate_appearances[-1]
            if not (last.is_bottom and last.inning >= 9 and game.home_score > game.away_score):
                continue
            walk_offs += 1
            self.assertGreater(last.runs_scored, 0, "サヨナラなのに最後の打席で得点していない")
            # 最後の打席の得点が無ければ、ホームはまだ勝ち越していない
            self.assertLessEqual(game.home_score - last.runs_scored, game.away_score)
            # その後に3アウト目の打席が続いていない（決勝点で終わる）
            half = [p for p in played.plate_appearances if p.half_inning == last.half_inning]
            self.assertLess(sum(p.outs_recorded for p in half), 3)
        self.assertGreater(walk_offs, 0, "サヨナラ勝ちが1試合も無い（この検査が空振りしている）")

    def test_each_team_fields_nine_distinct_positions_at_every_plate_appearance(self):
        """代打を出したあとも守備位置は空かない（次の守備から誰かがその位置に就く）。"""
        for played in self.games:
            alignments = fielders_by_plate_appearance(played.game)
            for entry in played.plate_appearances:
                alignment = alignments[entry.sequence]
                self.assertEqual(len(alignment), 9, f"seq={entry.sequence} {sorted(p.value for p in alignment)}")
                self.assertEqual(len(set(alignment.values())), 9, "同じ選手が2つの守備位置に就いている")

    def test_the_lineup_records_when_each_substitute_entered(self):
        substitutes = 0
        for played in self.games:
            first_batting: dict[int, int] = {}
            for entry in played.plate_appearances:
                first_batting.setdefault(entry.batter_id, entry.sequence)
            by_sequence = {entry.sequence: entry for entry in played.plate_appearances}
            for row in played.lineup:
                if row.slot_sequence == 0:
                    self.assertIsNone(row.entered_sequence)
                    continue
                substitutes += 1
                self.assertIsNotNone(row.entered_sequence)
                entered = by_sequence[row.entered_sequence]
                if first_batting.get(row.player_id) == row.entered_sequence:
                    # 代打（と、そのまま守る代打）は初めて打席に立った打席から入る
                    self.assertEqual(entered.batter_id, row.player_id)
                else:
                    # 守備固めは、自分のチームが守る半回の最初の打席から入る
                    batting_side_is_bottom = row.team_id == played.game.home_team_id
                    self.assertNotEqual(entered.is_bottom, batting_side_is_bottom)
                    previous = by_sequence.get(row.entered_sequence - 1)
                    self.assertTrue(previous is None or previous.half_inning != entered.half_inning)
        self.assertGreater(substitutes, 0, "途中出場が1人も無い（この検査が空振りしている）")

    def test_a_pinch_hitter_only_appears_in_the_seventh_or_later_when_the_team_is_behind(self):
        found = 0
        for played in self.games:
            by_sequence = {entry.sequence: entry for entry in played.plate_appearances}
            for row in played.lineup:
                if row.slot_sequence == 0 or row.entered_sequence is None:
                    continue
                entry = by_sequence[row.entered_sequence]
                if entry.batter_id != row.player_id:
                    continue  # 守備固め
                found += 1
                self.assertGreaterEqual(entry.inning, 7)

                is_home = row.team_id == played.game.home_team_id
                before = [p for p in played.plate_appearances if p.sequence < entry.sequence]
                own = sum(p.runs_scored for p in before if p.is_bottom == is_home)
                other = sum(p.runs_scored for p in before if p.is_bottom != is_home)
                self.assertLess(own, other)
        self.assertGreater(found, 0, "代打が1度も出ていない（この検査が空振りしている）")

    def test_at_most_two_pinch_hitters_per_team_per_game(self):
        for played in self.games:
            by_sequence = {entry.sequence: entry for entry in played.plate_appearances}
            per_team = Counter(
                row.team_id
                for row in played.lineup
                if row.slot_sequence
                and row.entered_sequence
                and by_sequence[row.entered_sequence].batter_id == row.player_id
            )
            self.assertTrue(all(count <= 2 for count in per_team.values()), per_team)

    def test_strikeouts_have_no_path_and_double_plays_have_three(self):
        seen_double_plays = 0
        for played in self.games:
            for entry in played.plate_appearances:
                if entry.result.is_strikeout:
                    self.assertEqual(entry.fielded_by, ())
                elif entry.is_double_play:
                    seen_double_plays += 1
                    self.assertEqual(len(entry.fielded_by), 3, str(entry))
                elif entry.result in (P.SINGLE, P.DOUBLE, P.TRIPLE, P.HOME_RUN, P.WALK, P.HIT_BY_PITCH):
                    self.assertEqual(entry.fielded_by, ())
        self.assertGreater(seen_double_plays, 10)

    def test_the_fielding_path_is_scattered_across_positions_not_fixed_per_result(self):
        ground_firsts: Counter[FieldingPosition] = Counter()
        fly_firsts: Counter[FieldingPosition] = Counter()
        for played in self.games:
            for entry in played.plate_appearances:
                if entry.result is P.GROUND_OUT and not entry.is_double_play:
                    ground_firsts[entry.fielded_by[0]] += 1
                elif entry.result is P.FLY_OUT:
                    fly_firsts[entry.fielded_by[0]] += 1
        self.assertGreaterEqual(len(ground_firsts), 5)
        self.assertGreaterEqual(len(fly_firsts), 4)
        for position in (*ground_firsts, *fly_firsts):
            self.assertTrue(position.takes_the_field)
        # 遊撃が最も多いが、他の内野手にも飛ぶ
        self.assertEqual(ground_firsts.most_common(1)[0][0], FP.SHORTSTOP)

    def test_every_putout_and_assist_resolves_to_a_player(self):
        """経路の位置が選手に引けない（守備位置が空いている）と、守備成績が静かに欠ける。"""
        for played in self.games[:60]:
            # 盗塁刺・牽制死は打球の処理ではないので、刺殺には数えない
            fielded_outs = sum(
                1
                for p in played.plate_appearances
                for advance in p.advances
                if advance.is_out and not advance.reason.is_baserunning_out
            )
            putouts = sum(f.line.putouts for f in played.game.fielding)
            self.assertEqual(putouts, fielded_outs, "刺殺の合計が打球の処理で取ったアウトの数と合わない")

    def test_starters_are_rested_for_at_least_five_days(self):
        starts: dict[int, list[date]] = defaultdict(list)
        for played in self.games:
            for outing in played.game.pitching:
                if outing.appearance_order == 1:
                    starts[outing.player_id].append(played.game.played_on)
        for player_id, days in starts.items():
            for earlier, later in zip(days, days[1:], strict=False):
                self.assertGreaterEqual((later - earlier).days, 6, f"{player_id}: {days}")
        self.assertGreaterEqual(len(starts), 6 * len(self.rosters) - 6)

    def test_no_reliever_pitches_three_days_in_a_row(self):
        pitched: dict[int, set[date]] = defaultdict(set)
        for played in self.games:
            for outing in played.game.pitching:
                pitched[outing.player_id].add(played.game.played_on)
        for player_id, days in pitched.items():
            for day in days:
                run = {day + timedelta(days=offset) for offset in range(3)}
                self.assertFalse(run <= days, f"{player_id} が{day}から3連投している")

    def test_the_closer_collects_most_of_the_saves(self):
        saves: Counter[int] = Counter()
        for played in self.games:
            for outing in played.game.pitching:
                saves[outing.player_id] += outing.line.saves
        closers = {choose.team_id: self._closer_of(choose) for choose in self.rosters.values()}
        by_closers = sum(saves[pid] for pid in closers.values())
        self.assertGreater(by_closers / sum(saves.values()), 0.6)

    @staticmethod
    def _closer_of(roster):
        from myapp.domain.simulation.manager import plan_pitching_staff

        return plan_pitching_staff(roster.pitchers).closer.player_id

    def test_the_decisions_follow_the_same_rules_as_a_hand_scored_game(self):
        """勝敗は決着した試合に1つずつ。引分には付かない。"""
        for played in self.games:
            game = played.game
            wins = sum(o.line.wins for o in game.pitching)
            losses = sum(o.line.losses for o in game.pitching)
            if game.is_tie:
                self.assertEqual((wins, losses), (0, 0))
            else:
                self.assertEqual((wins, losses), (1, 1))

    def test_each_side_has_a_starter_and_orders_pitchers_from_one(self):
        for played in self.games:
            game = played.game
            for team_id in (game.home_team_id, game.away_team_id):
                roster = self.rosters[team_id]
                ids = {p.player_id for p in roster.pitchers}
                orders = sorted(o.appearance_order for o in game.pitching if o.player_id in ids)
                self.assertEqual(orders, list(range(1, len(orders) + 1)))


class ForeignQuotaInGameTest(TestCase):
    def test_foreign_players_never_exceed_the_game_limit(self):
        rng = make_random(77)
        pool = list(spread_league(rng, 2))
        clubs = []
        for club in pool:
            batters = tuple(replace(b, is_foreign=index < 5) for index, b in enumerate(club.batters))
            pitchers = tuple(replace(p, is_foreign=index < 5) for index, p in enumerate(club.pitchers))
            clubs.append(choose_active_roster(replace(club, batters=batters, pitchers=pitchers), 8))

        for limit in (2, 3):
            history = PitchingHistory()
            for day in range(24):
                home, away = (clubs[0], clubs[1]) if day % 2 else (clubs[1], clubs[0])
                played = _play(
                    day, home, away, day=OPENING_DAY + timedelta(days=day), history=history, foreign_game_limit=limit
                )
                foreign_ids = {b.player_id for c in clubs for b in c.batters if b.is_foreign} | {
                    p.player_id for c in clubs for p in c.pitchers if p.is_foreign
                }
                for club in (home, away):
                    team_ids = {b.player_id for b in club.batters} | {p.player_id for p in club.pitchers}
                    appeared = {entry.player_id for entry in played.lineup if entry.team_id == club.team_id}
                    appeared |= {o.player_id for o in played.game.pitching if o.player_id in team_ids}
                    self.assertLessEqual(len(appeared & foreign_ids), limit, f"limit={limit} day={day}")


class ThirdOutTest(TestCase):
    def test_no_run_scores_on_the_play_that_makes_the_third_out(self):
        """3つ目のアウトを打者がアウトになって取った打席では、得点は入らない（規則 5.08）。"""
        rng = make_random(3)
        rosters = [choose_active_roster(club, 4) for club in spread_league(rng, 2)]
        history = PitchingHistory()
        checked = 0
        for day in range(150):
            home, away = (rosters[0], rosters[1]) if day % 2 else (rosters[1], rosters[0])
            played = _play(day, home, away, day=OPENING_DAY + timedelta(days=day), history=history)
            outs = 0
            current = None
            for entry in played.plate_appearances:
                if entry.half_inning != current:
                    current, outs = entry.half_inning, 0
                batter_out = entry.batter_advance.is_out
                outs += entry.outs_recorded
                if outs == 3 and batter_out:
                    checked += 1
                    self.assertEqual(entry.runs_scored, 0, f"{entry} {entry.result.label}")
        self.assertGreater(checked, 500)
