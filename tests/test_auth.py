"""账号密码和会话令牌的安全边界测试。"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import auth
import db
from memory import MemoryManager


@pytest.fixture
def auth_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with patch("db.get_conn", return_value=conn):
        db.init_db()
        yield conn
    conn.close()


def test_first_account_hashes_password_and_session_can_be_revoked(auth_db):
    with patch("auth.get_conn", return_value=auth_db):
        user = auth.register_first_user("campus_admin", "a-strong-password")
        token, _ = auth.create_session(user["id"])
        assert user["role"] == "admin"
        assert auth.get_user_by_token(token)["username"] == "campus_admin"
        stored = auth_db.execute("SELECT password_hash FROM users").fetchone()[0]
        assert stored != "a-strong-password"
        assert "a-strong-password" not in stored
        auth.revoke_token(token)
        assert auth.get_user_by_token(token) is None


def test_two_character_username_is_allowed(auth_db):
    with patch("auth.get_conn", return_value=auth_db):
        user = auth.register_first_user("梦梦", "a-strong-password")
        assert user["username"] == "梦梦"


def test_wrong_password_and_second_initial_registration_are_rejected(auth_db):
    with patch("auth.get_conn", return_value=auth_db):
        auth.register_first_user("campus_admin", "a-strong-password")
        with pytest.raises(ValueError, match="用户名或密码错误"):
            auth.authenticate("campus_admin", "wrong-pass")
        with pytest.raises(ValueError, match="管理员账号已创建"):
            auth.register_first_user("another_user", "another-password")


def test_memories_are_scoped_to_account(auth_db):
    with patch("memory.get_conn", return_value=auth_db):
        manager = MemoryManager()
        manager.add("账号一的专业是农学", user_id=1)
        manager.add("账号二的专业是计算机", user_id=2)
        assert [row["content"] for row in manager.list_all(1)] == ["账号一的专业是农学"]
        assert [row["content"] for row in manager.list_all(2)] == ["账号二的专业是计算机"]
        assert manager.search("农学", user_id=2) == []
