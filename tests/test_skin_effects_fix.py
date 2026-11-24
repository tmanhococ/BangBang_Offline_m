import sys
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import skin_effects

class TestSkinEffectsFix(unittest.TestCase):
    def test_translate_packets_with_none_bots(self):
        state = {
            'tank_ext': 1,
            'tank_id': 10000001,
            'bot': None,
            'ally_bot': None
        }
        # Packets can be empty or have dummy frames
        packets = [(1501, b'\x00' * 10)]
        try:
            result = skin_effects.translate_packets(packets, state)
            self.assertIsInstance(result, list)
        except AttributeError as e:
            self.fail(f"translate_packets raised AttributeError with None bots: {e}")

if __name__ == '__main__':
    unittest.main()
