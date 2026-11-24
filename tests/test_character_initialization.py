import sys
import unittest
from pathlib import Path
import json
import time

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from auth_store import Store
import characters
import storage

class TestCharacterInitialization(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.prefix = f"test_char_{int(time.time()*1000)%1000000}"

    def tearDown(self):
        with storage.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE username LIKE 'test_char_%'")
            profile_ids = [r[0] for r in db.execute("SELECT profile_id FROM characters WHERE username LIKE 'test_char_%'").fetchall()]
            db.execute("DELETE FROM garage WHERE profile_id IN (SELECT profile_id FROM characters WHERE username LIKE 'test_char_%')")
            db.execute("DELETE FROM characters WHERE username LIKE 'test_char_%'")
            for p_id in profile_ids:
                db.execute("DELETE FROM worlds WHERE profile_id=?", (p_id,))
                db.execute("DELETE FROM settings WHERE name LIKE ?", (f"%:{p_id}",))
            try:
                db.execute("DELETE FROM account_buff_flags WHERE username LIKE 'test_char_%'")
            except Exception:
                pass
            db.execute("DELETE FROM users WHERE username LIKE 'test_char_%'")

    def test_character_init_unbuffed(self):
        # Create unbuffed user
        username = f"{self.prefix}_normal"
        # Register normal user
        self.store.register(username, "password123")
        
        # Initialize character for this user
        profile_id = characters.for_username(username)
        self.assertIsInstance(profile_id, int)

        with storage.connection() as db:
            world_row = db.execute("SELECT state FROM worlds WHERE profile_id=?", (profile_id,)).fetchone()
            self.assertIsNotNone(world_row)
            state = json.loads(world_row[0])

            # Requirements for unbuffed normal account:
            # 1. Level 1, VIP 0
            self.assertEqual(state.get('level'), 1, "Normal account must start at level 1")
            self.assertEqual(state.get('vip_level'), 0, "Normal account must start at VIP 0")

            # 2. Basic starter tanks in garage (e.g. Iron Man 10000001, Chopper 10000029, Gundam 10000019)
            garage_rows = db.execute("SELECT tank_id, data FROM garage WHERE profile_id=?", (profile_id,)).fetchall()
            tank_ids = [r[0] for r in garage_rows]
            self.assertIn(10000001, tank_ids, "Iron Man must be a starter tank")
            self.assertIn(10000029, tank_ids, "Chopper must be a starter tank")
            self.assertIn(10000019, tank_ids, "Gundam must be a starter tank")
            self.assertLessEqual(len(tank_ids), 5, "Normal account should have only a few starter tanks (not full 91)")

            # Check that all starter tanks are level 1
            for r in garage_rows:
                t_data = json.loads(r[1])
                self.assertEqual(t_data.get('level'), 1, f"Starter tank {r[0]} must be level 1")

            # 3. Currency starting balance: 10,000 Silver, 0 Gold/Cash
            shop_profile = db.execute("SELECT value FROM settings WHERE name=?", (storage.setting_key('cosmetic_shop_profile', profile_id),)).fetchone()
            self.assertIsNotNone(shop_profile)
            currency = json.loads(shop_profile[0])['currency']
            self.assertEqual(currency[1], 10000, "Normal account should start with 10,000 Silver")
            self.assertEqual(currency[0], 0, "Normal account should start with 0 Cash")
            self.assertEqual(currency[2], 0, "Normal account should start with 0 Gift vouchers")

    def test_character_init_buffed(self):
        username = f"{self.prefix}_buffed"
        # Admin creates with buff=True (or flag set directly)
        with storage.connection(write=True) as db:
            from auth_store import hash_pw
            import secrets
            salt = secrets.token_hex(8)
            db.execute("INSERT INTO users VALUES (?,?,?,?)", (username, salt, hash_pw("pass123", salt), int(time.time())))
            db.execute("INSERT OR REPLACE INTO account_buff_flags (username, is_buff) VALUES (?,?)", (username, 1))

        profile_id = characters.for_username(username)
        self.assertIsInstance(profile_id, int)

        with storage.connection() as db:
            world_row = db.execute("SELECT state FROM worlds WHERE profile_id=?", (profile_id,)).fetchone()
            self.assertIsNotNone(world_row)
            state = json.loads(world_row[0])

            # Requirements for buffed account:
            # Level 36, VIP 6, 91 tanks
            self.assertEqual(state.get('level'), 36, "Buffed account must have max level 36")
            self.assertEqual(state.get('vip_level'), 6, "Buffed account must have VIP 6")

            garage_rows = db.execute("SELECT tank_id FROM garage WHERE profile_id=?", (profile_id,)).fetchall()
            self.assertEqual(len(garage_rows), 91, "Buffed account must have all 91 tanks")

if __name__ == '__main__':
    unittest.main()
