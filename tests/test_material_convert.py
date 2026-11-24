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
import inventory
import crystals


class TestMaterialConvert(unittest.TestCase):
    def setUp(self):
        self.profile_id = 9999905
        self.state = {
            'char_id': self.profile_id,
            'char_uuid': f'char-{self.profile_id}',
            'name': f'user{self.profile_id}',
            'level': 1,
            'sex': 1,
            'tank_id': 10000001,
            'avatar': 1,
            'decorate_id': 0
        }
        with storage.for_profile(self.profile_id):
            crystals.initialize()
            with storage.connection(write=True) as db:
                db.execute('INSERT OR REPLACE INTO users(username, salt, pw, created) VALUES (?,?,?,?)',
                           (f'user{self.profile_id}', 's', 'p', 1.0))
                db.execute('INSERT OR REPLACE INTO worlds(profile_id, state) VALUES (?,?)',
                           (self.profile_id, json.dumps(self.state)))
                db.execute('INSERT OR REPLACE INTO characters(profile_id, username) VALUES (?,?)',
                           (self.profile_id, f'user{self.profile_id}'))
                cosmetic_shop._set(db, cosmetic_shop.PROFILE, {'avatar': 1, 'decorate': 0, 'currency': [0, 10000, 0, 0, 0, 0]})
                
                # Give player 20 Sắt (3000001) and 10 Topaz (3000051) in container 3
                items = [
                    {
                        'template_id': 3000001,
                        'life_time': 0,
                        'ext_flag': 1,
                        'count': 20,
                        'level': 0,
                        'up_level': 0,
                        'exp': 0,
                        'rank': 0,
                        'up_cost': 0,
                        'rank_cost': 0,
                        'create_time': 10000,
                        'slot_no': 0,
                        'uuid': 'item-iron-20'
                    },
                    {
                        'template_id': 3000051,
                        'life_time': 0,
                        'ext_flag': 1,
                        'count': 10,
                        'level': 0,
                        'up_level': 0,
                        'exp': 0,
                        'rank': 0,
                        'up_cost': 0,
                        'rank_cost': 0,
                        'create_time': 10000,
                        'slot_no': 1,
                        'uuid': 'item-topaz-10'
                    }
                ]
                inventory.save({'2': [], '3': items, '4': [], '7': [], '10': []}, db)

    def test_convert_iron_to_steel(self):
        """Recipe 58: 5 Sắt (3000001) + 50 Bạc -> 1 Thép (3000003)"""
        # C155 wire: count=1 (H), [(recipe_id=58, amount=2)] (II), coins=0 (H)
        payload = struct.pack('<HIIH', 1, 58, 2, 0)
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                packets, error = crystals.apply(self.state, 155, payload, db)
                self.assertIsNone(error)
                self.assertTrue(len(packets) > 0)

                # Check inventory: Sắt should be 20 - 10 = 10, Thép should be 2
                containers = inventory.load(db)
                c3 = containers.get('3', [])
                iron = next((i for i in c3 if i['template_id'] == 3000001), None)
                steel = next((i for i in c3 if i['template_id'] == 3000003), None)
                self.assertIsNotNone(iron)
                self.assertEqual(iron['count'], 10)
                self.assertIsNotNone(steel)
                self.assertEqual(steel['count'], 2)

                # Check wallet: silver was 10000, 2 conversions * 50 = 100 fee -> 9900
                wallet = cosmetic_shop.snapshot(db)
                self.assertEqual(wallet['currency'][1], 9900)

    def test_convert_topaz_to_ruby(self):
        """Recipe 62: 5 Topaz (3000051) + 50 Bạc -> 1 Hồng ngọc (3000052)"""
        payload = struct.pack('<HIIH', 1, 62, 1, 0)
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                packets, error = crystals.apply(self.state, 155, payload, db)
                self.assertIsNone(error)

                containers = inventory.load(db)
                c3 = containers.get('3', [])
                topaz = next((i for i in c3 if i['template_id'] == 3000051), None)
                ruby = next((i for i in c3 if i['template_id'] == 3000052), None)
                self.assertIsNotNone(topaz)
                self.assertEqual(topaz['count'], 5)
                self.assertIsNotNone(ruby)
                self.assertEqual(ruby['count'], 1)

                wallet = cosmetic_shop.snapshot(db)
                self.assertEqual(wallet['currency'][1], 9950)

    def test_convert_insufficient_materials(self):
        """Trying to convert 10 times (needs 50 iron, player only has 20) fails"""
        payload = struct.pack('<HIIH', 1, 58, 10, 0)
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                packets, error = crystals.apply(self.state, 155, payload, db)
                self.assertIsNotNone(error)
                self.assertIn('Không đủ vật phẩm', error)


if __name__ == '__main__':
    unittest.main()
