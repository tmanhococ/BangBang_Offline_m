"""Native crystal ownership, boards and crafting; no live-battle writes."""
from collections import Counter, defaultdict
from copy import deepcopy
from functools import lru_cache
import json
import random
import struct
import time
import uuid

import storage
import inventory
import cosmetic_shop as shop
from storage_import import tank_instance_payload

SETTING = 'crystal_profile'
PIECES = 8000024
# Native CMT values; both packages spend COIN_GIFT, as approved.
PACKAGES = {5: (0, 1, 10), 6: (0, 5, 40), 3: (1, 1, 20), 4: (1, 5, 90)}
CAPACITY = {'3': 135, '10': 100}  # Native ITEM_SLOT_DIVIDE, zero-based.


@lru_cache(maxsize=8)
def _catalog(database):
    items = {int(r['nItemId']): r for r in storage.template_rows('ItemTemplate_table.xml')}
    crystals = {i: r for i, r in items.items() if int(r.get('nType', 0)) == 56 and i % 10000 >= 100}
    if len(crystals) != 53:
        raise ValueError('Expected the approved 53 modern crystals')
    slots = {int(r['nPosId']): int(r['nCrystalType']) for r in storage.template_rows('CrystalSchemeTemplate_table.xml')}
    drops = defaultdict(list)
    for r in storage.template_rows('SecondDropTemplate_table.xml'):
        if r['nID'] not in ('301', '311', '321', '331'):
            continue
        group = int(r['nID'])
        ident, weight = int(r['nItemID']), int(r.get('iDropRate', 0))
        if weight <= 0:
            continue
        if ident not in crystals or int(r['iCountMin']) != 1 or int(r['iCountMax']) != 1:
            raise ValueError('Unexpected crystal drop')
        drops[group].append((ident, weight))
    if any(sum(w for _, w in drops[g]) != 10_000_000 for g in (301, 311, 321, 331)):
        raise ValueError('Crystal drop weights changed')
    attrs = defaultdict(list)
    for r in storage.template_rows('CrystalAttrTemplate_table.xml'):
        attrs[int(r['nAttrGroupId'])].append((int(r['nAttrType']), int(r['nValueType']), r['fAttrValue']))
    recipes = {}
    for r in storage.template_rows('ItemConvertTemplate_table.xml'):
        ntype = int(r.get('nType', 0))
        nid = int(r['nId'])
        if ntype == 29:
            new_id = int(r['nNewItemId'])
            if new_id in crystals and int(crystals[new_id]['iReuseD']) in (4, 5):
                recipes[nid] = r
        elif ntype in (2, 3):
            recipes[nid] = r
    return items, crystals, slots, drops, attrs, recipes


def catalog():
    return _catalog(str(storage.database_path()))


def initialize():
    catalog()
    with storage.connection(write=True) as db:
        if shop._get(db, SETTING) is None:
            shop._set(db, SETTING, {'boards': {str(i): {'name': '', 'items': [0] * 30} for i in (1, 2)},
                                    'rolls': [0, 0]})


def snapshot(db):
    profile = shop._get(db, SETTING)
    if profile is None:
        raise RuntimeError('Crystals have not been initialized')
    return profile


def pstr(text):
    raw = text.encode('utf-8')
    return struct.pack('<H', len(raw)) + raw


def board_packets(profile):
    boards = sorted(profile['boards'].items(), key=lambda pair: int(pair[0]))
    count = struct.pack('<H', len(boards))
    names = count + b''.join(struct.pack('<I', int(i)) + pstr(b['name']) for i, b in boards)
    details = count + b''.join(struct.pack('<31I', int(i), *b['items']) for i, b in boards)
    return [(1455, names), (1454, details)]


def limit_packet(profile):
    # Client adds one itself when describing the next guaranteed roll.
    return 1045, struct.pack('<HIiIi', 2, 284, profile['rolls'][0] % 5, 285, profile['rolls'][1] % 5)


def login_packets(db):
    profile = snapshot(db)
    return board_packets(profile) + [limit_packet(profile),
        (1100, inventory.list_payload(10, inventory.load(db).get('10', [])))]


def owned(containers):
    counts = Counter()
    for item in containers.get('10', []):
        counts[item['template_id']] += item['count']
    return counts


def validate_board(values, containers):
    _, crystals, slots, *_ = catalog()
    counts = Counter(i for i in values if i)
    for pos, ident in enumerate(values, 1):
        if ident and (ident not in crystals or int(crystals[ident]['iReuseA']) != slots[pos]):
            raise ValueError('Tinh thể không đúng loại ô.')
    if counts - owned(containers):
        raise ValueError('Không đủ tinh thể sở hữu để lưu bảng.')


def change_items(containers, ident, delta):
    template = catalog()[0][ident]
    container = str(template['nContType'])
    items = containers.setdefault(container, [])
    matches = [i for i in items if i['template_id'] == ident]
    if delta < 0:
        if sum(i['count'] for i in matches) < -delta:
            raise ValueError('Không đủ vật phẩm.')
        for item in matches:
            take = min(item['count'], -delta)
            item['count'] -= take
            delta += take
            if not item['count']:
                items.remove(item)
            if not delta:
                break
        return
    maximum = int(template['iStackMax'])
    for item in matches:
        take = min(maximum - item['count'], delta)
        item['count'] += take
        delta -= take
    occupied = {i['slot_no'] for i in items}
    while delta:
        slot = next((n for n in range(CAPACITY[container]) if n not in occupied), None)
        if slot is None:
            raise ValueError('Túi đã đầy.')
        take = min(maximum, delta)
        item = dict.fromkeys(inventory.FIELDS, 0)
        item.update(uuid='crystal-' + uuid.uuid4().hex, template_id=ident, count=take,
                    create_time=int(time.time()), slot_no=slot, ext_flag=1)
        items.append(item)
        occupied.add(slot)
        delta -= take


def bag_updates(before, after, containers):
    """Native delete/add events refresh open crystal windows as well as bags."""
    packets = []
    for container in sorted(containers):
        old = {i['uuid']: i for i in before.get(str(container), [])}
        new = {i['uuid']: i for i in after.get(str(container), [])}
        removed = [item for key, item in old.items() if key not in new]
        changed = [item for key, item in new.items() if old.get(key) != item]
        if removed:
            body = struct.pack('<IH', container, len(removed))
            body += b''.join(struct.pack('<h', i['slot_no']) for i in removed)
            body += struct.pack('<H', len(removed)) + b''.join(pstr(i['uuid']) for i in removed)
            packets.append((1103, body))
        if changed:
            packets.append((1100, inventory.list_payload(container, changed, notify_type=2)))
    return packets


def award_packet(ids, kind=19):
    # Craft animation draws one card per row and ignores m_count. Preserve each
    # roll, including duplicates, while exchange rewards may still be stacked.
    entries = [(ident, 1) for ident in ids] if kind == 19 else list(Counter(ids).items())
    # NotifySettleAward -> SettleAwardInfo -> one BATTLE_ST_BASE item vector.
    body = struct.pack('<I4iHIH', kind, 0, 0, 0, 0, 1, 0, len(entries))
    body += b''.join(struct.pack('<Ii', ident, count) for ident, count in entries)
    return 1104, body + struct.pack('<HiHBI', 0, 0, 0, 1, 0)


def craft(profile, containers, wallet, kind, rng):
    if kind not in PACKAGES:
        raise ValueError('Chỉ hỗ trợ hai gói chế tạo bằng xu khóa đã mở.')
    index, count, price = PACKAGES[kind]
    if wallet['currency'][2] < price:
        raise ValueError('Không đủ xu khóa.')
    drops = catalog()[3]
    ids = []
    for _ in range(count):
        profile['rolls'][index] += 1
        group = (301 if index == 0 else 321) + (10 if profile['rolls'][index] % 5 == 0 else 0)
        draw = rng.randrange(10_000_000)
        for ident, weight in drops[group]:
            if draw < weight:
                ids.append(ident)
                change_items(containers, ident, 1)
                break
            draw -= weight
        else:
            raise RuntimeError('Unreachable crystal draw')
    wallet['currency'][2] -= price
    return ids


def apply(state, mid, payload, db, *, rng=None):
    """Validate before committing wallet, items, counters and boards together."""
    try:
        if mid == 423:
            shop._end(payload, 0)
            return board_packets(snapshot(db)), None
        if mid == 560:
            kind, = struct.unpack_from('<I', payload)
            shop._end(payload, 4)
            if kind != 20:
                return [], None
            return [(1524, struct.pack('<IH', 20, 0))], None
        if state.get('battle'):
            raise ValueError('Hãy về sảnh hoặc phòng chọn tank để đổi tinh thể.')
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        profile, containers, wallet = snapshot(db), inventory.load(db), shop.snapshot(db)
        previous_items = deepcopy(containers)
        boards = profile['boards']
        packets, changed_containers = [], set()
        if mid == 424:
            values = struct.unpack_from('<31I', payload)
            shop._end(payload, 124)
            if str(values[0]) not in boards:
                raise ValueError('Bảng tinh thể chưa tồn tại.')
            validate_board(values[1:], containers)
            boards[str(values[0])]['items'] = list(values[1:])
            packets = board_packets(profile) + [(1456, struct.pack('<I', 0))]
        elif mid == 425:
            ident, size = struct.unpack_from('<IH', payload)
            name = payload[6:6 + size].decode('utf-8')
            shop._end(payload, 6 + size)
            if len(name) > 32 or any(ord(c) < 32 for c in name):
                raise ValueError('Tên bảng tinh thể không hợp lệ.')
            if str(ident) not in boards:
                if ident != len(boards) + 1 or ident > 255:
                    raise ValueError('Không thể thêm bảng tinh thể này.')
                boards[str(ident)] = {'name': name, 'items': [0] * 30}
            else:
                boards[str(ident)]['name'] = name
            packets = board_packets(profile) + [(1457, struct.pack('<I', 0))]
        elif mid == 427:
            ident, size = struct.unpack_from('<IH', payload)
            tank_uuid = payload[6:6 + size].decode('utf-8')
            shop._end(payload, 6 + size)
            if str(ident) not in boards:
                raise ValueError('Bảng tinh thể chưa tồn tại.')
            row = db.execute('SELECT data FROM garage WHERE profile_id=? AND uuid=?',
                             (storage.profile_id(), tank_uuid)).fetchone()
            if row is None:
                raise ValueError('Tank không thuộc sở hữu của bạn.')
            record = json.loads(row[0])
            record['crystal_scheme_id'] = ident
            db.execute('UPDATE garage SET data=? WHERE profile_id=? AND uuid=?',
                       (json.dumps(record, ensure_ascii=False), storage.profile_id(), tank_uuid))
            selected = (state.get('selected_tank_uuid') == tank_uuid or
                        not state.get('selected_tank_uuid') and state.get('tank_id') == record['tank_id'])
            visible = dict(record, ext_flag=(record['ext_flag'] & ~2) | (2 if selected else 0))
            packets = [(1151, tank_instance_payload(visible) + b'\x13')]
        elif mid == 430:
            kind, = struct.unpack_from('<I', payload)
            shop._end(payload, 4)
            ids = craft(profile, containers, wallet, kind, rng or random.SystemRandom())
            changed_containers.add(10)
            packets = [limit_packet(profile), award_packet(ids)]
            packets.insert(0, (1039, struct.pack('<HH6i', 2, 0, *wallet['currency'])))
        elif mid == 426:
            kind, count = struct.unpack_from('<IH', payload)
            if kind != 1 or not 0 < count <= 1000:
                raise ValueError('Yêu cầu bán tinh thể không hợp lệ.')
            ids = struct.unpack_from('<' + 'I' * count, payload, 6)
            shop._end(payload, 6 + 4 * count)
            reserved = Counter()
            for board in boards.values():
                reserved |= Counter(i for i in board['items'] if i)
            selling = Counter(ids)
            if selling - (owned(containers) - reserved) or any(i not in catalog()[1] for i in selling):
                raise ValueError('Tinh thể đang dùng trong bảng hoặc không đủ để bán.')
            pieces = 0
            for ident, amount in selling.items():
                change_items(containers, ident, -amount)
                pieces += 10 * int(catalog()[1][ident]['iReuseD']) * amount
            change_items(containers, PIECES, pieces)
            changed_containers.update((3, 10))
            packets = [(1459, struct.pack('<I', 0))]
        elif mid == 155:
            count, = struct.unpack_from('<H', payload)
            if not 0 < count <= 100:
                raise ValueError('Yêu cầu đổi tinh thể không hợp lệ.')
            requests = [struct.unpack_from('<II', payload, 2 + 8 * i) for i in range(count)]
            offset = 2 + 8 * count
            coins, = struct.unpack_from('<H', payload, offset)
            shop._end(payload, offset + 2)
            ids = []
            silver_fee = 0
            for ident, amount in requests:
                recipe = catalog()[5].get(ident)
                if recipe is None or not 0 < amount <= 999:
                    raise ValueError('Công thức đổi chưa được mở.')
                ntype = int(recipe.get('nType', 0))
                out = int(recipe['nNewItemId'])
                out_count = int(recipe.get('nNewItemCount') or 1) * amount
                if ntype == 29:
                    if coins:
                        raise ValueError('Đổi tinh thể chỉ dùng mảnh tinh thể.')
                    change_items(containers, PIECES, -int(recipe['iCount1']) * amount)
                    change_items(containers, out, out_count)
                    changed_containers.update((3, 10))
                elif ntype in (2, 3):
                    in_id = int(recipe['nItemId1'])
                    in_count = int(recipe['iCount1']) * amount
                    fee = int(recipe.get('iPrice') or 0) * amount
                    coin_type = int(recipe.get('nCoinType') or 2)
                    if coin_type == 2:  # Bạc
                        silver_fee += fee
                    change_items(containers, in_id, -in_count)
                    change_items(containers, out, out_count)
                    changed_containers.add(3)
                else:
                    raise ValueError('Loại công thức chuyển đổi chưa hỗ trợ.')
                ids.extend([out] * out_count)
            if silver_fee:
                if wallet['currency'][1] < silver_fee:
                    raise ValueError('Không đủ bạc để chuyển đổi.')
                wallet['currency'][1] -= silver_fee
            packets = [award_packet(ids, 8)]
            if silver_fee:
                packets.insert(0, (1039, struct.pack('<HH6i', 2, 0, *wallet['currency'])))
        else:
            raise ValueError('Thao tác tinh thể chưa hỗ trợ.')
        shop._set(db, SETTING, profile)
        shop._set(db, shop.PROFILE, wallet)
        inventory.save(containers, db)
        packets = bag_updates(previous_items, containers, changed_containers) + packets
        db.commit()
        return packets, None
    except (ValueError, UnicodeError, struct.error) as exc:
        db.rollback()
        message = str(exc) if type(exc) is ValueError else 'Yêu cầu tinh thể không hợp lệ.'
        packets = [shop.tip(mid, message)]
        if mid in (424, 425):
            packets += board_packets(snapshot(db)) + [(1456 if mid == 424 else 1457, struct.pack('<I', 1))]
        return packets, message
    except BaseException:
        db.rollback()
        raise


def battle_build(db, tank_uuid):
    """Snapshot once at battle entry, never read SQLite in combat ticks."""
    row = db.execute('SELECT data FROM garage WHERE profile_id=? AND uuid=?',
                     (storage.profile_id(), tank_uuid)).fetchone()
    if row is None:
        return []
    ident = json.loads(row[0]).get('crystal_scheme_id') or 1
    board = snapshot(db)['boards'].get(str(ident))
    if not board:
        return []
    validate_board(board['items'], inventory.load(db))
    _, crystals, _, _, attrs, _ = catalog()
    return [list(attr) for i in board['items'] if i for attr in attrs[int(crystals[i]['iReuseC'])]]
