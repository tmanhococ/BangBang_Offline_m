import sys
import unittest
from pathlib import Path
import json

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import normal_shop

class TestShopCatalog(unittest.TestCase):
    def test_catalog_loads_and_verifies_hash(self):
        # Must load cleanly without RewardError (proves revision SHA256 digest is valid)
        cat = normal_shop.catalog()
        self.assertIsInstance(cat, dict)
        self.assertIn('offers', cat)
        self.assertIn('boxes', cat)
        self.assertIn('rewards', cat)
        self.assertIn('revision', cat)

    def test_catalog_contains_expanded_tanks(self):
        cat = normal_shop.catalog()
        tank_offers = [o for o in cat['offers'] if o.get('tab') == 2 and o.get('grantKind') == 'tank']
        
        # We require at least 25 tanks in tab 2
        self.assertGreaterEqual(len(tank_offers), 25, "Tab 2 (Tank) should have at least 25 tanks available")
        
        sold_tank_ids = {o['itemId'] for o in tank_offers}
        expected_new_tanks = [
            10000073,  # Baymax
            10000077,  # Zoro
            10000078,  # Mikasa
            10000080,  # Doraemon
            10000096,  # Kirito
            10000102,  # Saitama
            10000093,  # Sasuke
            10000095,  # Kurumi
            10000082,  # Na Tra
            10000083,  # Doctor Strange
        ]
        for t_id in expected_new_tanks:
            self.assertIn(t_id, sold_tank_ids, f"Tank ID {t_id} must be in tab 2 offers")
            reward_key = f"tank:{t_id}"
            self.assertIn(reward_key, cat['rewards'], f"Reward descriptor for {reward_key} must exist")

    def test_catalog_contains_expanded_egg_boxes(self):
        cat = normal_shop.catalog()
        boxes = cat['boxes']
        
        # Check that we have at least 15 egg boxes registered
        egg_box_ids = [
            2009302,  # Trứng Xe Bí Ngô
            2009300,  # Trứng Thánh Gióng
            2010007,  # Trứng Groot
            2003072,  # Trứng Baymax
            2003077,  # Trứng Zoro
            2003078,  # Trứng Mikasa
            2003080,  # Trứng Doraemon
            2003097,  # Trứng Kirito
            2003520,  # Trứng Saitama
            2003093,  # Trứng Sasuke
            2003095,  # Trứng Kurumi
            2003082,  # Trứng Na Tra
            2003083,  # Trứng Doctor Strange
            2710025,  # Trứng Sơn Tinh
            2003079,  # Trứng Thủy Tinh
            2710014,  # Trứng Thor
            2710017,  # Trứng Songoku
            2710019,  # Trứng Naruto
            2710020,  # Trứng Luffy
            2710021,  # Trứng Pikachu
        ]
        for b_id in egg_box_ids:
            str_id = str(b_id)
            self.assertIn(str_id, boxes, f"Egg Box {str_id} must be registered in catalog boxes")
            box = boxes[str_id]
            self.assertEqual(box.get('mode'), 'random')
            self.assertEqual(box.get('main_percent'), 10)
            self.assertEqual(box.get('material_percent'), 90)
            self.assertEqual(box.get('duplicate_policy'), 'reject_owned_unique_main')
            self.assertTrue(len(box.get('main_rewards', [])) >= 1)

            # Check that offer exists for this egg
            offer = next((o for o in cat['offers'] if o.get('itemId') == b_id), None)
            self.assertIsNotNone(offer, f"Offer for egg {b_id} must exist in catalog offers")
            self.assertIn(offer['tab'], (1, 4), "Egg offers should be in tab 1 (Featured) or tab 4 (Items)")

if __name__ == '__main__':
    unittest.main()
