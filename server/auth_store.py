"""Existing local web login semantics, persisted transactionally in SQLite."""
import hashlib
import secrets
import time
from storage import connection
from account_lifecycle import account_lock, blocked, valid_user


def hash_pw(password, salt):
    return hashlib.sha256((salt+password).encode('utf-8')).hexdigest()


class Store:
    def __init__(self, path=None):
        self.path = path

    def _mint(self, db, username):
        if not valid_user(db, username):
            return None, None
        token,cookie = secrets.token_hex(16),secrets.token_hex(16)
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(token,username,cookie,time.time()+300))
        return token,cookie

    def login(self, username, password):
        username = username.strip()
        # Explicit local test account requested by the owner. It still has a
        # stored salted password and follows the same session/identity checks.
        # Other accounts retain the existing registration/login length policy.
        local_test = username == '1' and len(password) >= 1
        if not local_test and (not 3<=len(username)<=32 or len(password)<6):
            return {'success':False,'message':'Tài khoản 3–32 ký tự, mật khẩu ≥ 6.'}
        with account_lock(username, self.path), connection(self.path,write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS account_buff_flags (username TEXT PRIMARY KEY, is_buff INTEGER NOT NULL DEFAULT 0)')
            if blocked(db, username):
                return {'success':False,'message':'Tài khoản đã bị xóa. Liên hệ quản trị local.'}
            user = db.execute('SELECT salt,pw FROM users WHERE username=?',(username,)).fetchone()
            if user is None:
                return {'success':False,'message':'Tài khoản không tồn tại. Vui lòng đăng ký.'}
            elif user['pw']!=hash_pw(password,user['salt']):
                return {'success':False,'message':'Sai tài khoản hoặc mật khẩu.'}
            token,cookie = self._mint(db,username)
        return dict(success=True,username=username,loginToken=token,cookie=cookie)

    def register(self, username, password, is_buff=0):
        username = username.strip()
        local_test = username == '1' and len(password) >= 1
        if not local_test and (not 3 <= len(username) <= 32 or len(password) < 6):
            return {'success': False, 'message': 'Tài khoản 3–32 ký tự, mật khẩu ≥ 6 ký tự.'}
        with account_lock(username, self.path), connection(self.path, write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS account_buff_flags (username TEXT PRIMARY KEY, is_buff INTEGER NOT NULL DEFAULT 0)')
            if blocked(db, username):
                return {'success': False, 'message': 'Tài khoản đã bị xóa. Liên hệ quản trị local.'}
            existing = db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone()
            if existing is not None:
                return {'success': False, 'message': 'Tên tài khoản đã tồn tại.'}
            salt = secrets.token_hex(8)
            db.execute('INSERT INTO users VALUES (?,?,?,?)',
                       (username, salt, hash_pw(password, salt), int(time.time())))
            db.execute('INSERT OR REPLACE INTO account_buff_flags (username, is_buff) VALUES (?,?)',
                       (username, int(bool(is_buff))))
            token, cookie = self._mint(db, username)
        return dict(success=True, username=username, loginToken=token, cookie=cookie)

    def _session(self, field, value, lifetime, expected_username=None):
        if not value:
            return None
        # Resolve the username first, then re-read under the same account lock
        # as resets/deletion.  Never revive a session revoked between the reads.
        with connection(self.path) as db:
            candidate = db.execute(f'SELECT username FROM sessions WHERE {field}=? LIMIT 1', (value,)).fetchone()
        if candidate is None or expected_username is not None and candidate['username'] != expected_username:
            return None
        with account_lock(candidate['username'], self.path), connection(self.path,write=True) as db:
            row = db.execute(f'SELECT * FROM sessions WHERE {field}=? AND exp>=? LIMIT 1',
                             (value,time.time())).fetchone()
            if row is None or not valid_user(db, row['username']):
                return None
            exp = time.time()+lifetime
            db.execute('UPDATE sessions SET exp=? WHERE token=?',(exp,row['token']))
            return dict(username=row['username'],cookie=row['cookie'],exp=exp)

    def session_from_token(self, token, expected_username=None):
        return self._session('token',token,300,expected_username)

    def session_from_cookie(self, cookie):
        return self._session('cookie',cookie,3600)

    def mint_flash_token(self, username):
        with account_lock(username, self.path), connection(self.path,write=True) as db:
            return self._mint(db,username)[0]

    def refresh_from_cookie(self, cookie):
        """Revalidate the source cookie and mint AUTH atomically with resets.

        A prior session_from_cookie result is not an authorization to mint:
        an administrator may have reset the password since that lookup.
        """
        if not cookie:
            return None
        with connection(self.path) as db:
            candidate = db.execute('SELECT username FROM sessions WHERE cookie=? LIMIT 1', (cookie,)).fetchone()
        if candidate is None:
            return None
        with account_lock(candidate['username'], self.path), connection(self.path,write=True) as db:
            row = db.execute('SELECT username,token FROM sessions WHERE cookie=? AND exp>=? LIMIT 1',
                             (cookie,time.time())).fetchone()
            if row is None or not valid_user(db,row['username']):
                return None
            db.execute('UPDATE sessions SET exp=? WHERE token=?', (time.time()+3600,row['token']))
            token = self._mint(db,row['username'])[0]
            return dict(success=True,username=row['username'],loginToken=token) if token else None

    def logout_cookie(self, cookie):
        if cookie:
            with connection(self.path,write=True) as db:
                db.execute('DELETE FROM sessions WHERE cookie=?',(cookie,))
