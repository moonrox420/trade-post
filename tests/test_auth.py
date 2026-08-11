"""Unit tests for trade_post.security.auth."""

import unittest

from trade_post.core.config import load_settings
from trade_post.security.auth import (
    hash_password, verify_password, new_session_id, new_user_id, session_expiry,
)


class TestPasswordHashing(unittest.TestCase):
    def setUp(self):
        self.s = load_settings()

    def test_hash_and_verify(self):
        h = hash_password("secret-pw", self.s)
        self.assertTrue(h.startswith("pbkdf2-sha256$"))
        self.assertTrue(verify_password("secret-pw", h))

    def test_wrong_password(self):
        h = hash_password("correct", self.s)
        self.assertFalse(verify_password("wrong", h))

    def test_empty_password_raises(self):
        from trade_post.core.errors import AuthenticationError
        with self.assertRaises(AuthenticationError):
            hash_password("", self.s)

    def test_verify_empty_returns_false(self):
        h = hash_password("x", self.s)
        self.assertFalse(verify_password("", h))
        self.assertFalse(verify_password("x", ""))
        self.assertFalse(verify_password("x", "garbage"))


class TestSession(unittest.TestCase):
    def test_new_ids_unique(self):
        self.assertNotEqual(new_session_id(), new_session_id())
        self.assertNotEqual(new_user_id(), new_user_id())
        self.assertGreater(len(new_session_id()), 32)

    def test_session_expiry_future(self):
        from datetime import datetime, timedelta
        s = load_settings()
        exp = session_expiry(s)
        now = datetime.utcnow()
        # Should be at least `session_ttl_minutes` minutes in the future.
        delta = exp - now
        self.assertGreater(delta, timedelta(minutes=s.session_ttl_minutes - 1))


if __name__ == "__main__":
    unittest.main()
