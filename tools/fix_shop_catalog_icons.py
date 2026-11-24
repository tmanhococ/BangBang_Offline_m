import json
import sqlite3
import os
import hashlib
import struct
import zlib
import xml.etree.ElementTree as ET
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import economy_actions

CATALOG_PATH = SERVER_DIR / 'normal_shop_catalog.json'

def png_dimensions(path):
    with open(path, 'rb') as f:
        head = f.read(24)
        if head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR':
            return struct.unpack('>II', head[16:24])
    return 100, 100

def get_file_info(rel_path):
    disk_path = ROOT / 'www' / 'rez' / rel_path
    if not disk_path.exists():
        raise FileNotFoundError(f"File not found: {disk_path}")
    with open(disk_path, 'rb') as f:
        content = f.read()
        sha = hashlib.sha256(content).hexdigest()
    w, h = png_dimensions(disk_path)
    return {
        'url': f'/rez/{rel_path}',
        'width': w,
        'height': h,
        'sha256': sha
    }

def main():
    # 1. Load fileList.xml
    with open(ROOT / 'www' / 'rez' / 'fileList.xml', 'rb') as f:
        xml_data = zlib.decompress(f.read())
    root = ET.fromstring(xml_data)
    mapping = {}
    for entry in root.findall('.//entry'):
        k = entry.find('key')
        v = entry.find('value')
        if k is not None and v is not None and k.text and v.text:
            mapping[k.text] = v.text

    # 2. Database connection
    db = sqlite3.connect(ROOT / 'data' / 'bangbang.sqlite3')
    db.row_factory = sqlite3.Row

    with open(CATALOG_PATH, 'r', encoding='utf-8') as f:
        cat = json.load(f)

    # 3. Fix 2003096 -> 2003097 in boxes & rewards
    if '2003096' in cat['boxes']:
        box_data = cat['boxes'].pop('2003096')
        box_data['itemId'] = 2003097
        cat['boxes']['2003097'] = box_data

    if 'item:2003096' in cat['rewards']:
        rew_data = cat['rewards'].pop('item:2003096')
        rew_data['key'] = 'item:2003097'
        rew_data['id'] = 2003097
        cat['rewards']['item:2003097'] = rew_data

    # Update offers
    for offer in cat['offers']:
        if offer.get('itemId') == 2003096:
            offer['itemId'] = 2003097
        if offer.get('boxId') == '2003096':
            offer['boxId'] = '2003097'

    # 4. Resolve icons for all offers
    for offer in cat['offers']:
        kind = offer.get('grantKind')
        item_id = offer.get('itemId')
        name = offer.get('name')

        if kind == 'tank':
            row = db.execute('SELECT szMicroHead FROM client_TankBaseTemplate_table WHERE nTankId=?', (item_id,)).fetchone()
            if row and row['szMicroHead']:
                micro = row['szMicroHead']
                logical = f'zh_CN/avatar/micro/{micro}'
                resolved = mapping.get(logical)
                if resolved:
                    offer['icon'] = get_file_info(resolved)
                    print(f"Tank {name} ({item_id}) -> {resolved}")
        elif kind == 'item':
            row = db.execute('SELECT szIconPath1 FROM client_ItemTemplate_table WHERE nItemId=?', (item_id,)).fetchone()
            if row and row['szIconPath1']:
                icon = row['szIconPath1']
                logical = f'zh_CN/icon/item/{icon}'
                resolved = mapping.get(logical)
                if resolved:
                    offer['icon'] = get_file_info(resolved)
                    print(f"Item {name} ({item_id}) -> {resolved}")

        # Keep original featured eggs in tab 1 (Đề cử), others in tab 4 (Vật phẩm)
        if 'Trứng' in name:
            if item_id in (2009302, 2009300, 2010007):
                offer['tab'] = 1
            else:
                offer['tab'] = 4

    # Also make sure featured items in tab 1 have good representation
    # 5. Recompute revision digest
    raw_for_digest = {k: v for k, v in cat.items() if k != 'revision'}
    cat['revision'] = economy_actions.digest(raw_for_digest)

    with open(CATALOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cat, f, ensure_ascii=False, indent=2)

    print(f"\nSuccessfully updated catalog with revision {cat['revision']}")

if __name__ == '__main__':
    main()
