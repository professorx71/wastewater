import base64, csv, hashlib, hmac, io, json, os, re, sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

ROOT=Path(__file__).parent
DB=Path(os.environ.get('PBF_DB','/var/lib/pbf-wastewater/history.sqlite3'))
AUTH=Path(os.environ.get('PBF_WEB_AUTH','/etc/pbf-wastewater/web-auth.json'))
TAGS={'pH_Reading':('EQ pH','pH',1), 'EQ_Tank_Level_Ft':('EQ tank level','ft',1),
      'FLOW_RATE':('Discharge flow','L/min',3.78541), 'Total_Flow_Li':('Total flow','L',1)}
DIAGRAM_TAGS={'Rake_Speed_VFD':('Rake motor speed','Hz'),
              'Coag_Pump_Rate_Percent':('Coagulant pump rate','%'),'Coag_Pump_Run':('Coagulant pump run','ml/min'),
              'Floc_Pump_Rate_Percent':('Flocculant pump rate','%'),'Floc_Pump_Run':('Flocculant pump run','ml/min'),
              'SLUDGE_TANK_LEVEL':('Sludge tank full (PLC B3:0/13, 1 = full)',''),
              'SUMP_PUMP_STATUS':('Electric transfer pump / sump pump running (PLC B3:1/9, 1 = on)',''),
              'Mixer_Pump_Staus':('Electric circulation pump / mixer pump running (PLC N142:0, not 0 = on)',''),
              'SYSTEM_ENABLED':('System enabled (PLC B3:0/4, 1 = on)','')}
# PLC alarm bits (1 = alarm, 0 = good): tag, Alarms-page event name, banner text, PLC address, help for the email.
# Each one is an alarm on the Alarms page, shows on the overview alarm banner and is emailed to the recipients.
# Every alarm is acknowledged automatically when it is recorded (new_alarm_event); it closes when it clears.
PLC_ALARMS=(('SLUDGE_TANK_LEVEL','Sludge tank FULL','SLUDGE TANK FULL · arrange a sludge pickup','B3:0/13','Arrange a sludge pickup.'),
            ('E_STOP_STATUS','E-Stop active','E-STOP ACTIVE','N7:1/0','Check the E-Stop buttons before restarting the system.'),
            ('MIXER_PUMP_FAULTED','Mixer pump faulted','MIXER PUMP FAULTED','N7:1/2','Check the mixer (circulation) pump.'),
            ('RAKE_MOTOR_FAULTED','Rake motor faulted','RAKE MOTOR FAULTED','N7:1/8','Check the DAF rake motor and its VFD.'),
            ('MAX_RGT_PUMP_FAULTED','MAX RGT pump faulted','MAX RGT PUMP FAULTED','N7:1/10','Check the MAX RGT pump.'),
            ('FLOC_COAG_PUMP_FAULTED','Floc / Coag pump faulted','FLOC / COAG PUMP FAULTED','N7:0/14','Check the flocculant and coagulant dosing pumps.'),
            ('EQ_TANK_FULL','EQ tank FULL','EQ TANK FULL','N7:1/5','Check the EQ tank level and the discharge.'))
AUTO_ACK_ON_CLEAR={'SLUDGE_TANK_LEVEL':'system (auto-acknowledged: sludge tank emptied)','EQ_TANK_FULL':'system (auto-acknowledged: EQ tank no longer full)'}
for _tag,_kind,_banner,_addr,_ in PLC_ALARMS:
    DIAGRAM_TAGS.setdefault(_tag,(f'{_kind} (PLC {_addr}, 1 = alarm)',''))
UTC=timezone.utc

def iso(t): return t.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00','Z')
def parse(t):
    d=datetime.fromisoformat(t.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Timezone required')
    return d.astimezone(UTC)
def good_quality(q):
    """Ewon quality: none, 'good' or 'initialGood' (first value after a change or restart) are good."""
    return not q or q.lower() in ('good','initialgood')

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
                if row['value'] is None or not good_quality(row['quality']): item['status']='invalid'
            result.append(item)
        ages=[t['age_seconds'] for t in result if t['age_seconds'] is not None]
        # Diagram-only readings: kept out of 'tags' so cards, banner, trends and reports are unchanged.
        # The Ewon logs on change, so a steady value (e.g. rake stopped at 0 Hz) gets no new samples.
        # Its last good reading still holds while the collector keeps receiving other tags.
        feed_live=bool(ages) and min(ages)<=180
        extras={}
        for name,(label,unit) in DIAGRAM_TAGS.items():
            row=db.execute('SELECT stamp,value,quality FROM samples WHERE name=? AND stamp<=? ORDER BY stamp DESC LIMIT 1',(name,iso(now))).fetchone()
            item={'name':name,'label':label,'unit':unit,'value':None,'stamp':None,'status':'missing'}
            if row:
                age=(now-parse(row['stamp'])).total_seconds()
                item.update(stamp=row['stamp'],value=row['value'],status='current' if age<=180 or feed_live else 'stale')
                if name in DOSING_RUN_TAGS:   # sent as ml/min whatever unit the PLC uses
                    item.update(raw_value=row['value'],raw_unit=raw_unit(name,row['stamp']),value=ml_per_min(name,row['stamp'],row['value']))
                if row['value'] is None or not good_quality(row['quality']): item['status']='invalid'
            extras[name]=item
        return {'revision':'1.2.38','now':iso(now),'timezone':'America/Vancouver','tags':result,'collector':state,
                'data_age_seconds':min(ages) if ages else None,'extras':extras}
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
REPORT_TAGS=('EQ_Tank_Level_Ft','FLOW_RATE','Total_Flow_Li','Rake_Speed_VFD',
             'Coag_Pump_Rate_Percent','Coag_Pump_Run','Floc_Pump_Rate_Percent','Floc_Pump_Run')
RAW_UNITS={'pH_Reading':'pH','EQ_Tank_Level_Ft':'ft','FLOW_RATE':'US gal/min','Total_Flow_Li':'L','Rake_Speed_VFD':'Hz',
           'Coag_Pump_Rate_Percent':'%','Coag_Pump_Run':'ml/min','Floc_Pump_Rate_Percent':'%','Floc_Pump_Run':'ml/min',
           'SLUDGE_TANK_LEVEL':'1 = full'}
# Unit changes made in the PLC/Ewon: (UTC time from which the new unit applies, unit), oldest first.
# Coag_Pump_Run (F192:0) changed from ml/min to ml/h on 11/19/2026 at 11:01 Vancouver time.
UNIT_CHANGES={'Coag_Pump_Run':(('2026-11-19T18:01:00.000000Z','ml/h'),)}
DOSING_RUN_TAGS=('Coag_Pump_Run','Floc_Pump_Run')
_PER_MIN={'ml/min':1.0,'ml/h':1/60}
def raw_unit(name,stamp):
    """Unit the Ewon used for this tag at this time (stamp: stored ISO UTC text)."""
    unit=RAW_UNITS[name]
    for since,u in UNIT_CHANGES.get(name,()):
        if stamp>=since:unit=u
    return unit
def ml_per_min(name,stamp,value):
    return None if value is None else value*_PER_MIN[raw_unit(name,stamp)]
_XL_EPOCH=datetime(1899,12,30)
# Cell styles: 0 normal, 1 date-time, 2 two decimals, 3 bold, 4 date, 5 three decimals
_XL_STYLES=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="3"><numFmt numFmtId="164" formatCode="mm/dd/yyyy hh:mm:ss"/><numFmt numFmtId="165" formatCode="mm/dd/yyyy"/><numFmt numFmtId="166" formatCode="0.000"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
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


CHEMICAL_TAGS=(('Coag_Pump_Run','Coagulant'),('Floc_Pump_Run','Flocculant'))
def chemical_usage(start,end,zone):
    """Litres used per local day for each dosing pump: the pump's reading (converted to ml/min) is held until the
    next reading (the Ewon logs on change) and multiplied by the time it was held."""
    end=min(end,datetime.now(UTC))
    used={tag:{} for tag,_ in CHEMICAL_TAGS}
    db=database()
    try:
        for tag,_ in CHEMICAL_TAGS:
            prior=db.execute('SELECT stamp,value FROM samples WHERE name=? AND stamp<? ORDER BY stamp DESC LIMIT 1',(tag,iso(start))).fetchone()
            points=[(start,ml_per_min(tag,*prior))] if prior else []
            points+=[(parse(t),ml_per_min(tag,t,v)) for t,v in db.execute('SELECT stamp,value FROM samples WHERE name=? AND stamp>=? AND stamp<? ORDER BY stamp',(tag,iso(start),iso(end)))]
            acc=used[tag]
            for i,(a,v) in enumerate(points):
                b=points[i+1][0] if i+1<len(points) else end
                if v is None or v<=0:continue
                while a<b:
                    day=a.astimezone(zone).date()
                    seg=min(b,datetime.combine(day+timedelta(days=1),time.min,zone).astimezone(UTC))
                    acc[day]=acc.get(day,0.0)+v*(seg-a).total_seconds()/60/1000
                    a=seg
    finally:db.close()
    return used

def chemical_rows(start,end,zone,date_range):
    B=lambda t:(t,3);num=lambda v:(round(v,3),2)
    used=chemical_usage(start,end,zone)
    first=start.astimezone(zone).date();last=(end.astimezone(zone)-timedelta(microseconds=1)).date()
    days=[];d=first
    while d<=last:days.append(d);d+=timedelta(days=1)
    names=[n for _,n in CHEMICAL_TAGS]
    rows=[[B('Chemical usage (litres)')],['Tags','Coag_Pump_Run (ml/h from 11/19/2026 11:01, ml/min before), Floc_Pump_Run (ml/min)'],['Date range (America/Vancouver)',date_range],[]]
    totals=[sum(used[t].values()) for t,_ in CHEMICAL_TAGS]
    rows+=[[B('Total for date range')]+[B(f'{n} (L)') for n in names],['All days']+[num(v) for v in totals],[]]
    groups={}
    for d in days:
        for k in (('Quarter',quarter_label(d)),('Month',f'{d:%m/%Y}')):
            g=groups.setdefault(k,[0.0]*len(CHEMICAL_TAGS))
            for i,(t,_) in enumerate(CHEMICAL_TAGS):g[i]+=used[t].get(d,0.0)
    for kind in ('Quarter','Month'):
        rows.append([B(kind)]+[B(f'{n} (L)') for n in names])
        rows+=[[key]+[num(v) for v in vals] for (k,key),vals in groups.items() if k==kind]
        rows.append([])
    discharged={d:dict(litres=0.0) for d in days}
    db=database()
    try:daily_discharge(db,start,min(end,datetime.now(UTC)),zone,discharged)
    finally:db.close()
    per_m3=lambda chem_l,water_l:(round(chem_l*1000/(water_l/1000),1),2) if water_l>=1 else ''
    rows.append([B('Date')]+[B(f'{n} (L)') for n in names]+[B('Discharged (m³)')]+[B(f'{n} (mL per m³ discharged)') for n in names])
    rows+=[[d]+[num(used[t].get(d,0.0)) for t,_ in CHEMICAL_TAGS]+[(round(discharged[d]['litres']/1000,2),2)]
           +[per_m3(used[t].get(d,0.0),discharged[d]['litres']) for t,_ in CHEMICAL_TAGS] for d in days]
    rows+=[[],['Litres = pump reading in ml/min (ml/h readings divided by 60) x minutes it was held, / 1000. Each reading is held until the next reading, '
               'because the Ewon logs on change; the first day uses the last reading before the range. Readings of 0 or below count as 0. '
               'Days without data show 0. Discharged (m³): increase of the Total_Flow_Li meter that day / 1000. '
               'mL per m³ discharged = chemical used x 1000 / m³ discharged: a rising figure with the same pH means more chemical per volume.']]
    return rows

SLUDGE_TAG='SLUDGE_TANK_LEVEL'
PICKUP_DAYS='Mon & Thu'   # the sludge truck comes every Monday and Thursday

def sludge_full_periods(start,end):
    """FULL periods (bit = 1) of the sludge tank that overlap start..end, as [(full_from, cleared_at or None)].
    A period that began before the range keeps its real start time."""
    db=database()
    try:
        empty=db.execute('SELECT stamp FROM samples WHERE name=? AND stamp<? AND value<1 ORDER BY stamp DESC LIMIT 1',(SLUDGE_TAG,iso(start))).fetchone()
        rows=db.execute('SELECT stamp,value FROM samples WHERE name=? AND stamp>=? AND stamp<? AND value IS NOT NULL ORDER BY stamp',
                        (SLUDGE_TAG,empty[0] if empty else iso(start-timedelta(days=30)),iso(end))).fetchall()
    finally:db.close()
    periods=[];since=None
    for stamp,value in rows:
        if value>=1 and since is None:since=parse(stamp)
        elif value<1 and since is not None:periods.append((since,parse(stamp)));since=None
    if since is not None:periods.append((since,None))
    return [(a,b) for a,b in periods if b is None or b>start]

def pickup_records(conn,start=None,end=None):
    q='SELECT * FROM sludge_pickups';args=()
    if start is not None:q+=' WHERE picked_at>=? AND picked_at<?';args=(iso(start),iso(end))
    return [dict(r) for r in conn.execute(q+' ORDER BY picked_at',args)]

def sludge_analysis(start,end):
    """Pickups in the range matched to the FULL alarm that came before each one."""
    with appdb() as conn:
        pickups=pickup_records(conn,start,end)
        before=conn.execute('SELECT picked_at FROM sludge_pickups WHERE picked_at<? ORDER BY picked_at DESC LIMIT 1',(iso(start),)).fetchone()
    periods=sludge_full_periods(start,end)
    prev=parse(before[0]) if before else None
    out=[]
    for p in pickups:
        t=parse(p['picked_at'])
        # the FULL alarm this pickup answered: the latest one that started before it and after the previous pickup
        full=max((a for a,_ in periods if a<=t and (prev is None or a>prev)),default=None)
        if full is None and prev is None:full=max((a for a,_ in sludge_full_periods(t-timedelta(days=30),t) if a<=t),default=None)
        out.append(dict(p,at=t,full_since=full,wait_h=(t-full).total_seconds()/3600 if full else None,
                        since_prev_d=(t-prev).total_seconds()/86400 if prev else None))
        prev=t
    alarms=[]
    for a,b in periods:
        if a>=end:continue
        picked=next((x['at'] for x in out if x['at']>=a),None)
        alarms.append(dict(full_from=a,cleared=b,picked=picked))
    return out,alarms

def sludge_rows(start,end,zone,date_range):
    B=lambda t:(t,3);num=lambda v:'' if v is None else (round(v,2),2)
    L=lambda t:t.astimezone(zone) if t else ''
    pickups,alarms=sludge_analysis(start,end)
    waits=[p['wait_h'] for p in pickups if p['wait_h'] is not None]
    gaps=[p['since_prev_d'] for p in pickups if p['since_prev_d'] is not None]
    rows=[[B('Sludge pickups and sludge tank FULL alarms')],['Tag',f'{SLUDGE_TAG} (PLC B3:0/13, 1 = full)'],
          ['Date range (America/Vancouver)',date_range],[],
          [B('Summary'),B('Value')],['Pickups logged',len(pickups)],['Sludge tank FULL alarms',len(alarms)],
          ['Average hours from FULL to pickup',num(sum(waits)/len(waits) if waits else None)],
          ['Longest hours from FULL to pickup',num(max(waits) if waits else None)],
          ['Average days between pickups',num(sum(gaps)/len(gaps) if gaps else None)],
          ['FULL alarms not picked up yet',sum(1 for a in alarms if a['picked'] is None)],[],
          [B('Pickup (America/Vancouver)'),B('Tank FULL since'),B('Hours FULL to pickup'),B('Days since previous pickup'),B('Logged by'),B('Note')]]
    rows+=[[L(p['at']),L(p['full_since']),num(p['wait_h']),num(p['since_prev_d']),p['logged_by'],p['note']] for p in pickups]
    if not pickups:rows.append(['No pickups logged in this date range.'])
    rows+=[[],[B('Tank FULL from'),B('FULL cleared (bit back to 0)'),B('Hours FULL'),B('Picked up at'),B('Hours FULL to pickup')]]
    for a in alarms:
        upto=a['cleared'] or min(end,datetime.now(UTC))
        rows.append([L(a['full_from']),L(a['cleared']) or 'Still FULL',num((upto-a['full_from']).total_seconds()/3600),
                     L(a['picked']) or 'Not picked up yet',num((a['picked']-a['full_from']).total_seconds()/3600 if a['picked'] else None)])
    if not alarms:rows.append(['No sludge tank FULL alarms in this date range.'])
    rows+=[[],['Pickups are logged by operators on the Reports page (date and time of the pickup). Tank FULL since is the start of the latest '
               'FULL alarm before the pickup and after the previous pickup; blank if the tank was not reported FULL first. '
               'FULL alarms come from the PLC level switch SLUDGE_TANK_LEVEL (B3:0/13) via the Ewon.']]
    return rows

def report_xlsx(params,data_tags=None):
    """Excel report for one tag or 'all'. Returns (filename, bytes).
    Summary: Total_Flow_Li end-of-day (last valid) reading of each day, the usage since the
    previous EOD, and monthly and quarterly totals. data_tags overrides which tag worksheets
    are included (emails use fewer to keep attachments small)."""
    choice=params.get('tag',['all'])[0]
    if choice=='consumption':
        with appdb() as c:cfg=settings(c)
        try:year=int(params.get('year',[params.get('startDate',[''])[0][:4]])[0])
        except ValueError:year=0
        if not 2020<=year<=datetime.now(UTC).year+1:raise ValueError('Choose a year.')
        rows=consumption_rows(year,cfg)
        return f'wastewater-chemical-consumption-{year}.xlsx',xlsx([('Chemical Consumption',rows,(16,40,18,22,22,22,18,18,18))])
    if choice=='purchases':
        with appdb() as c:cfg=settings(c)
        try:year=int(params.get('year',[params.get('startDate',[''])[0][:4]])[0])
        except ValueError:year=0
        if not 2020<=year<=datetime.now(UTC).year+1:raise ValueError('Choose a year.')
        rows=purchase_rows(year,cfg)
        return f'wastewater-chemical-purchases-{year}.xlsx',xlsx([('Chemical Purchases',rows,(14,26,14,40,18,12,12,18,16,16,30,22))])
    if choice in ('alarms','compliance','pumps'):
        start,end=window(params,REPORT_MAX_DAYS);zone=ZoneInfo('America/Vancouver')
        a=start.astimezone(zone).date();b=(end.astimezone(zone)-timedelta(microseconds=1)).date();rng=f'{a:%m/%d/%Y} to {b:%m/%d/%Y}'
        with appdb() as c:cfg=settings(c)
        name,rows,widths={'alarms':('Alarm History',lambda:alarm_rows(start,end,zone,rng),(22,30,60,22,14,30,22,14,14)),
                          'compliance':('Discharge Compliance',lambda:compliance_rows(start,end,zone,rng,cfg),(16,12,10,10,10,16,18,22,16,12)),
                          'pumps':('Pump Run Hours',lambda:pump_rows(start,end,zone,rng),(34,22,14,14,18))}[choice]
        rows=rows();stem=f'wastewater-{choice}-{a:%Y-%m-%d}-to-{b:%Y-%m-%d}'
        return stem+'.xlsx',xlsx([(name,rows,widths)])
    if choice=='calibration':
        stamp=datetime.now(ZoneInfo('America/Vancouver'))
        return f'wastewater-sensor-calibrations-{stamp:%Y-%m-%d}.xlsx',xlsx([('Calibrations',calibration_rows(),(34,18,22,22,30,40,24,26))])
    if choice not in ('all','chemical','sludge') and choice not in REPORT_TAGS:raise ValueError('Choose a report type from the list.')
    names=(tuple(data_tags) if data_tags is not None else REPORT_TAGS if choice=='all'
           else tuple(t for t,_ in CHEMICAL_TAGS) if choice=='chemical' else (SLUDGE_TAG,) if choice=='sludge' else (choice,))
    start,end=window(params,REPORT_MAX_DAYS)
    zone=ZoneInfo('America/Vancouver')
    B=lambda t:(t,3)
    local_start=start.astimezone(zone).date()
    local_end=(end.astimezone(zone)-timedelta(microseconds=1)).date()
    date_range=f'{local_start:%m/%d/%Y} to {local_end:%m/%d/%Y}'
    query='SELECT stamp,value FROM samples WHERE name=? AND stamp>=? AND stamp<? ORDER BY stamp'

    def readings(name):  # tag worksheet, streamed straight from the database
        yield [B('Timestamp UTC'),B('Timestamp America/Vancouver'),B('Raw Value'),B('Unit Of Measure')]
        db=database()
        try:
            for stamp,value in db.execute(query,(name,iso(start),iso(end))):
                t=parse(stamp);yield [t,t.astimezone(zone),value,raw_unit(name,stamp)]
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
        ph={}   # local date -> [sum, count] of the pH readings that day
        for stamp,value in db.execute(query,('pH_Reading',iso(start),iso(end))):
            if value is None:continue
            x=ph.setdefault(parse(stamp).astimezone(zone).date(),[0.0,0]);x[0]+=value;x[1]+=1
    finally:db.close()
    def ph_avg(days):
        tot=sum(ph[d][0] for d in days if d in ph);n=sum(ph[d][1] for d in days if d in ph)
        return tot/n if n else None
    # The Total_Flow_Li meter resets every day, so the EOD reading is that day's usage.
    last=(parse(prior[0]).astimezone(zone),prior[1]) if prior else None
    daily=[]   # (date, EOD usage, reading time, note)
    day=local_start
    while day<=local_end:
        if day in valid:last=valid[day]
        if last:
            note=f'No reading above 1 L; carried forward from {last[0]:%m/%d/%Y %H:%M}' if last[0].date()!=day else ''
            daily.append((day,truncate2(last[1]),last[0],note))
        day+=timedelta(days=1)
    months={};quarters={}
    for day,v,_,_ in daily:months[f'{day:%Y-%m}']=months.get(f'{day:%Y-%m}',Decimal(0))+v
    for m,v in months.items():
        q=quarter_label(date.fromisoformat(m+'-01'));quarters[q]=quarters.get(q,Decimal(0))+v
    num=lambda v:'' if v is None else (float(v),2)
    m3=lambda v:'' if v is None else (float(v)/1000,5)          # litres -> cubic metres, 3 decimals
    pH=lambda v:'' if v is None else (round(v,2),2)
    in_month=lambda m:[d for d,*_ in daily if f'{d:%Y-%m}'==m]
    in_quarter=lambda q:[d for d,*_ in daily if quarter_label(d)==q]
    summary_rows=[[B('Wastewater report summary')],
                  ['Tag',tag],['Unit Of Measure','L (litres) and m³ (cubic metres)'],['Date range (America/Vancouver)',date_range],[]]
    if not daily:
        summary_rows.append(['No valid Total_Flow_Li readings in or before this date range.'])
    else:
        summary_rows.append([B('Quarter'),B('Total Quarterly Usage (L)'),B('Total Quarterly Usage (m³)'),B('pH Average')])
        summary_rows+=[[q,num(v),m3(v),pH(ph_avg(in_quarter(q)))] for q,v in sorted(quarters.items())]
        summary_rows+=[[],[B('Month'),B('Total Monthly Usage (L)'),B('Total Monthly Usage (m³)'),B('pH Average')]]
        summary_rows+=[[f'{m[5:]}/{m[:4]}',num(v),m3(v),pH(ph_avg(in_month(m)))] for m,v in sorted(months.items())]
        summary_rows+=[[],[B('Date'),B('EOD Daily Usage (L)'),B('EOD Daily Usage (m³)'),B('pH Average'),B('Reading Used (America/Vancouver)'),B('Notes')]]
        summary_rows+=[[day,num(v),m3(v),pH(ph_avg([day])),when.replace(tzinfo=None),note] for day,v,when,note in daily]
    summary_rows+=[[],['EOD Daily Usage is the last Total_Flow_Li reading above 1 L on that day (America/Vancouver), cut to 2 decimals; '
                       'the meter resets every day, so this is the litres used that day. '
                       'If a day has no reading above 1 L, the last valid reading before it is used (see Reading Used and Notes). '
                       'Monthly total = sum of the EOD daily usage in the month; quarterly total = sum of those monthly totals. '
                       'Only days inside the selected date range are counted. m³ = litres / 1000. '
                       'pH Average = average of all EQ pH readings that day (month or quarter: all readings in it); blank when there were none.']]
    chem=[('Chemical Usage',chemical_rows(start,end,zone,date_range),(34,22,22))]
    sheets=(chem if choice=='chemical' else [('Sludge Pickups',sludge_rows(start,end,zone,date_range),(30,26,22,24,22,40))] if choice=='sludge'
            else [('Summary',summary_rows,(34,24,26,12,30,60))]+(chem if choice=='all' and data_tags is None else []))
    sheets+=[(n,readings(n),(22,30,14,18)) for n in names]
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
          'quarterly_enabled':False,
          # PLC alarms: sludge tank FULL, E-Stop and pump/motor faults (PLC_ALARMS).
          'plc_alarms_enabled':True,
          # Sensor calibration intervals (days) and reminder lead time.
          'cal_reminders_enabled':True,'cal_ph_days':30,'cal_level_days':365,'cal_remind_days':7,
          # Chemical stock: alarm when the estimated litres fall below these (0 = off).
          'stock_alarm_enabled':True,'stock_low_coag_l':20.0,'stock_low_floc_l':5.0,'stock_low_caustic_l':20.0,'stock_low_sa93_l':5.0,
          # Price per container for each chemical product (None = not set yet).
          'chemical_prices':{'caustic':None,'floc':None,'coag':None,'sa93':None},
          # Text messages for new alarms, sent by email to carrier email-to-SMS addresses.
          'sms_enabled':False,'sms_recipients':[],
          # Predictive pH: warn when pH, inside the limits, is heading for one (trend over window, reaching it within lookahead).
          'ph_trend_enabled':True,'ph_trend_window_min':30,'ph_trend_lookahead_min':60,
          # Dosing pump stopped (0 ml/min) for this many minutes while wastewater is being discharged.
          'dosing_alarm_enabled':False,'dosing_alarm_minutes':15}
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
    CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, role TEXT NOT NULL, salt TEXT NOT NULL,
        iterations INTEGER NOT NULL, hash TEXT NOT NULL, updated TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sludge_pickups (id INTEGER PRIMARY KEY AUTOINCREMENT, picked_at TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '', logged_by TEXT NOT NULL, logged_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS calibrations (id INTEGER PRIMARY KEY AUTOINCREMENT, sensor TEXT NOT NULL, calibrated_on TEXT NOT NULL,
        calibrated_by TEXT NOT NULL, certificate_no TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
        file_name TEXT, file_type TEXT, file_data BLOB, logged_by TEXT NOT NULL, logged_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS chemical_stock (id INTEGER PRIMARY KEY AUTOINCREMENT, chemical TEXT NOT NULL, kind TEXT NOT NULL,
        litres REAL NOT NULL, at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', logged_by TEXT NOT NULL, logged_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS chemical_purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, chemical TEXT NOT NULL, purchased_on TEXT NOT NULL,
        received_at TEXT, litres REAL NOT NULL, cost REAL, po TEXT NOT NULL DEFAULT '', supplier TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', logged_by TEXT NOT NULL, logged_at TEXT NOT NULL, received_by TEXT);
    CREATE TABLE IF NOT EXISTS shelves (key TEXT PRIMARY KEY, until TEXT NOT NULL, reason TEXT NOT NULL, shelved_by TEXT NOT NULL, shelved_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS login_failures (client TEXT NOT NULL, attempt TEXT NOT NULL, stamp TEXT NOT NULL,
        PRIMARY KEY (client, attempt));''')
    # cleared_at: when a PLC alarm's condition went away (it may still wait for acknowledgement).
    if not any(r[1]=='cleared_at' for r in conn.execute('PRAGMA table_info(events)')):
        try:conn.execute('ALTER TABLE events ADD COLUMN cleared_at TEXT')
        except sqlite3.OperationalError:pass   # added by another process meanwhile
    migrate_alarms(conn)
    conn.commit()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def migrate_alarms(conn):
    """Rev 1.2.38: all alarms are auto-acknowledged and their messages carry no PLC address or tag name."""
    conn.execute("UPDATE events SET state=CASE WHEN cleared_at IS NULL THEN 'acknowledged' ELSE 'closed' END,"
                 "actor=COALESCE(actor,?),ack_at=COALESCE(ack_at,stamp) WHERE state='open'",(AUTO_ACK_ACTOR,))
    if conn.execute("SELECT 1 FROM settings WHERE key='alarm_text_v2'").fetchone():return
    for eid,detail in conn.execute("SELECT id,detail FROM events WHERE detail LIKE '%(PLC %, tag %' OR detail LIKE '% back to 0'").fetchall():
        new=re.sub(r' \(PLC [^,()]+, tag [A-Za-z0-9_]+ = 1\)','',detail)
        new=re.sub(r'(Incident \d+; )[A-Za-z0-9_]+ back to 0$',r'\1back to normal',new)
        if new!=detail:conn.execute('UPDATE events SET detail=? WHERE id=?',(new,eid))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('alarm_text_v2','true')")

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
    if conn.execute('SELECT COUNT(*) FROM users WHERE lower(username)=lower(?)',(username,)).fetchone()[0]:
        raise AccountError('That username is already used by an Engineering or Guest login')
    exists=conn.execute('SELECT 1 FROM operators WHERE username=?',(username,)).fetchone()
    if not exists and conn.execute('SELECT COUNT(*) FROM operators').fetchone()[0]>=50:raise AccountError('Maximum 50 Operator logins')
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,PBKDF2_ITERATIONS).hex()
    conn.execute('INSERT OR REPLACE INTO operators(username,salt,iterations,hash,updated) VALUES (?,?,?,?,?)',
                 (username,salt.hex(),PBKDF2_ITERATIONS,digest,iso(datetime.now(UTC))))

# Engineering and Guest: named logins like Operators, kept in their own table so an older
# revision (rollback) simply ignores them instead of treating them as Operators.
USER_ROLES=('Engineering','Operator','Guest')   # logins the Admin manages from Settings
def user_records(conn):
    return [dict(username=r['username'],role=r['role'],salt=r['salt'],iterations=int(r['iterations']),hash=r['hash'],source='settings')
            for r in conn.execute('SELECT * FROM users ORDER BY role,username COLLATE NOCASE')]

def save_user(conn,role,username,password):
    if role=='Operator':return save_operator(conn,username,password)
    if role not in USER_ROLES:raise AccountError('Choose Engineering, Operator or Guest')
    check_username(username);check_password(role,password)
    admin=load_account('Admin',conn)
    if admin and admin['username'].lower()==username.lower():raise AccountError('That username is the Admin login')
    if any(r['username'].lower()==username.lower() for r in operator_records(conn)):raise AccountError('That username is already an Operator login')
    other=conn.execute('SELECT role FROM users WHERE lower(username)=lower(?) AND NOT (username=? AND role=?)',(username,username,role)).fetchone()
    if other:raise AccountError(f'That username is already a {other["role"]} login')
    exists=conn.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone()
    if not exists and conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]>=50:raise AccountError('Maximum 50 Engineering and Guest logins')
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,PBKDF2_ITERATIONS).hex()
    conn.execute('INSERT OR REPLACE INTO users(username,role,salt,iterations,hash,updated) VALUES (?,?,?,?,?,?)',
                 (username,role,salt.hex(),PBKDF2_ITERATIONS,digest,iso(datetime.now(UTC))))

def remove_user(conn,role,username):
    if role=='Operator':return remove_operator(conn,username)
    return conn.execute('DELETE FROM users WHERE username=? AND role=?',(username,role)).rowcount

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
                else:
                    for rec in user_records(conn):
                        if hmac.compare_digest(user,rec['username']) and password_ok(rec,pwd):found=(rec['role'],user);break
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

MIN_PASSWORD_LENGTH={'Admin':6,'Engineering':6,'Operator':4,'Guest':4}
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
    if any(r['username'].lower()==username.lower() for r in operator_records(conn)+user_records(conn)):
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
            'operators':[{'username':r['username'],'source':r['source']} for r in ops],
            'users':[{'username':r['username'],'role':'Operator','source':r['source']} for r in ops]
                    +[{'username':r['username'],'role':r['role'],'source':r['source']} for r in user_records(conn)],
            'permissions':{r:sorted(p) for r,p in role_permissions(conn).items()},
            'permission_choices':list(PERMISSION_CHOICES)}

def actor(role,username):return f'{username} ({role})'

# Role permissions, enforced server-side for every endpoint. Admin always has everything,
# including managing logins and these rights. For Engineering, Operator and Guest the Admin
# chooses the rights in Settings; every role can always view readings, trends and alarms.
ADMIN_PERMISSIONS=frozenset({'view','export','acknowledge','pickup','settings','audit','accounts'})
PERMISSION_CHOICES=('export','acknowledge','pickup','settings','audit')
DEFAULT_ROLE_PERMISSIONS={'Engineering':('export','acknowledge','pickup','settings','audit'),
                          'Operator':('export','acknowledge','pickup'),'Guest':()}
# Rights saved before a choice existed get that choice's default (saves record which choices they knew).
_FIRST_CHOICES=('export','acknowledge','settings','audit')
def role_permissions(conn=None):
    saved={}
    try:
        if conn is None:
            with appdb() as c:return role_permissions(c)
        row=conn.execute("SELECT value FROM settings WHERE key='role_permissions'").fetchone()
        if row:saved=json.loads(row['value'])
    except (OSError,sqlite3.Error,ValueError):saved={}
    if not isinstance(saved,dict):saved={}
    out={'Admin':ADMIN_PERMISSIONS}
    known=saved.get('_choices',_FIRST_CHOICES) if role_saved(saved) else ()
    for role in USER_ROLES:
        chosen=saved.get(role,DEFAULT_ROLE_PERMISSIONS[role])
        if not isinstance(chosen,list):chosen=DEFAULT_ROLE_PERMISSIONS[role]
        chosen=set(chosen)|{p for p in DEFAULT_ROLE_PERMISSIONS[role] if p not in known and role in saved}
        out[role]=frozenset({'view'}|{p for p in chosen if p in PERMISSION_CHOICES})
    return out
def role_saved(saved):return any(r in saved for r in USER_ROLES)
def can(role,permission): return permission in role_permissions().get(role,())

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
                'stale_alert_enabled','level_alarm_enabled','flow_alarm_enabled','quarterly_enabled','plc_alarms_enabled',
                'cal_reminders_enabled','stock_alarm_enabled','sms_enabled','ph_trend_enabled','dosing_alarm_enabled'):
        if type(new[key]) is not bool:raise ValueError('Invalid switch')
    if type(new['stale_alert_minutes']) is not int or not 5<=new['stale_alert_minutes']<=1440:raise ValueError('Data-stopped alert: 5–1440 minutes')
    if type(new['level_high_ft']) not in (int,float) or not 0<new['level_high_ft']<=100:raise ValueError('High level: 0–100 ft')
    if type(new['flow_low_lpm']) not in (int,float) or not 0<new['flow_low_lpm']<=10000:raise ValueError('Low flow: 0–10000 L/min')
    if type(new['flow_low_minutes']) is not int or not 5<=new['flow_low_minutes']<=1440:raise ValueError('Low flow duration: 5–1440 minutes')
    if type(new['ph_trend_window_min']) is not int or not 10<=new['ph_trend_window_min']<=240:raise ValueError('pH trend: look back 10–240 minutes')
    if type(new['ph_trend_lookahead_min']) is not int or not 10<=new['ph_trend_lookahead_min']<=480:raise ValueError('pH trend: warn 10–480 minutes ahead')
    if type(new['dosing_alarm_minutes']) is not int or not 5<=new['dosing_alarm_minutes']<=240:raise ValueError('Dosing pump stopped: 5–240 minutes')
    if not isinstance(new['alarm_days'],list) or any(type(d)!=int or d not in range(7) for d in new['alarm_days']):raise ValueError('Invalid days')
    for k in ('alarm_on','alarm_off','daily_time','monthly_time'):
        if not isinstance(new[k],str):raise ValueError('Invalid time')
        time.fromisoformat(new[k])
        if len(new[k])!=5:raise ValueError('Expected HH:MM')
    if type(new['monthly_day']) is not int or not 1<=new['monthly_day']<=28:raise ValueError('Choose day 1–28')
    for key in ('alarm_confirm_samples','alarm_recovery_samples'):
        if type(new[key]) is not int or not 1<=new[key]<=10:raise ValueError('Alarm confirmation must be 1–10 consecutive readings')
    for key,hi in (('cal_ph_days',3650),('cal_level_days',3650),('cal_remind_days',90)):
        if type(new[key]) is not int or not 1<=new[key]<=hi:raise ValueError(f'Calibration days: 1–{hi}')
    prices=new['chemical_prices']
    if not isinstance(prices,dict) or set(prices)-{'caustic','floc','coag','sa93'}:raise ValueError('Invalid chemical prices')
    for v in prices.values():
        if v is not None and (type(v) not in (int,float) or not 0<=v<=1000000):raise ValueError('Chemical price: 0–1000000 per container')
    new['chemical_prices']={k:prices.get(k) for k in ('caustic','floc','coag','sa93')}
    for key in ('stock_low_coag_l','stock_low_floc_l','stock_low_caustic_l','stock_low_sa93_l'):
        if type(new[key]) not in (int,float) or not 0<=new[key]<=100000:raise ValueError('Low stock: 0–100000 L')
    if not isinstance(new['recipients'],list) or len(new['recipients'])>10:raise ValueError('Too many recipients')
    if not isinstance(new['sms_recipients'],list) or len(new['sms_recipients'])>10:raise ValueError('Too many text message addresses')
    for address in new['recipients']+new['sms_recipients']:
        if not isinstance(address,str) or len(address)>254 or '@' not in address or '\n' in address or '\r' in address:raise ValueError('Invalid email address')
    return new

def records(conn,table,limit=200):
    return [dict(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY id DESC LIMIT ?', (min(limit,500),))]

def save_pickup(conn,when,note,who):
    """Log a sludge pickup. when = 'YYYY-MM-DDTHH:MM' in Vancouver time (from the form)."""
    if not isinstance(when,str) or len(when)!=16:raise AccountError('Enter the pickup date and time')
    try:local=datetime.strptime(when,'%Y-%m-%dT%H:%M').replace(tzinfo=ZoneInfo('America/Vancouver'))
    except ValueError:raise AccountError('Enter the pickup date and time')
    t=local.astimezone(UTC);now=datetime.now(UTC)
    if t>now+timedelta(minutes=10):raise AccountError('The pickup time is in the future')
    if t<now-timedelta(days=366):raise AccountError('The pickup time is more than a year ago')
    if not isinstance(note,str) or len(note.strip())>200:raise AccountError('Note: up to 200 characters')
    note=' '.join(note.split())
    if conn.execute('SELECT 1 FROM sludge_pickups WHERE picked_at=?',(iso(t),)).fetchone():raise AccountError('That pickup is already logged')
    conn.execute('INSERT INTO sludge_pickups(picked_at,note,logged_by,logged_at) VALUES (?,?,?,?)',(iso(t),note,who,iso(now)))
    return t,note

# Sensor calibration log (Audit page): date calibrated, who calibrated it, certificate number and an
# optional certificate file. Files are capped so the base64 request stays under nginx's default 1 MB limit.
CAL_SENSORS={'ph':('pH sensor','pH_Reading'),'level':('EQ tank height (level) sensor','EQ_Tank_Level_Ft')}
CAL_FILE_MAX=700_000
CAL_FILE_TYPES=((b'%PDF','application/pdf','.pdf'),(b'\x89PNG\r\n\x1a\n','image/png','.png'),(b'\xff\xd8\xff','image/jpeg','.jpg'))
CAL_BODY_MAX=1_000_000

def _cal_text(v,label,limit,required=False):
    if v is None:v=''
    if not isinstance(v,str):raise AccountError(f'{label}: text required')
    v=' '.join(v.split())
    if required and not v:raise AccountError(f'Enter {label.lower()}')
    if len(v)>limit:raise AccountError(f'{label}: up to {limit} characters')
    return v

def save_calibration(conn,body,who):
    sensor=body.get('sensor')
    if sensor not in CAL_SENSORS:raise AccountError('Choose the sensor')
    try:day=date.fromisoformat(body.get('date') or '')
    except (TypeError,ValueError):raise AccountError('Enter the date calibrated')
    today=datetime.now(ZoneInfo('America/Vancouver')).date()
    if day>today:raise AccountError('The calibration date is in the future')
    if day<date(2000,1,1):raise AccountError('The calibration date is too far in the past')
    name=_cal_text(body.get('name'),'Name of the person who calibrated',100,True)
    cert=_cal_text(body.get('certificate'),'Certificate number',100)
    note=_cal_text(body.get('note'),'Note',300)
    fname=ftype=data=None
    f=body.get('file')
    if f:
        if not isinstance(f,dict) or not isinstance(f.get('data'),str):raise AccountError('Certificate file could not be read')
        try:data=base64.b64decode(f['data'],validate=True)
        except ValueError:raise AccountError('Certificate file could not be read')
        if len(data)>CAL_FILE_MAX:raise AccountError(f'Certificate file is too large (max {CAL_FILE_MAX//1000} KB). Scan or save it at a lower resolution.')
        kind=next((k for k in CAL_FILE_TYPES if data.startswith(k[0])),None)
        if not kind:raise AccountError('Certificate file must be a PDF, JPG or PNG')
        ftype=kind[1]
        stem=''.join(c for c in str(f.get('name') or 'certificate').rsplit('.',1)[0] if c.isalnum() or c in ' ._-')[:80].strip() or 'certificate'
        fname=stem+kind[2]
    if not cert and not data:raise AccountError('Enter the certificate number or attach the certificate')
    cur=conn.execute('INSERT INTO calibrations(sensor,calibrated_on,calibrated_by,certificate_no,note,file_name,file_type,file_data,logged_by,logged_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (sensor,day.isoformat(),name,cert,note,fname,ftype,data,who,iso(datetime.now(UTC))))
    return cur.lastrowid,CAL_SENSORS[sensor][0],day,name,cert

def calibration_records(conn):
    return [dict(r) for r in conn.execute('SELECT id,sensor,calibrated_on,calibrated_by,certificate_no,note,file_name,file_type,logged_by,logged_at,'
                                          'length(file_data) AS file_size FROM calibrations ORDER BY calibrated_on DESC,id DESC')]

def calibration_rows():
    """Excel worksheet rows for the calibration report (all records)."""
    B=lambda t:(t,3)
    with appdb() as conn:recs=calibration_records(conn)
    today=datetime.now(ZoneInfo('America/Vancouver')).date()
    rows=[[B('Sensor calibration report')],['Generated (America/Vancouver)',datetime.now(ZoneInfo('America/Vancouver')).replace(tzinfo=None)],[],
          [B('Sensor'),B('Tag'),B('Last calibrated'),B('Days since'),B('Calibrated by'),B('Certificate no.')]]
    for key,(label,tag) in CAL_SENSORS.items():
        last=next((r for r in recs if r['sensor']==key),None)
        if last:
            d=date.fromisoformat(last['calibrated_on'])
            rows.append([label,tag,d,(today-d).days,last['calibrated_by'],last['certificate_no']])
        else:rows.append([label,tag,'Never logged'])
    rows+=[[],[B('Sensor'),B('Date calibrated'),B('Calibrated by'),B('Certificate no.'),B('Certificate file'),B('Note'),B('Logged by'),B('Logged at (America/Vancouver)')]]
    for r in recs:
        rows.append([CAL_SENSORS.get(r['sensor'],(r['sensor'],))[0],date.fromisoformat(r['calibrated_on']),r['calibrated_by'],r['certificate_no'],
                     r['file_name'] or 'none',r['note'],r['logged_by'],parse(r['logged_at']).astimezone(ZoneInfo('America/Vancouver')).replace(tzinfo=None)])
    if not recs:rows.append(['No calibrations logged yet.'])
    rows+=[[],['Calibrations are logged on the Audit page. Certificate files are kept in the dashboard database and can be downloaded from the Audit page.']]
    return rows

def handle_post(path,env,role,conn,user=''):
    who=actor(role,user)
    # Custom header + same-origin check defend cookie/browser cross-site submission with Basic auth.
    if env.get('HTTP_X_WW_REQUEST')!='1':return '403 Forbidden',{'error':'Request header required'}
    if env.get('HTTP_SEC_FETCH_SITE','same-origin') not in ('same-origin','none'):return '403 Forbidden',{'error':'Cross-site blocked'}
    origin=env.get('HTTP_ORIGIN')
    if origin and urlsplit(origin).netloc!=env.get('HTTP_HOST',''):return '403 Forbidden',{'error':'Origin rejected'}
    length=int(env.get('CONTENT_LENGTH') or 0)
    if length<1 or length>(CAL_BODY_MAX if path=='/api/calibrations' else 12000):raise ValueError('Invalid request length')
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
        if path=='/api/accounts/user':
            role_name=body.get('role');save_user(conn,role_name,body.get('username'),body.get('password'))
            audit(conn,who,f'{role_name} login set','username '+body['username']);conn.commit()
            return '200 OK',{'saved':role_name,'username':body['username']}
        if path=='/api/accounts/user/remove':
            role_name,name=body.get('role'),body.get('username')
            if role_name not in USER_ROLES or not isinstance(name,str) or not remove_user(conn,role_name,name):return '404 Not Found',{'error':'Login not found'}
            audit(conn,who,f'{role_name} login removed','username '+name);conn.commit()
            return '200 OK',{'removed':name}
        if path=='/api/accounts/permissions':
            new={}
            for role_name in USER_ROLES:
                chosen=body.get(role_name)
                if not isinstance(chosen,list) or any(p not in PERMISSION_CHOICES for p in chosen):raise ValueError('Invalid permissions')
                new[role_name]=sorted(set(chosen))
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('role_permissions',?)",(json.dumps({**new,'_choices':list(PERMISSION_CHOICES)}),))
            audit(conn,who,'Role rights changed','; '.join(f"{r}: {', '.join(p) or 'view only'}" for r,p in new.items()));conn.commit()
            return '200 OK',{'permissions':new}
        if path=='/api/accounts/operator/remove':
            name=body.get('username')
            if not isinstance(name,str) or not remove_operator(conn,name):return '404 Not Found',{'error':'Operator not found'}
            audit(conn,who,'Operator login removed','username '+name);conn.commit()
            return '200 OK',{'removed':name}
        if path=='/api/accounts/operator/disable':
            disable_operator(conn);audit(conn,who,'Operator login disabled','all Operator logins');conn.commit()
            return '200 OK',{'disabled':'operator'}
        return '404 Not Found',{'error':'Not found'}
    if path=='/api/pickups':
        if not can(role,'pickup'):return '403 Forbidden',{'error':'Not permitted to log sludge pickups'}
        t,note=save_pickup(conn,body.get('when'),body.get('note',''),who)
        audit(conn,who,'Sludge pickup logged',f"{t.astimezone(ZoneInfo('America/Vancouver')):%Y-%m-%d %H:%M}"+(' · '+note if note else ''));conn.commit()
        return '200 OK',{'picked_at':iso(t)}
    if path=='/api/calibrations':
        if not can(role,'audit'):return '403 Forbidden',{'error':'Not permitted to log calibrations'}
        cid,label,day,name,cert=save_calibration(conn,body,who)
        audit(conn,who,'Sensor calibration logged',f"{label} calibrated {day:%Y-%m-%d} by {name}"+(f'; certificate {cert}' if cert else ''));conn.commit()
        return '200 OK',{'id':cid}
    if path=='/api/calibrations/remove':
        if not can(role,'audit'):return '403 Forbidden',{'error':'Not permitted to change calibrations'}
        cid=body.get('id')
        if type(cid)!=int or cid<1:raise ValueError('Invalid calibration ID')
        row=conn.execute('SELECT sensor,calibrated_on,calibrated_by FROM calibrations WHERE id=?',(cid,)).fetchone()
        if not row:return '404 Not Found',{'error':'Calibration not found'}
        conn.execute('DELETE FROM calibrations WHERE id=?',(cid,))
        audit(conn,who,'Sensor calibration removed',f"{CAL_SENSORS.get(row['sensor'],(row['sensor'],))[0]} calibrated {row['calibrated_on']} by {row['calibrated_by']}");conn.commit()
        return '200 OK',{'removed':cid}
    if path=='/api/stock':
        if not can(role,'pickup'):return '403 Forbidden',{'error':'Not permitted to log chemical stock'}
        sid,label,kind,litres,t=save_stock(conn,body,who)
        audit(conn,who,'Chemical stock logged',f"{label} {'delivery' if kind=='delivery' else 'tank count'} {litres:g} L at {t.astimezone(ZoneInfo('America/Vancouver')):%Y-%m-%d %H:%M}");conn.commit()
        return '200 OK',{'id':sid}
    if path=='/api/purchases':
        if not can(role,'pickup'):return '403 Forbidden',{'error':'Not permitted to log chemical purchases'}
        pid,name,bought,litres,received=save_purchase(conn,body,who)
        audit(conn,who,'Chemical purchase logged',f"{name} {litres:g} L purchased {bought:%Y-%m-%d}"+(f", received {received.astimezone(ZoneInfo('America/Vancouver')):%Y-%m-%d %H:%M}" if received else ', not received yet'));conn.commit()
        return '200 OK',{'id':pid}
    if path=='/api/purchases/receive':
        if not can(role,'pickup'):return '403 Forbidden',{'error':'Not permitted to change chemical purchases'}
        row,t=receive_purchase(conn,body,who)
        audit(conn,who,'Chemical purchase received',f"{CHEM_PRODUCTS[row['chemical']]['name']} {row['litres']:g} L (purchased {row['purchased_on']}) received {t.astimezone(ZoneInfo('America/Vancouver')):%Y-%m-%d %H:%M}");conn.commit()
        return '200 OK',{'received':row['id']}
    if path=='/api/purchases/remove':
        if not can(role,'pickup'):return '403 Forbidden',{'error':'Not permitted to change chemical purchases'}
        pid=body.get('id')
        if type(pid)!=int or pid<1:raise ValueError('Invalid purchase ID')
        row=conn.execute('SELECT chemical,litres,purchased_on FROM chemical_purchases WHERE id=?',(pid,)).fetchone()
        if not row:return '404 Not Found',{'error':'Purchase not found'}
        conn.execute('DELETE FROM chemical_purchases WHERE id=?',(pid,))
        audit(conn,who,'Chemical purchase removed',f"{CHEM_PRODUCTS.get(row['chemical'],{}).get('name',row['chemical'])} {row['litres']:g} L purchased {row['purchased_on']}");conn.commit()
        return '200 OK',{'removed':pid}
    if path=='/api/stock/remove':
        if not can(role,'pickup'):return '403 Forbidden',{'error':'Not permitted to change chemical stock'}
        sid=body.get('id')
        if type(sid)!=int or sid<1:raise ValueError('Invalid stock entry ID')
        row=conn.execute('SELECT chemical,kind,litres,at FROM chemical_stock WHERE id=?',(sid,)).fetchone()
        if not row:return '404 Not Found',{'error':'Stock entry not found'}
        conn.execute('DELETE FROM chemical_stock WHERE id=?',(sid,))
        audit(conn,who,'Chemical stock entry removed',f"{STOCK_CHEMS.get(row['chemical'],(row['chemical'],))[0]} {row['kind']} {row['litres']:g} L");conn.commit()
        return '200 OK',{'removed':sid}
    if path=='/api/shelves':
        if not can(role,'settings'):return '403 Forbidden',{'error':'Not permitted to shelve alarms'}
        key=body.get('key');hours=body.get('hours');choices=shelf_choices()
        if key not in choices:raise AccountError('Choose the alarm to shelve')
        if type(hours) is not int or not 1<=hours<=SHELF_MAX_HOURS:raise AccountError(f'Shelve for 1–{SHELF_MAX_HOURS} hours')
        reason=_cal_text(body.get('reason'),'Reason',200,True)
        now=datetime.now(UTC);until=now+timedelta(hours=hours)
        conn.execute('INSERT OR REPLACE INTO shelves(key,until,reason,shelved_by,shelved_at) VALUES (?,?,?,?,?)',(key,iso(until),reason,who,iso(now)))
        audit(conn,who,'Alarm shelved',f"{choices[key]} for {hours} h until {until.astimezone(ZoneInfo('America/Vancouver')):%Y-%m-%d %H:%M}: {reason}");conn.commit()
        return '200 OK',{'key':key,'until':iso(until)}
    if path=='/api/shelves/remove':
        if not can(role,'settings'):return '403 Forbidden',{'error':'Not permitted to unshelve alarms'}
        key=body.get('key')
        if not isinstance(key,str) or not conn.execute('DELETE FROM shelves WHERE key=?',(key,)).rowcount:return '404 Not Found',{'error':'Shelf not found'}
        audit(conn,who,'Alarm unshelved',shelf_choices().get(key,key));conn.commit()
        return '200 OK',{'removed':key}
    if path=='/api/pickups/remove':
        if not can(role,'pickup'):return '403 Forbidden',{'error':'Not permitted to change sludge pickups'}
        pid=body.get('id')
        if type(pid)!=int or pid<1:raise ValueError('Invalid pickup ID')
        row=conn.execute('SELECT picked_at FROM sludge_pickups WHERE id=?',(pid,)).fetchone()
        if not row:return '404 Not Found',{'error':'Pickup not found'}
        conn.execute('DELETE FROM sludge_pickups WHERE id=?',(pid,))
        audit(conn,who,'Sludge pickup removed',f"{parse(row['picked_at']).astimezone(ZoneInfo('America/Vancouver')):%Y-%m-%d %H:%M}");conn.commit()
        return '200 OK',{'removed':pid}
    if path=='/api/ack':
        if not can(role,'acknowledge'):return '403 Forbidden',{'error':'Not permitted'}
        eid=body.get('id')
        if type(eid)!=int or eid<1:raise ValueError('Invalid event ID')
        # A PLC alarm that has already cleared is finished once acknowledged.
        cur=conn.execute("UPDATE events SET state=CASE WHEN cleared_at IS NULL THEN 'acknowledged' ELSE 'closed' END, actor=?, ack_at=? WHERE id=? AND state='open'",
                         (who,iso(datetime.now(UTC)),eid))
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
                d=summary();d['role']=role;d['user']=user;d['permissions']=sorted(role_permissions()[role])
                with appdb() as conn:
                    cfg=settings(conn);d['limits']={'low':cfg['ph_low'],'high':cfg['ph_high'],'enabled':cfg['alarms_enabled'],'confirm_samples':cfg['alarm_confirm_samples'],
                                                    'stale_minutes':cfg['stale_alert_minutes']}
                    d['active_alarms']=[dict(r) for r in conn.execute("SELECT id,stamp,kind,detail,state,cleared_at FROM events WHERE state IN ('open','acknowledged') ORDER BY id DESC LIMIT 10")]
                    last=conn.execute('SELECT picked_at FROM sludge_pickups ORDER BY picked_at DESC LIMIT 1').fetchone()
                    d['last_pickup']=last[0] if last else None;d['pickup_days']=PICKUP_DAYS
                    d['plc_alarms']=[{'tag':t,'kind':k,'banner':b,'address':a} for t,k,b,a,_ in PLC_ALARMS]
                    d['shelved']=[dict(r,label=shelf_choices().get(r['key'],r['key'])) for r in active_shelves(conn).values()]
                ph=next((t for t in d['tags'] if t['name']=='pH_Reading'),{})
                d['ph_trend']=ph_trend(cfg) if cfg['ph_trend_enabled'] and ph.get('status')=='current' else None
                return send('200 OK',d)
            if path=='/api/insights':
                if not can(role,'view'):return send('403 Forbidden',{'error':'Not permitted'})
                with appdb() as conn:cfg=settings(conn)
                return send('200 OK',{'days':30,'insights':insights(cfg)})
            if path in ('/api/history','/api/export'):
                if not can(role,'export' if path.endswith('export') else 'view'):return send('403 Forbidden',{'error':'Not permitted'})
                query=parse_qs(env.get('QUERY_STRING',''))
                if path=='/api/export' and 'tag' in query and query.get('format',[''])[0]=='csv':
                    fname,data=report_csv(query)
                    return send('200 OK',data,'text/csv; charset=utf-8',[('Content-Disposition',f'attachment; filename="{fname}"')])
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
            if path=='/api/pickups':
                with appdb() as conn:
                    rows=[dict(r) for r in conn.execute('SELECT * FROM sludge_pickups ORDER BY picked_at DESC LIMIT 100')]
                return send('200 OK',{'rows':rows,'pickup_days':PICKUP_DAYS})
            if path=='/api/calibrations':
                if not can(role,'audit'):return send('403 Forbidden',{'error':'Not permitted'})
                with appdb() as conn:return send('200 OK',{'rows':calibration_records(conn),'status':calibration_status(conn,settings(conn)),
                    'sensors':{k:{'label':l,'tag':t} for k,(l,t) in CAL_SENSORS.items()},'max_file_kb':CAL_FILE_MAX//1000})
            if path=='/api/stock':
                with appdb() as conn:
                    cfg=settings(conn)
                    st=stock_status(conn)
                    for k,(_,_,limit_key) in STOCK_CHEMS.items():st[k]['low_l']=cfg[limit_key]
                    rows=[dict(r) for r in conn.execute('SELECT * FROM chemical_stock ORDER BY at DESC,id DESC LIMIT 100')]
                return send('200 OK',{'status':st,'rows':rows,'products':CHEM_PRODUCTS,'labels':{k:v[0] for k,v in STOCK_CHEMS.items()}})
            if path=='/api/purchases':
                with appdb() as conn:
                    prices=settings(conn)['chemical_prices']
                    rows=[dict(r,est_cost=purchase_cost(r,prices)[0] if r['cost'] is None else None) for r in purchase_records(conn)[:100]]
                return send('200 OK',{'rows':rows,'products':CHEM_PRODUCTS})
            if path=='/api/shelves':
                with appdb() as conn:return send('200 OK',{'choices':shelf_choices(),'active':list(active_shelves(conn).values()),'max_hours':SHELF_MAX_HOURS})
            if path=='/api/calibrations/certificate':
                if not can(role,'audit'):return send('403 Forbidden',{'error':'Not permitted'})
                try:cid=int(parse_qs(env.get('QUERY_STRING','')).get('id',[''])[0])
                except ValueError:return send('400 Bad Request',{'error':'Invalid calibration ID'})
                with appdb() as conn:row=conn.execute('SELECT file_name,file_type,file_data FROM calibrations WHERE id=?',(cid,)).fetchone()
                if not row or row['file_data'] is None:return send('404 Not Found',{'error':'No certificate file for this calibration'})
                return send('200 OK',bytes(row['file_data']),row['file_type'],[('Content-Disposition',f'attachment; filename="{row["file_name"]}"')])
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
                cur=new_alarm_event(conn,iso(datetime.now(UTC)),f'pH {status}',
                     f"Confirmed over {count} readings; pH {ph['value']:.2f}; limits {cfg['ph_low']:.2f}–{cfg['ph_high']:.2f}")
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
            cur=new_alarm_event(conn,iso(datetime.now(UTC)),f'pH {status}',
                 f"Confirmed over {count} readings; pH {ph['value']:.2f}; limits {cfg['ph_low']:.2f}–{cfg['ph_high']:.2f}")
            active=status;active_event=cur.lastrowid
            audit(conn,'system','Confirmed pH incident',f'{status} incident {active_event}')
            cand=None;count=0
    conn.execute('''UPDATE ph_alarm_v2 SET last_sample_stamp=?,candidate_kind=?,candidate_count=?,
        active_kind=?,active_event=?,recovery_count=?,pending_recovery_event=?,recovery_origin_event=?
        WHERE id=1''',(stamp,cand,count,active,active_event,recovery_count,recovered_event,recovery_origin))
    conn.commit()


AUTO_ACK_ACTOR='system (auto-acknowledged)'
def new_alarm_event(conn,stamp,kind,detail):
    """Record a new alarm. Alarms are acknowledged automatically, so nobody has to press
    Acknowledge; the alarm stays 'active' until its condition clears, then closes."""
    return conn.execute('INSERT INTO events(stamp,kind,detail,state,actor,ack_at) VALUES (?,?,?,?,?,?)',
                        (stamp,kind,detail,'acknowledged',AUTO_ACK_ACTOR,stamp))

def update_condition(conn,key,enabled,abnormal,stamp,kind,detail,recovered_kind,recovered_detail,confirm=1,recover=1,latch=False,auto_ack=None):
    """Generic incident tracker for the data-stopped, high-level, no-flow and PLC alarms.

    abnormal: True/False for a fresh evaluation, None when there is nothing to judge.
    Each stamp counts once; an incident opens after `confirm` abnormal evaluations in a row
    and closes after `recover` normal ones. State is kept as JSON in alarm_state[key].
    latch: when the condition clears, an alarm nobody has acknowledged stays open (marked
    cleared) until someone acknowledges it. auto_ack: actor recorded when the clear itself
    acknowledges the alarm (sludge tank emptied).
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
                cur=new_alarm_event(conn,now,kind,detail)
                st['active']=cur.lastrowid;st['cand']=0
                audit(conn,'system',f'Confirmed {kind}',f'incident {st["active"]}')
        elif abnormal:
            st['rec']=0
        else:
            st['rec']=st.get('rec',0)+1
            if st['rec']>=recover:
                eid=st['active']
                if auto_ack:
                    conn.execute("UPDATE events SET state='closed',cleared_at=?,actor=COALESCE(actor,?),ack_at=COALESCE(ack_at,?) WHERE id=? AND state IN ('open','acknowledged')",
                                 (now,auto_ack,now,eid))
                elif latch:
                    conn.execute("UPDATE events SET cleared_at=?,state=CASE WHEN state='acknowledged' THEN 'closed' ELSE state END WHERE id=? AND state IN ('open','acknowledged')",
                                 (now,eid))
                else:
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

def check_plc_alarms(conn,cfg,extras):
    """PLC alarm bits (PLC_ALARMS, 1 = alarm). Returns {'plc:<tag>': state}."""
    out={}
    for tag,kind,_,addr,hint in PLC_ALARMS:
        r=(extras or {}).get(tag) or {}
        known=r.get('status')=='current' and r.get('value') is not None
        out['plc:'+tag]=update_condition(conn,'plc:'+tag,cfg['plc_alarms_enabled'],
            (r['value']>=0.5) if known else None, r.get('stamp'),
            kind,f'{kind}. {hint}',
            f'{kind} cleared','back to normal',latch=True,auto_ack=AUTO_ACK_ON_CLEAR.get(tag))
    return out

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
    # Predictive pH: judged once a minute while pH is live and inside the limits (outside: the pH alarm takes over).
    ph=tag('pH_Reading')
    tr=ph_trend(cfg) if cfg['ph_trend_enabled'] and ph.get('status')=='current' else None
    inside=tr is not None and cfg['ph_low']<=tr['value']<=cfg['ph_high']
    for side in ('high','low'):
        hit=None if not inside else tr['direction']==side
        out['ph_trend_'+side]=update_condition(conn,'ph_trend_'+side,cfg['ph_trend_enabled'],hit,now.strftime('%Y-%m-%dT%H:%M'),
            f'pH trending {side}',trend_text(tr) if hit else '',f'pH trending {side} cleared','pH is no longer heading for the limit',
            cfg['alarm_confirm_samples'],cfg['alarm_recovery_samples'])
    # Dosing pump stopped while discharging
    for key,(ptag,label) in DOSING_PUMPS.items():
        hit,peak=dosing_stopped(ptag,cfg['dosing_alarm_minutes'],cfg['flow_low_lpm']) if cfg['dosing_alarm_enabled'] else (None,None)
        out['dosing:'+key]=update_condition(conn,'dosing:'+key,cfg['dosing_alarm_enabled'],hit,now.strftime('%Y-%m-%dT%H:%M'),
            f'{label} pump not dosing',
            f"{label} dosing pump at 0 ml/min for {cfg['dosing_alarm_minutes']} minutes while wastewater is being discharged "
            f"(flow up to {peak or 0:.2f} L/min). Check the pump, its chemical drum and suction line, and the PLC setpoint.",
            f'{label} pump not dosing cleared','the dosing pump is running again or discharge stopped')
    return out

# Email wording for each PLC alarm (as specified by the plant): tag -> (email title, alarm code).
PLC_EMAIL_WORDING={'SLUDGE_TANK_LEVEL':('Wastewater Sludge Tank are Full','SLUDGE_TANK_FULL'),
                   'MIXER_PUMP_FAULTED':('Circulating Mixer Pump Stop Faulted','MIXER_PUMP_FAULTED'),
                   'RAKE_MOTOR_FAULTED':('Sludge Rake Gear Motor Stop Faulted','RAKE_MOTOR_FAULTED'),
                   'FLOC_COAG_PUMP_FAULTED':('Flocculant and Coagulant Pump Motor Stop Faulted','FLOC_COAG_PUMP_FAULTED'),
                   'E_STOP_STATUS':('Emergency Stop Activated','E_STOP_STATUS'),
                   'MAX_RGT_PUMP_FAULTED':('Regenerative Turbine Pump Stop Faulted','MAX_RGT_PUMP_FAULTED'),
                   'EQ_TANK_FULL':('Wastewater EQ Tank Full','EQ_TANK_FULL')}
CONDITION_SUBJECTS={'ph_trend_high':'Wastewater WARNING: pH trending HIGH','ph_trend_low':'Wastewater WARNING: pH trending LOW',
                    'dosing:coag':'Wastewater WARNING: coagulant pump not dosing','dosing:floc':'Wastewater WARNING: flocculant pump not dosing',
                    'data':'Wastewater ALERT: no new data','level':'Wastewater EQ tank HIGH level alarm','flow':'Wastewater NO DISCHARGE FLOW alarm',
                    'stock:coag':'Wastewater LOW COAGULANT stock (PROTECT 7000)','stock:floc':'Wastewater LOW FLOCCULANT stock (PF 875AH)',
                    'stock:caustic':'Wastewater LOW CAUSTIC stock (NEUTRO 5045)','stock:sa93':'Wastewater LOW SA-93 stock',
                    **{'plc:'+t:f'{w} ({c})' for t,(w,c) in PLC_EMAIL_WORDING.items()}}
RECOVERY_SUBJECTS={'plc:'+t:f'CLEARED: {w} ({c})' for t,(w,c) in PLC_EMAIL_WORDING.items()}

def alarm_report(start,end,limit=100):
    """Text list of the alarms that started in start..end (UTC datetimes), for the report emails."""
    zone=ZoneInfo('America/Vancouver')
    with appdb() as conn:
        rows=conn.execute("SELECT stamp,kind,detail,state,actor,cleared_at FROM events WHERE stamp>=? AND stamp<? "
                          "AND kind NOT LIKE '%recovered' AND kind NOT LIKE '%restored' AND kind NOT LIKE '%cleared' ORDER BY stamp",
                          (iso(start),iso(end))).fetchall()
    out=[f'Alarms in this period: {len(rows)}']
    for r in rows[:limit]:
        status='still active' if r['state'] in ('open','acknowledged') and not r['cleared_at'] else 'cleared'
        status+=f", acknowledged by {r['actor']}" if r['actor'] else ', not acknowledged'
        out.append(f"- {parse(r['stamp']).astimezone(zone):%Y-%m-%d %H:%M} {r['kind']}: {r['detail']} [{status}]")
    if len(rows)>limit:out.append(f'... and {len(rows)-limit} more (see the Alarms page).')
    return '\n'.join(out)

def report_csv(params):
    """CSV download (Reports page) of the raw readings for one tag or 'all'. Returns (filename, bytes)."""
    choice=params.get('tag',['all'])[0]
    if choice=='consumption':
        with appdb() as c:cfg=settings(c)
        try:year=int(params.get('year',[params.get('startDate',[''])[0][:4]])[0])
        except ValueError:year=0
        if not 2020<=year<=datetime.now(UTC).year+1:raise ValueError('Choose a year.')
        rows=consumption_rows(year,cfg)
        return f'wastewater-chemical-consumption-{year}.csv',rows_csv(rows)
    if choice=='purchases':
        with appdb() as c:cfg=settings(c)
        try:year=int(params.get('year',[params.get('startDate',[''])[0][:4]])[0])
        except ValueError:year=0
        if not 2020<=year<=datetime.now(UTC).year+1:raise ValueError('Choose a year.')
        rows=purchase_rows(year,cfg)
        return f'wastewater-chemical-purchases-{year}.csv',rows_csv(rows)
    if choice in ('alarms','compliance','pumps'):
        start,end=window(params,REPORT_MAX_DAYS);zone=ZoneInfo('America/Vancouver')
        a=start.astimezone(zone).date();b=(end.astimezone(zone)-timedelta(microseconds=1)).date();rng=f'{a:%m/%d/%Y} to {b:%m/%d/%Y}'
        with appdb() as c:cfg=settings(c)
        name,rows,widths={'alarms':('Alarm History',lambda:alarm_rows(start,end,zone,rng),(22,30,60,22,14,30,22,14,14)),
                          'compliance':('Discharge Compliance',lambda:compliance_rows(start,end,zone,rng,cfg),(16,12,10,10,10,16,18,22,16,12)),
                          'pumps':('Pump Run Hours',lambda:pump_rows(start,end,zone,rng),(34,22,14,14,18))}[choice]
        rows=rows();stem=f'wastewater-{choice}-{a:%Y-%m-%d}-to-{b:%Y-%m-%d}'
        return stem+'.csv',rows_csv(rows)
    if choice=='calibration':
        out=io.StringIO(newline='');writer=csv.writer(out)
        writer.writerow(['Sensor','Tag','Date calibrated','Calibrated by','Certificate no.','Certificate file','Note','Logged by','Logged at (America/Vancouver)'])
        with appdb() as conn:recs=calibration_records(conn)
        for r in recs:
            label,tag=CAL_SENSORS.get(r['sensor'],(r['sensor'],''))
            writer.writerow([label,tag,r['calibrated_on'],r['calibrated_by'],r['certificate_no'],r['file_name'] or 'none',r['note'],r['logged_by'],
                             parse(r['logged_at']).astimezone(ZoneInfo('America/Vancouver')).strftime('%Y-%m-%d %H:%M')])
        return f"wastewater-sensor-calibrations-{datetime.now(ZoneInfo('America/Vancouver')):%Y-%m-%d}.csv",out.getvalue().encode('utf-8-sig')
    if choice not in ('all','chemical','sludge') and choice not in REPORT_TAGS:raise ValueError('Choose a report type from the list.')
    names=REPORT_TAGS if choice=='all' else (choice,)
    start,end=window(params,REPORT_MAX_DAYS)
    zone=ZoneInfo('America/Vancouver')
    out=io.StringIO(newline='');writer=csv.writer(out)
    local_start=start.astimezone(zone).date();local_end=(end.astimezone(zone)-timedelta(microseconds=1)).date()
    if choice=='chemical':
        used=chemical_usage(start,end,zone)
        writer.writerow(['Date (America/Vancouver)']+[f'{n} used (L)' for _,n in CHEMICAL_TAGS])
        d=local_start
        while d<=local_end:
            writer.writerow([d.isoformat()]+[f'{used[t].get(d,0.0):.2f}' for t,_ in CHEMICAL_TAGS]);d+=timedelta(days=1)
        writer.writerow(['Total']+[f'{sum(used[t].values()):.2f}' for t,_ in CHEMICAL_TAGS])
        return f'wastewater-chemical-usage-{local_start:%Y-%m-%d}-to-{local_end:%Y-%m-%d}.csv',out.getvalue().encode('utf-8-sig')
    if choice=='sludge':
        f=lambda t:t.astimezone(zone).strftime('%Y-%m-%d %H:%M') if t else ''
        writer.writerow(['Pickup (America/Vancouver)','Tank FULL since','Hours FULL to pickup','Days since previous pickup','Logged by','Note'])
        for p in sludge_analysis(start,end)[0]:
            writer.writerow([f(p['at']),f(p['full_since']),'' if p['wait_h'] is None else f"{p['wait_h']:.2f}",
                             '' if p['since_prev_d'] is None else f"{p['since_prev_d']:.2f}",p['logged_by'],p['note']])
        return f'wastewater-sludge-pickups-{local_start:%Y-%m-%d}-to-{local_end:%Y-%m-%d}.csv',out.getvalue().encode('utf-8-sig')
    writer.writerow(['Tag','Timestamp UTC','Timestamp America/Vancouver','Raw Value','Unit Of Measure'])
    db=database()
    try:
        for name in names:
            for stamp,value in db.execute('SELECT stamp,value FROM samples WHERE name=? AND stamp>=? AND stamp<? ORDER BY stamp',(name,iso(start),iso(end))):
                t=parse(stamp)
                writer.writerow([name,t.strftime('%Y-%m-%d %H:%M:%S'),t.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S'),'' if value is None else value,raw_unit(name,stamp)])
    finally:db.close()
    return f'wastewater-{choice}-{local_start:%Y-%m-%d}-to-{local_end:%Y-%m-%d}.csv',out.getvalue().encode('utf-8-sig')


def period_report(start,end,data_tags):
    """Excel report for Vancouver dates start..end inclusive, for email attachments."""
    return report_xlsx({'tag':['all'],'startDate':[start.isoformat()],'endDate':[end.isoformat()]},data_tags)

# ---------------------------------------------------------------- calibration due dates
CAL_INTERVAL_KEYS={'ph':'cal_ph_days','level':'cal_level_days'}
def calibration_status(conn,cfg,today=None):
    """Per sensor: last calibration, due date, days left and state (ok, soon, overdue, never)."""
    today=today or datetime.now(ZoneInfo('America/Vancouver')).date()
    out={}
    for key,(label,tag) in CAL_SENSORS.items():
        last=conn.execute('SELECT id,calibrated_on,calibrated_by FROM calibrations WHERE sensor=? ORDER BY calibrated_on DESC,id DESC LIMIT 1',(key,)).fetchone()
        every=cfg[CAL_INTERVAL_KEYS[key]];due=left=None;state='never'
        if last:
            due=date.fromisoformat(last['calibrated_on'])+timedelta(days=every);left=(due-today).days
            state='overdue' if left<0 else 'soon' if left<=cfg['cal_remind_days'] else 'ok'
        out[key]=dict(label=label,tag=tag,every_days=every,last=last['calibrated_on'] if last else None,last_id=last['id'] if last else None,
                      last_by=last['calibrated_by'] if last else None,due=due.isoformat() if due else None,days_left=left,state=state)
    return out

# ---------------------------------------------------------------- shared time helpers
def day_segments(a,b,zone):
    """Split UTC interval a..b at local midnights: yields (local date, seg_start, seg_end)."""
    while a<b:
        day=a.astimezone(zone).date()
        seg=min(b,datetime.combine(day+timedelta(days=1),time.min,zone).astimezone(UTC))
        yield day,a,seg
        a=seg

def local_days(start,end,zone):
    first=start.astimezone(zone).date();last=(end.astimezone(zone)-timedelta(microseconds=1)).date()
    out=[];d=first
    while d<=last:out.append(d);d+=timedelta(days=1)
    return out

def held_points(db,tag,start,end):
    """[(time, value)] for a tag in start..end, starting with the last reading before start (held, Ewon logs on change)."""
    prior=db.execute('SELECT value FROM samples WHERE name=? AND stamp<? AND value IS NOT NULL ORDER BY stamp DESC LIMIT 1',(tag,iso(start))).fetchone()
    pts=[(start,prior[0])] if prior else []
    pts+=[(parse(t),v) for t,v in db.execute('SELECT stamp,value FROM samples WHERE name=? AND stamp>=? AND stamp<? AND value IS NOT NULL ORDER BY stamp',(tag,iso(start),iso(end)))]
    return pts

def rows_csv(rows):
    """Excel-style rows (cells may be (value, style)) as CSV bytes."""
    out=io.StringIO(newline='');w=csv.writer(out)
    for row in rows:
        cells=[c[0] if isinstance(c,tuple) else c for c in row]
        w.writerow(['' if c is None else c.strftime('%Y-%m-%d %H:%M') if isinstance(c,datetime) else c.isoformat() if isinstance(c,date) else c for c in cells])
    return out.getvalue().encode('utf-8-sig')

# ---------------------------------------------------------------- alarm history report
RECOVERY_SUFFIXES=('recovered','restored','cleared')
def alarm_history(start,end):
    """Alarms that started in start..end with their clear time (cleared_at or the matching recovery event)."""
    with appdb() as conn:
        out=[]
        for r in conn.execute('SELECT * FROM events WHERE stamp>=? AND stamp<? ORDER BY stamp',(iso(start),iso(end))).fetchall():
            r=dict(r)
            if r['kind'].endswith(RECOVERY_SUFFIXES):continue
            cleared=r.get('cleared_at')
            if not cleared:
                rec=conn.execute('SELECT stamp FROM events WHERE id>? AND detail LIKE ? ORDER BY id LIMIT 1',(r['id'],f"Incident {r['id']};%")).fetchone()
                cleared=rec[0] if rec else None
            out.append(dict(r,cleared=cleared))
    return out

def alarm_rows(start,end,zone,date_range):
    B=lambda t:(t,3);num=lambda v:'' if v is None else (round(v,1),2)
    L=lambda s:parse(s).astimezone(zone).replace(tzinfo=None) if s else ''
    now=datetime.now(UTC);hist=alarm_history(start,end);groups={}
    for h in hist:
        t0=parse(h['stamp']);active=h['state'] in ('open','acknowledged') and not h['cleared']
        t1=parse(h['cleared']) if h['cleared'] else now if active else None
        h['mins']=(t1-t0).total_seconds()/60 if t1 else None
        h['ack_mins']=(parse(h['ack_at'])-t0).total_seconds()/60 if h['ack_at'] else None
        h['status']='still active' if active else 'cleared'
        g=groups.setdefault(h['kind'],{'n':0,'mins':0.0,'acks':[],'unacked':0})
        g['n']+=1;g['mins']+=h['mins'] or 0
        if h['ack_mins'] is not None:g['acks'].append(h['ack_mins'])
        if not h['actor']:g['unacked']+=1
    rows=[[B('Alarm history')],['Date range (America/Vancouver)',date_range],['Alarms in range',len(hist)],[],
          [B('Alarm'),B('Times'),B('Total minutes active'),B('Average minutes to acknowledge'),B('Never acknowledged')]]
    rows+=[[k,g['n'],num(g['mins']),num(sum(g['acks'])/len(g['acks']) if g['acks'] else None),g['unacked']] for k,g in sorted(groups.items())]
    if not groups:rows.append(['No alarms in this date range.'])
    rows+=[[],[B('Started'),B('Alarm'),B('Detail'),B('Cleared'),B('Minutes active'),B('Acknowledged by'),B('Acknowledged at'),B('Minutes to acknowledge'),B('Status')]]
    rows+=[[L(h['stamp']),h['kind'],h['detail'],L(h['cleared']) or ('—' if h['status']=='cleared' else 'still active'),num(h['mins']),
            h['actor'] or 'not acknowledged',L(h['ack_at']),num(h['ack_mins']),h['status']] for h in hist]
    rows+=[[],['Minutes active run from the start of the alarm to its clear (PLC bit back to 0, or the confirmed recovery). '
               'Alarms still active count up to now. Auto-acknowledged alarms (sludge and EQ tank FULL) count their clear as the acknowledgement.']]
    return rows

# ---------------------------------------------------------------- discharge compliance
FLOW_FACTOR=TAGS['FLOW_RATE'][2]
PH_HOLD_MAX=timedelta(minutes=15)   # a pH reading counts for at most this long (longer gaps are missing data)

def held_integral(points,a,b):
    """Area of a held (step) series over a..b: sum of value x minutes. points: [(time, value)] sorted."""
    total=0.0
    for i,(t,v) in enumerate(points):
        nxt=points[i+1][0] if i+1<len(points) else b
        lo,hi=max(t,a),min(nxt,b)
        if hi>lo and v is not None and v>0:total+=v*(hi-lo).total_seconds()/60
    return total

def ph_excursions(db,start,end,cfg):
    """Periods when pH was outside the limits, with the litres discharged during each.
    Each out-of-limit reading counts until the next reading (at most PH_HOLD_MAX); readings on the same side
    that follow each other join into one excursion. Litres: FLOW_RATE (held, Ewon logs on change) x time."""
    pts=[(parse(t),v) for t,v in db.execute("SELECT stamp,value FROM samples WHERE name='pH_Reading' AND stamp>=? AND stamp<? AND value IS NOT NULL ORDER BY stamp",(iso(start),iso(end)))]
    out=[]
    for i,(t,v) in enumerate(pts):
        side='high' if v>cfg['ph_high'] else 'low' if v<cfg['ph_low'] else None
        if not side:continue
        b=min(pts[i+1][0] if i+1<len(pts) else end,t+PH_HOLD_MAX,end)
        if b<=t:continue
        e=out[-1] if out else None
        if e and e['side']==side and e['end']>=t:
            e['end']=b;e['extreme']=max(e['extreme'],v) if side=='high' else min(e['extreme'],v)
        else:out.append({'start':t,'end':b,'side':side,'extreme':v})
    flow=held_points(db,'FLOW_RATE',start,end)
    for e in out:
        e['minutes']=(e['end']-e['start']).total_seconds()/60
        e['litres']=held_integral(flow,e['start'],e['end'])*FLOW_FACTOR
    return out,flow

def daily_discharge(db,start,end,zone,days):
    """Litres discharged per local day: increase of the Total_Flow_Li meter (meter resets are skipped)."""
    prev=db.execute("SELECT value FROM samples WHERE name='Total_Flow_Li' AND stamp<? AND value>? ORDER BY stamp DESC LIMIT 1",(iso(start),SUMMARY_MIN)).fetchone()
    prev=prev[0] if prev else None
    for t,v in db.execute("SELECT stamp,value FROM samples WHERE name='Total_Flow_Li' AND stamp>=? AND stamp<? AND value>? ORDER BY stamp",(iso(start),iso(end),SUMMARY_MIN)):
        d=days.get(parse(t).astimezone(zone).date())
        if prev is not None and v>=prev and d is not None:d['litres']+=v-prev   # a drop is a meter reset: skipped
        prev=v

def daily_compliance(start,end,zone,cfg,with_excursions=False):
    """Per local day: pH min/avg/max, minutes outside the pH limits, litres discharged, litres discharged while
    pH was outside the limits, peak flow. with_excursions: also return the list of excursions."""
    end=min(end,datetime.now(UTC));days={d:dict(ph=[],out_min=0.0,litres=0.0,out_litres=0.0,peak=None) for d in local_days(start,end,zone)}
    db=database()
    try:
        for t,v in db.execute("SELECT stamp,value FROM samples WHERE name='pH_Reading' AND stamp>=? AND stamp<? AND value IS NOT NULL ORDER BY stamp",(iso(start),iso(end))):
            d=days.get(parse(t).astimezone(zone).date())
            if d is not None:d['ph'].append(v)
        exc,flow=ph_excursions(db,start,end,cfg)
        for e in exc:
            for day,a,c in day_segments(e['start'],e['end'],zone):
                if day in days:
                    days[day]['out_min']+=(c-a).total_seconds()/60
                    days[day]['out_litres']+=held_integral(flow,a,c)*FLOW_FACTOR
        daily_discharge(db,start,end,zone,days)
        for t,v in db.execute("SELECT stamp,value FROM samples WHERE name='FLOW_RATE' AND stamp>=? AND stamp<? AND value IS NOT NULL ORDER BY stamp",(iso(start),iso(end))):
            d=days.get(parse(t).astimezone(zone).date())
            if d is not None:d['peak']=max(d['peak'] or 0,v*FLOW_FACTOR)
    finally:db.close()
    return (days,exc) if with_excursions else days

def compliance_rows(start,end,zone,date_range,cfg):
    B=lambda t:(t,3);num=lambda v,n=2:'' if v is None else (round(v,n),2)
    L=lambda t:t.astimezone(zone).replace(tzinfo=None)
    days,exc=daily_compliance(start,end,zone,cfg,with_excursions=True)
    tot_out=sum(x['out_min'] for x in days.values());tot_l=sum(x['litres'] for x in days.values())
    tot_out_l=sum(x['out_litres'] for x in days.values())
    covered=(min(end,datetime.now(UTC))-start).total_seconds()/60
    pct=lambda part,whole:'' if not whole else (round(100*(1-part/whole),2),2)
    rows=[[B('Daily discharge compliance summary')],['Date range (America/Vancouver)',date_range],
          ['pH limits (current Settings)',f"{cfg['ph_low']:.2f} – {cfg['ph_high']:.2f}"],
          ['Time inside pH limits (%)',pct(tot_out,covered)],['Volume discharged inside pH limits (%)',pct(tot_out_l,tot_l)],
          ['pH excursions',len(exc)],[],
          [B('Date'),B('pH readings'),B('pH min'),B('pH avg'),B('pH max'),B('Minutes outside pH limits'),B('Litres discharged'),
           B('Litres discharged while pH outside limits'),B('Peak flow (L/min)'),B('Compliant')]]
    for d,x in days.items():
        ph=x['ph']
        ok='no data' if not ph else 'yes' if x['out_min']<0.5 else 'NO'
        rows.append([d,len(ph),num(min(ph) if ph else None),num(sum(ph)/len(ph) if ph else None),num(max(ph) if ph else None),
                     num(x['out_min'],1),num(x['litres']),num(x['out_litres']),num(x['peak']),ok])
    rows+=[[B('Total'),'','','','',num(tot_out,1),num(tot_l),num(tot_out_l)],[],
           [B('pH excursions (each period outside the limits)')],
           [B('Started'),B('Ended'),B('Minutes'),B('High / low'),B('Highest / lowest pH'),B('Litres discharged during it')]]
    rows+=[[L(e['start']),L(e['end']),num(e['minutes'],1),e['side'],num(e['extreme']),num(e['litres'])] for e in exc] or [['None in this date range.']]
    rows+=[[],['Minutes outside pH limits: each pH reading outside the limits counts until the next reading (at most 15 minutes, so data gaps are not counted); '
               'readings on the same side that follow each other form one excursion. Litres discharged: increase of the Total_Flow_Li meter during the day '
               '(meter resets are skipped). Litres discharged while pH outside limits: discharge flow (FLOW_RATE, held until the next reading) x the minutes outside. '
               'Peak flow: highest FLOW_RATE reading. Compliant = no minutes outside the pH limits that day. Uses the pH limits in Settings now, not the limits at the time.']]
    return rows

def compliance_line(day,cfg):
    """One-line compliance summary of a local day, for the daily email."""
    zone=ZoneInfo('America/Vancouver')
    a=datetime.combine(day,time.min,zone).astimezone(UTC);b=datetime.combine(day+timedelta(days=1),time.min,zone).astimezone(UTC)
    x=daily_compliance(a,b,zone,cfg).get(day)
    if not x or not x['ph']:return 'Discharge compliance: no pH readings.'
    out_l=x['out_litres'];outside=f', of which {out_l:,.0f} L while pH was outside the limits' if x['out_min']>=0.5 else ''
    return (f"Discharge compliance: pH {min(x['ph']):.2f}–{max(x['ph']):.2f}, {x['out_min']:.0f} minutes outside {cfg['ph_low']:.2f}–{cfg['ph_high']:.2f}"
            f" ({'COMPLIANT' if x['out_min']<0.5 else 'NOT COMPLIANT'}); {x['litres']:,.0f} L discharged{outside}.")

# ---------------------------------------------------------------- predictive pH (trend toward the limits)
PH_TREND_MIN_RATE=0.1   # pH per hour: slower drift is not reported
def ph_trend(cfg,now=None):
    """Where pH is heading: least-squares slope of the held pH readings on a one-minute grid over the last
    ph_trend_window_min minutes. Returns {'value','rate_per_h','direction','limit','eta_min'} or None when there
    is not enough history. direction is 'high' / 'low' when pH is inside the limits but, at this rate, reaches
    one within ph_trend_lookahead_min minutes; otherwise None."""
    now=now or datetime.now(UTC);win=int(cfg['ph_trend_window_min']);start=now-timedelta(minutes=win)
    db=database()
    try:pts=held_points(db,'pH_Reading',start,now)
    finally:db.close()
    if not pts or pts[0][0]>start+timedelta(minutes=win*0.2):return None   # one held reading = steady pH (Ewon logs on change)
    ys=[];j=0
    for m in range(win+1):
        t=start+timedelta(minutes=m)
        while j+1<len(pts) and pts[j+1][0]<=t:j+=1
        ys.append(pts[j][1] if pts[j][0]<=t else None)
    xy=[(x,y) for x,y in enumerate(ys) if y is not None]
    n=len(xy)
    if n<5:return None
    mx=sum(x for x,_ in xy)/n;my=sum(y for _,y in xy)/n
    sxx=sum((x-mx)**2 for x,_ in xy)
    slope=sum((x-mx)*(y-my) for x,y in xy)/sxx if sxx else 0.0   # pH per minute
    value=pts[-1][1];rate=slope*60;out={'value':value,'rate_per_h':rate,'direction':None,'limit':None,'eta_min':None}
    if not cfg['ph_low']<=value<=cfg['ph_high'] or abs(rate)<PH_TREND_MIN_RATE:return out
    limit=cfg['ph_high'] if slope>0 else cfg['ph_low']
    eta=(limit-value)/slope
    out.update(limit=limit,eta_min=eta)
    if eta<=cfg['ph_trend_lookahead_min']:out['direction']='high' if slope>0 else 'low'
    return out

def trend_text(tr):
    side=tr['direction'];verb='rising' if side=='high' else 'falling'
    return (f"pH {tr['value']:.2f} is {verb} {abs(tr['rate_per_h']):.2f} pH per hour; at this rate it reaches the {tr['limit']:.2f} limit "
            f"in about {max(tr['eta_min'],0):.0f} minutes. Check the {'acid' if side=='high' else 'caustic'} dosing and the incoming wastewater.")

# ---------------------------------------------------------------- dosing pump stopped while discharging
DOSING_PUMPS={'coag':('Coag_Pump_Run','Coagulant'),'floc':('Floc_Pump_Run','Flocculant')}
def dosing_stopped(tag,minutes,flow_lpm,now=None):
    """(stopped while discharging?, peak flow L/min) over the last `minutes`. None when there is not enough data:
    the pump reading must cover the window and the discharge flow must be known."""
    now=now or datetime.now(UTC);start=now-timedelta(minutes=minutes)
    peak,_=flow_window(minutes)
    if peak is None:return None,None
    db=database()
    try:pts=[(t,ml_per_min(tag,iso(t),v)) for t,v in held_points(db,tag,start,now)]
    finally:db.close()
    if not pts or pts[0][0]>start:return None,peak
    return (peak>=flow_lpm and all((v or 0)<=0 for _,v in pts)),peak

# ---------------------------------------------------------------- insights (rule-based analysis of repeated problems)
INSIGHT_ACTIONS=(
    ('pH trending high','Watch the acid dosing and the incoming wash water; the trend warns before the limit is reached.'),
    ('pH trending low','Watch the caustic dosing and the incoming wash water; the trend warns before the limit is reached.'),
    ('pH high','Check the acid dosing pump and acid supply, and which cleaning cycles (e.g. caustic wash) run before these alarms.'),
    ('pH low','Check the caustic dosing pump and caustic supply, and which cleaning cycles (e.g. acid wash) run before these alarms.'),
    ('EQ level high','Check the transfer to the DAF and the discharge pump; consider whether production wash-downs can be spread out.'),
    ('EQ tank FULL','Check the transfer to the DAF and the discharge pump; consider whether production wash-downs can be spread out.'),
    ('No discharge flow','Check the discharge pump, valves and the flow meter.'),
    ('Data stopped','Check the Ewon internet connection and power; repeated stops at the same time can point to a router or power schedule.'),
    ('Sludge tank FULL','Sludge fills faster than the pickups empty it: consider an extra pickup or check the DAF sludge settings.'),
    ('E-Stop','Find out why the E-Stop is being pressed; repeated stops may point to an equipment or training issue.'),
    ('pump faulted','Inspect the pump and its motor overloads; repeated faults usually need maintenance, not a reset.'),
    ('motor faulted','Inspect the motor and its VFD (fault codes, overloads, mechanical load).'),
    ('not dosing','Check the dosing pump, its chemical drum and suction line, and the PLC setpoint.'),
    ('stock low','Order the chemical; see Chemical stock on the Reports page.'),
)
def insight_action(kind):
    for key,text in INSIGHT_ACTIONS:
        if key.lower() in kind.lower():return text
    return 'Review the alarm details on the Alarms page and the equipment involved.'

def insights(cfg,days=30,now=None):
    """Repeated problems in the last `days` days, with suggested operator actions. Advisory only: nothing here
    changes the plant or the chemical dosing."""
    zone=ZoneInfo('America/Vancouver');now=now or datetime.now(UTC);start=now-timedelta(days=days)
    out=[]
    groups={}
    for h in alarm_history(start,now):
        groups.setdefault(h['kind'],[]).append(h)
    for kind,hs in sorted(groups.items(),key=lambda kv:-len(kv[1])):
        n=len(hs)
        if n<3:continue
        local=[parse(h['stamp']).astimezone(zone) for h in hs]
        hours={};weekdays={}
        for t in local:
            blk=t.hour//3*3;hours[blk]=hours.get(blk,0)+1;weekdays[t.strftime('%A')]=weekdays.get(t.strftime('%A'),0)+1
        blk,hn=max(hours.items(),key=lambda kv:kv[1]);wd,wn=max(weekdays.items(),key=lambda kv:kv[1])
        mins=[(parse(h['cleared'])-parse(h['stamp'])).total_seconds()/60 for h in hs if h.get('cleared')]
        detail=f'{n} times in the last {days} days, last on {local[-1]:%b %d %H:%M}.'
        if hn>=max(3,n*0.5):detail+=f' {hn} of them between {blk:02d}:00 and {blk+3:02d}:00.'
        if wn>=max(3,n*0.5):detail+=f' {wn} on a {wd}.'
        if mins:detail+=f' Average {sum(mins)/len(mins):.0f} minutes until it cleared.'
        out.append({'title':f'Repeated alarm: {kind}','detail':detail,'action':insight_action(kind),'severity':'high' if n>=10 else 'medium'})
    db=database()
    try:exc,_=ph_excursions(db,start,now,cfg)
    finally:db.close()
    if exc:
        mins=sum(e['minutes'] for e in exc);litres=sum(e['litres'] for e in exc)
        sides={s:sum(1 for e in exc if e['side']==s) for s in ('high','low')}
        hours={}
        for e in exc:
            blk=e['start'].astimezone(zone).hour//3*3;hours[blk]=hours.get(blk,0)+1
        blk,hn=max(hours.items(),key=lambda kv:kv[1])
        detail=(f"{len(exc)} periods outside {cfg['ph_low']:.2f}–{cfg['ph_high']:.2f} ({sides['high']} high, {sides['low']} low), "
                f"{mins:.0f} minutes in total, {litres:,.0f} L discharged during them.")
        if len(exc)>=3 and hn>=len(exc)*0.5:detail+=f' {hn} of them started between {blk:02d}:00 and {blk+3:02d}:00.'
        side='high' if sides['high']>=sides['low'] else 'low'
        out.append({'title':'pH outside the discharge limits','detail':detail,'severity':'high' if mins>=60 else 'medium',
                    'action':insight_action('pH '+side)+' The Daily discharge compliance report lists each excursion.'})
    if not out:
        out.append({'title':'No repeated problems','detail':f'No alarm happened 3 or more times and pH stayed inside the limits in the last {days} days.',
                    'action':'Nothing to do.','severity':'ok'})
    return out

# ---------------------------------------------------------------- pump run hours and starts
PUMP_TAGS=(('SUMP_PUMP_STATUS','Electric transfer (sump) pump'),('Mixer_Pump_Staus','Electric circulation (mixer) pump'))
def pump_usage(start,end,zone):
    """{tag: {day: [run hours, starts]}}: each on/off reading holds until the next (Ewon logs on change)."""
    end=min(end,datetime.now(UTC));out={}
    db=database()
    try:
        for tag,_ in PUMP_TAGS:
            acc={d:[0.0,0] for d in local_days(start,end,zone)};was=None
            pts=held_points(db,tag,start,end)
            for i,(t,v) in enumerate(pts):
                on=v!=0
                if on and was is False:   # OFF -> ON is a start
                    d=t.astimezone(zone).date()
                    if d in acc:acc[d][1]+=1
                if on:
                    for day,a,b in day_segments(t,pts[i+1][0] if i+1<len(pts) else end,zone):
                        if day in acc:acc[day][0]+=(b-a).total_seconds()/3600
                was=on
            out[tag]=acc
    finally:db.close()
    return out

def pump_rows(start,end,zone,date_range):
    B=lambda t:(t,3);num=lambda v:(round(v,2),2)
    use=pump_usage(start,end,zone);days=local_days(start,min(end,datetime.now(UTC)),zone)
    rows=[[B('Pump run hours and starts')],['Date range (America/Vancouver)',date_range],[],
          [B('Pump'),B('Tag'),B('Run hours'),B('Starts'),B('Average minutes per start')]]
    for tag,label in PUMP_TAGS:
        h=sum(v[0] for v in use[tag].values());n=sum(v[1] for v in use[tag].values())
        rows.append([label,tag,num(h),n,num(h*60/n) if n else ''])
    rows+=[[],[B('Date')]+[x for tag,label in PUMP_TAGS for x in (B(f'{label} run hours'),B(f'{label} starts'))]]
    rows+=[[d]+[x for tag,_ in PUMP_TAGS for x in (num(use[tag].get(d,[0,0])[0]),use[tag].get(d,[0,0])[1])] for d in days]
    rows+=[[],['Run hours: time the pump status was ON (SUMP_PUMP_STATUS B3:1/9 = 1; Mixer_Pump_Staus N142:0 not 0). Starts: OFF to ON changes. '
               'Each reading is held until the next one because the Ewon logs on change; the first day uses the last reading before the range.']]
    return rows

# ---------------------------------------------------------------- chemical stock
# Chemical products (CESCO). Only the coagulant and flocculant dosing pumps report their rate to the PLC,
# so caustic and SA-93 usage comes from tank counts alone. Containers: 55 US gal drum, 5 US gal pail.
GAL_L=3.785411784
CHEM_PRODUCTS={
    'caustic':dict(name='CESCO NEUTRO 5045 50% Caustic',part='FNEUT5045-Dr55',container='55 gal drum',container_l=55*GAL_L),
    'floc':dict(name='CESCO PF 875AH Flocculant',part='FPF875AH-P5',container='5 gal pail',container_l=5*GAL_L),
    'coag':dict(name='CESCO PROTECT 7000 Coagulant',part='FPROT7000-Dr55',container='55 gal drum',container_l=55*GAL_L),
    'sa93':dict(name='CESCO SA-93',part='FSA93-P5',container='5 gal pail',container_l=5*GAL_L)}
# key: (short label, dosing pump tag or None, low-stock setting)
STOCK_CHEMS={'caustic':('Caustic (NEUTRO 5045)',None,'stock_low_caustic_l'),'floc':('Flocculant (PF 875AH)','Floc_Pump_Run','stock_low_floc_l'),
             'coag':('Coagulant (PROTECT 7000)','Coag_Pump_Run','stock_low_coag_l'),'sa93':('SA-93',None,'stock_low_sa93_l')}
def stock_entries(conn,key):
    """Tank counts and deliveries of one product, oldest first: (kind, litres, at). A purchase counts as a
    delivery once it has a received date; older 'delivery' entries in chemical_stock still count too."""
    return [tuple(r) for r in conn.execute(
        "SELECT kind,litres,at FROM chemical_stock WHERE chemical=? "
        "UNION ALL SELECT 'delivery',litres,received_at FROM chemical_purchases WHERE chemical=? AND received_at IS NOT NULL ORDER BY 3",(key,key))]

def stock_status(conn,now=None):
    """Estimated litres in stock per chemical: last tank count (or 0 at the first entry) + deliveries since − pump usage since
    (coagulant and flocculant only; caustic and SA-93 change only when a new tank count is logged)."""
    now=now or datetime.now(UTC);zone=ZoneInfo('America/Vancouver');out={}
    for key,(label,tag,_) in STOCK_CHEMS.items():
        entries=stock_entries(conn,key)
        if not entries:out[key]=dict(label=label,tag=tag,litres=None,**CHEM_PRODUCTS[key]);continue
        counts=[e for e in entries if e[0]=='count']
        base_t,base=(counts[-1][2],counts[-1][1]) if counts else (entries[0][2],0.0)
        # after a tank count, only later deliveries add; without one, every delivery counts
        added=sum(l for k,l,at in entries if k=='delivery' and (at>base_t if counts else at>=base_t))
        t0=parse(base_t)
        used=sum(chemical_usage(t0,now,zone)[tag].values()) if tag and t0<now else 0.0
        week=sum(chemical_usage(max(t0,now-timedelta(days=7)),now,zone)[tag].values()) if tag and t0<now else 0.0
        span=max((now-max(t0,now-timedelta(days=7))).total_seconds()/86400,1/24)
        per_day=week/span if tag else None
        litres=base+added-used
        out[key]=dict(label=label,tag=tag,**CHEM_PRODUCTS[key],litres=litres,since=base_t,base=base,added=added,used=used,per_day=per_day,
                      days_left=(litres/per_day if per_day and per_day>0.01 and litres>0 else None))
    return out

def chemical_consumption(year,cfg):
    """Monthly delivered and used litres per product for a calendar year (America/Vancouver).
    Usage from tank counts: previous count + deliveries in between − next count, booked to the month of the
    next count. Usage from the dosing pump (coagulant and flocculant only): Coag_Pump_Run / Floc_Pump_Run.
    Cost uses the price per container in Settings (blank when no price is set)."""
    zone=ZoneInfo('America/Vancouver')
    start=datetime(year,1,1,tzinfo=zone).astimezone(UTC);end=datetime(year+1,1,1,tzinfo=zone).astimezone(UTC)
    month=lambda t:parse(t).astimezone(zone).month if isinstance(t,str) else t.astimezone(zone).month
    prices=cfg.get('chemical_prices') or {}
    out={}
    with appdb() as conn:
        for key,(label,tag,_) in STOCK_CHEMS.items():
            m={i:dict(delivered=0.0,counted=None,pump=None) for i in range(1,13)}
            rows=[dict(kind=k,litres=l,at=at) for k,l,at in stock_entries(conn,key)]
            for r in rows:
                if r['kind']=='delivery' and iso(start)<=r['at']<iso(end):m[month(r['at'])]['delivered']+=r['litres']
            counts=[r for r in rows if r['kind']=='count']
            for a,b in zip(counts,counts[1:]):
                if not iso(start)<=b['at']<iso(end):continue
                between=sum(r['litres'] for r in rows if r['kind']=='delivery' and a['at']<r['at']<=b['at'])
                mm=m[month(b['at'])];mm['counted']=(mm['counted'] or 0.0)+a['litres']+between-b['litres']
            if tag and start<datetime.now(UTC):
                for day,litres in chemical_usage(start,end,zone)[tag].items():
                    mm=m[day.month];mm['pump']=(mm['pump'] or 0.0)+litres
            price=prices.get(key);size=CHEM_PRODUCTS[key]['container_l']
            for mm in m.values():
                mm['used']=mm['pump'] if tag else mm['counted']
                mm['cost_delivered']=mm['delivered']/size*price if price is not None else None
                mm['cost_used']=mm['used']/size*price if price is not None and mm['used'] is not None else None
            out[key]=m
    return out

def consumption_rows(year,cfg):
    B=lambda t:(t,3);num=lambda v:'' if v is None else (round(v,2),2)
    data=chemical_consumption(year,cfg);prices=cfg.get('chemical_prices') or {}
    tot=lambda key,f:(sum(mm[f] for mm in data[key].values() if mm[f] is not None) if any(mm[f] is not None for mm in data[key].values()) else None)
    rows=[[B(f'Chemical consumption {year}')],['Year (America/Vancouver)',year],[],
          [B('Product'),B('Part #'),B('Container'),B('Litres per container'),B('Price per container'),B('Usage measured by')]]
    for key,p in CHEM_PRODUCTS.items():
        rows.append([p['name'],p['part'],p['container'],num(p['container_l']),num(prices.get(key)) or 'not set',
                     'dosing pump (PLC)' if STOCK_CHEMS[key][1] else 'tank counts'])
    rows+=[[],[B(f'Year {year} totals'),B('Delivered (L)'),B('Delivered (containers)'),B('Used (L)'),B('Used (containers)'),B('Cost of deliveries'),B('Cost of usage')]]
    for key,p in CHEM_PRODUCTS.items():
        size=p['container_l'];d=tot(key,'delivered');u=tot(key,'used')
        rows.append([p['name'],num(d),num(d/size if d is not None else None),num(u),num(u/size if u is not None else None),num(tot(key,'cost_delivered')),num(tot(key,'cost_used'))])
    rows+=[[],[B('Month'),B('Product'),B('Delivered (L)'),B('Delivered (containers)'),B('Used per tank counts (L)'),B('Used per dosing pump (L)'),
               B('Used (containers)'),B('Cost of deliveries'),B('Cost of usage')]]
    for i in range(1,13):
        for key,p in CHEM_PRODUCTS.items():
            mm=data[key][i];size=p['container_l']
            rows.append([f'{i:02d}/{year}',p['name'],num(mm['delivered']),num(mm['delivered']/size),num(mm['counted']),
                         num(mm['pump']) if STOCK_CHEMS[key][1] else 'no PLC tag',num(mm['used']/size if mm['used'] is not None else None),
                         num(mm['cost_delivered']),num(mm['cost_used'])])
    rows+=[[],['Used per tank counts: previous tank count + deliveries in between − next tank count, booked to the month of the later count. '
               'Used per dosing pump: from Coag_Pump_Run and Floc_Pump_Run (coagulant and flocculant only). '
               'Used (containers) and cost of usage use the dosing pump figure where there is one, otherwise the tank counts. '
               'Prices are the price per container in Settings; costs are blank until a price is entered.']]
    return rows

def check_stock(conn,cfg,now=None):
    out={};st=stock_status(conn);stamp=(now or datetime.now(UTC)).strftime('%Y-%m-%dT%H:%M')
    for key,(label,tag,limit_key) in STOCK_CHEMS.items():
        s=st[key];low=cfg[limit_key];known=s['litres'] is not None
        out['stock:'+key]=update_condition(conn,'stock:'+key,cfg['stock_alarm_enabled'] and low>0,
            (s['litres']<low) if known else None,stamp,f'{label} stock low',
            f"{label} estimated stock {s['litres']:.1f} L is below {low:.1f} L. Order more {label.lower()}." if known else '',
            f'{label} stock restored',f"estimated stock {s['litres']:.1f} L" if known else '')
    return out

def save_stock(conn,body,who):
    chem=body.get('chemical');kind=body.get('kind')
    if chem not in STOCK_CHEMS:raise AccountError('Choose the chemical')
    if kind not in ('delivery','count'):raise AccountError('Choose delivery or tank count')
    litres=body.get('litres')
    if type(litres) not in (int,float) or not 0<=litres<=100000 or (kind=='delivery' and litres<=0):raise AccountError('Litres: 0–100000 (a delivery must be more than 0)')
    when=body.get('when')
    try:t=datetime.strptime(when,'%Y-%m-%dT%H:%M').replace(tzinfo=ZoneInfo('America/Vancouver')).astimezone(UTC)
    except (TypeError,ValueError):raise AccountError('Enter the date and time')
    now=datetime.now(UTC)
    if t>now+timedelta(minutes=10):raise AccountError('The time is in the future')
    if t<now-timedelta(days=366):raise AccountError('The time is more than a year ago')
    note=_cal_text(body.get('note'),'Note',200)
    cur=conn.execute('INSERT INTO chemical_stock(chemical,kind,litres,at,note,logged_by,logged_at) VALUES (?,?,?,?,?,?,?)',
                     (chem,kind,float(litres),iso(t),note,who,iso(now)))
    return cur.lastrowid,STOCK_CHEMS[chem][0],kind,float(litres),t

# ---------------------------------------------------------------- chemical purchases
def _local_dt(v,label):
    try:return datetime.strptime(v,'%Y-%m-%dT%H:%M').replace(tzinfo=ZoneInfo('America/Vancouver')).astimezone(UTC)
    except (TypeError,ValueError):raise AccountError(f'Enter the {label}')

def _check_received(t,purchased_on):
    now=datetime.now(UTC)
    if t>now+timedelta(minutes=10):raise AccountError('The received time is in the future')
    if t.astimezone(ZoneInfo('America/Vancouver')).date()<purchased_on:raise AccountError('The received date is before the purchase date')

def save_purchase(conn,body,who):
    chem=body.get('chemical')
    if chem not in CHEM_PRODUCTS:raise AccountError('Choose the chemical')
    try:bought=date.fromisoformat(body.get('purchased_on') or '')
    except (TypeError,ValueError):raise AccountError('Enter the purchase date')
    today=datetime.now(ZoneInfo('America/Vancouver')).date()
    if bought>today:raise AccountError('The purchase date is in the future')
    if bought<date(2020,1,1):raise AccountError('The purchase date is too far in the past')
    litres=body.get('litres')
    if type(litres) not in (int,float) or not 0<litres<=100000:raise AccountError('Quantity must be more than 0')
    cost=body.get('cost')
    if cost is not None and (type(cost) not in (int,float) or not 0<=cost<=10000000):raise AccountError('Total cost: 0–10000000, or leave blank')
    po=_cal_text(body.get('po'),'PO / invoice number',60);supplier=_cal_text(body.get('supplier'),'Supplier',80)
    note=_cal_text(body.get('note'),'Note',200)
    received=None
    if body.get('received'):
        received=_local_dt(body['received'],'received date and time');_check_received(received,bought)
    cur=conn.execute('INSERT INTO chemical_purchases(chemical,purchased_on,received_at,litres,cost,po,supplier,note,logged_by,logged_at,received_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (chem,bought.isoformat(),iso(received) if received else None,float(litres),None if cost is None else float(cost),po,supplier,note,who,iso(datetime.now(UTC)),who if received else None))
    return cur.lastrowid,CHEM_PRODUCTS[chem]['name'],bought,float(litres),received

def receive_purchase(conn,body,who):
    pid=body.get('id')
    if type(pid)!=int or pid<1:raise ValueError('Invalid purchase ID')
    row=conn.execute('SELECT * FROM chemical_purchases WHERE id=?',(pid,)).fetchone()
    if not row:raise AccountError('Purchase not found')
    if row['received_at']:raise AccountError('This purchase is already marked received')
    t=_local_dt(body.get('received'),'received date and time');_check_received(t,date.fromisoformat(row['purchased_on']))
    conn.execute('UPDATE chemical_purchases SET received_at=?,received_by=? WHERE id=?',(iso(t),who,pid))
    return row,t

def purchase_records(conn,start=None,end=None):
    q='SELECT * FROM chemical_purchases';args=()
    if start:q+=' WHERE purchased_on>=? AND purchased_on<?';args=(start.isoformat(),end.isoformat())
    return [dict(r) for r in conn.execute(q+' ORDER BY purchased_on DESC,id DESC',args)]

def purchase_cost(r,prices):
    """(cost, estimated?) of a purchase: the entered total, else containers x price per container in Settings."""
    if r['cost'] is not None:return r['cost'],False
    price=(prices or {}).get(r['chemical'])
    return (r['litres']/CHEM_PRODUCTS[r['chemical']]['container_l']*price,True) if price is not None else (None,False)

def purchase_rows(year,cfg):
    """Excel rows: chemical purchases of a calendar year by month, quarter and year, plus the purchase list.
    Purchases are booked by their purchase date."""
    B=lambda t:(t,3);num=lambda v:'' if v is None else (round(v,2),2);zone=ZoneInfo('America/Vancouver')
    prices=cfg.get('chemical_prices') or {}
    with appdb() as conn:
        recs=purchase_records(conn,date(year,1,1),date(year+1,1,1))
        years=[r[0] for r in conn.execute("SELECT DISTINCT substr(purchased_on,1,4) FROM chemical_purchases ORDER BY 1")]
        allrecs=purchase_records(conn)
    def totals(items):
        t={k:dict(n=0,l=0.0,c=0.0,cost=None,est=False) for k in CHEM_PRODUCTS}
        for r in items:
            x=t[r['chemical']];x['n']+=1;x['l']+=r['litres'];x['c']+=r['litres']/CHEM_PRODUCTS[r['chemical']]['container_l']
            cost,est=purchase_cost(r,prices)
            if cost is not None:x['cost']=(x['cost'] or 0.0)+cost;x['est']=x['est'] or est
        return t
    def block(title,groups):
        out=[[B(title),B('Product'),B('Purchases'),B('Containers'),B('Litres'),B('Cost')]]
        for label,items in groups:
            t=totals(items);grand=None
            for k,p in CHEM_PRODUCTS.items():
                x=t[k]
                if not x['n']:continue
                out.append([label,p['name'],x['n'],num(x['c']),num(x['l']),num(x['cost']) if x['cost'] is None or not x['est'] else f"{x['cost']:.2f} (estimated)"])
                if x['cost'] is not None:grand=(grand or 0.0)+x['cost']
            out.append([label,B('Total'),sum(x['n'] for x in t.values()),'','',num(grand)])
        return out+[[]]
    month=lambda r:int(r['purchased_on'][5:7])
    rows=[[B(f'Chemical purchases {year}')],['Booked by purchase date (America/Vancouver)'],[]]
    rows+=block(f'Year {year}',[(str(year),recs)])
    rows+=block('Quarter',[(f'{year} Q{q}',[r for r in recs if (month(r)-1)//3==q-1]) for q in range(1,5)])
    rows+=block('Month',[(f'{m:02d}/{year}',[r for r in recs if month(r)==m]) for m in range(1,13)])
    rows+=block('All years',[(y,[r for r in allrecs if r['purchased_on'][:4]==y]) for y in years])
    rows+=[[B('Purchased'),B('Received (America/Vancouver)'),B('Days to receive'),B('Product'),B('Part #'),B('Containers'),B('Litres'),
            B('Cost'),B('PO / invoice'),B('Supplier'),B('Note'),B('Logged by')]]
    for r in sorted(recs,key=lambda r:(r['purchased_on'],r['id'])):
        p=CHEM_PRODUCTS[r['chemical']];cost,est=purchase_cost(r,prices)
        got=parse(r['received_at']).astimezone(zone) if r['received_at'] else None
        rows.append([date.fromisoformat(r['purchased_on']),got.replace(tzinfo=None) if got else 'not received yet',
                     (got.date()-date.fromisoformat(r['purchased_on'])).days if got else '',p['name'],p['part'],
                     num(r['litres']/p['container_l']),num(r['litres']),'' if cost is None else (f'{cost:.2f} (estimated)' if est else num(cost)),
                     r['po'],r['supplier'],r['note'],r['logged_by']])
    if not recs:rows.append([f'No chemical purchases logged in {year}.'])
    rows+=[[],['Cost is the total entered for the purchase. When it was left blank, it is estimated from the price per container in Settings '
               '(marked "estimated"), or left empty when no price is set. A purchase adds to the chemical stock from its received date.']]
    return rows

# ---------------------------------------------------------------- alarm shelving
SHELF_MAX_HOURS=72
def shelf_choices():
    return {'ph':'pH alarm','ph_trend_high':'pH trending high','ph_trend_low':'pH trending low','data':'Data stopped','level':'EQ level high','flow':'No discharge flow',
            'dosing:coag':'Coagulant pump not dosing','dosing:floc':'Flocculant pump not dosing',
            **{'plc:'+t:k for t,k,_,_,_ in PLC_ALARMS},**{'stock:'+k:f'{l} stock low' for k,(l,_,_) in STOCK_CHEMS.items()}}

def active_shelves(conn):
    now=iso(datetime.now(UTC))
    return {r['key']:dict(r) for r in conn.execute('SELECT * FROM shelves WHERE until>? ORDER BY until',(now,))}

# ---------------------------------------------------------------- text messages (email-to-SMS)
def sms_text(subject,eid,now):
    return f'{subject}. Incident #{eid} at {now:%b %d %H:%M}. See the dashboard Alarms page.'[:300]

def scheduler():
    now=datetime.now(ZoneInfo('America/Vancouver'))
    with appdb() as conn:
        cfg=settings(conn)
        current=summary();readings=current['tags']
        ph=next((r for r in readings if r['name']=='pH_Reading'),None)
        update_ph_incidents(conn,cfg,ph)
        conditions=check_conditions(conn,cfg,readings,now)
        conditions.update(check_plc_alarms(conn,cfg,current.get('extras')))
        conditions.update(check_stock(conn,cfg))
        shelved=active_shelves(conn)
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
        jobs=[]   # (delivery key, subject, body, attachment builder or None[, alarm key for shelving and texts])
        if eligible:
            tracker=conn.execute('SELECT * FROM ph_alarm_v2 WHERE id=1').fetchone()
            if tracker and tracker['active_event'] and tracker['active_kind'] in ('high','low') and ph_status(ph,cfg)==tracker['active_kind']:
                eid=tracker['active_event']
                jobs.append((f'alarm:incident:{eid}',f'Wastewater pH {tracker["active_kind"].upper()} alarm',
                    f'Confirmed pH incident #{eid}\nCurrent reading: {ph["value"]:.2f} pH\nLow: {cfg["ph_low"]:.2f}\nHigh: {cfg["ph_high"]:.2f}\nTimestamp: {now.isoformat()}',None,'ph'))
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
                        (CONDITION_SUBJECTS[key]+'\n\n' if key in RECOVERY_SUBJECTS else '')+
                        f'Incident #{eid}\n{ev["detail"] if ev else ""}\nTimestamp: {now.isoformat()}',None,key))
                if st.get('pending') and cfg['email_recovery_enabled']:
                    eid,rid=st['pending']
                    delivered=conn.execute('SELECT 1 FROM delivery WHERE key=?',(f'alarm:incident:{eid}',)).fetchone()
                    rec=conn.execute('SELECT kind,detail,stamp FROM events WHERE id=?',(rid,)).fetchone()
                    if delivered and rec and datetime.now(UTC)-parse(rec['stamp'])<timedelta(hours=24):
                        subject=RECOVERY_SUBJECTS.get(key,f'Wastewater {rec["kind"]}')
                        jobs.append((f'recovery:incident:{eid}',f'{subject} — incident #{eid}',
                            (subject+'\n\n' if key in RECOVERY_SUBJECTS else '')+f'{rec["detail"]}\nTimestamp: {now.isoformat()}',None))
        # Calibration reminders: once when a sensor is due within the reminder days, once when overdue.
        if cfg['cal_reminders_enabled'] and hhmm==cfg['daily_time']:
            for key,c in calibration_status(conn,cfg,now.date()).items():
                if c['state'] in ('soon','overdue'):
                    when='is OVERDUE' if c['state']=='overdue' else f"is due in {c['days_left']} day{'s' if c['days_left']!=1 else ''}"
                    jobs.append((f"caldue:{key}:{c['last_id']}:{c['state']}",f"Wastewater calibration reminder: {c['label']} {when}",
                        f"{c['label']} ({c['tag']}) calibration {when}.\nLast calibrated: {c['last']} by {c['last_by']}\nDue: {c['due']} "
                        f"(every {c['every_days']} days)\n\nLog the new calibration and certificate on the dashboard Audit page.",None))
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
        sms=cfg['sms_enabled'] and cfg['sms_recipients']
        for key,subject,body,build,*rest in jobs:
            alarm=rest[0] if rest else None
            if alarm in shelved:continue   # shelved: no email or text while the shelf lasts (sent later if still active)
            sends=[(key,lambda s=subject,b=body,bd=build:send_mail(mail,s,b,cfg['recipients'],[bd()] if bd else ()),'Email')]
            if sms and alarm and key.startswith('alarm:'):
                eid=key.rsplit(':',1)[-1]
                sends.append(('sms:'+key,lambda s=subject,e=eid:send_mail(mail,s[:60],sms_text(s,e,now),cfg['sms_recipients']),'Text message'))
            for dkey,send,what in sends:
                if conn.execute('SELECT 1 FROM delivery WHERE key=?',(dkey,)).fetchone():
                    continue
                try:
                    send()
                    conn.execute('INSERT INTO delivery(key,stamp,outcome) VALUES (?,?,?)',
                                 (dkey,iso(datetime.now(UTC)),'sent'))
                    audit(conn,'system',f'{what} delivered',dkey)
                except Exception as exc:
                    audit(conn,'system',f'{what} failed',dkey+' '+type(exc).__name__)
                    print(f'{what} failed:',type(exc).__name__,file=sys.stderr)
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
    daily=period=='daily'
    out=[f'Wastewater {period.title()} Historical Report',
         f'Period: {start:%Y-%m-%d %I:%M %p} through {end:%Y-%m-%d %I:%M %p} Pacific',
         'pH average; other readings maximum (converted to display units)' if daily else
         'Sample count, minimum, average, maximum (converted to display units)', '']
    with database() as conn:
        for name,(label,unit,factor) in TAGS.items():
            row=conn.execute('SELECT COUNT(value),MIN(value),AVG(value),MAX(value) FROM samples WHERE name=? AND stamp>=? AND stamp<?',
                             (name,iso(start),iso(end))).fetchone()
            count,mn,avg,mx=row
            if daily and name=='Total_Flow_Li':label='Daily Total Flow'
            if not count:out.append(f'{label}: no readings')
            elif daily and name=='pH_Reading':out.append(f'{label} ({unit}): avg {avg*factor:.2f}')
            elif daily:out.append(f'{label} ({unit}): max {mx*factor:.2f}')
            else:out.append(f'{label} ({unit}): count {count:,}; min {mn*factor:.2f}; avg {avg*factor:.2f}; max {mx*factor:.2f}')
    if daily:
        try:
            with appdb() as c:cfg=settings(c)
            out.extend(['',compliance_line(start.date(),cfg)])
        except (OSError,sqlite3.Error):pass
    try:out.extend(['',alarm_report(start.astimezone(UTC),end.astimezone(UTC))])
    except (OSError,sqlite3.Error):out.extend(['','Alarm list unavailable (app database could not be read).'])
    out.extend(['','Report uses recorded samples; missing or invalid-quality data should be reviewed separately.'])
    return '\n'.join(out)

def test_email():
    """Send one test email to the Settings recipients with the server's SMTP config (shell only)."""
    mail=json.loads(EMAIL_CFG.read_text())
    with appdb() as conn:recipients=settings(conn)['recipients']
    if not recipients:sys.exit('No recipients in Settings; add one under Settings > Email recipients first.')
    send_mail(mail,'Wastewater dashboard test email',
              f'Test email from the wastewater dashboard.\nSender: {mail["sender"]}\nSMTP: {mail["host"]}:{mail.get("port",587)}\n',recipients)
    print('Test email sent from',mail['sender'],'to',', '.join(recipients))

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
    elif len(sys.argv)>1 and sys.argv[1]=='--test-email':test_email()
    elif len(sys.argv)>1 and sys.argv[1]=='--backup':backup(sys.argv[2] if len(sys.argv)>2 else None)
    else: print('Usage: python3 web.py --init|--scheduler|--backup [DIR]|--set-admin USERNAME|--set-operator USERNAME')
