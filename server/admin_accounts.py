"""Confirmed local account administration; passwords never enter receipts.

Deletion is explicitly SOFT: credentials cannot authenticate, active admin/mail
lists omit the account, and all game data/settlement receipts retain identity.
Restore keeps the same username/profile.  It does not re-grant starter items.
"""
import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata

import account_lifecycle as lifecycle
import characters
import storage
from auth_store import hash_pw
from mail_rewards import RewardError

REQUEST_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
REVISION = re.compile(r'^[0-9a-f]{64}$')
ACTIONS = {'create', 'password', 'delete', 'restore'}
LABELS = {'create': 'Tạo tài khoản', 'password': 'Đổi mật khẩu',
          'delete': 'Xóa mềm (giữ dữ liệu)', 'restore': 'Khôi phục tài khoản'}


class AccountError(RewardError):
    def __init__(self, code, message, *, status=409):
        super().__init__(code, message)
        self.status = status


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _mac(key, domain, value):
    return hmac.new(key, (domain + '\0' + _json(value)).encode('utf-8'), hashlib.sha256).hexdigest()


def initialize():
    with storage.connection(write=True) as db:
        db.execute('CREATE TABLE IF NOT EXISTS admin_account_deleted_v1 ('
                   'username TEXT PRIMARY KEY,profile_id INTEGER,deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),'
                   'changed_at INTEGER NOT NULL,request_id TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS admin_account_operations_v1 ('
                   'request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,action TEXT NOT NULL,'
                   'public_json TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN (\'prepared\',\'completed\',\'cancelled\')),'
                   'result_json TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)')


def _request_id(value):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise AccountError('account_request_invalid', 'Mã thao tác tài khoản không hợp lệ.', status=400)
    return value


def _username(value):
    if not isinstance(value, str):
        raise AccountError('account_username_invalid', 'Tên đăng nhập không hợp lệ.', status=400)
    value = value.strip()
    if (value != '1' and not 3 <= len(value) <= 32 or len(value.encode('utf-8')) > 128
            or any(c.isspace() or unicodedata.category(c)[0] == 'C' or c in '<>&' for c in value)):
        raise AccountError('account_username_invalid',
                           'Tên đăng nhập cần 3–32 ký tự, không chứa khoảng trắng/ký tự điều khiển hoặc < > &; tài khoản test 1 được giữ ngoại lệ.', status=400)
    return value


def _password(value, username):
    minimum = 1 if username == '1' else 6
    if (not isinstance(value, str) or not minimum <= len(value) <= 256
            or len(value.encode('utf-8')) > 1024 or '\0' in value):
        raise AccountError('account_password_invalid', f'Mật khẩu cần {minimum}–256 ký tự, tối đa 1.024 byte UTF-8.', status=400)
    return value


def canonical(body):
    if not isinstance(body, dict) or not isinstance(body.get('action'), str) or body['action'] not in ACTIONS:
        raise AccountError('account_action_invalid', 'Chọn chức năng tài khoản hợp lệ.', status=400)
    action = body['action']
    expected = {'action', 'request_id', 'username'} if action == 'create' else {'action', 'request_id', 'target'}
    if action in ('create', 'password'):
        expected.add('password')
    allowed = set(expected)
    if action == 'create':
        allowed.add('buff')
    if not (expected <= set(body) <= allowed):
        raise AccountError('account_payload_invalid', 'Nội dung thao tác tài khoản không hợp lệ.', status=400)
    result = dict(action=action, request_id=_request_id(body['request_id']))
    if action == 'create':
        username = _username(body['username'])
        result['username'] = username
        result['buff'] = bool(body.get('buff', True))
    else:
        target = body['target']
        if (not isinstance(target, dict) or set(target) != {'username', 'profile_id', 'revision'}
                or not isinstance(target['username'], str) or not 1 <= len(target['username']) <= 128
                or target['profile_id'] is not None and (type(target['profile_id']) is not int or not 0 < target['profile_id'] <= 0xffffffff)
                or not isinstance(target['revision'], str) or not REVISION.fullmatch(target['revision'])):
            raise AccountError('account_target_invalid', 'Chọn lại đúng tài khoản từ danh sách.', status=400)
        username = target['username']
        result['target'] = dict(target)
    if action in ('create', 'password'):
        result['password'] = _password(body['password'], username)
    return result


def fingerprint(key, payload):
    # Keyed digest prevents the operation table becoming an unsalted password
    # oracle.  No plaintext password, salted login hash or key is returned.
    return _mac(key, 'm31-account-intent-v1', payload)


def _record(db, username):
    row = db.execute('SELECT u.username,u.salt,u.pw,u.created,c.profile_id,w.state,w.arena '
                     'FROM users u LEFT JOIN characters c ON c.username=u.username '
                     'LEFT JOIN worlds w ON w.profile_id=c.profile_id WHERE u.username=?', (username,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['deleted'] = lifecycle.blocked(db, username)
    result['world'] = json.loads(result['state']) if result['state'] is not None else None
    if result['profile_id'] is not None and not isinstance(result['world'], dict):
        raise AccountError('account_data_invalid', 'Dữ liệu nhân vật không hợp lệ; chưa thay đổi tài khoản.')
    return result


def _public(record, key):
    world = record['world'] or {}
    revision = _mac(key, 'm31-account-revision-v1', [record['username'], record['created'], record['salt'], record['pw'],
                    record['profile_id'], world.get('char_uuid'), world.get('name'), record['deleted']])
    return dict(username=record['username'], profile_id=record['profile_id'], name=world.get('name'),
                created_at=record['created'], deleted=record['deleted'], revision=revision,
                online=bool(record['profile_id'] is not None and characters.profile_is_online(record['profile_id'])),
                in_progress=bool(world.get('battle') or world.get('tank_selection') or record['arena']))


def accounts(db, key, query='', status='active', offset=0, limit=25):
    if status not in ('active', 'deleted', 'all'):
        raise AccountError('account_filter_invalid', 'Bộ lọc tài khoản không hợp lệ.', status=400)
    query = query.casefold()
    rows = []
    for entry in db.execute('SELECT username FROM users ORDER BY username COLLATE NOCASE'):
        record = _record(db, entry[0])
        if status != 'all' and record['deleted'] != (status == 'deleted'):
            continue
        item = _public(record, key)
        if query and not any(query in str(item[field] or '').casefold() for field in ('username', 'profile_id', 'name')):
            continue
        rows.append(item)
    return dict(rows=rows[offset:offset + limit], total=len(rows), offset=offset, limit=limit)


def _target(db, key, payload):
    expected = payload.get('target')
    username = payload.get('username') if expected is None else expected['username']
    record = _record(db, username)
    if payload['action'] == 'create':
        if record is None and lifecycle.blocked(db, username):
            raise AccountError('account_deleted_name', 'Tên này thuộc tài khoản đã xóa mềm nhưng thiếu dữ liệu gốc. Chưa tạo đè tài khoản.')
        if record:
            message = 'Tên này đã được xóa mềm. Chọn Khôi phục để dùng lại đúng dữ liệu cũ.' if record['deleted'] else 'Tên đăng nhập đã tồn tại.'
            raise AccountError('account_exists', message)
        return None
    if record is None:
        raise AccountError('account_missing', 'Tài khoản không còn tồn tại. Tải lại danh sách.')
    current = _public(record, key)
    if current['profile_id'] != expected['profile_id'] or not secrets.compare_digest(current['revision'], expected['revision']):
        raise AccountError('account_changed', 'Tài khoản đã thay đổi từ lúc chọn. Hủy thao tác chờ rồi chọn lại.')
    return record


def _conditions(db, payload, record, *, held=False):
    action = payload['action']
    if action == 'create':
        return
    if action == 'restore' and not record['deleted']:
        raise AccountError('account_not_deleted', 'Tài khoản này đang hoạt động; không cần khôi phục.')
    if action != 'restore' and record['deleted']:
        raise AccountError('account_deleted', 'Khôi phục tài khoản trước khi đổi mật khẩu.')
    ident = record['profile_id']
    if ident is None:
        return
    if not held and characters.profile_is_online(ident):
        raise AccountError('account_online', 'Nhân vật đang mở trong game. Đóng phiên game rồi kiểm tra lại.')
    world = record['world']
    if world.get('battle') or world.get('tank_selection') or record['arena']:
        raise AccountError('account_in_progress', 'Nhân vật còn trận hoặc chọn tank đang lưu. Vào game, kết thúc/rời trận về sảnh rồi đóng game.')
    if action == 'delete':
        if ident == storage.PROFILE_ID or record['username'] == 'danchoi36':
            raise AccountError('account_legacy_protected', 'Không xóa tài khoản gốc dùng cho dữ liệu nền/migration. Vẫn có thể đổi mật khẩu khi đã đóng game.')
        if lifecycle.has_table(db, 'guild_members') and db.execute('SELECT 1 FROM guild_members WHERE profile=?', (ident,)).fetchone():
            raise AccountError('account_guild_member', 'Nhân vật còn trong Guild. Chuyển quyền trưởng nếu cần và rời Guild trong game trước khi xóa mềm.')


def _operation(db, request_id):
    row = db.execute('SELECT * FROM admin_account_operations_v1 WHERE request_id=?', (request_id,)).fetchone()
    return dict(row) if row else None


def _receipt(row):
    result = dict(request_id=row['request_id'], status=row['status'], action=row['action'],
                  summary=json.loads(row['public_json']))
    if row['result_json'] is not None:
        result['result'] = json.loads(row['result_json'])
    return result


def receipt(request_id):
    request_id = _request_id(request_id)
    with storage.connection() as db:
        row = _operation(db, request_id)
    return _receipt(row) if row else dict(request_id=request_id, status='unknown')


def preview(body, key):
    payload = canonical(body)
    digest = fingerprint(key, payload)
    username = payload.get('username') or payload['target']['username']
    with lifecycle.account_lock(username), storage.connection(write=True) as db:
        prior = _operation(db, payload['request_id'])
        if prior and prior['status'] == 'cancelled':
            raise AccountError('account_operation_cancelled', 'Thao tác này đã hủy; không được dùng lại mã cũ.')
        if prior and not secrets.compare_digest(prior['fingerprint'], digest):
            raise AccountError('account_idempotency_conflict', 'Mã thao tác đã gắn với nội dung khác; kiểm tra biên nhận cũ trước.')
        if prior and prior['status'] == 'completed':
            return _receipt(prior), None
        record = _target(db, key, payload)
        _conditions(db, payload, record)
        account = _public(record, key) if record else dict(username=username, profile_id=None, name=None)
        summary = dict(action=payload['action'], label=LABELS[payload['action']], account=account,
                       warnings=['Xóa mềm sẽ khóa đăng nhập và ẩn tài khoản khỏi danh sách hoạt động; toàn bộ đồ, nhân vật, thư và biên nhận được giữ để khôi phục.']
                       if payload['action'] == 'delete' else
                       ['Giữ nguyên tài khoản/nhân vật và dữ liệu cũ; không cấp lại vật phẩm khởi đầu.']
                       if payload['action'] == 'restore' else
                       ['Thu hồi các phiên web và mã đăng nhập game cũ; đăng nhập lại bằng mật khẩu mới.']
                       if payload['action'] == 'password' else
                       ['Chỉ tạo tên đăng nhập và mật khẩu. Nhân vật được tạo theo thiết lập hiện có ở lần vào game đầu tiên; không buff thêm đồ.'])
        if prior is None:
            now = int(time.time())
            db.execute('INSERT INTO admin_account_operations_v1 VALUES(?,?,?,?,\'prepared\',NULL,?,?)',
                       (payload['request_id'], digest, payload['action'], _json(summary), now, now))
        result = dict(request_id=payload['request_id'], status='prepared', **summary)
    return result, digest


def cancel(request_id):
    request_id = _request_id(request_id)
    with storage.connection(write=True) as db:
        row = _operation(db, request_id)
        if row is None:
            # A request may not have reached preview() yet.  Persisting the
            # cancellation first also rejects that late preview/execute.
            now = int(time.time())
            db.execute('INSERT INTO admin_account_operations_v1 VALUES(?,?,?, ?,\'cancelled\',NULL,?,?)',
                       (request_id, '', '', '{}', now, now))
        elif row['status'] == 'prepared':
            db.execute('UPDATE admin_account_operations_v1 SET status=\'cancelled\',updated_at=? WHERE request_id=?',
                       (int(time.time()), request_id))
        return _receipt(_operation(db, request_id))


def execute(body, key, proof_check):
    if not isinstance(body, dict) or set(body) - {'intent', 'preview_token', 'confirmation'} or not {'intent', 'preview_token'} <= set(body):
        raise AccountError('account_payload_invalid', 'Nội dung xác nhận tài khoản không hợp lệ.', status=400)
    payload = canonical(body['intent'])
    digest = fingerprint(key, payload)
    username = payload.get('username') or payload['target']['username']
    lock = None
    revoke = None
    with lifecycle.account_lock(username):
        try:
            # Read only the immutable target identity before acquiring the
            # native profile lock. Never wait for that lock while holding a
            # SQLite writer: live GATE owns profile→DB in that order.
            with storage.connection() as db:
                prior = _operation(db, payload['request_id'])
                if prior and prior['status'] == 'completed':
                    if not secrets.compare_digest(prior['fingerprint'], digest):
                        raise AccountError('account_idempotency_conflict', 'Mã thao tác đã gắn với nội dung khác.')
                    return dict(_receipt(prior), duplicate=True)
                if prior is None or prior['status'] != 'prepared':
                    raise AccountError('account_operation_cancelled', 'Không có thao tác đã kiểm tra còn hiệu lực; chưa thay đổi tài khoản.')
                if not secrets.compare_digest(prior['fingerprint'], digest):
                    raise AccountError('account_idempotency_conflict', 'Nội dung xác nhận không khớp nội dung đã kiểm tra.')
                if not proof_check(body['preview_token'], digest):
                    raise AccountError('account_preview_required', 'Xác nhận đã hết hạn/đổi phiên. Hủy thao tác chờ rồi nhập lại; chưa đổi tài khoản.')
                record = _target(db, key, payload)
                _conditions(db, payload, record)
            if record and record['profile_id'] is not None:
                try:
                    lock = characters.acquire(record['profile_id'])
                except ValueError as exc:
                    raise AccountError('account_online', 'Nhân vật vừa mở trong game. Đóng phiên game rồi kiểm tra lại.') from exc
            with storage.connection(write=True) as db:
                # Cancel and execute serialize on this same prepared row.
                prior = _operation(db, payload['request_id'])
                if prior['status'] != 'prepared':
                    if prior['status'] == 'completed':
                        return dict(_receipt(prior), duplicate=True)
                    raise AccountError('account_operation_cancelled', 'Thao tác đã được hủy; chưa thay đổi tài khoản.')
                record = _target(db, key, payload)
                _conditions(db, payload, record, held=True)
                action = payload['action']
                if action == 'delete' and body.get('confirmation') != 'XOA ' + username:
                    raise AccountError('account_confirmation_required', 'Gõ đúng XOA và tên đăng nhập để xác nhận xóa mềm.', status=400)
                if action == 'create':
                    salt = secrets.token_hex(8)
                    db.execute('INSERT INTO users VALUES(?,?,?,?)', (username, salt, hash_pw(payload['password'], salt), int(time.time())))
                    is_buff = 1 if payload.get('buff', True) else 0
                    db.execute('CREATE TABLE IF NOT EXISTS account_buff_flags (username TEXT PRIMARY KEY, is_buff INTEGER NOT NULL DEFAULT 0)')
                    db.execute('INSERT OR REPLACE INTO account_buff_flags (username, is_buff) VALUES (?,?)', (username, is_buff))
                elif action == 'password':
                    salt = secrets.token_hex(8)
                    db.execute('UPDATE users SET salt=?,pw=? WHERE username=?', (salt, hash_pw(payload['password'], salt), username))
                else:
                    db.execute('INSERT INTO admin_account_deleted_v1 VALUES(?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET '
                               'profile_id=excluded.profile_id,deleted=excluded.deleted,changed_at=excluded.changed_at,request_id=excluded.request_id',
                               (username, record['profile_id'], int(action == 'delete'), int(time.time()), payload['request_id']))
                if action != 'create':
                    db.execute('DELETE FROM sessions WHERE username=?', (username,))
                    revoke = record['profile_id']
                # Entire result is serializable BEFORE commit. A lost HTTP ACK
                # is resolved by receipt; never repeat an action under a new ID.
                result = dict(operation=action, account=_public(_record(db, username), key),
                              message={'create': 'Đã tạo tài khoản; đăng nhập game để tạo nhân vật.',
                                       'password': 'Đã đổi mật khẩu và thu hồi phiên đăng nhập cũ.',
                                       'delete': 'Đã xóa mềm: không thể đăng nhập, dữ liệu được giữ để khôi phục.',
                                       'restore': 'Đã khôi phục đúng tài khoản và dữ liệu cũ; hãy đăng nhập lại.'}[action])
                # The lock here belongs to this administrative transaction,
                # not a live GATE connection. Do not persist a false online
                # status in an otherwise immutable operation receipt.
                if lock is not None:
                    result['account']['online'] = False
                db.execute('UPDATE admin_account_operations_v1 SET status=\'completed\',result_json=?,updated_at=? WHERE request_id=?',
                           (_json(result), int(time.time()), payload['request_id']))
                response = dict(_receipt(_operation(db, payload['request_id'])), duplicate=False)
            return response
        finally:
            # Even an error after attempting commit must not leave a stale
            # in-memory AUTH key usable. No active world can be held here.
            if revoke is not None:
                characters.invalidate_profile_sessions(revoke)
            if lock is not None:
                lock.release()
