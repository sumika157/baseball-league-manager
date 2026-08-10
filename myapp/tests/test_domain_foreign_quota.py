"""外国人枠（登録上限）の単体テスト。Django も DB も使わない。"""

from dataclasses import replace
from unittest import TestCase

from myapp.domain.entities import Team
from myapp.domain.exceptions import ForeignPlayerQuotaExceeded
from myapp.domain.value_objects import JerseyNumber, Position, ensure_quota_not_exceeded


def _make_foreign(player):
    player.profile = replace(player.profile, is_foreign_player=True)
    return player


class EnsureQuotaNotExceededTest(TestCase):
    def test_allows_none_limit(self):
        ensure_quota_not_exceeded(1000, None, 'エラー')  # 例外にならない

    def test_allows_exactly_at_the_limit(self):
        ensure_quota_not_exceeded(3, 3, 'エラー')  # 例外にならない

    def test_raises_when_count_exceeds_limit(self):
        with self.assertRaises(ForeignPlayerQuotaExceeded):
            ensure_quota_not_exceeded(4, 3, 'エラー')


class TeamForeignPlayerQuotaTest(TestCase):
    def setUp(self):
        self.team = Team(name='テストチーム', id=1, league_id=1)

    def _add_foreign(self, name, number, from_year=2026):
        player = self.team.add_player(name, JerseyNumber(number), Position.INFIELDER, from_year=from_year)
        return _make_foreign(player)

    def test_quota_is_ignored_when_no_limit_set(self):
        for i in range(5):
            self._add_foreign(f'外国人{i}', 10 + i)

        self.team.ensure_foreign_player_quota(None)  # 例外にならない

    def test_quota_allows_exactly_at_the_limit(self):
        self._add_foreign('外国人1', 10)
        self._add_foreign('外国人2', 11)

        self.team.ensure_foreign_player_quota(2)  # 例外にならない

    def test_quota_rejects_when_over_limit(self):
        self._add_foreign('外国人1', 10)
        self._add_foreign('外国人2', 11)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.team.ensure_foreign_player_quota(1)

    def test_non_foreign_players_do_not_count(self):
        self.team.add_player('日本人', JerseyNumber(10), Position.INFIELDER, from_year=2026)
        self._add_foreign('外国人1', 11)

        self.assertEqual(self.team.foreign_player_count, 1)
        self.team.ensure_foreign_player_quota(1)  # 例外にならない

    def test_retired_foreign_players_do_not_count(self):
        player = self._add_foreign('外国人1', 10, from_year=2024)
        self.team.retire_player(player, 2025)

        self.assertEqual(self.team.foreign_player_count, 0)
        self.team.ensure_foreign_player_quota(0)  # 例外にならない
