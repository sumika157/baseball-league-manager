"""ポストシーズンの勝ち上がり（`domain/pennant/postseason.py`）の単体テスト。Django も DB も使わない。

守りたいこと:
- NPB の規定（FS・Final の1勝と2勝のアドバンテージ・打ち切り・日本シリーズの 2-3-2 と第8戦以降）の全場合
- 1〜8リーグへの写し方（CS の段・トーナメントのシード・回の名前・リーグの優先順の本拠地）
- 日付は最後の公式戦の日から導き、年をまたがない
- 結果から毎回同じ勝ち上がりを導き、食い違う結果を拒む
"""

import random
import unittest
from collections import defaultdict
from datetime import date, timedelta

from myapp.domain.exceptions import InvalidPostseason
from myapp.domain.pennant.postseason import (
    DAYS_BEFORE_POSTSEASON,
    DAYS_BEFORE_SERIES_ROUND,
    DAYS_BETWEEN_CS_STAGES,
    FINAL_STAGE,
    FINAL_STAGE_TWO_WIN_ADVANTAGE,
    FIRST_STAGE,
    JAPAN_SERIES,
    POSTSEASON_MAX_INNINGS,
    FinalStanding,
    LeagueFinish,
    OutcomeKind,
    PostseasonBracket,
    PostseasonFormat,
    PostseasonOutcome,
    PostseasonResult,
    PostseasonStatus,
    Series,
    SeriesStage,
    champion_title,
    final_stage_rules,
)
from myapp.domain.value_objects import TeamRecord

LAST_REGULAR = date(2026, 9, 15)
START = LAST_REGULAR + timedelta(days=DAYS_BEFORE_POSTSEASON)
NPB = PostseasonFormat.NPB
CHAMPIONS = PostseasonFormat.CHAMPIONS


def _days(n: int) -> timedelta:
    return timedelta(days=n)


def _league(league_index: int, teams: int = 6) -> LeagueFinish:
    """リーグ `league_index`（id は 100 + 番号）。球団 id は 番号 × 10 + 順位。順位が下がるほど3勝ずつ少ない。

    1位は番号が小さいほど勝率が高い（種の順が番号の順になる）。3位と1位の差は6ゲーム（1勝のアドバンテージ）。
    """
    rows = tuple(
        FinalStanding(
            team_id=league_index * 10 + rank,
            record=TeamRecord(wins=90 - league_index - 3 * (rank - 1), losses=50 + league_index + 3 * (rank - 1)),
        )
        for rank in range(1, teams + 1)
    )
    return LeagueFinish(league_id=100 + league_index, standings=rows)


def _bracket(*leagues: LeagueFinish, fmt: PostseasonFormat = NPB, priority=None, last=LAST_REGULAR):
    ids = [league.league_id for league in leagues] if priority is None else priority
    return PostseasonBracket.derive(fmt, leagues, ids, last)


def _play(bracket: PostseasonBracket, scripts=None, default: str = "U") -> PostseasonBracket:
    """`next_day()` に従って終わりまで進める。

    `scripts` は {frozenset(2球団): "UTL…"}（U 上位の勝ち・L 下位の勝ち・T 引分）。尽きたら `default`。
    """
    scripts = scripts or {}
    played: defaultdict[frozenset[int], int] = defaultdict(int)
    while (day := bracket.next_day()) is not None:
        results = []
        for game in day.games:
            series = game.series
            key = frozenset((series.upper_team_id, series.lower_team_id))
            script = scripts.get(key, "")
            index = played[key]
            played[key] += 1
            outcome = script[index] if index < len(script) else default
            winner = {"U": series.upper_team_id, "L": series.lower_team_id, "T": None}[outcome]
            results.append(PostseasonResult(game.played_on, game.home_team_id, game.away_team_id, winner))
        bracket = bracket.played(results)
    return bracket


def _series(bracket: PostseasonBracket, stage: SeriesStage, league_id: int | None = None) -> list[Series]:
    return [s for s in bracket.series() if s.stage is stage and (league_id is None or s.league_id == league_id)]


def _first_stage(script: str) -> Series:
    """1リーグの FS を `script` で進めたあとのシリーズ。"""
    bracket = _play(_bracket(_league(1)), {frozenset((12, 13)): script})
    return _series(bracket, SeriesStage.FIRST)[0]


class FirstStageTest(unittest.TestCase):
    """CS ファーストステージ: 3試合制・先に2勝・全試合2位のホーム・同数なら2位。"""

    def check(self, script: str, winner: int, games: int) -> None:
        series = _first_stage(script)
        self.assertEqual(winner, series.winner)
        self.assertEqual(games, len(series.games))

    def test_the_second_place_wins_twice_in_a_row(self):
        self.check("UU", 12, 2)

    def test_one_win_and_a_tie_ends_it_for_the_second_place(self):
        # 2位の勝利数が3位の勝利数と同数以上になることが確定（打ち切り）
        self.check("UT", 12, 2)
        self.check("TU", 12, 2)

    def test_the_third_place_wins_twice_in_a_row(self):
        self.check("LL", 13, 2)

    def test_one_win_each_goes_to_the_third_game(self):
        self.check("ULU", 12, 3)
        self.check("ULL", 13, 3)
        self.check("LUL", 13, 3)

    def test_two_ties_then_the_third_game(self):
        self.check("TTU", 12, 3)
        self.check("TTL", 13, 3)
        # 3試合で勝利数が同じ（0勝0敗3分）なら2位
        self.check("TTT", 12, 3)

    def test_a_loss_and_a_tie_then_the_third_game(self):
        self.check("LTU", 12, 3)  # 1勝1敗1分 → 同数で2位
        self.check("LTT", 13, 3)  # 0勝1敗2分 → 3位
        self.check("TLL", 13, 3)

    def test_the_second_place_is_at_home_every_day(self):
        series = _first_stage("ULT")
        self.assertEqual([12, 12, 12], [g.home_team_id for g in series.games])
        self.assertEqual([START, START + _days(1), START + _days(2)], [g.played_on for g in series.games])
        self.assertEqual(FIRST_STAGE, series.rules)


class FinalStageTest(unittest.TestCase):
    """CS ファイナルステージ（6試合制）: 1位に1勝のアドバンテージ・先に4勝・全試合1位のホーム・同数なら1位。"""

    def final(self, script: str, first_stage: str = "UU") -> Series:
        bracket = _play(_bracket(_league(1)), {frozenset((12, 13)): first_stage, frozenset((11, 12)): script})
        return _series(bracket, SeriesStage.FINAL)[0]

    def check(self, script: str, winner: int, games: int) -> None:
        series = self.final(script)
        self.assertEqual(FINAL_STAGE, series.rules)
        self.assertEqual(winner, series.winner)
        self.assertEqual(games, len(series.games))

    def test_the_first_place_ends_it_with_three_wins(self):
        self.check("UUU", 11, 3)

    def test_two_wins_and_a_tie_end_it_for_the_first_place(self):
        # アドバンテージ込みで3勝0敗・残り3試合 → 打ち切り
        self.check("UUT", 11, 3)
        self.check("TUU", 11, 3)

    def test_the_challenger_needs_four_wins(self):
        self.check("LLLL", 12, 4)
        self.check("ULLLL", 12, 5)

    def test_the_challenger_wins_when_the_first_place_can_no_longer_catch_up(self):
        # 0勝3敗2分（アドバンテージ込みで1勝3敗）・残り1試合 → 第6戦を全部勝っても2勝3敗
        self.check("LLTLT", 12, 5)

    def test_even_after_six_games_goes_to_the_first_place(self):
        # アドバンテージ込みで3勝3敗（2勝3敗1分）
        self.check("LULULT", 11, 6)
        self.check("LULULU", 11, 6)
        self.check("LULULL", 12, 6)

    def test_the_first_place_is_at_home_and_it_starts_after_a_day_off(self):
        # FS が2試合で終わっても、ファイナルは FS の予定の最終日（3日目）の2日後に始まる
        series = self.final("LUT")
        first_stage_last = START + _days(2)
        self.assertEqual(first_stage_last + _days(DAYS_BETWEEN_CS_STAGES), series.starts_on)
        self.assertGreater(len(series.games), 3)
        self.assertEqual({11}, {g.home_team_id for g in series.games})
        self.assertEqual(
            [series.starts_on + _days(i) for i in range(len(series.games))], [g.played_on for g in series.games]
        )

    def test_the_challenger_is_the_first_stage_winner(self):
        self.assertEqual(13, self.final("UUU", first_stage="LL").lower_team_id)


class TwoWinAdvantageTest(unittest.TestCase):
    """2026年の規定: 相手が1位から10ゲーム差以上か、勝率5割未満なら、7試合制・2勝のアドバンテージ・先に5勝。"""

    LEADER = TeamRecord(wins=85, losses=55)

    def test_exactly_ten_games_behind_gives_two_wins(self):
        challenger = TeamRecord(wins=75, losses=65)
        self.assertEqual(10.0, challenger.games_behind(self.LEADER))
        self.assertEqual(FINAL_STAGE_TWO_WIN_ADVANTAGE, final_stage_rules(self.LEADER, challenger))

    def test_nine_and_a_half_games_behind_gives_one_win(self):
        challenger = TeamRecord(wins=75, losses=64)
        self.assertEqual(9.5, challenger.games_behind(self.LEADER))
        self.assertEqual(FINAL_STAGE, final_stage_rules(self.LEADER, challenger))

    def test_exactly_five_hundred_gives_one_win(self):
        leader = TeamRecord(wins=74, losses=66)
        self.assertEqual(FINAL_STAGE, final_stage_rules(leader, TeamRecord(wins=70, losses=70)))

    def test_below_five_hundred_gives_two_wins(self):
        leader = TeamRecord(wins=74, losses=66)
        self.assertEqual(FINAL_STAGE_TWO_WIN_ADVANTAGE, final_stage_rules(leader, TeamRecord(wins=69, losses=70)))

    def bracket(self) -> PostseasonBracket:
        # 3位が1位から10ゲーム以上離れたリーグ
        league = LeagueFinish(
            league_id=101,
            standings=(
                FinalStanding(11, TeamRecord(wins=90, losses=50)),
                FinalStanding(12, TeamRecord(wins=80, losses=60)),
                FinalStanding(13, TeamRecord(wins=78, losses=62)),
            ),
        )
        return _bracket(league)

    def final(self, script: str, first_stage: str) -> Series:
        scripts = {frozenset((12, 13)): first_stage, frozenset((11, 12)): script, frozenset((11, 13)): script}
        bracket = _play(self.bracket(), scripts)
        return _series(bracket, SeriesStage.FINAL)[0]

    def test_the_rule_depends_on_the_first_stage_winner(self):
        # 2位（10ゲーム差）が勝てば2勝、3位（12ゲーム差）が勝っても2勝。ここでは 1位と2位の差がちょうど10
        self.assertEqual(FINAL_STAGE_TWO_WIN_ADVANTAGE, self.final("UUU", "UU").rules)
        self.assertEqual(FINAL_STAGE_TWO_WIN_ADVANTAGE, self.final("UUU", "LL").rules)

    def test_seven_games_and_five_wins(self):
        self.assertEqual(3, len(self.final("UUU", "UU").games))  # 2 + 3 = 5勝
        challenger = self.final("LLLLL", "UU")
        self.assertEqual((12, 5), (challenger.winner, len(challenger.games)))
        # 4連勝しても2勝4敗で、まだ決まらない
        self.assertEqual(6, len(self.final("LLLLUL", "UU").games))
        # 引分が続くと、アドバンテージの2勝が残り試合数以上になった第5戦で1位の勝ち（打ち切り）
        all_ties = self.final("TTTTTTT", "UU")
        self.assertEqual((11, 5), (all_ties.winner, len(all_ties.games)))

    def test_a_league_without_a_large_gap_gets_one_win(self):
        bracket = _play(_bracket(_league(1)))
        self.assertEqual(FINAL_STAGE, _series(bracket, SeriesStage.FINAL)[0].rules)


class JapanSeriesTest(unittest.TestCase):
    """日本シリーズ: 先に4勝・2-3-2・第7戦までは12回で引分・第8戦以降は延長の制限なし。"""

    def series(self, script: str, priority=(101, 102)) -> Series:
        bracket = _bracket(_league(1), _league(2), fmt=CHAMPIONS, priority=list(priority))
        bracket = _play(bracket, {frozenset((11, 21)): script})
        return _series(bracket, SeriesStage.JAPAN_SERIES)[0]

    def test_four_wins_in_a_row(self):
        series = self.series("UUUU")
        self.assertEqual((11, 4), (series.winner, len(series.games)))
        self.assertEqual(JAPAN_SERIES, series.rules)
        self.assertEqual("日本シリーズ", series.name)

    def test_two_three_two_homes_and_travel_days(self):
        series = self.series("ULULULU")
        start = series.starts_on
        self.assertEqual(START, start)  # 優勝球団で日本シリーズ: 最後の公式戦の3日後
        self.assertEqual([11, 11, 21, 21, 21, 11, 11], [g.home_team_id for g in series.games])
        offsets = [(g.played_on - start).days for g in series.games]
        self.assertEqual([0, 1, 3, 4, 5, 7, 8], offsets)

    def test_the_first_home_follows_the_league_priority(self):
        series = self.series("UUUU", priority=(102, 101))
        self.assertEqual(21, series.upper_team_id)
        self.assertEqual(21, series.games[0].home_team_id)

    def test_ties_bring_an_eighth_game_the_next_day_at_the_same_park(self):
        series = self.series("TULULUL" + "U")
        self.assertEqual(8, len(series.games))
        self.assertEqual(series.games[6].played_on + _days(1), series.games[7].played_on)
        self.assertEqual(series.games[6].home_team_id, series.games[7].home_team_id)
        self.assertEqual(11, series.winner)

    def test_a_ninth_game_and_later_alternate_with_a_travel_day(self):
        series = self.series("TTTTTTT" + "ULULULU")
        self.assertEqual(14, len(series.games))
        homes = [g.home_team_id for g in series.games[7:]]
        self.assertEqual([11, 21, 11, 21, 11, 21, 11], homes)
        offsets = [(g.played_on - series.starts_on).days for g in series.games[7:]]
        self.assertEqual([9, 11, 13, 15, 17, 19, 21], offsets)

    def test_the_inning_limit_is_lifted_from_the_eighth_game(self):
        bracket = _bracket(_league(1), _league(2), fmt=CHAMPIONS)
        limits = []
        while (day := bracket.next_day()) is not None:
            game = day.games[0]
            limits.append(game.max_innings)
            # 第7戦までは引分、制限の無い第8戦からは本拠地の球団が勝つ（引分は不正な結果になる）
            winner = None if game.max_innings is not None else game.home_team_id
            bracket = bracket.played([PostseasonResult(game.played_on, game.home_team_id, game.away_team_id, winner)])
            if len(limits) == 9:
                break
        self.assertEqual([POSTSEASON_MAX_INNINGS] * 7 + [None, None], limits)

    def test_a_tie_in_a_game_without_an_inning_limit_is_rejected(self):
        series = self.series("TTTTTTT")
        bracket = _bracket(_league(1), _league(2), fmt=CHAMPIONS).played(series.games[:7])
        day = bracket.next_day()
        assert day is not None
        game = day.games[0]
        self.assertIsNone(game.max_innings)
        with self.assertRaises(InvalidPostseason):
            bracket.played([PostseasonResult(game.played_on, game.home_team_id, game.away_team_id, None)])

    def test_climax_series_games_are_always_limited(self):
        bracket = _bracket(_league(1))
        for _ in range(3):
            day = bracket.next_day()
            assert day is not None
            self.assertEqual([POSTSEASON_MAX_INNINGS], [g.max_innings for g in day.games])
            game = day.games[0]
            bracket = bracket.played([PostseasonResult(game.played_on, game.home_team_id, game.away_team_id, None)])


class LeagueCountTest(unittest.TestCase):
    """1〜8リーグの勝ち上がり（全部上位が勝つ＝各リーグの1位が代表になる）。"""

    def rounds(self, count: int, fmt: PostseasonFormat = NPB) -> list[list[Series]]:
        bracket = _play(_bracket(*[_league(i) for i in range(1, count + 1)], fmt=fmt))
        self.assertEqual(PostseasonStatus.FINISHED, bracket.status)
        return [list(round_) for round_ in bracket.rounds]

    def series_rounds(self, count: int) -> list[list[tuple[int, int]]]:
        return [
            [(s.upper_team_id, s.lower_team_id) for s in round_]
            for round_ in self.rounds(count)
            if round_[0].stage is SeriesStage.JAPAN_SERIES
        ]

    def test_one_league_has_no_japan_series(self):
        bracket = _play(_bracket(_league(1)))
        self.assertEqual([SeriesStage.FIRST, SeriesStage.FINAL], [s.stage for s in bracket.series()])
        self.assertEqual(11, bracket.champion)

    def test_two_leagues_play_one_japan_series(self):
        rounds = self.rounds(2)
        self.assertEqual([2, 2, 1], [len(r) for r in rounds])
        self.assertEqual([[(11, 21)]], self.series_rounds(2))

    def test_three_leagues_give_the_top_seed_a_bye_to_the_final(self):
        self.assertEqual([[(21, 31)], [(11, 21)]], self.series_rounds(3))
        names = [s.name for r in self.rounds(3)[2:] for s in r]
        self.assertEqual(["日本シリーズ準決勝", "日本シリーズ"], names)

    def test_four_leagues(self):
        self.assertEqual([[(11, 41), (21, 31)], [(11, 21)]], self.series_rounds(4))

    def test_five_to_seven_leagues_give_byes_to_the_top_seeds(self):
        self.assertEqual([[(41, 51)], [(11, 41), (21, 31)], [(11, 21)]], self.series_rounds(5))
        self.assertEqual([[(41, 51), (31, 61)], [(11, 41), (21, 31)], [(11, 21)]], self.series_rounds(6))
        self.assertEqual([[(41, 51), (21, 71), (31, 61)], [(11, 41), (21, 31)], [(11, 21)]], self.series_rounds(7))

    def test_eight_leagues_play_three_rounds(self):
        rounds = self.series_rounds(8)
        self.assertEqual([4, 2, 1], [len(r) for r in rounds])
        self.assertEqual([(11, 81), (41, 51), (21, 71), (31, 61)], rounds[0])
        names = [s.name for r in self.rounds(8)[2:] for s in r]
        self.assertEqual(["日本シリーズ準々決勝"] * 4 + ["日本シリーズ準決勝"] * 2 + ["日本シリーズ"], names)

    def test_series_of_a_round_start_on_the_same_day(self):
        for round_ in self.rounds(8):
            self.assertEqual(1, len({s.starts_on for s in round_}))

    def test_a_later_round_waits_for_the_latest_series(self):
        # 準々決勝の1つが第8戦まで行くと、準決勝は予定の最終日（第7戦）ではなく第8戦の4日後に始まる
        leagues = [_league(i) for i in range(1, 9)]
        bracket = _play(_bracket(*leagues), {frozenset((41, 51)): "TULULUL" + "U"})
        quarter, semi = bracket.rounds[2], bracket.rounds[3]
        last = max(g.played_on for s in quarter for g in s.games)
        self.assertEqual(quarter[0].starts_on + _days(9), last)
        self.assertEqual(last + _days(DAYS_BEFORE_SERIES_ROUND), semi[0].starts_on)

    def test_the_first_series_round_starts_four_days_after_the_scheduled_end_of_the_final_stage(self):
        rounds = self.rounds(2)
        final_stage_end = rounds[1][0].scheduled_last_day
        self.assertEqual(final_stage_end + _days(DAYS_BEFORE_SERIES_ROUND), rounds[2][0].starts_on)

    def test_leagues_with_one_two_and_three_or_more_teams(self):
        bracket = _play(_bracket(_league(1, teams=1), _league(2, teams=2), _league(3, teams=4)))
        firsts = _series(bracket, SeriesStage.FIRST)
        finals = _series(bracket, SeriesStage.FINAL)
        self.assertEqual([103], [s.league_id for s in firsts])
        self.assertEqual(
            [(102, 21, 22), (103, 31, 32)], [(s.league_id, s.upper_team_id, s.lower_team_id) for s in finals]
        )
        # 2球団のリーグもファイナルは FS の日程の後（同じ回は同じ日に始まる）
        self.assertEqual(finals[0].starts_on, finals[1].starts_on)
        # 1球団のリーグの球団もトーナメントに出る
        japan = _series(bracket, SeriesStage.JAPAN_SERIES)
        self.assertIn(11, {t for s in japan for t in (s.upper_team_id, s.lower_team_id)})

    def test_only_two_team_leagues_start_with_the_final_stage(self):
        bracket = _bracket(_league(1, teams=2), _league(2, teams=2))
        self.assertEqual([SeriesStage.FINAL] * 2, [s.stage for s in bracket.series()])
        self.assertEqual(START, bracket.series()[0].starts_on)

    def test_seeds_tied_on_percentage_follow_the_league_priority(self):
        same = TeamRecord(wins=80, losses=60)
        leagues = [LeagueFinish(league_id=100 + i, standings=(FinalStanding(i * 10 + 1, same),)) for i in (1, 2, 3)]
        bracket = PostseasonBracket.derive(NPB, leagues, [102, 103, 101], LAST_REGULAR)
        # 種は 21・31・11 の順。1位の種（21）がシード、31 と 11 が準決勝（第1戦は優先順で先の 31 の本拠地）
        semi = bracket.series()[0]
        self.assertEqual((31, 11), (semi.upper_team_id, semi.lower_team_id))

    def test_a_higher_percentage_gets_the_better_seed_regardless_of_the_league(self):
        leagues = [_league(1), _league(2), _league(3)]
        bracket = _play(_bracket(*leagues, fmt=CHAMPIONS, priority=[103, 102, 101]))
        semi = bracket.series()[0]
        # 1位の種は勝率の最も高い 11。準決勝は 21 対 31（優先順で先の 31 が第1戦の本拠地）
        self.assertEqual((31, 21), (semi.upper_team_id, semi.lower_team_id))


class StatusTest(unittest.TestCase):
    def test_only_a_finished_or_absent_postseason_is_settled(self):
        """締めてよい（世界の終了を出してよい）のは、終わった・無いときだけ。"""
        settled = {status for status in PostseasonStatus if status.is_settled}
        self.assertEqual(settled, {PostseasonStatus.NONE, PostseasonStatus.FINISHED})


class FormatTest(unittest.TestCase):
    def test_none_has_no_postseason(self):
        bracket = _bracket(_league(1), _league(2), fmt=PostseasonFormat.NONE)
        self.assertEqual(PostseasonStatus.NONE, bracket.status)
        self.assertIsNone(bracket.next_day())
        self.assertIsNone(bracket.champion)
        self.assertEqual([], bracket.series())

    def test_champions_plays_only_the_league_champions(self):
        bracket = _play(_bracket(_league(1), _league(2), fmt=CHAMPIONS), {frozenset((11, 21)): "LLLL"})
        self.assertEqual([SeriesStage.JAPAN_SERIES], [s.stage for s in bracket.series()])
        self.assertEqual(21, bracket.champion)

    def test_champions_with_one_league_does_nothing(self):
        self.assertEqual(PostseasonStatus.NONE, _bracket(_league(1), fmt=CHAMPIONS).status)

    def test_a_single_team_world_has_no_postseason(self):
        self.assertEqual(PostseasonStatus.NONE, _bracket(_league(1, teams=1)).status)

    def test_every_format_can_be_derived(self):
        for fmt in PostseasonFormat:
            for count in range(1, 9):
                with self.subTest(fmt=fmt, count=count):
                    bracket = _play(_bracket(*[_league(i) for i in range(1, count + 1)], fmt=fmt))
                    self.assertIn(bracket.status, (PostseasonStatus.NONE, PostseasonStatus.FINISHED))

    def test_status_moves_from_not_started_to_finished(self):
        bracket = _bracket(_league(1), _league(2))
        self.assertEqual(PostseasonStatus.NOT_STARTED, bracket.status)
        day = bracket.next_day()
        assert day is not None
        game = day.games[0]
        bracket = bracket.played([PostseasonResult(game.played_on, game.home_team_id, game.away_team_id, None)])
        self.assertEqual(PostseasonStatus.IN_PROGRESS, bracket.status)
        self.assertIsNone(bracket.champion)
        bracket = _play(bracket)
        self.assertEqual(PostseasonStatus.FINISHED, bracket.status)
        self.assertIsNone(bracket.next_day())


class DerivationTest(unittest.TestCase):
    """結果から毎回導く。食い違う結果は拒む。"""

    def finished(self) -> PostseasonBracket:
        return _play(_bracket(_league(1), _league(2)), {frozenset((11, 21)): "ULTLUTU"})

    def test_the_same_results_in_any_order_give_the_same_bracket(self):
        bracket = self.finished()
        shuffled = list(bracket.results)
        random.Random(7).shuffle(shuffled)
        again = PostseasonBracket.derive(NPB, [_league(1), _league(2)], [101, 102], LAST_REGULAR, shuffled)
        self.assertEqual(bracket.rounds, again.rounds)
        self.assertEqual(bracket.champion, again.champion)

    def test_the_next_day_is_derived_from_the_results_so_far(self):
        bracket = self.finished()
        results = sorted(bracket.results, key=lambda r: r.played_on)
        partial = PostseasonBracket.derive(NPB, [_league(1), _league(2)], [101, 102], LAST_REGULAR, results[:5])
        day = partial.next_day()
        assert day is not None
        self.assertEqual(results[5].played_on, day.played_on)
        self.assertIn(
            (results[5].home_team_id, results[5].away_team_id), [(g.home_team_id, g.away_team_id) for g in day.games]
        )

    def assert_rejected(self, bracket: PostseasonBracket, *results: PostseasonResult) -> None:
        with self.assertRaises(InvalidPostseason):
            bracket.played(results)

    def test_a_game_on_a_different_day_is_rejected(self):
        bracket = _bracket(_league(1))
        self.assert_rejected(bracket, PostseasonResult(START + _days(1), 12, 13, 12))

    def test_a_game_of_a_decided_series_is_rejected(self):
        bracket = _bracket(_league(1)).played(
            [PostseasonResult(START, 12, 13, 12), PostseasonResult(START + _days(1), 12, 13, 12)]
        )
        self.assert_rejected(bracket, PostseasonResult(START + _days(2), 12, 13, 12))

    def test_two_games_of_a_series_on_the_same_day_are_rejected(self):
        bracket = _bracket(_league(1))
        self.assert_rejected(bracket, PostseasonResult(START, 12, 13, 12), PostseasonResult(START, 12, 13, 13))

    def test_a_team_not_in_the_series_is_rejected(self):
        bracket = _bracket(_league(1))
        self.assert_rejected(bracket, PostseasonResult(START, 11, 14, 11))

    def test_the_wrong_home_is_rejected(self):
        bracket = _bracket(_league(1))
        self.assert_rejected(bracket, PostseasonResult(START, 13, 12, 12))

    def test_a_game_after_the_champion_is_decided_is_rejected(self):
        bracket = self.finished()
        last = max(r.played_on for r in bracket.results)
        self.assert_rejected(bracket, PostseasonResult(last + _days(1), 11, 21, 11))

    def test_a_game_without_a_postseason_is_rejected(self):
        bracket = _bracket(_league(1), fmt=PostseasonFormat.NONE)
        self.assert_rejected(bracket, PostseasonResult(START, 12, 13, 12))

    def test_a_skipped_series_on_a_day_is_rejected_once_a_later_day_comes(self):
        # 2リーグの FS の初日に片方だけ結果があり、翌日の結果が来たら食い違い
        bracket = _bracket(_league(1), _league(2))
        self.assert_rejected(
            bracket, PostseasonResult(START, 12, 13, 12), PostseasonResult(START + _days(1), 12, 13, 12)
        )

    def test_a_partial_day_leaves_the_rest_of_that_day_next(self):
        bracket = _bracket(_league(1), _league(2)).played([PostseasonResult(START, 12, 13, 12)])
        day = bracket.next_day()
        assert day is not None
        self.assertEqual(START, day.played_on)
        self.assertEqual([(22, 23)], [(g.home_team_id, g.away_team_id) for g in day.games])

    def test_the_winner_must_be_one_of_the_teams(self):
        with self.assertRaises(InvalidPostseason):
            PostseasonResult(START, 12, 13, 11)
        with self.assertRaises(InvalidPostseason):
            PostseasonResult(START, 12, 12, None)

    def test_inputs_are_checked(self):
        with self.assertRaises(InvalidPostseason):
            PostseasonBracket.derive(NPB, [_league(1), _league(2)], [101], LAST_REGULAR)
        with self.assertRaises(InvalidPostseason):
            PostseasonBracket.derive(NPB, [_league(1), _league(1)], [101], LAST_REGULAR)
        # 優先順に同じリーグが2回
        with self.assertRaises(InvalidPostseason):
            PostseasonBracket.derive(NPB, [_league(1), _league(2)], [101, 102, 101], LAST_REGULAR)
        # 同じリーグ id の最終順位が2つ（球団は別）
        other = LeagueFinish(league_id=101, standings=(FinalStanding(99, TeamRecord(wins=1, losses=1)),))
        with self.assertRaises(InvalidPostseason):
            PostseasonBracket.derive(NPB, [_league(1), other], [101], LAST_REGULAR)
        # 別のリーグ id で同じ球団を持つ
        shared = LeagueFinish(league_id=102, standings=(FinalStanding(11, TeamRecord(wins=1, losses=1)),))
        with self.assertRaises(InvalidPostseason):
            PostseasonBracket.derive(NPB, [_league(1), shared], [101, 102], LAST_REGULAR)


class CalendarTest(unittest.TestCase):
    """日付は最後の公式戦の日から導き、年をまたがない。"""

    def test_the_first_stage_starts_three_days_after_the_last_regular_game(self):
        day = _bracket(_league(1)).next_day()
        assert day is not None
        self.assertEqual(LAST_REGULAR + _days(3), day.played_on)

    def test_eight_leagues_with_the_longest_series_stay_in_the_year(self):
        # 最後の公式戦が9月末でも、CS を全部最後まで・日本シリーズの全回を最長（7分のあと第14戦）にして年内に終わる
        leagues = [_league(i) for i in range(1, 9)]
        longest = "TTTTTTT" + "ULULULU"
        scripts = defaultdict(lambda: longest)
        bracket = PostseasonBracket.derive(NPB, leagues, [101 + i for i in range(8)], date(2026, 9, 30))
        played: defaultdict[frozenset[int], int] = defaultdict(int)
        while (day := bracket.next_day()) is not None:
            results = []
            for game in day.games:
                series = game.series
                key = frozenset((series.upper_team_id, series.lower_team_id))
                script = "ULT" if series.stage is SeriesStage.FIRST else scripts[key]
                if series.stage is SeriesStage.FINAL:
                    script = "LULULT" if series.rules is FINAL_STAGE else "LULULTT"
                outcome = script[played[key]]
                played[key] += 1
                winner = {"U": series.upper_team_id, "L": series.lower_team_id, "T": None}[outcome]
                results.append(PostseasonResult(game.played_on, game.home_team_id, game.away_team_id, winner))
            bracket = bracket.played(results)
        self.assertEqual(PostseasonStatus.FINISHED, bracket.status)
        last = max(r.played_on for r in bracket.results)
        self.assertEqual(2026, last.year)
        self.assertTrue(all(len(s.games) == 14 for s in _series(bracket, SeriesStage.JAPAN_SERIES)))
        self.assertEqual(date(2026, 12, 26), last)

    def test_a_date_past_the_year_end_is_rejected(self):
        bracket = _bracket(_league(1), _league(2), last=date(2026, 12, 20))
        with self.assertRaises(InvalidPostseason):
            _play(bracket)


class OutcomeTest(unittest.TestCase):
    """球団ごとの結果（`outcome_of`）・優勝の呼び名・まだ組めていない回の名前・方式の説明（#241）。"""

    def test_before_it_starts_only_the_teams_out_of_the_postseason_are_decided(self):
        bracket = _bracket(_league(1), _league(2))
        self.assertEqual(bracket.outcome_of(14).kind, OutcomeKind.NOT_QUALIFIED)
        self.assertEqual(bracket.outcome_of(14).label, "出場なし")
        for team_id in (11, 12, 13, 21):
            with self.subTest(team_id=team_id):
                self.assertIsNone(bracket.outcome_of(team_id))

    def test_every_team_gets_its_outcome_at_the_end(self):
        # 上位が全部勝つ: 各リーグの1位が日本シリーズへ、優先順で先の 11 が日本一
        bracket = _play(_bracket(_league(1), _league(2)))
        self.assertEqual(bracket.outcome_of(11), PostseasonOutcome(OutcomeKind.CHAMPION, "日本一"))
        self.assertEqual(bracket.outcome_of(21).label, "日本シリーズ敗退")
        self.assertEqual(bracket.outcome_of(12).label, "CS ファイナルステージ敗退")
        self.assertEqual(bracket.outcome_of(13).label, "CS ファーストステージ敗退")
        self.assertEqual(bracket.outcome_of(16).label, "出場なし")
        self.assertEqual(bracket.outcome_of(999).label, "出場なし")

    def test_with_champions_only_the_first_places_take_part(self):
        bracket = _bracket(_league(1), _league(2), fmt=CHAMPIONS)
        self.assertIsNone(bracket.outcome_of(11))
        self.assertEqual(bracket.outcome_of(12).kind, OutcomeKind.NOT_QUALIFIED)

    def test_no_postseason_has_no_outcome_and_no_label(self):
        bracket = _bracket(_league(1), fmt=CHAMPIONS)
        self.assertIs(bracket.status, PostseasonStatus.NONE)
        self.assertIsNone(bracket.outcome_of(11))
        self.assertEqual(bracket.champion_label, "")
        self.assertEqual(bracket.pending_stage_names(), [])

    def test_the_champion_label_depends_on_the_last_round(self):
        self.assertEqual(_bracket(_league(1), _league(2)).champion_label, "日本一")
        self.assertEqual(_bracket(_league(1), _league(2), fmt=CHAMPIONS).champion_label, "日本一")
        one_league = _play(_bracket(_league(1)))
        self.assertEqual(one_league.champion_label, "CS 優勝")
        self.assertEqual(one_league.outcome_of(11).label, "CS 優勝")

    def test_the_label_is_the_title_of_the_league_count(self):
        """勝ち上がりの呼び名は `champion_title`（保存した日本一の呼び名と同じ出典）。1〜8リーグで一致する。"""
        for count in range(1, 9):
            with self.subTest(leagues=count):
                bracket = _bracket(*(_league(n) for n in range(1, count + 1)))
                self.assertEqual(bracket.champion_label, champion_title(count))
        self.assertEqual((champion_title(1), champion_title(2), champion_title(8)), ("CS 優勝", "日本一", "日本一"))

    def test_the_pending_rounds_are_named_until_they_are_built(self):
        leagues = [_league(n) for n in range(1, 5)]
        bracket = _bracket(*leagues)
        self.assertEqual(
            bracket.pending_stage_names(),
            ["CS ファイナルステージ", "日本シリーズ準決勝", "日本シリーズ"],
        )
        finished = _play(bracket)
        self.assertEqual(finished.pending_stage_names(), [])
        # 回の名前は組んだシリーズの名前と同じ
        self.assertEqual(
            [finished.plan.stage_name(i) for i in range(len(finished.rounds))],
            [round_[0].name for round_ in finished.rounds],
        )

    def test_every_format_has_a_description(self):
        for postseason_format in PostseasonFormat:
            with self.subTest(postseason_format=postseason_format):
                self.assertTrue(postseason_format.description)
