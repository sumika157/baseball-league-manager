"""選手プロフィールと球場の単体テスト。Django も DB も使わない。"""

from datetime import date
from unittest import TestCase

from myapp.domain.entities import Stadium, Team
from myapp.domain.exceptions import InvalidDraftPick, InvalidProfile, InvalidSeason, PlayerNotFound
from myapp.domain.value_objects import (
    AcquisitionRoute,
    ContractStatus,
    DraftPick,
    Handedness,
    JerseyNumber,
    Position,
    Profile,
    RosterLimits,
    StadiumProfile,
)


class HandednessTest(TestCase):
    def test_labels(self):
        self.assertEqual(Handedness.labels(), ["右", "左", "両"])

    def test_blank_means_unset(self):
        """全選手に入力を強いないため、未設定を許す。"""
        self.assertIsNone(Handedness.from_label(""))
        self.assertIsNone(Handedness.from_label(None))

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(InvalidProfile):
            Handedness.from_label("右利き")


class ProfileTest(TestCase):
    def test_every_field_is_optional(self):
        profile = Profile()

        self.assertTrue(profile.is_empty)
        self.assertIsNone(profile.age(date(2026, 8, 10)))
        self.assertEqual(profile.throws_bats, "")

    def test_age_before_birthday_this_year(self):
        profile = Profile(birth_date=date(1998, 12, 31))
        self.assertEqual(profile.age(date(2026, 8, 10)), 27)

    def test_age_on_the_birthday(self):
        profile = Profile(birth_date=date(1998, 8, 10))
        self.assertEqual(profile.age(date(2026, 8, 10)), 28)

    def test_age_after_birthday(self):
        profile = Profile(birth_date=date(1998, 3, 15))
        self.assertEqual(profile.age(date(2026, 8, 10)), 28)

    def test_age_is_not_stored(self):
        """年齢を保持すると翌年ずれるため、生年月日から都度求める。"""
        profile = Profile(birth_date=date(2000, 1, 1))

        self.assertEqual(profile.age(date(2026, 1, 1)), 26)
        self.assertEqual(profile.age(date(2027, 1, 1)), 27)

    def test_age_or_none_does_not_raise_before_birth(self):
        profile = Profile(birth_date=date(2000, 5, 5))

        self.assertIsNone(profile.age_or_none(date(1999, 1, 1)))
        self.assertEqual(profile.age_or_none(date(2026, 5, 5)), 26)
        self.assertIsNone(Profile().age_or_none(date(2026, 5, 5)))

    def test_age_before_birth_is_rejected(self):
        profile = Profile(birth_date=date(2000, 1, 1))
        with self.assertRaises(InvalidProfile):
            profile.age(date(1999, 1, 1))

    def test_throws_bats_needs_both(self):
        self.assertEqual(Profile(throws=Handedness.RIGHT, bats=Handedness.LEFT).throws_bats, "右投左打")
        self.assertEqual(Profile(throws=Handedness.RIGHT).throws_bats, "")

    def test_unrealistic_height_is_rejected(self):
        with self.assertRaises(InvalidProfile):
            Profile(height_cm=0)
        with self.assertRaises(InvalidProfile):
            Profile(height_cm=400)

    def test_non_numeric_height_is_rejected(self):
        with self.assertRaises(InvalidProfile):
            Profile(height_cm="高い")

    def test_draft_makes_the_profile_non_empty(self):
        self.assertFalse(Profile(draft=DraftPick(2020)).is_empty)

    def test_is_empty_becomes_false_with_any_field(self):
        self.assertFalse(Profile(birthplace="大阪府").is_empty)
        self.assertFalse(Profile(high_school="甲子園高校").is_empty)

    def test_nationality_and_foreign_flag_default_to_unset(self):
        profile = Profile()
        self.assertEqual(profile.nationality, "")
        self.assertFalse(profile.is_foreign_player)

    def test_nationality_makes_the_profile_non_empty(self):
        self.assertFalse(Profile(nationality="アメリカ合衆国").is_empty)

    def test_is_foreign_player_alone_does_not_make_the_profile_non_empty(self):
        """枠制度上の分類であり、記述情報ではないため is_empty には影響しない。"""
        self.assertTrue(Profile(is_foreign_player=True).is_empty)


class AmateurCareerTest(TestCase):
    """プロ入り前の経歴。順路は人によって異なる。"""

    def test_full_path(self):
        profile = Profile(high_school="甲子園高校", university="六大学", corporate_team="○○重工")

        self.assertEqual(
            profile.amateur_career,
            [("高校", "甲子園高校"), ("大学", "六大学"), ("社会人", "○○重工")],
        )
        self.assertEqual(profile.amateur_path, "甲子園高校 → 六大学 → ○○重工")

    def test_straight_from_high_school(self):
        """高校からそのままプロ入りする順路。"""
        profile = Profile(high_school="甲子園高校")

        self.assertEqual(profile.amateur_career, [("高校", "甲子園高校")])
        self.assertEqual(profile.amateur_path, "甲子園高校")

    def test_high_school_to_corporate_without_university(self):
        """大学を経ずに社会人へ進む順路。"""
        profile = Profile(high_school="甲子園高校", corporate_team="○○重工")

        self.assertEqual(profile.amateur_career, [("高校", "甲子園高校"), ("社会人", "○○重工")])

    def test_unknown_stages_are_omitted(self):
        """入力されていない区分は並べない。"""
        self.assertEqual(Profile(university="六大学").amateur_career, [("大学", "六大学")])

    def test_no_career_recorded(self):
        self.assertEqual(Profile().amateur_career, [])
        self.assertEqual(Profile().amateur_path, "")


class StadiumProfileTest(TestCase):
    def test_defaults_are_empty(self):
        profile = StadiumProfile()
        self.assertEqual(profile.city, "")
        self.assertIsNone(profile.capacity)

    def test_surface_is_restricted(self):
        self.assertEqual(StadiumProfile(surface="人工芝").surface, "人工芝")
        with self.assertRaises(InvalidProfile):
            StadiumProfile(surface="芝生")

    def test_negative_capacity_is_rejected(self):
        with self.assertRaises(InvalidProfile):
            StadiumProfile(capacity=-1)

    def test_opened_year_is_validated_as_a_season(self):
        with self.assertRaises(InvalidSeason):
            StadiumProfile(opened_year=1800)

    def test_roof_is_restricted(self):
        self.assertEqual(StadiumProfile(roof="ドーム").roof, "ドーム")
        with self.assertRaises(InvalidProfile):
            StadiumProfile(roof="ガラス張り")

    def test_covered_stadiums_are_not_affected_by_weather(self):
        """開閉式は閉じればドームと同じなので、覆える側に入れる。"""
        self.assertTrue(StadiumProfile(roof="ドーム").is_covered)
        self.assertTrue(StadiumProfile(roof="開閉式屋根").is_covered)
        self.assertFalse(StadiumProfile(roof="屋外").is_covered)

    def test_unknown_roof_is_not_treated_as_covered(self):
        """未設定は「分からない」。覆えると言い切れない以上そう扱わない。"""
        self.assertFalse(StadiumProfile().is_covered)


class StadiumTest(TestCase):
    def test_city_comes_from_the_stadium(self):
        """所在地は球場が持つ。チーム側には持たせない。"""
        stadium = Stadium(name="テスト球場", profile=StadiumProfile(city="仙台市"))

        self.assertEqual(stadium.city, "仙台市")
        self.assertEqual(str(stadium), "テスト球場")


class ChangePlayerProfileTest(TestCase):
    """`Team.change_player_profile`。検査に落ちたら選手のプロフィールは変わらない。"""

    def setUp(self):
        self.team = Team(name="テストチーム", id=1, league_id=1)
        self.player = self.team.add_player(
            "山田",
            JerseyNumber("120"),
            Position.PITCHER,
            from_year=2020,
            contract=ContractStatus.DEVELOPMENTAL,
            acquired_via=AcquisitionRoute.DEVELOPMENTAL_DRAFT,
            limits=RosterLimits.UNLIMITED,
        )
        self.player.id = 7
        self.before = Profile(height_cm=180, birthplace="東京都", draft=DraftPick(2019, round=2, is_development=True))
        self.player.profile = self.before

    def test_replaces_the_profile(self):
        profile = Profile(
            birth_date=date(2001, 4, 2),
            throws=Handedness.RIGHT,
            bats=Handedness.LEFT,
            height_cm=185,
            draft=DraftPick(2019, round=3, is_development=True),
            name_kana="ヤマダ",
            is_foreign_player=True,
        )
        self.team.change_player_profile(7, profile)
        self.assertEqual(self.player.profile, profile)

    def test_a_draft_that_contradicts_the_first_stint_is_rejected_and_nothing_changes(self):
        """育成ドラフトで入団した選手のドラフトを本指名にはできない（`ensure_draft_fits_career`）。"""
        with self.assertRaises(InvalidDraftPick):
            self.team.change_player_profile(7, Profile(height_cm=190, draft=DraftPick(2019, round=1)))
        self.assertEqual(self.player.profile, self.before)

    def test_an_unrealistic_height_is_rejected_and_nothing_changes(self):
        """値の上限は `Profile` が作るときに検査するので、集約に渡る前に止まる。"""
        with self.assertRaises(InvalidProfile):
            self.team.change_player_profile(7, Profile(height_cm=301))
        self.assertEqual(self.player.profile, self.before)

    def test_an_unknown_player_is_rejected(self):
        with self.assertRaises(PlayerNotFound):
            self.team.change_player_profile(999, Profile())
