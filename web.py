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
        ages=[t['age_seconds'] for t in result if t['age_seconds'] is not None]
        return {'revision':'1.2.9','now':iso(now),'timezone':'America/Vancouver','tags':result,'collector':state,
                'data_age_seconds':min(ages) if ages else None}
    finally: db.close()

def window(params,max_days=31):
    now=datetime.now(UTC)
    start=parse(params['from'][0]) if 'from' in params else now-timedelta(hours=6)
    end=parse(params['to'][0]) if 'to' in params else now
    if 'startDate' in params and 'endDate' in params:
        zone=ZoneInfo('America/Vancouver')
        start=datetime.combine(date.fromisoformat(params['startDate'][0]),time.min,zone).astimezone(UTC)
        end=datetime.combine(date.fromisoformat(params['endDate'][0])+timedelta(days=1),time.min,zone).astimezone(UTC)
    end=min(end,now)
    if start>=end or end-start>timedelta(days=max_days): raise ValueError(f'Choose a range of up to {max_days} days, ending after its start.')
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


# Excel report (Reports page): one worksheet per tag plus a Summary worksheet.
# Built with the standard library only, so the server needs no extra packages.
import zipfile
from xml.sax.saxutils import escape as _xml
REPORT_TAGS=('EQ_Tank_Level_Ft','FLOW_RATE','Total_Flow_Li')
RAW_UNITS={'pH_Reading':'pH','EQ_Tank_Level_Ft':'ft','FLOW_RATE':'US gal/min','Total_Flow_Li':'L'}
_XL_EPOCH=datetime(1899,12,30)
# Cell styles: 0 normal, 1 date-time, 2 two decimals, 3 bold, 4 date
_XL_STYLES=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="mm/dd/yyyy hh:mm:ss"/><numFmt numFmtId="165" formatCode="mm/dd/yyyy"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')

def _xl_col(i):
    s=''
    i+=1
    while i:i,r=divmod(i-1,26);s=chr(65+r)+s
    return s

def _xl_cell(ref,v,style=0):
    if v is None or v=='':return ''
    if isinstance(v,datetime):
        return f'<c r="{ref}" s="{style or 1}"><v>{(v.replace(tzinfo=None)-_XL_EPOCH).total_seconds()/86400!r}</v></c>'
    if isinstance(v,date):
        return f'<c r="{ref}" s="{style or 4}"><v>{(v-_XL_EPOCH.date()).days}</v></c>'
    if isinstance(v,(int,float)):
        return f'<c r="{ref}" s="{style}"><v>{v!r}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{_xml(str(v))}</t></is></c>'

def _xl_sheet(z,path,rows,widths):
    """Write one worksheet into the zip, row by row (rows may be a generator)."""
    with z.open(path,'w',force_zip64=True) as f:
        w=lambda t:f.write(t.encode('utf-8'))
        w('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>'
          +''.join(f'<col min="{i+1}" max="{i+1}" width="{x}" customWidth="1"/>' for i,x in enumerate(widths))
          +'</cols><sheetData>')
        for n,row in enumerate(rows,1):
            w(f'<row r="{n}">'+''.join(_xl_cell(f'{_xl_col(i)}{n}',*(c if isinstance(c,tuple) else (c,))) for i,c in enumerate(row))+'</row>')
        w('</sheetData></worksheet>')

def xlsx(sheets):
    """sheets: list of (name, rows, column widths). A cell is a value or (value, style)."""
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            +''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))
            +'</Types>')
        z.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            +''.join(f'<sheet name="{_xml(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_,_) in enumerate(sheets,1))
            +'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            +''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))
            +f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml',_XL_STYLES)
        for i,(_,rows,widths) in enumerate(sheets,1):_xl_sheet(z,f'xl/worksheets/sheet{i}.xml',rows,widths)
    return buf.getvalue()

REPORT_MAX_DAYS=92   # one full quarter
SUMMARY_TAG='Total_Flow_Li'
SUMMARY_MIN=1   # readings at or below this (e.g. 0 after a meter reset) are not valid EOD values
from decimal import Decimal, ROUND_DOWN
def truncate2(v):
    """Cut to 2 decimals without rounding: 524714.2551 -> 524714.25."""
    return Decimal(repr(v)).quantize(Decimal('0.01'),rounding=ROUND_DOWN)
def quarter_label(d):
    q=(d.month-1)//3
    return f"{d.year} Q{q+1} ({('Jan-Mar','Apr-Jun','Jul-Sep','Oct-Dec')[q]})"

def report_xlsx(params,data_tags=None):
    """Excel report for one tag or 'all'. Returns (filename, bytes).
    Summary: Total_Flow_Li end-of-day (last valid) reading of each day, the usage since the
    previous EOD, and monthly and quarterly totals. data_tags overrides which tag worksheets
    are included (emails use fewer to keep attachments small)."""
    choice=params.get('tag',['all'])[0]
    if choice!='all' and choice not in REPORT_TAGS:raise ValueError('Choose a report type from the list.')
    names=tuple(data_tags) if data_tags is not None else REPORT_TAGS if choice=='all' else (choice,)
    start,end=window(params,REPORT_MAX_DAYS)
    zone=ZoneInfo('America/Vancouver')
    B=lambda t:(t,3)
    local_start=start.astimezone(zone).date()
    local_end=(end.astimezone(zone)-timedelta(microseconds=1)).date()
    date_range=f'{local_start:%m/%d/%Y} to {local_end:%m/%d/%Y}'
    query='SELECT stamp,value FROM samples WHERE name=? AND stamp>=? AND stamp<? ORDER BY stamp'

    def readings(name):  # tag worksheet, streamed straight from the database
        unit=RAW_UNITS[name]
        yield [B('Timestamp UTC'),B('Timestamp America/Vancouver'),B('Raw Value'),B('Unit Of Measure')]
        db=database()
        try:
            for stamp,value in db.execute(query,(name,iso(start),iso(end))):
                t=parse(stamp);yield [t,t.astimezone(zone),value,unit]
        finally:db.close()

    # Summary: Total_Flow_Li only (litres). EOD value of a day = last reading above SUMMARY_MIN
    # that day; if the day has none (meter reads 0 or no data), the last valid reading before it.
    tag=SUMMARY_TAG
    db=database()
    try:
        prior=db.execute('SELECT stamp,value FROM samples WHERE name=? AND stamp<? AND value>? ORDER BY stamp DESC LIMIT 1',
                         (tag,iso(start),SUMMARY_MIN)).fetchone()
        valid={}   # local date -> (local time, value) of the day's last valid reading
        for stamp,value in db.execute(query,(tag,iso(start),iso(end))):
            if value is None or value<=SUMMARY_MIN:continue
            local=parse(stamp).astimezone(zone)
            valid[local.date()]=(local,value)
    finally:db.close()
    last=(parse(prior[0]).astimezone(zone),prior[1]) if prior else None
    prev=truncate2(last[1]) if last else None
    daily=[]   # (date, EOD value, usage or None, reading time, note)
    day=local_start
    while day<=local_end:
        if day in valid:last=valid[day]
        if last:
            v=truncate2(last[1]);note=''
            if last[0].date()!=day:note=f'No reading above 1 L; carried forward from {last[0]:%m/%d/%Y %H:%M}'
            if prev is None:usage=None;note=note or 'No earlier reading; usage not calculated'
            elif v<prev:usage=None;note=(note+'; ' if note else '')+'Meter reading dropped (reset?); usage not calculated'
            else:usage=v-prev
            daily.append((day,v,usage,last[0],note));prev=v
        day+=timedelta(days=1)
    months={};quarters={}
    def add(bucket,key,v,usage):
        total,use=bucket.get(key,(Decimal(0),None))
        bucket[key]=(total+v,use if usage is None else (use or Decimal(0))+usage)
    for day,v,usage,_,_ in daily:add(months,f'{day:%Y-%m}',v,usage)
    for m,(v,usage) in months.items():add(quarters,quarter_label(date.fromisoformat(m+'-01')),v,usage)
    num=lambda v:'' if v is None else (float(v),2)
    summary_rows=[[B('Wastewater report summary')],
                  ['Tag',tag],['Unit Of Measure','L (litres)'],['Date range (America/Vancouver)',date_range],[]]
    if not daily:
        summary_rows.append(['No valid Total_Flow_Li readings in or before this date range.'])
    else:
        summary_rows.append([B('Quarter'),B('Total Quarterly Value (L)'),B('Quarterly Usage (L)')])
        summary_rows+=[[q,num(v),num(u)] for q,(v,u) in sorted(quarters.items())]
        summary_rows+=[[],[B('Month'),B('Total Monthly Value (L)'),B('Monthly Usage (L)')]]
        summary_rows+=[[f'{m[5:]}/{m[:4]}',num(v),num(u)] for m,(v,u) in sorted(months.items())]
        summary_rows+=[[],[B('Date'),B('EOD Daily Value (L)'),B('Daily Usage (L)'),B('Reading Used (America/Vancouver)'),B('Notes')]]
        summary_rows+=[[day,num(v),num(u),when.replace(tzinfo=None),note] for day,v,u,when,note in daily]
    summary_rows+=[[],['EOD Daily Value is the last Total_Flow_Li reading above 1 L on that day (America/Vancouver), cut to 2 decimals. '
                       'If a day has no reading above 1 L, the last valid reading before it is used (see Reading Used and Notes). '
                       'Monthly total = sum of the EOD daily values in the month; quarterly total = sum of those monthly totals. '
                       'Daily Usage = EOD value minus the previous EOD value (litres that went through the meter that day); '
                       'monthly and quarterly usage add up the daily usage. Only days inside the selected date range are counted.']]
    sheets=[('Summary',summary_rows,(34,22,20,30,60))]+[(n,readings(n),(22,30,14,18)) for n in names]
    fname=f'wastewater-report-{choice}-{local_start:%Y-%m-%d}-to-{local_end:%Y-%m-%d}.xlsx'
    return fname,xlsx(sheets)


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
          'email_recovery_enabled':True,
          # Data-stopped alert: no new reading from any tag for this many minutes.
          'stale_alert_enabled':True,'stale_alert_minutes':15,
          # EQ tank high level (ft), confirmed like pH over consecutive readings.
          'level_alarm_enabled':False,'level_high_ft':10.0,
          # No discharge flow: flow below this rate (L/min) for this many minutes.
          'flow_alarm_enabled':False,'flow_low_lpm':1.0,'flow_low_minutes':30,
          # Quarterly Excel report, sent on the monthly day/time in Jan, Apr, Jul and Oct.
          'quarterly_enabled':False}
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
        iterations INTEGER NOT NULL, hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS operators (username TEXT PRIMARY KEY, salt TEXT NOT NULL, iterations INTEGER NOT NULL,
        hash TEXT NOT NULL, updated TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS login_failures (client TEXT NOT NULL, attempt TEXT NOT NULL, stamp TEXT NOT NULL,
        PRIMARY KEY (client, attempt));''')
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

# Operators: any number of named Operator logins, each with its own password, so the
# audit log shows who did what. Until the first change from Settings, the original single
# Operator login (accounts row or operator-auth.json) is used; the first change copies it
# into the operators table, which is used from then on.
def _operators_managed(conn):
    return conn.execute("SELECT 1 FROM settings WHERE key='operators_table'").fetchone() is not None

def operator_records(conn):
    if _operators_managed(conn):
        return [dict(username=r['username'],salt=r['salt'],iterations=int(r['iterations']),hash=r['hash'],source='settings')
                for r in conn.execute('SELECT * FROM operators ORDER BY username COLLATE NOCASE')]
    legacy=load_account('Operator',conn)
    return [legacy] if legacy else []

def _manage_operators(conn):
    if _operators_managed(conn):return
    for rec in operator_records(conn):
        conn.execute('INSERT OR REPLACE INTO operators(username,salt,iterations,hash,updated) VALUES (?,?,?,?,?)',
                     (rec['username'],rec['salt'],rec['iterations'],rec['hash'],iso(datetime.now(UTC))))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('operators_table','true')")

def save_operator(conn,username,password):
    check_username(username);check_password('Operator',password)
    admin=load_account('Admin',conn)
    if admin and admin['username'].lower()==username.lower():raise AccountError('Admin and Operator usernames must be different')
    _manage_operators(conn)
    if conn.execute('SELECT COUNT(*) FROM operators WHERE username!=? AND lower(username)=lower(?)',(username,username)).fetchone()[0]:
        raise AccountError('An Operator with that username (different capitals) already exists')
    exists=conn.execute('SELECT 1 FROM operators WHERE username=?',(username,)).fetchone()
    if not exists and conn.execute('SELECT COUNT(*) FROM operators').fetchone()[0]>=50:raise AccountError('Maximum 50 Operator logins')
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,PBKDF2_ITERATIONS).hex()
    conn.execute('INSERT OR REPLACE INTO operators(username,salt,iterations,hash,updated) VALUES (?,?,?,?,?)',
                 (username,salt.hex(),PBKDF2_ITERATIONS,digest,iso(datetime.now(UTC))))

def remove_operator(conn,username):
    _manage_operators(conn)
    return conn.execute('DELETE FROM operators WHERE username=?',(username,)).rowcount

# Sign-in lockout: after LOCK_ATTEMPTS different wrong passwords from one client within
# LOCK_WINDOW, that client is refused (429) until the attempts age out. Repeating the SAME
# wrong password (e.g. an old browser tab after a password change) counts only once.
LOCK_ATTEMPTS=10
LOCK_WINDOW=timedelta(minutes=15)

def client_id(env,user):
    remote=env.get('REMOTE_ADDR','')
    if remote in ('127.0.0.1','::1',''):
        # Behind the HTTPS proxy: use the address it forwards. Without one, lock per username.
        fwd=env.get('HTTP_X_REAL_IP') or env.get('HTTP_X_FORWARDED_FOR','').split(',')[-1].strip()
        return 'ip:'+fwd[:64] if fwd else 'user:'+user.lower()[:64]
    return 'ip:'+remote[:64]

def _login_secret(conn):
    row=conn.execute("SELECT value FROM settings WHERE key='login_secret'").fetchone()
    if not row:
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('login_secret',?)",(json.dumps(secrets.token_hex(32)),))
        row=conn.execute("SELECT value FROM settings WHERE key='login_secret'").fetchone()
    return json.loads(row['value'])

def locked_out(conn,client):
    since=iso(datetime.now(UTC)-LOCK_WINDOW)
    return conn.execute('SELECT COUNT(*) FROM login_failures WHERE client=? AND stamp>=?',(client,since)).fetchone()[0]>=LOCK_ATTEMPTS

def record_failure(conn,client,user,pwd):
    now=datetime.now(UTC)
    conn.execute('DELETE FROM login_failures WHERE stamp<?',(iso(now-LOCK_WINDOW),))
    attempt=hmac.new(_login_secret(conn).encode(),(user+'\0'+pwd).encode(),'sha256').hexdigest()[:32]
    conn.execute('INSERT OR REPLACE INTO login_failures(client,attempt,stamp) VALUES (?,?,?)',(client,attempt,iso(now)))
    if conn.execute('SELECT COUNT(*) FROM login_failures WHERE client=?',(client,)).fetchone()[0]==LOCK_ATTEMPTS:
        audit(conn,'system','Sign-in locked',f'{client} after {LOCK_ATTEMPTS} wrong passwords; unlocks within {LOCK_WINDOW.seconds//60} minutes')

LOCKED='locked'
def who_for(env):
    """(role, username) for valid credentials, LOCKED when this client is locked out, else None."""
    cred=credentials(env)
    if not cred:return None
    user,pwd=cred
    try:
        with appdb() as conn:
            client=client_id(env,user)
            if locked_out(conn,client):return LOCKED
            admin=load_account('Admin',conn)
            found=None
            if admin and hmac.compare_digest(user,admin['username']) and password_ok(admin,pwd):found=('Admin',user)
            else:
                for rec in operator_records(conn):
                    if hmac.compare_digest(user,rec['username']) and password_ok(rec,pwd):found=('Operator',user);break
            if found:conn.execute('DELETE FROM login_failures WHERE client=?',(client,))
            else:record_failure(conn,client,user,pwd)
            return found
    except (OSError,sqlite3.Error):
        # Storage unavailable: fall back to the original server files, without lockout.
        for role in ROLES:
            rec=_file_account(role)
            try:
                if rec and hmac.compare_digest(user,rec['username']) and password_ok(rec,pwd):return (role,user)
            except (ValueError,KeyError,TypeError):pass
        return None
    except (ValueError,KeyError,TypeError):return None

def role_for(env):
    who=who_for(env)
    return who[0] if isinstance(who,tuple) else None

MIN_PASSWORD_LENGTH={'Admin':6,'Operator':4}
USERNAME_CHARS=set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')
def check_username(name):
    if not isinstance(name,str) or not 1<=len(name)<=64 or set(name)-USERNAME_CHARS:
        raise AccountError('Username: 1–64 letters, numbers, dot, dash or underscore')
def check_password(role,pwd):
    low=MIN_PASSWORD_LENGTH[role]
    if not isinstance(pwd,str) or not low<=len(pwd)<=256:raise AccountError(f'{role} password must be {low}–256 characters')

def save_account(conn,role,username,password):
    if role=='Operator':return save_operator(conn,username,password)
    check_username(username);check_password(role,password)
    if any(r['username'].lower()==username.lower() for r in operator_records(conn)):
        raise AccountError('Admin and Operator usernames must be different')
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,PBKDF2_ITERATIONS).hex()
    conn.execute('INSERT OR REPLACE INTO accounts(role,username,salt,iterations,hash,disabled,updated) VALUES (?,?,?,?,?,0,?)',
                 (role,username,salt.hex(),PBKDF2_ITERATIONS,digest,iso(datetime.now(UTC))))

def disable_operator(conn):
    """Turn off ALL Operator logins (also marks the original single login off for older revisions)."""
    _manage_operators(conn)
    conn.execute('DELETE FROM operators')
    conn.execute("INSERT OR REPLACE INTO accounts(role,username,salt,iterations,hash,disabled,updated) VALUES ('Operator','','',1,'',1,?)",
                 (iso(datetime.now(UTC)),))

def account_summary(conn):
    admin=load_account('Admin',conn)
    ops=operator_records(conn)
    return {'admin':{'enabled':admin is not None,'username':admin['username'] if admin else None,'source':admin['source'] if admin else None},
            # 'operator' is kept for older tools (set-login): all operator usernames, or off.
            'operator':{'enabled':bool(ops),'username':', '.join(r['username'] for r in ops) if ops else None,'source':ops[0]['source'] if ops else None},
            'operators':[{'username':r['username'],'source':r['source']} for r in ops]}

def actor(role,username):return f'{username} ({role})'

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
    for key in ('alarms_enabled','email_enabled','daily_enabled','monthly_enabled','email_recovery_enabled',
                'stale_alert_enabled','level_alarm_enabled','flow_alarm_enabled','quarterly_enabled'):
        if type(new[key]) is not bool:raise ValueError('Invalid switch')
    if type(new['stale_alert_minutes']) is not int or not 5<=new['stale_alert_minutes']<=1440:raise ValueError('Data-stopped alert: 5–1440 minutes')
    if type(new['level_high_ft']) not in (int,float) or not 0<new['level_high_ft']<=100:raise ValueError('High level: 0–100 ft')
    if type(new['flow_low_lpm']) not in (int,float) or not 0<new['flow_low_lpm']<=10000:raise ValueError('Low flow: 0–10000 L/min')
    if type(new['flow_low_minutes']) is not int or not 5<=new['flow_low_minutes']<=1440:raise ValueError('Low flow duration: 5–1440 minutes')
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

def handle_post(path,env,role,conn,user=''):
    who=actor(role,user)
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
        audit(conn,who,'Settings updated',', '.join(body.keys()))
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
            audit(conn,who,'Admin login changed','username '+username);conn.commit()
            return '200 OK',{'saved':'admin','username':username}
        if path=='/api/accounts/operator':
            save_operator(conn,body.get('username'),body.get('password'))
            audit(conn,who,'Operator login set','username '+body['username']);conn.commit()
            return '200 OK',{'saved':'operator','username':body['username']}
        if path=='/api/accounts/operator/remove':
            name=body.get('username')
            if not isinstance(name,str) or not remove_operator(conn,name):return '404 Not Found',{'error':'Operator not found'}
            audit(conn,who,'Operator login removed','username '+name);conn.commit()
            return '200 OK',{'removed':name}
        if path=='/api/accounts/operator/disable':
            disable_operator(conn);audit(conn,who,'Operator login disabled','all Operator logins');conn.commit()
            return '200 OK',{'disabled':'operator'}
        return '404 Not Found',{'error':'Not found'}
    if path=='/api/ack':
        if not can(role,'acknowledge'):return '403 Forbidden',{'error':'Not permitted'}
        eid=body.get('id')
        if type(eid)!=int or eid<1:raise ValueError('Invalid event ID')
        cur=conn.execute("UPDATE events SET state='acknowledged', actor=?, ack_at=? WHERE id=? AND state='open'",(who,iso(datetime.now(UTC)),eid))
        if cur.rowcount==0:return '404 Not Found',{'error':'Open event not found'}
        audit(conn,who,'Alarm acknowledged',str(eid));conn.commit()
        return '200 OK',{'acknowledged':eid}
    return '404 Not Found',{'error':'Not found'}

def application(env,start_response):
    headers=[('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','no-referrer'),
             ('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'")]
    def send(status,body,kind='application/json; charset=utf-8',extra=()):
        if not isinstance(body,bytes):body=json.dumps(body,allow_nan=False).encode()
        start_response(status,headers+[('Content-Type',kind),('Content-Length',str(len(body)))]+list(extra))
        return [body]
    who=who_for(env)
    if who==LOCKED:
        return send('429 Too Many Requests',b'<!doctype html><meta charset="utf-8"><title>Sign-in locked</title>'
                    b'<p>Too many wrong passwords. Sign-in is locked for up to 15 minutes. Close this tab and try again later.</p>',
                    'text/html; charset=utf-8',[('Retry-After','900')])
    role,user=who if who else (None,'')
    if env.get('PATH_INFO')=='/switch':
        # Basic Auth has no logout. Refuse the login the user is switching FROM so the browser
        # shows its sign-in box; once a different valid login arrives, return to the dashboard.
        old=parse_qs(env.get('QUERY_STRING','')).get('from',[''])[0]
        if role and user!=old:return send('302 Found',b'',extra=[('Location','/')])
        page=(b'<!doctype html><meta charset="utf-8"><title>Switch user</title>'
              b'<p>Sign in with a different account to switch user.</p><p><a href="/">Back to dashboard</a></p>')
        return send('401 Unauthorized',page,'text/html; charset=utf-8',[('WWW-Authenticate','Basic realm="Wastewater Dashboard", charset="UTF-8"')])
    if not role:return send('401 Unauthorized',{'error':'Sign in required'},extra=[('WWW-Authenticate','Basic realm="Wastewater Dashboard", charset="UTF-8"')])
    method=env.get('REQUEST_METHOD','GET');path=env.get('PATH_INFO','/')
    try:
        if method=='GET':
            if path=='/api/summary':
                d=summary();d['role']=role;d['user']=user;d['permissions']=sorted(ROLE_PERMISSIONS[role])
                with appdb() as conn:
                    cfg=settings(conn);d['limits']={'low':cfg['ph_low'],'high':cfg['ph_high'],'enabled':cfg['alarms_enabled'],'confirm_samples':cfg['alarm_confirm_samples'],
                                                    'stale_minutes':cfg['stale_alert_minutes']}
                    d['active_alarms']=[dict(r) for r in conn.execute("SELECT id,stamp,kind,detail,state FROM events WHERE state IN ('open','acknowledged') ORDER BY id DESC LIMIT 10")]
                return send('200 OK',d)
            if path in ('/api/history','/api/export'):
                if not can(role,'export' if path.endswith('export') else 'view'):return send('403 Forbidden',{'error':'Not permitted'})
                query=parse_qs(env.get('QUERY_STRING',''))
                if path=='/api/export' and 'tag' in query:
                    fname,data=report_xlsx(query)
                    return send('200 OK',data,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',[('Content-Disposition',f'attachment; filename="{fname}"')])
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
                status,body=handle_post(path,env,role,conn,user)
                return send(status,body)
        return send('405 Method Not Allowed',{'error':'Unsupported method'},extra=[('Allow','GET, POST')])
    except AccountError as e:return send('400 Bad Request',{'error':str(e)})
    except (ValueError,KeyError,json.JSONDecodeError):return send('400 Bad Request',{'error':'Invalid request data'})
    except (OSError,sqlite3.Error):return send('503 Service Unavailable',{'error':'Storage temporarily unavailable'})

# Run via separate systemd timer, never in Gunicorn workers. Email disabled by default.
XLSX_MIME=('application','vnd.openxmlformats-officedocument.spreadsheetml.sheet')
def send_mail(config,subject,body,recipients,attachments=()):
    """attachments: (filename, bytes) Excel files."""
    msg=EmailMessage();msg['Subject']=subject;msg['From']=config['sender'];msg['To']=', '.join(recipients);msg.set_content(body)
    for fname,data in attachments:msg.add_attachment(data,maintype=XLSX_MIME[0],subtype=XLSX_MIME[1],filename=fname)
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


def update_condition(conn,key,enabled,abnormal,stamp,kind,detail,recovered_kind,recovered_detail,confirm=1,recover=1):
    """Generic incident tracker for the data-stopped, high-level and no-flow alarms.

    abnormal: True/False for a fresh evaluation, None when there is nothing to judge.
    Each stamp counts once; an incident opens after `confirm` abnormal evaluations in a row
    and closes after `recover` normal ones. State is kept as JSON in alarm_state[key].
    """
    row=conn.execute('SELECT state FROM alarm_state WHERE key=?',(key,)).fetchone()
    try:st=json.loads(row['state']) if row else {}
    except ValueError:st={}
    if not isinstance(st,dict):st={}
    now=iso(datetime.now(UTC))
    if not enabled:
        if st.get('active'):
            conn.execute("UPDATE events SET state='closed' WHERE id=? AND state IN ('open','acknowledged')",(st['active'],))
            audit(conn,'system',f'{kind} monitoring turned off',f'incident {st["active"]} closed')
        st={}
    elif abnormal is not None and stamp and stamp!=st.get('last'):
        st['last']=stamp
        if not st.get('active'):
            st['cand']=st.get('cand',0)+1 if abnormal else 0
            st['rec']=0
            if st['cand']>=confirm:
                cur=conn.execute('INSERT INTO events(stamp,kind,detail,state) VALUES (?,?,?,?)',(now,kind,detail,'open'))
                st['active']=cur.lastrowid;st['cand']=0
                audit(conn,'system',f'Confirmed {kind}',f'incident {st["active"]}')
        elif abnormal:
            st['rec']=0
        else:
            st['rec']=st.get('rec',0)+1
            if st['rec']>=recover:
                eid=st['active']
                conn.execute("UPDATE events SET state='closed' WHERE id=? AND state IN ('open','acknowledged')",(eid,))
                cur=conn.execute('INSERT INTO events(stamp,kind,detail,state) VALUES (?,?,?,?)',
                                 (now,recovered_kind,f'Incident {eid}; {recovered_detail}','closed'))
                st['pending']=[eid,cur.lastrowid];st['active']=None;st['rec']=0
                audit(conn,'system',f'{recovered_kind} confirmed',f'incident {eid}')
    conn.execute('INSERT OR REPLACE INTO alarm_state(key,state) VALUES (?,?)',(key,json.dumps(st)))
    conn.commit()
    return st

def flow_window(minutes):
    """(max flow L/min, latest stamp) over the last `minutes`, or (None, None) when the
    window is not covered by at least two readings."""
    now=datetime.now(UTC);start=now-timedelta(minutes=minutes)
    db=database()
    try:
        rows=db.execute("SELECT stamp,value FROM samples WHERE name='FLOW_RATE' AND stamp>=? AND stamp<=? AND value IS NOT NULL ORDER BY stamp",
                        (iso(start),iso(now))).fetchall()
    finally:db.close()
    if len(rows)<2 or parse(rows[0]['stamp'])>start+timedelta(minutes=minutes*0.2):return None,None
    return max(r['value'] for r in rows)*TAGS['FLOW_RATE'][2],rows[-1]['stamp']

def check_conditions(conn,cfg,readings,now):
    """Data stopped, EQ tank high level and no discharge flow. Returns {key: state}."""
    tag=lambda n:next((r for r in readings if r['name']==n),None) or {}
    out={}
    ages=[r['age_seconds'] for r in readings if r.get('age_seconds') is not None]
    age=min(ages) if ages else None
    limit=cfg['stale_alert_minutes']
    out['data']=update_condition(conn,'data',cfg['stale_alert_enabled'],
        None if age is None else age>limit*60, now.strftime('%Y-%m-%dT%H:%M'),
        'Data stopped',f'No new reading from any tag for {0 if age is None else age//60} minutes (limit {limit} min). Check the Ewon, Talk2M and the collector.',
        'Data restored','new readings are arriving again')
    lv=tag('EQ_Tank_Level_Ft')
    fresh=lv.get('status')=='current' and lv.get('value') is not None
    out['level']=update_condition(conn,'level',cfg['level_alarm_enabled'],
        (lv['value']>cfg['level_high_ft']) if fresh else None, lv.get('stamp'),
        'EQ level high',f"EQ tank level {lv['value']:.2f} ft is above the {cfg['level_high_ft']:.2f} ft limit" if fresh else '',
        'EQ level recovered',f"level {lv['value']:.2f} ft" if fresh else '',
        cfg['alarm_confirm_samples'],cfg['alarm_recovery_samples'])
    peak,stamp=flow_window(cfg['flow_low_minutes']) if cfg['flow_alarm_enabled'] else (None,None)
    out['flow']=update_condition(conn,'flow',cfg['flow_alarm_enabled'],
        None if peak is None else peak<cfg['flow_low_lpm'], stamp,
        'No discharge flow',f"Discharge flow stayed below {cfg['flow_low_lpm']:.2f} L/min for {cfg['flow_low_minutes']} minutes (highest {0 if peak is None else peak:.2f} L/min). Check the pumps.",
        'Discharge flow restored',f"flow up to {0 if peak is None else peak:.2f} L/min")
    return out

CONDITION_SUBJECTS={'data':'Wastewater ALERT: no new data','level':'Wastewater EQ tank HIGH level alarm','flow':'Wastewater NO DISCHARGE FLOW alarm'}

def period_report(start,end,data_tags):
    """Excel report for Vancouver dates start..end inclusive, for email attachments."""
    return report_xlsx({'tag':['all'],'startDate':[start.isoformat()],'endDate':[end.isoformat()]},data_tags)

def scheduler():
    now=datetime.now(ZoneInfo('America/Vancouver'))
    with appdb() as conn:
        cfg=settings(conn)
        readings=summary()['tags']
        ph=next((r for r in readings if r['name']=='pH_Reading'),None)
        update_ph_incidents(conn,cfg,ph)
        conditions=check_conditions(conn,cfg,readings,now)
        if not EMAIL_CFG.is_file() or not cfg['recipients']:
            return
        mail=json.loads(EMAIL_CFG.read_text())
        if not all(mail.get(k) for k in ('host','sender','username','password')):
            return
        hhmm=now.strftime('%H:%M');today=now.strftime('%Y-%m-%d');weekday=now.weekday()
        on=time.fromisoformat(cfg['alarm_on']);off=time.fromisoformat(cfg['alarm_off']);moment=now.time()
        within=(on<=moment<=off) if on<=off else (moment>=on or moment<=off)
        notify=cfg['email_enabled'] and weekday in cfg['alarm_days'] and within
        eligible=notify and cfg['alarms_enabled']
        jobs=[]   # (delivery key, subject, body, attachment builder or None)
        if eligible:
            tracker=conn.execute('SELECT * FROM ph_alarm_v2 WHERE id=1').fetchone()
            if tracker and tracker['active_event'] and tracker['active_kind'] in ('high','low') and ph_status(ph,cfg)==tracker['active_kind']:
                eid=tracker['active_event']
                jobs.append((f'alarm:incident:{eid}',f'Wastewater pH {tracker["active_kind"].upper()} alarm',
                    f'Confirmed pH incident #{eid}\nCurrent reading: {ph["value"]:.2f} pH\nLow: {cfg["ph_low"]:.2f}\nHigh: {cfg["ph_high"]:.2f}\nTimestamp: {now.isoformat()}',None))
            if tracker and tracker['pending_recovery_event'] and cfg['email_recovery_enabled']:
                eid=tracker['recovery_origin_event']; rid=tracker['pending_recovery_event']
                delivered=conn.execute('SELECT 1 FROM delivery WHERE key=?',(f'alarm:incident:{eid}',)).fetchone()
                recovery=conn.execute('SELECT stamp FROM events WHERE id=?',(rid,)).fetchone()
                recent=recovery and (datetime.now(UTC)-parse(recovery['stamp']))<timedelta(hours=24)
                if delivered and recent:
                    jobs.append((f'recovery:incident:{eid}',f'Wastewater pH recovered — incident #{eid}',
                        f'Incident #{eid} recovered after {cfg["alarm_recovery_samples"]} normal readings.\nTimestamp: {now.isoformat()}',None))
        if notify:
            for key,st in conditions.items():
                if st.get('active'):
                    eid=st['active'];ev=conn.execute('SELECT detail FROM events WHERE id=?',(eid,)).fetchone()
                    jobs.append((f'alarm:incident:{eid}',CONDITION_SUBJECTS[key],
                        f'Incident #{eid}\n{ev["detail"] if ev else ""}\nTimestamp: {now.isoformat()}',None))
                if st.get('pending') and cfg['email_recovery_enabled']:
                    eid,rid=st['pending']
                    delivered=conn.execute('SELECT 1 FROM delivery WHERE key=?',(f'alarm:incident:{eid}',)).fetchone()
                    rec=conn.execute('SELECT kind,detail,stamp FROM events WHERE id=?',(rid,)).fetchone()
                    if delivered and rec and datetime.now(UTC)-parse(rec['stamp'])<timedelta(hours=24):
                        jobs.append((f'recovery:incident:{eid}',f'Wastewater {rec["kind"]} — incident #{eid}',
                            f'{rec["detail"]}\nTimestamp: {now.isoformat()}',None))
        if cfg['daily_enabled'] and hhmm==cfg['daily_time']:
            jobs.append((f'daily:{today}','Wastewater Daily Report '+today,historical_report(now,'daily'),None))
        if now.day==cfg['monthly_day'] and hhmm==cfg['monthly_time']:
            first=now.date().replace(day=1)
            if cfg['monthly_enabled']:
                start=(first-timedelta(days=1)).replace(day=1)
                jobs.append((f'monthly:{now:%Y-%m}','Wastewater Monthly Report '+now.strftime('%Y-%m'),
                    historical_report(now,'monthly')+'\n\nAttached: Excel report for the month (Summary and Total_Flow_Li readings).',
                    lambda s=start,e=first-timedelta(days=1):period_report(s,e,(SUMMARY_TAG,))))
            if cfg['quarterly_enabled'] and now.month in (1,4,7,10):
                start=(first-timedelta(days=80)).replace(day=1)
                label=quarter_label(start)
                jobs.append((f'quarterly:{label}','Wastewater Quarterly Report '+label,
                    f'Wastewater Quarterly Report {label}\nPeriod: {start:%m/%d/%Y} through {first-timedelta(days=1):%m/%d/%Y} (America/Vancouver)\n\n'
                    'Attached: Excel Summary (Total_Flow_Li EOD values, usage, monthly and quarterly totals).\n'
                    'Download the full readings from the Reports page if needed.',
                    lambda s=start,e=first-timedelta(days=1):period_report(s,e,())))
        for key,subject,body,build in jobs:
            if conn.execute('SELECT 1 FROM delivery WHERE key=?',(key,)).fetchone():
                continue
            try:
                send_mail(mail,subject,body,cfg['recipients'],[build()] if build else ())
                conn.execute('INSERT INTO delivery(key,stamp,outcome) VALUES (?,?,?)',
                             (key,iso(datetime.now(UTC)),'sent'))
                audit(conn,'system','Email delivered',key)
            except Exception as exc:
                audit(conn,'system','Email failed',key+' '+type(exc).__name__)
                print('Email failed:',type(exc).__name__,file=sys.stderr)
            conn.commit()

BACKUP_DIR=Path(os.environ.get('PBF_BACKUP_DIR','/var/backups/pbf-wastewater-nightly'))
def backup(dest=None,keep=14):
    """Consistent online copies of both SQLite databases into dest/<stamp>/; keeps the newest `keep`."""
    dest=Path(dest or BACKUP_DIR)
    folder=dest/datetime.now(ZoneInfo('America/Vancouver')).strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True,exist_ok=False)
    os.chmod(folder,0o750)
    for src,name in ((DB,'history.sqlite3'),(APP_DB,'dashboard.sqlite3')):
        if not Path(src).exists():continue
        with sqlite3.connect(f'file:{src}?mode=ro',uri=True) as inp, sqlite3.connect(folder/name) as out:inp.backup(out)
        with sqlite3.connect(folder/name) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError(f'Backup failed integrity check: {name}')
        os.chmod(folder/name,0o640)
    old=sorted(p for p in dest.iterdir() if p.is_dir() and len(p.name)==15 and p.name[8]=='-')[:-keep]
    import shutil
    for p in old:shutil.rmtree(p)
    print('Backup written:',folder)
    return folder

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
    elif len(sys.argv)>1 and sys.argv[1]=='--backup':backup(sys.argv[2] if len(sys.argv)>2 else None)
    else: print('Usage: python3 web.py --init|--scheduler|--backup [DIR]|--set-admin USERNAME|--set-operator USERNAME')
