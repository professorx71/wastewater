"""Run the wastewater dashboard locally for development.

    python run_local.py                 # demo data, http://127.0.0.1:8000
    python run_local.py --port 8080
    python run_local.py --db path\\to\\history-copy.sqlite3   # use a copied history DB (read-only)
    python run_local.py --reset         # wipe and regenerate local demo data

Everything lives in ./.local (git-ignored). Never points at production paths,
never sends email, and never touches the VPS. Sign in with the printed
local-only Admin or Operator username/password.
"""
import argparse, hashlib, json, math, os, random, secrets, sqlite3, sys, threading, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

ROOT = Path(__file__).parent
LOCAL = ROOT / '.local'
USER, PASSWORD = 'local', 'local'                # Admin
OP_USER, OP_PASSWORD = 'operator', 'operator'    # Operator
UTC = timezone.utc


def iso(t):
    return t.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def write_auth(path, user, password):
    salt = secrets.token_bytes(16)
    iterations = 100_000
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations).hex()
    path.write_text(json.dumps({'username': user, 'salt': salt.hex(), 'iterations': iterations, 'hash': digest}))


def reading(t, total):
    """Plausible raw tag values at time t (FLOW_RATE is raw US gal/min)."""
    h = t.timestamp() / 3600
    ph = 7.0 + 0.35 * math.sin(h / 3.1) + random.gauss(0, 0.04)
    level = 2.5 + 0.45 * math.sin(h / 5.3) + random.gauss(0, 0.02)
    gpm = max(0.0, 13.2 + 1.6 * math.sin(h / 2.2) + random.gauss(0, 0.25))
    rake = max(0.0, 30.0 + 2.0 * math.sin(h / 1.7) + random.gauss(0, 0.2))
    return {'pH_Reading': ph, 'EQ_Tank_Level_Ft': level, 'FLOW_RATE': gpm, 'Total_Flow_Li': total, 'Rake_Speed_VFD': rake,
            'Coag_Pump_Rate_Percent': 15 + random.gauss(0, 0.3), 'Coag_Pump_Run': 9.5 + random.gauss(0, 0.1),
            'Floc_Pump_Rate_Percent': 51 + random.gauss(0, 0.5), 'Floc_Pump_Run': 1.6 + random.gauss(0, 0.03)}


def insert(db, t, total):
    stamp = iso(t)
    for name, value in reading(t, total).items():
        db.execute('INSERT INTO samples(tag_id,name,stamp,value,quality,received_at) VALUES (?,?,?,?,?,?)',
                   (name, name, stamp, round(value, 4), 'good', stamp))


def seed_history(path, days=7, step=60):
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS state(key TEXT,value TEXT);
            CREATE TABLE IF NOT EXISTS samples(tag_id TEXT,name TEXT,stamp TEXT,value REAL,quality TEXT,received_at TEXT);
            CREATE TABLE IF NOT EXISTS alarms(tag_id TEXT,payload TEXT,received_at TEXT);
            CREATE INDEX IF NOT EXISTS samples_name_stamp ON samples(name,stamp);''')
        db.execute("INSERT INTO state(key,value) VALUES ('source','run_local.py demo data')")
        now = datetime.now(UTC)
        t, total = now - timedelta(days=days), 0.0
        while t <= now:
            gpm = reading(t, total)['FLOW_RATE']
            total += gpm * 3.78541 * step / 60
            insert(db, t, total)
            t += timedelta(seconds=step)
        db.commit()


def live_feed(path, interval=30):
    """Append a fresh demo sample every `interval` seconds so readings stay 'current'."""
    while True:
        time.sleep(interval)
        try:
            with sqlite3.connect(path, timeout=10) as db:
                row = db.execute("SELECT value FROM samples WHERE name='Total_Flow_Li' ORDER BY stamp DESC LIMIT 1").fetchone()
                now = datetime.now(UTC)
                total = (row[0] if row else 0.0) + reading(now, 0)['FLOW_RATE'] * 3.78541 * interval / 60
                insert(db, now, total)
        except sqlite3.Error as e:
            print('demo feed error:', e, file=sys.stderr)


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Browsers open idle pre-connections; a single-threaded server blocks on them."""
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        if not str(code).startswith(('2', '3')) or not self.path.startswith('/api/'):
            super().log_request(code, size)


def main():
    ap = argparse.ArgumentParser(description='Run the wastewater dashboard locally.')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--db', type=Path, help='existing history SQLite copy to read (disables demo data)')
    ap.add_argument('--reset', action='store_true', help='delete and regenerate .local demo data')
    ap.add_argument('--set-admin', metavar='USERNAME', help='change the local Admin login (asks for the password), then exit')
    ap.add_argument('--set-operator', metavar='USERNAME', help='change the local Operator login (asks for the password), then exit')
    args = ap.parse_args()

    try:
        from zoneinfo import ZoneInfo
        ZoneInfo('America/Vancouver')
    except Exception:
        sys.exit('Time-zone data is missing (normal on Windows). Install it once with:\n'
                 '    py -m pip install tzdata\nthen run this again.')

    LOCAL.mkdir(exist_ok=True)
    history = LOCAL / 'history.sqlite3'
    if args.reset:
        for f in LOCAL.glob('*'):
            if f.is_file():
                f.unlink()

    auth = LOCAL / 'web-auth.json'
    if not auth.exists():
        write_auth(auth, USER, PASSWORD)
    op_auth = LOCAL / 'operator-auth.json'
    if not op_auth.exists():
        write_auth(op_auth, OP_USER, OP_PASSWORD)

    if args.db:
        history = args.db.resolve()
        if not history.is_file():
            sys.exit(f'History database not found: {history}')
        try:
            with sqlite3.connect(f'file:{history}?mode=ro', uri=True) as db:
                db.execute('SELECT name,stamp,value,quality FROM samples LIMIT 1')
        except sqlite3.Error:
            sys.exit(f'{history} is not a collector history database (no "samples" table).\n'
                     'Omit --db to use the demo history in .local/.')
    elif not history.exists():
        print('Generating 7 days of demo history in .local/ ...')
        seed_history(history)

    # web.py reads these at import time, so set them first.
    os.environ['PBF_DB'] = str(history)
    os.environ['PBF_WEB_AUTH'] = str(auth)
    os.environ['PBF_APP_DB'] = str(LOCAL / 'dashboard.sqlite3')
    os.environ['PBF_OPERATOR_AUTH'] = str(op_auth)
    os.environ['PBF_EMAIL_CONFIG'] = str(LOCAL / 'email.json')           # absent → email never sends
    sys.path.insert(0, str(ROOT))
    import web

    if args.set_admin or args.set_operator:
        web.set_login('Admin' if args.set_admin else 'Operator', args.set_admin or args.set_operator)
        return

    if not args.db:
        threading.Thread(target=live_feed, args=(history,), daemon=True).start()

    with make_server(args.host, args.port, web.application, server_class=ThreadingWSGIServer, handler_class=QuietHandler) as httpd:
        print(f'\nWastewater dashboard running at http://{args.host}:{args.port}')
        with web.appdb() as conn:
            logins = web.account_summary(conn)
        for role, default in (('admin', (USER, PASSWORD)), ('operator', (OP_USER, OP_PASSWORD))):
            v = logins[role]
            shown = 'turned off' if not v['enabled'] else (f'{v["username"]} / (your password)' if v['source'] == 'settings' else '%s / %s' % default)
            print(f'{role.capitalize() + " login:":<17}{shown}')
        print(f'History DB:   {history}' + ('' if args.db else '  (demo data, new sample every 30 s)'))
        print('Press Ctrl+C to stop.\n')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped.')


if __name__ == '__main__':
    main()
