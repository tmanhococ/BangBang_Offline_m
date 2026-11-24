import sys
import unittest
from pathlib import Path
import sqlite3
import time

# Ensure server module can be imported
SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from auth_store import Store, hash_pw
import admin_accounts
import storage

class TestAuthRegistration(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.test_username = f"test_reg_{int(time.time()*1000)%1000000}"
        self.test_password = "password123"

    def tearDown(self):
        with storage.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE username LIKE 'test_reg_%' OR username LIKE 'test_admin_%'")
            db.execute("DELETE FROM users WHERE username LIKE 'test_reg_%' OR username LIKE 'test_admin_%'")
            db.execute("DELETE FROM characters WHERE username LIKE 'test_reg_%' OR username LIKE 'test_admin_%'")
            # If account_buff_flags exists, clean up too
            try:
                db.execute("DELETE FROM account_buff_flags WHERE username LIKE 'test_reg_%' OR username LIKE 'test_admin_%'")
            except sqlite3.OperationalError:
                pass

    def test_register_new_user_success(self):
        # Must be able to call register on Store
        self.assertTrue(hasattr(self.store, 'register'), "Store must have register method")
        res = self.store.register(self.test_username, self.test_password)
        self.assertTrue(res.get('success'), f"Registration failed: {res.get('message')}")

        with storage.connection() as db:
            user = db.execute("SELECT username, salt, pw FROM users WHERE username=?", (self.test_username,)).fetchone()
            self.assertIsNotNone(user, "User was not created in users table")
            self.assertEqual(user['pw'], hash_pw(self.test_password, user['salt']))
            
            # Check buff flag is 0 for normal registration
            flag_row = db.execute("SELECT is_buff FROM account_buff_flags WHERE username=?", (self.test_username,)).fetchone()
            self.assertIsNotNone(flag_row, "Buff flag entry must exist")
            self.assertEqual(flag_row[0], 0, "Normal registration must have is_buff = 0")

    def test_register_duplicate_username_fails(self):
        self.assertTrue(hasattr(self.store, 'register'))
        res1 = self.store.register(self.test_username, self.test_password)
        self.assertTrue(res1.get('success'))

        res2 = self.store.register(self.test_username, "anotherpassword")
        self.assertFalse(res2.get('success'))
        self.assertIn("đã tồn tại", res2.get('message', '').lower())

    def test_register_invalid_inputs(self):
        self.assertTrue(hasattr(self.store, 'register'))
        # Username too short (< 3)
        res = self.store.register("ab", "password123")
        self.assertFalse(res.get('success'))

        # Password too short (< 6)
        res = self.store.register("validuser", "12345")
        self.assertFalse(res.get('success'))

    def test_login_unregistered_user_fails(self):
        # Crucial requirement: unregistered user cannot simply log in to auto-create
        unreg = f"unreg_{int(time.time()*1000)%1000000}"
        res = self.store.login(unreg, "password123")
        self.assertFalse(res.get('success'))
        self.assertIn("không tồn tại", res.get('message', '').lower())

    def test_admin_create_account_with_buff_flag(self):
        admin_key = b"0123456789abcdef0123456789abcdef"
        
        # Test 1: Admin creates account with buff = True
        u_buff = f"test_admin_b_{int(time.time()*1000)%1000000}"
        payload_buff = {
            'action': 'create',
            'request_id': f"req_{int(time.time()*1000)%1000000}_1",
            'username': u_buff,
            'password': 'password123',
            'buff': True
        }
        res_preview, digest = admin_accounts.preview(payload_buff, admin_key)
        self.assertEqual(res_preview.get('status'), 'prepared')
        exec_body = {'intent': payload_buff, 'preview_token': digest}
        res_exec = admin_accounts.execute(exec_body, admin_key, lambda t, d: True)
        self.assertEqual(res_exec.get('status'), 'completed')

        with storage.connection() as db:
            flag_row = db.execute("SELECT is_buff FROM account_buff_flags WHERE username=?", (u_buff,)).fetchone()
            self.assertIsNotNone(flag_row)
            self.assertEqual(flag_row[0], 1, "Admin creation with buff=True must set is_buff = 1")

        # Test 2: Admin creates account with buff = False
        u_nobuff = f"test_admin_nb_{int(time.time()*1000)%1000000}"
        payload_nobuff = {
            'action': 'create',
            'request_id': f"req_{int(time.time()*1000)%1000000}_2",
            'username': u_nobuff,
            'password': 'password123',
            'buff': False
        }
        res_preview2, digest2 = admin_accounts.preview(payload_nobuff, admin_key)
        self.assertEqual(res_preview2.get('status'), 'prepared')
        exec_body2 = {'intent': payload_nobuff, 'preview_token': digest2}
        res_exec2 = admin_accounts.execute(exec_body2, admin_key, lambda t, d: True)
        self.assertEqual(res_exec2.get('status'), 'completed')

        with storage.connection() as db:
            flag_row2 = db.execute("SELECT is_buff FROM account_buff_flags WHERE username=?", (u_nobuff,)).fetchone()
            self.assertIsNotNone(flag_row2)
            self.assertEqual(flag_row2[0], 0, "Admin creation with buff=False must set is_buff = 0")


if __name__ == '__main__':
    unittest.main()
