"""Per-character solo chapter-one progress; rewards share the world-save transaction."""
from copy import deepcopy
from collections import Counter
import struct

import boosters
import cosmetic_shop as shop
import inventory
import storage
from pve_catalog import MAPS, CITIES, NORMAL_TO_HARD, resolve_request
from crystals import award_packet, bag_updates

SETTING = 'pve_progress'
REWARD = (3700002, 3700002, 3800001)
REWARD_V3 = REWARD + (3800002, 4400001, 4400002, 3900002, 4000001)
SECOND_PET = 4300000


def profile(db):
    data=shop._get(db, SETTING, dict(clears=0, last_run='', pending=0))
    data.setdefault('best_grade',0)
    data.setdefault('pending_v3',0)
    data.setdefault('second_pet_earned',False)
    data.setdefault('second_pet_pending',False)
    data.setdefault('maps',{})
    data.setdefault('hard_maps',{})
    return data


def prepare_run(state, db):
    """Freeze balance/grading/reward for a new battle, never change an old save."""
    from pve_balance import profile_for_room
    state['battle']['pve_balance'] = profile_for_room(state['room'])
    from pve_scoring import profile_for_run
    state['battle']['pve_scoring'] = profile_for_run(state)
    data = profile(db)
    state['battle']['pve_reward'] = dict(version=3,
        second_pet=state['room']['map_id']==201 and not data['second_pet_earned'])


def city_record(clears, best_grade=0, map_id=201, unlocked=True, *, hard_clears=0, hard_grade=0):
    # ENABLE/LEVEL/QUEST/PRE_CITY; COMMON is the permanent normal-clear bit.
    # HERO32 is the permanent hard-clear bit. Daily pass/expiry fields remain
    # zero: offline has no daily quota and does not fabricate TODAY64/128.
    flags = (15 if unlocked else 7) | (16 if clears else 0) | (32 if hard_clears else 0)
    return struct.pack('<IIBBII', CITIES[map_id], flags, best_grade, hard_grade, 0, 0)


def map_progress(data, map_id, *, difficulty=0):
    # Preserve old201 counters and pending rewards without a destructive migration.
    if difficulty:
        return data.get('hard_maps', {}).get(str(map_id), dict(clears=0, best_grade=0))
    return data if map_id==201 else data['maps'].get(str(map_id),dict(clears=0,best_grade=0))


def unlocked(data, map_id):
    try:
        normal, difficulty = resolve_request(map_id)
    except ValueError:
        return False
    if difficulty:
        # Complete this exact normal stage first. A hard win does not skip a
        # normal stage, retroactively grant clears, or unlock special/co-op maps.
        return map_progress(data, normal)['clears'] > 0
    return normal==201 or map_progress(data,MAPS[MAPS.index(normal)-1])['clears']>0


def city_body(data):
    return struct.pack('<H',len(MAPS))+b''.join(city_record(
        map_progress(data,m)['clears'],map_progress(data,m)['best_grade'],m,unlocked(data,m),
        hard_clears=map_progress(data,m,difficulty=1)['clears'] if m in NORMAL_TO_HARD else 0,
        hard_grade=map_progress(data,m,difficulty=1)['best_grade'] if m in NORMAL_TO_HARD else 0) for m in MAPS)


def city_packets(db):
    data=profile(db)
    return [(1303, city_body(data))]


def reward_packet(won, state=None):
    rule = (state or {}).get('battle', {}).get('pve_reward', {})
    items = REWARD_V3 if rule.get('version') == 3 else REWARD
    if rule.get('version') == 3 and rule.get('second_pet'):
        items += (SECOND_PET,)
    return award_packet(items if won else (), kind=1)


def deliver(db, progress):
    if not any(progress.get(k) for k in ('pending','pending_v3','second_pet_pending')): return []
    containers = inventory.load(db)
    before = deepcopy(containers)
    try:
        for reward, key in ((REWARD, 'pending'), (REWARD_V3, 'pending_v3')):
            for ident, count in Counter(reward).items():
                if progress.get(key, 0):
                    boosters._grant(containers, ident, count*progress[key])
        if progress.get('second_pet_pending'):
            boosters._grant(containers, SECOND_PET, 1, level=1)
    except ValueError:
        return [shop.tip(0, 'Túi nguyên liệu/trợ thủ đầy. Thưởng PvE đã lưu; dọn túi rồi đăng nhập lại để nhận.')]
    progress['pending'] = 0
    progress['pending_v3'] = 0
    progress['second_pet_pending'] = False
    inventory.save(containers, db)
    return bag_updates(before, containers, (3, 5))


def initialize():
    with storage.connection(write=True) as db:
        progress = profile(db)
        deliver(db, progress)
        shop._set(db, SETTING, progress)


def complete(db, state, arena):
    """Called inside save_world's transaction, before its durable snapshot."""
    if arena.geometry.id not in MAPS or not arena.pk['finished']:
        return []
    run = state['battle']['pve_run']
    if not isinstance(run, str) or not run or len(run) > 128:
        raise ValueError('Invalid persistent PvE run identity')
    if not db.in_transaction:
        raise RuntimeError('PvE settlement requires the world-save transaction')
    progress = profile(db)
    # A last-run pointer protects only A,A, not A,B,A. Keep an append-only,
    # profile-scoped receipt for each completed run in the existing settings
    # store. The receipt, pending reward, inventory and world snapshot commit
    # or roll back together; no schema rewrite or player-data replacement.
    receipt = f'pve_run_receipt:{storage.profile_id()}:{run}'
    inserted = db.execute('INSERT OR IGNORE INTO settings(name,value) VALUES(?,?)',
                          (receipt, '1')).rowcount
    if not inserted:
        return []
    if progress['last_run'] == run:
        # Backfill the current legacy receipt without awarding it again.
        return []
    progress['last_run'] = run
    won = arena.pk['winner'] == 1
    if won:
        from pve_rules import evaluation
        from pve_balance import frozen_profile
        balance = frozen_profile(arena)
        difficulty = balance['difficulty'] if balance is not None else 0
        current=map_progress(progress,arena.geometry.id,difficulty=difficulty)
        current['clears'] += 1
        if difficulty:
            progress['hard_maps'][str(arena.geometry.id)] = current
        elif arena.geometry.id!=201:
            progress['maps'][str(arena.geometry.id)]=current
        rule = state['battle'].get('pve_reward', {})
        progress['pending_v3' if rule.get('version') == 3 else 'pending'] += 1
        if arena.geometry.id==201 and rule.get('version') == 3 and rule.get('second_pet') and not progress['second_pet_earned']:
            progress['second_pet_earned'] = True
            progress['second_pet_pending'] = True
        current['best_grade'] = max(current['best_grade'],evaluation(arena)[0])
    packets = deliver(db, progress) if won else []
    if won:
        import match_rewards
        _, pve_rew_pkts = match_rewards.apply_pve_rewards(state, db, won)
        packets.extend(pve_rew_pkts)
    shop._set(db, SETTING, progress)
    packets.append((1304, city_body(progress)))
    return packets
