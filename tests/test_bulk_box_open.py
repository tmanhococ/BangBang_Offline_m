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
import normal_shop
from mail_rewards import RewardError


class TestBulkBoxOpen(unittest.TestCase):
    def setUp(self):
        cosmetic_shop.initialize()
        self.profile_id = 9999904
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
            with storage.connection(write=True) as db:
                db.execute('INSERT OR REPLACE INTO users(username, salt, pw, created) VALUES (?,?,?,?)',
                           (f'user{self.profile_id}', 's', 'p', 1.0))
                db.execute('INSERT OR REPLACE INTO worlds(profile_id, state) VALUES (?,?)',
                           (self.profile_id, json.dumps(self.state)))
                db.execute('INSERT OR REPLACE INTO characters(profile_id, username) VALUES (?,?)',
                           (self.profile_id, f'user{self.profile_id}'))
                db.execute('DELETE FROM settings WHERE name LIKE ?', (f'shop_v1:{self.profile_id}:%',))
                cosmetic_shop._set(db, cosmetic_shop.PROFILE, {'avatar': 1, 'decorate': 0, 'currency': [0, 10000, 0, 0, 0, 0]})
                
                # Give player 10 "Trứng Xe Bí Ngô" (2009302) in container 2
                item = {
                    'template_id': 2009302,
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
                    'slot_no': 0,
                    'uuid': 'item-box-10'
                }
                inventory.save({'2': [item], '3': [], '4': [], '7': []}, db)

    def test_open_box_intent_validation(self):
        ref = {
            'container': 2,
            'uuid': 'item-box-10',
            'template_id': 2009302,
            'count': 10,
            'slot_no': 0
        }
        # Valid single
        res1 = normal_shop.canonical({'kind': 'open_box', 'item': ref})
        self.assertEqual(res1['quantity'], 1)

        # Valid bulk 5
        res5 = normal_shop.canonical({'kind': 'open_box', 'item': ref, 'quantity': 5})
        self.assertEqual(res5['quantity'], 5)

        # Invalid quantity
        with self.assertRaises(RewardError):
            normal_shop.canonical({'kind': 'open_box', 'item': ref, 'quantity': 0})
        with self.assertRaises(RewardError):
            normal_shop.canonical({'kind': 'open_box', 'item': ref, 'quantity': 101})

    def test_bulk_open_box_apply(self):
        context = normal_shop.session()
        ref = {
            'container': 2,
            'uuid': 'item-box-10',
            'template_id': 2009302,
            'count': 10,
            'slot_no': 0
        }
        intent = {'kind': 'open_box', 'item': ref, 'quantity': 5}
        
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                # 1. Preview
                req_preview = {
                    'v': 1,
                    'request_id': 'req1',
                    'operation_id': 'op1',
                    'action': 'preview',
                    'intent': intent
                }
                body_prev = normal_shop.text(req_preview).encode('utf-8')
                wire_prev = struct.pack('<H', len(body_prev)) + body_prev
                pkts_prev, err_prev = normal_shop.apply(self.state, wire_prev, db, context)
                self.assertIsNone(err_prev)
                resp_prev = json.loads(pkts_prev[-1][1][2:].decode('utf-8'))
                self.assertEqual(resp_prev['status'], 'preview')
                self.assertEqual(resp_prev['quote']['quantity'], 5)
                proof = resp_prev['proof']

                # 2. Apply
                req_apply = {
                    'v': 1,
                    'request_id': 'req2',
                    'operation_id': 'op1',
                    'action': 'apply',
                    'intent': intent,
                    'proof': proof
                }
                body_apply = normal_shop.text(req_apply).encode('utf-8')
                wire_apply = struct.pack('<H', len(body_apply)) + body_apply
                pkts_apply, err_apply = normal_shop.apply(self.state, wire_apply, db, context)
                self.assertIsNone(err_apply)
                resp_apply = json.loads(pkts_apply[-1][1][2:].decode('utf-8'))
                self.assertEqual(resp_apply['status'], 'committed')
                self.assertEqual(len(resp_apply['receipt']['rewards']), 5)

                # Check that remaining boxes is 10 - 5 = 5
                containers = inventory.load(db)
                c2 = containers.get('2', [])
                box_item = next((i for i in c2 if i['template_id'] == 2009302), None)
                self.assertIsNotNone(box_item)
                self.assertEqual(box_item['count'], 5)


if __name__ == '__main__':
    unittest.main()
