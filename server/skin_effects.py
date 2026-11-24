"""Presentation-only ID translation for verified skins; combat keeps base IDs.

Maps are loaded once from SQLite plus guarded cosmetic overrides at startup.
The protocol boundary is deliberate:
damage, buff ownership, pending events and checkpoints never use cosmetic IDs.
"""
import struct
import skin_catalog_overrides
from combat_protocol import immediate_positions, team_space_cooldowns

SETTING = 'skin_effect_maps'
_maps = {}


def preload(db):
    global _maps
    _support, effects = skin_catalog_overrides.load(db)
    _maps = {int(ext): {**spec, **{kind: {int(k): int(v) for k, v in spec.get(kind, {}).items()}
              for kind in ('skill', 'buff', 'scene', 'npc')}}
             for ext, spec in effects.items()}


def present_packets(packets, state, arena=None, viewer=None):
    """Final native wire presentation, including authoritative position snaps."""
    if arena is not None:
        packets=team_space_cooldowns(packets,arena,viewer)
        packets=immediate_positions(packets,arena.scene_id)
    return translate_packets(packets,state,arena,viewer)


def translate_packets(packets, state, arena=None, viewer=None):
    # CHAR_BRIEF already carries each fighter's exterior. Equipment cannot
    # change during a battle, including a checkpoint restored after restart.
    fighters = {1: state, 2: state.get('bot', {}), 3: state.get('ally_bot', {})}
    if state.get('room') and state['room']['map_id'] in (1,4,7,9,10,12,14,17,18,19,20,21,30,40,34,35,36,37,50,70,169,170):
        fighters={1:state,**{b['object_id']:b for b in state.get('bots',[])}}
    if arena is not None and arena.state.get('multiplayer_players'):
        fighters={actor.id:actor.info for actor in arena.actors.values()}
    selected = {owner: _maps[info.get('tank_ext')]
                for owner, info in fighters.items()
                if info and info.get('tank_ext') in _maps
                and _maps[info['tank_ext']]['tank'] == info.get('tank_id')}
    if not selected:
        return packets

    def owned_npc(ident, template, owner):
        # Only explicitly reviewed NPCs inherit the owner's cosmetic kit.
        # Keep dead actors too: their already-fired projectiles can survive.
        spec = selected.get(owner)
        if spec and template in spec.get('npc', {}):
            selected[ident] = spec

    if arena is not None:
        for actor in arena.actors.values():
            if actor and actor.info:
                owned_npc(actor.id, actor.info.get('npc_template'), actor.info.get('owner'))
    # A spawn packet may precede its own buff/fire in this batch. This also
    # covers passive objects (device/wall) which aren't combat actors.
    for mid, body in packets:
        if mid == 1556:
            ident, template = struct.unpack_from('<II', body, 4)
            owned_npc(ident, template, struct.unpack_from('<I', body, 25)[0])

    def translate(mid, original):
        body = bytearray(original)

        def u32(pos):
            return struct.unpack_from('<I', body, pos)[0]

        def u16(pos):
            return struct.unpack_from('<H', body, pos)[0]

        def replace(pos, owner, kind):
            mapping = selected.get(owner, {}).get(kind, {})
            value = u32(pos)
            if value in mapping:
                struct.pack_into('<I', body, pos, mapping[value])

        def tank(pos):
            # TankInfo: name UTF after 22 fixed bytes, then BuffInfo[13].
            pos += 24 + u16(pos + 22)
            count = u16(pos)
            pos += 2
            for _ in range(count):
                replace(pos, u32(pos + 9), 'buff')
                pos += 13
            return pos + 20

        if mid == 1564:
            # SyncSceneData vectors: Move, Fire, Skill, HP, NPC death, MP,
            # exploded, removed, expired and Shield. No variable-size entries.
            pos = 0
            for index, size in enumerate((12, 22, 22, 8, 8, 8, 4, 4, 4, 8)):
                count = u16(pos)
                pos += 2
                for _ in range(count):
                    if index == 1:
                        replace(pos + 18, u32(pos + 4), 'skill')
                    elif index == 2:
                        replace(pos + 8, u32(pos), 'skill')
                    pos += size
            if pos != len(body):
                raise ValueError('Unexpected scene layout for skin presentation')
        elif mid == 1558:
            replace(4, u32(13), 'buff')  # caster, not buff recipient
        elif mid == 1556:
            owner, template = u32(25), u32(8)
            kind = 'npc' if template in selected.get(owner, {}).get('npc', {}) else 'scene'
            replace(8, owner, kind)  # scene ID + ObjInfo.owner
            for pos in range(35, 35 + 13*u16(33), 13):
                caster = u32(pos + 9)
                # Existing Minion device snapshots put3000 in casterId;
                # this is its self visual335321, owned by the summoner.
                # Translate that template without changing saved/protocol fields.
                if template == 20007 and u32(pos) == 335321 and caster == 3000:
                    caster = owner
                replace(pos, caster, 'buff')
        elif mid == 1572:
            replace(0, u32(22), 'skill')
        elif mid == 1579:
            replace(0, viewer.id if viewer is not None else 1, 'skill')  # native local-slot notification
        elif mid == 1583:
            replace(4, u32(0), 'skill')
        elif mid == 1550:
            owner = u32(0)
            pos = tank(0) + 4  # fire interval / speed
            count = u16(pos)
            pos += 2
            for _ in range(count):
                replace(pos, owner, 'skill')
                pos += 22
        elif mid == 1551:
            pos = 2
            for _ in range(u16(0)):
                pos = tank(pos)
        elif mid == 1552:
            tank(0)
        return mid, bytes(body)

    return [translate(mid, body) for mid, body in packets]
