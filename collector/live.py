"""Live readings from the Ewon through the Talk2M M2Web API.

Every few seconds this reads the instant value of each dashboard tag straight from the Ewon
(Ewon export $dtIV: instant values) and keeps the latest value per tag in a small SQLite file.
The dashboard shows these on the cards and the process diagram while they are fresh.

History, trends, reports, usage and costs still come ONLY from DataMailbox (collector.py):
nothing here is written to history.sqlite3.

Credentials: /etc/pbf-wastewater/m2web.json (root only, 0600), handed to the service by systemd
(LoadCredential). Fields: account, username, password, developer_id, ewon_name,
device_username, device_password, and optionally interval (seconds, 2-300, default 5).
Never printed.

  python3 live.py          run the loop (the systemd service)
  sudo python3 live.py --test   one read: prints each tag, its value and quality (no secrets)
"""
import csv
import io
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collector import TAGS  # the same tags the history collector keeps

DB = Path(os.environ.get('PBF_LIVE_DB', '/var/lib/pbf-wastewater-live/live.sqlite3'))
CREDS = Path(os.environ.get('CREDENTIALS_DIRECTORY', '/etc/pbf-wastewater')) / 'm2web.json'
FIELDS = ('account', 'username', 'password', 'developer_id', 'ewon_name', 'device_username', 'device_password')
BASE = 'https://m2web.talk2m.com/t2mapi/get/'


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def load_config(path=CREDS):
    cfg = json.loads(Path(path).read_text())
    missing = [k for k in FIELDS if not cfg.get(k)]
    if missing:
        raise ValueError('m2web.json is missing: ' + ', '.join(missing))
    interval = cfg.get('interval', 5)
    if type(interval) not in (int, float) or not 2 <= interval <= 300:
        raise ValueError('m2web.json interval must be 2-300 seconds')
    cfg['interval'] = float(interval)
    return cfg


def good_quality(q):
    """Ewon quality is OPC-style: the top two bits of the low byte set (0xC0) mean good.
    A missing or non-numeric quality is taken as good."""
    q = (q or '').strip()
    if not q.lstrip('-').isdigit():
        return True
    return (int(q) & 0xC0) == 0xC0


def parse(text, tags=TAGS):
    """{tag: (value or None, quality, good)} from the Ewon instant-values export ($dtIV$ftT):
    "TagId";"TagName";"Value";"AlStatus";"AlType";"Quality" (semicolon separated)."""
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=';') if r]
    if not rows:
        raise ValueError('Empty reply from the Ewon')
    head = [h.strip().lower() for h in rows[0]]
    if 'tagname' not in head or 'value' not in head:
        raise ValueError('Unexpected reply from M2Web (not the Ewon tag export)')
    out = {}
    for r in rows[1:]:
        d = dict(zip(head, (c.strip() for c in r)))
        name = d.get('tagname')
        if name not in tags:
            continue
        try:
            value = float(d.get('value', ''))
        except ValueError:
            value = None
        quality = d.get('quality', '')
        out[name] = (value, quality, value is not None and good_quality(quality))
    if not out:
        raise ValueError('None of the dashboard tags are in the Ewon reply (check the tag names)')
    return out


def read_once(cfg):
    url = BASE + quote(cfg['ewon_name'], safe='') + '/rcgi.bin/ParamForm?AST_Param=$dtIV$ftT'
    body = urlencode({'t2maccount': cfg['account'], 't2musername': cfg['username'], 't2mpassword': cfg['password'],
                      't2mdeveloperid': cfg['developer_id'], 't2mdeviceusername': cfg['device_username'],
                      't2mdevicepassword': cfg['device_password']}).encode()
    request = Request(url, data=body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(request, timeout=20) as response:
            text = response.read().decode('utf-8', 'replace')
    except HTTPError as error:
        raise ValueError(f'M2Web replied HTTP {error.code}') from None
    return parse(text)


def connect(path=DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.executescript('''
      CREATE TABLE IF NOT EXISTS live (name TEXT PRIMARY KEY, value REAL, quality TEXT, good INTEGER, read_at TEXT);
      CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
    ''')
    return db


def save(db, values, at=None):
    at = at or now_iso()
    with db:
        for name, (value, quality, good) in values.items():
            db.execute('INSERT OR REPLACE INTO live(name,value,quality,good,read_at) VALUES (?,?,?,?,?)',
                       (name, value, quality, 1 if good else 0, at))
        db.execute("INSERT OR REPLACE INTO state(key,value) VALUES ('last_ok',?)", (at,))


def note_error(db, message):
    with db:
        db.execute("INSERT OR REPLACE INTO state(key,value) VALUES ('last_error',?)", (f'{now_iso()} {message}',))


def run():
    cfg = load_config()
    db = connect()
    failures = 0
    print(f'Live readings every {cfg["interval"]:g} s from M2Web.', flush=True)
    while True:
        started = time.monotonic()
        try:
            save(db, read_once(cfg))
            if failures:
                print('Live readings restored.', flush=True)
            failures = 0
        except Exception as error:  # never print credentials, URLs with secrets or reply bodies
            failures += 1
            message = str(error) if isinstance(error, ValueError) else type(error).__name__
            if failures in (1, 5) or failures % 60 == 0:
                print(f'Live read failed ({failures} in a row): {message}', flush=True)
            note_error(db, message)
        # back off after repeated failures: up to 2 minutes between tries
        wait = cfg['interval'] if failures < 3 else min(120, cfg['interval'] * failures)
        time.sleep(max(1.0, wait - (time.monotonic() - started)))


def test():
    cfg = load_config(Path('/etc/pbf-wastewater/m2web.json') if 'CREDENTIALS_DIRECTORY' not in os.environ else CREDS)
    values = read_once(cfg)
    print(f'M2Web OK: {len(values)} of {len(TAGS)} dashboard tags read from {cfg["ewon_name"]!r}.')
    for name in TAGS:
        v = values.get(name)
        print(f'  {name:26} ' + ('NOT IN REPLY' if v is None else f'{v[0]!s:>12}  quality {v[1] or "-":>6}  {"good" if v[2] else "BAD"}'))


if __name__ == '__main__':
    try:
        test() if '--test' in sys.argv else run()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print('Live readings stopped:', str(error) if isinstance(error, ValueError) else type(error).__name__, flush=True)
        sys.exit(1)
