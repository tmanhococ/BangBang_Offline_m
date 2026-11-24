import sys
import unittest
import struct
import json
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import storage
import cosmetic_shop
import normal_shop

class TestFlashBuy(unittest.TestCase):
    def setUp(self):
        cosmetic_shop.initialize()
        self.profile_id = 9999901
        self.state = {
            'char_id': self.profile_id,
            'char_uuid': f'char-{self.profile_id}',
            'tank_id': 10000001,
            'avatar': 1,
            'decorate_id': 0
        }
        with storage.connection(write=True) as db:
            storage.for_profile(self.profile_id)
            db.execute('INSERT OR REPLACE INTO users(username, salt, pw, created) VALUES (?,?,?,?)', (f'user{self.profile_id}', 's', 'p', 1.0))
            db.execute('INSERT OR REPLACE INTO worlds(profile_id, state) VALUES (?,?)', (self.profile_id, json.dumps(self.state)))
            db.execute('INSERT OR REPLACE INTO characters(profile_id, username) VALUES (?,?)', (self.profile_id, f'user{self.profile_id}'))
            # Give player 10,000,000 Silver, 100,000 Cash, 100,000 Gift
            profile = {
                'avatar': 1,
                'decorate': 0,
                'currency': [100000, 10000000, 100000, 0, 0, 0]
            }
            cosmetic_shop._set(db, cosmetic_shop.PROFILE, profile)
            db.execute('DELETE FROM garage WHERE profile_id=?', (self.profile_id,))

    def tearDown(self):
        with storage.connection(write=True) as db:
            db.execute('DELETE FROM garage WHERE profile_id=?', (self.profile_id,))
            db.execute('DELETE FROM characters WHERE profile_id=?', (self.profile_id,))
            db.execute('DELETE FROM worlds WHERE profile_id=?', (self.profile_id,))
            db.execute('DELETE FROM users WHERE username=?', (f'user{self.profile_id}',))

    def test_buy_tank_via_c250(self):
        # Baymax goodsId is 910000073
        cat = normal_shop.catalog()
        baymax_offer = next(o for o in cat['offers'] if o['itemId'] == 10000073)
        goods_id = baymax_offer['goodsId']
        price = baymax_offer['payments'][0]['price']
        coin_type = baymax_offer['payments'][0]['coinType'] # 2 = Silver

        # Pack C250 payload: kind(i), goods(I), coin(I), price(i), count(i), n(H), [coin_key(I), coin_val(i)]
        n = 1
        payload = struct.pack('<iIIiiH', 1, goods_id, coin_type, price, 1, n)
        payload += struct.pack('<Ii', coin_type, price)

        with storage.connection(write=True) as db:
            packets, error = cosmetic_shop.apply(self.state, 250, payload, db)
            self.assertIsNone(error)
            # Check packets: 1039 (wallet), 1150 (garage), 1501 (tip)
            msg_ids = [mid for mid, _ in packets]
            self.assertIn(1039, msg_ids)
            self.assertIn(1150, msg_ids)
            self.assertIn(1501, msg_ids)

            # Check garage has Baymax
            row = db.execute('SELECT tank_id FROM garage WHERE profile_id=? AND tank_id=10000073', (self.profile_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 10000073)

            # Buying again should fail with already owned
            packets2, error2 = cosmetic_shop.apply(self.state, 250, payload, db)
            self.assertIsNotNone(error2)
            self.assertIn('đã sở hữu', error2)

    def test_buy_item_via_c250(self):
        # Trứng Xe Bí Ngô goodsId is 902009302
        cat = normal_shop.catalog()
        egg_offer = next(o for o in cat['offers'] if o['itemId'] == 2009302)
        goods_id = egg_offer['goodsId']
        price = egg_offer['payments'][0]['price']
        coin_type = egg_offer['payments'][0]['coinType']

        n = 1
        payload = struct.pack('<iIIiiH', 1, goods_id, coin_type, price, 1, n)
        payload += struct.pack('<Ii', coin_type, price)

        with storage.connection(write=True) as db:
            packets, error = cosmetic_shop.apply(self.state, 250, payload, db)
            self.assertIsNone(error)
            msg_ids = [mid for mid, _ in packets]
            self.assertIn(1039, msg_ids)
            self.assertIn(1100, msg_ids)
            self.assertIn(1501, msg_ids)

if __name__ == '__main__':
    unittest.main()
