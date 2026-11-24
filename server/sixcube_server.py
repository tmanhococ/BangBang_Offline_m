"""Sixcube Full TCP Server for Bang Bang Private.

Handles all 3 roles on port 49119:
  1. AUTH socket (Version check 0x0034 + Login 0x0033 -> returns server list)
  2. GATE socket (Handshake + World Enter 0x0038 -> syncs Lobby, Tanks, Items)
  3. CHAT socket (Token bind 0x0036 -> Chat connect confirm 0x0DAC)
  4. In-band Policy probe (<policy-file-request/> -> sends Policy XML)
"""
from __future__ import annotations
from pve_catalog import MAPS as PVE_MAPS

import select
import socket
import struct
import sys
import threading
import time
import logging
from pathlib import Path
from contextlib import ExitStack

from policy import POLICY
from battle_entry import entry_packets, ensure_test_ally
from bot_setup import room_bots, configure_room, load_setup, BOT_MAPS, MODE_MAPS
from battle_runtime import BattleArena, SUPPORTED_TANKS
from combat_data import preload as preload_combat_data
from combat_geometry import map101_tank_bounds
import storage
import inventory
import tank_upgrade
import tank_skins
import tank_halos
import character_rename
import ranked_matches
import cosmetic_shop
import crystals
import boosters
import pve_progress
import pve_stamina
import offline_mail
import economy_actions
import normal_shop
import world_boss_store
import uuid
import military_rank
import prebattle_selection
import characters
import guilds
import guild_protocol
import guild_combat
import queue
import skin_effects
from storage_import import garage_payload, tank_instance_payload
from battle_persistence import dump_arena, restore_arena, resume_packets
from server_logging import get_logger

log = get_logger('sixcube')

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "data" / "packet_log"
LISTEN_PORT = 49119

# Global template frames from captured session entering lobby
GATE_TEMPLATES: list[bytes] = []


def _hex(data: bytes) -> str:
    return data.hex(" ")


def _dump(addr, direction: str, data: bytes, tag: str = "") -> None:
    # Skip parsing, hex conversion and disk access entirely outside debug mode.
    if not log.isEnabledFor(logging.DEBUG):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = f"[{tag}] " if tag else ""
    private = direction == 'C->S' and any(
        len(f) >= 8 and struct.unpack_from('<H', f, 2)[0] in (51, 54)
        for f in _pop_frames(bytearray(data)))
    rendered = '[login/session redacted]' if private else _hex(data)
    line = f"{stamp} {prefix}{direction} {addr} len={len(data)} {rendered}\n"
    preview = rendered[:140]
    log.debug('[sixcube] %s%s %s %sb %s', prefix, direction, addr, len(data), preview)
    try:
        with (LOG_DIR / "tcp.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        log.exception('[sixcube] Packet trace unavailable')


def _frame(msg_id: int, payload: bytes = b"") -> bytes:
    inner = struct.pack("<H", msg_id) + struct.pack("<I", 0) + payload
    return struct.pack("<H", 2 + len(inner)) + inner


def _record_error(addr, role, exc, stage="error"):
    """Keep disconnect evidence even when a manually opened console scrolls away."""
    log.error('[sixcube] %s [%s] %s: %s: %s', addr, role, stage,
              type(exc).__name__, exc, exc_info=(type(exc), exc, exc.__traceback__))


def _pstr(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def _advertise_host() -> str:
    return storage.setting('advertise_host')


def make_tick(client_time: int = 0) -> bytes:
    # NotifyPingGateSvr is server UTC milliseconds (UINT64), then UTC seconds.
    # The old client_time/416 pair made every epoch-based skill cooldown wrong.
    now_ms = time.time_ns() // 1_000_000
    payload = struct.pack("<QI", now_ms, now_ms // 1000)
    return _frame(0x03EB, payload)


def build_server_list(host: str, port: int) -> bytes:
    sid = 299001
    now = int(time.time()) & 0xFFFFFFFF
    payload = (
        struct.pack("<H", 1)
        + struct.pack("<I", sid)
        + _pstr("Private-01")
        + _pstr(host)
        + struct.pack("<H", port)
        + _pstr(host)
        + struct.pack("<H", port)
        + b"\x00\x00"
        + struct.pack("<I", now)
        + b"\x00\x00\x00\x00"
    )
    return _frame(0x038A, payload)


# ==============================================================================
# TANK DATABASE & ROOM SERIALIZERS
# ==============================================================================
TANKS_DB: dict[str, dict] = {}


def load_tank_database() -> dict[str, dict]:
    global TANKS_DB
    preload_combat_data()
    with storage.connection() as db:
        skin_effects.preload(db)
    map101_tank_bounds()  # Cache SQLite wall data before the first movement/skill.
    tanks = {}
    for record in storage.garage():
        tanks[record['uuid']] = tanks[str(record['tank_id'])] = record
    TANKS_DB = tanks
    return tanks


def serialize_player_brief(
    acct_id: int = 30000791,
    char_uuid: str = "multiplayer-char-30000791",
    name: str = "Vua36",
    level: int = 36,
    sex: int = 1,
    vip_level: int = 6,
    elo_section: int = 1,
    pre_elo_section: int = 1,
    avatar: int = 0,
    decorate_id: int = 0,
    graduate: int = 0,
    elo: int = 1200,
    honor_id: int = 0,
    booster_attack: int = 0,
    military_star: int = 0,
    team_place: int = 0,
    team_name: str = "",
) -> bytes:
    return (
        struct.pack("<I", acct_id)
        + _pstr(char_uuid)
        + _pstr(name)
        + struct.pack("<BBBBB", level, sex, vip_level, elo_section, pre_elo_section)
        + struct.pack("<II", avatar, decorate_id)
        + struct.pack("<Hh", graduate, elo)
        + struct.pack("<IIII", honor_id, booster_attack, military_star, team_place)
        + _pstr(team_name)
    )


def serialize_other_brief(
    tank_id: int = 10000056,
    tank_level: int = 5,
    try_tank: int = 0,
    bt_item_id: int = 0,
    bt_rank: int = 0,
    bt_level: int = 0,
    bt_up_level: int = 0,
    bt_up_cost: int = 0,
    bt_rank_cost: int = 0,
    honor_id: int = 0,
    tank_external: int = 395,
    halo_id: int = 0,
    skin_frame_type: int = 0,
    bt_atk: int = 0,
    tank_part_score: int = 0,
    command_skill: int = 0,
    spray_id: int = 0,
) -> bytes:
    # Kill banners call initFromExternalId directly and do not resolve zero,
    # unlike garage/scoreboard avatars. Resolve only the wire appearance.
    if not tank_external:
        from combat_data import table_row
        tank_external=int(table_row('TankUpTemplate_table.xml',nTankId=tank_id,nLevel=tank_level)['nExternal'])
    return (
        struct.pack("<I", tank_id)
        + struct.pack("<BB", tank_level, try_tank)
        + struct.pack("<I", bt_item_id)
        + struct.pack("<BBB", bt_rank, bt_level, bt_up_level)
        + struct.pack("<III", bt_up_cost, bt_rank_cost, honor_id)
        + struct.pack("<II", tank_external, halo_id)
        + struct.pack("<B", skin_frame_type)
        + struct.pack("<IIII", bt_atk, tank_part_score, command_skill, spray_id)
    )


def serialize_char_brief(
    player_brief: bytes,
    other_brief: bytes,
    region_uid: int = 299001,
    ip: int = 0,
    camp: int = 1,
    buff_vec: list[int] | None = None,
) -> bytes:
    if buff_vec is None:
        buff_vec = []
    b = (
        player_brief
        + other_brief
        + struct.pack("<IIB", region_uid, ip, camp)
        + struct.pack("<H", len(buff_vec))
    )
    for buff_id in buff_vec:
        b += struct.pack("<I", buff_id)
    return b


def serialize_room_char_brief(
    char_brief: bytes,
    ready: int = 1,
    battle: int = 0,
) -> bytes:
    return struct.pack("<BB", ready, battle) + char_brief


def serialize_room_config(
    map_id: int = 101,
    ban_robot: int = 0,
    tank_avail_level: int = 0,
    ladder_min: int = 0,
    ladder_max: int = 0,
) -> bytes:
    return struct.pack("<IBBBB", map_id, ban_robot, tank_avail_level, ladder_min, ladder_max)


def serialize_room_info(
    room_id: int = 1001,
    num: int = 1,
    max_num: int = 8,
    map_id: int = 101,
    mode: int = 1,
    state: int = 0,
    is_validate: int = 1,
    room_type: int = 1,
    name: str = "PhÃ²ng cá»§a Vua36",
    team_id: int = 0,
) -> bytes:
    return (
        struct.pack("<IIIIIBBB", room_id, num, max_num, map_id, mode, state, is_validate, room_type)
        + _pstr(name)
        + struct.pack("<I", team_id)
    )


def make_hall_display_list(
    rooms: list[bytes] | None = None,
    scenes: list[bytes] | None = None,
    site: int = 0,
    room_type: int = 1,
) -> bytes:
    if rooms is None:
        rooms = []
    if scenes is None:
        scenes = []
    payload = (
        struct.pack("<H", len(rooms))
        + b"".join(rooms)
        + struct.pack("<H", len(scenes))
        + b"".join(scenes)
        + struct.pack("<iB", site, room_type)
    )
    return _frame(0x06BC, payload)


def make_enter_room_ok(
    room_id: int = 1001,
    create_id: int = 30000791,
    max_num: int = 10,
    min_num: int = 2,
    ob_num: int = 2,
    password: int = 0,
    name: str = "Phong 5v5 [Bot Gundam]",
    state: int = 0,
    room_type: int = 1,
    mode: int = 1,
    room_config: bytes | None = None,
    char_vec: list[bytes] | None = None,
    observer_vec: list[bytes] | None = None,
) -> bytes:
    if room_config is None:
        room_config = serialize_room_config()
    if char_vec is None:
        char_vec = []
    if observer_vec is None:
        observer_vec = []
    payload = (
        struct.pack("<II", room_id, create_id)
        + struct.pack("<BBB", max_num, min_num, ob_num)
        + struct.pack("<I", password)
        + _pstr(name)
        + struct.pack("<BB", state, room_type)
        + struct.pack("<I", mode)
        + room_config
        + struct.pack("<H", len(char_vec))
        + b"".join(char_vec)
        + struct.pack("<H", len(observer_vec))
        + b"".join(observer_vec)
    )
    return _frame(0x06A4, payload)


def make_room_char_change(player_brief: bytes, other_brief: bytes) -> bytes:
    return _frame(0x06AF, player_brief + other_brief)


def make_room_op(room_op: int, start_oid: int, target: int) -> bytes:
    return _frame(0x06A5, struct.pack("<BII", room_op, start_oid, target))


def make_loaded_rate(tank_id: int, delay: int = 15, loaded: int = 100) -> bytes:
    # MessageType.MSG_NOTIFY_LOADED_RATE = 1709 (1706 is MIDWAY_JOIN).
    return _frame(0x06AD, struct.pack("<III", tank_id, delay, loaded))


def make_room_map(map_id: int) -> bytes:
    return _frame(0x06A8, struct.pack("<I", map_id))


def make_start_game(
    scene_id: int = 1001,
    map_id: int = 101,
    room_type: int = 1,
    char_data: list[bytes] | None = None,
    back_data: list[int] | None = None,
    enter_type: int = 0,
) -> bytes:
    """NotifyStartGame (0x06A9 / 1705): transition client into battle scene."""
    if char_data is None:
        char_data = []
    if back_data is None:
        back_data = []
    payload = (
        struct.pack("<IIB", scene_id, map_id, room_type)
        + struct.pack("<H", len(char_data))
        + b"".join(char_data)
        + struct.pack("<H", len(back_data))
    )
    for b_item in back_data:
        payload += struct.pack("<I", b_item)
    payload += struct.pack("<B", enter_type)
    return _frame(0x06A9, payload)




def get_room_char_briefs(session_state: dict) -> tuple[list[bytes], list[bytes]]:
    """Generate serialized CHAR_BRIEF and ROOM_CHAR_BRIEF lists for player and bot."""
    r = session_state.get("room")
    player_camp = r.get("camp", 1) if r else 1
    player_ready = r.get("ready", 1) if r else 1

    pb_player = serialize_player_brief(
        acct_id=session_state["char_id"],
        char_uuid=session_state["char_uuid"],
        name=session_state["name"],
        level=session_state["level"],
        vip_level=session_state["vip_level"],
        avatar=session_state.get('avatar', 0),
        decorate_id=session_state.get('decorate_id', 0),
        military_star=session_state.get('military_star', 0),
        elo=session_state.get('elo',1000), elo_section=session_state.get('elo_section',1),
        pre_elo_section=session_state.get('pre_elo_section',1),
        booster_attack=session_state.get('booster_attack',0),
    )
    ob_player = serialize_other_brief(
        tank_id=session_state["tank_id"],
        tank_level=session_state["tank_level"],
        tank_external=session_state["tank_ext"],
        halo_id=session_state["halo_id"],
        skin_frame_type=session_state["skin_frame_type"],
        command_skill=guild_combat.command(session_state.get('guild_build',{})),
        **boosters.other_brief(session_state),
    )
    guild_buffs=[v['skill'] for k,v in session_state.get('guild_build',{}).items() if k!='1']
    cb_player = serialize_char_brief(pb_player, ob_player, camp=player_camp,buff_vec=guild_buffs)
    rcb_player = serialize_room_char_brief(cb_player, ready=player_ready)

    cbs = [cb_player]
    rcbs = [rcb_player]
    if r and r.get('room_type')==4:
        return cbs,rcbs

    if not r or r['map_id'] not in BOT_MAPS:ensure_test_ally(session_state)
    for bot in room_bots(session_state):
        if not bot or not bot.get('in_room',True):
            continue
        bot_camp = player_camp if bot.get('side')=='allies' or bot is session_state.get('ally_bot') else 3-player_camp
        bot["camp"] = bot_camp
        pb_bot = serialize_player_brief(
            acct_id=bot["acct_id"],
            char_uuid=bot["char_uuid"],
            name=bot["name"],
            level=36,
            vip_level=6,
            avatar=bot.get('avatar',0),
            decorate_id=bot.get('decorate_id',0),
            military_star=bot.get('military_star',0),
            elo=bot.get('elo',1000), elo_section=bot.get('elo_section',1),
            pre_elo_section=bot.get('pre_elo_section',1),
        )
        ob_bot = serialize_other_brief(
            tank_id=bot["tank_id"],
            tank_level=bot["tank_level"],
            tank_external=bot.get("tank_ext", 0),
            halo_id=bot.get("halo_id", 0),
            skin_frame_type=1,
            command_skill=guild_combat.command(bot.get('guild',{})),
        )
        cb_bot = serialize_char_brief(pb_bot, ob_bot, camp=bot_camp,
            buff_vec=[v['skill'] for k,v in bot.get('guild',{}).items() if k!='1'])
        rcb_bot = serialize_room_char_brief(cb_bot, ready=bot.get("ready", 1))
        cbs.append(cb_bot)
        rcbs.append(rcb_bot)

    return cbs, rcbs


def load_templates() -> list[bytes]:
    inventory.initialize()
    cosmetic_shop.initialize()
    crystals.initialize()
    frames = storage.lobby_frames()
    garage_body = garage_payload(storage.garage(),bytes.fromhex(storage.setting('garage_trailer')))
    return [_frame(1150,garage_body) if struct.unpack_from('<H',frame,2)[0]==1150 else frame
            for frame in frames]


def garage_for_state(state):
    records = storage.garage()
    selected = state.get('selected_tank_uuid') or next(
        (r['uuid'] for r in records if r['tank_id']==state['tank_id']),None)
    for record in records:
        # Original TANK_EXT_FLAG_BIT.TEF_JOIN_BATTLE = 1 << 1.
        record['ext_flag'] = (record['ext_flag'] & ~2) | (2 if record['uuid']==selected else 0)
    return records


def _template_batch(frames: list[bytes], state=None) -> bytes:
    # Refresh live garage data; omit ping replies from the captured session.
    result = []
    items = None
    from player_settings import snapshot
    preferences = snapshot() if any(struct.unpack_from('<H',f,2)[0] in (1036,1047) for f in frames) else {}
    appearance = cosmetic_shop.snapshot() if any(struct.unpack_from('<H',f,2)[0] in (1016,1023) for f in frames) else None
    rank_stars = None
    thew_profile = None
    if any(struct.unpack_from('<H', f, 2)[0] == 1016 for f in frames):
        with storage.connection() as db:
            rank_stars = military_rank.stars(db)
            thew_profile = pve_stamina.snapshot(db)
        if state is not None:
            state['military_star'] = rank_stars
    if appearance is not None and state is not None:
        state.update(avatar=appearance['avatar'], decorate_id=appearance['decorate'])
    for fr in frames:
        mid = struct.unpack_from('<H',fr,2)[0]
        if mid==1003:
            # PingManager computes RTT from its latest C61. A captured reply
            # belongs to the old session, never to this lobby sync request.
            continue
        elif mid in (1016,1023):
            fr = _frame(mid, cosmetic_shop.login_payload(mid, fr[8:], appearance))
            if mid == 1016:
                fr = _frame(mid, military_rank.login_payload(fr[8:], rank_stars))
                fr = _frame(mid, pve_stamina.login_payload(fr[8:], thew_profile))
        elif mid==1150 and state is not None:
            records = garage_for_state(state)
            fr = _frame(1150,garage_payload(records,bytes.fromhex(storage.setting('garage_trailer'))))
        elif mid==1100:
            if items is None:
                items = inventory.load()
            container, = struct.unpack_from('<I',fr,8)
            fr = _frame(1100,inventory.list_payload(container,items.get(str(container),[])))
        elif mid==1303:
            continue  # Captured progress never belongs to the current profile.
        elif mid in preferences:
            fr = _frame(mid,preferences[mid])
        if state is not None and 'char_id' in state:
            fr = _frame(mid, characters.login_payload(mid, fr[8:], state))
        result.append(fr)
        if mid == 1016 and state is not None:
            result.extend(_frame(m,b) for m,b in ranked_matches.update_packets(state))
        if mid == 1100 and container == 4:
            # Captured login predates the background bag. Sync it alongside the closet.
            result.append(_frame(1100, inventory.list_payload(7, items.get('7', []))))
            with storage.connection() as db:
                result.extend(_frame(mid, body) for mid, body in crystals.login_packets(db))
                result.extend(_frame(mid, body) for mid, body in boosters.login_packets(db))
                result.extend(_frame(mid, body) for mid, body in pve_progress.city_packets(db))
    return b''.join(result)


def _pop_frames(buf: bytearray) -> list[bytes]:
    out = []
    while len(buf) >= 4:
        if buf.startswith(b"<policy-file-request"):
            z = buf.find(b"\x00")
            if z < 0:
                break
            out.append(bytes(buf[: z + 1]))
            del buf[: z + 1]
            continue
        length = struct.unpack_from("<H", buf, 0)[0]
        if length < 4 or length > 1_000_000:
            del buf[0]
            continue
        if len(buf) < length:
            break
        out.append(bytes(buf[:length]))
        del buf[:length]
    return out


def _tune(conn: socket.socket) -> None:
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


def _client_handler(conn: socket.socket, addr) -> None:
    _tune(conn)
    conn.settimeout(0.5)
    buf = bytearray()
    role = "UNKNOWN"
    saw_init_net = False
    session_state, saved_arena = {'battle': None}, None
    owns_world = False
    world_lock = None
    tanks_db = {}
    resume_sent = False
    world_connections = ExitStack()
    world_db = None
    guild_inbox = None
    mail_inbox = None
    mail_extension = False  # Negotiated by a valid C60000 v1, never persisted.
    economy_context = economy_actions.session()  # Ephemeral GATE generation, never checkpointed.
    shop_context = normal_shop.session()  # Separate proof secret; never checkpointed.
    selection_deadline = None
    multiplayer_gate = None
    pending_pve_sync = []
    boss_ready = False
    boss_ui_open = False
    boss_metadata = False  # Valid C60002 v1 opt-in only; reset each GATE session.
    boss_notice_at = 0
    boss_notice_state = None

    def checkpoint():
        nonlocal saved_arena
        if not owns_world:
            return
        if session_state.get('multiplayer_room_id'):
            return  # The shared worker alone owns this profile's live world checkpoint.
        battle = session_state['battle']
        arena = battle.get('actions') if battle else None
        if arena:
            saved_arena = dump_arena(arena)
        if battle is None:
            saved_arena = None
        if session_state.get('tank_selection') and selection_deadline is not None:
            session_state['tank_selection']['remaining'] = max(0, selection_deadline - time.monotonic())
        if arena and arena.geometry.id == world_boss_store.MAP:
            storage.save_world(session_state,saved_arena,db=world_db,
                before_write=lambda db:world_boss_store.checkpoint(db,session_state,arena))
        elif arena and arena.geometry.id in PVE_MAPS:
            # PvE rewards and its snapshot commit together; never write PvP rank
            # history for a solo run or replace the existing ranked transaction.
            pending_pve_sync.extend(storage.save_world(session_state,saved_arena,db=world_db,
                before_write=lambda db:pve_progress.complete(db,session_state,arena)) or [])
        else:
            ranked_matches.checkpoint(session_state,arena,saved_arena,world_db)

    def send_actions(packets):
        if pending_pve_sync:
            packets=list(pending_pve_sync)+list(packets)
            pending_pve_sync.clear()
        arena = (session_state.get('battle') or {}).get('actions')
        if arena and arena.geometry.id == world_boss_store.MAP and arena.pk['world_boss']['energy']>=8:
            # Native E timers keep progressing; the temporary E owns its HUD.
            packets=[(mid,body) for mid,body in packets if not
                     (mid==1572 and len(body)>=26 and body[4]==1
                      and struct.unpack_from('<I',body,22)[0]==arena.player.id)]
        if arena and arena.geometry.id not in PVE_MAPS and any(mid==1708 for mid,_ in packets):
            battle = session_state.get('battle')
            reward_pkts = []
            if battle and not battle.get('rewards_applied'):
                import match_rewards
                try:
                    if not world_db.in_transaction:
                        world_db.execute('BEGIN IMMEDIATE')
                    _, reward_pkts = match_rewards.apply_pvp_rewards(session_state, arena, world_db)
                    world_db.commit()
                    battle['rewards_applied'] = True
                except Exception as exc:
                    world_db.rollback()
                    log.warning('[sixcube] PvP rewards delivery error: %s', exc)
                    reward_pkts = []
            from pk_rules import settlement
            packets=[settlement(arena) if mid==1708 else (mid,body) for mid,body in packets]
            packets+=reward_pkts
            packets+=ranked_matches.update_packets(session_state)
        frames=[_frame(msg_id,body) for msg_id,body in skin_effects.present_packets(packets,session_state,arena)]
        if frames:
            # TCP already carries concatenated frames. Keep every packet and
            # its order, but avoid a socket write/file open/flush per tiny event.
            data=b''.join(frames)
            conn.sendall(data)
            _dump(addr, "S->C", data, tag="COMBAT-ACTION")

    def tick_actions():
        if multiplayer_gate is not None:
            multiplayer_gate.poll()
        sync_world_boss()
        if selection_deadline is not None and time.monotonic() >= selection_deadline:
            finish_selection()
        if mail_inbox is not None:
            try:
                mail_inbox.get_nowait()
            except queue.Empty:
                pass
            else:
                data = b''.join(_frame(mid, body) for mid, body in offline_mail.changed_packets(world_db, include_extension=mail_extension))
                conn.sendall(data)
        if guild_inbox is not None:
            while True:
                try:
                    notifications = guild_inbox.get_nowait()
                except queue.Empty:
                    break
                data = b''.join(_frame(mid, body) for mid, body in notifications)
                if data:
                    conn.sendall(data)
                    _dump(addr, 'S->C', data, tag='GUILD-NOTIFY')
        battle = session_state["battle"]
        if battle and battle.get("entered") and battle.get("actions"):
            packets = battle["actions"].tick()
            checkpoint()
            send_actions(packets)

    def deliver_boss_rewards():
        # Reuse the GATE owner connection, commit bag + receipt together, and
        # ensure every reply is encodable before commit. Never grant from HTTP.
        try:
            world_db.execute('BEGIN IMMEDIATE')
            # If no client polled during an offline timeout, create the
            # round's pending receipts before this login delivery attempt.
            # Closing the round, granting the bag and its receipt are atomic.
            world_boss_store.advance(world_db)
            packets=world_boss_store.deliver(world_db)
            for mid,body in packets:
                _frame(mid,body)
            world_db.commit()
            return packets
        except BaseException:
            world_db.rollback()
            raise

    def sync_world_boss(force=False):
        nonlocal boss_notice_at, boss_notice_state
        if not boss_ready or not owns_world:
            return
        now=world_boss_store.now_ms()
        battle=session_state.get('battle') or {}
        arena=battle.get('actions')
        in_boss=bool(arena and arena.geometry.id==world_boss_store.MAP)
        if not force and now<boss_notice_at and not in_boss:
            return
        row=world_boss_store.poll(world_db,now)
        mark=(row['id'],row['closed_ms'] is not None)
        packets=[]
        notify=force or mark!=boss_notice_state or now>=boss_notice_at and (boss_ui_open or in_boss or boss_metadata)
        if notify:
            packets+=world_boss_store.packets(world_db,row,transition=mark!=boss_notice_state)
        if mark!=boss_notice_state and boss_notice_state is not None:
            # A participant can already be back in the hall when another
            # arena finishes the boss. Deliver there too, not only on login.
            packets+=deliver_boss_rewards()
        if notify and boss_metadata:
            packets.append(world_boss_store.metadata(world_db,row,now))
        if now>=boss_notice_at or force:
            boss_notice_at=now+1000
        boss_notice_state=mark
        if in_boss:
            from world_boss_mode import synchronize
            # S1952 marks the activity inactive before GateManager exits the
            # map. Open the native result only afterwards, so changing to MAIN
            # cannot dismiss a result window that was just opened by S1951.
            closing=[p for p in packets if p[0]==1951 and struct.unpack_from('<i',p[1],4)[0]==3]
            packets=[p for p in packets if p not in closing]
            packets+=synchronize(arena,row,now)+closing
        if packets:send_actions(packets)

    def retire_world_boss():
        room=session_state.get('room') or {}
        if room.get('map_id')!=world_boss_store.MAP:return
        battle=session_state.get('battle') or {}
        arena=battle.get('actions')
        meta=battle.get('world_boss') or {}
        row=world_boss_store.poll(world_db)
        closed=row['id']!=meta.get('round_id') or row['closed_ms'] is not None
        if not closed and not (arena and arena.pk['finished']):return
        checkpoint()
        clear_match_selection()
        session_state['battle']=session_state['room']=None
        conn.settimeout(0.5)
        checkpoint()

    def enter_world_boss():
        retire_world_boss()
        retire_finished_solo_pve()
        if session_state.get('battle') or session_state.get('tank_selection'):return
        row=world_boss_store.poll(world_db)
        if row['closed_ms'] is not None:
            send_actions([cosmetic_shop.tip(350,'Boss đang nghỉ. Đợt tiếp theo mở sau tối đa 60 giây.')])
            return
        session_state['room']=dict(room_id=1001,name='Boss Thế Giới — Kẻ Hủy Diệt',
            map_id=world_boss_store.MAP,mode=1,room_type=7,state=0,camp=1,ready=1,max_num=1,min_num=1,ob_num=0)
        send_start_battle()

    def ensure_room():
        if session_state["room"] is None:
            session_state["room"] = {
                "room_id": 1001,
                "name": "Phong 5v5 [Bot Gundam]",
                "map_id": 101,
                "mode": 1,
                "room_type": 1,
                "state": 1,
                "camp": 1,
                "ready": 1,
                "max_num": 10,
                "min_num": 2,
                "ob_num": 2,
            }
            configure_room(session_state,world_db)

    def send_selection(resuming=False):
        nonlocal selection_deadline
        room=session_state.get('room') or {}
        if room.get('room_type')==4 and room.get('map_id') in PVE_MAPS:
            retire_finished_solo_pve(keep_room=True)
            send_start_battle()
            return
        if session_state.get('battle'):
            return
        ensure_room()
        r = session_state['room']
        if session_state.get('tank_selection') and not resuming:
            return  # Repeated matchmaking/ready must not reset the timer.
        if not resuming:
            prebattle_selection.begin(session_state, storage.garage())
        if r['map_id'] in BOT_MAPS:
            from bot_setup import unique_room_roster
            unique_room_roster(session_state)
            from bot_cosmetics import prepare_selection
            prepare_selection(session_state,resuming=resuming)
            r['room_type'] = 9 if r.get('ranked') else 2
        if not resuming:
            ranked_matches.register_selection(session_state,world_db)
        session_state['guild_build'] = guilds.battle_build(world_db)
        selection_deadline = time.monotonic() + session_state['tank_selection']['remaining']
        cbs, _ = get_room_char_briefs(session_state)
        checkpoint()
        packet = _frame(1721, prebattle_selection.pair_payload(
            session_state, cbs, [b['acct_id'] for b in room_bots(session_state)]))
        conn.sendall(packet)
        _dump(addr, 'S->C', packet, tag='GATE-SELECT-TANK')

    def notify_selection(changed_bots=()):
        if multiplayer_gate is not None and multiplayer_gate.selection_updated():
            return
        # S1711 refreshes level/name; S1727 refreshes the chosen head/skin.
        subjects = [dict(acct_id=session_state['char_id'], char_uuid=session_state['char_uuid'],
                         name=session_state['name'], tank_id=session_state['tank_id'],
                         tank_level=session_state['tank_level'], tank_ext=session_state['tank_ext'],
                         halo_id=session_state.get('halo_id', 0),
                         skin_frame_type=session_state.get('skin_frame_type', 0),
                         command_skill=guild_combat.command(session_state.get('guild_build', {})),
                         level=session_state['level'], vip_level=session_state['vip_level'],
                         avatar=session_state.get('avatar', 0), decorate_id=session_state.get('decorate_id', 0),
                         military_star=session_state.get('military_star', 0),
                         elo=session_state.get('elo', 1000), elo_section=session_state.get('elo_section', 1),
                         pre_elo_section=session_state.get('pre_elo_section', 1),
                         booster_attack=session_state.get('booster_attack',0))] + list(changed_bots)
        packets = []
        if (changed_bots and session_state.get('tank_selection')
                and (session_state.get('room') or {}).get('ranked')
                and session_state.get('rank_bot_roster')):
            # S1711 only updates an already-known account. A permanent reserve
            # has its own identity, so first replace the full selection roster.
            # C215 checkpoint already refreshed remaining from the unchanged
            # deadline; do not begin a new selection or reset its timer here.
            cbs, _ = get_room_char_briefs(session_state)
            packets.append(_frame(1721, prebattle_selection.pair_payload(
                session_state, cbs, [bot['acct_id'] for bot in room_bots(session_state)])))
        for subject in subjects:
            command = subject.get('command_skill', 0)
            skin_frame = subject.get('skin_frame_type', 1)
            pb = serialize_player_brief(**{key: subject[key] for key in
                ('acct_id', 'char_uuid', 'name', 'level', 'vip_level', 'avatar', 'decorate_id', 'military_star',
                 'elo', 'elo_section', 'pre_elo_section', 'booster_attack')
                if key in subject})
            ob = serialize_other_brief(tank_id=subject['tank_id'], tank_level=subject['tank_level'],
                tank_external=subject.get('tank_ext', 0), halo_id=subject.get('halo_id', 0),
                skin_frame_type=skin_frame, command_skill=command,
                **(boosters.other_brief(session_state,world_db)
                   if subject['acct_id']==session_state['char_id'] else {}))
            packets.append(make_room_char_change(pb, ob))
            packets.append(_frame(1727, prebattle_selection.selected_payload(subject['acct_id'],
                subject['tank_id'], struct.unpack_from('<I', ob, 25)[0], skin_frame, command)))
        data = b''.join(packets)
        conn.sendall(data)
        _dump(addr, 'S->C', data, tag='GATE-TANK-SELECTED')

    def finish_selection():
        nonlocal selection_deadline
        if not session_state.get('tank_selection') or session_state.get('battle'):
            return
        selection_deadline = None
        session_state.pop('tank_selection')
        locked = _frame(1728, struct.pack('<III', session_state['char_id'], session_state['tank_id'], 0))
        # Commit the battle before sending lock/start: reconnect cannot replay a
        # confirmed selection. The per-match UUID remains through battle resume.
        send_start_battle(prefix=locked)

    def clear_match_selection():
        nonlocal selection_deadline
        selection_deadline = None
        prebattle_selection.restore_primary(session_state, storage.garage())

    def send_start_battle(resuming=False, prefix=b''):
        ensure_room()
        r = session_state["room"]
        if session_state["battle"] is not None and not resuming:
            return  # Ignore duplicate start requests during load or battle.
        if r['map_id'] in BOT_MAPS:
            from bot_setup import unique_room_roster
            unique_room_roster(session_state)
            r['room_type']=9 if r.get('ranked') else 2  # Native ranked or normal PvP.
        r["state"] = 1  # RST_BATTLE
        if not resuming:
            session_state['guild_build']=guilds.battle_build(world_db)
            session_state['booster_build']=boosters.battle_build(world_db,r)
            session_state["battle"] = {"scene_id": r["room_id"], "loaded": 0, "entered": False}
            from bot_policy import prepare_match
            prepare_match(session_state)
            if r.get('room_type')==4:
                session_state['battle']['pve_run']=uuid.uuid4().hex
                pve_progress.prepare_run(session_state,world_db)
            elif r.get('room_type')==7 and r['map_id']==world_boss_store.MAP:
                row=world_boss_store.poll(world_db)
                if row['closed_ms'] is not None:
                    session_state['battle']=None
                    r['state']=0
                    send_actions([cosmetic_shop.tip(350,'Boss đang nghỉ. Hãy chờ đợt tiếp theo.')])
                    return
                session_state['battle']['world_boss']=dict(round_id=row['id'],run=uuid.uuid4().hex,
                    hp=row['hp'],remaining_ms=max(0,row['end_ms']-world_boss_store.now_ms()))
            session_state["bot"]["hp"] = session_state["bot"]["max_hp"]
        ranked_matches.start_metadata(session_state)
        if not resuming:
            ranked_matches.register_selection(session_state,world_db)
        cbs, _ = get_room_char_briefs(session_state)
        f_start = make_start_game(
            scene_id=r["room_id"],
            map_id=r["map_id"],
            room_type=r["room_type"],
            char_data=cbs,
            back_data=[],
            enter_type=0,
        )
        checkpoint()
        conn.sendall(prefix + f_start)
        _dump(addr, "S->C", prefix + f_start, tag="GATE-START-GAME")
        log.info(
            f"[sixcube] {addr} [GATE] Loading battle scene {r['room_id']}, map {r['map_id']}, {len(cbs)} fighters (not entered yet)",
        )

    def retire_finished_solo_pve(*,restore_saved=False,keep_room=False):
        room=session_state.get('room') or {}
        if not boosters.is_pve(room):return
        battle=session_state.get('battle')
        actions=battle.get('actions') if battle else None
        if battle and not actions and restore_saved and saved_arena:
            from battle_persistence import decode
            if not decode(saved_arena).get('pk',{}).get('finished'):return
            actions=battle['actions']=restore_arena(session_state,saved_arena)
        if not actions or actions.geometry.id not in PVE_MAPS or not actions.pk or not actions.pk['finished']:return
        checkpoint()  # Result/reward is durable before its run ID is retired.
        clear_match_selection()
        session_state['battle']=None
        if keep_room:room['state']=0
        else:session_state['room']=None
        session_state['bot']['in_room']=True
        ensure_test_ally(session_state)['in_room']=True
        conn.settimeout(0.5)
        checkpoint()
        send_actions([])

    def enter_solo_pve(map_id,request_id):
        retire_finished_solo_pve()
        if session_state.get('battle') or session_state.get('tank_selection'):return
        from pve_catalog import solo_room
        from pve_balance import profile_for_room
        try:
            room = solo_room(map_id)
            # Refuse incomplete static config before sending room/start ACK.
            # The actual new battle freezes a fresh profile in prepare_run.
            profile_for_room(room)
        except ValueError as exc:
            send_actions([cosmetic_shop.tip(request_id,str(exc))])
            return
        if not pve_progress.unlocked(pve_progress.profile(world_db),map_id):
            message = ('Hãy hoàn thành bản thường của màn này để mở bản khó.'
                       if room['pve_difficulty'] else 'Hãy hoàn thành màn trước để mở màn này.')
            send_actions([cosmetic_shop.tip(request_id,message)])
            return
        session_state['room'] = room
        _,rcbs=get_room_char_briefs(session_state)
        enter=make_enter_room_ok(room_id=1001,create_id=session_state['char_id'],max_num=1,
            min_num=1,ob_num=0,name=session_state['room']['name'],room_type=4,mode=1,
            char_vec=rcbs,room_config=serialize_room_config(room['map_id']))
        # A waiting-room edit is durable before S1700. An immediate reconnect
        # must not reload the previous difficulty after its ACK was received.
        if request_id!=304:checkpoint()
        conn.sendall(enter)
        _dump(addr,'S->C',enter,tag='PVE-ENTER-ROOM')
        if request_id==304:send_start_battle()

    try:
        # Step 1: Check if this is an immediate policy-file-request
        # Non-blocking peek for 60ms to see if Flash sent <policy-file-request/>
        r, _, _ = select.select([conn], [], [], 0.06)
        if r:
            chunk = conn.recv(1024)
            if chunk:
                buf += chunk
                _dump(addr, "C->S", chunk, tag="PROBE")
                if b"<policy-file-request" in chunk:
                    conn.sendall(POLICY)
                    _dump(addr, "S->C", POLICY, tag="POLICY")
                    log.info(f"[sixcube] {addr} answered socket policy probe")
                    return

        # Step 2: Send Flash CNetConnection encryption handshake (00 00)
        # This tells ActionScript 3: encryptMode=0, keyLen=0 -> state becomes NS_WORKING
        conn.sendall(b"\x00\x00")
        saw_init_net = True
        _dump(addr, "S->C", b"\x00\x00", tag="HANDSHAKE")
        log.info(f"[sixcube] {addr} sent CNetConnection initNet (00 00)")

        # Step 3: Handle protocol frames
        while True:
            tick_actions()
            frames = _pop_frames(buf)
            if not frames:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    # The next loop starts with tick_actions; don't tick/save
                    # twice back-to-back before reading pending client input.
                    continue

                if not chunk:
                    log.info(f"[sixcube] {addr} [{role}] peer closed connection")
                    break

                buf += chunk
                _dump(addr, "C->S", chunk, tag=role)
                frames = _pop_frames(buf)

            for fr in frames:
                if fr.startswith(b"<policy-file-request"):
                    conn.sendall(POLICY)
                    _dump(addr, "S->C", POLICY, tag="POLICY")
                    return

                if len(fr) < 8:
                    continue

                mid = struct.unpack_from("<H", fr, 2)[0]
                payload = fr[8:]
                log.debug("[sixcube] %s [%s] frame id=0x%04x len=%s", addr, role, mid, len(fr))

                # Route role based on initial messages
                if role == "UNKNOWN":
                    if mid == 0x0036:
                        role = "CHAT"
                        log.info(f"[sixcube] {addr} identified as CHAT socket")
                    elif mid == 0x0034 and b"curVersion" in payload:
                        role = "AUTH"
                        log.info(f"[sixcube] {addr} identified as AUTH socket")
                    elif mid in (0x003D, 0x0034):
                        role = "GATE"
                        log.info(f"[sixcube] {addr} identified as GATE socket")

                # ==============================================================
                # AUTH ROLE
                # ==============================================================
                if role == "AUTH":
                    if mid == 0x0034:
                        # Version OK
                        reply = _frame(0x0387, b"\x00\x00")
                        conn.sendall(reply)
                        _dump(addr, "S->C", reply, tag="AUTH-VER-OK")

                    elif mid == 0x0033:
                        try:
                            auth_ok_payload = characters.login(payload)
                        except (ValueError, struct.error, UnicodeError) as exc:
                            conn.sendall(_frame(902, struct.pack('<iI', 1, 0) + _pstr(str(exc))))
                            continue
                        f_ack = _frame(0x0387, b"\x00\x00")
                        f_ok = _frame(0x0385, auth_ok_payload)
                        f_list = build_server_list(_advertise_host(), LISTEN_PORT)
                        reply = f_ack + f_ok + f_list
                        conn.sendall(reply)
                        _dump(addr, "S->C", reply, tag="AUTH-LOGIN-RESP")
                        log.info(f"[sixcube] {addr} [AUTH] Login successful, sent server list")

                # ==============================================================
                # GATE ROLE (LOBBY SYNC)
                # ==============================================================
                elif role == "GATE":
                    if session_state.get('tank_selection') and (
                            mid in (304, 600, 601) or mid == 602 and payload and payload[0] not in (1, 24)):
                        continue  # The native choice screen has no room/team editing controls.
                    if not owns_world and mid not in (52, 54, 61):
                        raise ValueError('GATE must bind a valid account session first')
                    if multiplayer_gate is not None and multiplayer_gate.handle(mid, payload):
                        continue
                    if mid == 0x003D:
                        # Only C61 requests S1003. Unsolicited replies make
                        # Flash display time since the last request as RTT.
                        f_tick = make_tick(0)
                        conn.sendall(f_tick)
                        _dump(addr, "S->C", f_tick, tag="GATE-TICK")
                        continue  # Ping changes no world state to checkpoint.

                    elif mid == economy_actions.REQUEST:
                        if not boss_ready:
                            continue  # Same authenticated final-sync barrier as the M30 extension.
                        packets, error = economy_actions.apply(session_state, payload, world_db, economy_context)
                        conn.sendall(b''.join(_frame(m, b) for m, b in packets))
                        continue  # Inventory/wallet/receipt already committed together; no extra checkpoint.

                    elif mid == normal_shop.REQUEST:
                        if not boss_ready:
                            continue  # Authenticated owner after final login sync only.
                        packets, error = normal_shop.apply(session_state, payload, world_db, shop_context)
                        if not error and any(message == 1150 for message, _ in packets):
                            tanks_db = {key: record for record in storage.garage()
                                        for key in (record['uuid'], str(record['tank_id']))}
                        conn.sendall(b''.join(_frame(m, b) for m, b in packets))
                        continue  # Shop receipt and economy committed together; no checkpoint rewrite.

                    elif mid == offline_mail.REQUEST:
                        try:
                            offline_mail.decode_request(payload)
                        except ValueError:
                            pass  # Invalid input must not opt an old client in.
                        else:
                            mail_extension = True
                        packets, error = offline_mail.apply(session_state, payload, world_db)
                        if not error and any(message == 1150 for message, _ in packets):
                            # A mail claim can add a permanent tank. Reload the
                            # same GATE owner's lookup before its next command.
                            tanks_db = {key: record for record in storage.garage()
                                        for key in (record['uuid'], str(record['tank_id']))}
                        response = b''.join(_frame(m, b) for m, b in packets)
                        conn.sendall(response)
                        continue  # Mail/claim receipt already committed atomically.

                    elif mid == world_boss_store.META_REQUEST:
                        if not boss_ready:
                            continue
                        try:
                            world_boss_store.validate_meta_request(payload)
                        except ValueError:
                            continue
                        boss_metadata=True
                        sync_world_boss(True)
                        continue  # Negotiation/snapshot changes no player world.

                    elif mid in (350,556,557):
                        if not boss_ready or payload:
                            continue
                        if mid==350:
                            enter_world_boss()
                        else:
                            boss_ui_open=mid==556
                            if boss_ui_open:
                                sync_world_boss(True)
                            continue

                    elif mid == pve_stamina.REQUEST:
                        packets, error = pve_stamina.apply(session_state, payload, world_db)
                        response = b''.join(_frame(m, b) for m, b in packets)
                        conn.sendall(response)
                        _dump(addr, 'S->C', response, tag='PVE-STAMINA')
                        if error:
                            log.warning('[sixcube] Stamina purchase rejected: %s', error)
                        continue  # The wallet and personal stamina already committed together.

                    elif mid in (79,88):
                        from player_settings import save
                        try:
                            reply_id,body=save(mid,payload)
                        except (ValueError,UnicodeError) as exc:
                            log.warning(f'[sixcube] Invalid character settings: {exc}')
                            continue
                        reply=_frame(reply_id,body)
                        conn.sendall(reply)
                        _dump(addr,'S->C',reply,tag='GATE-SETTINGS-SAVED')
                        continue

                    elif mid in boosters.REQUESTS:
                        packets,error=boosters.apply(session_state,mid,payload,world_db)
                        response=b''.join(_frame(m,b) for m,b in packets)
                        conn.sendall(response)
                        _dump(addr,'S->C',response,tag='BOOSTER')
                        if error:log.warning('[sixcube] Booster C%s rejected: %s',mid,error)
                        elif session_state.get('room'):notify_selection()
                        continue

                    elif mid in (155,423,424,425,426,427,430,560):
                        packets,error = crystals.apply(session_state,mid,payload,world_db)
                        response=b''.join(_frame(m,b) for m,b in packets)
                        conn.sendall(response)
                        _dump(addr,'S->C',response,tag='CRYSTAL')
                        if mid==427 and not error:
                            tanks_db = {key: record for record in storage.garage()
                                        for key in (record['uuid'], str(record['tank_id']))}
                        if error:log.warning(f'[sixcube] Crystal request rejected: {error}')
                        continue

                    elif mid in guilds.REQUESTS or mid == 250 and guilds.is_purchase(payload):
                        packets, error = guild_protocol.apply(session_state, mid, payload, world_db)
                        data = b''.join(_frame(packet_id, body) for packet_id, body in packets)
                        if data:
                            conn.sendall(data)
                            _dump(addr, 'S->C', data, tag='GUILD')
                        if not error and session_state.get('tank_selection'):
                            session_state['guild_build'] = guilds.battle_build(world_db)
                            notify_selection()
                        log.log(logging.WARNING if error else logging.INFO, f'[sixcube] Guild C{mid}: {error or "saved"}')

                    elif mid == 75:
                        changed,packets,error=character_rename.apply(session_state,payload,world_db)
                        reply=b''.join(_frame(m,b) for m,b in packets)
                        if changed:
                            reply+=_template_batch([f for f in GATE_TEMPLATES if struct.unpack_from('<H',f,2)[0]==1016],session_state)
                        conn.sendall(reply)
                        if changed and session_state.get('room'):
                            notify_selection()
                        continue

                    elif mid in (153,250):
                        packets,error = cosmetic_shop.apply(session_state,mid,payload,world_db)
                        reply=b''.join(_frame(msg,body) for msg,body in packets)
                        conn.sendall(reply)
                        _dump(addr,'S->C',reply,tag='GATE-COSMETIC-SHOP')
                        if error:log.warning(f'[sixcube] Cosmetic shop: {error}')
                        elif mid == 153 and session_state.get('room'):
                            pb=serialize_player_brief(acct_id=session_state['char_id'],char_uuid=session_state['char_uuid'],
                                name=session_state['name'],level=session_state['level'],vip_level=session_state['vip_level'],
                                avatar=session_state['avatar'],decorate_id=session_state['decorate_id'],
                                military_star=session_state.get('military_star',0),
                                booster_attack=session_state.get('booster_attack',0))
                            ob=serialize_other_brief(tank_id=session_state['tank_id'],tank_level=session_state['tank_level'],
                                tank_external=session_state['tank_ext'],halo_id=session_state.get('halo_id',0),
                                skin_frame_type=session_state.get('skin_frame_type',0),
                                **boosters.other_brief(session_state,world_db))
                            conn.sendall(make_room_char_change(pb,ob))
                        continue  # Profile/inventory committed atomically; no battle checkpoint.

                    elif mid == 0x0034:
                        # Version 1.0.1.8480 -> Send Version OK
                        f_ver = _frame(0x0387, b"\x00\x00")
                        conn.sendall(f_ver)
                        _dump(addr, "S->C", f_ver, tag="GATE-VER-OK")

                    elif mid == 0x0036:
                        if owns_world:
                            continue
                        ident, game_token = characters.bind(payload)
                        world_lock = characters.acquire(ident, game_token)
                        world_connections.enter_context(storage.for_profile(ident))
                        world_db = world_connections.enter_context(storage.connection())
                        session_state, saved_arena = storage.load_world()
                        with storage.connection(write=True) as rank_db:
                            session_state.update(ranked_matches.brief(rank_db,ident))
                        session_state['game_token'] = game_token
                        pve_stamina.initialize()
                        try:
                            boosters.initialize()
                            pve_progress.initialize()
                        except ValueError as exc:
                            log.warning('[sixcube] Booster starter deferred: %s',exc)
                        session_state['booster_attack']=boosters.score(world_db)
                        owns_world = True
                        guild_inbox = guild_protocol.register(ident)
                        mail_inbox = offline_mail.subscribe(ident)
                        tanks_db = {key: record for record in storage.garage()
                                    for key in (record['uuid'], str(record['tank_id']))}
                        if session_state['battle']:
                            session_state['battle']['entered'] = False
                            session_state['battle']['loaded'] = 0
                        from multiplayer_gate import MultiplayerGate
                        multiplayer_gate = MultiplayerGate(conn, session_state, world_db, sys.modules[__name__], checkpoint)
                        f_pick = _frame(1008, _pstr(session_state['char_uuid']))
                        conn.sendall(f_pick)
                        _dump(addr, "S->C", f_pick, tag="GATE-PICK-APPROVED")
                        log.info(f"[sixcube] {addr} [GATE] Sent Char Pick Approved")

                    elif mid == 0x0038:
                        characters.validate_enter(payload, session_state)
                        # World Enter (multiplayer-char-30000791) -> Send CharData batch (Frames 3..11)
                        if len(GATE_TEMPLATES) >= 12:
                            batch = _template_batch(GATE_TEMPLATES[3:12],session_state)
                            conn.sendall(batch)
                            _dump(addr, "S->C", batch, tag="GATE-CHARDATA-BATCH")
                            log.info(f"[sixcube] {addr} [GATE] Sent CharData batch (frames 3..11, {len(batch)}b)")
                        else:
                            # Minimal fallback if template missing
                            f_enter = _frame(0x03ED, b"\x97\xc6\xc9\x01\x0e\x15\x3b\x2c\x00\x00")
                            conn.sendall(f_enter)
                            _dump(addr, "S->C", f_enter, tag="GATE-ENTER-FALLBACK")

                    elif mid == 0x0048:
                        # Client ready -> Send Inventory & Tank Garage (Frames 12..21)
                        if len(GATE_TEMPLATES) >= 22:
                            batch = _template_batch(GATE_TEMPLATES[12:22],session_state)
                            conn.sendall(batch)
                            _dump(addr, "S->C", batch, tag="GATE-GARAGE-BATCH")
                            log.info(f"[sixcube] {addr} [GATE] Sent Garage & Items batch (frames 12..21, {len(batch)}b)")

                    elif mid == 0x00A5:
                        # Final sync confirm -> Send Frames 22..23 (Lobby ready!)
                        if len(GATE_TEMPLATES) >= 24:
                            batch = _template_batch(GATE_TEMPLATES[22:24],session_state)
                            conn.sendall(batch)
                            _dump(addr, "S->C", batch, tag="GATE-LOBBY-COMPLETE")
                            log.info(f"[sixcube] {addr} [GATE] === ENTERED LOBBY (SANH) SUCCESSFULLY! ===")

                        data = b''.join(_frame(mid, body) for mid, body in guild_protocol.snapshot(world_db, storage.profile_id()))
                        conn.sendall(data)
                        _dump(addr, 'S->C', data, tag='GUILD-LOGIN')
                        conn.sendall(b''.join(_frame(mid, body) for mid, body in offline_mail.changed_packets(world_db, include_extension=mail_extension)))

                        boss_ready=True
                        if multiplayer_gate is not None and multiplayer_gate.mark_ready():
                            resume_sent = True
                        send_actions(deliver_boss_rewards())
                        sync_world_boss(True)
                        retire_world_boss()

                        if not resume_sent:
                            retire_finished_solo_pve(restore_saved=True)
                        if not resume_sent and session_state['room']:
                            resume_sent = True
                            r = session_state['room']
                            if r.get('room_type')==7:
                                send_start_battle(resuming=True)
                                continue
                            _,rcbs = get_room_char_briefs(session_state)
                            packet = make_enter_room_ok(room_id=r['room_id'],create_id=session_state['char_id'],
                                max_num=r['max_num'],min_num=r['min_num'],ob_num=r['ob_num'],name=r['name'],
                                room_type=r['room_type'],mode=r['mode'],char_vec=rcbs,
                                room_config=serialize_room_config(r['map_id']))
                            conn.sendall(packet)
                            _dump(addr,'S->C',packet,tag='RESTORE-ROOM')
                            if session_state['battle']:
                                send_start_battle(resuming=True)
                            elif session_state.get('tank_selection'):
                                send_selection(resuming=True)

                    elif mid == 0x025F:
                        # RequestInHall (607) -> Send Hall Display List (1724)
                        retire_finished_solo_pve()
                        retire_world_boss()
                        rooms_to_send = []
                        if session_state["room"] is not None:
                            r = session_state["room"]
                            r_info = serialize_room_info(
                                room_id=r["room_id"],
                                num=1+len(room_bots(session_state)),
                                max_num=r.get("max_num", 10),
                                map_id=r["map_id"],
                                mode=r["mode"],
                                state=r["state"],
                                room_type=r.get("room_type", 1),
                                name=r["name"],
                            )
                            rooms_to_send.append(r_info)
                        else:
                            # Always have persistent 5v5 Room 1001 with Bot Gundam waiting!
                            r_info = serialize_room_info(
                                room_id=1001,
                                num=1,
                                max_num=10,
                                map_id=MODE_MAPS[load_setup(world_db)['mode']],
                                mode=1 if load_setup(world_db)['mode']=='test' else 3,
                                state=0,
                                room_type=1,
                                name="Trận bot local - thiết lập trên web",
                            )
                            rooms_to_send.append(r_info)
                        f_hall = make_hall_display_list(rooms=rooms_to_send, site=0, room_type=1)
                        conn.sendall(f_hall)
                        _dump(addr, "S->C", f_hall, tag="GATE-HALL-LIST")
                        log.info(f"[sixcube] {addr} [GATE] Sent Hall display list (1724, {len(rooms_to_send)} rooms)")

                    elif mid==558 and len(payload)>=4:
                        map_id,=struct.unpack_from('<I',payload)
                        from pve_catalog import REQUEST_MAPS
                        if map_id not in REQUEST_MAPS:
                            send_actions([cosmetic_shop.tip(mid,'Màn PvE không hợp lệ.')])
                        else:
                            # Native best-record response. No fabricated speed records.
                            send_actions([(1516,struct.pack('<IIIHB',map_id,0,0,0,1))])

                    elif mid==304:
                        if len(payload)<4:
                            send_actions([cosmetic_shop.tip(mid,'Yêu cầu vào màn PvE không hợp lệ.')])
                        else:enter_solo_pve(struct.unpack_from('<I',payload)[0],mid)

                    elif mid == 0x0258:
                        # RequestCreateRoom (600) -> Send EnterRoomOk (1700)
                        r_type = payload[0] if len(payload) > 0 else 1
                        r_mode = payload[1] if len(payload) > 1 else 1
                        map_id = struct.unpack_from("<I", payload, 2)[0] if len(payload) >= 6 else 101
                        if r_type==4 or map_id>=200:
                            enter_solo_pve(map_id if r_type==4 else None,mid)
                            continue
                        if session_state["battle"]:
                            continue
                        password = struct.unpack_from("<I", payload, 6)[0] if len(payload) >= 10 else 0
                        off = 10
                        sz_name = "Phong 5v5 [Bot Gundam]"
                        if len(payload) >= off + 2:
                            slen = struct.unpack_from("<H", payload, off)[0]
                            off += 2
                            if len(payload) >= off + slen:
                                parsed = payload[off : off + slen].decode("utf-8", "ignore")
                                if parsed:
                                    sz_name = parsed
                        session_state["room"] = {
                            "room_id": 1001,
                            "name": sz_name,
                            "map_id": map_id if map_id != 0 else 101,
                            "mode": r_mode if r_mode != 0 else 1,
                            "room_type": 1,  # 5v5
                            "state": 0,  # RST_WAIT
                            "camp": 1,  # CAMP_A
                            "ready": 1,
                            "max_num": 10,
                            "min_num": 2,
                            "ob_num": 2,
                        }
                        configure_room(session_state,world_db)
                        _, rcbs = get_room_char_briefs(session_state)
                        f_enter_ok = make_enter_room_ok(
                            room_id=session_state["room"]["room_id"],
                            create_id=session_state["char_id"],
                            max_num=session_state["room"]["max_num"],
                            min_num=session_state["room"]["min_num"],
                            ob_num=session_state["room"]["ob_num"],
                            name=session_state["room"]["name"],
                            room_type=session_state["room"]["room_type"],
                            mode=session_state["room"]["mode"],
                            char_vec=rcbs,room_config=serialize_room_config(session_state['room']['map_id']),
                        )
                        conn.sendall(f_enter_ok)
                        _dump(addr, "S->C", f_enter_ok, tag="GATE-ENTER-ROOM-OK")
                        log.info(
                            f"[sixcube] {addr} [GATE] === CREATED & ENTERED 5v5 ROOM 1001 ({sz_name})! CharCount={len(rcbs)} ===",
                        )

                    elif mid == 0x0259:
                        # RequestJoinRoom (601) -> Join room & send EnterRoomOk (1700)
                        r_id = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 1001
                        r_type = payload[4] if len(payload) >= 5 else 1
                        if r_type==4:
                            send_actions([cosmetic_shop.tip(mid,'PvE tổ đội chưa mở; hãy chọn màn PvE đơn.')])
                            continue
                        r_mode = payload[5] if len(payload) >= 6 else 1
                        map_id = struct.unpack_from("<I", payload, 6)[0] if len(payload) >= 10 else 101
                        if session_state["room"] is None:
                            session_state["room"] = {
                                "room_id": r_id if r_id != 0 else 1001,
                                "name": "Phong 5v5 [Bot Gundam]",
                                "map_id": map_id if map_id != 0 else 101,
                                "mode": r_mode if r_mode != 0 else 1,
                                "room_type": 1,
                                "state": 0,
                                "camp": 1,
                                "ready": 1,
                                "max_num": 10,
                                "min_num": 2,
                                "ob_num": 2,
                            }
                            configure_room(session_state,world_db)
                        _, rcbs = get_room_char_briefs(session_state)
                        f_enter_ok = make_enter_room_ok(
                            room_id=session_state["room"]["room_id"],
                            create_id=session_state["char_id"],
                            max_num=session_state["room"]["max_num"],
                            min_num=session_state["room"]["min_num"],
                            ob_num=session_state["room"]["ob_num"],
                            name=session_state["room"]["name"],
                            room_type=session_state["room"]["room_type"],
                            mode=session_state["room"]["mode"],
                            char_vec=rcbs,room_config=serialize_room_config(session_state['room']['map_id']),
                        )
                        conn.sendall(f_enter_ok)
                        _dump(addr, "S->C", f_enter_ok, tag="GATE-JOIN-ROOM-OK")
                        log.info(f"[sixcube] {addr} [GATE] Joined 5v5 room 1001 (chars={len(rcbs)})")

                    elif mid == 0x00C8:
                        if session_state["battle"] or session_state.get('tank_selection'):
                            continue
                        # ChangeBattleTank (200) -> original garage flag update + room slot.
                        slen = struct.unpack_from("<H", payload, 0)[0] if len(payload) >= 2 else 0
                        tank_uuid = payload[2 : 2 + slen].decode("utf-8", "ignore") if len(payload) >= 2 + slen else ""
                        log.info(f"[sixcube] {addr} [GATE] ChangeBattleTank request: uuid={tank_uuid}")
                        tank_info = tanks_db.get(tank_uuid)
                        if tank_info is None:
                            continue  # A missing instance cannot be updated by the client.
                        old_flags = {r['uuid']:r['ext_flag'] for r in garage_for_state(session_state)}
                        session_state['selected_tank_uuid'] = tank_info['uuid']
                        session_state["tank_id"] = tank_info["tank_id"]
                        session_state["tank_level"] = tank_info["level"]
                        session_state['tank_strength'] = tank_info.get('stth_level',0)
                        session_state["tank_ext"] = tank_info["external_id"]
                        session_state["halo_id"] = tank_info.get("halo_id", 0)
                        session_state["skin_frame_type"] = tank_info.get("skin_frame_type", 0)
                        from bot_setup import unique_room_roster
                        rerolled_bots=unique_room_roster(session_state)
                        checkpoint()
                        changed = [r for r in garage_for_state(session_state)
                                   if r['ext_flag']!=old_flags[r['uuid']]]
                        # Clear the previous flag before setting the new one.
                        changed.sort(key=lambda r: bool(r['ext_flag'] & 2))
                        for record in changed:
                            update = _frame(1151,tank_instance_payload(record)+b'\x08')  # TIOT_EXT_FLAG
                            conn.sendall(update)
                            _dump(addr,'S->C',update,tag='GATE-TANK-UPDATE')

                        log.info(
                            f"[sixcube] {addr} [GATE] Switched active tank to ID {session_state['tank_id']} (ext={session_state['tank_ext']}, lvl={session_state['tank_level']})",
                        )

                        pb = serialize_player_brief(
                            acct_id=session_state["char_id"],
                            char_uuid=session_state["char_uuid"],
                            name=session_state["name"],
                            level=session_state["level"],
                            vip_level=session_state["vip_level"],
                            avatar=session_state.get('avatar', 0),
                            decorate_id=session_state.get('decorate_id', 0),
                            military_star=session_state.get('military_star', 0),
                            booster_attack=session_state.get('booster_attack', 0),
                        )
                        ob = serialize_other_brief(
                            tank_id=session_state["tank_id"],
                            tank_level=session_state["tank_level"],
                            tank_external=session_state["tank_ext"],
                            halo_id=session_state["halo_id"],
                            skin_frame_type=session_state["skin_frame_type"],
                            **boosters.other_brief(session_state,world_db),
                        )
                        if session_state["room"] is not None:
                            f_change = make_room_char_change(pb, ob)
                            conn.sendall(f_change)
                            _dump(addr, "S->C", f_change, tag="GATE-ROOM-CHAR-CHANGE")
                            for bot in rerolled_bots:
                                changed_bot=make_room_char_change(
                                    serialize_player_brief(acct_id=bot['acct_id'],char_uuid=bot['char_uuid'],name=bot['name']),
                                    serialize_other_brief(tank_id=bot['tank_id'],tank_level=bot['tank_level'],tank_external=bot['tank_ext'],halo_id=bot.get('halo_id',0)))
                                conn.sendall(changed_bot)
                                _dump(addr,'S->C',changed_bot,tag='GATE-BOT-TANK-CHANGE')
                            log.info(
                                f"[sixcube] {addr} [GATE] Broadcasted NotifyRoomCharChange (1711) for tank {session_state['tank_id']}",
                            )

                    elif mid in (203,204,207,210):
                        if mid == 210:
                            record, packets, error = tank_halos.apply(session_state,payload,world_db)
                        elif mid == 207:
                            record, packets, error = tank_skins.apply(session_state,payload,world_db)
                        else:
                            record, packets, error = tank_upgrade.apply(session_state,mid,payload,world_db)
                        if record is not None:
                            tanks_db[record['uuid']] = tanks_db[str(record['tank_id'])] = record
                            if session_state.get('tank_selection'):
                                chosen = next(r for r in storage.garage()
                                              if r['uuid'] == session_state['match_tank_uuid'])
                                prebattle_selection.use_tank(session_state, chosen)
                                notify_selection()
                        for packet_id, body in packets:
                            response = _frame(packet_id,body)
                            conn.sendall(response)
                            _dump(addr,'S->C',response,tag='GATE-TANK-SKIN' if mid==207 else 'GATE-TANK-UPGRADE')
                        if record is not None and session_state.get('room') and (mid==203 or session_state.get('selected_tank_uuid')==record['uuid']):
                            pb=serialize_player_brief(acct_id=session_state['char_id'],char_uuid=session_state['char_uuid'],
                                name=session_state['name'],level=session_state['level'],vip_level=session_state['vip_level'],
                                avatar=session_state.get('avatar',0),decorate_id=session_state.get('decorate_id',0),
                                military_star=session_state.get('military_star',0),
                                booster_attack=session_state.get('booster_attack',0))
                            ob=serialize_other_brief(tank_id=session_state['tank_id'],tank_level=session_state['tank_level'],
                                tank_external=session_state['tank_ext'],halo_id=session_state.get('halo_id',0),
                                skin_frame_type=session_state.get('skin_frame_type',0),
                                **boosters.other_brief(session_state,world_db))
                            conn.sendall(make_room_char_change(pb,ob))
                        log.log(logging.WARNING if error else logging.INFO, f'[sixcube] tank {"skin" if mid==207 else "upgrade"} C{mid}: {error or "saved"}')

                    elif mid == 0x025A:
                        # RequestRoomOp (602) -> Ready, Cancel, Camp switch, Leave, Kick, Start
                        room_op = payload[0] if len(payload) >= 1 else 0
                        if (boosters.is_pve(session_state.get('room') or {})
                                and room_op==1 and session_state.get('battle')):
                            send_actions([cosmetic_shop.tip(mid,'Hãy dùng rời trận PvE; không thể xóa trận bằng thao tác rời phòng chờ.')])
                            continue
                        if boosters.is_pve(session_state.get('room') or {}) and room_op not in (1,24):
                            send_actions([cosmetic_shop.tip(mid,'Phòng PvE đơn giữ một người ở phe 1.')])
                            continue
                        target = struct.unpack_from("<I", payload, 1)[0] if len(payload) >= 5 else session_state["char_id"]
                        log.info(f"[sixcube] {addr} [GATE] RequestRoomOp op={room_op} target={target}")
                        if room_op == 1:  # ROPT_LEAVE
                            clear_match_selection()
                            session_state["room"] = None
                            session_state["battle"] = None
                            conn.settimeout(0.5)
                            session_state["bot"]["in_room"] = True  # Reset bot for next room entry
                            ensure_test_ally(session_state)['in_room'] = True
                            f_op = make_room_op(1, session_state["char_id"], session_state["char_id"])
                            conn.sendall(f_op)
                            _dump(addr, "S->C", f_op, tag="GATE-ROOM-OP-LEAVE")
                            log.info(f"[sixcube] {addr} [GATE] Player left room")
                        elif room_op == 2:  # ROPT_MEMBER_READY
                            if session_state["room"]:
                                session_state["room"]["ready"] = 1
                            f_op = make_room_op(2, session_state["char_id"], session_state["char_id"])
                            conn.sendall(f_op)
                            _dump(addr, "S->C", f_op, tag="GATE-ROOM-OP-READY")
                        elif room_op == 3:  # ROPT_MEMBER_CANCEL
                            if session_state["room"]:
                                session_state["room"]["ready"] = 0
                            f_op = make_room_op(3, session_state["char_id"], session_state["char_id"])
                            conn.sendall(f_op)
                            _dump(addr, "S->C", f_op, tag="GATE-ROOM-OP-CANCEL")
                        elif room_op == 4:  # ROPT_CHANGE_CAMP
                            if session_state.get('room',{}).get('map_id') in BOT_MAPS:
                                session_state['room']['camp']=3-session_state['room']['camp']
                                for acct in [session_state['char_id']]+[b['acct_id'] for b in room_bots(session_state)]:
                                    conn.sendall(make_room_op(4,acct,acct))
                                checkpoint()
                                continue
                            if session_state["room"]:
                                cur_camp = session_state["room"].get("camp", 1)
                                new_camp = 2 if cur_camp == 1 else 1
                                session_state["room"]["camp"] = new_camp
                            f_op = make_room_op(4, session_state["char_id"], session_state["char_id"])
                            conn.sendall(f_op)
                            _dump(addr, "S->C", f_op, tag="GATE-ROOM-OP-CAMP")
                            # If bot is in room, toggle bot camp too so they stay opposing!
                            if session_state["bot"].get("in_room", True):
                                bot_camp = 1 if session_state["room"]["camp"] == 2 else 2
                                session_state["bot"]["camp"] = bot_camp
                                f_bot_op = make_room_op(4, session_state["bot"]["acct_id"], session_state["bot"]["acct_id"])
                                conn.sendall(f_bot_op)
                                _dump(addr, "S->C", f_bot_op, tag="GATE-ROOM-BOT-CAMP")
                                log.info(f"[sixcube] {addr} [GATE] Camp toggled: Player={session_state['room']['camp']}, Bot={bot_camp}")
                            ally = ensure_test_ally(session_state)
                            ally['camp'] = session_state['room']['camp']
                            if ally.get('in_room',True):
                                conn.sendall(make_room_op(4,ally['acct_id'],ally['acct_id']))
                        elif room_op == 20:  # ROPT_KICKOUT
                            if session_state.get('room',{}).get('map_id') in BOT_MAPS:
                                # Team composition belongs to next-room web settings.
                                # Keep at least one opponent so the approved PK rule can run.
                                continue
                            if target == session_state["bot"]["acct_id"]:
                                session_state["bot"]["in_room"] = False
                                f_op = make_room_op(20, session_state["char_id"], target)
                                conn.sendall(f_op)
                                _dump(addr, "S->C", f_op, tag="GATE-ROOM-KICK-BOT")
                                log.info(f"[sixcube] {addr} [GATE] Bot Gundam kicked out of room by master!")
                            else:
                                ally = session_state.get('ally_bot')
                                if ally and target==ally['acct_id']:
                                    ally['in_room'] = False
                                f_op = make_room_op(20, session_state["char_id"], target)
                                conn.sendall(f_op)
                                _dump(addr, "S->C", f_op, tag="GATE-ROOM-KICK")
                        elif room_op == 24:  # ROPT_ENTER_SCENE
                            send_selection()

                    elif mid == 0x0262:
                        # ROOM_LADDER_PVP=9 goes directly to native selection.
                        if not payload or any(payload[1:]) or payload[0] not in (1,2,9):
                            continue
                        if payload[0]==9:
                            if session_state.get('battle') or session_state.get('tank_selection'):
                                continue
                            ranked_matches.prepare_room(session_state,world_db)
                        send_selection()

                    elif mid == 215:
                        try:
                            record = prebattle_selection.select(session_state, payload, storage.garage(), SUPPORTED_TANKS)
                        except ValueError as exc:
                            log.warning('[sixcube] select tank: %s', exc)
                            continue
                        if record is not None:
                            from bot_setup import unique_room_roster
                            changed_bots = unique_room_roster(session_state)
                            checkpoint()
                            notify_selection(changed_bots)

                    elif mid == 614:
                        if (session_state.get('tank_selection') and len(payload) >= 4
                                and not any(payload[4:])
                                and struct.unpack_from('<I', payload)[0] == session_state['room']['room_id']):
                            finish_selection()

                    elif mid == 611 and session_state.get('tank_selection'):
                        clear_match_selection()
                        session_state['room'] = None
                        checkpoint()
                        conn.sendall(_frame(1017, b''))

                    elif mid == 0x025D:
                        # SyncLoadAndDelay (605), four u32 fields; Flash may add padding.
                        if len(payload) < 16:
                            continue
                        scene_id, char_id, delay, loaded = struct.unpack_from("<IIII", payload)
                        battle = session_state["battle"]
                        if char_id != session_state["char_id"] or loaded > 100:
                            continue
                        if scene_id and (battle is None or scene_id != battle["scene_id"]):
                            continue
                        if battle and scene_id == battle["scene_id"]:
                            battle["loaded"] = loaded
                            log.info(f"[sixcube] {addr} [BATTLE-LOAD] scene={scene_id} player={loaded}% bot=100%")
                        f_rate = make_loaded_rate(char_id, delay=delay, loaded=loaded)
                        to_send = f_rate
                        for bot in room_bots(session_state):
                            to_send+=make_loaded_rate(bot['acct_id'],delay=10,loaded=100)
                        conn.sendall(to_send)
                        _dump(addr, "S->C", to_send, tag="GATE-LOADED-RATE")

                    elif mid == 0x0228:
                        # RequestBattleEnter (552), sent once the client finishes loading.
                        if len(payload) < 4:
                            continue
                        scene_id = struct.unpack_from("<I", payload)[0]
                        battle = session_state["battle"]
                        if battle is None or battle["scene_id"] != scene_id or battle["entered"]:
                            continue
                        if session_state['room']['map_id']==world_boss_store.MAP:
                            retire_world_boss()
                            if session_state.get('battle') is None:
                                send_actions([(1017,b'')])
                                continue
                            row=world_boss_store.poll(world_db)
                            battle['world_boss'].update(hp=row['hp'],remaining_ms=max(0,row['end_ms']-world_boss_store.now_ms()))
                        # 552 itself confirms load completion; don't depend on a timer
                        # delivering a separate 100% progress packet first.
                        try:
                            if not saved_arena:
                                tank_uuid=session_state.get('match_tank_uuid') or session_state.get('selected_tank_uuid') or next(
                                    (r['uuid'] for r in storage.garage() if r['tank_id']==session_state['tank_id']), '')
                                session_state['crystal_build']=crystals.battle_build(world_db,tank_uuid)
                                session_state['military_star']=military_rank.stars(world_db)
                                session_state['military_build']=military_rank.build(session_state['military_star'])
                                # Guild/booster builds were frozen before S1705.
                            actions = restore_arena(session_state,saved_arena) if saved_arena else BattleArena(session_state,bot_attacks=True)
                            if actions.geometry.id==world_boss_store.MAP:
                                from world_boss_mode import synchronize
                                synchronize(actions,world_boss_store.current(world_db),world_boss_store.now_ms())
                            packets = entry_packets(session_state,actions)
                            if saved_arena:
                                packets.extend(resume_packets(actions))
                            elif actions.pk:
                                packets.extend(a.snapshot(actions.clock()) for a in actions.actors.values() if a.info.get('robo'))
                        except (ValueError, KeyError, OSError) as exc:
                            _record_error(addr, role, exc, "battle entry error")
                            continue
                        rates = make_loaded_rate(session_state["char_id"], loaded=100)
                        for bot in room_bots(session_state):
                            rates+=make_loaded_rate(bot['acct_id'],delay=10,loaded=100)
                        conn.sendall(rates)
                        _dump(addr, "S->C", rates, tag="GATE-LOADED-RATE")
                        packet=b''.join(_frame(msg_id,body) for msg_id,body in
                                        skin_effects.present_packets(packets,session_state,actions))
                        conn.sendall(packet)
                        _dump(addr,"S->C",packet,tag="BATTLE-ENTRY")
                        battle["loaded"] = 100
                        battle["entered"] = True
                        battle["actions"] = actions
                        if actions:
                            conn.settimeout(0.02)
                        log.info(f"[sixcube] {addr} [BATTLE-ENTRY] scene={scene_id} snapshot sent; awaiting client verification")

                    elif mid in (501, 503, 510, 514):
                        # Dispatch only tanks with implemented base mechanics.
                        battle = session_state["battle"]
                        if battle and battle.get("entered") and battle.get("actions"):
                            packets = battle["actions"].handle(mid,payload)
                            checkpoint()
                            send_actions(packets)
                            continue  # Already committed before the combat reply.

                    elif mid in (509, 512, 613):
                        # Original RequestExitBattle / RequestSurrenderVotation.
                        # Recorded settings button: C512, scene u32 + padding.
                        # The local sandbox has one human; surrender exits it
                        # immediately, without adding PvP votes/rewards/penalties.
                        battle = session_state['battle']
                        if mid==613:
                            actions=battle.get('actions') if battle else None
                            if not actions or not actions.pk or not actions.pk['finished']:continue
                            payload=struct.pack('<I',battle['scene_id'])
                        if len(payload)<4:
                            continue
                        if battle is None:
                            if (session_state.get('tank_selection')
                                    and struct.unpack_from('<I',payload)[0] == session_state['room']['room_id']):
                                clear_match_selection()
                                session_state['room'] = None
                                checkpoint()
                                conn.sendall(_frame(1017,b''))
                                continue
                            if session_state['room'] is None:
                                # Retry after a completed exit: recover a stale
                                # battle view without touching a new waiting room.
                                reply = _frame(1017,b'')
                                conn.sendall(reply)
                                _dump(addr,'S->C',reply,tag='BATTLE-EXIT-RETRY')
                            continue
                        scene_id, = struct.unpack_from('<I',payload)
                        if scene_id!=battle['scene_id']:
                            continue
                        actions=battle.get('actions')
                        solo_pve=boosters.is_pve(session_state.get('room') or {})
                        if actions and actions.pk and not actions.pk['finished']:
                            from pk_rules import finish
                            finish(actions,actions.clock())
                            actions.pk['winner']=3-actions.player.camp
                            battle['forfeit']=True
                        if actions and actions.pk and not solo_pve and not battle.get('rewards_applied'):
                            import match_rewards
                            try:
                                if not world_db.in_transaction:
                                    world_db.execute('BEGIN IMMEDIATE')
                                match_rewards.apply_pvp_rewards(session_state, actions, world_db)
                                world_db.commit()
                                battle['rewards_applied'] = True
                            except Exception as exc:
                                world_db.rollback()
                                log.warning('[sixcube] PvP exit rewards error: %s', exc)
                        if actions and actions.pk:
                            ranked_matches.start_metadata(session_state)
                            checkpoint()
                        clear_match_selection()
                        session_state['battle'] = session_state['room'] = None
                        session_state['bot']['in_room'] = True
                        ensure_test_ally(session_state)['in_room'] = True
                        conn.settimeout(0.5)
                        # Commit before confirming; reconnect must not resurrect
                        # the surrendered match or its pending combat events.
                        checkpoint()
                        # Flush the committed city/reward sync with this exit,
                        # not an unrelated later request after returning to hall.
                        if solo_pve:
                            send_actions([])
                        # S1707 clears battle state; the original S1017 handler
                        # clears room/pairing state and explicitly opens MAIN.
                        # S1701 only removed the player, leaving Flash on the map.
                        reply = _frame(1707,struct.pack('<IIIII',0,scene_id,
                                        session_state['char_id'],2,0))  # EXIT_PLAYER
                        if not solo_pve:
                            reply += b''.join(_frame(m,b) for m,b in ranked_matches.update_packets(session_state))
                        if len(GATE_TEMPLATES) >= 6:
                            reply += _template_batch([f for f in GATE_TEMPLATES if struct.unpack_from('<H', f, 2)[0] == 1016], session_state)
                        wallet = cosmetic_shop.snapshot(world_db)
                        reply += _frame(1039, struct.pack('<HH6i', 2, 0, *wallet['currency']))
                        containers = inventory.load(world_db)
                        reply += _frame(1100, inventory.list_payload(3, containers.get('3', [])))
                        reply += _frame(1017,b'')  # NotifyBackToMain has no fields.
                        conn.sendall(reply)
                        _dump(addr,'S->C',reply,tag='BATTLE-EXIT')

                    elif mid == 0x025B:
                        if (session_state.get('room') or {}).get('room_type')==4:
                            send_actions([cosmetic_shop.tip(mid,'Hãy rời trận rồi chọn màn PvE đã mở.')])
                            continue
                        # SetRoomMap (603) -> NotifyRoomMap (1704).
                        map_id = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 101
                        if session_state["room"]:
                            # Mode/roster are fixed at room creation from web settings.
                            map_id=session_state['room']['map_id']
                        f_map = make_room_map(map_id)
                        conn.sendall(f_map)
                        _dump(addr, "S->C", f_map, tag="GATE-ROOM-MAP")

                    elif mid == 0x0260:
                        # RequestOutHall (608) is not a ping request.
                        pass

                # ==============================================================
                # CHAT ROLE
                # ==============================================================
                elif role == "CHAT":
                    if mid == 0x0036:
                        characters.bind(payload)
                        # Bind chat token -> Chat connect confirm (0x0DAC)
                        f_chat_ok = _frame(0x0DAC, b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00")
                        conn.sendall(f_chat_ok)
                        _dump(addr, "S->C", f_chat_ok, tag="CHAT-CONFIRM")
                        log.info(f"[sixcube] {addr} [CHAT] Chat connection confirmed (0x0DAC)")
                    elif mid == 0x003C:
                        # Chat heartbeat ping - just keep alive
                        pass

                checkpoint()

    except Exception as exc:
        _record_error(addr,role,exc)
    finally:
        if multiplayer_gate is not None:
            try:
                multiplayer_gate.disconnect()
            except Exception as exc:
                _record_error(addr, role, exc, 'Shared room disconnect error')
        try:
            checkpoint()
        except Exception as exc:
            _record_error(addr,role,exc,"checkpoint error")
        if guild_inbox is not None:
            try:
                guild_protocol.unregister(session_state['char_id'], guild_inbox)
            except Exception as exc:
                _record_error(addr, role, exc, 'Guild disconnect error')
        if mail_inbox is not None:
            offline_mail.unsubscribe(session_state['char_id'], mail_inbox)
        world_connections.close()
        if world_lock is not None:
            world_lock.release()
        try:
            conn.close()
        except Exception:
            pass
        log.info(f"[sixcube] {addr} [{role}] closed (init={saw_init_net})")


def _serve(bind="0.0.0.0", ready=None) -> None:
    global GATE_TEMPLATES
    from cpp_backend import initialize as initialize_acceleration
    initialize_acceleration()
    characters.initialize()
    guilds.initialize()
    offline_mail.initialize()
    world_boss_store.initialize()
    ranked_matches.initialize()
    GATE_TEMPLATES = load_templates()
    load_tank_database()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except Exception:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind, LISTEN_PORT))
    except OSError as exc:
        log.exception("[sixcube] bind :%s failed", LISTEN_PORT)
        return
    sock.listen(32)
    if ready is not None:
        ready.set()
    log.info(f"[sixcube] listening :{LISTEN_PORT} (AUTH/GATE/CHAT ready)")
    while True:
        try:
            conn, addr = sock.accept()
            threading.Thread(target=_client_handler, args=(conn, addr), daemon=True).start()
        except Exception as exc:
            log.exception("[sixcube] accept error")


def start_sixcube(bind="0.0.0.0") -> None:
    # Fail required-cpp mode synchronously, before launching a daemon thread.
    from cpp_backend import initialize as initialize_acceleration
    initialize_acceleration()
    ready = threading.Event()
    threading.Thread(target=_serve, args=(bind, ready), daemon=True).start()
    if bind == "127.0.0.1" and not ready.wait(60):
        raise RuntimeError("Native local server did not become ready; see the error above")


if __name__ == "__main__":
    from server_logging import configure
    from storage_import import initialize
    configure()
    initialize()
    _serve()
