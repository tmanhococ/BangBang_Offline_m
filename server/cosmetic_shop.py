"""Native avatar/background shop. One permanent item for 100 silver, SQLite atomicity."""
from __future__ import annotations

import json
import struct
import time
import uuid
import xml.etree.ElementTree as ET

import inventory
import storage

CATALOG = 'cosmetic_shop_catalog'
PROFILE = 'cosmetic_shop_profile'
PRICE = 100
SILVER = 2  # COIN_GOLDEN: CurrencyTool labels this currency silver.
GOODS_BASE = 70_000_000
CAPACITY = {4: 100, 7: 80}


def _get(db, key, default=None):
    row = db.execute('SELECT value FROM settings WHERE name=?', (storage.setting_key(key),)).fetchone()
    return json.loads(row[0]) if row else default


def _set(db, key, value):
    db.execute('INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
               (storage.setting_key(key), json.dumps(value, ensure_ascii=False, separators=(',', ':'))))


def currency_offset(body):
    # CHAR_BASIC: 126 fixed bytes followed by name and character UUID (UTF).
    pos = 126
    for _ in range(2):
        size, = struct.unpack_from('<H', body, pos)
        pos += 2 + size
    if pos + 24 > len(body):
        raise ValueError('Truncated native character data')
    return pos


def initialize():
    inventory.initialize()
    with storage.connection(write=True) as db:
        if _get(db, PROFILE) is not None:
            return
        frame = next(bytes(r[0]) for r in db.execute('SELECT frame FROM lobby_frames ORDER BY ordinal')
                     if struct.unpack_from('<H', r[0], 2)[0] == 1016)
        body = frame[8:]
        avatar, decorate = struct.unpack_from('<II', body, 52)
        _set(db, PROFILE, dict(avatar=avatar, decorate=decorate,
                              currency=list(struct.unpack_from('<6i', body, currency_offset(body)))))


def snapshot(db=None):
    if db is None:
        with storage.connection() as opened:
            return snapshot(opened)
    profile = _get(db, PROFILE)
    if profile is None:
        raise RuntimeError('Cosmetic shop has not been initialized')
    return profile


def login_payload(mid, body, profile):
    if mid == 1023:
        return struct.pack('<I', profile['avatar'])
    if mid == 1016:
        data = bytearray(body)
        struct.pack_into('<II', data, 52, profile['avatar'], profile['decorate'])
        struct.pack_into('<6i', data, currency_offset(data), *profile['currency'])
        return bytes(data)
    return body


def shop_query(qs):
    """Zero-based paging, native first/second labels; supports cosmetic and normal shop offers."""
    try:
        gt, st, page, size = (int(qs.get(k, [str(d)])[0]) for k, d in
                              (('gt', 1), ('st', 1), ('p', 0), ('r', 12)))
    except (ValueError, TypeError, IndexError):
        gt, st, page, size = 1, 1, 0, 12
    size, page = max(1, min(size, 100)), max(0, page)
    if gt in (3, 9):
        with storage.connection() as db:
            catalog = _get(db, CATALOG, {})
            rows = [r for r in catalog.values() if r['label'] == gt]
        rows.sort(key=lambda r: r['id'])
        xml = ET.Element('query', amount=str(len(rows)), cur_time=str(int(time.time())))
        for row in rows[page * size:(page + 1) * size]:
            values = dict(goods_id=GOODS_BASE + row['id'], template_oid=row['id'],
                          slot_no=0, first_label=gt, second_label=1,
                          goods_type=3 if gt == 3 else 8, coin_type=SILVER,
                          price1=PRICE, price2=0, price3=0, count_1=0, count_2=0, count_3=0,
                          coin_type2=0, is_agio=0, is_new=0, is_hot=0, is_limit_time=0,
                          is_limit_count=0, limit_level=0, vip_level=0, can_use_gift=0)
            ET.SubElement(xml, 'row', {k: str(v) for k, v in values.items()})
        return ET.tostring(xml, encoding='utf-8', xml_declaration=True)

    import normal_shop
    try:
        cat = normal_shop.catalog()
        offers = cat.get('offers', [])
    except Exception:
        offers = []

    if gt == 1:
        seen = set()
        matching = [o for o in offers if (o.get('tab') == 1 or o.get('is_hot') or o.get('is_new')) and not (o['goodsId'] in seen or seen.add(o['goodsId']))]
    elif gt == 2:
        seen = set()
        matching = [o for o in offers if (o.get('grantKind') == 'tank' or o.get('tab') == 2) and not (o['goodsId'] in seen or seen.add(o['goodsId']))]
    elif gt == 6:
        seen = set()
        matching = [o for o in offers if (o.get('grantKind') == 'item' or o.get('tab') in (1, 4, 6)) and o.get('grantKind') != 'tank' and not (o['goodsId'] in seen or seen.add(o['goodsId']))]
    elif gt == 7:
        matching = [o for o in offers if o.get('tab') == 5]
    elif gt == 8:
        matching = [o for o in offers if o.get('tab') == 6]
    else:
        matching = [o for o in offers if o.get('tab') == gt]
    if st == 3:
        filtered = [o for o in matching if any(p.get('coinType') == 3 for p in o.get('payments', [])) or o.get('canUseGift')]
        if filtered:
            matching = filtered

    xml = ET.Element('query', amount=str(len(matching)), cur_time=str(int(time.time())))
    for offer in matching[page * size:(page + 1) * size]:
        p1 = offer.get('payments', [{}])[0]
        p2 = offer.get('payments', [{}])[1] if len(offer.get('payments', [])) > 1 else {}
        is_tank = offer.get('grantKind') == 'tank'
        values = dict(
            goods_id=offer['goodsId'],
            template_oid=offer['itemId'],
            slot_no=0,
            first_label=gt,
            second_label=st,
            goods_type=1 if is_tank else 6,
            coin_type=p1.get('coinType', 2),
            price1=p1.get('price', 0),
            price2=0,
            price3=0,
            count_1=0,
            count_2=0,
            count_3=0,
            coin_type2=p2.get('coinType', 0),
            price2_1=p2.get('price', 0),
            price2_2=0,
            price2_3=0,
            token_itemid=0,
            is_new=1 if offer.get('is_new') else 0,
            is_hot=1 if offer.get('is_hot') else 0,
            is_agio=0,
            is_limit_time=0,
            is_limit_count=0,
            agio_price1=0,
            agio_price2=0,
            agio_price3=0,
            agio_price2_1=0,
            agio_price2_2=0,
            agio_price2_3=0,
            limit_level=0,
            vip_level=0,
            can_use_gift=1 if offer.get('canUseGift') or p1.get('coinType') == 3 else 0,
            goods_uuid=f"offline-goods-{offer['goodsId']}",
            goods_sort=0,
            space_count=0,
            salesum=0,
            char_count=""
        )
        ET.SubElement(xml, 'row', {k: str(v) for k, v in values.items()})
    return ET.tostring(xml, encoding='utf-8', xml_declaration=True)


def tip(mid, message):
    raw = message.encode('utf-8')
    return 1501, struct.pack('<IiHH', mid, 3997, 1, len(raw)) + raw


def _end(payload, end):
    if len(payload) - end not in (0, (-(6 + end)) % 8) or any(payload[end:]):
        raise ValueError('Yêu cầu shop không hợp lệ.')


def _buy(payload):
    kind, goods, coin, price, count, n = struct.unpack_from('<iIIiiH', payload)
    if n > 6:
        raise ValueError('Yêu cầu mua không hợp lệ.')
    coins = {}
    for i in range(n):
        key, val = struct.unpack_from('<Ii', payload, 22 + 8 * i)
        if key in coins or key not in range(1, 7) or val < 0:
            raise ValueError('Yêu cầu thanh toán không hợp lệ.')
        coins[key] = val
    _end(payload, 22 + n * 8)
    return dict(goods=goods, coin=coin, price=price, count=max(1, count), coins=coins)


def _use(payload, state):
    container, slot, size = struct.unpack_from('<IhH', payload)
    if size > 200:
        raise ValueError('Yêu cầu sử dụng không hợp lệ.')
    target = payload[8:8 + size].decode('utf-8')
    count, auto = struct.unpack_from('<iB', payload, 8 + size)
    _end(payload, 13 + size)
    if (container not in CAPACITY or slot < (-1 if container == 7 else 0) or count not in (0, 1) or auto
            or target not in ('', state['char_uuid'])):
        raise ValueError('Chỉ hỗ trợ chọn avatar/nền đã sở hữu cho nhân vật của bạn.')
    return container, slot


def apply(state, mid, payload, db):
    """Acknowledge only after wallet, ownership and equipped selection commit."""
    try:
        if state.get('battle'):
            raise ValueError('Hãy về sảnh hoặc phòng chờ để mua/chọn avatar và nền.')
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        profile, containers = snapshot(db), inventory.load(db)
        catalog = _get(db, CATALOG, {})
        if mid == 250:
            buy_info = _buy(payload)
            goods_id = buy_info['goods']
            coin = buy_info['coin']
            price = buy_info['price']
            count = buy_info['count']

            import normal_shop
            cat = normal_shop.catalog()
            offers_by_id = {o['goodsId']: o for o in cat.get('offers', [])}

            if goods_id in offers_by_id:
                offer = offers_by_id[goods_id]
                total_cost = price * count
                coin_idx = 0 if coin == 1 else 1 if coin == 2 else 2
                currency_names = {1: 'Xu', 2: 'Bạc', 3: 'Xu khóa'}
                curr_name = currency_names.get(coin, 'tiền')

                if profile['currency'][coin_idx] < total_cost:
                    raise ValueError(f'Bạn không đủ {curr_name}.')
                profile['currency'][coin_idx] -= total_cost

                char_id = state['char_id']
                if offer.get('grantKind') == 'tank':
                    tank_id = offer['itemId']
                    owned = db.execute('SELECT 1 FROM garage WHERE profile_id=? AND tank_id=?',
                                       (char_id, tank_id)).fetchone()
                    if owned:
                        raise ValueError(f"Bạn đã sở hữu tank {offer['name']} rồi.")

                    from combat_data import table_row
                    ext_row = table_row('TankUpTemplate_table.xml', nTankId=tank_id, nLevel=1)
                    ext_id = int(ext_row['nExternal']) if ext_row else 0
                    now_ts = int(time.time())
                    tank_uuid = f"char-{char_id}-tank-{tank_id}"
                    tank_data = {
                        'uuid': tank_uuid, 'tank_id': tank_id, 'level': 1, 'n_exp': 0, 'ext_flag': 0,
                        'stth_level': 0, 'external_id': ext_id, 'war_count': 0, 'week_war_count': 0,
                        'cont_type': 0, 'cash_hire': 0, 'coin_hire': 0, 'exist_time': 0, 'halo_id': 0,
                        'skin_frame_type': 0, 'own_time': now_ts, 'stth_count': 0, 'com_skill_id': 0,
                        'crystal_scheme_id': 0, 'tal_page_uuid': '', 'prof_val': 0, 'battle_gains': 0
                    }
                    max_ord = db.execute('SELECT MAX(ordinal) FROM garage WHERE profile_id=?', (char_id,)).fetchone()[0]
                    next_ord = (max_ord + 1) if max_ord is not None else 0
                    db.execute('INSERT INTO garage VALUES (?,?,?,?,?)',
                               (char_id, next_ord, tank_uuid, tank_id, json.dumps(tank_data)))
                    _set(db, PROFILE, profile)
                    db.commit()

                    from storage_import import garage_payload
                    records = [json.loads(r[0]) for r in db.execute('SELECT data FROM garage WHERE profile_id=? ORDER BY ordinal', (char_id,))]
                    try:
                        trailer = bytes.fromhex(json.loads(storage.setting('garage_trailer')))
                    except Exception:
                        trailer = b'\x01\x00\x00\x00'
                    garage_pkt = (1150, garage_payload(records, trailer))
                    wallet_pkt = (1039, struct.pack('<HH6i', 2, 0, *profile['currency']))
                    tip_pkt = tip(mid, f"Mua thành công tank {offer['name']}!")
                    return [wallet_pkt, garage_pkt, tip_pkt], None
                else:
                    item_id = offer['itemId']
                    cont = offer.get('containerType', 2)
                    items = containers.setdefault(str(cont), [])
                    existing = next((i for i in items if i['template_id'] == item_id), None)
                    if existing:
                        existing['count'] += count
                        item_rec = existing
                    else:
                        occupied = {i['slot_no'] for i in items}
                        slot = next((s for s in range(100) if s not in occupied), 0)
                        item_rec = dict.fromkeys(inventory.FIELDS, 0)
                        item_rec.update(uuid='item-' + uuid.uuid4().hex, template_id=item_id, count=count,
                                        create_time=int(time.time()), slot_no=slot)
                        items.append(item_rec)
                    inventory.save(containers, db)
                    _set(db, PROFILE, profile)
                    db.commit()
                    item_pkt = (1100, inventory.list_payload(cont, [item_rec], notify_type=2))
                    wallet_pkt = (1039, struct.pack('<HH6i', 2, 0, *profile['currency']))
                    tip_pkt = tip(mid, f"Mua thành công {offer['name']} x{count}!")
                    return [wallet_pkt, item_pkt, tip_pkt], None

            request = goods_id - GOODS_BASE
            row = catalog.get(str(request))
            if not row:
                raise ValueError('Món này chưa đủ tài nguyên để mở bán.')
            container = row['container']
            items = containers.setdefault(str(container), [])
            if any(i['template_id'] == request and i['count'] > 0 for i in items):
                raise ValueError('Bạn đã sở hữu món này vĩnh viễn, không cần mua lại.')
            if profile['currency'][1] < PRICE:
                raise ValueError('Bạn không đủ 100 bạc.')
            occupied = {i['slot_no'] for i in items}
            slot = next((s for s in range(CAPACITY[container]) if s not in occupied), None)
            if slot is None:
                raise ValueError('Túi avatar/nền đã đầy.')
            item = dict.fromkeys(inventory.FIELDS, 0)
            item.update(uuid='cosmetic-' + uuid.uuid4().hex, template_id=request, count=1,
                        create_time=int(time.time()), slot_no=slot)
            items.append(item)
            profile['currency'][1] -= PRICE
            packets = [(1039, struct.pack('<HH6i', 2, 0, *profile['currency'])),
                       (1100, inventory.list_payload(container, [item], notify_type=2))]
            packets.append(tip(mid, 'Đã mua vĩnh viễn. Vào Túi đồ để chọn sử dụng.'))
        else:
            request = _use(payload, state)
            container, slot = request
            items = containers.get(str(container), [])
            item = next((i for i in items if i['slot_no'] == slot and i['count'] > 0), None)
            removing = container == 7 and slot == -1
            if not removing and (item is None or item.get('life_time', 0)):
                raise ValueError('Không tìm thấy avatar/nền vĩnh viễn trong túi.')
            row = catalog.get(str(item['template_id'])) if item else None
            if not removing and (not row or row['container'] != container):
                raise ValueError('Món này chưa đủ tài nguyên để sử dụng.')
            key = 'avatar' if container == 4 else 'decorate'
            profile[key] = 0 if removing else item['template_id']
            for other in items:
                other['ext_flag'] = (other['ext_flag'] & ~32) | (32 if other is item else 0)
            packets = [(1100, inventory.list_payload(container, items)),
                       (1023, struct.pack('<I', profile[key])) if container == 4 else
                       (85, struct.pack('<BIH', 1, profile[key], 0))]
        inventory.save(containers, db)
        _set(db, PROFILE, profile)
        db.commit()
        state.update(avatar=profile['avatar'], decorate_id=profile['decorate'])
        return packets, None
    except (ValueError, UnicodeError, struct.error) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else 'Yêu cầu shop không hợp lệ.'
        return [tip(mid, message)], message
    except BaseException:
        db.rollback()
        raise
