from django.test import TestCase
from django.contrib.auth.models import User
from .services import MemoService, ValidationError


class MemoServiceTest(TestCase):
    def setUp(self):
        # テスト用のユーザーを作成
        self.user = User.objects.create_user(username="testuser", password="password")

    def test_create_memo_empty_content(self):
        """空のメモを投稿しようとすると ValidationError が出ることのテスト"""
        with self.assertRaises(ValidationError):
            MemoService.create_memo(self.user, "")

    def test_create_memo_too_long(self):
        """200文字を超えるメモを投稿しようとすると ValidationError が出ることのテスト"""
        long_content = "あ" * 201
        with self.assertRaises(ValidationError):
            MemoService.create_memo(self.user, long_content)

    def test_create_memo_success(self):
        """正しい内容ならメモが作成されることのテスト"""
        memo = MemoService.create_memo(self.user, "正常なメモ")
        self.assertEqual(memo.content, "正常なメモ")
