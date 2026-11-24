"""Account-owned characters and native AUTH/GATE identity.

Shared templates stay global; mutable data is scoped by storage.for_profile.
Migration preserves Vua36 byte-for-byte and records an immutable starter seed.
"""
from copy import deepcopy
import json
import secrets
import struct
import threading
import time
import uuid

import storage
import starter_appearance
import account_lifecycle

_sessions = {}
_locks = {}
_guard = threading.Lock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def initialize():
    import inventory
    import cosmetic_shop
    import crystals
    inventory.initialize()
    cosmetic_shop.initialize()
    crystals.initialize()
    with storage.connection(write=True) as db:
        # Approved 15/10: keep all 79 existing Vua36 items and expose 90 slots.
        db.execute('INSERT INTO settings(name,value) VALUES (?,?) ON CONFLICT(name) DO NOTHING',
                   (f'inventory_slots:{storage.PROFILE_ID}', '90'))
        db.execute('CREATE TABLE IF NOT EXISTS characters ('
                   'profile_id INTEGER PRIMARY KEY REFERENCES worlds(profile_id),'
                   'username TEXT UNIQUE NOT NULL REFERENCES users(username))')
        db.execute('CREATE TABLE IF NOT EXISTS account_buff_flags ('
                   'username TEXT PRIMARY KEY,'
                   'is_buff INTEGER NOT NULL DEFAULT 0)')
        import military_rank
        military_rank.initialize_ranking(db)
        if db.execute('SELECT 1 FROM characters WHERE profile_id=?', (storage.PROFILE_ID,)).fetchone():
            return
        if not db.execute('SELECT 1 FROM users WHERE username=?', ('danchoi36',)).fetchone():
            raise RuntimeError('Approved legacy owner danchoi36 is missing; migration stopped')
        db.execute('INSERT INTO characters VALUES (?,?)', (storage.PROFILE_ID, 'danchoi36'))
        state = json.loads(db.execute('SELECT state FROM worlds WHERE profile_id=?',
                                     (storage.PROFILE_ID,)).fetchone()[0])
        records = [json.loads(r[0]) for r in db.execute(
            'SELECT data FROM garage WHERE profile_id=? ORDER BY ordinal', (storage.PROFILE_ID,))]
        seed = dict(state=state, tanks=records, skins=inventory.load(db).get('6', []),
                    currency=cosmetic_shop.snapshot(db)['currency'])
        # Seed is fixed at migration, never copied again from a player's later progress.
        db.execute('INSERT INTO settings VALUES (?,?)', ('character_seed_v1', _json(seed)))


def for_username(username, db=None):
    if db is None:
        with account_lifecycle.account_lock(username), storage.connection(write=True) as opened:
            return for_username(username, opened)
    if account_lifecycle.blocked(db, username):
        raise ValueError('Tài khoản đã bị xóa. Liên hệ quản trị local.')
    row = db.execute('SELECT profile_id FROM characters WHERE username=?', (username,)).fetchone()
    if row:
        return int(row[0])
    if not db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
        raise ValueError('Tài khoản không tồn tại.')
    db.execute('CREATE TABLE IF NOT EXISTS account_buff_flags (username TEXT PRIMARY KEY, is_buff INTEGER NOT NULL DEFAULT 0)')
    buff_row = db.execute('SELECT is_buff FROM account_buff_flags WHERE username=?', (username,)).fetchone()
    is_buff = int(buff_row[0]) if buff_row is not None else 1

    ident = account_lifecycle.next_profile_id(db)

    if is_buff:
        row = db.execute('SELECT value FROM settings WHERE name=?', ('character_seed_v1',)).fetchone()
        if row is None:
            raise RuntimeError('Character migration has not been installed')
        seed = json.loads(row[0])
        state = deepcopy(seed['state'])
        state.update(char_id=ident, char_uuid=f'multiplayer-char-{ident}', name=username,
                     room=None, battle=None, avatar=starter_appearance.ASH, decorate_id=0,
                     crystal_build={}, military_build={}, military_star=0, guild_build={},
                     booster_build={}, booster_attack=0)
        records = []
        for original in seed['tanks']:
            tank = deepcopy(original)
            tank.update(uuid=f'char-{ident}-tank-{tank["tank_id"]}', crystal_scheme_id=0,
                        com_skill_id=0, war_count=0, week_war_count=0, battle_gains=0)
            records.append(tank)
        selected = next(t for t in records if t['tank_id'] == state['tank_id'])
        state['selected_tank_uuid'] = selected['uuid']
        db.execute('INSERT INTO worlds(profile_id,state) VALUES (?,?)', (ident, _json(state)))
        db.execute('INSERT INTO characters VALUES (?,?)', (ident, username))
        db.executemany('INSERT INTO garage VALUES (?,?,?,?,?)',
                       ((ident, i, t['uuid'], t['tank_id'], _json(t)) for i, t in enumerate(records)))
        import military_rank
        military_rank.record_stars(db, ident, military_rank.count_stars(records))
        skins = deepcopy(seed['skins'])
        for item in skins:
            item['uuid'] = 'skin-' + uuid.uuid4().hex
        currency = seed['currency']
    else:
        # Starter tank IDs for level 1 account: Iron Man (10000001), Chopper (10000029), Gundam (10000019)
        starter_tank_ids = [10000001, 10000029, 10000019]
        now = int(time.time())
        records = []
        for t_id in starter_tank_ids:
            records.append({
                'uuid': f'char-{ident}-tank-{t_id}',
                'tank_id': t_id,
                'level': 1,
                'n_exp': 0,
                'ext_flag': 0,
                'stth_level': 0,
                'external_id': 0,
                'war_count': 0,
                'week_war_count': 0,
                'cont_type': 0,
                'cash_hire': 0,
                'coin_hire': 0,
                'exist_time': 0,
                'halo_id': 0,
                'skin_frame_type': 0,
                'own_time': now,
                'stth_count': 0,
                'com_skill_id': 0,
                'crystal_scheme_id': 0,
                'tal_page_uuid': '',
                'prof_val': 0,
                'battle_gains': 0,
            })
        selected = records[0]  # Iron Man
        state = dict(
            char_id=ident, char_uuid=f'multiplayer-char-{ident}', name=username,
            level=1, sex=1, vip_level=0, tank_id=selected['tank_id'], tank_level=1,
            tank_ext=0, halo_id=0, skin_frame_type=0, room=None, bot=None,
            selected_tank_uuid=selected['uuid'], ally_bot=None, tank_strength=0,
            bots=None, avatar=starter_appearance.ASH, decorate_id=0,
            crystal_build={}, military_star=0, military_build={}, battle=None,
            guild_build={}, booster_build={}, booster_attack=0
        )
        db.execute('INSERT INTO worlds(profile_id,state) VALUES (?,?)', (ident, _json(state)))
        db.execute('INSERT INTO characters VALUES (?,?)', (ident, username))
        db.executemany('INSERT INTO garage VALUES (?,?,?,?,?)',
                       ((ident, i, t['uuid'], t['tank_id'], _json(t)) for i, t in enumerate(records)))
        import military_rank
        military_rank.record_stars(db, ident, 0)
        skins = []
        currency = [0, 10000, 0, 0, 0, 0]  # 10,000 Silver, 0 Cash

    settings = {'inventory': {'6': skins, '10': [], '2': [], '3': [],
                              '4': [starter_appearance.new_item(ident)], '7': []},
                'cosmetic_shop_profile': dict(avatar=starter_appearance.ASH, decorate=0, currency=currency),
                starter_appearance.marker_key(ident): starter_appearance.receipt(ident, equipped=True, added=True),
                'crystal_profile': {'boards': {str(i): {'name': '', 'items': [0]*30} for i in (1, 2)},
                                    'rolls': [0, 0]}}
    db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',
                   ((storage.setting_key(k, ident), _json(v)) for k, v in settings.items()))
    return ident


def pstr(text):
    raw = text.encode('utf8')
    return struct.pack('<H', len(raw)) + raw


def read_string(data, offset):
    size, = struct.unpack_from('<H', data, offset)
    end = offset + 2 + size
    if end > len(data):
        raise ValueError('Truncated login request')
    return data[offset+2:end].decode('utf8'), end


def login(payload):
    """C51 carries web userId/accessToken in name/passwd, not a raw password."""
    values, pos = [], 1
    for _ in range(8):
        value, pos = read_string(payload, pos)
        values.append(value)
    name, token = values[:2]
    with account_lifecycle.account_lock(name):
        return _login_identity(name, token)


def _login_identity(name, token):
    from auth_store import Store
    rec = Store().session_from_token(token, expected_username=name)
    if rec is None or not secrets.compare_digest(name.encode('utf-8'), rec['username'].encode('utf-8')):
        raise ValueError('Phiên đăng nhập hết hạn. Hãy đăng nhập lại trên web.')
    ident = for_username(name)
    expires = int(time.time()) + 3600
    with _guard:
        expired = [k for k, v in _sessions.items() if v[1] < time.time()]
        for key in expired:
            del _sessions[key]
        key = secrets.randbits(32)
        while not key or key in _sessions:
            key = secrets.randbits(32)
        _sessions[key] = (ident, expires)
    return struct.pack('<4I', key, expires, 1, ident)


def bind(payload):
    key, ident = struct.unpack_from('<II', payload)
    with _guard:
        rec = _sessions.get(key)
    if not rec or rec[0] != ident or rec[1] < time.time():
        raise ValueError('Phiên game không hợp lệ. Hãy đăng nhập lại.')
    return ident, key


def acquire(ident, game_token=None):
    with _guard:
        lock = _locks.setdefault(ident, threading.Lock())
    if not lock.acquire(blocking=False):
        raise ValueError('Nhân vật đang mở ở phiên khác. Hãy đóng phiên cũ trước.')
    if game_token is not None:
        # An admin reset/delete may run after bind() but before this lock was
        # obtained.  It revokes native tokens while holding the same lock.
        with _guard:
            rec = _sessions.get(game_token)
        if not rec or rec[0] != ident or rec[1] < time.time():
            lock.release()
            raise ValueError('Phiên game đã bị thu hồi. Hãy đăng nhập lại.')
    return lock


def invalidate_profile_sessions(ident):
    """Caller holds the profile lock and the username lifecycle lock."""
    with _guard:
        for key in [key for key, value in _sessions.items() if value[0] == ident]:
            del _sessions[key]


def profile_is_online(ident):
    with _guard:
        lock = _locks.get(ident)
        return bool(lock and lock.locked())


def validate_enter(payload, state):
    char, pos = read_string(payload, 0)
    _, pos = read_string(payload, pos)
    ident, = struct.unpack_from('<I', payload, pos)
    _, pos = read_string(payload, pos + 4)  # m_strEnterKey
    struct.unpack_from('<h', payload, pos)  # m_bIsFirstLogin; native tail padding is allowed.
    # GameTool.requestEnterWorld sets the UUID but leaves m_accountId at its
    # uint default (0). C54 already bound this connection to the authenticated
    # profile. Still reject another UUID or an explicitly conflicting account.
    if char != state['char_uuid'] or ident not in (0, state['char_id']):
        raise ValueError('Nhân vật không thuộc phiên đăng nhập.')


def login_payload(mid, body, state):
    if mid == 1005:
        return struct.pack('<II', state['char_id'], state.get('game_token', 0))
    if mid == 1016:
        # Replace only CHAR_BASIC identity; preserve the native remainder.
        _, pos = read_string(body, 126)
        _, end = read_string(body, pos)
        head = bytearray(body[:126])
        struct.pack_into('<I', head, 0, state['char_id'])
        head[4:6] = bytes((state['sex'], state['level']))
        struct.pack_into('<i', head, 10, int(state.get('n_exp', 0)))
        import inventory
        with storage.connection() as db:
            struct.pack_into('<H', head, 118, inventory.grocery_capacity(db)-45)
        return bytes(head) + pstr(state['name']) + pstr(state['char_uuid']) + body[end:]
    return body
