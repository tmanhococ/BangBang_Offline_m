"""Match completion rewards, EXP progression, and character leveling."""
from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from copy import deepcopy

import storage
import cosmetic_shop
import inventory
import boosters
from crystals import bag_updates

_LEVEL_THRESHOLDS: dict[int, int] | None = None
MAX_LEVEL = 40


def _load_thresholds() -> dict[int, int]:
    global _LEVEL_THRESHOLDS
    if _LEVEL_THRESHOLDS is not None:
        return _LEVEL_THRESHOLDS
    xml_path = Path(__file__).resolve().parents[1] / 'data' / 'xmlpack' / 'CharLevelTemplate_table.xml'
    thresholds = {}
    if xml_path.exists():
        tree = ET.parse(str(xml_path))
        for e in tree.findall('.//entry'):
            lvl = e.findtext('nLevel')
            need = e.findtext('iNeedExp')
            if lvl is not None and need is not None:
                thresholds[int(lvl)] = int(need)
    else:
        # Fallback standard progression
        thresholds = {
            1: 350, 2: 550, 3: 800, 4: 1000, 5: 1250, 6: 1600, 7: 2000,
            8: 2500, 9: 3300, 10: 4500, 40: -1
        }
    _LEVEL_THRESHOLDS = thresholds
    return thresholds


def get_need_exp(level: int) -> int:
    thresholds = _load_thresholds()
    return thresholds.get(level, -1)


def calculate_level_progression(current_level: int, current_exp: int, gained_exp: int) -> tuple[int, int]:
    thresholds = _load_thresholds()
    level = current_level
    exp = current_exp + gained_exp
    while level < MAX_LEVEL:
        need = thresholds.get(level, -1)
        if need <= 0:
            break
        if exp < need:
            break
        exp -= need
        level += 1
    if level >= MAX_LEVEL:
        level = MAX_LEVEL
        exp = 0
    return level, exp


def apply_pvp_rewards(state: dict, arena, db) -> tuple[dict, list[tuple[int, bytes]]]:
    """Calculate and persist match rewards, update arena stats, wallet, and inventory."""
    pk = getattr(arena, 'pk', {})
    winner = pk.get('winner', 0)

    # Find player actor in arena
    player_actor = getattr(arena, 'player', None)
    if player_actor is None:
        for a in getattr(arena, 'actors', {}).values():
            if getattr(a, 'info', {}).get('acct_id') == state.get('char_id'):
                player_actor = a
                break

    is_winner = player_actor is not None and (winner != 0 and player_actor.camp == winner)
    
    # Check MVP status
    stats = pk.get('stats', {})
    is_mvp = False
    if winner and stats and player_actor:
        winners = [i for i in stats if arena.actors.get(i) and arena.actors[i].camp == winner]
        if winners:
            # MVP calculation matching pk_rules.py
            best_mvp = max(winners, key=lambda i: (stats[i][1] + stats[i][3], stats[i][6], -i))
            is_mvp = (best_mvp == player_actor.id)

    # Base reward formula: ~10 matches = 1 tank (1,000,000 silver / ~3600-4000 coupons)
    if is_winner:
        exp = 400 + (50 if is_mvp else 0)
        silver = 120000 + (20000 if is_mvp else 0)
        coupon = 400 + (50 if is_mvp else 0)
        iron = 25 + (5 if is_mvp else 0)
        topaz = 25 + (5 if is_mvp else 0)
    else:
        exp = 250 + (50 if is_mvp else 0)
        silver = 80000 + (20000 if is_mvp else 0)
        coupon = 300 + (50 if is_mvp else 0)
        iron = 15 + (5 if is_mvp else 0)
        topaz = 15 + (5 if is_mvp else 0)

    # Update arena stats for settlement packet 1708 (index 10 is exp, index 11 is coins)
    if player_actor and player_actor.id in stats:
        stats[player_actor.id][10] = exp
        stats[player_actor.id][11] = silver

    # Update state level & exp
    cur_level = state.get('level', 1)
    cur_exp = state.get('n_exp', 0)
    new_level, new_exp = calculate_level_progression(cur_level, cur_exp, exp)
    state['level'] = new_level
    state['n_exp'] = new_exp

    # Persist state to worlds
    char_id = state.get('char_id')
    if char_id:
        db.execute('UPDATE worlds SET state=? WHERE profile_id=?',
                   (json.dumps(state, ensure_ascii=False, separators=(',', ':')), char_id))

    # Persist wallet currencies
    wallet = cosmetic_shop.snapshot(db)
    # currency: [cash(0), silver(1), coupon(2), honor(3), crystal(4), presentPoint(5)]
    wallet['currency'][1] += silver
    wallet['currency'][2] += coupon
    cosmetic_shop._set(db, cosmetic_shop.PROFILE, wallet)

    # Persist inventory materials: 3000001 (Sắt), 3000051 (Topaz)
    containers = inventory.load(db)
    previous_items = deepcopy(containers)
    boosters._grant(containers, 3000001, iron)
    boosters._grant(containers, 3000051, topaz)
    inventory.save(containers, db)

    # Construct reply packets
    wallet_pkt = (1039, struct.pack('<HH6i', 2, 0, *wallet['currency']))
    bag_pkts = bag_updates(previous_items, containers, (3,))
    packets = [wallet_pkt] + bag_pkts

    reward_info = {
        'exp': exp,
        'silver': silver,
        'coupon': coupon,
        'iron': iron,
        'topaz': topaz,
        'is_winner': is_winner,
        'is_mvp': is_mvp,
        'new_level': new_level,
        'new_exp': new_exp,
    }
    return reward_info, packets


def apply_pve_rewards(state: dict, db, won: bool) -> tuple[dict, list[tuple[int, bytes]]]:
    """Grant PvE rewards including character EXP and currencies on clear."""
    if not won:
        return {'exp': 0, 'silver': 0, 'coupon': 0, 'iron': 0, 'topaz': 0}, []

    exp = 350
    silver = 100000
    coupon = 350
    iron = 20
    topaz = 20

    arena = (state.get('battle') or {}).get('actions')
    if arena and hasattr(arena, 'pk') and isinstance(arena.pk.get('stats'), dict) and 1 in arena.pk['stats']:
        arena.pk['stats'][1][10] = exp
        arena.pk['stats'][1][11] = silver

    cur_level = state.get('level', 1)
    cur_exp = state.get('n_exp', 0)
    new_level, new_exp = calculate_level_progression(cur_level, cur_exp, exp)
    state['level'] = new_level
    state['n_exp'] = new_exp

    char_id = state.get('char_id')
    if char_id:
        db.execute('UPDATE worlds SET state=? WHERE profile_id=?',
                   (json.dumps(state, ensure_ascii=False, separators=(',', ':')), char_id))

    wallet = cosmetic_shop.snapshot(db)
    wallet['currency'][1] += silver
    wallet['currency'][2] += coupon
    cosmetic_shop._set(db, cosmetic_shop.PROFILE, wallet)

    containers = inventory.load(db)
    previous_items = deepcopy(containers)
    boosters._grant(containers, 3000001, iron)
    boosters._grant(containers, 3000051, topaz)
    inventory.save(containers, db)

    wallet_pkt = (1039, struct.pack('<HH6i', 2, 0, *wallet['currency']))
    bag_pkts = bag_updates(previous_items, containers, (3,))
    packets = [wallet_pkt] + bag_pkts

    reward_info = {
        'exp': exp,
        'silver': silver,
        'coupon': coupon,
        'iron': iron,
        'topaz': topaz,
        'new_level': new_level,
        'new_exp': new_exp,
    }
    return reward_info, packets
