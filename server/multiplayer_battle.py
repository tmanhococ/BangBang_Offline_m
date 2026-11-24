"""One authoritative native arena per shared room (M37).

GATE threads own their sockets. This worker owns every shared arena and its
SQLite connection; commands are serialized, snapshots commit before fanout,
and native actor IDs stay stable for every recipient. No socket or SQLite
connection crosses a thread boundary. A disconnected last player pauses the
room's clock; reconnect attaches to the same arena rather than cloning it.
"""
from collections import deque, OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import math
import queue
import struct
import threading
import time
import uuid

import storage

log = logging.getLogger(__name__)
TICK_SECONDS = .02
LOADING_TIMEOUT = 20.0
DISCONNECT_GRACE = 120.0
QUEUE_BYTES = 4 * 1024 * 1024
DDL = ('CREATE TABLE IF NOT EXISTS multiplayer_matches ('
       'room_id INTEGER PRIMARY KEY,match_uuid TEXT UNIQUE NOT NULL,phase TEXT NOT NULL,'
       'state TEXT NOT NULL,arena TEXT,updated_ms INTEGER NOT NULL)')


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _clean(state):
    result = {k:deepcopy(v) for k,v in state.items()
              if k not in ('battle','game_token','multiplayer_players')}
    battle = state.get('battle')
    result['battle'] = {k:deepcopy(v) for k,v in battle.items() if k != 'actions'} if battle else None
    return result


def _serialization_view(state):
    """A synchronous worker-owned JSON view; no mutable value escapes."""
    result={k:v for k,v in state.items() if k not in ('battle','game_token','multiplayer_players')}
    battle=state.get('battle')
    result['battle']={k:v for k,v in battle.items() if k!='actions'} if battle else None
    return result


_NAVIGATION = OrderedDict()
_NAVIGATION_GUARD = threading.Lock()

def prepare_navigation(config,human_states,bots):
    """Build immutable walk edges before the shared simulation worker starts.

    A ProV3 planner's first connected-component flood used to stall every
    live room. Geometry/edge order are unchanged; later rooms copy only the
    small dictionaries and share immutable tuples/frozensets. Each room still
    invalidates its own obstacle generation independently.
    """
    if not bots:return None
    from bot_setup import BOT_MAPS
    map_id=config.get('map_id',(next(iter(human_states.values())).get('room') or {}).get('map_id'))
    if map_id not in BOT_MAPS:return None
    key=(str(storage.database_path()),int(map_id))
    with _NAVIGATION_GUARD:
        if key in _NAVIGATION:
            _NAVIGATION.move_to_end(key)
            return _NAVIGATION[key]
        from battle_map import BattleMap
        from bot_ai import Navigation
        geometry=BattleMap(map_id);nav=Navigation(geometry)
        left,top,right,bottom=geometry.bounds
        x0=12+24*math.ceil((left-12)/24)
        y0=12+24*math.ceil((top-12)/24)
        for x in range(x0,math.floor(right)+1,24):
            for y in range(y0,math.floor(bottom)+1,24):
                point=(x,y)
                if point not in nav.components and geometry.can_walk_to(point):
                    nav.reachable(point)
        signature=(tuple(geometry.walls),tuple(geometry.movement_walls),tuple(geometry._walk_objects.items()))
        seed=(dict(nav.components),dict(nav.walk_cells),dict(nav.neighbor_edges),signature)
        _NAVIGATION[key]=seed
        while len(_NAVIGATION)>8:_NAVIGATION.popitem(last=False)
        return seed


def install_navigation(arena,seed):
    if seed is None:return
    from bot_ai import Navigation
    signature=(tuple(arena.geometry.walls),tuple(arena.geometry.movement_walls),tuple(arena.geometry._walk_objects.items()))
    if seed[3]!=signature:return  # A map script changed initial occupancy.
    nav=Navigation(arena.geometry)
    nav.components,nav.walk_cells,nav.neighbor_edges=(dict(part) for part in seed[:3])
    arena.navigation=nav
    arena._multiplayer_navigation_seed=seed


@dataclass
class Mailbox:
    # Only immutable native payloads cross the worker/socket boundary. Both
    # views share one bounded queue so synchronous verification and live GATE
    # draining cannot deliver the same committed event twice.
    raw: deque = field(default_factory=deque)
    native: deque = field(default_factory=deque)
    byte_count: int = 0
    resync: bool = False


@dataclass
class Match:
    room_id: int
    state: dict
    states: dict
    objects: dict
    clock_ms: int
    arena: object = None
    phase: str = 'loading'
    connected: set = field(default_factory=set)
    entered: set = field(default_factory=set)
    initialized: set = field(default_factory=set)
    left: set = field(default_factory=set)
    loaded: dict = field(default_factory=dict)
    disconnected_at: dict = field(default_factory=dict)
    mailboxes: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)
    last_real: float = field(default_factory=time.monotonic)
    finished_at: float = 0.0
    ticks: int = 0
    error: str = ''
    dirty: bool = False
    inputs: deque = field(default_factory=deque)
    world_json: dict = field(default_factory=dict)
    clock_remainder: float = 0.0

    @property
    def queue_bytes(self):
        return {ident:mail.byte_count for ident,mail in self.mailboxes.items()}

    def clock(self):
        return self.clock_ms

    def active(self):
        return bool((self.connected & self.entered) - self.left)


class BattleService:
    def __init__(self):
        self.matches = {}
        self.membership = {}
        self._commands = queue.Queue(maxsize=2048)
        self._guard = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._thread = None
        self._startup_error = None
        self._db = None
        self._mail_lock = threading.Lock()
        self._mailboxes = {}
        self._statuses = {}

    def _ensure(self):
        with self._guard:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='shared-battle', daemon=True)
                self._thread.start()
        if not self._ready.wait(15):
            raise RuntimeError('Máy chủ phòng đang khởi tạo. Hãy thử lại.')
        if self._startup_error is not None:
            raise RuntimeError('Không mở được dữ liệu phòng chơi chung.') from self._startup_error
        if self._closed.is_set():
            raise RuntimeError('Dịch vụ phòng đã dừng.')

    def _call(self, operation, *args):
        self._ensure()
        if threading.current_thread() is self._thread:
            return getattr(self, '_' + operation)(*args)
        done, result = threading.Event(), []
        try:
            self._commands.put_nowait((operation,args,done,result))
        except queue.Full:
            raise ValueError('Phòng đang xử lý nhiều yêu cầu. Hãy thử lại.') from None
        # A timed-out mutation must never execute later behind an exit ACK.
        # The single worker completes the command before its caller proceeds.
        done.wait()
        ok, value = result[0]
        if not ok:
            raise value
        return value

    def _run(self):
        try:
            with storage.connection() as db:
                self._db = db
                self._recover()
                self._ready.set()
                next_tick = time.monotonic() + TICK_SECONDS
                while not self._closed.is_set():
                    remaining = max(0, next_tick-time.monotonic())
                    try:
                        command = self._commands.get(timeout=min(TICK_SECONDS,remaining))
                    except queue.Empty:
                        command = None
                    if command:
                        self._execute(command)
                        command_budget=time.monotonic()+.004
                        for _ in range(31):
                            if time.monotonic() >= command_budget:
                                break
                            try:
                                self._execute(self._commands.get_nowait())
                            except queue.Empty:
                                break
                    now = time.monotonic()
                    if now >= next_tick:
                        dirty=[]
                        for match in tuple(self.matches.values()):
                            try:
                                if self._tick(match,now):dirty.append(match)
                            except Exception as exc:
                                if not match.error:
                                    log.exception('Shared room %s checkpoint/tick failed',match.room_id)
                                match.error = str(exc)
                                self._publish_status(match)
                        if dirty:
                            try:
                                self._persist_many(dirty)
                                for match in dirty:self._publish_committed(match)
                            except Exception as exc:
                                for match in dirty:
                                    if not match.error:log.exception('Shared room batch checkpoint failed')
                                    match.error=str(exc)
                                    self._publish_status(match)
                        for match in tuple(self.matches.values()):
                            if not match.error and not match.dirty:
                                try:self._flush_resyncs(match)
                                except Exception as exc:
                                    log.exception('Shared room %s resnapshot failed',match.room_id)
                                    match.error=str(exc);self._publish_status(match)
                        # Fixed cadence includes computation time. Do not add
                        # another full20ms after already spending it on rooms.
                        next_tick=max(next_tick+TICK_SECONDS,time.monotonic())
        except Exception as exc:
            self._startup_error = exc
            log.exception('Shared battle worker stopped')
        finally:
            self._ready.set()
            self._closed.set()
            while True:
                try:
                    _,_,done,result=self._commands.get_nowait()
                except queue.Empty:
                    break
                if result is not None:result.append((False,RuntimeError('Dịch vụ phòng đã dừng.')))
                if done is not None:done.set()

    def _execute(self,command):
        operation,args,done,result=command
        try:
            value=getattr(self,'_'+operation)(*args)
            if result is not None:result.append((True,value))
        except Exception as exc:
            if result is not None:result.append((False,exc))
            else:log.debug('Shared input rejected: %s',exc)
        finally:
            if done is not None:done.set()

    def _recover(self):
        """A restart never restores per-account copies of one shared battle.

        Keep the canonical checkpoint for diagnostics, close only unfinished
        shared references, and apply no fabricated ranked result/penalty.
        Normal solo saves and already committed shared histories are intact.
        """
        import ranked_matches
        db=self._db
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute(DDL)
            ranked_matches.maintain(db)
            db.execute("UPDATE multiplayer_matches SET phase='interrupted' WHERE phase IN ('loading','battle')")
            for row in db.execute('SELECT profile_id,state FROM worlds').fetchall():
                state=json.loads(row['state'])
                if not state.get('multiplayer_room_id'):
                    continue
                state.pop('multiplayer_room_id',None)
                state.pop('multiplayer_object_id',None)
                state.pop('multiplayer_left',None)
                state['room']=state['battle']=None
                state['multiplayer_notice']='Máy chủ đã khởi động lại. Phòng cũ đã đóng; trận chưa xong không cộng/trừ điểm.'
                db.execute('UPDATE worlds SET state=?,arena=NULL,updated_ms=? WHERE profile_id=?',
                           (_json(state),time.time_ns()//1000000,row['profile_id']))
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def start(self,room_id,room_config,human_states,bots):
        seed=prepare_navigation(room_config,human_states,bots)
        return self._call('start',int(room_id),deepcopy(room_config),deepcopy(human_states),deepcopy(bots),seed)

    def _start(self,room_id,config,human_states,bots,navigation_seed=None):
        from battle_runtime import BattleArena
        from bot_setup import BOT_MAPS
        if room_id in self.matches:
            old=self.matches[room_id]
            if set(old.states)==set(human_states) and old.phase in ('loading','battle'):
                return self._status(next(iter(old.states)))
            raise ValueError('Phòng đã có một trận đang chạy.')
        if not human_states or len(human_states)+len(bots)>10:
            raise ValueError('Phòng cần 1–10 người/bot và tối đa 10 vị trí.')
        members={int(m['profile_id']):m for m in config.get('members',[])}
        owner=int(config.get('owner_id',config.get('host_id',next(iter(human_states)))))
        if owner not in human_states:
            raise ValueError('Chủ phòng không còn trong danh sách người chơi.')
        room=dict(human_states[owner].get('room') or {})
        # Private native brief bytes never enter the persistent canonical world.
        room.update({k:deepcopy(v) for k,v in config.items() if k not in ('members','bots','private','config')})
        room.update(room_id=room_id,room_type=9 if config.get('ranked',room.get('ranked')) else 2,
                    max_num=10,multiplayer=True)
        room['ranked']=room['room_type']==9
        if room.get('map_id') not in BOT_MAPS or room.get('tank_test'):
            raise ValueError('Bản đồ này chưa hỗ trợ phòng nhiều người.')
        states,objects,occupied,camps={}, {}, set(), {1:0,2:0}
        for ident,original in human_states.items():
            ident=int(ident)
            if ident in self.membership:
                raise ValueError('Một người chơi đã ở trong trận khác.')
            if int(original.get('char_id',0)) != ident:
                raise ValueError('Nhân vật không thuộc phiên đã xác thực.')
            member=members.get(ident,{})
            object_id=int(original.get('multiplayer_object_id',member.get('object_id',1 if ident==owner else 0)))
            camp=int((original.get('room') or {}).get('camp',member.get('camp',0)))
            if not 1<=object_id<=10 or object_id in occupied or camp not in (1,2):
                raise ValueError('Vị trí người chơi không hợp lệ.')
            if ident==owner and object_id!=1:
                raise ValueError('Vị trí chủ phòng đã thay đổi trước khi bắt đầu.')
            occupied.add(object_id);camps[camp]+=1;objects[ident]=object_id
            state=_clean(original)
            state.pop('multiplayer_left',None)
            state.update(multiplayer_room_id=room_id,multiplayer_object_id=object_id)
            state['room']=dict(room,camp=camp)
            states[ident]=state
        if room['ranked']:
            # A queue/selection can span a season boundary. Freeze from each
            # authenticated profile's durable current-season row, never a
            # stale HUD medal or the room owner's rating copied to a guest.
            import ranked_matches
            try:
                self._db.execute('BEGIN IMMEDIATE')
                for ident,state in states.items():
                    context=ranked_matches.brief(self._db,ident)
                    state.update(context)
                    state['room']['rank_season_id']=context['season_id']
                # Bots keep their own earned rank. A season rollover must not
                # carry Gold gear over from a previous season's lobby snapshot.
                bot_ranks=[]
                for bot in bots:
                    bot_owner=int(bot.get('rank_owner_id',owner))
                    if bot_owner not in states:
                        raise ValueError('Hồ sơ rank của bot không thuộc trận này.')
                    context=states[bot_owner]
                    bot_ranks.append(ranked_matches._rank_fields(self._db,context['season_id'],
                        bot_owner,ranked_matches._bot_id(bot),ranked_matches.bot_rating(context['elo'])))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
        if room['ranked']:
            import bot_loadouts
            # Template reads / equipment assembly are outside the rank writer.
            for bot,fields in zip(bots,bot_ranks):
                bot.update(fields)
                bot_loadouts.apply(bot,room)
        host_camp=states[owner]['room']['camp']
        accounts=set(states)
        for bot in bots:
            object_id=int(bot['object_id'])
            camp=int(bot.get('camp',host_camp if bot.get('side')=='allies' else 3-host_camp))
            if (not 1<=object_id<=10 or object_id in occupied or camp not in (1,2)
                    or int(bot['acct_id']) in accounts or bot.get('human')):
                raise ValueError('Vị trí/nhân vật bot bị trùng.')
            occupied.add(object_id);accounts.add(int(bot['acct_id']));camps[camp]+=1
            bot.update(camp=camp,side='allies' if camp==host_camp else 'enemies',in_room=True)
            if room['ranked']:
                bot.setdefault('rank_owner_id',owner)
        if not all(1<=count<=5 for count in camps.values()):
            raise ValueError('Mỗi phe phải có từ 1 đến 5 người/bot.')
        match_uuid=uuid.uuid4().hex
        now_ms=time.time_ns()//1000000
        scene_id=int(config.get('scene_id',room_id))
        battle=dict(scene_id=scene_id,room_id=room_id,map_id=room['map_id'],loaded=0,
                    entered=False,shared=True,match_uuid=match_uuid,match_started_at=now_ms//1000)
        policies=dict(room.get('bot_ai_camps') or {
            str(host_camp):room.get('bot_ai_allies',room.get('bot_ai','legacy')),
            str(3-host_camp):room.get('bot_ai_enemies',room.get('bot_ai','legacy'))})
        if room['ranked']:
            from ranked_matches import section, PRO_MIN_SECTION_TYPE
            by_camp={camp:[state for state in states.values() if state['room']['camp']==camp] for camp in (1,2)}
            for camp in (1,2):
                humans=by_camp[camp] or list(states.values())
                rating=max(int(state.get('elo',1000)) for state in humans)
                policies[str(camp)]='elite_v1' if section(rating)['type']>=PRO_MIN_SECTION_TYPE else 'legacy'
        if policies:
            battle['bot_ai_camps']={str(c):policies.get(str(c),policies.get(c,'legacy')) for c in (1,2)}
        for state in states.values():
            if battle.get('bot_ai_camps'):state['room']['bot_ai_camps']=deepcopy(battle['bot_ai_camps'])
            state['battle']=deepcopy(battle)
            state['bots']=deepcopy(bots)
        canonical=deepcopy(states[owner])
        canonical['bots']=bots
        canonical['multiplayer_players']=[dict(object_id=objects[ident],camp=state['room']['camp'],state=state)
                                          for ident,state in states.items()]
        match=Match(room_id,canonical,states,objects,now_ms)
        match.connected=set(states)
        match.mailboxes={ident:Mailbox() for ident in states}
        match.loaded={ident:0 for ident in states}
        with storage.for_profile(owner):
            match.arena=BattleArena(canonical,clock=match.clock,bot_attacks=True)
        install_navigation(match.arena,navigation_seed)
        self._persist(match)
        self.matches[room_id]=match
        self.membership.update({ident:room_id for ident in states})
        with self._mail_lock:self._mailboxes.update(match.mailboxes)
        return self._status(owner)

    def _match(self,profile):
        ident=int(profile)
        room_id=self.membership.get(ident)
        return self.matches.get(room_id) if room_id is not None else None

    def status(self,profile):
        return self._call('status',int(profile))

    def _status(self,profile):
        match=self._match(profile)
        if match is None:
            return None
        return self._status_of(match,profile)

    def _status_of(self,match,profile):
        return dict(room_id=match.room_id,scene_id=match.arena.scene_id,object_id=match.objects[profile],
                    phase=match.phase,finished=bool(match.arena.pk['finished']),
                    entered=profile in match.entered,connected=profile in match.connected,
                    match_uuid=match.state['battle']['match_uuid'],ticks=match.ticks,
                    error=match.error,ranked=bool(match.state['room'].get('ranked')))

    def _publish_status(self,match):
        values={ident:self._status_of(match,ident) for ident in match.states if ident not in match.left}
        with self._mail_lock:self._statuses.update(values)

    def published_status(self,profile):
        """Last committed metadata; never wait behind another room's tick."""
        if self._closed.is_set():return None
        with self._mail_lock:
            value=self._statuses.get(int(profile))
            return dict(value) if value is not None else None

    def _clear_mailbox(self,match,profile):
        with self._mail_lock:
            mail=match.mailboxes[profile]
            mail.raw.clear();mail.native.clear();mail.byte_count=0;mail.resync=False

    def _retire_mailbox(self,match,profile):
        self._clear_mailbox(match,profile)
        with self._mail_lock:
            if self._mailboxes.get(profile) is match.mailboxes[profile]:
                self._mailboxes.pop(profile,None)
                self._statuses.pop(profile,None)

    def snapshot_state(self,profile):
        return self._call('snapshot_state',int(profile))

    def _snapshot_state(self,profile):
        match=self._match(profile)
        if match is None:
            return None
        state=match.states[profile]
        return {key:deepcopy(state[key]) for key in ('room','battle','multiplayer_room_id','multiplayer_object_id',
                'elo','elo_section','pre_elo_section','season_id') if key in state}

    def bind(self,profile,session_state):
        snapshot=self._call('bind',int(profile))
        if snapshot is None:
            if session_state.get('multiplayer_room_id'):
                session_state.pop('multiplayer_room_id',None)
                session_state.pop('multiplayer_object_id',None)
                session_state['room']=session_state['battle']=None
            return False
        session_state.update(snapshot)
        return True

    def _bind(self,profile):
        match=self._match(profile)
        if match is None:
            return None
        if profile not in match.connected:
            self._advance_clock(match,time.monotonic())
            match.connected.add(profile)
        match.disconnected_at.pop(profile,None)
        snapshot=self._snapshot_state(profile)
        snapshot['bots']=deepcopy(match.state['bots'])
        return snapshot

    def loaded(self,profile,progress):
        return self._call('loaded',int(profile),int(progress))

    def _loaded(self,profile,progress):
        match=self._match(profile)
        if match is None or not 0<=progress<=100:
            raise ValueError('Tiến trình tải phòng không hợp lệ.')
        match.loaded[profile]=progress
        match.states[profile]['battle']['loaded']=progress
        packet=(1709,struct.pack('<III',profile,15,progress))
        self._fanout(match,[packet],loading=True)

    def entry(self,profile):
        return self._call('entry',int(profile))

    def _entry(self,profile):
        match=self._match(profile)
        if match is None:raise ValueError('Phòng chơi không còn tồn tại.')
        if profile in match.initialized:return []
        was_active=match.active()
        self._advance_clock(match,time.monotonic())
        if match.phase=='battle' and not was_active:self._rebase_clock(match)
        match.connected.add(profile);match.entered.add(profile)
        match.disconnected_at.pop(profile,None)
        match.loaded[profile]=100
        match.states[profile]['battle'].update(loaded=100,entered=True)
        if match.phase=='loading':
            if not (match.connected-match.entered-match.left):
                return self._open_barrier(match,profile)
            self._persist(match)
            self._fanout(match,[(1709,struct.pack('<III',profile,15,100))],loading=True)
            return []
        self._persist(match)
        return self._snapshot_packets(match,profile)

    def _snapshot_packets(self,match,profile):
        from battle_entry import entry_packets
        from battle_persistence import resume_packets
        actor=match.arena.actors[match.objects[profile]]
        self._clear_mailbox(match,profile)
        match.initialized.add(profile)
        rates=[(1709,struct.pack('<III',ident,15,match.loaded[ident])) for ident in match.states]
        rates += [(1709,struct.pack('<III',bot['acct_id'],10,100)) for bot in match.state['bots']]
        return rates+entry_packets(match.states[profile],match.arena,actor)+resume_packets(match.arena,actor)

    def _open_barrier(self,match,direct_profile=None):
        self._rebase_clock(match)
        match.phase='battle';match.last_real=time.monotonic()
        self._persist(match)
        direct=[]
        for ident in sorted((match.connected & match.entered)-match.left):
            packets=self._snapshot_packets(match,ident)
            if ident==direct_profile:direct=packets
            else:self._queue_for(match,ident,packets)
        return direct

    def _rebase_clock(self,match):
        """Keep paused time while restoring the native S1003 UTC epoch.

        The existing checkpoint rebasing covers every native cooldown, buff,
        callback and map deadline. Adopt it into the SAME shared arena object;
        peers never receive separate simulations or duplicate entry snapshots.
        """
        real_ms=time.time_ns()//1000000
        if real_ms==match.clock_ms:return
        from battle_persistence import dump_arena,restore_arena
        arena=match.arena;seed=getattr(arena,'_multiplayer_navigation_seed',None)
        snapshot=dump_arena(arena)
        previous=match.clock_ms
        match.clock_ms=real_ms;match.clock_remainder=0.0
        try:restored=restore_arena(match.state,snapshot,clock=match.clock)
        except BaseException:
            match.clock_ms=previous
            raise
        arena.__dict__.clear();arena.__dict__.update(restored.__dict__)
        for actor in arena.actors.values():actor.arena=arena
        arena.pending=[(due,order,getattr(arena,callback.__name__) if callback.__self__ is restored else callback,args)
                       for due,order,callback,args in arena.pending]
        install_navigation(arena,seed)
        match.last_real=time.monotonic()

    def submit_input(self,profile,mid,payload):
        """Nonblocking authenticated input; output still waits for commit.

        The GATE does not wait a worker round merely to enqueue movement. Exit,
        bind, entry and settlement remain synchronous ordered barriers.
        """
        self._ensure()
        if int(mid) not in (501,503,510,514) or not {501:9,503:13,510:12,514:8}[int(mid)]<=len(payload)<=128:return False
        try:
            self._commands.put_nowait(('handle',(int(profile),int(mid),bytes(payload)),None,None))
        except queue.Full:return False
        return True

    def handle(self,profile,mid,payload):
        return self._call('handle',int(profile),int(mid),bytes(payload))

    def _handle(self,profile,mid,payload):
        match=self._match(profile)
        if (match is None or match.phase!='battle' or profile not in match.connected
                or profile not in match.entered or mid not in (501,503,510,514)
                or not {501:9,503:13,510:12,514:8}[mid]<=len(payload)<=128):
            return
        if mid==503:
            scene,target,slot=struct.unpack_from('<IIB',payload)
            if (target and target not in match.arena.actors) or slot not in (0,1,2,4,10):return
        if match.error:raise ValueError('Phòng đang chờ lưu dữ liệu. Hãy thử lại.')
        if len(match.inputs)>=256:raise ValueError('Quá nhiều thao tác trong một nhịp phòng.')
        # All participants submit to one native20ms input frame. One world
        # advance precedes that frame; another human never adds a duplicate
        # actor-update pass or a second simulation clock to the same room.
        match.inputs.append((profile,mid,payload))

    def _advance_clock(self,match,now):
        elapsed=max(0,now-match.last_real)
        match.last_real=now
        if match.phase=='battle' and match.active():
            precise=elapsed*1000+match.clock_remainder
            whole=int(precise);match.clock_remainder=precise-whole
            match.clock_ms+=whole

    def _tick(self,match,now):
        expired=[ident for ident,at in match.disconnected_at.items()
                 if ident not in match.left and now-at>=DISCONNECT_GRACE]
        remaining=set(match.states)-match.left
        if expired and not (match.connected-match.left):
            # With nobody present, an earlier disconnect must not award the
            # other offline participant a win. Wait for the last reconnect
            # grace to expire, then retire the shared match without a result.
            if set(expired)==remaining:self._abandon(match)
            return
        for ident in expired:
            self._leave(ident,True)
        if (match.phase=='loading' and match.active()
                and (not (match.connected-match.entered-match.left) or now-match.created>=LOADING_TIMEOUT)):
            self._open_barrier(match)
            now=time.monotonic()
        self._advance_clock(match,now)
        if match.phase=='battle' and match.active() and not match.arena.pk['finished'] and not match.error:
            match.ticks+=1
            match.pending.extend(match.arena.tick());match.dirty=True
            try:
                while match.inputs:
                    profile,mid,payload=match.inputs.popleft()
                    if profile not in match.connected or profile not in match.entered or profile in match.left:continue
                    match.arena._shared_input_dispatch=True
                    match.pending.extend(match.arena.handle_for(match.objects[profile],mid,payload))
            finally:
                match.arena._shared_input_dispatch=False
        if match.pending or (match.arena.pk['finished'] and not match.state['battle'].get('completed_recorded')):
            match.dirty=True
        # Retire arenas only after every participant explicitly left; offline
        # participants keep their stable actor slot for a genuine reconnect.
        if len(match.left)==len(match.states) and (match.phase=='finished' or not match.active()):
            self.matches.pop(match.room_id,None)
        return match.dirty

    def _persist(self,match):
        self._persist_many([match])

    def _persist_many(self,matches):
        from battle_persistence import dump_arena
        import ranked_matches
        # The graph snapshot is computed once per room, not once per viewer.
        snapshots={m.room_id:dump_arena(m.arena) for m in matches}
        db=self._db;results=[]
        try:
            db.execute('BEGIN IMMEDIATE')
            for match in matches:
                settled=merged=None
                phase='finished' if match.arena.pk['finished'] else match.phase
                if match.arena.pk['finished'] and not match.state['battle'].get('completed_recorded'):
                    settled,merged=ranked_matches.settle_shared(db,match.states,match.arena)
                states=settled or match.states
                canonical=_serialization_view(match.state)
                canonical['multiplayer_players']=[dict(object_id=match.objects[ident],camp=state['room']['camp'],state=state)
                                                  for ident,state in states.items()]
                if settled is not None:
                    canonical['battle'].update(completed_recorded=True,rank_result=merged)
                db.execute('INSERT INTO multiplayer_matches VALUES(?,?,?,?,?,?) '
                           'ON CONFLICT(room_id) DO UPDATE SET match_uuid=excluded.match_uuid,phase=excluded.phase,'
                           'state=excluded.state,arena=excluded.arena,updated_ms=excluded.updated_ms',
                           (match.room_id,canonical['battle']['match_uuid'],phase,_json(canonical),snapshots[match.room_id],time.time_ns()//1000000))
                world_json={}
                for ident,state in states.items():
                    if ident in match.left:continue
                    encoded=_json(_serialization_view(state))
                    if match.world_json.get(ident)!=encoded:
                        db.execute('UPDATE worlds SET state=?,arena=NULL,updated_ms=? WHERE profile_id=?',
                                   (encoded,time.time_ns()//1000000,ident))
                        world_json[ident]=encoded
                results.append((match,phase,settled,merged,world_json))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        for match,phase,settled,merged,world_json in results:
            match.world_json.update(world_json)
            if settled is not None:
                for ident,state in settled.items():
                    match.states[ident].clear();match.states[ident].update(state)
                match.state['battle'].update(completed_recorded=True,rank_result=merged)
            match.phase=phase;match.dirty=False
            if phase=='finished' and not match.finished_at:match.finished_at=time.monotonic()
            self._publish_status(match)

    def _commit_and_publish(self,match,packets):
        match.pending.extend(packets);match.dirty=True
        self._persist(match)
        self._publish_committed(match)

    def _publish_committed(self,match):
        if any(mid==1708 for mid,_ in match.pending):
            import ranked_matches
            import match_rewards
            for ident,state in match.states.items():
                try:
                    with storage.for_profile(ident):
                        with storage.connection(write=True) as db:
                            match_rewards.apply_pvp_rewards(state, match.arena, db)
                except Exception as exc:
                    log.warning('[multiplayer] PvP rewards error for %s: %s', ident, exc)
            from pk_rules import settlement
            match.pending=[packet for packet in match.pending if packet[0]!=1708]+[settlement(match.arena)]
            from battle_participants import private
            for ident,state in match.states.items():
                actor=match.arena.actors[match.objects[ident]]
                match.pending.extend(private(actor,packet) for packet in ranked_matches.update_packets(state))
        self._fanout(match,match.pending)
        match.pending=[];match.error=''

    def _queue_for(self,match,ident,packets):
        mail=match.mailboxes[ident]
        with self._mail_lock:
            if mail.resync:return
        actor_id=match.objects[ident]
        raw=[]
        for packet in packets:
            viewers=getattr(packet,'viewers',None)
            if (viewers is not None and actor_id not in viewers) or actor_id in getattr(packet,'exclude',()):continue
            raw.append(packet)
        if not raw:return
        # Presentation reads live arena/cosmetic metadata only in its owning
        # thread, after durability. Socket threads receive completed bytes.
        native=self._present(ident,raw)
        size=max(sum(len(p[1])+8 for p in raw),sum(len(p[1])+8 for p in native))
        with self._mail_lock:
            mail.raw.extend(raw);mail.native.extend(native);mail.byte_count+=size
            if mail.byte_count>QUEUE_BYTES:
                mail.raw.clear();mail.native.clear();mail.byte_count=0;mail.resync=True

    def _flush_resyncs(self,match):
        with self._mail_lock:
            waiting=[ident for ident,mail in match.mailboxes.items() if mail.resync
                     and ident in match.connected and ident in match.initialized and ident not in match.left]
        for ident in waiting:self._queue_for(match,ident,self._snapshot_packets(match,ident))

    def _fanout(self,match,packets,*,loading=False):
        for ident in match.connected-match.left:
            if not loading and ident not in match.initialized:continue
            self._queue_for(match,ident,packets)

    def drain(self,profile):
        return self._call('drain',int(profile))

    def _drain(self,profile):
        match=self._match(profile)
        if match is None:return []
        mail=match.mailboxes[profile]
        with self._mail_lock:
            resync=mail.resync
            if not resync:
                packets=list(mail.raw)
                mail.raw.clear();mail.native.clear();mail.byte_count=0
                return packets
        return self._snapshot_packets(match,profile) if profile in match.initialized else []

    def drain_presented(self,profile):
        """Nonblocking committed output: a slow room never parks GATE I/O."""
        with self._mail_lock:
            mail=self._mailboxes.get(int(profile))
            if mail is None or mail.resync:return []
            packets=list(mail.native)
            mail.raw.clear();mail.native.clear();mail.byte_count=0
            return packets

    def present(self,profile,packets):
        return self._call('present',int(profile),list(packets))

    def _present(self,profile,packets):
        from battle_participants import for_viewer
        from skin_effects import present_packets
        match=self._match(profile)
        if match is None:
            return []
        actor=match.arena.actors[match.objects[profile]]
        return present_packets(for_viewer(packets,actor),match.states[profile],match.arena,actor)

    def disconnect(self,profile):
        return self._call('disconnect',int(profile))

    def _disconnect(self,profile):
        match=self._match(profile)
        if match is None:
            return
        self._advance_clock(match,time.monotonic())
        actor=match.arena.actors[match.objects[profile]]
        from combat_protocol import stop_move
        actor.position=actor.position_at(match.clock());actor.motion=None
        match.connected.discard(profile);match.entered.discard(profile);match.initialized.discard(profile)
        match.inputs=deque(item for item in match.inputs if item[0]!=profile)
        match.disconnected_at.setdefault(profile,time.monotonic())
        match.states[profile]['battle']['entered']=False
        self._clear_mailbox(match,profile)
        self._commit_and_publish(match,[stop_move(match.arena.scene_id,actor.id,actor.position)])

    def leave(self,profile,forfeit=False):
        return self._call('leave',int(profile),bool(forfeit))

    def _leave(self,profile,forfeit):
        match=self._match(profile)
        if match is None:
            return None
        self._advance_clock(match,time.monotonic())
        actor=match.arena.actors[match.objects[profile]]
        if forfeit and match.state['room'].get('ranked') and not match.arena.pk['finished']:
            from pk_rules import finish,settlement
            finish(match.arena,match.clock())
            match.arena.pk['winner']=3-actor.camp
            match.states[profile]['battle']['forfeit']=True
            self._commit_and_publish(match,[settlement(match.arena)])
        elif not match.arena.pk['finished']:
            from combat_protocol import scene
            actor.position=actor.position_at(match.clock());actor.motion=None
            actor.generation+=1;actor.info['in_room']=False
            match.states[profile]['multiplayer_left']=True
            if actor.id==1:match.state['multiplayer_left']=True
            self._commit_and_publish(match,[scene(removed=(struct.pack('<I',actor.id),))])
        projected=self._snapshot_state(profile)
        lobby=self._lobby_state(match.states[profile])
        try:
            self._db.execute('BEGIN IMMEDIATE')
            self._db.execute('UPDATE worlds SET state=?,arena=NULL,updated_ms=? WHERE profile_id=?',
                             (_json(lobby),time.time_ns()//1000000,profile))
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        match.disconnected_at.pop(profile,None)
        match.left.add(profile);match.connected.discard(profile);match.entered.discard(profile);match.initialized.discard(profile)
        match.inputs=deque(item for item in match.inputs if item[0]!=profile)
        self._clear_mailbox(match,profile)
        self.membership.pop(profile,None)
        self._retire_mailbox(match,profile)
        # The caller refreshes its session/primary tank from this projection;
        # offline expiry already has a durable clean lobby without a GATE.
        return projected

    def abort_start(self,room_id,match_uuid):
        return self._call('abort_start',int(room_id),str(match_uuid))

    def _abort_start(self,room_id,match_uuid):
        match=self.matches.get(room_id)
        if (match is None or match.state['battle']['match_uuid']!=match_uuid
                or match.entered or match.phase!='loading'):
            return False
        self._abandon(match,phase='interrupted',notice='Phòng đã hủy trước khi bắt đầu. Không cộng/trừ điểm.')
        return True

    def _lobby_state(self,state):
        clean=_clean(state)
        if clean.get('match_tank_uuid'):
            records=[json.loads(row[0]) for row in self._db.execute(
                'SELECT data FROM garage WHERE profile_id=? ORDER BY ordinal',(clean['char_id'],))]
            primary=next((r for r in records if r.get('uuid')==clean.get('selected_tank_uuid')),None)
            if primary is not None:
                from prebattle_selection import use_tank
                use_tank(clean,primary)
        for key in ('multiplayer_room_id','multiplayer_object_id','multiplayer_left','match_tank_uuid','tank_selection'):
            clean.pop(key,None)
        clean['room']=clean['battle']=None
        return clean

    def _abandon(self,match,*,phase='abandoned',notice=None):
        # Every human exceeded reconnect grace. There is no arbitrary winning
        # side and no new ranked penalty merely because detach order differs.
        try:
            self._db.execute('BEGIN IMMEDIATE')
            for ident,state in match.states.items():
                if ident in match.left:continue
                lobby=self._lobby_state(state)
                lobby['multiplayer_notice']=notice or 'Phòng đã đóng sau khi mọi người mất kết nối. Trận chưa xong không cộng/trừ điểm.'
                self._db.execute('UPDATE worlds SET state=?,arena=NULL,updated_ms=? WHERE profile_id=?',
                                 (_json(lobby),time.time_ns()//1000000,ident))
            self._db.execute('UPDATE multiplayer_matches SET phase=?,updated_ms=? WHERE room_id=?',
                             (phase,time.time_ns()//1000000,match.room_id))
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        for ident in match.states:
            self.membership.pop(ident,None)
            self._retire_mailbox(match,ident)
        match.phase=phase;match.left.update(match.states)
        self.matches.pop(match.room_id,None)

    def close(self):
        self._closed.set()
        thread=self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)


service = BattleService()
