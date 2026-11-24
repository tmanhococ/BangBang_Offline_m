"""Local ranked play, durable settlement and bounded match history (M8.13).

Native PvpSectionTemplate thresholds; the offline scoring/reset rules are V1:
start1000, win+150, loss-15, draw0, floor1000. A season is14 days in UTC+7.
Only authoritative completed arenas change ratings. M37 shared rooms settle
each authenticated human from their own camp in one transaction. Remaining
virtual slots retain their persistent per-owner profiles.
Their tank/name/cosmetics/level do not reroll; one fixed reserve avoids an
allied duplicate when the human changes tank. No registered accounts are made.
New ranked rooms use
PRO V3 from Diamond upwards and the original AI below Diamond, based on the
human owner's current-season rating when the room is created. The bots' lower
displayed medal does not select the controller.
"""
import json
import bot_cosmetics
import random
import struct
import time
import uuid
from datetime import datetime, timezone, timedelta

import storage

SEASON_SECONDS = 14 * 86400
ANCHOR = int(datetime(2026, 10, 28, tzinfo=timezone(timedelta(hours=7))).timestamp())
INITIAL_RATING = 1000
MAX_RATING = 32767  # Native PLAYER_BRIEF / TANK_SETTLE_INFO signed short.
HISTORY_LIMIT = 20
PRO_MIN_SECTION_TYPE = 5  # Native Kim Cương / Diamond medal, beginning at1600.
BOT_PROFILE_VERSION = 1
BOT_IDS = tuple(range(2, 11))
RESERVE_ID = 11
SECTIONS = tuple((i + 1, 1000 + i * 50, 1 if i < 3 else 2 if i < 6 else
                  3 if i < 9 else 4 if i < 12 else 5 if i < 16 else 6,
                  name) for i, name in enumerate((
    'Đồng I', 'Đồng II', 'Đồng III', 'Bạc I', 'Bạc II', 'Bạc III',
    'Vàng I', 'Vàng II', 'Vàng III', 'Bạch Kim I', 'Bạch Kim II', 'Bạch Kim III',
    'Kim Cương I', 'Kim Cương II', 'Kim Cương III', 'Kim Cương IV', 'Chỉ Huy')))
DDL = (
    'CREATE TABLE IF NOT EXISTS rank_seasons (id INTEGER PRIMARY KEY,starts_at INTEGER NOT NULL,ends_at INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS rank_entries (season_id INTEGER NOT NULL REFERENCES rank_seasons(id) ON DELETE CASCADE,'
    'entry_key TEXT NOT NULL,profile_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,is_bot INTEGER NOT NULL,'
    'object_id INTEGER NOT NULL,rating INTEGER NOT NULL CHECK(rating BETWEEN 1000 AND 32767),'
    'played INTEGER NOT NULL DEFAULT 0,wins INTEGER NOT NULL DEFAULT 0,losses INTEGER NOT NULL DEFAULT 0,'
    'draws INTEGER NOT NULL DEFAULT 0,updated_ms INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(season_id,entry_key))',
    'CREATE INDEX IF NOT EXISTS rank_order ON rank_entries(season_id,rating DESC,updated_ms,entry_key)',
    'CREATE TABLE IF NOT EXISTS match_history (profile_id INTEGER NOT NULL,match_uuid TEXT NOT NULL,'
    'season_id INTEGER NOT NULL REFERENCES rank_seasons(id) ON DELETE CASCADE,ended_ms INTEGER NOT NULL,'
    'ranked INTEGER NOT NULL,summary TEXT NOT NULL,PRIMARY KEY(profile_id,match_uuid))',
    'CREATE INDEX IF NOT EXISTS match_recent ON match_history(profile_id,ended_ms DESC,match_uuid DESC)',
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def season_number(now=None):
    return max(1, 1 + (int(time.time() if now is None else now) - ANCHOR) // SEASON_SECONDS)


def maintain(db, now=None):
    """Called at startup/queries/new matches, never for each combat tick."""
    for statement in DDL:
        db.execute(statement)
    last = db.execute('SELECT max(id) FROM rank_seasons').fetchone()[0] or 1
    current = max(last, season_number(now))  # A backwards system clock cannot erase a newer season.
    for ident in range(max(1, current - 1), current + 1):
        start = ANCHOR + (ident - 1) * SEASON_SECONDS
        db.execute('INSERT OR IGNORE INTO rank_seasons VALUES(?,?,?)', (ident, start, start + SEASON_SECONDS))
    db.execute('DELETE FROM rank_seasons WHERE id<?', (current - 1,))
    return current


def initialize():
    with storage.connection(write=True) as db:
        maintain(db)


def section(rating):
    row = next(row for row in reversed(SECTIONS) if max(INITIAL_RATING, int(rating)) >= row[1])
    return dict(id=row[0], threshold=row[1], type=row[2], name=row[3])


def bot_rating(rating):
    """Initial seed only; earned bot ratings are never rebased to the human."""
    current = section(rating)
    lower = [row for row in SECTIONS if row[2] == max(1, current['type'] - 1)]
    own = [row for row in SECTIONS if row[2] == current['type']]
    if current['type'] == 1:
        return INITIAL_RATING  # Bronze has no invented tier below it.
    offset = max(0, min(len(lower) - 1, current['id'] - own[0][0]))
    return lower[offset][1]


def _key(ident, object_id=0):
    return f'bot:{ident}:{object_id}' if object_id else f'player:{ident}'


def _entry(db, season, ident, object_id=0):
    return db.execute('SELECT * FROM rank_entries WHERE season_id=? AND entry_key=?',
                      (season, _key(ident, object_id))).fetchone()


def _portrait(state):
    return dict(char_oid=state['char_id'], char_uuid=state['char_uuid'], char_name=state['name'],
                char_level=state['level'], char_avatar=state.get('avatar', 0),
                char_decorate=state.get('decorate_id', 0), char_maintank=state['tank_id'],
                char_maintank_l=state['tank_level'], char_maintank_e=state.get('tank_ext', 0),
                halo_id=state.get('halo_id', 0))


def _ensure_entry(db, season, ident, state, now):
    db.execute('INSERT OR IGNORE INTO rank_entries '
               '(season_id,entry_key,profile_id,owner_id,is_bot,object_id,rating,updated_ms,data) '
               'VALUES(?,?,?,?,0,0,?,?,?)',
               (season, _key(ident), ident, ident, INITIAL_RATING, int(now * 1000), _json(_portrait(state))))
    return _entry(db, season, ident)


def brief(db, ident, now=None):
    current = maintain(db, now)
    old, previous = _entry(db, current, ident), _entry(db, current - 1, ident)
    rating = int(old['rating']) if old else INITIAL_RATING
    return dict(elo=rating, elo_section=section(rating)['id'],
                pre_elo_section=section(previous['rating'])['id'] if previous else section(rating)['id'],
                season_id=current)


def _bot_id(bot):
    """Combat slots may change for the reserve; rank identity never does."""
    return int(bot.get('rank_bot_id', bot['object_id']))


def _profile_key(ident):
    return f'rank_bot_profiles:{int(ident)}'


def _new_bot(ident, side, tank, level, rng):
    from bot_setup import AI_TANKS, AI_EXTERNALS
    bot = dict(rank_bot_id=ident, acct_id=88888886 + ident,
               char_uuid=f'robot-pk-{ident}', name=f'{AI_TANKS[tank]} {ident - 1}',
               tank_id=tank, tank_level=level, tank_ext=AI_EXTERNALS.get(tank, 0),
               tank_strength=0, side=side)
    bot_cosmetics.roll(bot, rng)
    return bot


def _profile(bot, ident, side):
    return dict(rank_bot_id=ident, acct_id=int(bot['acct_id']), char_uuid=bot['char_uuid'],
                name=bot['name'], tank_id=int(bot['tank_id']), tank_level=int(bot['tank_level']),
                tank_ext=int(bot.get('tank_ext', 0)), tank_strength=int(bot.get('tank_strength', 0)),
                avatar=bot_cosmetics.valid_avatar(bot.get('avatar', 0)),
                decorate_id=int(bot.get('decorate_id', 0)), halo_id=int(bot.get('halo_id', 0)),
                cosmetics_tank_id=int(bot['tank_id']), side=side)


def _validate_profiles(value, owner):
    from bot_setup import AI_TANKS
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != BOT_PROFILE_VERSION or value.get('owner_id') != owner
            or not isinstance(value.get('bots'), list)):
        raise ValueError('Hồ sơ bot rank không hợp lệ; không tự tạo lại hoặc xóa tiến độ.')
    bots = value['bots']
    if (len(bots) != 10 or not all(isinstance(bot, dict) for bot in bots)
            or not all(type(bot.get('rank_bot_id')) is int for bot in bots)
            or {bot['rank_bot_id'] for bot in bots} != set((*BOT_IDS, RESERVE_ID))):
        raise ValueError('Danh sách định danh bot rank không hợp lệ.')
    for bot in bots:
        ident = bot['rank_bot_id']
        side = 'enemies' if ident < 7 else 'allies' if ident < RESERVE_ID else 'reserve'
        if (bot.get('side') != side or type(bot.get('tank_id')) is not int
                or bot['tank_id'] not in AI_TANKS
                or type(bot.get('tank_level')) is not int or not 1 <= bot['tank_level'] <= 6
                or type(bot.get('acct_id')) is not int or not 0 < bot['acct_id'] <= 0xffffffff
                or bot['acct_id'] == owner or not isinstance(bot.get('char_uuid'), str)
                or not bot['char_uuid'] or not isinstance(bot.get('name'), str) or not bot['name']):
            raise ValueError('Thông tin nhân vật bot rank không hợp lệ.')
    if len({b['acct_id'] for b in bots}) != 10 or len({b['char_uuid'] for b in bots}) != 10:
        raise ValueError('Định danh tài khoản bot rank bị trùng.')
    for side in ('enemies', 'allies'):
        team = [b['tank_id'] for b in bots if b['side'] == side]
        if len(team) != len(set(team)):
            raise ValueError('Hồ sơ bot rank có tank trùng trong cùng phe.')
    reserve = next(b for b in bots if b['rank_bot_id'] == RESERVE_ID)
    if reserve['tank_id'] in {b['tank_id'] for b in bots if b is not reserve}:
        raise ValueError('Tank của bot dự bị phải khác các bot chính.')
    return bots


def _persistent_profiles(db, state, seed_bots, rng):
    """Lazy, transactional migration of virtual data only; never user/auth data.

    Retain the last native bot portrait and every existing rating/statistic.
    A first-use profile has no past results to reconstruct. The setting survives
    normal two-season pruning, while seasonal entries still reset normally.
    """
    from bot_setup import AI_TANKS
    owner = state['char_id']
    saved = db.execute('SELECT value FROM settings WHERE name=?', (_profile_key(owner),)).fetchone()
    if saved:
        return _validate_profiles(json.loads(saved[0]), owner)
    legacy = {}
    for row in db.execute('SELECT * FROM rank_entries WHERE owner_id=? AND is_bot=1 '
                          'ORDER BY season_id DESC,object_id', (owner,)):
        if row['object_id'] in BOT_IDS:
            legacy.setdefault(row['object_id'], json.loads(row['data']))
    seeded = {bot['object_id']:bot for bot in seed_bots}
    profiles = {}
    for ident, old in legacy.items():
        side = 'enemies' if ident < 7 else 'allies'
        bot = dict(acct_id=old['char_oid'], char_uuid=old['char_uuid'], name=old['char_name'],
                   tank_id=old['char_maintank'], tank_level=old['char_maintank_l'],
                   tank_ext=old.get('char_maintank_e', 0), avatar=old.get('char_avatar', 0),
                   decorate_id=old.get('char_decorate', 0), halo_id=old.get('halo_id', 0))
        profiles[ident] = _profile(bot, ident, side)
    for ident in BOT_IDS:
        if ident in profiles:
            continue
        side = 'enemies' if ident < 7 else 'allies'
        bot = seeded[ident]
        used = {b['tank_id'] for b in profiles.values() if b['side'] == side}
        if side == 'allies':
            used.add(state['tank_id'])
        if bot['tank_id'] in used:
            tank = rng.choice([tank for tank in AI_TANKS if tank not in used])
            bot = _new_bot(ident, side, tank, bot['tank_level'], rng)
        profiles[ident] = _profile(bot, ident, side)
    used = {b['tank_id'] for b in profiles.values()} | {state['tank_id']}
    tank = rng.choice([tank for tank in AI_TANKS if tank not in used])
    reserve = _new_bot(RESERVE_ID, 'reserve', tank, profiles[7]['tank_level'], rng)
    profiles[RESERVE_ID] = _profile(reserve, RESERVE_ID, 'reserve')
    value = dict(version=BOT_PROFILE_VERSION, owner_id=owner,
                 bots=[profiles[ident] for ident in (*BOT_IDS, RESERVE_ID)])
    _validate_profiles(value, owner)
    db.execute('INSERT INTO settings(name,value) VALUES(?,?)', (_profile_key(owner), _json(value)))
    return value['bots']


def _rank_fields(db, season, owner, ident, initial):
    row, previous = _entry(db, season, owner, ident), _entry(db, season - 1, owner, ident)
    rating = int(row['rating']) if row else initial
    return dict(elo=rating, elo_section=section(rating)['id'],
                pre_elo_section=section(previous['rating'])['id'] if previous else section(rating)['id'])


def stable_room_roster(state):
    """Select fixed identities for this lobby, with no random tank replacement.

    Only the one allied account whose permanent tank matches the human rests;
    the reserve takes that combat slot. Changing back restores the same account.
    Current room Guild/crystal switches still build equipment per native rules.
    """
    import bot_loadouts
    value = state.get('rank_bot_roster')
    if not value:
        return []  # Old saved selections migrate at their next new ranked room.
    profiles = {b['rank_bot_id']:b for b in _validate_profiles(value, state['char_id'])}
    old = {b['object_id']:b for b in state.get('bots', [])}
    bots, changed = [], []
    for slot in BOT_IDS:
        side = profiles[slot]['side']
        profile = profiles[RESERVE_ID] if (side == 'allies' and
                                          profiles[slot]['tank_id'] == state['tank_id']) else profiles[slot]
        bot = dict(profile, object_id=slot, side=side, ready=1, in_room=True)
        bot_loadouts.apply(bot, state['room'])
        bots.append(bot)
        if old.get(slot) != bot:
            changed.append(bot)
    state['bots'] = bots
    state['bot_cosmetics_prepared'] = True
    return changed


def prepare_room(state, db, *, now=None, rng=None):
    """Build a random supported room in memory; only S1721 is sent to the UI."""
    from bot_setup import configure_room, load_setup, MODE_MAPS, BOT_MAPS
    from bot_policy import ELITE, LEGACY
    if state.get('battle') or state.get('tank_selection'):
        return False
    now = time.time() if now is None else now
    rng = rng or random.SystemRandom()
    with storage.connection(write=True) as rank_db:
        context = brief(rank_db, state['char_id'], now)
    setup = load_setup(db)
    # Only this local room setup is changed. The saved unranked choice stays
    # intact; configure_room freezes the selection and new-battle prepare_match
    # copies it once. Restore/respawn never re-evaluates rank or next settings.
    setup['bot_ai'] = ELITE if section(context['elo'])['type'] >= PRO_MIN_SECTION_TYPE else LEGACY
    setup['bot_ai_allies'] = setup['bot_ai_enemies'] = setup['bot_ai']
    setup['mode'] = rng.choice([mode for mode, ident in MODE_MAPS.items()
                                if ident in BOT_MAPS and not mode.startswith('test')])
    # Ranked is always5v5. Preserve the saved tank-level/loadout choices.
    setup['allies'] = (setup['allies'] + [5] * 4)[:4]
    setup['enemies'] = (setup['enemies'] + [5] * 5)[:5]
    state['room'] = dict(room_id=1001, name='Đấu rank', map_id=19, mode=3, room_type=9,
                         state=0, camp=1, ready=1, max_num=10, min_num=2, ob_num=0)
    configure_room(state, db, setup=setup, rng=rng)
    state['room'].update(ranked=True, room_type=9, rank_season_id=context['season_id'])
    state.update(context)
    rating = bot_rating(context['elo'])
    with storage.connection(write=True) as rank_db:
        profiles = _persistent_profiles(rank_db, state, state['bots'], rng)
        profiles = [dict(bot, **_rank_fields(rank_db, context['season_id'], state['char_id'],
                                           bot['rank_bot_id'], rating)) for bot in profiles]
    state['rank_bot_roster'] = dict(version=BOT_PROFILE_VERSION, owner_id=state['char_id'], bots=profiles)
    stable_room_roster(state)
    return True


def start_metadata(state, now=None):
    battle = state.get('battle')
    if battle is not None and 'match_uuid' not in battle:
        battle.update(match_uuid=uuid.uuid4().hex, match_started_at=int(time.time() if now is None else now))


def _register_bots(db, season, state, rating, now):
    ident = state['char_id']
    entries = {}
    # Existing identities, portraits, points and counters are immutable here.
    # Only authoritative settlement changes ratings; selection cannot reset them.
    for bot in state.get('bots', []):
        if (not bot.get('in_room', True) or bot.get('human')
                or int(bot.get('rank_owner_id',ident)) != ident):
            continue
        obj = _bot_id(bot)
        data = dict(char_oid=bot['acct_id'], char_uuid=bot['char_uuid'], char_name=bot['name'],
                    char_level=36, char_avatar=bot_cosmetics.valid_avatar(bot.get('avatar', 0)), char_decorate=bot.get('decorate_id', 0),
                    char_maintank=bot['tank_id'], char_maintank_l=bot['tank_level'],
                    char_maintank_e=bot.get('tank_ext', 0), halo_id=bot.get('halo_id', 0))
        db.execute('INSERT INTO rank_entries (season_id,entry_key,profile_id,owner_id,is_bot,object_id,'
                   'rating,updated_ms,data) VALUES(?,?,?,?,1,?,?,?,?) ON CONFLICT(season_id,entry_key) DO NOTHING',
                   (season, _key(ident, obj), bot['acct_id'], ident, obj, rating, int(now * 1000), _json(data)))
        entries[obj] = _entry(db, season, ident, obj)
    return entries


def _settle(db, state, arena, now, *, player=None):
    battle = state['battle']
    current = maintain(db, now)
    ident = state['char_id']
    room = state.get('room') or {}
    ranked = bool(room.get('ranked'))
    season = int(room.get('rank_season_id', current)) if ranked else current
    expired = season < current - 1
    if expired:
        season = current  # Never recreate deleted seasons after a long paused match.
    pk = arena.pk
    winner = pk['winner']
    camp = (player or arena.player).camp
    outcome = 'draw' if not winner else 'win' if winner == camp else 'loss'
    info = _ensure_entry(db, season, ident, state, now) if ranked and not expired else None
    before = int(info['rating']) if info else 0
    after = max(INITIAL_RATING, min(MAX_RATING, before + {'win':150, 'loss':-15, 'draw':0}[outcome])) if info else 0
    rank_result = {}
    if info:
        db.execute('UPDATE rank_entries SET rating=?,played=played+1,wins=wins+?,losses=losses+?,'
                   'draws=draws+?,updated_ms=?,data=? WHERE season_id=? AND entry_key=?',
                   (after, outcome == 'win', outcome == 'loss', outcome == 'draw', int(now * 1000),
                    _json(_portrait(state)), season, _key(ident)))
        rank_result[str(ident)] = dict(old=before, new=after, section=section(after)['id'])
        entries = _register_bots(db, season, state, bot_rating(before), now)
        for bot in state.get('bots', []):
            actor = arena.actors.get(bot['object_id'])
            if (not bot.get('in_room', True) or bot.get('human') or not actor
                    or actor.info.get('human',actor.id == 1) or actor.id not in pk['stats']
                    or int(bot.get('rank_owner_id',ident)) != ident):
                continue
            bot_outcome = 'draw' if not winner else 'win' if actor.camp == winner else 'loss'
            bot_ident = _bot_id(bot)
            bot_before = int(entries[bot_ident]['rating'])
            bot_after = max(INITIAL_RATING, min(MAX_RATING,
                bot_before + {'win':150, 'loss':-15, 'draw':0}[bot_outcome]))
            db.execute('UPDATE rank_entries SET rating=?,played=played+1,wins=wins+?,losses=losses+?,draws=draws+?,updated_ms=? '
                       'WHERE season_id=? AND entry_key=?',
                       (bot_after, bot_outcome == 'win', bot_outcome == 'loss', bot_outcome == 'draw',
                        int(now * 1000), season, _key(ident, bot_ident)))
            rank_result[str(bot['acct_id'])] = dict(old=bot_before, new=bot_after,
                                                   section=section(bot_after)['id'])
    rows = []
    for obj, stats in pk['stats'].items():
        actor = arena.actors[obj]
        rows.append(dict(account_id=actor.info['acct_id'], name=actor.info.get('name', ''),
                         tank_id=actor.info['tank_id'], camp=actor.camp, bot=not actor.info.get('human',obj == 1),
                         kills=stats[1], deaths=stats[0], assists=stats[3]))
    summary = dict(match_uuid=battle['match_uuid'], season_id=season, ranked=ranked,
                   expired_season=expired, map_id=arena.geometry.id, outcome=outcome,
                   started_at=battle['match_started_at'], ended_at=int(now),
                   duration=pk.get('elapsed', 0), old_rating=before, new_rating=after,
                   delta=after - before, forfeit=bool(battle.get('forfeit')),
                   roster=rows, scores={str(k): v for k, v in pk['scores'].items()},
                   rank_results=rank_result)
    db.execute('INSERT INTO match_history VALUES(?,?,?,?,?,?)',
               (ident, battle['match_uuid'], season, int(now * 1000), ranked, _json(summary)))
    db.execute('DELETE FROM match_history WHERE profile_id=? AND match_uuid NOT IN '
               '(SELECT match_uuid FROM match_history WHERE profile_id=? ORDER BY ended_ms DESC,match_uuid DESC LIMIT ?)',
               (ident, ident, HISTORY_LIMIT))
    battle.update(completed_recorded=True, rank_result=rank_result, result_summary=summary)
    context = brief(db, ident, now)
    state.update(context)
    import match_rewards
    try:
        match_rewards.apply_pvp_rewards(state, arena, db)
    except Exception as exc:
        log.warning('PvP reward error in _settle for %s: %s', ident, exc)



def settle_shared(db, states, arena, *, now=None):
    """Prepare every human's result in the caller's one durable transaction.

    A shared UUID is idempotent independently for each real profile. Virtual
    bot ratings belong only to their persistent owner; foreign humans never
    become a bot profile. Caller commits before publishing any S1708.
    """
    from copy import deepcopy
    now=time.time() if now is None else now
    working={ident:deepcopy(state) for ident,state in states.items()}
    merged={}
    for ident,state in working.items():
        battle=state['battle']
        done=db.execute('SELECT summary FROM match_history WHERE profile_id=? AND match_uuid=?',
                        (ident,battle['match_uuid'])).fetchone()
        if done:
            summary=json.loads(done[0])
            result=summary.get('rank_results',{})
            battle.update(completed_recorded=True,result_summary=summary,rank_result=result)
            state.update(brief(db,ident,now))
        else:
            actor=next(a for a in arena.actors.values()
                       if a.info.get('human',a.id==1) and a.info['acct_id']==ident)
            _settle(db,state,arena,now,player=actor)
        merged.update(battle.get('rank_result',{}))
    # Everyone sees the same complete ranked scoreboard, while own history
    # outcome/delta remain computed from that human's actual camp.
    for ident,state in working.items():
        battle=state['battle'];battle['rank_result']=dict(merged)
        battle['result_summary']['rank_results']=dict(merged)
        db.execute('UPDATE match_history SET summary=? WHERE profile_id=? AND match_uuid=?',
                   (_json(battle['result_summary']),ident,battle['match_uuid']))
    return working,merged

def checkpoint(state, arena, snapshot, db, *, now=None):
    """Result + history + replay marker + world commit in ONE transaction.

    The normal50Hz path is precisely the existing save_world path. Mutate the
    live settlement flags only after commit, so failed commits remain retryable.
    """
    battle = state.get('battle')
    pending = bool(battle and arena and arena.pk and arena.pk['finished']
                   and not battle.get('completed_recorded'))
    if not pending:
        return storage.save_world(state, snapshot, db=db)
    now = time.time() if now is None else now
    working = dict(state, battle={k:v for k,v in battle.items() if k != 'actions'})
    try:
        db.execute('BEGIN IMMEDIATE')
        # Supports a recovery from a durable result whose old world was copied
        # by an external backup; retries of this match still cannot add points.
        done = db.execute('SELECT summary FROM match_history WHERE profile_id=? AND match_uuid=?',
                          (state['char_id'], battle['match_uuid'])).fetchone()
        if done:
            summary = json.loads(done[0])
            # New summaries retain every participant, including a reserve whose
            # account ID is not tied to its combat slot. Legacy summaries remain
            # readable without inventing unavailable historical bot ratings.
            result = summary.get('rank_results')
            if not isinstance(result, dict):
                result = {str(state['char_id']): dict(old=summary['old_rating'],
                          new=summary['new_rating'], section=section(summary['new_rating'])['id'])}
            working['battle'].update(completed_recorded=True, result_summary=summary,
                                    rank_result=result if summary['ranked'] else {})
            working.update(brief(db, state['char_id'], now))
        else:
            _settle(db, working, arena, now)
        db.execute('UPDATE worlds SET state=?,arena=?,updated_ms=? WHERE profile_id=?',
                   (_json(working), snapshot, int(now * 1000), storage.profile_id()))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    battle.update(working['battle'])
    for key in ('elo', 'elo_section', 'pre_elo_section', 'season_id'):
        if key in working:
            state[key] = working[key]


def register_selection(state, db, now=None):
    if not (state.get('room') or {}).get('ranked'):
        return
    now = time.time() if now is None else now
    with storage.connection(write=True) as opened:
        season = maintain(opened, now)
        row = _ensure_entry(opened, season, state['char_id'], state, now)
        rating = int(row['rating'])
        _register_bots(opened, season, state, bot_rating(rating), now)
        identities = {_bot_id(bot) for bot in state['bots']}
        identities.update(bot['rank_bot_id'] for bot in (state.get('rank_bot_roster') or {}).get('bots', []))
        fields = {ident:_rank_fields(opened, season, state['char_id'], ident, bot_rating(rating))
                  for ident in identities}
    state['room']['rank_season_id'] = season
    state.update(elo=rating, elo_section=section(rating)['id'])
    import bot_loadouts
    for bot in state['bots']:
        bot.update(fields[_bot_id(bot)])
        bot_loadouts.apply(bot, state['room'])
    for bot in (state.get('rank_bot_roster') or {}).get('bots', []):
        bot.update(fields[bot['rank_bot_id']])


def rank_list(db, season, *, owner=None):
    # Humans are global. Virtual ranked profiles are private to the viewing
    # account, including its fixed reserve after that profile has participated.
    rows = db.execute('SELECT * FROM rank_entries WHERE season_id=? AND (is_bot=0 OR owner_id=?) '
                      'ORDER BY rating DESC,is_bot,updated_ms,object_id,entry_key', (season, owner or -1)).fetchall()
    commanders = [r['entry_key'] for r in rows if not r['is_bot'] and r['rating'] >= 1800][:10]
    result = []
    for index, row in enumerate(rows, 1):
        data = json.loads(row['data'])
        if not row['is_bot']:
            live = db.execute('SELECT state FROM worlds WHERE profile_id=?', (row['profile_id'],)).fetchone()
            if live:
                data.update(_portrait(json.loads(live[0])))
        else:
            data['char_avatar']=bot_cosmetics.valid_avatar(data.get('char_avatar'))
        sec = section(row['rating'])
        challenger = row['entry_key'] in commanders
        data.update(char_rank=index, char_point=row['rating'], all_point=row['rating'],
                    char_elo=row['rating'], elo_section_id=sec['id'], rank_type=sec['type'],
                    rank_name='Thách Đấu' if challenger else sec['name'], challenger=challenger,
                    is_bot=bool(row['is_bot']), played=row['played'], wins=row['wins'],
                    losses=row['losses'], draws=row['draws'], entry_key=row['entry_key'])
        result.append(data)
    return result


def status(db, ident, now=None):
    now = time.time() if now is None else now
    current = maintain(db, now)
    row = _entry(db, current, ident)
    rating = int(row['rating']) if row else INITIAL_RATING
    season = db.execute('SELECT * FROM rank_seasons WHERE id=?', (current,)).fetchone()
    ranks = rank_list(db, current, owner=ident)
    own = next((r for r in ranks if not r['is_bot'] and r['char_oid'] == ident), None)
    history = [json.loads(r[0]) for r in db.execute('SELECT summary FROM match_history WHERE profile_id=? '
              'ORDER BY ended_ms DESC,match_uuid DESC LIMIT ?', (ident, HISTORY_LIMIT))]
    return dict(season=dict(season), server_time=int(now), rating=rating, section=section(rating),
                rank=own['char_rank'] if own else 0, challenger=bool(own and own['challenger']),
                played=row['played'] if row else 0, wins=row['wins'] if row else 0,
                losses=row['losses'] if row else 0, draws=row['draws'] if row else 0,
                history=history, rules=dict(win=150, loss=-15, draw=0, initial=1000,
                days=14, keep_seasons=2, history_limit=20, bot_initial_tier_offset=-1,
                bot_persistent=True, bot_rating_by_match_result=True))


def update_packets(state):
    return [(85, struct.pack('<BIH', kind, state[key], 0))
            for kind, key in ((6, 'elo'), (8, 'elo_section')) if key in state]


