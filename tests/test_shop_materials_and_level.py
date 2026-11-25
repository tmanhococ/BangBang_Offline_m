import sys
import unittest
import json
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import storage
import normal_shop
import cosmetic_shop
import inventory
import match_rewards
from multiplayer_battle import BattleService


class TestShopMaterialsAndLevel(unittest.TestCase):
    def setUp(self):
        cosmetic_shop.initialize()
        self.profile_id = 9999904
        self.state = {
            'char_id': self.profile_id,
            'char_uuid': f'char-{self.profile_id}',
            'name': f'testuser{self.profile_id}',
            'level': 1,
            'n_exp': 0,
            'sex': 1,
            'tank_id': 10000001,
            'avatar': 1,
            'decorate_id': 0
        }
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                db.execute('INSERT OR REPLACE INTO users(username, salt, pw, created) VALUES (?,?,?,?)',
                           (f'user{self.profile_id}', 's', 'p', 1.0))
                db.execute('INSERT OR REPLACE INTO worlds(profile_id, state) VALUES (?,?)',
                           (self.profile_id, json.dumps(self.state)))
                db.execute('INSERT OR REPLACE INTO characters(profile_id, username) VALUES (?,?)',
                           (self.profile_id, f'user{self.profile_id}'))
                profile = {
                    'avatar': 1,
                    'decorate': 0,
                    'currency': [0, 500000, 5000, 0, 0, 0]  # 500k silver, 5k coupons
                }
                cosmetic_shop._set(db, cosmetic_shop.PROFILE, profile)
                inventory.save({str(c): [] for c in range(1, 11)}, db)

    def test_catalog_contains_basic_upgrade_materials(self):
        catalog = normal_shop.catalog()
        self.assertIn('offers', catalog)
        offers = catalog['offers']
        
        # Check that Topaz (3000051), Mảnh Sắt (3000001), Thạch anh mặt trời (3000148), Thủy Tinh Moonlight (3000147) exist
        required_items = {
            3000001: 'Mảnh Sắt',
            3000051: 'Topaz',
            3000148: 'Thạch anh mặt trời',
            3000147: 'Thủy Tinh Moonlight'
        }
        for item_id, name in required_items.items():
            # In Tab 5 (Nguyên liệu)
            tab5_match = [o for o in offers if o.get('tab') == 5 and o.get('itemId') == item_id]
            self.assertTrue(len(tab5_match) > 0, f"Missing {name} ({item_id}) in Tab 5")
            # In Tab 1 (Đề cử)
            tab1_match = [o for o in offers if o.get('tab') == 1 and o.get('itemId') == item_id]
            self.assertTrue(len(tab1_match) > 0, f"Missing {name} ({item_id}) in Tab 1")

    def test_purchase_basic_materials_with_silver_and_coupons(self):
        # Plan buying Mảnh Sắt (goodsId 998005126) with silver
        with storage.for_profile(self.profile_id):
            with storage.connection(write=False) as db:
                intent_iron = {'kind': 'buy', 'goods_id': 998005126, 'payment': 'alternate', 'quantity': 1}
                p_iron = normal_shop.plan(db, intent_iron)
                self.assertIsNotNone(p_iron)
                self.assertEqual(p_iron['quote']['total_price'], 500)

                # Plan buying Topaz (goodsId 998005127) with silver
                intent_topaz = {'kind': 'buy', 'goods_id': 998005127, 'payment': 'alternate', 'quantity': 1}
                p_topaz = normal_shop.plan(db, intent_topaz)
                self.assertIsNotNone(p_topaz)
                self.assertEqual(p_topaz['quote']['total_price'], 500)

                # Plan buying Thạch anh mặt trời (goodsId 998005128) with silver
                intent_sun = {'kind': 'buy', 'goods_id': 998005128, 'payment': 'alternate', 'quantity': 1}
                p_sun = normal_shop.plan(db, intent_sun)
                self.assertIsNotNone(p_sun)
                self.assertEqual(p_sun['quote']['total_price'], 100000)

                # Plan buying Thủy Tinh Moonlight (goodsId 998005129) with silver
                intent_moon = {'kind': 'buy', 'goods_id': 998005129, 'payment': 'alternate', 'quantity': 1}
                p_moon = normal_shop.plan(db, intent_moon)
                self.assertIsNotNone(p_moon)
                self.assertEqual(p_moon['quote']['total_price'], 100000)

    def test_multiplayer_snapshot_includes_level_and_exp(self):
        service = BattleService.__new__(BattleService)
        dummy_match = type('Match', (), {
            'states': {
                self.profile_id: {
                    'room': {'room_id': 1},
                    'battle': {'scene_id': 1},
                    'level': 3,
                    'n_exp': 150
                }
            }
        })()
        service._match = lambda p: dummy_match if p == self.profile_id else None
        snap = service._snapshot_state(self.profile_id)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.get('level'), 3)
        self.assertEqual(snap.get('n_exp'), 150)


if __name__ == '__main__':
    unittest.main()
