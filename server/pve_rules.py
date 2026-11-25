"""Approved solo map201 controller. No PvP scoring, battle EXP or pet actor."""
import math
import struct

from combat_math import D
from combat_protocol import scene
from pve_catalog import CHAPTER2, CHAPTER3, CHAPTER4, CHAPTER5, CHAPTER6, CHAPTER7, CHAPTER8, CHAPTER9


def chapter(arena):
    if arena.geometry.id in CHAPTER8 + CHAPTER9:
        import pve_chapter8_9
        return pve_chapter8_9
    if arena.geometry.id in CHAPTER7:
        import pve_chapter7
        return pve_chapter7
    if arena.geometry.id in CHAPTER6:
        import pve_chapter6
        return pve_chapter6
    if arena.geometry.id in CHAPTER5:
        import pve_chapter5
        return pve_chapter5
    if arena.geometry.id in CHAPTER4:
        import pve_chapter4
        return pve_chapter4
    if arena.geometry.id in CHAPTER3:
        import pve_chapter3
        return pve_chapter3
    if arena.geometry.id in CHAPTER2:
        import pve_chapter2
        return pve_chapter2
    import pve_chapter1
    return pve_chapter1


def record_hp(arena, target, before):
    """Only actual enemy PvE NPC HP loss; summons credit their own player."""
    if arena.pk['finished']: return
    loss = max(D(0), before-target.hp)
    if target is arena.player:
        arena.pk['stats'][1][13] += round(loss)
    if (not loss or not target.info.get('pve_npc') or target.camp==arena.player.camp
            or target.info.get('pve_unscored')): return
    credit = getattr(target, 'damage_credit', None)
    source = arena.actors.get(credit[0]) if credit else None
    valid_source = bool(source and source.generation == credit[1] and source.camp != target.camp)
    owner = (source if source is arena.player else arena.actors.get(source.info.get('owner'))) if valid_source else None
    credited = owner is arena.player and owner.camp == source.camp
    from pve_scoring import record_hp as record_score_hp
    record_score_hp(arena, target, before, credited)
    if not credited: return
    pve = arena.pk['pve']
    pve['damage'] = pve.get('damage', D(0)) + loss
    arena.pk['stats'][1][6] = round(pve['damage'])


def evaluation(arena):
    """M29 frozen grading, or byte-compatible legacy rows for an old run."""
    from pve_scoring import evaluation as new_evaluation
    revised = new_evaluation(arena)
    if revised is not None: return revised
    if arena.pk['winner'] != 1: return (0, 0, 0, 0, 0)
    elapsed = arena.pk['elapsed']
    time_grade = next((grade for limit,grade in ((180,7),(300,6),(480,5),(720,4)) if elapsed<=limit),3)
    damage = arena.pk['pve'].get('damage',D(0))
    harm_grade = next((grade for percent,grade in ((100,7),(85,6),(65,5),(40,4))
                       if damage*100 >= arena.pk['pve'].get('damage_total',1950)*percent),3)
    dead_grade = (7,5,3)[min(2,arena.pk['stats'][1][0])]
    return ((time_grade+harm_grade+dead_grade)//3,harm_grade,dead_grade,time_grade,0)


def install(arena, now):
    from map_objects import install as objects
    ready = now+3000
    arena.pk = dict(begin=ready, end=ready+900000, intro_until=ready,
                    intro_buffs=[(1, arena.new_id(6), arena.new_id(6), arena.new_id(6))],
                    finished=False, winner=0, scores={1: 0, 2: 0}, stats={1: [0]*21},
                    pve=dict(stage='ambush', triggered=False, lives=3, spawns=[]))
    objects(arena)
    if arena.geometry.id!=201:
        chapter(arena).initialize(arena)
    else:
        from pve_balance import frozen_profile, hp_for
        if frozen_profile(arena) is not None:
            # The ambush, two two-NPC waves and the final boss. The grading
            # denominator must use the very same frozen health as spawns.
            arena.pk['pve']['damage_total'] = sum(
                (hp_for(arena, template) for template in (1109, 1102, 1115, 1102, 1115, 1120)), D(0))
    arena.queue(ready, arena.player.on_spawn)
    arena.queue(ready, arena._map_tick)


def spawn(arena, point, template, now):
    from pve_npc import PveNpc
    definition = next(r for r in arena.geometry.data['pve_spawns'] if r['point'] == point)
    ident = arena.new_id(5)
    npc = PveNpc(arena, ident, dict(point=point, npc_template=template), 0, tuple(definition['position']))
    arena.actors[ident] = npc
    return [npc.snapshot(now)]


def ambush(arena, now):
    pve = arena.pk['pve']
    if pve['triggered'] or arena.pk['finished']: return []
    pve['triggered'] = True
    return spawn(arena, 27, 1109, now)


def death(arena, victim, now):
    pk, pve = arena.pk, arena.pk['pve']
    if pk['finished']: return []
    if victim is arena.player:
        pk['stats'][1][0] += 1
        pve['lives'] -= 1
        packets = [(1560, struct.pack('<IBi', victim.info['acct_id'], 5, pve['lives']))]
        if pve['lives'] == 0: packets += finish(arena, now, False)
        return packets  # Common TankActions supplies exactly one 5s respawn.
    if not victim.info.get('pve_npc'): return []
    if arena.geometry.id!=201:
        return chapter(arena).death(arena,victim,now)
    pk['stats'][1][2] += 1
    if victim.info['npc_template'] == 1120:
        return finish(arena, now, True)
    if victim.info['point'] != 27 or pve['stage'] != 'ambush': return []
    pve['stage'] = 'boss'
    packets = []
    for instance in (22, 23, 24):
        obj = arena.actors[(3 << 26) | instance]
        obj.hp = D(0); obj.info['hp'] = 0
        packets += [obj._hp_packet(obj.id, 0), scene(removed=(struct.pack('<I', obj.id),))]
    from map_objects import rebuild_geometry
    rebuild_geometry(arena)
    for point in (10, 12):
        for index, template in enumerate((1102, 1115)):
            pve['spawns'].append(dict(point=point, template=template, spawn_due_at=now+(index+1)*1000))
    pve['spawns'].append(dict(point=6, template=1120, spawn_due_at=now+5000))
    return packets


def _patrol(arena, actor, now):
    pos = actor.position_at(now)
    if not actor.route_queue:
        node = next((p for p in arena.geometry.data['pve_logic'] if p['id'] == actor.route_point), None)
        if not node or not node['next']: return pos
        options = [i for i in node['next'] if i != actor.route_previous] or node['next']
        end = arena.rng.choice(options)
        edge = next(p for p in arena.geometry.data['pve_paths'] if set(p['ends']) == {actor.route_point, end})
        points = edge['points'] if edge['ends'][0] == actor.route_point else list(reversed(edge['points']))
        # Authored top entry paths start inside the off-screen wall. Retain
        # their traversable suffix and the rest of the original route graph.
        actor.route_queue = [tuple(p) for p in points if arena.geometry.can_walk_to(tuple(p))]
        actor.route_previous, actor.route_point = actor.route_point, end
    while actor.route_queue and math.dist(pos, actor.route_queue[0]) < 24:
        actor.route_queue.pop(0)
    return actor.route_queue[0] if actor.route_queue else pos


def tick(arena, now):
    if arena.geometry.id!=201:
        return chapter(arena).tick(arena,now)
    pve = arena.pk['pve']; packets = []
    player = arena.player
    if player.alive and not player.in_stasis() and not pve['triggered']:
        x, y = player.position_at(now)
        left, top, width, height = arena.geometry.data['pve_ambush_zone']
        if left <= x <= left+width and top <= y <= top+height:
            packets += ambush(arena, now)
    remaining = []
    for record in pve['spawns']:
        if now >= record['spawn_due_at']:
            packets += spawn(arena, record['point'], record['template'], now)
        else: remaining.append(record)
    pve['spawns'] = remaining
    from bot_ai import observable, stop
    from pve_movement import patrol_goal, move
    for actor in list(arena.actors.values()):
        if not actor.info.get('pve_npc') or not actor.alive: continue
        brain = arena.bot_brains.setdefault(actor.id, dict(path=[], notice_at=0))
        from pve_tactics import tick as tactical_tick
        tactical = tactical_tick(actor, brain, now)
        if tactical is not None:
            packets += tactical
            if arena.pk['finished']: break
            continue
        if now >= brain['notice_at']:
            brain['notice_at'] = now+200
            brain['pve_patrolling'] = False
            targets = [a for a in arena.actors.values() if observable(arena, actor, a, now, 600)]
            target = min(targets, key=lambda a: (math.dist(actor.position_at(now), a.position_at(now)), a.id), default=None)
            pos = actor.position_at(now)
            if target:
                dest = target.position_at(now)
                in_range = math.dist(pos, dest) <= int(actor.stats['nFireRange'])
                brain['goal'] = pos if in_range else dest
                if in_range:
                    packets += stop(actor, now)
                    packets += actor.cast(0, pos, dest, target.id, now)
            else: brain['goal'] = patrol_goal(arena, actor, now)
        if arena.pk['finished']: break
        packets += move(arena, actor, brain, now)
    return packets


def finish(arena, now, won=False):
    pk = arena.pk
    if pk['finished']: return []
    pk.update(finished=True, winner=1 if won else 0, elapsed=max(0, (now-pk['begin'])//1000))
    from pve_scoring import freeze_result
    freeze_result(arena)
    for actor in arena.actors.values():
        actor.position, actor.motion = actor.position_at(now), None
    arena.pending.clear()
    arena.state['room']['state'] = 0  # RST_WAIT for the native return-to-room flow.
    from pve_progress import reward_packet
    # S1708 clears isServerInBattle, but not Flash's isInBattle. EXIT_END
    # clears the latter without navigating away from the PvE result panel.
    exit_end = (1707, struct.pack('<IIIII', 0, arena.scene_id,
                                  arena.player.info['acct_id'], 1, 0))
    return [exit_end, reward_packet(won, arena.state), settlement(arena)]


def settlement(arena):
    pk = arena.pk; actor = arena.player; won = pk['winner'] == 1
    stats = pk['stats'][1]
    exp_val = stats[10] if stats[10] > 0 else (350 if won else 0)
    silver_val = stats[11] if stats[11] > 0 else (100000 if won else 0)
    exp_bytes = struct.pack('<Hii', 1, 0, exp_val) if exp_val > 0 else b'\x00\x00'
    coins_bytes = struct.pack('<Hii', 1, 0, silver_val) if silver_val > 0 else b'\x00\x00'
    tank_exp_bytes = b'\x00\x00'
    items_bytes = struct.pack('<HIiBiIiBi', 2, 3000001, 20, 1, 0, 3000051, 20, 1, 0) if won else b'\x00\x00'
    pve_bytes = struct.pack('<5B', *evaluation(arena))
    base_part = struct.pack('<IIH21i4hi7B', actor.info['acct_id'], actor.info['tank_id'], 21, *stats,
                      0, 0, 0, 0, 0, 0, int(won), 0, 0, actor.info['tank_level'], 1, 1)
    row = base_part + exp_bytes + coins_bytes + tank_exp_bytes + items_bytes + pve_bytes
    uid = arena.state['battle']['pve_run'].encode()
    header = struct.pack('<IIIBBBhhH', arena.scene_id, arena.geometry.id, pk['elapsed'], 4, pk['winner'], 1,
                         pk['pve']['lives'], 0, 1)
    tail = struct.pack('<HffB7IH', 0, 1., 1., 0, *([0]*7), len(uid)) + uid
    return 1708, header+row+tail
