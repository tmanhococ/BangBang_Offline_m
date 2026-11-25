"""Authenticated GATE adapter for shared rooms and one shared native arena.

The caller owns this object's socket and SQLite connection. RoomHub only
coordinates trusted snapshots; BattleService alone advances and saves live
shared combat. Native packets continue to own client phase transitions.
"""
from copy import deepcopy
import json
import logging
import struct
import time
import uuid

import storage
import bot_setup
import boosters
import crystals
import cosmetic_shop
import guilds
import guild_combat
import military_rank
import prebattle_selection
import ranked_matches
from battle_runtime import SUPPORTED_TANKS
from multiplayer_rooms import HUB, REQUEST, REPLY, RoomError, decode_request, encode_reply
from multiplayer_battle import service

log = logging.getLogger(__name__)
INPUT_MESSAGES = frozenset((501, 503, 510, 514))
ROOM_MESSAGES = frozenset((200, 203, 204, 207, 210, 215, 304, 350, 509, 512, 552,
                           600, 601, 602, 603, 605, 607, 610, 611, 613, 614))


def clean_state(state):
    """Never hand authentication tokens, live arenas or connections to peers."""
    result = {key: deepcopy(value) for key, value in state.items()
              if key not in ('game_token', 'battle', 'multiplayer_players')}
    battle = state.get('battle')
    result['battle'] = ({key: deepcopy(value) for key, value in battle.items()
                         if key != 'actions'} if battle else None)
    return result


class MultiplayerGate:
    def __init__(self, conn, state, db, wire, checkpoint, *, hub=HUB, battles=service):
        self.conn, self.state, self.db, self.wire = conn, state, db, wire
        self.checkpoint, self.hub, self.battles = checkpoint, hub, battles
        self.profile = int(state['char_id'])
        self.generation = None
        self.ready = False
        self.room_id = None
        self.room_revision = None
        self.selection_room = None
        self.selection_key = None
        self.started_room = None
        self.last_control = self.last_profile = self.last_status = 0.0
        self.last_public = self.last_presence = None
        self.skip_legacy_resume = bool(state.get('multiplayer_room_id'))
        if self.skip_legacy_resume:
            live = self.battles.bind(self.profile, state)
            if not live:
                self._restore_primary()
                # The process-local room cannot survive a server restart.
                state['room'] = state['battle'] = None
                state.pop('multiplayer_room_id', None)
                state.pop('multiplayer_object_id', None)
                self.checkpoint()

    def mark_ready(self):
        self.ready = True
        return self.skip_legacy_resume

    @property
    def enabled(self):
        return self.generation is not None

    @property
    def managed(self):
        return bool(self.state.get('multiplayer_room_id'))

    def _send(self, packets):
        data = b''.join(self.wire._frame(mid, body) for mid, body in packets)
        if data:
            self.conn.sendall(data)

    def _tip(self, mid, error):
        self._send([cosmetic_shop.tip(mid, str(error))])

    def _public(self, state=None):
        s = self.state if state is None else state
        return dict(name=s['name'], level=s['level'], avatar_id=s.get('avatar', 0),
                    tank_id=s['tank_id'], tank_level=s['tank_level'], external_id=s.get('tank_ext', 0))

    def _private(self, state=None):
        s, w = self.state if state is None else state, self.wire
        pb = w.serialize_player_brief(acct_id=self.profile, char_uuid=s['char_uuid'],
            name=s['name'], level=s['level'], sex=s.get('sex', 1), vip_level=s['vip_level'],
            avatar=s.get('avatar', 0), decorate_id=s.get('decorate_id', 0),
            military_star=s.get('military_star', 0), elo=s.get('elo', 1000),
            elo_section=s.get('elo_section', 1), pre_elo_section=s.get('pre_elo_section', 1),
            booster_attack=s.get('booster_attack', 0))
        ob = w.serialize_other_brief(tank_id=s['tank_id'], tank_level=s['tank_level'],
            tank_external=s.get('tank_ext', 0), halo_id=s.get('halo_id', 0),
            skin_frame_type=s.get('skin_frame_type', 0),
            command_skill=guild_combat.command(s.get('guild_build', {})),
            **boosters.other_brief(s, self.db))
        return dict(state=clean_state(s), player_brief=pb, other_brief=ob)

    def _presence(self):
        return 'busy' if self.state.get('room') and not self.managed else 'lobby'

    def _register(self):
        if self.enabled:
            return
        self._freeze_build()
        self.generation = self.hub.register(self.profile, self._public(), self._private(), self._presence())
        self.conn.settimeout(.1)
        self.last_profile = 0

    def _refresh_profile(self, *, freeze=False):
        if freeze:
            self._freeze_build()
        self.hub.update_peer(self.profile, self.generation, public=self._public(),
                             private=self._private(), status=self._presence())
        self.last_profile = time.monotonic()

    def _freeze_build(self, state=None):
        """Read only this authenticated profile on its GATE-owned connection."""
        s = self.state if state is None else state
        s['guild_build'] = guilds.battle_build(self.db)
        s['booster_build'] = boosters.battle_build(self.db, s.get('room') or {})
        tank_uuid = s.get('match_tank_uuid') or s.get('selected_tank_uuid')
        if not tank_uuid:
            tank_uuid = next(r['uuid'] for r in storage.garage() if r['tank_id'] == s['tank_id'])
        s['crystal_build'] = crystals.battle_build(self.db, tank_uuid)
        s['military_star'] = military_rank.stars(self.db)
        s['military_build'] = military_rank.build(s['military_star'])

    def _restore_primary(self):
        prebattle_selection.restore_primary(self.state, storage.garage())

    def _room(self):
        return self.hub.room_for(self.profile, self.generation)

    def _apply_room(self, room):
        member = next(m for m in room['members'] if m['profile_id'] == self.profile)
        seat = next(s for s in room['seats'] if s['account_id'] == self.profile)
        config = deepcopy(room['config'])
        config.update(room_id=room['room_id'], camp=member['camp'], ready=int(seat['ready']),
                      owner_id=room['owner_id'], multiplayer=True, state=int(room['phase'] != 'waiting'),
                      max_num=10, min_num=2, ob_num=0)
        self.state.update(room=config, bots=deepcopy(room['bots']),
                          multiplayer_room_id=room['room_id'], multiplayer_object_id=member['object_id'])
        self.room_id = room['room_id']

    def _briefs(self, room):
        w = self.wire
        rows = []
        for member in room['members']:
            private = member['private']
            frozen = private['state']
            cb = w.serialize_char_brief(private['player_brief'], private['other_brief'], camp=member['camp'],
                buff_vec=[v['skill'] for k, v in frozen.get('guild_build', {}).items() if k != '1'])
            seat = next(s for s in room['seats'] if s['account_id'] == member['account_id'])
            rows.append((member['object_id'], cb, w.serialize_room_char_brief(cb, ready=int(seat['ready']))))
        for bot in room['bots']:
            pb = w.serialize_player_brief(acct_id=bot['acct_id'], char_uuid=bot['char_uuid'],
                name=bot['name'], level=bot.get('level', 36), vip_level=bot.get('vip_level', 6),
                avatar=bot.get('avatar', 0), decorate_id=bot.get('decorate_id', 0),
                military_star=bot.get('military_star', 0),
                elo=bot.get('elo', 1000), elo_section=bot.get('elo_section', 1),
                pre_elo_section=bot.get('pre_elo_section', 1))
            ob = w.serialize_other_brief(tank_id=bot['tank_id'], tank_level=bot['tank_level'],
                tank_external=bot.get('tank_ext', 0), halo_id=bot.get('halo_id', 0),
                skin_frame_type=1, command_skill=guild_combat.command(bot.get('guild', {})))
            cb = w.serialize_char_brief(pb, ob, camp=bot['camp'],
                buff_vec=[v['skill'] for k, v in bot.get('guild', {}).items() if k != '1'])
            rows.append((bot['object_id'], cb, w.serialize_room_char_brief(cb, ready=1)))
        rows.sort(key=lambda row: row[0])
        return [r[1] for r in rows], [r[2] for r in rows]

    def _send_room(self, room):
        self._apply_room(room)
        _, rcbs = self._briefs(room)
        r = self.state['room']
        self.conn.sendall(self.wire.make_enter_room_ok(room_id=room['room_id'], create_id=room['owner_id'],
            max_num=10, min_num=2, ob_num=0, name=r['name'], room_type=r['room_type'], mode=r['mode'],
            char_vec=rcbs, room_config=self.wire.serialize_room_config(r['map_id'])))
        self.room_revision = room['revision']

    def _send_selection(self, room):
        self._apply_room(room)
        member = next(m for m in room['members'] if m['profile_id'] == self.profile)
        if self.selection_room != room['room_id'] or self.selection_key != room['selection_id']:
            # A reconnect keeps the already selected/locked tank and deadline.
            saved = member['private']['state']
            if (saved.get('tank_selection') and saved.get('multiplayer_room_id') == room['room_id']
                    and saved['tank_selection'].get('shared_selection_id') == room['selection_id']):
                for key in ('tank_id', 'tank_level', 'tank_strength', 'tank_ext', 'halo_id', 'skin_frame_type',
                            'match_tank_uuid', 'tank_selection', 'guild_build', 'booster_build',
                            'crystal_build', 'military_build', 'military_star'):
                    if key in saved:
                        self.state[key] = deepcopy(saved[key])
            else:
                staged = clean_state(self.state)
                prebattle_selection.begin(staged, storage.garage())
                staged['tank_selection']['shared_selection_id'] = room['selection_id']
                self._freeze_build(staged)
                if room['selection_remaining_ms'] > 0 and self.profile not in room['locked_ids']:
                    try:
                        self.hub.select(self.profile, self.generation, public=self._public(staged), private=self._private(staged))
                    except RoomError as exc:
                        if exc.code not in ('selection_locked', 'not_in_room'):
                            raise
                    else:
                        self.state.update(staged)
                    room = self._room()
                    if room is None or room['phase'] != 'selecting':
                        return
            self.selection_room = room['room_id']
            self.selection_key = room['selection_id']
        self.state['tank_selection'] = {'remaining': room['selection_remaining_ms'] / 1000,
                                        'shared_selection_id': room['selection_id']}
        cbs, _ = self._briefs(room)
        locks = list(room['locked_ids']) + [b['acct_id'] for b in room['bots']]
        self._send([(1721, prebattle_selection.pair_payload(self.state, cbs, locks))])
        self.room_revision = room['revision']

    def _selection_update(self, room, account, locked=False):
        member = next((m for m in room['members'] if m['profile_id'] == account), None)
        if not member:
            return
        private = member['private']
        s, ob = private['state'], private['other_brief']
        self.conn.sendall(self.wire.make_room_char_change(private['player_brief'], ob))
        packets = [(1727, prebattle_selection.selected_payload(account, s['tank_id'],
            struct.unpack_from('<I', ob, 25)[0], s.get('skin_frame_type', 0),
            guild_combat.command(s.get('guild_build', {}))))]
        if locked:
            packets.append((1728, struct.pack('<III', account, s['tank_id'], 0)))
        self._send(packets)

    def selection_updated(self):
        if not self.enabled or not self.managed:
            return False
        room = self._room()
        if room and room['phase'] == 'selecting' and self.profile not in room['locked_ids']:
            staged = clean_state(self.state)
            self._freeze_build(staged)
            try:
                self.hub.select(self.profile, self.generation, public=self._public(staged), private=self._private(staged))
            except RoomError as exc:
                if exc.code not in ('selection_locked', 'not_in_room'):
                    raise
                member = next((m for m in room['members'] if m['profile_id'] == self.profile), None)
                if member:
                    saved = member['private']['state']
                    for key in ('tank_id', 'tank_level', 'tank_strength', 'tank_ext', 'halo_id', 'skin_frame_type', 'match_tank_uuid'):
                        if key in saved:
                            self.state[key] = deepcopy(saved[key])
            else:
                self.state.update(staged)
        elif room and room['phase'] == 'waiting':
            self._refresh_profile()
        return True

    def _send_start(self, room):
        self._apply_room(room)
        if not self.battles.bind(self.profile, self.state):
            self.hub.detach(self.profile, self.generation, reason='expired')
            self._clear_local(battle_exit=True)
            self._tip(552, 'Trận trước đã đóng. Bạn có thể tạo hoặc vào phòng mới.')
            return
        self.state.pop('tank_selection', None)
        cbs, _ = self._briefs(dict(room, bots=self.state['bots']))
        self.conn.sendall(self.wire.make_start_game(scene_id=self.state['battle']['scene_id'],
            map_id=room['map_id'], room_type=room['room_type'], char_data=cbs, back_data=[], enter_type=0))
        self.started_room = room['room_id']
        self.conn.settimeout(.02)

    def _create(self, payload=None):
        self._refresh_profile()
        setup = bot_setup.load_setup(self.db)
        if setup['mode'].startswith('test'):
            return False
        s = clean_state(self.state)
        s['battle'] = None
        s['room'] = dict(room_id=0, camp=1, ready=1, room_type=2, map_id=19, mode=3, state=0)
        bot_setup.configure_room(s, self.db, setup=setup)
        if payload is not None:
            name, _ = self.wire.characters.read_string(payload, 10)
            if name.strip():
                s['room']['name'] = name.strip()[:80]
        self.hub.create(self.profile, self.generation, s['room'], s['bots'])
        return True

    def _solo_pve_room_change(self, payload):
        """Route native C600 edits back to the solo waiting-room owner.

        The legacy handler validates unlocks and the frozen balance source
        before replacing the room and replying S1700. Shared rooms must never
        take this route, including a stale shared-room marker after disconnect.
        """
        current = self.state.get('room') or {}
        if current.get('room_type') != 4 or not payload or payload[0] != 4:
            return False
        from pve_catalog import MAPS, resolve_request
        if (self.managed or current.get('map_id') not in MAPS
                or current.get('state', 0) != 0 or self.state.get('battle')
                or self.state.get('tank_selection') or self.started_room):
            raise ValueError('Chỉ đổi màn / độ khó khi đang ở phòng chờ PvE.')
        if len(payload) < 16 or payload[1] != 1:
            raise ValueError('Yêu cầu đổi màn PvE không hợp lệ.')
        _, end = self.wire.characters.read_string(payload, 10)
        if len(payload) != end + 4:
            raise ValueError('Yêu cầu đổi màn PvE không hợp lệ.')
        resolve_request(struct.unpack_from('<I', payload, 2)[0])
        return True

    def _add_bot(self, request):
        room = self._room()
        if room is None:
            return None
        setup = bot_setup.default_setup()
        setup.update(allies=[], enemies=[request['level']],
                     bot_guild=room['config'].get('bot_guild', False),
                     bot_crystals=room['config'].get('bot_crystals', False),
                     bot_military=room['config'].get('bot_military', False))
        bot = bot_setup.make_roster(setup)[0]
        bot['camp'] = request['camp']
        return bot

    def _primary_tank(self, payload):
        """Change an owned lobby tank without editing the authoritative bots."""
        tank_uuid, end = self.wire.characters.read_string(payload, 0)
        if any(payload[end:]):
            raise ValueError('Yêu cầu chọn xe không hợp lệ.')
        record = next((r for r in storage.garage() if r['uuid'] == tank_uuid and r.get('cont_type', 0) == 0), None)
        if record is None or record['tank_id'] not in SUPPORTED_TANKS:
            raise ValueError('Hãy chọn xe đang sở hữu trong kho.')
        old_flags = {r['uuid']: r['ext_flag'] for r in self.wire.garage_for_state(self.state)}
        staged = clean_state(self.state)
        staged.pop('tank_selection', None)
        staged.pop('match_tank_uuid', None)
        staged['selected_tank_uuid'] = record['uuid']
        prebattle_selection.use_tank(staged, record)
        self._freeze_build(staged)
        self.hub.update_waiting_player(self.profile, self.generation,
            public=self._public(staged), private=self._private(staged))
        # Persist only the primary choice, never the local copy of a shared
        # room/battle. A concurrent start has already frozen this same trusted
        # choice under the RoomHub lock before its worker checkpoint.
        fields = ('tank_id', 'tank_level', 'tank_strength', 'tank_ext', 'halo_id', 'skin_frame_type')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            stored = self.db.execute('SELECT state FROM worlds WHERE profile_id=?', (self.profile,)).fetchone()
            if stored is None:
                raise ValueError('Không tìm thấy dữ liệu nhân vật.')
            durable = json.loads(stored[0])
            durable['selected_tank_uuid'] = record['uuid']
            if not durable.get('multiplayer_room_id'):
                durable.update({k: staged[k] for k in fields if k in staged})
            self.db.execute('UPDATE worlds SET state=?,updated_ms=? WHERE profile_id=?',
                (json.dumps(durable, ensure_ascii=False, separators=(',', ':')), time.time_ns()//1000000, self.profile))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.state.update(staged)
        changed = [r for r in self.wire.garage_for_state(self.state) if r['ext_flag'] != old_flags[r['uuid']]]
        changed.sort(key=lambda r: bool(r['ext_flag'] & 2))
        self._send([(1151, self.wire.tank_instance_payload(r)+b'\x08') for r in changed])

    def _rank_group(self, group):
        try:
            prepared = clean_state(self.state)
            prepared['battle'] = prepared['room'] = None
            prepared.pop('tank_selection', None)
            if not ranked_matches.prepare_room(prepared, self.db):
                raise ValueError('Không chuẩn bị được phòng rank.')
            bots = prepared['bots']
            if len(group['account_ids']) == 2:
                removed = next(b for b in bots if b['side'] == 'enemies')
                bots = [b for b in bots if b is not removed]
            for bot in bots:
                bot['rank_owner_id'] = self.profile
            for key in ('rank_bot_roster', 'elo', 'elo_section', 'pre_elo_section', 'season_id'):
                if key in prepared:
                    self.state[key] = deepcopy(prepared[key])
            self._refresh_profile()
            room = self.hub.create_ranked(group['reservation_id'], prepared['room'], bots)
            frozen = self.hub.room_data(room['room_id'])
            ratings = {camp: [int(m['private']['state'].get('elo', 1000)) for m in frozen['members'] if m['camp'] == camp]
                       for camp in (1, 2)}
            fallback = [rating for values in ratings.values() for rating in values]
            policies = {str(camp): 'elite_v1' if ranked_matches.section(max(ratings[camp] or fallback))['type']
                        >= ranked_matches.PRO_MIN_SECTION_TYPE else 'legacy' for camp in (1, 2)}
            self.hub.set_rank_policies(room['room_id'], policies)
            self.hub.begin_selection(self.profile, self.generation)
            return room
        except Exception:
            self.hub.release_rank_group(group['reservation_id'])
            raise

    def _start_claim(self, claim):
        room = self.hub.room_data(claim['room_id'])
        if room is None:
            return
        started = None
        try:
            humans = {}
            for member in room['members']:
                state = deepcopy(member['private']['state'])
                config = deepcopy(room['config'])
                config.update(camp=member['camp'], owner_id=room['owner_id'])
                state.update(room=config, multiplayer_room_id=room['room_id'],
                             multiplayer_object_id=member['object_id'])
                state.pop('tank_selection', None)
                humans[member['profile_id']] = state
            config = dict(room['config'], owner_id=room['owner_id'])
            started = self.battles.start(room['room_id'], config, humans, room['bots'])
            for member in room['members']:
                if not member['connected']:
                    self.battles.disconnect(member['profile_id'])
            self.hub.start_battle(room['room_id'], claim['claim_id'])
        except Exception:
            if started is not None:
                self.battles.abort_start(room['room_id'], started['match_uuid'])
            self.hub.release_selection_claim(room['room_id'], claim['claim_id'])
            self.hub.close_room(room['room_id'], reason='start_failed')
            raise

    def _clear_local(self, *, battle_exit=False, reason='left', actor=None):
        old_scene = (self.state.get('battle') or {}).get('scene_id', self.room_id or 0)
        self._restore_primary()
        self.state['room'] = self.state['battle'] = None
        self.state.pop('multiplayer_room_id', None)
        self.state.pop('multiplayer_object_id', None)
        self.room_id = self.room_revision = self.selection_room = self.started_room = None
        self.selection_key = None
        self.conn.settimeout(.1)
        if battle_exit:
            row = self.db.execute('SELECT state FROM worlds WHERE profile_id=?', (self.profile,)).fetchone()
            if row and row[0]:
                try:
                    fresh = json.loads(row[0])
                    for key in ('level', 'n_exp', 'rating', 'played', 'wins', 'losses', 'draws'):
                        if key in fresh:
                            self.state[key] = fresh[key]
                except Exception:
                    pass
        self.checkpoint()
        if battle_exit:
            import cosmetic_shop
            import inventory
            wallet = cosmetic_shop.snapshot(self.db)
            containers = inventory.load(self.db)
            wallet_pkt = (1039, struct.pack('<HH6i', 2, 0, *wallet['currency']))
            container_pkt = (1100, inventory.list_payload(3, containers.get('3', []), notify_type=2))
            level_pkt = (85, struct.pack('<BIH', 2, int(self.state.get('level', 1)), 0))
            exp_pkt = (85, struct.pack('<BIH', 3, int(self.state.get('n_exp', 0)), 0))
            self._send([(1707, struct.pack('<IIIII', 0, old_scene, self.profile, 2, 0))]
                       + ranked_matches.update_packets(self.state)
                       + [wallet_pkt, container_pkt, level_pkt, exp_pkt, (1017, b'')])
        else:
            self.conn.sendall(self.wire.make_room_op(20 if reason == 'kicked' else 1,
                                actor or self.profile, self.profile))
            self._send([(1017, b'')])
        self._refresh_profile()

    def _leave(self, *, forfeit=False):
        room = self._room()
        if room is None:
            return
        was_battle = bool(self.started_room or (self.state.get('battle') or {}).get('shared'))
        if was_battle:
            projected = self.battles.leave(self.profile, forfeit=forfeit)
            if projected:
                self.state.update(projected)
        self.hub.detach(self.profile, self.generation)
        self._clear_local(battle_exit=was_battle)

    def poll(self, *, force=False):
        if not self.enabled:
            return
        now = time.monotonic()
        if self.started_room:
            packets = self.battles.drain_presented(self.profile)
            if packets:
                self._send(packets)
            if now - self.last_status >= 1:
                self.last_status = now
                status = self.battles.published_status(self.profile)
                if status is None:
                    self.hub.detach(self.profile, self.generation, reason='expired')
                    self._clear_local(battle_exit=True)
                if status and status['finished']:
                    projected = self.battles.snapshot_state(self.profile)
                    if projected:
                        self.state.update(projected)
                if status and status['error']:
                    self._tip(552, 'Lỗi xử lý phòng: ' + status['error'])
        if not force and now - self.last_control < .1:
            return
        self.last_control = now
        if now - self.last_profile >= .5 and not self.started_room:
            self._refresh_profile()
        groups = self.hub.claim_rank_groups(self.profile, self.generation) if not self.managed else ()
        for group in groups:
            try:
                self._rank_group(group)
            except Exception as exc:
                log.exception('Cannot match ranked profiles for %s', self.profile)
                self._tip(REQUEST, exc)
        events = self.hub.drain(self.profile, self.generation)
        if self.started_room and not events and not force:
            return  # Combat packets do not require a room/private-state copy.
        room = self._room()
        if room is None:
            if self.managed:
                left = next((e for e in events if e['kind'] == 'room_left'), {})
                self._clear_local(reason=left.get('reason', 'left'), actor=left.get('actor_id'))
        elif room['phase'] == 'waiting' and not self.started_room:
            if self.selection_room:
                self._restore_primary()
                self.state['battle'] = None
                self.selection_room = None
                self.selection_key = None
                self._send([(1017, b'')])
            if force or self.room_revision != room['revision']:
                self._send_room(room)
        elif room['phase'] == 'selecting':
            if (self.selection_room != room['room_id'] or self.selection_key != room['selection_id']
                    or any(e['kind'] == 'resync_required' for e in events)):
                self._send_selection(room)
                room = self._room()
            for event in events:
                if room and room['phase'] == 'selecting' and event['kind'] == 'selection_changed':
                    self._selection_update(room, event['account_id'], event['locked'])
            for claim in self.hub.claim_selection_starts(self.profile, self.generation):
                try:
                    self._start_claim(claim)
                except Exception as exc:
                    log.exception('Cannot start shared room %s', claim['room_id'])
                    self._tip(552, exc)
            room = self._room()
        if room and room['phase'] == 'battle' and self.started_room != room['room_id']:
            self._send_start(room)
        for event in events:
            if event['kind'] == 'peer_grace_expired' and self.started_room:
                # The hub emits once. Runtime settlement and detach are idempotent.
                self.battles.leave(event['account_id'], forfeit=True)
                self.hub.detach(event['account_id'], reason='disconnected')
        if events or force:
            self._send([(REPLY, encode_reply(self.hub.snapshot(self.profile, self.generation, action='changed')))])

    def handle(self, mid, payload):
        if mid == REQUEST:
            if not self.ready:
                return True
            try:
                request = decode_request(payload)
                self._register()
                if request['action'] == 'rank_queue' and self.state.get('room') is None:
                    with storage.connection(write=True) as opened:
                        self.state.update(ranked_matches.brief(opened, self.profile))
                self._refresh_profile(freeze=request['action'] == 'rank_queue')
                bot = self._add_bot(request) if request['action'] == 'add_bot' else None
                reply = self.hub.command(self.profile, self.generation, request, bot=bot)
                self._send([(REPLY, encode_reply(reply))])
                self.poll(force=True)
            except (ValueError, KeyError) as exc:
                self._tip(REQUEST, exc)
            return True
        if not self.enabled:
            return False
        if mid in INPUT_MESSAGES:
            if self.started_room:
                self.battles.submit_input(self.profile, mid, payload)
                return True
            return self.managed
        if mid not in ROOM_MESSAGES:
            return False  # Heartbeats/economy/login never clone the room roster.
        room = self._room()
        try:
            if mid == 607:
                rooms = [self.wire.serialize_room_info(room_id=r['room_id'], num=r['human_count'],
                    max_num=10, map_id=r['map_id'], mode=r['mode'], state=0, is_validate=1,
                    room_type=r['room_type'], name=r['name']) for r in self.hub.list_rooms()]
                self.conn.sendall(self.wire.make_hall_display_list(rooms=rooms))
                return True
            if mid in (304, 350, 600, 601, 610) and room is None:
                self._refresh_profile()
                queue = self.hub.snapshot(self.profile, self.generation)['queue']
                if queue['active'] and not (mid == 610 and payload == b'\x09'):
                    raise ValueError('Hủy tìm rank trước khi vào phòng khác.')
            if mid in (304, 350) and room:
                raise ValueError('Hãy rời phòng hiện tại trước khi chọn chế độ khác.')
            if mid == 600:
                if room:
                    raise ValueError('Hãy rời phòng hiện tại trước khi tạo phòng mới.')
                if self.state.get('room'):
                    if self._solo_pve_room_change(payload):
                        return False
                    raise ValueError('Hãy rời phòng hiện tại trước khi tạo phòng mới.')
                if len(payload) < 6:
                    raise ValueError('Yêu cầu tạo phòng không hợp lệ.')
                if payload[0] == 4 or struct.unpack_from('<I', payload, 2)[0] >= 200:
                    return False
                if not self._create(payload):
                    return False
            elif mid == 601:
                if len(payload) != 18:
                    raise ValueError('Yêu cầu vào phòng không hợp lệ.')
                code = struct.unpack_from('<I', payload)[0]
                if code:
                    self.hub.join(self.profile, self.generation, str(code))
                else:
                    eligible = [r for r in self.hub.list_rooms() if r['human_count'] < 10]
                    joined = False
                    for candidate in eligible:
                        try:
                            self.hub.join(self.profile, self.generation, candidate['code'])
                            joined = True
                            break
                        except RoomError as exc:
                            if exc.code not in ('room_full', 'room_missing', 'room_started'):
                                raise
                    if not joined and not self._create():
                        return False
            elif mid == 610:
                if payload == b'\x09':
                    if self.state.get('room') is None:
                        with storage.connection(write=True) as opened:
                            self.state.update(ranked_matches.brief(opened, self.profile))
                    self._refresh_profile(freeze=True)
                    self.hub.enqueue_rank(self.profile, self.generation)
                elif room:
                    self.hub.begin_selection(self.profile, self.generation)
                elif self.state.get('room'):
                    return False
                else:
                    if not self._create():
                        return False
                    self.hub.begin_selection(self.profile, self.generation)
            elif room is None:
                return False
            elif mid == 200:
                if room['phase'] != 'waiting':
                    raise ValueError('Hãy chọn xe trong màn chọn trước trận.')
                self._primary_tank(payload)
            elif mid == 602:
                op, target = struct.unpack('<BI', payload)
                if target not in (0, self.profile):
                    raise ValueError('Thao tác không thuộc nhân vật hiện tại.')
                if op == 1:
                    self._leave(forfeit=bool(self.started_room))
                elif op == 24:
                    self.hub.begin_selection(self.profile, self.generation)
                else:
                    values = dict(v=1, request_id=uuid.uuid4().hex)
                    if op in (2, 3):
                        values.update(action='ready', ready=op == 2)
                    elif op == 4:
                        camp = next(m['camp'] for m in room['members'] if m['profile_id'] == self.profile)
                        values.update(action='team', camp=3-camp, revision=room['revision'])
                    else:
                        raise ValueError('Thao tác phòng chưa được hỗ trợ.')
                    reply = self.hub.command(self.profile, self.generation, values)
                    if not reply['ok']:
                        raise ValueError(reply['error']['message'])
            elif mid == 215:
                if (room['phase'] != 'selecting' or self.profile in room['locked_ids']
                        or room['start_claimed'] or room['selection_remaining_ms'] <= 0):
                    raise ValueError('Lượt chọn xe đã được khóa.')
                staged = clean_state(self.state)
                record = prebattle_selection.select(staged, payload, storage.garage(), SUPPORTED_TANKS)
                if record:
                    self._freeze_build(staged)
                    self.hub.select(self.profile, self.generation, public=self._public(staged), private=self._private(staged))
                    self.state.update(staged)
            elif mid == 614:
                if len(payload) != 4 or struct.unpack('<I', payload)[0] != room['room_id']:
                    raise ValueError('Mã phòng chọn xe không khớp.')
                self.hub.lock(self.profile, self.generation)
                self._send([(1728, struct.pack('<III', self.profile, self.state['tank_id'], 0))])
            elif mid == 611:
                if room['phase'] == 'selecting':
                    self._leave()
            elif mid == 605:
                scene, account, delay, progress = struct.unpack('<IIII', payload)
                if account != self.profile or scene != (self.state.get('battle') or {}).get('scene_id'):
                    raise ValueError('Tiến trình tải không thuộc trận hiện tại.')
                self.battles.loaded(self.profile, progress)
            elif mid == 552:
                if len(payload) != 4 or struct.unpack('<I', payload)[0] != (self.state.get('battle') or {}).get('scene_id'):
                    raise ValueError('Trận không còn hợp lệ.')
                packets = self.battles.entry(self.profile)
                self._send(self.battles.present(self.profile, packets))
                self.state['battle'].update(loaded=100, entered=True)
            elif mid in (509, 512, 613):
                if self.started_room:
                    status = self.battles.status(self.profile)
                    if mid == 613 and (not status or not status['finished']):
                        return True
                    if mid != 613 and (len(payload) < 4 or struct.unpack_from('<I', payload)[0] != status['scene_id']):
                        raise ValueError('Mã trận cần rời không khớp.')
                    self._leave(forfeit=bool(status and not status['finished']))
                elif room['phase'] == 'selecting':
                    self._leave()
            elif mid == 603:
                self.conn.sendall(self.wire.make_room_map(room['map_id']))
            elif (mid in (203, 204, 207, 210) and room['phase'] == 'selecting'
                    and (self.profile in room['locked_ids'] or room['start_claimed'] or room['selection_remaining_ms'] <= 0)):
                raise ValueError('Đã chốt xe; hãy đợi trận tiếp theo để thay trang bị.')
            else:
                return False
            self.poll(force=True)
        except (ValueError, KeyError, StopIteration, struct.error) as exc:
            self._tip(mid, str(exc) or 'Thao tác phòng không hợp lệ.')
        return True

    def disconnect(self):
        try:
            if self.managed or self.started_room or (self.state.get('battle') or {}).get('shared'):
                self.battles.disconnect(self.profile)
        finally:
            if self.enabled:
                self.hub.unregister(self.profile, self.generation)
