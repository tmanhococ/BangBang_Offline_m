import sys
import unittest
import json
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import economy_actions
import normal_shop


class TestShopCatalogRebalance(unittest.TestCase):
    def test_catalog_validity_and_rebalanced_prices(self):
        # 1. Normal shop catalog load must be valid and pass revision verification
        cat = normal_shop.catalog()
        expected_rev = economy_actions.digest({k: v for k, v in cat.items() if k != 'revision'})
        self.assertEqual(cat['revision'], expected_rev, 'Catalog revision must match content hash')

        # 2. Check tank tab offers
        tab2_offers = [o for o in cat['offers'] if o.get('tab') == 2]
        self.assertTrue(len(tab2_offers) >= 30, 'Should have at least 30 tank offers in tab 2')

        # Iron Man check (formerly 360 gift / 100,000 silver -> now 3,600 / 1,000,000)
        iron_man = next((o for o in tab2_offers if o['goodsId'] == 902001636), None)
        self.assertIsNotNone(iron_man)
        p_primary = next(p for p in iron_man['payments'] if p['key'] == 'primary')
        p_alt = next(p for p in iron_man['payments'] if p['key'] == 'alternate')
        self.assertEqual(p_primary['price'], 3600, 'Iron Man xu khóa should be 3600 (x10)')
        self.assertEqual(p_alt['price'], 1000000, 'Iron Man silver should be 1000000 (x10)')

        # Gundam check (formerly 100,000 silver -> now 1,000,000 silver)
        gundam = next((o for o in tab2_offers if o['goodsId'] == 902000003), None)
        self.assertIsNotNone(gundam)
        self.assertEqual(gundam['payments'][0]['price'], 1000000)


if __name__ == '__main__':
    unittest.main()
