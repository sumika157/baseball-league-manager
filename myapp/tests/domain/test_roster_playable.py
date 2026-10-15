"""試合を組める名簿の最小条件（世界の作成の検査と、手動の1軍登録の検査が同じ規則を通る）。"""

import unittest

from myapp.domain.exceptions import InvalidWorld
from myapp.domain.pennant.club_plan import ClubMember, ensure_roster_playable, playable_shortfall
from myapp.domain.simulation.manager import MIN_ACTIVE_PITCHERS
from myapp.domain.value_objects import ForeignPlayerRules, Position

C, IF, OF, P = Position.CATCHER, Position.INFIELDER, Position.OUTFIELDER, Position.PITCHER

# 捕手1・内野手5・外野手3の野手9人と、下限ちょうどの投手
PLAYABLE = [C, IF, IF, IF, IF, IF, OF, OF, OF] + [P] * MIN_ACTIVE_PITCHERS
# 計2人だけの外国人枠（投手・野手の内訳は無制限）
TOTAL_TWO = ForeignPlayerRules(active=2, pitchers=None, batters=None)


class PlayableShortfallTest(unittest.TestCase):
    def test_a_roster_with_every_slot_filled_can_play(self):
        self.assertIsNone(playable_shortfall(PLAYABLE))

    def test_too_few_batters_cannot_play(self):
        message = playable_shortfall([C, IF, IF, IF, IF, OF, OF, OF] + [P] * MIN_ACTIVE_PITCHERS)

        assert message is not None
        self.assertIn("野手8人", message)

    def test_too_few_pitchers_cannot_play(self):
        message = playable_shortfall([C, IF, IF, IF, IF, IF, OF, OF, OF] + [P] * (MIN_ACTIVE_PITCHERS - 1))

        assert message is not None
        self.assertIn(f"投手{MIN_ACTIVE_PITCHERS - 1}人", message)

    def test_a_roster_without_a_catcher_cannot_play(self):
        message = playable_shortfall([IF, IF, IF, IF, IF, IF, OF, OF, OF] + [P] * MIN_ACTIVE_PITCHERS)

        assert message is not None
        self.assertIn("捕手", message)

    def test_a_roster_that_cannot_cover_the_outfield_cannot_play(self):
        """人数がそろっていても、守れない位置があれば試合は組めない（内野手は外野を守れない）。"""
        message = playable_shortfall([C, IF, IF, IF, IF, IF, IF, IF, IF] + [P] * MIN_ACTIVE_PITCHERS)

        assert message is not None
        self.assertIn("守れる野手がいない", message)


def members_of(positions, *, foreign=()):
    """登録位置の並びから球団の選手を作る（`foreign` は外国人にする位置の番号）。"""
    return [ClubMember(index, f"選手{index}", position, index in foreign) for index, position in enumerate(positions)]


class SubjectTest(unittest.TestCase):
    def test_the_message_names_what_is_counted(self):
        default = playable_shortfall([C])
        roster = playable_shortfall([C], subject="在籍選手")

        assert default is not None and roster is not None
        self.assertTrue(default.startswith("1軍には"))
        self.assertTrue(roster.startswith("在籍選手には"))


class EnsureRosterPlayableTest(unittest.TestCase):
    def test_a_playable_roster_passes(self):
        ensure_roster_playable("東京", members_of(PLAYABLE), foreign_rules=ForeignPlayerRules.UNLIMITED)

    def test_the_error_names_the_team_and_the_shortfall_in_japanese(self):
        with self.assertRaises(InvalidWorld) as raised:
            ensure_roster_playable("東京", members_of([C]), foreign_rules=ForeignPlayerRules.UNLIMITED)

        self.assertIn("東京", str(raised.exception))
        self.assertIn("試合を組める名簿ではありません", str(raised.exception))
        self.assertIn("在籍選手には野手", str(raised.exception))
        self.assertNotIn("1軍", str(raised.exception), "世界の作成では数えているのは在籍選手")

    def test_exactly_nine_batters_with_too_many_foreigners_cannot_play(self):
        """野手が9人ちょうどで、外国人枠を超える外国人がいると、自動のスタメンを組めず進行が止まる。"""
        foreign_batters = {1, 2, 3}  # 野手の3人（捕手以外）が外国人

        with self.assertRaises(InvalidWorld) as raised:
            ensure_roster_playable("東京", members_of(PLAYABLE, foreign=foreign_batters), foreign_rules=TOTAL_TWO)

        self.assertIn("外国人選手の枠（計2人）の中では", str(raised.exception))
        self.assertIn("組めるのは8人", str(raised.exception))

    def test_enough_domestic_batters_pass_the_game_limit(self):
        roster = members_of([C, IF, IF, IF, IF, IF, OF, OF, OF, OF] + [P] * MIN_ACTIVE_PITCHERS, foreign={1, 2, 3})

        ensure_roster_playable("東京", roster, foreign_rules=TOTAL_TWO)

    def test_a_foreign_pitcher_takes_one_slot_of_the_game_limit(self):
        pitcher_index = len(PLAYABLE) - 1
        roster = members_of(PLAYABLE, foreign={1, 2, pitcher_index})

        with self.assertRaises(InvalidWorld):
            ensure_roster_playable("東京", roster, foreign_rules=TOTAL_TWO)

    def test_no_game_limit_means_no_foreign_condition(self):
        ensure_roster_playable(
            "東京", members_of(PLAYABLE, foreign=set(range(9))), foreign_rules=ForeignPlayerRules.UNLIMITED
        )

    def test_the_agreement_lets_three_foreign_batters_start_beside_a_foreign_starter(self):
        """協約第82条の2（計4・野手3）: 外国人の投手が先発しても、外国人の野手3人がスタメンに入れる。"""
        roster = members_of(PLAYABLE, foreign={1, 2, 3, len(PLAYABLE) - 1})

        ensure_roster_playable("東京", roster, foreign_rules=ForeignPlayerRules.NPB)

    def test_the_agreement_refuses_nine_batters_with_four_foreigners(self):
        """野手は3人までなので、野手9人ちょうどのうち4人が外国人だとスタメンを組めない。"""
        with self.assertRaises(InvalidWorld) as raised:
            ensure_roster_playable(
                "東京", members_of(PLAYABLE, foreign={1, 2, 3, 4}), foreign_rules=ForeignPlayerRules.NPB
            )

        self.assertIn("計4人・投手3人・野手3人", str(raised.exception))
