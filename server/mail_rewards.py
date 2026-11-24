"""M29 mail rewards: explicit catalog, native limits, no implicit unlocks.

This module builds a *plan* on private copies. Only offline_mail.claim writes
that plan, in the authenticated GATE owner's SQLite transaction. Preview and
send never modify a wallet, bag, garage, entitlement or selected loadout.
"""
from __future__ import annotations

from copy import deepcopy
import json
import struct
import time
import uuid

import inventory
import skin_catalog_overrides
import storage
import cosmetic_shop
from display_locale import item_name as display_item_name
from storage_import import GARAGE_FIELDS, garage_payload, tank_instance_payload

MAX_INT = 0x7fffffff
MAX_ROWS = 16
CURRENCIES = {'cash': (0, 'Xu'), 'golden': (1, 'Bạc'), 'gift': (2, 'Xu khóa')}
CATEGORIES = [('currency', 'Tiền'), ('tank', 'Tank vĩnh viễn'),
              ('crystal', 'Tinh thể'), ('material', 'Nguyên liệu / thẻ'),
              ('helper', 'Trợ thủ'), ('avatar', 'Thời trang'),
              ('background', 'Trang trí'), ('halo', 'Vòng sáng'), ('skin', 'Skin')]
CAPACITIES = {3: 135, 4: 100, 5: 135, 6: 512, 7: 80, 9: 80, 10: 100}
# Recovered NotifyTankList.as: PLAYER_LOGIN_TANK=1, then
# TANK_CONTAINER_TYPE.as: TANK_CONT_OWN=0. A newly authored full snapshot
# must not inherit a captured notification type or its transport padding.
GARAGE_SNAPSHOT_TRAILER = bytes((1, 0))


class RewardError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details

    def envelope(self):
        value = dict(code=self.code, message=str(self))
        if self.details is not None:
            value['details'] = self.details
        return value


def _number(value, default=0):
    return int(value) if value not in (None, '') else default


def rows(db, filename):
    """Read shared static tables on the caller's connection/snapshot."""
    source = db.execute('SELECT sql_table FROM template_sources WHERE filename=?',
                        (filename,)).fetchone()
    if source is None:
        raise RewardError('catalog_missing', 'Thiếu bảng dữ liệu gốc: ' + filename)
    return [dict(row) for row in db.execute('SELECT * FROM ' + storage.quote(source[0])
                                           + ' ORDER BY _ordinal')]


def _setting(db, name, default):
    row = db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
    return json.loads(row[0]) if row else default


def _name(value, fallback):
    text = str(value or fallback).strip()
    # Source names are display-only, never markup or executable expressions.
    return text[:160]


def catalog(db):
    """Allow only types already consumed/rendered by the shipped runtime.

    Gift packs, VIP/rental cards, unknown autouse items, locked skins and
    incomplete halos never become grantable merely because a template exists.
    """
    from bot_setup import AI_TANKS
    from tank_halos import SUPPORTED as HALOS
    items = {_number(r['nItemId']): r for r in rows(db, 'ItemTemplate_table.xml')}
    items = skin_catalog_overrides.effective_items(db, items)
    levels = {(_number(r['nTankId']), _number(r['nLevel'])): r
              for r in rows(db, 'TankUpTemplate_table.xml')}
    pets = {_number(r['nId']): r for r in rows(db, 'BoosterTemplate_table.xml')}
    material_ids = {2600002, 2600003, 2600006, 2600007, 8000002, 8000024, 4000001}
    for filename in ('TankUpTemplate_table.xml', 'TankStrengthenTemplate_table.xml'):
        for row in rows(db, filename):
            if _number(row.get('nTankId')) not in AI_TANKS:
                continue
            for slot in range(1, 6):
                ident, amount = _number(row.get('nItemId' + str(slot))), _number(row.get('iItemCount' + str(slot)))
                if ident > 0 and amount > 0:
                    material_ids.add(ident)
    for row in rows(db, 'ConsumeLevelTemplate_table.xml'):
        if _number(row.get('nType')) in (1, 2) and _number(row.get('iItemCount')) > 0:
            material_ids.add(_number(row.get('nItemId')))
    cosmetics = _setting(db, cosmetic_shop.CATALOG, {})
    skin_support, _skin_effects = skin_catalog_overrides.load(db)
    result = {}
    for ident, (_, name) in CURRENCIES.items():
        key = 'currency:' + ident
        result[key] = dict(key=key, kind='currency', id=ident, category='currency',
                           name=name, max_quantity=2_000_000_000, unique=False)
    for ident, name in AI_TANKS.items():
        if ident in storage.DEFERRED_TANKS or (ident, 1) not in levels:
            continue
        key = 'tank:' + str(ident)
        result[key] = dict(key=key, kind='tank', id=ident, category='tank', name=name,
                           max_quantity=100, unique=False, level=1,
                           note='Tank vĩnh viễn LV1; không tự trang bị hoặc nâng cấp.')
    for ident, row in items.items():
        kind, cont = _number(row.get('nType')), _number(row.get('nContType'))
        maximum = _number(row.get('iStackMax'))
        if _number(row.get('nAutoUse')) or not 1 <= maximum <= MAX_INT:
            continue
        category, unique, level = None, False, 0
        if kind == 56 and cont == 10 and ident % 10000 >= 100 and 1 <= _number(row.get('iReuseD')) <= 5:
            category = 'crystal'
        elif cont in (2, 3) and (ident in material_ids or
                                kind == 37 and _number(row.get('iReuseA')) > 0 or
                                kind == 39 and _number(row.get('iReuseA')) > 0):
            category = 'material'
        elif kind == 36 and cont == 5 and maximum == 1 and _number(row.get('iReuseA')) in pets:
            category, level = 'helper', 1
        elif kind in (28, 30) and cont in (4, 7):
            ready = cosmetics.get(str(ident))
            if isinstance(ready, dict) and _number(ready.get('container')) == cont:
                category, unique = ('avatar' if cont == 4 else 'background'), True
        elif kind == 55 and cont == 9 and ident in HALOS:
            category, unique = 'halo', True
        elif kind == 29 and cont == 6:
            tank, external = _number(row.get('iReuseA')), _number(row.get('iReuseB'))
            if (tank in AI_TANKS and skin_support.get(str(tank), {}).get(str(external)) == 'ready'
                    and skin_catalog_overrides.item_matches(ident, tank, external)):
                category, unique = 'skin', True
        if not category:
            continue
        key = 'item:' + str(ident)
        result[key] = dict(key=key, kind='item', id=ident, category=category,
                           name=_name(display_item_name(ident, row.get('szName')), 'Vật phẩm ' + str(ident)),
                           max_quantity=1 if unique else 135 if category == 'helper' else 1_000_000,
                           unique=unique, level=level, container=cont, stack_max=maximum)
    return result


def normalize(rewards, available):
    if not isinstance(rewards, list) or not 1 <= len(rewards) <= MAX_ROWS:
        raise RewardError('rewards_invalid', 'Mỗi thư cần từ 1 đến 16 dòng quà.')
    result, seen = [], set()
    for reward in rewards:
        if not isinstance(reward, dict) or set(reward) != {'kind', 'id', 'quantity'}:
            raise RewardError('rewards_invalid', 'Dòng quà cần đúng kind, id, quantity.')
        kind, ident, quantity = reward['kind'], reward['id'], reward['quantity']
        if kind not in ('currency', 'tank', 'item') or (
                kind == 'currency' and (not isinstance(ident, str) or ident not in CURRENCIES)) or (
                kind != 'currency' and (type(ident) is not int or ident <= 0)):
            raise RewardError('rewards_invalid', 'Mã hoặc loại quà không hợp lệ.')
        key = kind + ':' + str(ident)
        row = available.get(key)
        if row is None:
            raise RewardError('reward_unsupported', 'Quà chưa được runtime hỗ trợ; không mở nội dung còn khóa.', {'key': key})
        if key in seen:
            raise RewardError('reward_duplicate', 'Gộp số lượng vào một dòng cho cùng một món quà.')
        if type(quantity) is not int or not 1 <= quantity <= row['max_quantity']:
            raise RewardError('quantity_invalid', 'Số lượng quà vượt giới hạn an toàn.', {'key': key, 'max_quantity': row['max_quantity']})
        result.append(dict(row, quantity=quantity))
        seen.add(key)
    return sorted(result, key=lambda reward: reward['key'])


def bare(rewards):
    return [dict(kind=r['kind'], id=r['id'], quantity=r['quantity']) for r in rewards]


def _new_item(row, count, slot):
    item = dict.fromkeys(inventory.FIELDS, 0)
    item.update(uuid='mail-item-' + uuid.uuid4().hex, template_id=row['id'],
                count=count, slot_no=slot, create_time=int(time.time()),
                ext_flag=1, level=row.get('level', 0))
    inventory.item_payload(item)  # Check the native ABI before any commit.
    return item


def _grant_item(containers, reward, capacities):
    cont, maximum, left = reward['container'], reward['stack_max'], reward['quantity']
    bag = containers.setdefault(str(cont), [])
    if not isinstance(bag, list):
        raise RewardError('bag_invalid', 'Dữ liệu túi không hợp lệ; quà vẫn còn trong thư.')
    slots = [item['slot_no'] for item in bag]
    if len(slots) != len(set(slots)) or any(type(s) is not int or not 0 <= s < capacities[cont] for s in slots):
        raise RewardError('bag_invalid', 'Ô túi hiện tại không hợp lệ; không ghi đè vật phẩm.')
    matches = [i for i in bag if i['template_id'] == reward['id'] and i['count'] > 0 and not i.get('life_time')]
    if reward['unique'] and matches:
        raise RewardError('already_owned', 'Đã sở hữu vĩnh viễn: ' + reward['name'] + '. Toàn bộ quà vẫn trong thư.', {'key': reward['key']})
    for item in matches:
        if item.get('level', 0) != reward.get('level', 0) or item.get('up_level', 0) or item.get('rank', 0):
            continue
        if type(item['count']) is not int or not 0 < item['count'] <= maximum:
            raise RewardError('stack_invalid', 'Số lượng trong túi vượt giới hạn gốc; chưa nhận quà.')
        take = min(left, maximum - item['count'])
        item['count'] += take
        left -= take
        if not left:
            break
    occupied = set(slots)
    while left:
        slot = next((s for s in range(capacities[cont]) if s not in occupied), None)
        if slot is None:
            raise RewardError('bag_full', 'Túi không đủ ô. Dọn túi rồi nhận lại; toàn bộ quà vẫn trong thư.', {'container': cont, 'key': reward['key']})
        take = min(left, maximum)
        bag.append(_new_item(reward, take, slot))
        occupied.add(slot)
        left -= take


def plan(db, profile_id, rewards, available=None):
    """Validate and simulate all rewards using the same SQLite snapshot."""
    from tank_skins import base_external
    available = catalog(db) if available is None else available
    rewards = normalize(bare(rewards), available)
    with storage.for_profile(profile_id):
        containers = inventory.load(db)
        wallet = cosmetic_shop.snapshot(db)
        if (not isinstance(wallet.get('currency'), list) or len(wallet['currency']) != 6 or
                any(type(n) is not int or not 0 <= n <= MAX_INT for n in wallet['currency'])):
            raise RewardError('wallet_invalid', 'Ví tiền chưa hợp lệ; chưa nhận quà.')
        original = deepcopy(containers)
        capacities = dict(CAPACITIES)
        capacities[2] = inventory.grocery_capacity(db)
        if not 1 <= capacities[2] <= 32768:
            raise RewardError('bag_invalid', 'Giới hạn túi hiện tại không hợp lệ.')
        saved = list(db.execute('SELECT ordinal,data FROM garage WHERE profile_id=? ORDER BY ordinal', (profile_id,)))
        garage = [json.loads(row['data']) for row in saved]
        owned = {int(r['tank_id']) for r in garage if int(r.get('cont_type', 0)) == 0}
        ordinal = max((int(row['ordinal']) for row in saved), default=-1) + 1
        new_tanks = []
        for reward in rewards:
            if reward['kind'] == 'currency':
                index = CURRENCIES[reward['id']][0]
                if wallet['currency'][index] > MAX_INT - reward['quantity']:
                    raise RewardError('wallet_overflow', 'Ví ' + reward['name'] + ' sẽ vượt giới hạn native; toàn bộ quà vẫn trong thư.')
                wallet['currency'][index] += reward['quantity']
            elif reward['kind'] == 'item':
                _grant_item(containers, reward, capacities)
            else:
                for _ in range(reward['quantity']):
                    tank = {field: '' if fmt == 's' else 0 for field, fmt in GARAGE_FIELDS}
                    tank.update(uuid='mail-tank-' + uuid.uuid4().hex, tank_id=reward['id'],
                                level=1, cont_type=0, own_time=int(time.time()))
                    tank['external_id'] = base_external(tank, db)
                    tank_instance_payload(tank)
                    new_tanks.append((ordinal, tank))
                    garage.append(tank)
                    owned.add(reward['id'])
                    ordinal += 1
        return dict(rewards=rewards, before=original, containers=containers, wallet=wallet,
                    new_tanks=new_tanks, garage=garage)


def commit_plan(db, state, value):
    """Caller owns BEGIN IMMEDIATE; do not commit here or change session state."""
    from crystals import bag_updates
    if not db.in_transaction:
        raise RuntimeError('Mail rewards require an existing transaction')
    profile_id = storage.profile_id()
    inventory.save(value['containers'], db)
    cosmetic_shop._set(db, cosmetic_shop.PROFILE, value['wallet'])
    for ordinal, tank in value['new_tanks']:
        db.execute('INSERT INTO garage(profile_id,ordinal,uuid,tank_id,data) VALUES(?,?,?,?,?)',
                   (profile_id, ordinal, tank['uuid'], tank['tank_id'],
                    json.dumps(tank, ensure_ascii=False, separators=(',', ':'))))
    packets = [(1039, struct.pack('<HH6i', 2, 0, *value['wallet']['currency']))]
    packets += bag_updates(value['before'], value['containers'],
                           {int(c) for c in set(value['before']) | set(value['containers'])})
    if value['new_tanks']:
        # Full native S1150 snapshot. Both Flash ItemManager and HTML5's reducer
        # require notify=PLAYER_LOGIN_TANK/cont=OWN to replace the complete
        # garage; notify=0 is invalid and silently loses the UI update.
        selected = state.get('selected_tank_uuid')
        visible = []
        for record in value['garage']:
            active = record['uuid'] == selected if selected else record['tank_id'] == state.get('tank_id')
            visible.append(dict(record, ext_flag=(record['ext_flag'] & ~2) | (2 if active else 0)))
        # NotifyTankList.writeToStream ends after these two native bytes.
        # Optional padding in the old capture belongs to that transport frame,
        # not this freshly length-framed packet; no suffix is synthesized.
        packets.append((1150, garage_payload(visible, GARAGE_SNAPSHOT_TRAILER)))
    # S1100/1150 are u16 framed; no wallet is consumed if a native payload is too large.
    if any(len(body) + 8 > 65535 for _, body in packets):
        raise RewardError('packet_limit', 'Dữ liệu đồng bộ vượt giới hạn native; toàn bộ quà vẫn trong thư.')
    return packets
