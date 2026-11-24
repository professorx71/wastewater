import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DB = Path('/var/lib/pbf-wastewater/history.sqlite3')
CREDS = Path('/etc/pbf-wastewater/dmweb.json')
TAGS = ('pH_Reading', 'EQ_Tank_Level_Ft', 'FLOW_RATE', 'Total_Flow_Li', 'Rake_Speed_VFD', 'Coag_Pump_Rate_Percent', 'Coag_Pump_Run', 'Floc_Pump_Rate_Percent', 'Floc_Pump_Rate', 'Caustic_Pump_Rate_Percent', 'Caustic_Pump_Run', 'Acid_Pump_Rate_Percent', 'Acid_Pump_Run', 'SLUDGE_TANK_LEVEL', 'SUMP_PUMP_STATUS', 'Mixer_Pump_Staus', 'SYSTEM_ENABLED', 'EQ_TANK_FULL', 'E_STOP_STATUS', 'MIXER_PUMP_FAULTED', 'RAKE_MOTOR_FAULTED', 'MAX_RGT_PUMP_FAULTED', 'FLOC_COAG_PUMP_FAULTED')

# Ewon tags renamed in the Ewon: old name -> new name. Floc_Pump_Run (F164:12) became Floc_Pump_Rate (F192:2, mL/min)
# on 11/23/2026; its earlier readings are moved to the new name so usage and trends continue.
TAG_RENAMES = {'Floc_Pump_Run': 'Floc_Pump_Rate'}

def connect(path=DB):
    db = sqlite3.connect(path, timeout=30)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS samples (
        tag_id TEXT, name TEXT, stamp TEXT, value REAL, quality TEXT,
        received_at TEXT, PRIMARY KEY(tag_id,stamp));
      CREATE INDEX IF NOT EXISTS samples_name_stamp ON samples(name,stamp);
      CREATE TABLE IF NOT EXISTS alarms (
        tag_id TEXT, payload TEXT, received_at TEXT,
        PRIMARY KEY(tag_id,payload));
    ''')
    with db:   # history kept under a renamed Ewon tag's new name
        for old, new in TAG_RENAMES.items():
            db.execute('UPDATE samples SET name=? WHERE name=?', (new, old))
    return db

def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp has no UTC offset')
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def save_page(db, result):
    if result.get('success') is not True:
        raise ValueError('API did not report success')
    cursor = result.get('transactionId')
    if isinstance(cursor, bool) or not isinstance(cursor, (str, int)) or str(cursor) == '':
        raise ValueError('API returned no transaction ID')
    if not isinstance(result.get('ewons'), list):
        raise ValueError('Invalid device list')
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    count = 0
    with db:
        for device in result['ewons']:
            if str(device.get('id')) != '745123':
                continue
            for tag in device.get('tags', []):
                ident = str(tag['id'])
                name = tag['name']
                for alarm in tag.get('alarms', []) or []:
                    db.execute('INSERT OR IGNORE INTO alarms VALUES (?,?,?)',
                               (ident,json.dumps(alarm,sort_keys=True),now))
                if name not in TAGS:
                    continue
                for point in tag.get('history', []) or []:
                    value = point.get('value')
                    if value is not None and (not isinstance(value, (int,float)) or not math.isfinite(value)):
                        raise ValueError('Invalid numeric history value')
                    db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?)',
                               (ident,name,timestamp(point['date']),value,point.get('quality'),now))
                    count += 1
            if device.get('lastSynchroDate'):
                db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                           ('last_upload',device['lastSynchroDate']))
        db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('cursor',str(cursor)))
        db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('last_success',now))
        db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                   ('backfill_pending',str(result.get('moreDataAvailable') is True)))
    return count

def sync():
    saved = json.loads(CREDS.read_text())
    credentials = {key:saved[key] for key in ('t2mdevid','t2mtoken')}
    db = connect()
    for page in range(10):
        row = db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
        params = {**credentials,'ewonIds':'745123','createTransaction':''}
        if row:
            params['lastTransactionId'] = row[0]
        request = Request('https://data.talk2m.com/syncdata',
            data=urlencode(params).encode(),
            headers={'Content-Type':'application/x-www-form-urlencoded'})
        with urlopen(request,timeout=45) as response:
            result = json.load(response)
        count = save_page(db,result)
        print(f'Page {page+1}: stored {count} historical samples.',flush=True)
        if result.get('moreDataAvailable') is not True:
            print('Synchronization complete.',flush=True)
            break
        if row and str(result['transactionId']) == row[0]:
            raise ValueError('Backfill cursor did not advance')
    db.close()

def status():
    db = sqlite3.connect(f'file:{DB}?mode=ro',uri=True)
    print('VPS UTC:',datetime.now(timezone.utc).isoformat())
    for key,value in db.execute("SELECT key,value FROM state WHERE key!='cursor'"):
        print(key+':',value)
    for name in TAGS:
        total = db.execute('SELECT COUNT(*) FROM samples WHERE name=?',(name,)).fetchone()[0]
        row = db.execute('SELECT stamp,value FROM samples WHERE name=? ORDER BY stamp DESC LIMIT 1',(name,)).fetchone()
        print(name, '| records:',total,'| latest:',row)

if __name__ == '__main__':
    try:
        status() if '--status' in sys.argv else sync()
    except Exception as error:
        # Never print credentials, response bodies, or request objects.
        print('Collector failed:',type(error).__name__,flush=True)
        sys.exit(1)
