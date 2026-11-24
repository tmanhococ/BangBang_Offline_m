import sys
import unittest
import json
import xml.etree.ElementTree as ET
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import capi

class TestFlashShopAndRent(unittest.TestCase):
    def test_ctankrentlist_returns_all_tanks(self):
        # Must handle both /ctankrentlist.php and /capi/ctankrentlist.php
        res = capi.handle('/ctankrentlist.php?noCache=1')
        self.assertIsNotNone(res, "handle('/ctankrentlist.php?noCache=1') should not return None")
        body, ctype = res
        self.assertIn('application/json', ctype)
        tanks = json.loads(body.decode('utf-8'))
        self.assertGreaterEqual(len(tanks), 88, "Should return at least 88 tanks")
        tank_ids = {t['tank_id'] for t in tanks}
        self.assertIn(10000001, tank_ids) # Iron Man
        self.assertIn(10000073, tank_ids) # Baymax

    def test_goodsshopquery_tab_1_returns_eggs_and_items(self):
        res = capi.handle('/goodsshopquery.php?gt=1&st=1&p=0&r=12')
        self.assertIsNotNone(res, "handle for gt=1 should not return None")
        body, ctype = res
        self.assertIn('application/xml', ctype)
        root = ET.fromstring(body.decode('utf-8'))
        rows = root.findall('row')
        self.assertGreaterEqual(len(rows), 1, "Tab 1 should have rows")
        template_oids = [int(r.attrib['template_oid']) for r in rows]
        # Should contain Trứng Xe Bí Ngô (2009302) or Trứng Thánh Gióng (2009300)
        self.assertTrue(any(t in (2009302, 2009300) for t in template_oids), "Tab 1 should contain egg boxes")

    def test_goodsshopquery_tab_2_returns_tanks(self):
        res = capi.handle('/goodsshopquery.php?gt=2&st=1&p=0&r=50')
        self.assertIsNotNone(res, "handle for gt=2 should not return None")
        body, ctype = res
        self.assertIn('application/xml', ctype)
        root = ET.fromstring(body.decode('utf-8'))
        rows = root.findall('row')
        self.assertGreaterEqual(len(rows), 25, "Tab 2 should have at least 25 tanks")
        for r in rows:
            self.assertEqual(r.attrib['goods_type'], '1', "Tanks in tab 2 must have goods_type=1")
        tank_ids = [int(r.attrib['template_oid']) for r in rows]
        self.assertIn(10000073, tank_ids, "Baymax should be in Tab 2")
        self.assertIn(10000077, tank_ids, "Zoro should be in Tab 2")

    def test_goodsshopquery_tab_4_returns_eggs(self):
        res = capi.handle('/goodsshopquery.php?gt=4&st=1&p=0&r=20')
        self.assertIsNotNone(res, "handle for gt=4 should not return None")
        body, ctype = res
        self.assertIn('application/xml', ctype)
        root = ET.fromstring(body.decode('utf-8'))
        rows = root.findall('row')
        self.assertGreaterEqual(len(rows), 5, "Tab 4 should have egg boxes")

if __name__ == '__main__':
    unittest.main()
