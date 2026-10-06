"""仮想選手の投入コマンド（seed_virtual_players）。

ORM に直接書き込むため Team 集約の検査を通らない。背番号の一意性・外国人登録枠
など、集約が守る不変条件を投入結果が満たしているかをここで確かめる。
"""

from collections import Counter, defaultdict
from datetime import date
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase

from myapp.domain.pennant.world import WorldScope
from myapp.domain.virtual_players import generator
from myapp.domain.virtual_players.generator import MAX_ROSTER, MIN_ROSTER
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoTeamRepository
from myapp.management.commands.seed_virtual_players import ORIGINAL_PLAYER_READINGS

from .base import BaseCase

COMMAND = "seed_virtual_players"


def run(*args) -> str:
    out = StringIO()
    call_command(COMMAND, "--seed", "1", *args, stdout=out)
    return out.getvalue()


def add_player(team, name, *, number, is_foreign=False, from_year=2020, **fields) -> orm_models.Player:
    player = orm_models.Player.objects.create(
        name=name,
        is_foreign_player=is_foreign,
        nationality="アメリカ合衆国" if is_foreign else "",
        **fields,
    )
    orm_models.PlayerStint.objects.create(player=player, team=team, number=number, from_year=from_year)
    return player


def active_stints(team) -> list[orm_models.PlayerStint]:
    return list(orm_models.PlayerStint.objects.filter(team=team, to_year__isnull=True).select_related("player"))


class SeedRosterTest(BaseCase):
    """新規投入（オプションなし）。"""

    def setUp(self):
        super().setUp()
        self.original = add_player(self.team, "藤井健吾", number=10)

    def test_fills_each_team_within_roster_range_and_keeps_existing(self):
        run()

        for team in (self.team, self.rival):
            with self.subTest(team=team.name):
                self.assertTrue(MIN_ROSTER <= len(active_stints(team)) <= MAX_ROSTER)
        # 既存の選手と在籍には手を付けない（追加専用）
        self.original.refresh_from_db()
        self.assertEqual(self.original.name, "藤井健吾")
        stint = orm_models.PlayerStint.objects.get(player=self.original)
        self.assertEqual((stint.team_id, stint.number, stint.to_year), (self.team.id, "10", None))

    def test_jersey_numbers_are_unique_among_active_players(self):
        run()

        for team in (self.team, self.rival):
            numbers = [s.number for s in active_stints(team)]
            with self.subTest(team=team.name):
                self.assertEqual(len(numbers), len(set(numbers)))
                self.assertTrue(all(0 <= int(n) <= 999 for n in numbers))

    def test_profile_is_consistent_with_birth_date_and_stint(self):
        run()

        today = date.today()
        for stint in active_stints(self.team):
            player = stint.player
            if player == self.original:
                continue
            born = player.birth_date
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            with self.subTest(player=player.name):
                self.assertTrue(19 <= age <= 40)
                # 在籍の開始は入団年。高卒の18歳〜助っ人の30歳で入団している
                self.assertEqual(stint.from_year, player.debut_year)
                self.assertTrue(18 <= player.debut_year - born.year <= 30)
                self.assertTrue(player.back_name)
                self.assertTrue(player.name_kana)

    def test_foreign_players_carry_nationality_and_no_japanese_amateur_career(self):
        with mock.patch.object(generator, "FOREIGN_PLAYER_RATIO", 0.5):
            run()

        new_players = [s.player for s in active_stints(self.team) if s.player != self.original]
        self.assertTrue(any(p.is_foreign_player for p in new_players))
        self.assertTrue(any(not p.is_foreign_player for p in new_players))
        for player in new_players:
            with self.subTest(player=player.name):
                if player.is_foreign_player:
                    self.assertEqual(player.nationality, player.birthplace)
                    self.assertEqual((player.high_school, player.university, player.corporate_team), ("", "", ""))
                else:
                    self.assertEqual(player.nationality, "")
                    self.assertTrue(player.high_school)

    def test_back_names_distinguish_same_surname_within_team(self):
        run()

        for team in (self.team, self.rival):
            by_surname = defaultdict(list)
            for stint in active_stints(team):
                if stint.player.back_name:
                    by_surname[stint.player.back_name.split(".")[-1]].append(stint.player.back_name)
            for surname, back_names in by_surname.items():
                if len(back_names) > 1:
                    with self.subTest(team=team.name, surname=surname):
                        self.assertTrue(all("." in name for name in back_names))

    def test_dry_run_writes_nothing(self):
        output = run("--dry-run")

        self.assertIn("[dry-run]", output)
        self.assertEqual(orm_models.Player.objects.count(), 1)
        self.assertEqual(orm_models.PlayerStint.objects.count(), 1)


class SeedForeignPlayerQuotaTest(BaseCase):
    """外国人登録枠（League.foreign_player_roster_limit）を超えて投入しない。

    以前は枠を見ずに12%の確率で外国人にしていたため、48チーム中11チームが
    枠（5人）を超えていた。
    """

    def test_does_not_exceed_roster_limit(self):
        self.league.foreign_player_roster_limit = 2
        self.league.save()
        add_player(self.team, "マイケル・ジョンソン", number=1, is_foreign=True)

        # 全員を外国人として抽選させ、枠だけが歯止めになる状況を作る
        with mock.patch.object(generator, "FOREIGN_PLAYER_RATIO", 1.0):
            run()

        for team in (self.team, self.rival):
            with self.subTest(team=team.name):
                foreign = [s for s in active_stints(team) if s.player.is_foreign_player]
                self.assertEqual(len(foreign), 2)
                # 集約自身の検査にも通る
                DjangoTeamRepository(WorldScope.real()).find_by_id(team.id).ensure_foreign_player_quota(2)

    def test_blank_limit_means_unlimited(self):
        self.league.foreign_player_roster_limit = None
        self.league.save()

        with mock.patch.object(generator, "FOREIGN_PLAYER_RATIO", 1.0):
            run()

        self.assertTrue(all(s.player.is_foreign_player for s in active_stints(self.team)))


class RenameExistingTest(BaseCase):
    """--rename-existing: 架空の日本人選手の氏名だけを選び直す。"""

    def test_renames_only_virtual_japanese_players(self):
        original = add_player(self.team, "藤井健吾", number=10)
        foreign = add_player(self.team, "マイケル・ジョンソン", number=11, is_foreign=True)
        virtual = add_player(self.team, "佐藤翔太", number=12, name_kana="サトウショウタ")

        output = run("--rename-existing")

        self.assertIn("選手 1人の氏名を選び直しました", output)
        for player, expected in ((original, "藤井健吾"), (foreign, "マイケル・ジョンソン")):
            player.refresh_from_db()
            self.assertEqual(player.name, expected)
        virtual.refresh_from_db()
        self.assertTrue(virtual.name_kana)
        # 新規投入はしない
        self.assertEqual(orm_models.Player.objects.count(), 3)

    def test_dry_run_keeps_names(self):
        virtual = add_player(self.team, "佐藤翔太", number=12)

        run("--rename-existing", "--dry-run")

        virtual.refresh_from_db()
        self.assertEqual(virtual.name, "佐藤翔太")


class AssignReadingsTest(BaseCase):
    """--assign-readings: 氏名から読みを逆引きし、背ネームを付ける。"""

    def test_assigns_readings_and_back_names(self):
        original = add_player(self.team, "藤井健吾", number=10)
        sato_a = add_player(self.team, "佐藤翔太", number=11)
        sato_b = add_player(self.team, "佐藤大輔", number=12)
        rare = add_player(self.team, "小鳥遊一心", number=13)
        foreign = add_player(self.team, "マイケル・ジョンソン", number=14, is_foreign=True)
        # 別チームの同姓は区別しなくてよい
        other_sato = add_player(self.rival, "佐藤健太", number=11)

        run("--assign-readings")

        expected = {
            original: (ORIGINAL_PLAYER_READINGS["藤井健吾"][0], "FUJII"),
            sato_a: ("サトウショウタ", "S.SATO"),
            sato_b: ("サトウダイスケ", "D.SATO"),
            rare: ("タカナシイッシン", "TAKANASHI"),
            foreign: ("マイケル・ジョンソン", "JOHNSON"),
            other_sato: ("サトウケンタ", "SATO"),
        }
        for player, (kana, back_name) in expected.items():
            player.refresh_from_db()
            with self.subTest(player=player.name):
                self.assertEqual((player.name_kana, player.back_name), (kana, back_name))

    def test_reports_names_it_cannot_read(self):
        unknown = add_player(self.team, "無名太郎", number=10)

        output = run("--assign-readings")

        self.assertIn("読みを特定できず未対応のまま: 1人", output)
        unknown.refresh_from_db()
        self.assertEqual(unknown.back_name, "")


class RefreshSchoolsTest(BaseCase):
    """--refresh-schools: 架空の日本人選手の出身校だけを選び直す。"""

    def test_refreshes_only_virtual_japanese_players(self):
        original = add_player(self.team, "藤井健吾", number=10, high_school="実在高校")
        foreign = add_player(self.team, "マイケル・ジョンソン", number=11, is_foreign=True)
        virtual = add_player(self.team, "佐藤翔太", number=12, birthplace="北海道", debut_year=2020)

        output = run("--refresh-schools")

        self.assertIn("選手 1人の出身高校", output)
        original.refresh_from_db()
        foreign.refresh_from_db()
        virtual.refresh_from_db()
        self.assertEqual(original.high_school, "実在高校")
        self.assertEqual(foreign.high_school, "")
        self.assertTrue(virtual.high_school)
        # 入団年など他の項目は変えない
        self.assertEqual(virtual.debut_year, 2020)

    def test_public_high_school_is_in_birthplace(self):
        virtual = add_player(self.team, "佐藤翔太", number=12, birthplace="北海道")

        # 私立を引かせない
        with mock.patch.object(generator, "PRIVATE_HIGH_SCHOOL_RATIO", 0.0):
            run("--refresh-schools")

        virtual.refresh_from_db()
        self.assertTrue(virtual.high_school.startswith("北海道"))
        self.assertNotIn("北海道立", virtual.high_school)


class LargestRemainderTest(SimpleTestCase):
    """ポジションの人数配分（最大剰余法）は合計を崩さない。"""

    def test_allocation_sums_to_total(self):
        ratios = generator.POSITION_RATIOS
        for total in range(MIN_ROSTER, MAX_ROSTER + 1):
            with self.subTest(total=total):
                allocation = generator.largest_remainder(total, ratios)
                self.assertEqual(sum(allocation.values()), total)
                self.assertEqual(Counter(allocation.keys()), Counter(ratios.keys()))
