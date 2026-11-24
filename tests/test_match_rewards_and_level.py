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
import match_rewards
import characters


class DummyActor:
    def __init__(self, actor_id, acct_id, camp, tank_id=10000001):
        self.id = actor_id
        self.camp = camp
        self.info = {
            'acct_id': acct_id,
            'tank_id': tank_id,
            'tank_level': 1,
            'human': True,
        }


class DummyArena:
    def __init__(self, scene_id, map_id, player_actor_id, player_acct_id, winner_camp=1):
        self.scene_id = scene_id
        player = DummyActor(player_actor_id, player_acct_id, camp=1)
        enemy = DummyActor(2, 9999999, camp=2)
        self.actors = {player_actor_id: player, 2: enemy}
        self.player = player
        self.geometry = type('Geom', (), {'id': map_id})()
        self.state = {}
        self.pk = {
            'begin': 0,
            'end': 60000,
            'finished': True,
            'winner': winner_camp,
            'stats': {
                player_actor_id: [0] * 21,
                2: [0] * 21
            }
        }


class TestMatchRewardsAndLevel(unittest.TestCase):
    def setUp(self):
        cosmetic_shop.initialize()
        self.profile_id = 9999903
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
                    'currency': [0, 10000, 0, 0, 0, 0]  # 10k silver, 0 cash, 0 coupon
                }
                cosmetic_shop._set(db, cosmetic_shop.PROFILE, profile)
                inventory.save({str(c): [] for c in range(1, 11)}, db)

    def test_level_progression_math(self):
        # Level 1 needs 350 EXP to reach Level 2
        lvl, exp = match_rewards.calculate_level_progression(current_level=1, current_exp=0, gained_exp=400)
        self.assertEqual(lvl, 2)
        self.assertEqual(exp, 50)

        # Level 2 needs 550 EXP to reach Level 3
        # Current 50 + gained 600 = 650 -> Level 3 with 100 EXP
        lvl2, exp2 = match_rewards.calculate_level_progression(current_level=lvl, current_exp=exp, gained_exp=600)
        self.assertEqual(lvl2, 3)
        self.assertEqual(exp2, 100)

        # Level 40 is max level
        lvl_max, exp_max = match_rewards.calculate_level_progression(current_level=40, current_exp=0, gained_exp=999999)
        self.assertEqual(lvl_max, 40)

    def test_apply_pvp_rewards_win(self):
        with storage.for_profile(self.profile_id):
            with storage.connection(write=True) as db:
                arena = DummyArena(scene_id=1, map_id=12, player_actor_id=1, player_acct_id=self.profile_id, winner_camp=1)
                arena.state['battle'] = {'match_uuid': 'test-match-1'}
                
                # Player won match
                reward_info, packets = match_rewards.apply_pvp_rewards(self.state, arena, db)
                
                # Check reward amounts
                self.assertGreaterEqual(reward_info['exp'], 400)
                self.assertGreaterEqual(reward_info['silver'], 120000)
                self.assertGreaterEqual(reward_info['coupon'], 400)
                self.assertGreaterEqual(reward_info['iron'], 25)
                self.assertGreaterEqual(reward_info['topaz'], 25)

                # Player should have reached Level 2 (needed 350 EXP)
                self.assertEqual(self.state['level'], 2)
                self.assertEqual(self.state['n_exp'], reward_info['exp'] - 350)

                # Stats in arena updated for settlement packet 1708
                self.assertEqual(arena.pk['stats'][1][10], reward_info['exp'])
                self.assertEqual(arena.pk['stats'][1][11], reward_info['silver'])

                # Check wallet in DB
                wallet = cosmetic_shop.snapshot(db)
                self.assertEqual(wallet['currency'][1], 10000 + reward_info['silver'])
                self.assertEqual(wallet['currency'][2], reward_info['coupon'])

                # Check materials in container 3
                containers = inventory.load(db)
                c3 = containers.get('3', [])
                iron_item = next((i for i in c3 if i['template_id'] == 3000001), None)
                topaz_item = next((i for i in c3 if i['template_id'] == 3000051), None)
                self.assertIsNotNone(iron_item)
                self.assertIsNotNone(topaz_item)
                self.assertEqual(iron_item['count'], reward_info['iron'])
                self.assertEqual(topaz_item['count'], reward_info['topaz'])

                # Check packets include wallet 1039
                self.assertTrue(any(mid == 1039 for mid, _ in packets))

    def test_login_payload_1016_packs_level_and_exp(self):
        # 1016 CHAR_BASIC frame
        dummy_body = bytearray(712)
        state = {
            'char_id': self.profile_id,
            'char_uuid': f'char-{self.profile_id}',
            'name': 'testuser',
            'sex': 1,
            'level': 5,
            'n_exp': 1234,
            'game_token': 0
        }
        res = characters.login_payload(1016, dummy_body, state)
        # Offset 4: sex (1), offset 5: level (5)
        self.assertEqual(res[4], 1)
        self.assertEqual(res[5], 5)
        # Offset 10..14: exp (1234)
        exp_unpacked, = struct.unpack_from('<i', res, 10)
        self.assertEqual(exp_unpacked, 1234)


if __name__ == '__main__':
    unittest.main()
