import sys
import unittest
import json
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import storage
import cosmetic_shop
import inventory
import mail_rewards
import shop_boxes
import normal_shop
from storage_import import GARAGE_FIELDS


class TestDuplicateTankOpening(unittest.TestCase):
    def setUp(self):
        cosmetic_shop.initialize()
        self.profile_id = 9999905
        self.state = {
            'char_id': self.profile_id,
            'char_uuid': f'char-{self.profile_id}',
            'name': f'testuser{self.profile_id}',
            'level': 1,
            'sex': 1,
            'tank_id': 10000052,
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
                    'currency': [100000, 1000000, 10000, 0, 0, 0]
                }
                cosmetic_shop._set(db, cosmetic_shop.PROFILE, profile)
                # Give 10 pumpkin egg boxes (template_id: 3000147 or similar egg)
                # Let's see normal_shop_catalog box id for Trứng Xe Bí Ngô: 3000147
                containers = {
                    '2': [
                        {'uuid': 'egg-box-1', 'template_id': 2009302, 'count': 10, 'slot_no': 0,
                         'ext_flag': 1, 'level': 0, 'up_level': 0, 'rank': 0, 'life_time': 0}
                    ],
                    '3': []
                }
                inventory.save(containers, db)
                # Add existing tank 10000052 into garage
                db.execute('DELETE FROM garage WHERE profile_id=?', (self.profile_id,))
                tank = {field: '' if fmt == 's' else 0 for field, fmt in GARAGE_FIELDS}
                tank.update(uuid='existing-tank-uuid-1', tank_id=10000052, level=1, cont_type=0)
                db.execute('INSERT OR REPLACE INTO garage(profile_id, ordinal, uuid, tank_id, data) VALUES (?,?,?,?,?)',
                           (self.profile_id, 0, 'existing-tank-uuid-1', 10000052, json.dumps(tank)))

    def test_mail_rewards_allows_duplicate_tank(self):
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                available = mail_rewards.catalog(db)
                rewards = [{'kind': 'tank', 'id': 10000052, 'quantity': 1}]
                # Should NOT raise already_owned
                plan = mail_rewards.plan(db, self.profile_id, rewards, available)
                self.assertEqual(len(plan['new_tanks']), 1)
                self.assertEqual(plan['new_tanks'][0][1]['tank_id'], 10000052)
                self.assertNotEqual(plan['new_tanks'][0][1]['uuid'], 'existing-tank-uuid-1')

                # Commit plan
                if not db.in_transaction:
                    db.execute('BEGIN IMMEDIATE')
                mail_rewards.commit_plan(db, self.state, plan)
                db.commit()

                # Garage should now have 2 instances of tank 10000052
                rows = db.execute('SELECT uuid, tank_id FROM garage WHERE profile_id=? AND tank_id=10000052',
                                  (self.profile_id,)).fetchall()
                self.assertEqual(len(rows), 2)

    def test_bulk_box_open_with_duplicate_tank(self):
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                catalog = normal_shop.catalog()
                box = catalog['boxes'].get('2009302')
                available = normal_shop._available(db, catalog)
                ref = {'container': 2, 'slot_no': 0, 'template_id': 2009302, 'count': 10, 'uuid': 'egg-box-1'}

                # draw_bulk 10 boxes
                res = shop_boxes.draw_bulk(db, box, ref, available, 10)
                self.assertIn('value', res)
                self.assertIn('all_rewards', res)
                self.assertEqual(len(res['all_rewards']), 10)


if __name__ == '__main__':
    unittest.main()
