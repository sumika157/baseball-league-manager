"""ワークスペースの業務ルール（DB 不要・Django 非依存）。"""

import random
import unittest

from myapp.domain.exceptions import InvalidWorkspace
from myapp.domain.workspace import (
    KEY_ALPHABET,
    KEY_LENGTH,
    MAX_NAME_LENGTH,
    Visibility,
    Workspace,
    generate_workspace_key,
    validate_workspace_key,
)

KEY = "abcde12345"


class VisibilityTest(unittest.TestCase):
    def test_choices_are_public_and_private(self):
        self.assertEqual(Visibility.choices(), [("public", "公開"), ("private", "非公開")])

    def test_every_member_has_a_label(self):
        for member in Visibility:
            self.assertTrue(member.label)


class WorkspaceTest(unittest.TestCase):
    def test_name_is_stripped(self):
        workspace = Workspace(name=" 私のリーグ ", key=KEY, owner_id=1, visibility=Visibility.PUBLIC)

        self.assertEqual(workspace.name, "私のリーグ")
        self.assertEqual(workspace.visibility, Visibility.PUBLIC)
        self.assertIsNone(workspace.id)

    def test_empty_or_blank_name_is_rejected(self):
        for name in ("", "   ", None):
            with self.subTest(name=name), self.assertRaises(InvalidWorkspace):
                Workspace(name=name, key=KEY, owner_id=1, visibility=Visibility.PUBLIC)  # type: ignore[arg-type]

    def test_too_long_name_is_rejected(self):
        Workspace(name="あ" * MAX_NAME_LENGTH, key=KEY, owner_id=1, visibility=Visibility.PUBLIC)
        with self.assertRaises(InvalidWorkspace):
            Workspace(name="あ" * (MAX_NAME_LENGTH + 1), key=KEY, owner_id=1, visibility=Visibility.PUBLIC)

    def test_owner_must_be_a_positive_int(self):
        for owner_id in (0, -1, True, None):
            with self.subTest(owner_id=owner_id), self.assertRaises(InvalidWorkspace):
                Workspace(name="a", key=KEY, owner_id=owner_id, visibility=Visibility.PUBLIC)  # type: ignore[arg-type]

    def test_visibility_must_be_a_visibility(self):
        with self.assertRaises(InvalidWorkspace):
            Workspace(name="a", key=KEY, owner_id=1, visibility="public")  # type: ignore[arg-type]

    def test_bad_key_is_rejected(self):
        with self.assertRaises(InvalidWorkspace):
            Workspace(name="a", key="short", owner_id=1, visibility=Visibility.PUBLIC)


class WorkspaceKeyTest(unittest.TestCase):
    def test_system_random_is_accepted(self):
        self.assertEqual(len(generate_workspace_key(random.SystemRandom())), KEY_LENGTH)

    def test_generated_key_has_the_right_shape(self):
        key = generate_workspace_key(random.Random(1))

        self.assertEqual(len(key), KEY_LENGTH)
        self.assertTrue(all(c in KEY_ALPHABET for c in key))
        self.assertEqual(validate_workspace_key(key), key)

    def test_same_random_source_gives_the_same_key(self):
        self.assertEqual(generate_workspace_key(random.Random(7)), generate_workspace_key(random.Random(7)))

    def test_different_seeds_give_different_keys(self):
        self.assertNotEqual(generate_workspace_key(random.Random(1)), generate_workspace_key(random.Random(2)))

    def test_validation_rejects_wrong_length_and_characters(self):
        for key in ("", "abcde1234", "abcde123456", "ABCDE12345", "abcde-2345", "あいうえおかきくけこ"):
            with self.subTest(key=key), self.assertRaises(InvalidWorkspace):
                validate_workspace_key(key)
