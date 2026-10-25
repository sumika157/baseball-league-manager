"""ワークスペースの数の上限（`WorkspaceQuota`）の業務ルール（DB 不要・Django 非依存）。"""

import unittest

from myapp.domain.exceptions import InvalidWorkspace, WorkspaceLimitReached
from myapp.domain.workspace import WorkspaceQuota


class WorkspaceQuotaTest(unittest.TestCase):
    def test_up_to_the_limit_can_be_created(self):
        """上限3なら、0〜2個持っている人はもう1つ作れ、3個目まで作れる。4個目は作れない。"""
        quota = WorkspaceQuota(max_owned_workspaces=3)
        for owned in (0, 1, 2):
            with self.subTest(owned=owned):
                self.assertTrue(quota.can_create_workspace(owned))
                quota.ensure_can_create_workspace(owned)
        for owned in (3, 4):
            with self.subTest(owned=owned):
                self.assertFalse(quota.can_create_workspace(owned))
                with self.assertRaisesRegex(WorkspaceLimitReached, "1人3つまで"):
                    quota.ensure_can_create_workspace(owned)

    def test_the_message_follows_the_limit(self):
        self.assertEqual(WorkspaceQuota(max_owned_workspaces=5).limit_message, "ワークスペースは1人5つまで作れます。")

    def test_zero_means_nobody_can_create(self):
        self.assertFalse(WorkspaceQuota(max_owned_workspaces=0).can_create_workspace(0))

    def test_the_limit_must_be_a_non_negative_int(self):
        for value in (-1, True, 3.0, "3"):
            with self.subTest(value=value), self.assertRaises(InvalidWorkspace):
                WorkspaceQuota(max_owned_workspaces=value)  # type: ignore[arg-type]
