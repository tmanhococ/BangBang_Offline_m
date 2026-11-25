"""Shared PK score/time rules and native settlement packets. No rewards."""
import struct
from pve_catalog import MAPS as PVE_MAPS
from fractions import Fraction
from combat_data import table_row


BASE_MAPS=(12,14,50,169)
# User-approved 8-second gap; assists retain their independent 5-second window.
MULTI_KILL_WINDOW_MS = 8000


def record_structure_hp(arena,target,before):
    """Native STAT_HARM_BASE: actual enemy turret/base HP loss, not overkill."""
    pk=arena.pk
    if (not pk or pk['finished'] or arena.geometry.id not in BASE_MAPS
            or target.camp not in (1,2)
            or not (target.info.get('base') or target.info.get('map_turret'))):return
    loss=max(0,before-target.hp)
    credit=getattr(target,'damage_credit',None)
    source=arena.actors.get(credit[0]) if credit else None
    if not loss or not source or source.generation!=credit[1] or source.camp==target.camp:return
    # Summon damage belongs to its player for this award only. Map turrets and
    # wave Robots have no player owner; do not infer one from their tank ID.
    owner=source if source.id in pk['stats'] else arena.actors.get(source.info.get('owner'))
    if not owner or owner.id not in pk['stats'] or owner.camp!=source.camp:return
    totals=pk.setdefault('structure_damage',{})
    totals[owner.id]=totals.get(owner.id,0)+loss
    # Keep fractional damage across hits/checkpoints; round only the wire stat.
    pk['stats'][owner.id][7]=round(totals[owner.id])


def initialize(arena,now):
    duration=150 if arena.geometry.id in (30,40) else 900 if arena.geometry.id in (12,14,50,70,169) else 600 if arena.geometry.id in (7,9,10,37,170) else 210 if arena.geometry.id in (18,34) else 240
    arena.pk=dict(begin=now,end=now+duration*1000,finished=False,winner=0,
                  scores={1:0,2:0},stats={a.id:[0]*21 for a in arena.actors.values()
                                        if not a.info.get('npc_template')})
    arena.bot_brains={}


def record_hp(arena,target,before,now):
    """Credit actual HP loss, including a lethal hit, before death is scored."""
    if arena.geometry.id == 40002:
        from world_boss_mode import record_hp as boss_hp
        return boss_hp(arena,target,before)
    if arena.geometry.id in PVE_MAPS:
        from pve_rules import record_hp as pve_hp
        return pve_hp(arena,target,before)
    pk=arena.pk
    if not pk or pk['finished'] or target.id not in pk['stats']:return
    loss=max(0,before-target.hp)
    if not loss:return
    credit=getattr(target,'damage_credit',None)
    source=arena.actors.get(credit[0]) if credit else None
    pk['stats'][target.id][13]+=round(loss)
    if not source or source.camp==target.camp or source.generation!=credit[1]:return
    if source.id not in pk['stats']:return
    pk['stats'][source.id][6]+=round(loss)
    records=pk.setdefault('contributors',{}).setdefault(target.id,{})
    records[source.id]=dict(generation=source.generation,victim_generation=target.generation,hit_at=now)


def score_packets(arena):
    pk=arena.pk
    rows=[]
    for ident,stats in pk['stats'].items():
        a=arena.actors[ident]
        rows.append(struct.pack('<IH21iB',a.info['acct_id'],21,*stats,a.info['tank_level']))
    packets=[(1731 if arena.geometry.id in (7,9,10,37,170) else 1710,struct.pack('<II',pk['scores'][1],pk['scores'][2])),
            (1561,struct.pack('<H',len(rows))+b''.join(rows))]
    if arena.geometry.id in (30,40):
        from death_mode import alive_packet
        return [(1733,struct.pack('<II',pk['scores'][1],pk['scores'][2])),alive_packet(arena)]+packets[1:]
    return packets[1:] if arena.geometry.id in (12,14,50,70,169) else packets


def death(arena,victim,now):
    if arena.geometry.id == 40002:
        from world_boss_mode import death as boss_death
        return boss_death(arena,victim,now)
    if arena.geometry.id in PVE_MAPS:
        from pve_rules import death as pve_death
        return pve_death(arena,victim,now)
    pk=arena.pk
    if not pk or pk['finished'] or (victim.info.get('npc_template') and not (victim.info.get('robo') or victim.info.get('mascot') or victim.info.get('totem'))):
        return []
    credit=getattr(victim,'damage_credit',None)
    killer=arena.actors.get(credit[0]) if credit else None
    if killer and killer.generation!=credit[1]:killer=None
    camp=3-victim.camp
    if camp not in (1,2):return []
    if arena.geometry.id not in (7,9,10,12,14,30,40,37,50,70,169,170):pk['scores'][camp]+=1
    if victim.id in pk['stats']:pk['stats'][victim.id][0]+=1
    if killer and killer.camp==camp and killer.id in pk['stats']:
        stats=pk['stats'][killer.id]
        stats[1]+=not victim.info.get('npc_template')
        stats[2]+=1
    packets=[]
    if arena.geometry.id==1 and victim.info.get('chieftain'):
        from maya_map import chieftain_death
        packets+=chieftain_death(arena,victim,now)
    streaks=pk.setdefault('streaks',{})
    dead_streak=streaks.pop(victim.id,0)
    assistants=[];multi=streak=first=0
    if not victim.info.get('npc_template'):
        records=pk.setdefault('contributors',{}).pop(victim.id,{})
        # Friendly/self map blasts retain the object as attribution source.
        # They award no assists either, even after an earlier enemy hit.
        uncredited_blast=bool(killer and killer.info.get('map_object') and getattr(killer,'explosion',None))
        for ident,hit in ({} if uncredited_blast else records).items():
            actor=arena.actors.get(ident)
            if (actor and actor is not killer and actor.camp==camp and actor.generation==hit['generation']
                    and hit['victim_generation']==victim.generation and now-hit['hit_at']<=5000):
                assistants.append(ident)
                pk['stats'][ident][3]+=1
        if killer and killer.id in pk['stats']:
            streak=streaks[killer.id]=streaks.get(killer.id,0)+1
            chains=pk.setdefault('multi',{})
            old=chains.get(killer.id,{})
            multi=old.get('count',0)+1 if now-old.get('kill_at',0)<=MULTI_KILL_WINDOW_MS else 1
            chains[killer.id]=dict(count=multi,kill_at=now)
            first=int(not pk.get('first_blood',False));pk['first_blood']=True
        pk.setdefault('multi',{}).pop(victim.id,None)
    if killer and killer.camp==camp:
        flight=getattr(arena,'current_flight',None) or {}
        # Direct/DOT executors can lack a projectile template; a valid base
        # attack template still lets the native client identify the killer.
        skill=flight.get('template') or (1000114 if killer.info.get('electric_tower') else 1000062 if killer.info.get('map_spider') else killer.shot_skill if killer.info.get('robo') else
               int(table_row('TankBaseTemplate_table.xml',nTankId=killer.info['tank_id'])['nBullet']))
        packets.append((1566,struct.pack('<IIIhhBBBBH',killer.id,victim.id,skill,
                         *victim.position_at(now),min(255,multi),min(255,streak),min(255,dead_streak),first,len(assistants))
                         +b''.join(struct.pack('<I',i) for i in sorted(assistants))))
    packets+=score_packets(arena)
    if arena.geometry.id not in (7,9,10,12,14,30,40,37,50,70,169,170) and max(pk['scores'].values())>=25:packets+=finish(arena,now)
    return packets


def finish(arena,now):
    if arena.geometry.id == 40002:
        from world_boss_mode import finish as boss_finish
        return boss_finish(arena,now)
    if arena.geometry.id in PVE_MAPS:
        from pve_rules import finish as pve_finish
        return pve_finish(arena,now)
    pk=arena.pk
    if pk['finished']:return []
    pk['finished']=True
    pk['elapsed']=max(0,min((pk['end']-pk['begin'])//1000,(now-pk['begin'])//1000))
    if arena.geometry.id in (30,40):pk['elapsed']=max(0,(now-pk['match_started_at'])//1000)
    a,b=pk['scores'][1],pk['scores'][2]
    if arena.geometry.id==70:
        from escort_mode import progress
        carts={v.camp:v for v in arena.actors.values() if v.info.get('bomb_cart')}
        a,b=(progress(carts[c],now) for c in (1,2))
    elif arena.geometry.id in (12,14,50,169):
        bases={a.camp:a for a in arena.actors.values() if a.info.get('base')}
        a,b=(bases[c].hp/bases[c].max_hp for c in (1,2))
    pk['winner']=1 if a>b else 2 if b>a else 0
    # Freeze all simulation; keep the final snapshot for reconnect/result retry.
    for actor in arena.actors.values():
        actor.position=actor.position_at(now)
        actor.motion=None
    arena.pending.clear()
    return [settlement(arena)]


def settlement(arena):
    if arena.geometry.id in PVE_MAPS:
        from pve_rules import settlement as pve_settlement
        return pve_settlement(arena)
    pk=arena.pk; winner=pk['winner'];rows=[]
    for ident,stats in pk['stats'].items():
        a=arena.actors[ident]
        rank=(arena.state.get('battle') or {}).get('rank_result',{}).get(str(a.info['acct_id']),{})
        is_human = a.info.get('human', ident == 1)
        exp_val = stats[10]
        silver_val = stats[11]
        exp_bytes = struct.pack('<Hii', 1, 0, exp_val) if is_human and exp_val > 0 else b'\x00\x00'
        coins_bytes = struct.pack('<Hii', 1, 0, silver_val) if is_human and silver_val > 0 else b'\x00\x00'
        tank_exp_bytes = b'\x00\x00'
        iron_amt = 25 if winner == a.camp else 15
        topaz_amt = 25 if winner == a.camp else 15
        items_bytes = struct.pack('<HIiBiIiBi', 2, 3000001, iron_amt, 1, 0, 3000051, topaz_amt, 1, 0) if is_human and exp_val > 0 else b'\x00\x00'
        pve_bytes = bytes(5)

        base_part = struct.pack('<IIH21i4hi7B', a.info['acct_id'], a.info['tank_id'], 21, *stats,
            0, rank.get('old',0), rank.get('new',0), 0, 0,
            0, int(winner==a.camp), int(not is_human), rank.get('section',0), a.info['tank_level'],
            sum(v.camp==a.camp and not v.info.get('npc_template') for v in arena.actors.values()), a.camp)
        rows.append(base_part + exp_bytes + coins_bytes + tank_exp_bytes + items_bytes + pve_bytes)
    uid=((arena.state.get('battle') or {}).get('match_uuid') or f'local-pk-{arena.scene_id}').encode()
    header=struct.pack('<IIIBBBhhH',arena.scene_id,arena.geometry.id,pk.get('elapsed',(pk['end']-pk['begin'])//1000),9 if (arena.state.get('room') or {}).get('ranked') else 2,winner,
                       4 if not winner else 1,pk['scores'][1],pk['scores'][2],len(rows))
    def best(index):
        candidates=[i for i,s in pk['stats'].items() if s[index]>0]
        return max(candidates,key=lambda i:(pk['stats'][i][index],pk['stats'][i][6],-i)) if candidates else None
    winners=[i for i in pk['stats'] if arena.actors[i].camp==winner]
    mvp=max(winners,key=lambda i:(Fraction(pk['stats'][i][1]+pk['stats'][i][3],max(1,pk['stats'][i][0])),
                                 pk['stats'][i][6],-i)) if winners else None
    totals=pk.get('structure_damage',{}) if arena.geometry.id in BASE_MAPS else {}
    destroyer=max((i for i in pk['stats'] if totals.get(i,0)>0),
                  key=lambda i:(totals[i],pk['stats'][i][6],-i),default=None)
    account=lambda ident:arena.actors[ident].info['acct_id'] if ident is not None else 0
    tail=struct.pack('<HffB7IH',0,1.,1.,0,0,account(best(1)),account(best(3)),account(mvp),account(destroyer),account(mvp),0,len(uid))+uid
    return 1708,header+b''.join(rows)+tail


def static_packets(arena):
    # Type3 static objects use the same ObjInfo wire layout as existing summons.
    # HP-bearing objects use their live state; immortal decorations stay static.
    packets=[]
    for npc in arena.geometry.data['npcs']:
        if npc.get('script_only'):continue
        ident=(3<<26)|npc['instance']
        actor=arena.actors.get(ident)
        if actor:
            if actor.alive:packets.append(actor.snapshot(arena.clock()))
            continue
        body=struct.pack('<IIhhiBiIiH',ident,npc['template'],npc['x'],npc['y'],npc['hp'],
                         npc['camp'],npc['hp'],0,0,0)
        packets.append((1556,struct.pack('<I',arena.scene_id)+body))
    return packets
