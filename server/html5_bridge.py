"""HTML5 browser transport for the existing Bang Bang AUTH/GATE/CHAT server.

This module relays native Sixcube bytes. It never creates native replies or
implements game rules. The web cookie and the native login must belong to the
same account. M4 adds source-verified combat inputs and the native result close
request. The native server still validates the scene, actor and game rules.
M8 adds bounded native outside-battle requests after the same authenticated
GATE synchronization. Prices, inventory, tank ownership and Guild authority
are still decided only by the current native handlers.

Handler integration::

    session = STORE.session_from_cookie(self._cookie())
    return handle_websocket(self, session=session, store=STORE)

The URL is /html5/ws?role=AUTH, GATE or CHAT. No client-provided target address
is accepted. TCP_DESTINATION is a server-side constant; tests may replace it
with another loopback listener in their isolated process.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct
import threading
import time
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TCP_DESTINATION = ("127.0.0.1", 49119)
MAX_FRAME = 1024 * 1024
SESSION_CHECK_INTERVAL = 5.0
CLIENT_IDLE_TIMEOUT = 90.0
# Only operations exposed by the current outside-match UI. No chat messages,
# arbitrary native commands, alternate TCP targets or authentication shortcuts.
M8_MESSAGES = frozenset((60000, 60002, 60004, 60006, 60008, 350, 556, 557, 68, 69, 75, 76, 77, 78, 79, 88, 153, 155, 156, 157, 158, 159, 166, 200, 203, 204, 207, 210, 250, 304, 558,
                        320, 321, 322, 423, 424, 425, 426, 427, 430,
                        651, 652, 653, 654, 655, 656, 657, 658, 659, 660, 662, 663, 665, 666, 668, 669, 670, 672, 778))
ALLOWED_MESSAGES = {
    "AUTH": frozenset((51, 52)),
    "GATE": frozenset((52, 54, 56, 61, 72, 165, 215, 501, 503, 509, 510, 512, 514, 552,
                       600, 601, 602, 605, 607, 610, 611, 613, 614)) | M8_MESSAGES,
    "CHAT": frozenset((54, 60)),
}


class WebSocketError(ValueError):
    def __init__(self, message, code=1002):
        super().__init__(message)
        self.code = code


def _read_exact(stream, n):
    result = bytearray()
    while len(result) < n:
        chunk = stream.read(n - len(result))
        if not chunk:
            raise EOFError("Socket closed")
        result.extend(chunk)
    return bytes(result)


def _pack_ws(payload, opcode=2):
    n = len(payload)
    if n < 126:
        header = bytes((0x80 | opcode, n))
    elif n <= 65535:
        header = bytes((0x80 | opcode, 126)) + struct.pack("!H", n)
    else:
        header = bytes((0x80 | opcode, 127)) + struct.pack("!Q", n)
    return header + payload


def _read_ws(stream):
    """Read one masked RFC 6455 frame, leaving message reassembly to the relay."""
    first, second = _read_exact(stream, 2)
    final, opcode = bool(first & 0x80), first & 0x0F
    if first & 0x70 or opcode not in (0, 1, 2, 8, 9, 10):
        raise WebSocketError("Unsupported WebSocket flags or opcode")
    if not second & 0x80:
        raise WebSocketError("Client frames must be masked")
    n = second & 127
    if n == 126:
        n, = struct.unpack("!H", _read_exact(stream, 2))
        if n < 126:
            raise WebSocketError("Noncanonical WebSocket length")
    elif n == 127:
        n, = struct.unpack("!Q", _read_exact(stream, 8))
        if n <= 65535 or n >= 1 << 63:
            raise WebSocketError("Invalid WebSocket length")
    if n > MAX_FRAME:
        raise WebSocketError("WebSocket message too large", 1009)
    if opcode in (8, 9, 10) and (not final or n > 125):
        raise WebSocketError("Invalid WebSocket control frame")
    key = _read_exact(stream, 4)
    payload = bytearray(_read_exact(stream, n))
    for i in range(n):
        payload[i] ^= key[i % 4]
    return final, opcode, bytes(payload)


def _validate_close(payload):
    if len(payload) == 1:
        raise WebSocketError("Invalid close payload")
    if len(payload) >= 2:
        code, = struct.unpack_from("!H", payload)
        if code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010,
                        1011, 1012, 1013, 1014) and not 3000 <= code <= 4999:
            raise WebSocketError("Invalid close status")
        try:
            payload[2:].decode("utf-8")
        except UnicodeError as exc:
            raise WebSocketError("Invalid close text", 1007) from exc


def _same_origin(handler):
    origin = handler.headers.get("Origin")
    host = handler.headers.get("Host", "")
    if not origin or not host:
        return False
    try:
        parsed = urlsplit(origin)
        forwarded_proto = handler.headers.get("X-Forwarded-Proto")
        scheme = forwarded_proto if forwarded_proto in ("http", "https") else ("https" if isinstance(handler.connection, ssl.SSLSocket) else "http")
        return (parsed.scheme == scheme and parsed.netloc.lower() == host.lower()
                and parsed.username is None and not parsed.path.rstrip("/")
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _requested_role(path):
    parsed = urlsplit(path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    values = query.get("role", [])
    if (parsed.path != "/html5/ws" or set(query) != {"role"}
            or len(values) != 1 or values[0] not in ALLOWED_MESSAGES):
        raise ValueError("A single AUTH, GATE or CHAT role is required")
    return values[0]


class SessionLease:
    """Read the current cookie without extending its lifetime during idle polls.

    The HTTP login/session endpoints retain their existing renewal semantics.
    No password, token or cookie is emitted to a log by this module.
    """
    def __init__(self, session, store):
        self.username = session["username"]
        self.cookie = session["cookie"]
        self.database = getattr(store, "path", None)

    def _row(self, sql, args):
        import storage
        with storage.connection(self.database) as db:
            return db.execute(sql, args).fetchone()

    def active(self):
        row = self._row("SELECT username FROM sessions WHERE cookie=? AND exp>=? LIMIT 1",
                        (self.cookie, time.time()))
        return row is not None and secrets.compare_digest(row[0], self.username)

    def token_owner(self, token):
        row = self._row("SELECT username FROM sessions WHERE token=? AND exp>=? LIMIT 1",
                        (token, time.time()))
        return row[0] if row is not None else None

    def profile_owner(self, ident):
        row = self._row("SELECT username FROM characters WHERE profile_id=?", (ident,))
        return row[0] if row is not None else None


class NativeGuard:
    """Frame and constrain C->S bytes; retain native authentication decisions."""
    def __init__(self, role, lease):
        self.role = role
        self.lease = lease
        self.buffer = bytearray()
        self.role_started = False
        self.bound_profile = None
        self.enter_sent = False
        self.ready_sent = False
        self.sync_sent = False

    def feed(self, data):
        self.buffer.extend(data)
        out = []
        while len(self.buffer) >= 2:
            length, = struct.unpack_from("<H", self.buffer)
            if length < 8:
                raise WebSocketError("Invalid Sixcube frame length")
            if len(self.buffer) < length:
                break
            frame = bytes(self.buffer[:length])
            del self.buffer[:length]
            mid, = struct.unpack_from("<H", frame, 2)
            self._validate(mid, frame[8:])
            out.append(frame)
        return out

    def _validate(self, mid, body):
        if mid not in ALLOWED_MESSAGES[self.role]:
            raise WebSocketError("Message outside the HTML5 connection role", 1008)
        try:
            self._validate_body(mid, body)
        except WebSocketError:
            raise
        except (ValueError, UnicodeError, struct.error, IndexError) as exc:
            # Never disclose an account identifier or credential from input.
            raise WebSocketError("Invalid native message", 1008) from exc

    def _validate_body(self, mid, body):
        import characters
        if self.role == "AUTH":
            if mid == 52:
                # VersionCheckMessage: 3 u32 followed by one u16-length UTF-8 string.
                struct.unpack_from("<III", body)
                characters.read_string(body, 12)
                if b"curVersion" not in body:
                    raise WebSocketError("AUTH version discriminator required", 1008)
                self.role_started = True
                return
            if not self.role_started:
                raise WebSocketError("AUTH must start with its version request", 1008)
            values, pos = [], 1
            for _ in range(8):
                value, pos = characters.read_string(body, pos)
                values.append(value)
            struct.unpack_from("<I", body, pos)  # clientSid, present in the AS3 schema.
            username, token = values[:2]
            if not secrets.compare_digest(username, self.lease.username):
                raise WebSocketError("Web and native account mismatch", 1008)
            owner = self.lease.token_owner(token)
            if owner is not None and not secrets.compare_digest(owner, self.lease.username):
                raise WebSocketError("Web and native account mismatch", 1008)
            # An unknown/expired token still goes to characters.login: native S902
            # is the authoritative rejection and is useful to the HTML5 client.
            return

        if self.role == "GATE":
            if mid == 52:
                struct.unpack_from("<III", body)
                characters.read_string(body, 12)
                if b"curVersion" in body:
                    raise WebSocketError("AUTH discriminator on GATE connection", 1008)
                self.role_started = True
                return
            if mid == 61:
                self.role_started = True
                return
            if not self.role_started:
                # Native UNKNOWN+C54 means CHAT, not GATE.
                raise WebSocketError("GATE must start with C52 or C61", 1008)

        if mid == 54:
            ident, _ = characters.bind(body)
            owner = self.lease.profile_owner(ident)
            if owner is None or not secrets.compare_digest(owner, self.lease.username):
                raise WebSocketError("Character does not belong to the web session", 1008)
            if self.bound_profile is not None and self.bound_profile != ident:
                raise WebSocketError("Native identity cannot change on this connection", 1008)
            self.bound_profile = ident
            self.role_started = True
            return
        if self.bound_profile is None:
            raise WebSocketError("Native account must bind first", 1008)
        if self.role == "CHAT" and mid == 60:
            # SystemHeartBeat.writeToStream emits UINT64 lowpart then hipart.
            # Native CHAT ignores the clock value, but the HTML5 wire shape
            # must still match the verified AS3 schema.
            if len(body) != 8:
                raise WebSocketError("C60 requires its native u64 clock", 1008)
            struct.unpack("<II", body)
            return
        if self.role == "GATE":
            if mid == 56:
                # The current native server checks UUID and permits accountId=0
                # or this bound profile. Do not invent an ID from a message number.
                self.enter_sent = True
            elif mid == 72:
                if not self.enter_sent:
                    raise WebSocketError("C56 must precede C72", 1008)
                self.ready_sent = True
            elif mid == 165:
                if not self.ready_sent:
                    raise WebSocketError("C72 must precede C165", 1008)
                self.sync_sent = True
            else:
                if not self.sync_sent:
                    raise WebSocketError("Lobby synchronization must precede room actions", 1008)
                if mid in M8_MESSAGES:
                    self._validate_m8(mid, body)
                elif mid in (501, 503, 510, 514, 613):
                    self._validate_m4(mid, body)
                else:
                    self._validate_m3(mid, body)

    def _validate_m4(self, mid, body):
        """Constrain input shape, without interpreting or simulating gameplay.

        Sources: TankActions.handle / Notify battle requests in native AS3.
        The C503 target is an object ID, not an account or tank template ID.
        Valid ordinary C501 steps do not receive a positive position ACK.
        """
        lengths = {501: 9, 503: 13, 510: 12, 514: 8, 613: 0}
        if len(body) != lengths[mid]:
            raise ValueError("M4 payload length")
        if mid == 613:
            return  # Native handler checks that this account's match has ended.
        if mid == 501:
            _, _, direction, scene = struct.unpack("<HHBI", body)
            if direction > 8:
                raise ValueError("C501 direction")
        elif mid == 503:
            scene, _, slot, _, _ = struct.unpack("<IIBhh", body)
            # Native Guild uses slot4. Ownership, selected skill, control and
            # cooldown are still checked by the authenticated native actor.
            if slot not in (0, 1, 2, 4, 10):
                raise ValueError("C503 outside native tank/Guild inputs")
        else:
            scene, = struct.unpack_from("<I", body)
        if not scene:
            raise ValueError("Scene ID required")

    def _validate_m3(self, mid, body):
        """Validate wire shape/identity; the native server owns all game rules.

        Schema sources: protocol_m3_rooms.json and protocol_m3_scene.json.
        C215 is per-match selection. M8 C200 is a separate garage request.
        """
        import characters
        lengths = {215: 13, 509: 4, 512: 4, 552: 4, 601: 18, 602: 5, 605: 16,
                   607: 5, 610: 1, 611: 0, 614: 4}
        if mid == 600:
            struct.unpack_from("<BBII", body)
            _, pos = characters.read_string(body, 10)
            if len(body) != pos + 4:
                raise ValueError("C600 length")
            struct.unpack_from("<I", body, pos)
        elif mid not in lengths or len(body) != lengths[mid]:
            raise ValueError("M3 payload length")
        if mid == 215:
            scene, tank, container, _ = struct.unpack("<IIIB", body)
            if not scene or not tank or container != 0:
                raise ValueError("C215 scene/tank/container")
        elif mid == 610:
            if body[0] not in (1, 2, 9):
                raise ValueError('C610 room type')
        elif mid == 602:
            op, target = struct.unpack("<BI", body)
            if op not in (1, 2, 3, 4, 24) or target not in (0, self.bound_profile):
                raise ValueError("C602 operation")
        elif mid == 605:
            scene, account, _, loaded = struct.unpack("<IIII", body)
            if not scene or account != self.bound_profile or loaded > 100:
                raise ValueError("C605 identity/progress")
        elif mid in (509, 512, 552, 614):
            if not struct.unpack("<I", body)[0]:
                raise ValueError("Scene ID required")

    def _validate_m8(self, mid, body):
        """Bound the reviewed wire schema; never duplicate economy/game rules.

        Sources: tank_upgrade.py, tank_skins.py, cosmetic_shop.py,
        crystals.py, player_settings.py, guilds.py and guild_protocol.py.
        These are exact unpadded frames emitted by the HTML5 encoders.
        """
        if mid in (350, 556, 557):
            if body:
                raise ValueError('World Boss request must have an empty body')
            return
        if mid == 60002:
            from world_boss_store import validate_meta_request
            validate_meta_request(body)
            return
        if mid == 60000:
            # Explicit M29 HTML5-only mail-v1 extension, not guessed C271 ABI.
            # This parser checks framing/types only; GATE owns profile/economy.
            from offline_mail import decode_request
            decode_request(body)
            return
        if mid == 60006:
            from multiplayer_rooms import decode_request
            decode_request(body)
            return
        if mid == 60004:
            # M32 versioned HTML5 economy: native C151/156/162 stay unchanged.
            from economy_actions import decode_request
            decode_request(body)
            return
        if mid == 60008:
            # M45 shop-v1 has its own strict schema; C250 stays unchanged.
            from normal_shop import decode_request
            decode_request(body)
            return
        pos = 0

        def read(fmt):
            nonlocal pos
            values = struct.unpack_from('<' + fmt, body, pos)
            pos += struct.calcsize('<' + fmt)
            return values[0] if len(values) == 1 else values

        def string(limit=200, empty=False):
            nonlocal pos
            size = read('H')
            if size > limit or (not empty and not size) or pos + size > len(body):
                raise ValueError('M8 string size')
            value = body[pos:pos + size].decode('utf-8')
            pos += size
            return value

        def board():
            ident = read('I')
            if not 1 <= ident <= 255:
                raise ValueError('M8 board identifier')
            return ident

        if mid == 78:
            # RequestBuyThew: bounded native GoodsCoinVal vector. The server
            # owns the actual price, coin choice, balances and purchase phase.
            if read('H') != 1:
                raise ValueError('C78 coin vector size')
            coin, amount = read('Ii')
            if coin not in range(1, 7) or amount < 0:
                raise ValueError('C78 coin vector')
        elif mid==166:
            string(limit=200)  # Owned persistent UUID; no arbitrary object ID.
        elif mid in (156,157,158,159):
            slot=read('h')
            if not 0<=slot<135:raise ValueError('Helper bag slot')
            if mid==156:
                material=read('i')
                if not 0<=material<135:raise ValueError('Helper EXP slot')
            elif mid==159:
                other=read('h')
                if not 0<=other<135 or other==slot:raise ValueError('Helper transfer slot')
            elif mid==158:
                count=read('H')
                if count>3:raise ValueError('Helper material count')
                for _ in range(count):
                    material=read('h')
                    if not 0<=material<135:raise ValueError('Helper material slot')
        elif mid in (304,558):
            from pve_catalog import REQUEST_MAPS
            if read('I') not in REQUEST_MAPS:raise ValueError('Unsupported solo PvE map')
        elif mid in (200, 203):
            string()
        elif mid == 204:
            string()
            if read('I') != 0:
                raise ValueError('M8 strengthen material mode')
        elif mid == 207:
            string(); string(empty=True)
            if read('B') != 0:
                raise ValueError('M8 owned tank container')
        elif mid == 210:
            string(); read('I')
        elif mid == 153:
            container, slot = read('Ih')
            target = string(empty=True)
            count, auto = read('iB')
            # M8 sends the native self-target sentinel, never another UUID.
            if container not in (4, 7) or slot < (-1 if container == 7 else 0) or target or count != 1 or auto:
                raise ValueError('M8 self cosmetic request')
        elif mid == 250:
            kind, goods, coin, price, count, entries = read('iIIiiH')
            # Only the exact silver Guild donation can exceed the historical
            # shop count. Its 5M units cost 500M silver and yield 50M points;
            # all other purchases retain the existing 1000-unit bridge guard.
            limit = 5_000_000 if (kind, goods, coin, price) == (8, 408000002, 2, 100) else 1000
            if kind not in (1, 8) or not goods or coin not in (1, 2) or price < 0 or not 1 <= count <= limit or not 1 <= entries <= 6:
                raise ValueError('M8 supported purchase shape')
            seen = set()
            for _ in range(entries):
                currency, amount = read('Ii')
                if currency not in range(1, 7) or currency in seen or amount < 0:
                    raise ValueError('M8 coin vector')
                seen.add(currency)
        elif mid == 79:
            read('I')
        elif mid == 88:
            options = json.loads(string(16384))
            if not isinstance(options, dict) or any(key in options and not isinstance(options[key], list)
                                                    for key in ('skillSlot', 'keyBoardSet')):
                raise ValueError('M8 native character options')
        elif mid in (423, 652, 657, 660, 666):
            pass
        elif mid == 424:
            board()
            for _ in range(30):
                read('I')
        elif mid == 425:
            board(); name = string(128, empty=True)
            if len(name) > 32 or any(ord(c) < 32 for c in name):
                raise ValueError('M8 crystal board name')
        elif mid == 427:
            board(); string()
        elif mid == 426:
            kind, count = read('IH')
            if kind != 1 or not 1 <= count <= 1000:
                raise ValueError('M8 crystal sale vector')
            for _ in range(count):
                read('I')
        elif mid == 430:
            if read('I') not in (3, 4, 5, 6):
                raise ValueError('M8 crystal pack')
        elif mid == 155:
            count = read('H')
            if not 1 <= count <= 100:
                raise ValueError('M8 crystal recipe vector')
            for _ in range(count):
                _, amount = read('II')
                if not 1 <= amount <= 999:
                    raise ValueError('M8 crystal exchange amount')
            if read('H') != 0:
                raise ValueError('M8 crystal exchange uses fragments')
        elif mid == 75:
            name = string(96)
            if not name.strip() or read('i') != 0:
                raise ValueError('M813 character rename')
        elif mid in (68, 69, 76, 77):
            for _ in range(2 if mid == 69 else 1):
                name = string(12)
                if not name.strip() or any(ord(c) < 32 for c in name):
                    raise ValueError('M8 Guild/team name')
            if mid == 76 and not read('I'):
                raise ValueError('M8 Guild team')
        elif mid == 651:
            if not all(read('II')):
                raise ValueError('M8 Guild invitation')
        elif mid in (653, 654):
            string()
            if mid == 654 and read('i') not in (0, 5, 9, 10):
                raise ValueError('M8 Guild office')
            if not read('I'):
                raise ValueError('M8 Guild team')
        elif mid == 655:
            kind, value = read('ii')
            if not (kind == 1 and 0 <= value <= 7 or kind == 2 and 1 <= value <= 100 or kind == 3 and 0 <= value <= 100000):
                raise ValueError('M8 Guild recruitment setting')
        elif mid == 665:
            if not read('I'):
                raise ValueError('M8 Guild team')
        elif mid == 668:
            if not read('I'):
                raise ValueError('M8 origin team')
            string()
            if not read('I'):
                raise ValueError('M8 destination team')
            string(empty=True)
        elif mid in (669, 670):
            if not read('I'):
                raise ValueError('M8 Guild team')
            string()
        elif mid == 778:
            string()
            if not read('I'):
                raise ValueError('M8 Guild team')
        elif mid == 656:
            guild = read('I'); inviter = string(empty=True); fast = read('B')
            if not guild or inviter or fast not in (0, 1):
                raise ValueError('M8 direct Guild application')
        elif mid in (320, 672):
            if not read('I'):
                raise ValueError('M8 Guild skill group')
        elif mid in (321, 322):
            if read('I') not in (1, 5, 6, 7, 8):
                raise ValueError('M8 Guild skill slot')
            if mid == 322:
                read('II')
        elif mid in (662, 663):
            string(1000, empty=True)
        elif mid == 658:
            string()
            if not read('I'):
                raise ValueError('M8 Guild team')
        elif mid == 659:
            string()
        else:
            raise ValueError('Unreviewed M8 request')
        if pos != len(body):
            raise ValueError('M8 payload length')


def handle_websocket(handler, *, session, store):
    """Upgrade one authenticated request and relay to the fixed local TCP server."""
    if not session or not session.get("username") or not session.get("cookie"):
        handler.send_error(HTTPStatus.UNAUTHORIZED, "Login required")
        return
    if not _same_origin(handler):
        handler.send_error(HTTPStatus.FORBIDDEN, "Same-origin page required")
        return
    try:
        role = _requested_role(handler.path)
    except ValueError:
        handler.send_error(HTTPStatus.BAD_REQUEST, "Invalid HTML5 connection role")
        return
    if (handler.headers.get("Upgrade", "").lower() != "websocket"
            or "upgrade" not in {s.strip().lower() for s in handler.headers.get("Connection", "").split(",")}
            or handler.headers.get("Sec-WebSocket-Version") != "13"):
        handler.send_error(HTTPStatus.BAD_REQUEST, "WebSocket v13 upgrade required")
        return
    try:
        key = handler.headers.get("Sec-WebSocket-Key", "")
        if len(base64.b64decode(key, validate=True)) != 16:
            raise ValueError("Bad key")
    except (ValueError, base64.binascii.Error):
        handler.send_error(HTTPStatus.BAD_REQUEST, "Invalid websocket key")
        return
    lease = SessionLease(session, store)
    try:
        active = lease.active()
    except Exception:
        handler.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Session service unavailable")
        return
    if not active:
        handler.send_error(HTTPStatus.UNAUTHORIZED, "Login required")
        return
    try:
        native = socket.create_connection(TCP_DESTINATION, timeout=3)
        native.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        native.settimeout(min(1.0, SESSION_CHECK_INTERVAL))
    except OSError:
        handler.send_error(HTTPStatus.BAD_GATEWAY, "Native TCP service unavailable")
        return

    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    previous = handler.protocol_version
    handler.protocol_version = "HTTP/1.1"
    try:
        handler.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
    except OSError:
        native.close()
        return
    finally:
        handler.protocol_version = previous
    handler.close_connection = True
    handler.connection.settimeout(CLIENT_IDLE_TIMEOUT)
    write_lock = threading.Lock()
    closed = threading.Event()
    close_sent = False
    guard = NativeGuard(role, lease)

    def send_ws(data, opcode=2):
        nonlocal close_sent
        with write_lock:
            if close_sent:
                return
            if opcode == 8:
                close_sent = True
            handler.wfile.write(_pack_ws(data, opcode))
            handler.wfile.flush()

    def send_close(code, reason):
        try:
            send_ws(struct.pack("!H", code) + reason.encode("utf-8")[:100], 8)
        except OSError:
            pass

    def stop_browser():
        try:
            handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def from_native():
        next_check = time.monotonic() + SESSION_CHECK_INTERVAL
        code, reason = 1011, "Native connection closed"
        try:
            while not closed.is_set():
                now = time.monotonic()
                if now >= next_check:
                    if not lease.active():
                        code, reason = 1008, "Web session expired"
                        break
                    next_check = now + SESSION_CHECK_INTERVAL
                try:
                    data = native.recv(16384)
                except socket.timeout:
                    continue
                if not data:
                    break
                send_ws(data)
        except (OSError, EOFError):
            pass
        except Exception:
            code, reason = 1011, "Session service unavailable"
        finally:
            if not closed.is_set():
                send_close(code, reason)
                closed.set()
                stop_browser()

    thread = threading.Thread(target=from_native, name="bb-html5-" + role.lower(), daemon=True)
    thread.start()
    fragment = None
    try:
        while not closed.is_set():
            final, opcode, payload = _read_ws(handler.rfile)
            if opcode == 8:
                _validate_close(payload)
                send_ws(payload, 8)
                break
            if opcode == 9:
                send_ws(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                raise WebSocketError("Binary messages required", 1003)
            if opcode == 2:
                if fragment is not None:
                    raise WebSocketError("Continuation frame expected")
                if not final:
                    fragment = bytearray(payload)
                    continue
            elif opcode == 0:
                if fragment is None:
                    raise WebSocketError("Unexpected continuation frame")
                fragment.extend(payload)
                if len(fragment) > MAX_FRAME:
                    raise WebSocketError("WebSocket message too large", 1009)
                if not final:
                    continue
                payload, fragment = bytes(fragment), None
            if not lease.active():
                raise WebSocketError("Web session expired", 1008)
            for frame in guard.feed(payload):
                native.sendall(frame)
    except WebSocketError as exc:
        send_close(exc.code, str(exc))
    except socket.timeout:
        send_close(1001, "Connection idle timeout")
    except (EOFError, OSError):
        pass
    except Exception:
        send_close(1011, "Session service unavailable")
    finally:
        closed.set()
        try:
            native.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        native.close()
        thread.join(timeout=2)
