import sys
import unittest
import struct
import json
import os
import hashlib
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import storage
import cosmetic_shop
import normal_shop
import mail_rewards
import inventory

class TestShopFixes(unittest.TestCase):
    def setUp(self):
        cosmetic_shop.initialize()
        self.profile_id = 9999902
        self.state = {
            'char_id': self.profile_id,
            'char_uuid': f'char-{self.profile_id}',
            'tank_id': 10000001,
            'avatar': 1,
            'decorate_id': 0
        }
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                db.execute('INSERT OR REPLACE INTO users(username, salt, pw, created) VALUES (?,?,?,?)', (f'user{self.profile_id}', 's', 'p', 1.0))
                db.execute('INSERT OR REPLACE INTO worlds(profile_id, state) VALUES (?,?)', (self.profile_id, json.dumps(self.state)))
                db.execute('INSERT OR REPLACE INTO characters(profile_id, username) VALUES (?,?)', (self.profile_id, f'user{self.profile_id}'))
                profile = {
                    'avatar': 1,
                    'decorate': 0,
                    'currency': [100000, 10000000, 100000, 0, 0, 0]
                }
                cosmetic_shop._set(db, cosmetic_shop.PROFILE, profile)
                inventory.save({str(c): [] for c in (2, 3, 4, 5, 6, 7, 9, 10)}, db)
                db.execute('DELETE FROM garage WHERE profile_id=?', (self.profile_id,))

    def tearDown(self):
        with storage.connection(write=True) as db:
            db.execute('DELETE FROM garage WHERE profile_id=?', (self.profile_id,))
            db.execute('DELETE FROM characters WHERE profile_id=?', (self.profile_id,))
            db.execute('DELETE FROM worlds WHERE profile_id=?', (self.profile_id,))
            db.execute('DELETE FROM users WHERE username=?', (f'user{self.profile_id}',))

    def test_normal_shop_available_no_catalog_mismatch(self):
        """Verify _available passes without 'catalog_mismatch' RewardError for any egg box."""
        with storage.connection() as db:
            source = normal_shop.catalog()
            avail = normal_shop._available(db, source)
            self.assertIsInstance(avail, dict)
            # Ensure all boxes in source['boxes'] are validated
            for box_id in source['boxes']:
                key = f"item:{box_id}"
                self.assertIn(key, avail, f"Box {box_id} must be in available catalog")

    def test_offer_icons_exist_and_match_hash(self):
        """Verify every offer's icon exists on disk and matches sha256 & dimensions."""
        cat = normal_shop.catalog()
        root = Path(__file__).resolve().parents[1]
        tank_icons = set()
        egg_icons = set()

        for offer in cat['offers']:
            icon_obj = offer.get('icon')
            self.assertIsNotNone(icon_obj, f"Offer {offer['name']} must have an icon")
            url = icon_obj['url']
            rel_path = url.lstrip('/')
            disk_path = root / 'www' / rel_path
            self.assertTrue(disk_path.exists(), f"Icon file {disk_path} for {offer['name']} must exist on disk")

            with open(disk_path, 'rb') as f:
                content = f.read()
                calc_sha = hashlib.sha256(content).hexdigest()
            self.assertEqual(icon_obj['sha256'], calc_sha, f"SHA256 mismatch for {offer['name']}")

            if offer.get('grantKind') == 'tank':
                self.assertEqual(icon_obj['width'], 24)
                self.assertEqual(icon_obj['height'], 24)
                tank_icons.add(url)
            elif 'Trứng' in offer.get('name', ''):
                self.assertEqual(icon_obj['width'], 100)
                self.assertEqual(icon_obj['height'], 100)
                egg_icons.add(url)

        # Ensure tank icons are not all identical to a single dummy icon
        self.assertGreater(len(tank_icons), 20, "Tank icons must be distinct, not all dummy Iron Man")
        self.assertGreater(len(egg_icons), 15, "Egg icons must be distinct, not all dummy Pumpkin Egg")

    def test_normal_shop_plan_buy_type69_eggs(self):
        """Verify planning purchases of nType=69 egg boxes succeeds without catalog_mismatch."""
        with storage.for_profile(self.profile_id):
            with storage.connection() as db:
                # 902003520 (Trứng Saitama, nType=69)
                intent = {'kind': 'buy', 'goods_id': 902003520, 'payment': 'primary', 'quantity': 1}
                plan = normal_shop.plan(db, intent)
                self.assertEqual(plan['quote']['name'], 'Trứng Saitama')

                # 902710014 (Trứng Thor, nType=69)
                intent_thor = {'kind': 'buy', 'goods_id': 902710014, 'payment': 'primary', 'quantity': 1}
                plan_thor = normal_shop.plan(db, intent_thor)
                self.assertEqual(plan_thor['quote']['name'], 'Trứng Thor')

                # 902003096 (Trứng Kirito, item 2003097, nType=27)
                intent_kirito = {'kind': 'buy', 'goods_id': 902003096, 'payment': 'primary', 'quantity': 1}
                plan_kirito = normal_shop.plan(db, intent_kirito)
                self.assertEqual(plan_kirito['quote']['name'], 'Trứng Kirito')

    def test_flash_shop_query_tabs(self):
        """Verify Flash query for gt=6 (VP) returns egg boxes, and gt=2 returns tanks."""
        import xml.etree.ElementTree as ET
        # gt=6 (VP)
        xml_vp = cosmetic_shop.shop_query({'gt': ['6'], 'p': ['0'], 'r': ['100']})
        root_vp = ET.fromstring(xml_vp)
        rows_vp = root_vp.findall('row')
        self.assertGreater(len(rows_vp), 15, "Tab 6 (VP) should return all egg boxes and items")

        # gt=2 (Tank)
        xml_tank = cosmetic_shop.shop_query({'gt': ['2'], 'p': ['0'], 'r': ['100']})
        root_tank = ET.fromstring(xml_tank)
        rows_tank = root_tank.findall('row')
        self.assertGreaterEqual(len(rows_tank), 25, "Tab 2 (Tank) should return all tanks")

    def test_flash_buy_type69_egg(self):
        """Verify buying a type-69 egg box via C250 succeeds in Flash client."""
        cat = normal_shop.catalog()
        saitama_offer = next(o for o in cat['offers'] if o['itemId'] == 2003520)
        goods_id = saitama_offer['goodsId']
        price = saitama_offer['payments'][0]['price']
        coin_type = saitama_offer['payments'][0]['coinType']

        payload = struct.pack('<iIIiiH', 1, goods_id, coin_type, price, 1, 1)
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
