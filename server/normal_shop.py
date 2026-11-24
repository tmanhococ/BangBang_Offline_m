"""M45 source-priced shop-v1; legacy cosmetic/Guild C250 stays unchanged.

The checked-in catalog is the only offer and box allowlist. An authenticated
GATE owns preview/proof/apply/status; price, quantity, wallet and reward pools
never come from the client. A profile-scoped receipt commits together with the
debit/consume and native inventory/garage grant. HTTP only exposes static data.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import struct
import time

import cosmetic_shop
import economy_actions
import inventory
import mail_rewards
import shop_boxes
import storage
from crystals import bag_updates
from mail_rewards import RewardError
from storage_import import garage_payload

REQUEST, REPLY, VERSION = 60008, 60009, 1
MAX_INT = 0x7fffffff
MAX_QUANTITY, MAX_REQUEST_BYTES, MAX_REPLY_BYTES, PREVIEW_SECONDS = 99, 32768, 60000, 120
CATALOG_PATH = Path(__file__).with_name('normal_shop_catalog.json')
COINS = {1: ('cash', 0), 2: ('golden', 1), 3: ('gift', 2)}
_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_HASH = re.compile(r'^[0-9a-f]{64}$')

text, digest, session = economy_actions.text, economy_actions.digest, economy_actions.session


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _ident(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def canonical(value):
    if not isinstance(value, dict):
        raise RewardError('intent_invalid', 'Thao tác shop không hợp lệ.')
    if value.get('kind') == 'buy':
        if (set(value) != {'kind', 'goods_id', 'payment', 'quantity'}
                or not _integer(value['goods_id'], 1, 0xffffffff)
                or value['payment'] not in ('primary', 'alternate')
                or not _integer(value['quantity'], 1, MAX_QUANTITY)):
            raise RewardError('intent_invalid', 'Mã hàng, cách trả hoặc số lượng mua không hợp lệ.')
        return dict(kind='buy', goods_id=value['goods_id'], payment=value['payment'], quantity=value['quantity'])
    if value.get('kind') == 'open_box':
        if not (set(value) == {'kind', 'item'} or set(value) == {'kind', 'item', 'quantity'}):
            raise RewardError('intent_invalid', 'Thao tác mở hộp chứa trường ngoài hợp đồng.')
        ref = economy_actions._ref(value['item'])
        if ref['container'] != 2:
            raise RewardError('container_blocked', 'Chỉ mở hộp được hỗ trợ trong ngăn Vật phẩm.')
        quantity = value.get('quantity', 1)
        if not _integer(quantity, 1, 100):
            raise RewardError('quantity_invalid', 'Số lượng mở hộp phải từ 1 đến 100.')
        return dict(kind='open_box', item=ref, quantity=quantity)
    raise RewardError('intent_invalid', 'Thao tác shop chưa được hỗ trợ.')


def decode_request(body):
    if len(body) < 2 or len(body) > MAX_REQUEST_BYTES + 2 or struct.unpack_from('<H', body)[0] != len(body) - 2:
        raise RewardError('request_invalid', 'Chiều dài gói shop không hợp lệ.')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(body[2:].decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RewardError('request_invalid', 'JSON shop không hợp lệ.') from exc
    base = {'v', 'request_id', 'operation_id', 'action'}
    if (not isinstance(value, dict) or type(value.get('v')) is not int or value['v'] != VERSION
            or not _ident(value.get('request_id')) or not _ident(value.get('operation_id'))):
        raise RewardError('request_invalid', 'Phiên bản hoặc mã yêu cầu shop không hợp lệ.')
    action = value.get('action')
    if action == 'status':
        if set(value) != base:
            raise RewardError('request_invalid', 'Gói kiểm tra kết quả chứa trường ngoài hợp đồng.')
    elif action in ('preview', 'apply'):
        if set(value) != base | {'intent'} | ({'proof'} if action == 'apply' else set()):
            raise RewardError('request_invalid', 'Gói shop chứa trường ngoài hợp đồng.')
        value['intent'] = canonical(value['intent'])
        if action == 'apply':
            proof = value['proof']
            if (not isinstance(proof, dict) or set(proof) != {'generation', 'revision', 'expires', 'token'}
                    or not isinstance(proof['generation'], str) or not re.fullmatch(r'[0-9a-f]{32}', proof['generation'])
                    or not _integer(proof['expires'], 1, 0x7fffffffffffffff)
                    or any(not isinstance(proof[key], str) or not _HASH.fullmatch(proof[key]) for key in ('revision', 'token'))):
                raise RewardError('request_invalid', 'Xác nhận mua/mở hộp không hợp lệ.')
    else:
        raise RewardError('request_invalid', 'Thao tác shop chưa được hỗ trợ.')
    return value


def packet(value):
    raw = text(dict(v=VERSION, **value)).encode('utf-8')
    if len(raw) > MAX_REPLY_BYTES:
        raise RewardError('packet_limit', 'Kết quả vượt giới hạn gói tin; chưa thay đổi tiền hoặc vật phẩm.')
    return REPLY, struct.pack('<H', len(raw)) + raw


@lru_cache(maxsize=1)
def catalog():
    value = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    if (value.get('v') != VERSION or not isinstance(value.get('revision'), str)
            or not _HASH.fullmatch(value['revision'])
            or value['revision'] != digest({k: v for k, v in value.items() if k != 'revision'})):
        raise RewardError('catalog_invalid', 'Danh mục shop chưa khớp bản dữ liệu đã kiểm tra.')
    rewards, boxes, offers = value['rewards'], value['boxes'], value['offers']
    if not isinstance(rewards, dict) or not isinstance(boxes, dict) or not isinstance(offers, list):
        raise RewardError('catalog_invalid', 'Cấu trúc danh mục shop không hợp lệ.')
    seen = set()
    for offer in offers:
        ident = offer['goodsId']
        kind = 'tank' if offer['grantKind'] == 'tank' else 'item'
        descriptor = rewards.get(kind + ':' + str(offer['itemId']))
        if (not _integer(ident, 1, 0xffffffff) or ident in seen or offer['tab'] not in (1, 2, 4, 5, 6, 7)
                or not descriptor or offer.get('available') is not True
                or not _integer(offer.get('maxCount'), 1, MAX_QUANTITY)):
            raise RewardError('catalog_invalid', 'Mặt hàng chưa có quyền cấp hoặc giới hạn đã kiểm tra.')
        seen.add(ident)
        payments = offer['payments']
        if not isinstance(payments, list) or not 1 <= len(payments) <= 2:
            raise RewardError('catalog_invalid', 'Mặt hàng chưa có giá hợp lệ.')
        keys = set()
        for payment in payments:
            if (payment['key'] not in ('primary', 'alternate') or payment['key'] in keys
                    or not _integer(payment['coinType'], 1, 3)
                    or not _integer(payment['price'], 1, MAX_INT)
                    or not _integer(payment['quantity'], 1, descriptor['max_quantity'])):
                raise RewardError('catalog_invalid', 'Giá hoặc số lượng nguồn chưa hợp lệ.')
            keys.add(payment['key'])
        if offer.get('autoUse') and str(offer['itemId']) not in boxes:
            raise RewardError('catalog_invalid', 'Vật phẩm dùng ngay chưa có cơ chế được hỗ trợ.')
    for key, box in boxes.items():
        descriptor = rewards.get('item:' + key)
        if (not key.isdigit() or int(key) != box['itemId'] or not descriptor
                or descriptor['container'] != 2 or descriptor.get('unique')):
            raise RewardError('catalog_invalid', 'Cấu hình hộp chưa khớp vật phẩm gốc.')
        for _, grants in shop_boxes.outcomes(box):
            mail_rewards.normalize(grants, rewards)
            if any(row['kind'] == 'item' and rewards['item:' + str(row['id'])]['container'] == 2 for row in grants):
                raise RewardError('catalog_invalid', 'Phần thưởng hộp cùng ngăn chưa được hỗ trợ.')
        if box['mode'] == 'random' and box.get('duplicate_policy') != 'reject_owned_unique_main':
            raise RewardError('catalog_invalid', 'Hộp chưa có chính sách phần thưởng trùng đã duyệt.')
    return value


def _descriptor(reward, available):
    row = available[reward['kind'] + ':' + str(reward['id'])]
    ident = reward['id']
    if reward['kind'] == 'currency':
        ident = next(coin for coin, (key, _) in COINS.items() if key == ident)
    value = dict(kind=reward['kind'], id=ident, name=row['name'], quantity=reward['quantity'], category=row['category'])
    if row.get('container') is not None:
        value['container'] = row['container']
    return value


def _public_box(box, available):
    value = {key: deepcopy(box[key]) for key in ('itemId', 'name', 'mode', 'main_percent', 'material_percent', 'duplicate_policy') if key in box}
    for key in ('main_rewards', 'material_rewards', 'fixed_rewards'):
        if key in box:
            value[key] = [_descriptor(reward, available) for reward in box[key]]
    return value


def public_catalog():
    source = catalog()
    return dict(v=VERSION, revision=source['revision'], tabs=deepcopy(source['tabs']),
                offers=deepcopy(source['offers']),
                boxes={key: _public_box(box, source['rewards']) for key, box in source['boxes'].items()})


def _available(db, source):
    # Runtime consumer guards remain authoritative; the shop never extends mail.
    current = mail_rewards.catalog(db)
    items = {int(row['nItemId']): row for row in mail_rewards.rows(db, 'ItemTemplate_table.xml')}
    result = {}
    for key, row in source['rewards'].items():
        if key.startswith('item:') and str(row['id']) in source['boxes']:
            item = items.get(row['id'], {})
            ntype = int(item.get('nType') or 0)
            ncont = int(item.get('nContType') or 0)
            stack = int(item.get('iStackMax') or 0)
            autouse = int(item.get('nAutoUse') or 0)
            if (ntype not in (20, 27, 69)
                    or ncont != row.get('container')
                    or stack != row.get('stack_max')
                    or (autouse and ntype not in (69,))):
                raise RewardError('catalog_mismatch', 'Hộp không khớp dữ liệu gốc hiện tại; chưa thay đổi gì.')
            result[key] = row
        elif key in current:
            runtime = current[key]
            if any(row.get(field) != runtime.get(field) for field in ('kind', 'id', 'category', 'unique', 'level', 'container', 'stack_max')):
                raise RewardError('catalog_mismatch', 'Mặt hàng không khớp cơ chế hiện tại; chưa thay đổi gì.', {'key': key})
            result[key] = runtime
    return result


def _receipt_key(operation_id):
    return f'shop_v1:{storage.profile_id()}:{operation_id}'


def _receipt(db, operation_id):
    row = db.execute('SELECT value FROM settings WHERE name=?', (_receipt_key(operation_id),)).fetchone()
    return json.loads(row[0]) if row else None


def _snapshot(db):
    containers, wallet = inventory.load(db), cosmetic_shop.snapshot(db)
    garage = [dict(ordinal=row['ordinal'], data=json.loads(row['data']))
              for row in db.execute('SELECT ordinal,data FROM garage WHERE profile_id=? ORDER BY ordinal', (storage.profile_id(),))]
    return dict(containers=containers, wallet=wallet, garage=garage, capacity=inventory.grocery_capacity(db))


def _snapshot_packets(db, state):
    value = _snapshot(db)
    packets = [(1039, struct.pack('<HH6i', 2, 0, *value['wallet']['currency']))]
    packets += [(1100, inventory.list_payload(cont, value['containers'].get(str(cont), [])))
                for cont in (2, 3, 4, 5, 6, 7, 9, 10)]
    selected = state.get('selected_tank_uuid')
    visible = []
    for row in value['garage']:
        record = row['data']
        active = record['uuid'] == selected if selected else record['tank_id'] == state.get('tank_id')
        visible.append(dict(record, ext_flag=(record['ext_flag'] & ~2) | (2 if active else 0)))
    packets.append((1150, garage_payload(visible, mail_rewards.GARAGE_SNAPSHOT_TRAILER)))
    return packets


def _validate_packets(packets):
    for mid, body in packets:
        struct.pack('<HHI', len(body) + 8, mid, 0)


def _preflight_values(values, state):
    """Check each possible native update before choosing a lottery branch."""
    for branch in values:
        value = branch['value']
        packets = [(1039, struct.pack('<HH6i', 2, 0, *value['wallet']['currency']))]
        packets += bag_updates(value['before'], value['containers'],
                               {int(c) for c in set(value['before']) | set(value['containers'])})
        if value['new_tanks']:
            selected = state.get('selected_tank_uuid')
            visible = []
            for record in value['garage']:
                active = record['uuid'] == selected if selected else record['tank_id'] == state.get('tank_id')
                visible.append(dict(record, ext_flag=(record['ext_flag'] & ~2) | (2 if active else 0)))
            packets.append((1150, garage_payload(visible, mail_rewards.GARAGE_SNAPSHOT_TRAILER)))
        _validate_packets(packets)


def _purchase_error(exc):
    if exc.code == 'already_owned':
        return RewardError(exc.code, 'Bạn đã sở hữu vật phẩm/tank vĩnh viễn này; chưa trừ tiền.', exc.details)
    if exc.code == 'bag_full':
        return RewardError(exc.code, 'Túi không đủ chỗ cho số lượng đã chọn; chưa trừ tiền.', exc.details)
    return exc


def plan(db, intent):
    """All plans use one read snapshot; no preview draw, debit or item mutation."""
    source = catalog()
    available = _available(db, source)
    before = _snapshot(db)
    revision = digest(dict(economy=before, catalog=source['revision']))
    if intent['kind'] == 'buy':
        offer = next((row for row in source['offers'] if row['goodsId'] == intent['goods_id']), None)
        if offer is None:
            raise RewardError('offer_unknown', 'Mặt hàng này chưa được bán hoặc chưa được runtime hỗ trợ.')
        payment = next((row for row in offer['payments'] if row['key'] == intent['payment']), None)
        if payment is None or intent['quantity'] > offer['maxCount']:
            raise RewardError('payment_invalid', 'Cách trả hoặc số lượng không có trong mặt hàng hiện tại.')
        cost, count = payment['price'] * intent['quantity'], payment['quantity'] * intent['quantity']
        if not 1 <= cost <= MAX_INT or not 1 <= count <= MAX_INT:
            raise RewardError('quantity_invalid', 'Tổng giá hoặc số lượng vượt giới hạn native; chưa mua.')
        kind = 'tank' if offer['grantKind'] == 'tank' else 'item'
        rewards = [dict(kind=kind, id=offer['itemId'], quantity=count)]
        box = source['boxes'].get(str(offer['itemId']))
        if box is not None:
            shop_boxes.require_usable_main(box, before['containers'], [row['data'] for row in before['garage']], available)
        if offer.get('autoUse'):
            if box['mode'] != 'fixed':
                raise RewardError('offer_unsupported', 'Hộp ngẫu nhiên dùng ngay chưa được hỗ trợ.')
            rewards = [dict(row, quantity=row['quantity'] * count) for row in box['fixed_rewards']]
        try:
            value = mail_rewards.plan(db, storage.profile_id(), rewards, available)
        except RewardError as exc:
            raise _purchase_error(exc) from exc
        index = COINS[payment['coinType']][1]
        if value['wallet']['currency'][index] < cost:
            raise RewardError('insufficient_funds', 'Không đủ ' + mail_rewards.CURRENCIES[COINS[payment['coinType']][0]][1] + '; chưa mua.')
        value['wallet']['currency'][index] -= cost
        quote = dict(kind='buy', profile_id=storage.profile_id(), policy='all_or_nothing',
                     goods_id=offer['goodsId'], item_id=offer['itemId'], name=offer['name'], quantity=intent['quantity'],
                     payment=payment['key'], coin_type=payment['coinType'], unit_price=payment['price'], total_price=cost,
                     rewards=[_descriptor(row, available) for row in value['rewards']])
        return dict(revision=revision, quote=quote, values=[dict(outcome='purchase', value=value)], available=available)
    ref = intent['item']
    quantity = intent.get('quantity', 1)
    box = source['boxes'].get(str(ref['template_id']))
    if box is None:
        raise RewardError('box_unsupported', 'Vật phẩm này chưa có cơ chế mở hộp được hỗ trợ.')
    item = economy_actions._lookup(before['containers'], ref)
    economy_actions._protection(item)
    if item['count'] < quantity:
        raise RewardError('quantity_invalid', 'Số lượng hộp trong túi không đủ.')
    values = shop_boxes.plans(db, box, ref, available, quantity=quantity)
    quote = dict(kind='open_box', profile_id=storage.profile_id(), policy='all_or_nothing', item_id=box['itemId'],
                 name=box['name'], quantity=quantity, box=_public_box(box, available))
    return dict(revision=revision, quote=quote, values=values, available=available)


def _proof(context, operation_id, intent, value, expires):
    signed = dict(profile=storage.profile_id(), generation=context['generation'], operation_id=operation_id,
                  intent=digest(intent), revision=value['revision'], quote=value['quote'], expires=expires)
    return dict(generation=context['generation'], revision=value['revision'], expires=expires,
                token=hmac.new(context['secret'], text(signed).encode('utf-8'), hashlib.sha256).hexdigest())


def apply(state, body, db, context):
    request_id = action = operation_id = ''
    known_absent = False
    try:
        request = decode_request(body)
        request_id, action, operation_id = (request[key] for key in ('request_id', 'action', 'operation_id'))
        if type(state.get('char_id')) is not int or state['char_id'] != storage.profile_id():
            raise RewardError('profile_mismatch', 'Phiên shop không khớp nhân vật đã đăng nhập.')
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE' if action == 'apply' else 'BEGIN')
        receipt = _receipt(db, operation_id)
        response = dict(request_id=request_id, operation_id=operation_id, action=action, ok=True, profile_id=storage.profile_id())
        native = []
        if action == 'status':
            response.update(status='committed' if receipt else 'not_committed', receipt=receipt)
            native = _snapshot_packets(db, state)
        elif receipt is not None:
            if receipt['intent_hash'] != digest(request['intent']):
                raise RewardError('operation_conflict', 'Mã thao tác đã dùng cho nội dung khác; không mua hoặc mở thêm.')
            response.update(status='committed', duplicate=True, receipt=receipt)
            native = _snapshot_packets(db, state)
        else:
            known_absent = True
            if (state.get('battle') or state.get('tank_selection') or state.get('room')
                    or state.get('multiplayer_room_id') or state.get('started_room')):
                raise RewardError('shop_phase', 'Về sảnh để mua hoặc mở vật phẩm.')
            value = plan(db, request['intent'])
            _preflight_values(value['values'], state)
            if action == 'preview':
                response.update(status='preview', intent_hash=digest(request['intent']), quote=value['quote'],
                                proof=_proof(context, operation_id, request['intent'], value, int(time.time()) + PREVIEW_SECONDS))
            else:
                proof, now = request['proof'], int(time.time())
                expected = _proof(context, operation_id, request['intent'], value, proof['expires'])
                if (proof['generation'] != context['generation'] or proof['revision'] != value['revision']
                        or not now <= proof['expires'] <= now + PREVIEW_SECONDS
                        or not hmac.compare_digest(proof['token'], expected['token'])):
                    raise RewardError('preview_stale', 'Xác nhận đã hết hạn hoặc túi/tiền/danh mục đã đổi; chưa tiêu gì. Hãy xem trước lại.')
                if request['intent']['kind'] == 'buy':
                    chosen = value['values'][0]
                elif request['intent'].get('quantity', 1) > 1:
                    source = catalog()
                    box = source['boxes'][str(request['intent']['item']['template_id'])]
                    chosen = shop_boxes.draw_bulk(db, box, request['intent']['item'], value['available'], request['intent']['quantity'])
                else:
                    chosen = shop_boxes.select(value['values'])
                native = mail_rewards.commit_plan(db, state, chosen['value'])
                raw_rewards = chosen.get('all_rewards', chosen['value']['rewards'])
                receipt = dict(operation_id=operation_id, profile_id=storage.profile_id(), intent_hash=digest(request['intent']),
                               committed_at=now, quote=value['quote'], outcome=chosen['outcome'],
                               rewards=[_descriptor(row, value['available']) for row in raw_rewards])
                db.execute('INSERT INTO settings(name,value) VALUES(?,?)', (_receipt_key(operation_id), text(receipt)))
                response.update(status='committed', duplicate=False, receipt=receipt)
        final = native + [packet(response)]
        _validate_packets(final)
        db.commit()
        return final, None
    except (RewardError, ValueError, UnicodeError, struct.error, sqlite3.Error) as exc:
        db.rollback()
        error = exc if isinstance(exc, RewardError) else RewardError('shop_invalid', 'Không thể hoàn tất giao dịch; kiểm tra kết quả trước khi thử lại.')
        envelope = error.envelope()
        # A failed commit/connection must be resolved by status rather than
        # claiming certainty about absence from a failed SQLite operation.
        envelope['not_committed'] = known_absent and not isinstance(exc, sqlite3.Error)
        return [packet(dict(request_id=request_id, operation_id=operation_id, action=action, ok=False,
                            profile_id=storage.profile_id(), error=envelope))], str(error)
    except BaseException:
        db.rollback()
        raise
