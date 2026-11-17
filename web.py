import base64, csv, hashlib, hmac, io, json, os, sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

ROOT=Path(__file__).parent
DB=Path(os.environ.get('PBF_DB','/var/lib/pbf-wastewater/history.sqlite3'))
AUTH=Path(os.environ.get('PBF_WEB_AUTH','/etc/pbf-wastewater/web-auth.json'))
TAGS={'pH_Reading':('EQ pH','pH',1), 'EQ_Tank_Level_Ft':('EQ tank level','ft',1),
      'FLOW_RATE':('Discharge flow','L/min',3.78541), 'Total_Flow_Li':('Total flow','L',1)}
UTC=timezone.utc

def iso(t): return t.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00','Z')
def parse(t):
    d=datetime.fromisoformat(t.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Timezone required')
    return d.astimezone(UTC)
def database():
    db=sqlite3.connect(f'file:{DB}?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db

def summary():
    now=datetime.now(UTC)
    db=database()
    try:
        state=dict(db.execute("SELECT key,value FROM state WHERE key!='cursor'"))
        result=[]
        for name,(label,unit,factor) in TAGS.items():
            row=db.execute('SELECT stamp,value,quality FROM samples WHERE name=? AND stamp<=? ORDER BY stamp DESC LIMIT 1',(name,iso(now))).fetchone()
            future=db.execute('SELECT COUNT(*) FROM samples WHERE name=? AND stamp>?',(name,iso(now))).fetchone()[0]
            item={'name':name,'label':label,'unit':unit,'value':None,'stamp':None,'age_seconds':None,'status':'missing','future_records':future}
            if row:
                age=(now-parse(row['stamp'])).total_seconds()
                item.update(stamp=row['stamp'],value=None if row['value'] is None else row['value']*factor,age_seconds=round(age),quality=row['quality'])
                item['status']='stale' if age>180 else 'current'
                if row['value'] is None or (row['quality'] and row['quality'].lower()!='good'): item['status']='invalid'
            result.append(item)
        return {'revision':'1.2.3','now':iso(now),'timezone':'America/Vancouver','tags':result,'collector':state}
    finally: db.close()

def window(params):
    now=datetime.now(UTC)
    start=parse(params['from'][0]) if 'from' in params else now-timedelta(hours=6)
    end=parse(params['to'][0]) if 'to' in params else now
    if 'startDate' in params and 'endDate' in params:
        zone=ZoneInfo('America/Vancouver')
        start=datetime.combine(date.fromisoformat(params['startDate'][0]),time.min,zone).astimezone(UTC)
        end=datetime.combine(date.fromisoformat(params['endDate'][0])+timedelta(days=1),time.min,zone).astimezone(UTC)
    end=min(end,now)
    if start>=end or end-start>timedelta(days=31): raise ValueError('Choose a range of up to 31 days, ending after its start.')
    return start,end

def history(params,export=False):
    start,end=window(params)
    db=database()
    try:
        rows=db.execute('SELECT name,stamp,value,quality FROM samples WHERE stamp>=? AND stamp<? AND name IN (?,?,?,?) ORDER BY stamp',(iso(start),iso(end),*TAGS)).fetchall()
    finally: db.close()
    if export:
        out=io.StringIO(newline=''); writer=csv.writer(out)
        writer.writerow(['Tag','Timestamp UTC','Timestamp America/Vancouver','Raw value','Display value','Display unit','Quality'])
        for r in rows:
            _,unit,factor=TAGS[r['name']]
            writer.writerow([r['name'],r['stamp'],parse(r['stamp']).astimezone(ZoneInfo('America/Vancouver')).isoformat(),r['value'],'' if r['value'] is None else f"{r['value']*factor:.2f}",unit,r['quality'] or 'not supplied'])
        return out.getvalue().encode('utf-8-sig')
    series={name:[] for name in TAGS}
    for r in rows:
        series[r['name']].append({'date':r['stamp'],'value':None if r['value'] is None else r['value']*TAGS[r['name']][2]})
    return {'from':iso(start),'to':iso(end),'series':series}


# Revision 1.2.1: historical samples remain read-only; settings/events live in separate SQLite.
import secrets, smtplib, sys
from contextlib import contextmanager
from email.message import EmailMessage
from urllib.parse import urlsplit
APP_DB=Path(os.environ.get('PBF_APP_DB','/var/lib/pbf-wastewater/dashboard-v1.2.sqlite3'))
OP_AUTH=Path(os.environ.get('PBF_OPERATOR_AUTH','/etc/pbf-wastewater/operator-auth.json'))
EMAIL_CFG=Path(os.environ.get('PBF_EMAIL_CONFIG','/etc/pbf-wastewater/email.json'))
DEFAULTS={'ph_low':5.8,'ph_high':9.0,'alarms_enabled':False,'email_enabled':False,
          'alarm_days':[0,1,2,3,4,5,6],'alarm_on':'00:00','alarm_off':'23:59',
          'daily_enabled':False,'daily_time':'08:00','monthly_enabled':False,'monthly_day':1,
          'monthly_time':'08:00','recipients':[],
          'alarm_confirm_samples':2,'alarm_recovery_samples':2,
          'email_recovery_enabled':True}
@contextmanager
def appdb():
    APP_DB.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(str(APP_DB),timeout=10)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    conn.executescript('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, stamp TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, state TEXT NOT NULL, actor TEXT, ack_at TEXT);
    CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, stamp TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS delivery (key TEXT PRIMARY KEY, stamp TEXT NOT NULL, outcome TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS alarm_state (key TEXT PRIMARY KEY, state TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ph_alarm_v2 (id INTEGER PRIMARY KEY CHECK (id=1),
        last_sample_stamp TEXT, candidate_kind TEXT, candidate_count INTEGER NOT NULL DEFAULT 0,
        active_kind TEXT, active_event INTEGER, recovery_count INTEGER NOT NULL DEFAULT 0,
        pending_recovery_event INTEGER, recovery_origin_event INTEGER);
    CREATE TABLE IF NOT EXISTS accounts (role TEXT PRIMARY KEY, username TEXT NOT NULL, salt TEXT NOT NULL,
        iterations INTEGER NOT NULL, hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL);''')
    conn.commit()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def settings(conn):
    result=dict(DEFAULTS)
    for r in conn.execute('SELECT key,value FROM settings'):
        if r['key'] in result: result[r['key']]=json.loads(r['value'])
    return result

# Logins: one Admin and one optional Operator. A login saved from Settings (or with
# --set-admin / --set-operator) lives in the app database and takes precedence; otherwise
# the original /etc/pbf-wastewater/*-auth.json files are used unchanged.
ROLES=('Admin','Operator')
ACCOUNT_FILES={'Admin':lambda:AUTH,'Operator':lambda:OP_AUTH}
PBKDF2_ITERATIONS=310000
_verified={}

class AccountError(ValueError):pass

def _file_account(role):
    try:
        obj=json.loads(ACCOUNT_FILES[role]().read_text())
        return dict(username=obj['username'],salt=obj['salt'],iterations=int(obj['iterations']),hash=obj['hash'],source='file')
    except (OSError,ValueError,KeyError,TypeError):return None

def load_account(role,conn=None):
    """Login record for a role, or None when missing or disabled."""
    try:
        if conn is None:
            with appdb() as c:return load_account(role,c)
        row=conn.execute('SELECT * FROM accounts WHERE role=?',(role,)).fetchone()
        if row:
            if row['disabled']:return None
            return dict(username=row['username'],salt=row['salt'],iterations=int(row['iterations']),hash=row['hash'],source='settings')
    except (OSError,sqlite3.Error):pass
    return _file_account(role)

def password_ok(rec,password):
    # PBKDF2 is deliberately slow; remember successful checks for this password + stored
    # record. Any password change alters the record, so old entries can never match.
    key=hashlib.sha256((password+'\0'+rec['salt']+rec['hash']).encode()).digest()
    if key in _verified:return True
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(rec['salt']),rec['iterations']).hex()
    if not hmac.compare_digest(digest,rec['hash']):return False
    if len(_verified)>256:_verified.clear()
    _verified[key]=True
    return True

def credentials(env):
    try:
        scheme,token=env.get('HTTP_AUTHORIZATION','').split(' ',1)
        if scheme.lower()!='basic':return None
        user,pwd=base64.b64decode(token,validate=True).decode('utf-8').split(':',1)
        return None if len(pwd)>256 else (user,pwd)
    except (ValueError,UnicodeError):return None

def role_for(env):
    cred=credentials(env)
    if not cred:return None
    user,pwd=cred
    try:
        with appdb() as conn:accounts={r:load_account(r,conn) for r in ROLES}
    except (OSError,sqlite3.Error):
        accounts={r:_file_account(r) for r in ROLES}
    for role in ROLES:
        rec=accounts[role]
        try:
            if rec and hmac.compare_digest(user,rec['username']) and password_ok(rec,pwd):return role
        except (ValueError,KeyError,TypeError):pass
    return None

MIN_PASSWORD_LENGTH={'Admin':6,'Operator':4}
USERNAME_CHARS=set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')
def check_username(name):
    if not isinstance(name,str) or not 1<=len(name)<=64 or set(name)-USERNAME_CHARS:
        raise AccountError('Username: 1–64 letters, numbers, dot, dash or underscore')
def check_password(role,pwd):
    low=MIN_PASSWORD_LENGTH[role]
    if not isinstance(pwd,str) or not low<=len(pwd)<=256:raise AccountError(f'{role} password must be {low}–256 characters')

def save_account(conn,role,username,password):
    check_username(username);check_password(role,password)
    other=load_account('Operator' if role=='Admin' else 'Admin',conn)
    if other and other['username'].lower()==username.lower():raise AccountError('Admin and Operator usernames must be different')
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,PBKDF2_ITERATIONS).hex()
    conn.execute('INSERT OR REPLACE INTO accounts(role,username,salt,iterations,hash,disabled,updated) VALUES (?,?,?,?,?,0,?)',
                 (role,username,salt.hex(),PBKDF2_ITERATIONS,digest,iso(datetime.now(UTC))))

def disable_operator(conn):
    conn.execute("INSERT OR REPLACE INTO accounts(role,username,salt,iterations,hash,disabled,updated) VALUES ('Operator','','',1,'',1,?)",
                 (iso(datetime.now(UTC)),))

def account_summary(conn):
    out={}
    for role in ROLES:
        rec=load_account(role,conn)
        out[role.lower()]={'enabled':rec is not None,'username':rec['username'] if rec else None,'source':rec['source'] if rec else None}
    return out

# Role permissions. Admin: everything. Operator: view readings/trends/alarms, download
# CSV history from Reports, and acknowledge alarms. Operator cannot view or change
# settings and cannot view the audit log. Enforced server-side for every endpoint.
ROLE_PERMISSIONS={
    'Admin':frozenset({'view','export','acknowledge','settings','audit','accounts'}),
    'Operator':frozenset({'view','export','acknowledge'}),
}
def can(role,permission): return permission in ROLE_PERMISSIONS.get(role,())

def audit(conn,actor,action,detail):
    conn.execute('INSERT INTO audit(stamp,actor,action,detail) VALUES (?,?,?,?)',
                 (iso(datetime.now(UTC)),actor,action,detail[:800]))

def val_settings(data,old):
    if not isinstance(data,dict) or set(data)-set(DEFAULTS):raise ValueError('Unknown setting')
    new=dict(old);new.update(data)
    for key in ('ph_low','ph_high'):
        if type(new[key]) not in (int,float) or not 0<=new[key]<=14:raise ValueError('Invalid pH limit')
    if new['ph_low']>=new['ph_high']:raise ValueError('pH low must be less than high')
    for key in ('alarms_enabled','email_enabled','daily_enabled','monthly_enabled','email_recovery_enabled'):
        if type(new[key]) is not bool:raise ValueError('Invalid switch')
    if not isinstance(new['alarm_days'],list) or any(type(d)!=int or d not in range(7) for d in new['alarm_days']):raise ValueError('Invalid days')
    for k in ('alarm_on','alarm_off','daily_time','monthly_time'):
        if not isinstance(new[k],str):raise ValueError('Invalid time')
        time.fromisoformat(new[k])
        if len(new[k])!=5:raise ValueError('Expected HH:MM')
    if type(new['monthly_day']) is not int or not 1<=new['monthly_day']<=28:raise ValueError('Choose day 1–28')
    for key in ('alarm_confirm_samples','alarm_recovery_samples'):
        if type(new[key]) is not int or not 1<=new[key]<=10:raise ValueError('Alarm confirmation must be 1–10 consecutive readings')
    if not isinstance(new['recipients'],list) or len(new['recipients'])>10:raise ValueError('Too many recipients')
    for address in new['recipients']:
        if not isinstance(address,str) or len(address)>254 or '@' not in address or '\n' in address or '\r' in address:raise ValueError('Invalid email address')
    return new

def records(conn,table,limit=200):
    return [dict(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY id DESC LIMIT ?', (min(limit,500),))]

def handle_post(path,env,role,conn):
    # Custom header + same-origin check defend cookie/browser cross-site submission with Basic auth.
    if env.get('HTTP_X_WW_REQUEST')!='1':return '403 Forbidden',{'error':'Request header required'}
    if env.get('HTTP_SEC_FETCH_SITE','same-origin') not in ('same-origin','none'):return '403 Forbidden',{'error':'Cross-site blocked'}
    origin=env.get('HTTP_ORIGIN')
    if origin and urlsplit(origin).netloc!=env.get('HTTP_HOST',''):return '403 Forbidden',{'error':'Origin rejected'}
    length=int(env.get('CONTENT_LENGTH') or 0)
    if length<1 or length>12000:raise ValueError('Invalid request length')
    body=json.loads(env['wsgi.input'].read(length))
    if not isinstance(body,dict):raise ValueError('JSON object required')
    if path=='/api/settings':
        if not can(role,'settings'):return '403 Forbidden',{'error':'Admin required to change settings'}
        updated=val_settings(body,settings(conn))
        for k,v in updated.items():conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',(k,json.dumps(v)))
        audit(conn,role,'Settings updated',', '.join(body.keys()))
        conn.commit()
        return '200 OK',updated
    if path.startswith('/api/accounts/'):
        if not can(role,'accounts'):return '403 Forbidden',{'error':'Admin required'}
        if path=='/api/accounts/admin':
            # Re-confirm the current Admin password (the one this request signed in with).
            cred=credentials(env);rec=load_account('Admin',conn);current=body.get('current_password')
            if not (cred and rec and isinstance(current,str) and hmac.compare_digest(current,cred[1]) and password_ok(rec,current)):
                return '403 Forbidden',{'error':'Current Admin password is incorrect'}
            username=body.get('username') or rec['username']
            save_account(conn,'Admin',username,body.get('new_password'))
            audit(conn,role,'Admin login changed','username '+username);conn.commit()
            return '200 OK',{'saved':'admin','username':username}
        if path=='/api/accounts/operator':
            save_account(conn,'Operator',body.get('username'),body.get('password'))
            audit(conn,role,'Operator login set','username '+body['username']);conn.commit()
            return '200 OK',{'saved':'operator','username':body['username']}
        if path=='/api/accounts/operator/disable':
            disable_operator(conn);audit(conn,role,'Operator login disabled','');conn.commit()
            return '200 OK',{'disabled':'operator'}
        return '404 Not Found',{'error':'Not found'}
    if path=='/api/ack':
        if not can(role,'acknowledge'):return '403 Forbidden',{'error':'Not permitted'}
        eid=body.get('id')
        if type(eid)!=int or eid<1:raise ValueError('Invalid event ID')
        cur=conn.execute("UPDATE events SET state='acknowledged', actor=?, ack_at=? WHERE id=? AND state='open'",(role,iso(datetime.now(UTC)),eid))
        if cur.rowcount==0:return '404 Not Found',{'error':'Open event not found'}
        audit(conn,role,'Alarm acknowledged',str(eid));conn.commit()
        return '200 OK',{'acknowledged':eid}
    return '404 Not Found',{'error':'Not found'}

def application(env,start_response):
    headers=[('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','no-referrer'),
             ('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'")]
    def send(status,body,kind='application/json; charset=utf-8',extra=()):
        if not isinstance(body,bytes):body=json.dumps(body,allow_nan=False).encode()
        start_response(status,headers+[('Content-Type',kind),('Content-Length',str(len(body)))]+list(extra))
        return [body]
    role=role_for(env)
    if env.get('PATH_INFO')=='/switch':
        # Basic Auth has no logout. Refuse the role the user is switching FROM so the browser
        # shows its sign-in box; once a different valid login arrives, return to the dashboard.
        old=parse_qs(env.get('QUERY_STRING','')).get('from',[''])[0]
        if role and role!=old:return send('302 Found',b'',extra=[('Location','/')])
        page=(b'<!doctype html><meta charset="utf-8"><title>Switch user</title>'
              b'<p>Sign in with a different account to switch user.</p><p><a href="/">Back to dashboard</a></p>')
        return send('401 Unauthorized',page,'text/html; charset=utf-8',[('WWW-Authenticate','Basic realm="Wastewater Dashboard", charset="UTF-8"')])
    if not role:return send('401 Unauthorized',{'error':'Sign in required'},extra=[('WWW-Authenticate','Basic realm="Wastewater Dashboard", charset="UTF-8"')])
    method=env.get('REQUEST_METHOD','GET');path=env.get('PATH_INFO','/')
    try:
        if method=='GET':
            if path=='/api/summary':
                d=summary();d['role']=role;d['permissions']=sorted(ROLE_PERMISSIONS[role])
                with appdb() as conn:
                    cfg=settings(conn);d['limits']={'low':cfg['ph_low'],'high':cfg['ph_high'],'enabled':cfg['alarms_enabled'],'confirm_samples':cfg['alarm_confirm_samples']}
                return send('200 OK',d)
            if path in ('/api/history','/api/export'):
                if not can(role,'export' if path.endswith('export') else 'view'):return send('403 Forbidden',{'error':'Not permitted'})
                data=history(parse_qs(env.get('QUERY_STRING','')),path.endswith('export'))
                if isinstance(data,bytes):return send('200 OK',data,'text/csv; charset=utf-8',[('Content-Disposition','attachment; filename="wastewater-history.csv"')])
                return send('200 OK',data)
            if path=='/api/settings':
                if not can(role,'settings'):return send('403 Forbidden',{'error':'Admin required'})
                with appdb() as conn:config=settings(conn)
                return send('200 OK',{'settings':config,'role':role,'email_configured':EMAIL_CFG.is_file()})
            if path=='/api/accounts':
                if not can(role,'accounts'):return send('403 Forbidden',{'error':'Admin required'})
                with appdb() as conn:return send('200 OK',account_summary(conn))
            if path in ('/api/events','/api/audit'):
                if not can(role,'audit' if path.endswith('audit') else 'view'):return send('403 Forbidden',{'error':'Admin required'})
                with appdb() as conn:return send('200 OK',{'rows':records(conn,'events' if path.endswith('events') else 'audit')})
            files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),'/process.jpg':('process.jpg','image/jpeg'),'/process-overview.webp':('process-overview.webp','image/webp')}
            if path in files:
                name,mime=files[path];return send('200 OK',(ROOT/'static'/name).read_bytes(),mime)
            return send('404 Not Found',{'error':'Not found'})
        if method=='POST':
            with appdb() as conn:
                status,body=handle_post(path,env,role,conn)
                return send(status,body)
        return send('405 Method Not Allowed',{'error':'Unsupported method'},extra=[('Allow','GET, POST')])
    except AccountError as e:return send('400 Bad Request',{'error':str(e)})
    except (ValueError,KeyError,json.JSONDecodeError):return send('400 Bad Request',{'error':'Invalid request data'})
    except (OSError,sqlite3.Error):return send('503 Service Unavailable',{'error':'Storage temporarily unavailable'})

# Run via separate systemd timer, never in Gunicorn workers. Email disabled by default.
def send_mail(config,subject,body,recipients):
    msg=EmailMessage();msg['Subject']=subject;msg['From']=config['sender'];msg['To']=', '.join(recipients);msg.set_content(body)
    import ssl
    host=config['host'];port=int(config.get('port',587))
    if port==465:
        with smtplib.SMTP_SSL(host,port,timeout=20,context=ssl.create_default_context()) as s:
            s.login(config['username'],config['password']);s.send_message(msg)
    else:
        with smtplib.SMTP(host,port,timeout=20) as s:
            s.starttls(context=ssl.create_default_context());s.login(config['username'],config['password']);s.send_message(msg)

def ph_status(ph, cfg):
    """Only fresh, non-invalid measurements participate in alarm decisions."""
    if not ph or ph.get('status')!='current' or ph.get('value') is None:
        return 'unknown'
    v=ph['value']
    return 'high' if v>cfg['ph_high'] else 'low' if v<cfg['ph_low'] else 'normal'


def update_ph_incidents(conn, cfg, ph):
    """One sample stamp counts once. Alarm and recovery require consecutive fresh samples.

    This is a simple confirmation filter, not a safety-rated control or PLC alarm.
    Legacy 1.2.0 current state is adopted to avoid duplicate notifications on upgrade.
    """
    old=conn.execute('SELECT * FROM ph_alarm_v2 WHERE id=1').fetchone()
    if old is None:
        prior=conn.execute("SELECT state FROM alarm_state WHERE key='ph'").fetchone()
        legacy=prior['state'] if prior and prior['state'] in ('high','low') else None
        conn.execute('''INSERT INTO ph_alarm_v2
             (id,last_sample_stamp,candidate_kind,candidate_count,active_kind,active_event,recovery_count)
             VALUES(1,NULL,NULL,0,?,NULL,0)''',(legacy,))
        old=conn.execute('SELECT * FROM ph_alarm_v2 WHERE id=1').fetchone()
    if not cfg['alarms_enabled']:
        # Keep prior incidents for audit, but pause confirmation and mail generation.
        conn.execute('UPDATE ph_alarm_v2 SET candidate_kind=NULL,candidate_count=0,recovery_count=0 WHERE id=1')
        conn.commit()
        return
    status=ph_status(ph,cfg)
    stamp=ph.get('stamp') if ph else None
    if status=='unknown' or not stamp or stamp==old['last_sample_stamp']:
        return
    active=old['active_kind']; active_event=old['active_event']
    cand=old['candidate_kind']; count=old['candidate_count']; recovery_count=old['recovery_count']
    recovered_event=old['pending_recovery_event']; recovery_origin=old['recovery_origin_event']
    if active is None:
        if status in ('high','low'):
            count=count+1 if cand==status else 1
            cand=status
            if count>=cfg['alarm_confirm_samples']:
                cur=conn.execute('INSERT INTO events(stamp,kind,detail,state) VALUES (?,?,?,?)',
                    (iso(datetime.now(UTC)),f'pH {status}',
                     f"Confirmed over {count} readings; pH {ph['value']:.2f}; limits {cfg['ph_low']:.2f}–{cfg['ph_high']:.2f}",'open'))
                active=status;active_event=cur.lastrowid
                audit(conn,'system','Confirmed pH incident',f'{status} incident {active_event}')
                cand=None;count=0
        else:
            cand=None;count=0
        recovery_count=0
    elif status==active:
        cand=None;count=0;recovery_count=0
    elif status=='normal':
        recovery_count+=1
        cand=None;count=0
        if recovery_count>=cfg['alarm_recovery_samples']:
            if active_event is not None:
                conn.execute("UPDATE events SET state='closed' WHERE id=? AND state IN ('open','acknowledged')",(active_event,))
                cur=conn.execute('INSERT INTO events(stamp,kind,detail,state) VALUES (?,?,?,?)',
                    (iso(datetime.now(UTC)),'pH recovered',f'Incident {active_event}; pH {ph["value"]:.2f}; confirmed normal over {recovery_count} readings','closed'))
                recovered_event=cur.lastrowid;recovery_origin=active_event
                audit(conn,'system','pH recovery confirmed',f'incident {active_event}')
            active=None;active_event=None;recovery_count=0
    else:
        # Direct high-to-low (or low-to-high) transition: confirm opposite extreme.
        count=count+1 if cand==status else 1
        cand=status;recovery_count=0
        if count>=cfg['alarm_confirm_samples']:
            if active_event is not None:
                conn.execute("UPDATE events SET state='closed' WHERE id=? AND state IN ('open','acknowledged')",(active_event,))
            cur=conn.execute('INSERT INTO events(stamp,kind,detail,state) VALUES (?,?,?,?)',
                (iso(datetime.now(UTC)),f'pH {status}',
                 f"Confirmed over {count} readings; pH {ph['value']:.2f}; limits {cfg['ph_low']:.2f}–{cfg['ph_high']:.2f}",'open'))
            active=status;active_event=cur.lastrowid
            audit(conn,'system','Confirmed pH incident',f'{status} incident {active_event}')
            cand=None;count=0
    conn.execute('''UPDATE ph_alarm_v2 SET last_sample_stamp=?,candidate_kind=?,candidate_count=?,
        active_kind=?,active_event=?,recovery_count=?,pending_recovery_event=?,recovery_origin_event=?
        WHERE id=1''',(stamp,cand,count,active,active_event,recovery_count,recovered_event,recovery_origin))
    conn.commit()


def scheduler():
    now=datetime.now(ZoneInfo('America/Vancouver'))
    with appdb() as conn:
        cfg=settings(conn)
        readings=summary()['tags']
        ph=next((r for r in readings if r['name']=='pH_Reading'),None)
        update_ph_incidents(conn,cfg,ph)
        if not EMAIL_CFG.is_file() or not cfg['recipients']:
            return
        mail=json.loads(EMAIL_CFG.read_text())
        if not all(mail.get(k) for k in ('host','sender','username','password')):
            return
        hhmm=now.strftime('%H:%M');today=now.strftime('%Y-%m-%d');weekday=now.weekday()
        on=time.fromisoformat(cfg['alarm_on']);off=time.fromisoformat(cfg['alarm_off']);moment=now.time()
        within=(on<=moment<=off) if on<=off else (moment>=on or moment<=off)
        eligible=cfg['email_enabled'] and cfg['alarms_enabled'] and weekday in cfg['alarm_days'] and within
        jobs=[]
        if eligible:
            tracker=conn.execute('SELECT * FROM ph_alarm_v2 WHERE id=1').fetchone()
            if tracker and tracker['active_event'] and tracker['active_kind'] in ('high','low') and ph_status(ph,cfg)==tracker['active_kind']:
                eid=tracker['active_event']
                jobs.append((f'alarm:incident:{eid}',f'Wastewater pH {tracker["active_kind"].upper()} alarm',
                    f'Confirmed pH incident #{eid}\\nCurrent reading: {ph["value"]:.2f} pH\\nLow: {cfg["ph_low"]:.2f}\\nHigh: {cfg["ph_high"]:.2f}\\nTimestamp: {now.isoformat()}'))
            if tracker and tracker['pending_recovery_event'] and cfg['email_recovery_enabled']:
                eid=tracker['recovery_origin_event']; rid=tracker['pending_recovery_event']
                delivered=conn.execute('SELECT 1 FROM delivery WHERE key=?',(f'alarm:incident:{eid}',)).fetchone()
                recovery=conn.execute('SELECT stamp FROM events WHERE id=?',(rid,)).fetchone()
                recent=recovery and (datetime.now(UTC)-parse(recovery['stamp']))<timedelta(hours=24)
                if delivered and recent:
                    jobs.append((f'recovery:incident:{eid}',f'Wastewater pH recovered — incident #{eid}',
                        f'Incident #{eid} recovered after {cfg["alarm_recovery_samples"]} normal readings.\\nTimestamp: {now.isoformat()}'))
        if cfg['daily_enabled'] and hhmm==cfg['daily_time']:
            jobs.append((f'daily:{today}','Wastewater Daily Report '+today,historical_report(now,'daily')))
        if cfg['monthly_enabled'] and now.day==cfg['monthly_day'] and hhmm==cfg['monthly_time']:
            jobs.append((f'monthly:{now:%Y-%m}','Wastewater Monthly Report '+now.strftime('%Y-%m'),historical_report(now,'monthly')))
        for key,subject,body in jobs:
            if conn.execute('SELECT 1 FROM delivery WHERE key=?',(key,)).fetchone():
                continue
            try:
                send_mail(mail,subject,body,cfg['recipients'])
                conn.execute('INSERT INTO delivery(key,stamp,outcome) VALUES (?,?,?)',
                             (key,iso(datetime.now(UTC)),'sent'))
                audit(conn,'system','Email delivered',key)
            except Exception as exc:
                audit(conn,'system','Email failed',key+' '+type(exc).__name__)
                print('Email failed:',type(exc).__name__,file=sys.stderr)
            conn.commit()
def historical_report(now,period):
    zone=ZoneInfo('America/Vancouver')
    if period=='daily':
        end=datetime.combine(now.date(),time.min,zone)
        start=end-timedelta(days=1)
    else:
        end=datetime(now.year,now.month,1,tzinfo=zone)
        if now.month==1:start=datetime(now.year-1,12,1,tzinfo=zone)
        else:start=datetime(now.year,now.month-1,1,tzinfo=zone)
    out=[f'Wastewater {period.title()} Historical Report',
         f'Period: {start:%Y-%m-%d %I:%M %p} through {end:%Y-%m-%d %I:%M %p} Pacific',
         'Sample count, minimum, average, maximum (converted to display units)', '']
    with database() as conn:
        for name,(label,unit,factor) in TAGS.items():
            row=conn.execute('SELECT COUNT(value),MIN(value),AVG(value),MAX(value) FROM samples WHERE name=? AND stamp>=? AND stamp<?',
                             (name,iso(start),iso(end))).fetchone()
            count,mn,avg,mx=row
            if not count:out.append(f'{label}: no readings')
            else:out.append(f'{label} ({unit}): count {count:,}; min {mn*factor:.2f}; avg {avg*factor:.2f}; max {mx*factor:.2f}')
    out.extend(['','Report uses recorded samples; missing or invalid-quality data should be reviewed separately.'])
    return '\n'.join(out)

def set_login(role,username):
    """Create or replace a login from the server shell (also the lockout-recovery path).
    The password is prompted, never passed on the command line."""
    import getpass
    pwd=getpass.getpass(f'New {role} password for {username}: ')
    if getpass.getpass('Repeat password: ')!=pwd:sys.exit('Passwords do not match')
    try:
        with appdb() as conn:
            save_account(conn,role,username,pwd)
            audit(conn,'server shell',f'{role} login changed','username '+username)
    except AccountError as e:sys.exit(str(e))
    print(f'{role} login saved for {username}. It takes effect at the next sign-in.')

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--scheduler':scheduler()
    elif len(sys.argv)>1 and sys.argv[1]=='--init':
        with appdb() as conn: print('App database ready:',APP_DB)
    elif len(sys.argv)>2 and sys.argv[1]=='--set-operator':set_login('Operator',sys.argv[2])
    elif len(sys.argv)>2 and sys.argv[1]=='--set-admin':set_login('Admin',sys.argv[2])
    else: print('Usage: python3 web.py --init|--scheduler|--set-admin USERNAME|--set-operator USERNAME')
