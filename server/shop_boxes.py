"""M45 explicitly reviewed boxes; no native Drop table is inferred.

The user authorized 10% main prize / 90% common materials. Each conditional
pool is uniform. Planning every outcome before the draw prevents bag capacity
or an already-owned permanent tank from becoming a free rare-prize reroll.
Only normal_shop calls this module inside its authenticated transaction.
"""
from __future__ import annotations

from copy import deepcopy
import secrets

import economy_actions
import mail_rewards
import storage
import inventory
from mail_rewards import RewardError


def require_usable_main(box, containers, garage, available):
    """Do not sell an egg whose permanent main prize is already owned.

    Purchase needs space for the egg only. Reward space is checked separately
    when opening; this guard concerns an irreversible ownership conflict.
    """
    if box['mode'] != 'random':
        return
    for reward in mail_rewards.normalize(box['main_rewards'], available):
        if not reward['unique'] or reward['kind'] == 'tank':
            continue
        if reward['kind'] == 'item':
            owned = any(row['template_id'] == reward['id'] and row['count'] > 0 and not row.get('life_time')
                        for row in containers.get(str(reward['container']), []))
        else:
            owned = False
        if owned:
            raise RewardError('already_owned',
                              'Bạn đã sở hữu phần thưởng vĩnh viễn trong hộp. Không thể mua thêm hộp này; '
                              'chưa trừ tiền. Không quay lại hoặc đổi phần thưởng trùng.', {'key': reward['key']})


def outcomes(box):
    """Return all possible rewards, without choosing or consuming randomness."""
    if box['mode'] == 'fixed':
        return [('fixed', deepcopy(box['fixed_rewards']))]
    if box['mode'] != 'random' or box.get('main_percent') != 10 or box.get('material_percent') != 90:
        raise RewardError('box_config', 'Hộp chưa có cấu hình phần thưởng đã kiểm tra.')
    return ([('main', [deepcopy(row)]) for row in box['main_rewards']]
            + [('material', [deepcopy(row)]) for row in box['material_rewards']])


def plans(db, box, ref, available, quantity=1):
    """Read-only plans for every allowed branch; preserve the box on any error.

    Current reviewed eggs award tanks/helpers/materials and the sole fixed
    present awards Bạc. None awards a grocery item, so planning the grant before
    removing the grocery box has exactly the same capacity as consume-first.
    Refuse any future catalog expansion that invalidates that invariant.
    """
    if quantity > 1:
        sim = draw_bulk(db, box, ref, available, quantity)
        return [sim]
    values = []
    for outcome, rewards in outcomes(box):
        for row in rewards:
            descriptor = available.get(row['kind'] + ':' + str(row['id']), {})
            if row['kind'] == 'item' and descriptor.get('container') == ref['container']:
                raise RewardError('box_config', 'Hộp có phần thưởng cùng ngăn chưa được kiểm tra; chưa mở hộp.')
        try:
            value = mail_rewards.plan(db, storage.profile_id(), rewards, available)
        except RewardError as exc:
            if exc.code == 'already_owned':
                raise RewardError('already_owned',
                                  'Bạn đã sở hữu phần thưởng vĩnh viễn trong hộp. Hộp được giữ nguyên; '
                                  'không quay lại hoặc đổi thành vật phẩm khác.', exc.details) from exc
            if exc.code == 'bag_full':
                raise RewardError('bag_full',
                                  'Cần đủ chỗ cho mọi phần thưởng có thể nhận. Dọn túi rồi mở lại; '
                                  'hộp được giữ nguyên.', exc.details) from exc
            if exc.code == 'wallet_overflow':
                raise RewardError('wallet_overflow', 'Ví sẽ vượt giới hạn; hộp được giữ nguyên.') from exc
            raise
        item = economy_actions._lookup(value['containers'], ref)
        economy_actions._protection(item)
        item['count'] -= 1
        if item['count'] == 0:
            value['containers'][str(ref['container'])].remove(item)
        values.append(dict(outcome=outcome, value=value))
    if not values:
        raise RewardError('box_config', 'Hộp chưa có phần thưởng hợp lệ.')
    return values


def select(values):
    """Only call after proof and all branch preflights, immediately before save."""
    if len(values) == 1 and values[0]['outcome'] == 'fixed':
        return values[0]
    outcome = 'main' if secrets.randbelow(100) < 10 else 'material'
    pool = [value for value in values if value['outcome'] == outcome]
    if not pool:
        raise RewardError('box_config', 'Nhánh phần thưởng chưa có cấu hình hợp lệ.')
    return pool[0] if len(pool) == 1 else pool[secrets.randbelow(len(pool))]


def draw_bulk(db, box, ref, available, quantity):
    import json
    if quantity <= 1:
        raise ValueError('draw_bulk requires quantity > 1')
    profile_id = storage.profile_id()
    saved = list(db.execute('SELECT ordinal,data FROM garage WHERE profile_id=? ORDER BY ordinal', (profile_id,)))
    garage = [json.loads(row[1]) for row in saved]
    containers = inventory.load(db)
    item = economy_actions._lookup(containers, ref)
    economy_actions._protection(item)
    if item['count'] < quantity:
        raise RewardError('quantity_invalid', 'Số lượng hộp trong túi không đủ.')

    all_rewards = []
    outcomes_list = []
    for _ in range(quantity):
        if box['mode'] == 'fixed':
            outcomes_list.append('fixed')
            all_rewards.extend(deepcopy(box['fixed_rewards']))
        else:
            is_main = secrets.randbelow(100) < 10
            chosen_rewards = None
            if is_main and box.get('main_rewards'):
                candidate = box['main_rewards'][0]
                if candidate['kind'] == 'item':
                    owned = any(int(row.get('template_id', 0)) == candidate['id'] and row.get('count', 0) > 0 and not row.get('life_time')
                                for row in containers.get(str(candidate['container']), []))
                else:
                    owned = False
                if not owned or candidate['kind'] == 'tank':
                    chosen_rewards = [deepcopy(candidate)]
                    outcomes_list.append('main')
                    if candidate['kind'] == 'tank':
                        garage.append({'tank_id': candidate['id'], 'cont_type': 0})
            if chosen_rewards is None:
                pool = box.get('material_rewards', [])
                pick = pool[secrets.randbelow(len(pool))] if pool else None
                if pick:
                    chosen_rewards = [deepcopy(pick)]
                else:
                    chosen_rewards = []
                outcomes_list.append('material')
            all_rewards.extend(chosen_rewards)

    merged = {}
    for r in all_rewards:
        key = (r['kind'], r['id'])
        if key in merged:
            merged[key]['quantity'] += r['quantity']
        else:
            merged[key] = deepcopy(r)
    combined_rewards = list(merged.values())

    value = mail_rewards.plan(db, profile_id, combined_rewards, available)
    item_in_plan = economy_actions._lookup(value['containers'], ref)
    item_in_plan['count'] -= quantity
    if item_in_plan['count'] == 0:
        value['containers'][str(ref['container'])].remove(item_in_plan)

    outcome_str = 'mixed' if len(set(outcomes_list)) > 1 else outcomes_list[0] if outcomes_list else 'fixed'
    return dict(outcome=outcome_str, value=value, all_rewards=all_rewards)

