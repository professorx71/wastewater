"""Regression checks using only temporary databases. Does not contact live VPS or SMTP."""
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import web

class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        web.APP_DB=Path(self.tmp.name)/'app.sqlite3'
        web.DB=Path(self.tmp.name)/'history.sqlite3'
        web.AUTH=Path(self.tmp.name)/'admin.json'
        with sqlite3.connect(web.DB) as db:
            db.executescript('''CREATE TABLE state(key TEXT,value TEXT);
                CREATE TABLE samples(tag_id TEXT,name TEXT,stamp TEXT,value REAL,quality TEXT,received_at TEXT);
                CREATE TABLE alarms(tag_id TEXT,payload TEXT,received_at TEXT);''')
            stamp=web.iso(datetime.now(timezone.utc)-timedelta(seconds=20))
            for name, val in [('pH_Reading',6.76),('EQ_Tank_Level_Ft',2.78),('FLOW_RATE',0.030656),('Total_Flow_Li',6524.04)]:
                db.execute('INSERT INTO samples(name,stamp,value,quality) VALUES(?,?,?,?)',(name,stamp,val,None))
            db.commit()
        with web.appdb() as db: pass

    def sample(self,value,minute,status='current'):
        return {'name':'pH_Reading','value':value,'status':status,'stamp':f'2026-11-16T12:{minute:02d}:00Z'}
    def config(self):
        return dict(web.DEFAULTS, alarms_enabled=True, email_enabled=True, email_recovery_enabled=True)

    def test_mapping_and_summary(self):
        s=web.summary()
        assert s['revision']=='1.2.32'
        tags={r['name']:r for r in s['tags']}
        self.assertAlmostEqual(tags['FLOW_RATE']['value'],0.030656*3.78541,places=6)
        self.assertEqual(tags['pH_Reading']['status'],'current')
        self.assertEqual(len(tags),4)

    def test_confirm_recover_second_incident(self):
        cfg=self.config()
        with web.appdb() as db:
            web.update_ph_incidents(db,cfg,self.sample(9.4,0))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
            web.update_ph_incidents(db,cfg,self.sample(9.3,0)) # Same timestamp does not double count
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
            web.update_ph_incidents(db,cfg,self.sample(9.4,1))
            first=db.execute("SELECT id FROM events WHERE kind='pH high'").fetchone()['id']
            self.assertTrue(first)
            web.update_ph_incidents(db,cfg,self.sample(6.8,2))
            self.assertEqual(db.execute('SELECT state FROM events WHERE id=?',(first,)).fetchone()[0],'open')
            web.update_ph_incidents(db,cfg,self.sample(6.8,3))
            self.assertEqual(db.execute('SELECT state FROM events WHERE id=?',(first,)).fetchone()[0],'closed')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='pH recovered'").fetchone()[0],1)
            web.update_ph_incidents(db,cfg,self.sample(9.2,4))
            web.update_ph_incidents(db,cfg,self.sample(9.2,5))
            ids=[r['id'] for r in db.execute("SELECT id FROM events WHERE kind='pH high' ORDER BY id")]
            self.assertEqual(len(ids),2)
            self.assertNotEqual(ids[0],ids[1])

    def test_stale_does_not_confirm_and_disabled_does_not_alert(self):
        cfg=self.config()
        with web.appdb() as db:
            web.update_ph_incidents(db,cfg,self.sample(9.8,0,'stale'))
            web.update_ph_incidents(db,cfg,self.sample(9.7,1))
            web.update_ph_incidents(db,cfg,self.sample(9.8,2,'invalid'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
            cfg['alarms_enabled']=False
            web.update_ph_incidents(db,cfg,self.sample(9.9,3))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_settings_validation(self):
        cfg=dict(web.DEFAULTS)
        a=web.val_settings({'alarm_confirm_samples':2,'alarm_recovery_samples':3},cfg)
        self.assertEqual(a['alarm_recovery_samples'],3)
        with self.assertRaises(ValueError):web.val_settings({'alarm_confirm_samples':0},cfg)
        with self.assertRaises(ValueError):web.val_settings({'alarm_confirm_samples':2.5},cfg)
        with self.assertRaises(ValueError):web.val_settings({'ph_low':10,'ph_high':9},cfg)

    def test_history_export_and_summaries(self):
        rows=web.history({},False)
        self.assertEqual(len(rows['series']['FLOW_RATE']),1)
        csv_content=web.history({},True).decode('utf-8-sig')
        self.assertIn('L/min',csv_content)
        self.assertIn('FLOW_RATE',csv_content)

    def test_chemical_usage_litres(self):
        import zipfile
        zone=web.ZoneInfo('America/Vancouver');at=lambda d,h:web.iso(datetime(2026,11,d,h,tzinfo=zone))
        with sqlite3.connect(web.DB) as db:
            db.execute("DELETE FROM samples WHERE name IN ('Coag_Pump_Run','Floc_Pump_Run')")
            for name,stamp,v in [('Coag_Pump_Run',at(16,23),10.0),('Coag_Pump_Run',at(17,1),0.0),
                                 ('Floc_Pump_Run',at(17,12),2.0),('Floc_Pump_Run',at(18,12),0.0)]:
                db.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',(name,stamp,v))
        q={'tag':['chemical'],'startDate':['2026-11-17'],'endDate':['2026-11-18']}
        rows=web.report_csv(q)[1].decode('utf-8-sig').splitlines()
        self.assertEqual(rows[1:],['2026-11-17,0.60,1.44','2026-11-18,0.00,1.44','Total,0.60,2.88'])
        book=zipfile.ZipFile(io.BytesIO(web.report_xlsx(q)[1])).read('xl/workbook.xml').decode()
        self.assertEqual(re.findall(r'<sheet name="([^"]+)"',book),['Chemical Usage','Coag_Pump_Run','Floc_Pump_Run'])

    def test_sludge_pickup_log_and_report(self):
        import zipfile
        web.OP_AUTH=Path(self.tmp.name)/'operator.json'
        self._login(web.AUTH,'admin','admin-pass');self._login(web.OP_AUTH,'op','op-pass')
        zone=web.ZoneInfo('America/Vancouver');now=datetime.now(zone).replace(second=0,microsecond=0)
        day=now-timedelta(days=3)
        at=lambda h,m=0:day.replace(hour=h,minute=m)
        # Tank FULL 08:00, still full until the 11:30 pickup empties it (bit back to 0 at 11:40)
        with sqlite3.connect(web.DB) as db:
            for t,v in [(at(6),0.0),(at(8),1.0),(at(9),1.0),(at(11,40),0.0),(at(20),1.0)]:
                db.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('SLUDGE_TANK_LEVEL',web.iso(t),v))
        A=('admin','admin-pass');O=('op','op-pass')
        # Operator may log pickups by default; Guest may not
        self.assertIn('pickup',json.loads(self.call('GET','/api/summary',*O)[1])['permissions'])
        when=lambda t:t.strftime('%Y-%m-%dT%H:%M')
        self.assertEqual(self.call('POST','/api/pickups',*O,{'when':when(at(11,30)),'note':'Truck 7'})[0],200)
        self.assertEqual(self.call('POST','/api/pickups',*O,{'when':when(at(11,30))})[0],400)          # duplicate
        self.assertEqual(self.call('POST','/api/pickups',*O,{'when':when(now+timedelta(hours=2))})[0],400)  # future
        self.assertEqual(self.call('POST','/api/pickups',*O,{'when':'yesterday'})[0],400)
        self.assertEqual(self.call('POST','/api/accounts/user',*A,{'role':'Guest','username':'g','password':'gst1'})[0],200)
        self.assertEqual(self.call('POST','/api/pickups','g','gst1',{'when':when(at(12))})[0],403)
        rows=json.loads(self.call('GET','/api/pickups','g','gst1')[1])['rows']
        self.assertEqual([(r['note'],r['logged_by']) for r in rows],[('Truck 7','op (Operator)')])
        self.assertEqual(json.loads(self.call('GET','/api/summary',*O)[1])['last_pickup'],rows[0]['picked_at'])
        # Report: pickup 3.5 h after FULL; the 20:00 FULL alarm is not picked up yet
        q={'tag':['sludge'],'startDate':[day.date().isoformat()],'endDate':[now.date().isoformat()]}
        csv_rows=web.report_csv(q)[1].decode('utf-8-sig').splitlines()
        self.assertEqual(csv_rows[1],f"{at(11,30):%Y-%m-%d %H:%M},{at(8):%Y-%m-%d %H:%M},3.50,,op (Operator),Truck 7")
        pickups,alarms=web.sludge_analysis(*web.window(q,92))
        self.assertEqual([(a['full_from'],a['picked']) for a in alarms],[(at(8).astimezone(timezone.utc),at(11,30).astimezone(timezone.utc)),(at(20).astimezone(timezone.utc),None)])
        book=zipfile.ZipFile(io.BytesIO(web.report_xlsx(q)[1]))
        self.assertEqual(re.findall(r'<sheet name="([^"]+)"',book.read('xl/workbook.xml').decode()),['Sludge Pickups','SLUDGE_TANK_LEVEL'])
        self.assertIn('Not picked up yet',book.read('xl/worksheets/sheet1.xml').decode())
        # Removing a pickup is audited
        self.assertEqual(self.call('POST','/api/pickups/remove',*O,{'id':rows[0]['id']})[0],200)
        with web.appdb() as conn:
            acts=[r['action'] for r in conn.execute('SELECT action FROM audit')]
        self.assertIn('Sludge pickup logged',acts);self.assertIn('Sludge pickup removed',acts)
        # Rights saved before the pickup right existed still give Operator the pickup right
        with web.appdb() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('role_permissions',?)",(json.dumps({'Engineering':['audit'],'Operator':['export'],'Guest':[]}),))
        self.assertEqual(sorted(web.role_permissions()['Operator']),['export','pickup','view'])
        self.assertEqual(sorted(web.role_permissions()['Guest']),['view'])
        # ...and once the Admin saves rights without it, it is off
        self.assertEqual(self.call('POST','/api/accounts/permissions',*A,{'Engineering':['audit'],'Operator':['export'],'Guest':[]})[0],200)
        self.assertEqual(sorted(web.role_permissions()['Operator']),['export','view'])

    def test_coag_unit_change_to_ml_per_hour(self):
        import zipfile
        # Coag_Pump_Run switched from ml/min to ml/h at 2026-11-19 18:01 UTC
        with sqlite3.connect(web.DB) as db:
            for stamp,v,q in [('2026-11-19T17:00:00.000000Z',10.0,None),('2026-11-19T18:01:02.000000Z',600.0,'initialGood')]:
                db.execute('INSERT INTO samples(name,stamp,value,quality) VALUES(?,?,?,?)',('Coag_Pump_Run',stamp,v,q))
        self.assertEqual(web.ml_per_min('Coag_Pump_Run','2026-11-19T18:00:59.000000Z',600.0),600.0)
        self.assertEqual(web.ml_per_min('Coag_Pump_Run','2026-11-19T18:01:02.000000Z',600.0),10.0)
        self.assertEqual(web.ml_per_min('Floc_Pump_Run','2026-11-20T00:00:00.000000Z',1.5),1.5)
        # Litres: 10 ml/min held 61 min 2 s, then 600 ml/h (= 10 ml/min) to the end of the Vancouver day
        start=datetime(2026,11,19,17,tzinfo=timezone.utc);end=datetime(2026,11,19,19,tzinfo=timezone.utc)
        # Summary sends ml/min; 'initialGood' counts as a good reading
        real=web.datetime
        class Fixed(real):
            @classmethod
            def now(cls,tz=None):return real(2026,11,19,18,2,tzinfo=timezone.utc)
        web.datetime=Fixed
        try:item=web.summary()['extras']['Coag_Pump_Run']
        finally:web.datetime=real
        class Later(real):
            @classmethod
            def now(cls,tz=None):return real(2026,11,19,20,tzinfo=timezone.utc)
        web.datetime=Later
        try:used=web.chemical_usage(start,end,web.ZoneInfo('America/Vancouver'))['Coag_Pump_Run']
        finally:web.datetime=real
        self.assertAlmostEqual(sum(used.values()),10*120/1000)
        self.assertEqual((item['status'],item['value'],item['raw_value'],item['raw_unit']),('current',10.0,600.0,'ml/h'))
        # Worksheet rows show the unit used at the time
        q={'tag':['Coag_Pump_Run'],'from':[web.iso(start)],'to':[web.iso(end)]}
        rows=web.report_csv(q)[1].decode('utf-8-sig').splitlines()[1:]
        self.assertEqual([r.split(',')[-1] for r in rows],['ml/min','ml/h'])

    def test_excel_report(self):
        import zipfile
        today=datetime.now(web.ZoneInfo('America/Vancouver')).date()
        params={'startDate':[(today-timedelta(days=1)).isoformat()],'endDate':[today.isoformat()]}
        fname,data=web.report_xlsx({**params,'tag':['all']})
        self.assertTrue(fname.endswith('.xlsx'))
        z=zipfile.ZipFile(io.BytesIO(data))
        book=z.read('xl/workbook.xml').decode()
        for name in ('Summary','Chemical Usage','EQ_Tank_Level_Ft','FLOW_RATE','Total_Flow_Li'):self.assertIn(f'name="{name}"',book)
        self.assertNotIn('pH_Reading',book)
        sheets=re.findall(r'<sheet name="([^"]+)"',book)
        flow=z.read(f'xl/worksheets/sheet{sheets.index("FLOW_RATE")+1}.xml').decode()
        self.assertIn('Timestamp America/Vancouver',flow)
        self.assertIn('US gal/min',flow)
        summary=z.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('EOD Daily Value (L)',summary);self.assertNotIn('FLOW_RATE',summary);self.assertIn('Total_Flow_Li',summary)
        self.assertIn('Total Quarterly Value',summary);self.assertIn('Total Monthly Value',summary)
        _,one=web.report_xlsx({**params,'tag':['FLOW_RATE']})
        self.assertEqual(zipfile.ZipFile(io.BytesIO(one)).read('xl/workbook.xml').decode().count('<sheet '),2)
        with self.assertRaises(ValueError):web.report_xlsx({**params,'tag':['nope']})

    def test_excel_summary_eod_total_flow(self):
        import zipfile
        zone=web.ZoneInfo('America/Vancouver')
        rows=[('2026-09-29 18:00',1000.009),                        # before the range: not counted
              ('2026-09-30 10:00',2000.5),('2026-09-30 18:00',524714.2551),('2026-09-30 23:00',0),
              ('2026-10-01 09:00',0),('2026-10-01 20:00',0.5),     # no valid reading: carry 09-30
              ('2026-10-02 12:00',300.129)]
        with sqlite3.connect(web.DB) as db:
            for local,v in rows:
                t=datetime.strptime(local,'%Y-%m-%d %H:%M').replace(tzinfo=zone)
                db.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('Total_Flow_Li',web.iso(t),v))
        _,data=web.report_xlsx({'tag':['FLOW_RATE'],'startDate':['2026-09-30'],'endDate':['2026-10-02']})
        summary=zipfile.ZipFile(io.BytesIO(data)).read('xl/worksheets/sheet1.xml').decode()
        values=[float(v) for v in __import__('re').findall(r's="2"><v>([^<]+)</v>',summary)]
        # quarters (Q3, Q4), months (Sep, Oct), days (09-30, 10-01 carried, 10-02)
        # quarter (total, usage), month (total, usage), day (EOD, usage); 10-02 drop has no usage
        self.assertEqual(values,[524714.25,523714.25,525014.37,0.0, 524714.25,523714.25,525014.37,0.0,
                                 524714.25,523714.25, 524714.25,0.0, 300.12])
        self.assertIn('carried forward from 09/30/2026 18:00',summary)
        self.assertIn('Meter reading dropped',summary)
        self.assertIn('2026 Q3 (Jul-Sep)',summary);self.assertIn('2026 Q4 (Oct-Dec)',summary)
        self.assertEqual(str(web.truncate2(524714.2551)),'524714.25')

    def test_scheduler_incident_email_dedup_and_recovery(self):
        web.EMAIL_CFG=Path(self.tmp.name)/'fake-email.json'
        web.EMAIL_CFG.write_text(json.dumps({'host':'example.invalid','sender':'test@example.com',
                     'username':'fake','password':'fake'}))
        with web.appdb() as db:
            cfg=self.config();cfg['recipients']=['test@example.com']
            for k,v in cfg.items():
                db.execute('INSERT INTO settings(key,value) VALUES(?,?)',(k,json.dumps(v)))
            db.commit()
        data=[self.sample(9.4,0),self.sample(9.5,1),self.sample(9.5,1),
              self.sample(6.7,2),self.sample(6.8,3),self.sample(9.1,4),self.sample(9.3,5)]
        with patch.object(web,'summary',side_effect=[{'tags':[r]} for r in data]),\
             patch.object(web,'send_mail') as delivery:
            for _ in data:web.scheduler()
        self.assertEqual(delivery.call_count,3) # incident 1, recovery, incident 2
        with web.appdb() as db:
            keys=[r[0] for r in db.execute('SELECT key FROM delivery ORDER BY stamp')]
            self.assertEqual(len(keys),3)
            self.assertEqual(set(keys),{'alarm:incident:1','recovery:incident:1','alarm:incident:3'})

    def _login(self,path,user,pwd):
        import hashlib
        salt=os.urandom(16)
        path.write_text(json.dumps({'username':user,'salt':salt.hex(),'iterations':1000,
            'hash':hashlib.pbkdf2_hmac('sha256',pwd.encode(),salt,1000).hex()}))

    def call(self,method,path,user,pwd,body=None,query=''):
        import base64
        data=json.dumps(body).encode() if body is not None else b''
        env={'REQUEST_METHOD':method,'PATH_INFO':path,'QUERY_STRING':query,'HTTP_HOST':'localhost',
             'HTTP_AUTHORIZATION':'Basic '+base64.b64encode(f'{user}:{pwd}'.encode()).decode(),
             'HTTP_X_WW_REQUEST':'1','CONTENT_LENGTH':str(len(data)),'wsgi.input':io.BytesIO(data)}
        out={}
        payload=b''.join(web.application(env,lambda status,headers:out.update(status=status)))
        return int(out['status'][:3]),payload

    def test_role_permissions(self):
        web.OP_AUTH=Path(self.tmp.name)/'operator.json'
        self._login(web.AUTH,'admin','admin-pass')
        self._login(web.OP_AUTH,'op','op-pass')
        with web.appdb() as db:
            db.execute("INSERT INTO events(stamp,kind,detail,state) VALUES ('2026-11-16T12:00:00Z','pH high','x','open')")
        new={'ph_low':6.0}
        # Operator: view data, download CSV, acknowledge alarms
        code,body=self.call('GET','/api/summary','op','op-pass')
        self.assertEqual(code,200)
        info=json.loads(body)
        self.assertEqual(info['role'],'Operator')
        self.assertNotIn('settings',info['permissions'])
        self.assertEqual(self.call('GET','/api/history','op','op-pass')[0],200)
        self.assertEqual(self.call('GET','/api/export','op','op-pass')[0],200)
        self.assertEqual(self.call('GET','/api/events','op','op-pass')[0],200)
        self.assertEqual(self.call('POST','/api/ack','op','op-pass',{'id':1})[0],200)
        # Operator: no settings (read or write) and no audit
        self.assertEqual(self.call('GET','/api/settings','op','op-pass')[0],403)
        self.assertEqual(self.call('POST','/api/settings','op','op-pass',new)[0],403)
        self.assertEqual(self.call('GET','/api/audit','op','op-pass')[0],403)
        with web.appdb() as db:
            self.assertEqual(web.settings(db)['ph_low'],web.DEFAULTS['ph_low'])
        # Admin: everything
        self.assertEqual(self.call('GET','/api/settings','admin','admin-pass')[0],200)
        self.assertEqual(self.call('POST','/api/settings','admin','admin-pass',new)[0],200)
        self.assertEqual(self.call('GET','/api/audit','admin','admin-pass')[0],200)
        with web.appdb() as db:
            self.assertEqual(web.settings(db)['ph_low'],6.0)
        # Wrong password: nothing
        self.assertEqual(self.call('GET','/api/summary','op','wrong')[0],401)
        # Switch user: the current username is refused with a sign-in prompt; a different login returns to '/'
        self.assertEqual(self.call('GET','/switch','op','op-pass',query='from=op')[0],401)
        self.assertEqual(self.call('GET','/switch','admin','admin-pass',query='from=op')[0],302)
        self.assertEqual(self.call('GET','/switch','admin','admin-pass',query='from=admin')[0],401)
        # Audit and acknowledgements name the person
        with web.appdb() as db:
            self.assertEqual(db.execute('SELECT actor FROM events WHERE id=1').fetchone()[0],'op (Operator)')
            self.assertIn('admin (Admin)',[r[0] for r in db.execute('SELECT actor FROM audit')])

    def test_engineering_guest_logins_and_rights(self):
        self._login(web.AUTH,'admin','admin-pass-1')
        A=('admin','admin-pass-1')
        # Admin adds Engineering and Guest logins (Engineering needs 6+ characters)
        self.assertEqual(self.call('POST','/api/accounts/user',*A,{'role':'Engineering','username':'eng','password':'abc12'})[0],400)
        self.assertEqual(self.call('POST','/api/accounts/user',*A,{'role':'Engineering','username':'eng','password':'eng-pass-1'})[0],200)
        self.assertEqual(self.call('POST','/api/accounts/user',*A,{'role':'Guest','username':'visitor','password':'gst1'})[0],200)
        self.assertEqual(self.call('POST','/api/accounts/user',*A,{'role':'Guest','username':'ENG','password':'gst1'})[0],400)
        self.assertEqual(self.call('POST','/api/accounts/user',*A,{'role':'Admin','username':'x2','password':'long-pass'})[0],400)
        info=json.loads(self.call('GET','/api/accounts',*A)[1])
        self.assertEqual(sorted((u['role'],u['username']) for u in info['users']),[('Engineering','eng'),('Guest','visitor')])
        # Default rights: Engineering gets settings and audit, Guest only views
        eng=json.loads(self.call('GET','/api/summary','eng','eng-pass-1')[1])
        self.assertEqual(eng['role'],'Engineering');self.assertIn('settings',eng['permissions']);self.assertNotIn('accounts',eng['permissions'])
        self.assertEqual(self.call('GET','/api/settings','eng','eng-pass-1')[0],200)
        self.assertEqual(self.call('GET','/api/accounts','eng','eng-pass-1')[0],403)
        self.assertEqual(self.call('POST','/api/accounts/permissions','eng','eng-pass-1',{'Engineering':['audit'],'Operator':[],'Guest':[]})[0],403)
        guest=json.loads(self.call('GET','/api/summary','visitor','gst1')[1])
        self.assertEqual(guest['permissions'],['view'])
        self.assertEqual(self.call('GET','/api/export','visitor','gst1',query='startDate=2026-11-01&endDate=2026-11-02')[0],403)
        self.assertEqual(self.call('GET','/api/settings','visitor','gst1')[0],403)
        # Admin changes the rights: Guest may download, Engineering loses Settings
        self.assertEqual(self.call('POST','/api/accounts/permissions',*A,{'Engineering':['export','audit'],'Operator':['export'],'Guest':['nope']})[0],400)
        self.assertEqual(self.call('POST','/api/accounts/permissions',*A,{'Engineering':['export','audit'],'Operator':['export'],'Guest':['export']})[0],200)
        self.assertEqual(json.loads(self.call('GET','/api/summary','visitor','gst1')[1])['permissions'],['export','view'])
        self.assertEqual(self.call('GET','/api/settings','eng','eng-pass-1')[0],403)
        self.assertEqual(self.call('GET','/api/audit','eng','eng-pass-1')[0],200)
        # Removing a login stops its sign-in
        self.assertEqual(self.call('POST','/api/accounts/user/remove',*A,{'role':'Guest','username':'visitor'})[0],200)
        self.assertEqual(self.call('GET','/api/summary','visitor','gst1')[0],401)
        with web.appdb() as conn:
            details=' '.join(r['action'] for r in conn.execute('SELECT action FROM audit'))
        for action in ('Engineering login set','Guest login set','Role rights changed','Guest login removed'):self.assertIn(action,details)

    def test_accounts_admin_and_operator_logins(self):
        web.OP_AUTH=Path(self.tmp.name)/'operator.json'
        self._login(web.AUTH,'admin','admin-pass-1')
        self._login(web.OP_AUTH,'op','op-pass-123')
        A=('admin','admin-pass-1')
        # Only Admin may see or manage accounts
        self.assertEqual(self.call('GET','/api/accounts',*A)[0],200)
        self.assertEqual(self.call('GET','/api/accounts','op','op-pass-123')[0],403)
        self.assertEqual(self.call('POST','/api/accounts/operator','op','op-pass-123',{'username':'x','password':'long-enough-pw'})[0],403)
        # Admin adds a named Operator login: the original operator login keeps working
        self.assertEqual(self.call('POST','/api/accounts/operator',*A,{'username':'shift','password':'abc'})[0],400)  # Operator minimum 4
        self.assertEqual(self.call('POST','/api/accounts/operator',*A,{'username':'ADMIN','password':'long-enough-pw'})[0],400)
        self.assertEqual(self.call('POST','/api/accounts/operator',*A,{'username':'shift','password':'shift-pass-99'})[0],200)
        self.assertEqual(self.call('GET','/api/summary','shift','shift-pass-99')[0],200)
        self.assertEqual(self.call('GET','/api/summary','op','op-pass-123')[0],200)
        # Removing one Operator leaves the others
        self.assertEqual(self.call('POST','/api/accounts/operator/remove',*A,{'username':'op'})[0],200)
        self.assertEqual(self.call('GET','/api/summary','op','op-pass-123')[0],401)
        self.assertEqual(self.call('POST','/api/accounts/operator/remove',*A,{'username':'op'})[0],404)
        # Admin password change needs the current password
        self.assertEqual(self.call('POST','/api/accounts/admin',*A,{'current_password':'wrong','new_password':'new-admin-pass'})[0],403)
        self.assertEqual(self.call('POST','/api/accounts/admin',*A,{'current_password':'admin-pass-1','new_password':'abcde'})[0],400)  # Admin minimum 6
        self.assertEqual(self.call('POST','/api/accounts/admin',*A,{'current_password':'admin-pass-1','new_password':'new-admin-pass'})[0],200)
        self.assertEqual(self.call('GET','/api/summary',*A)[0],401)
        A=('admin','new-admin-pass')
        code,body=self.call('GET','/api/accounts',*A)
        self.assertEqual(code,200)
        info=json.loads(body)
        self.assertEqual(info['admin']['source'],'settings')
        self.assertEqual([o['username'] for o in info['operators']],['shift'])
        self.assertNotIn('hash',body.decode())
        # Turning the Operator off blocks sign-in (file login does not come back)
        self.assertEqual(self.call('POST','/api/accounts/operator/disable',*A,{})[0],200)
        self.assertEqual(self.call('GET','/api/summary','shift','shift-pass-99')[0],401)
        self.assertEqual(self.call('GET','/api/summary','op','op-pass-123')[0],401)
        # Changes are audited without passwords
        with web.appdb() as db:
            details=' '.join(r['action']+' '+r['detail'] for r in db.execute('SELECT * FROM audit'))
        self.assertIn('Operator login disabled',details)
        self.assertNotIn('shift-pass-99',details)
        self.assertNotIn('new-admin-pass',details)

    def test_static_html_has_no_old_embed_sections(self):
        base=Path(web.__file__).parent/'static'
        html=(base/'index.html').read_text()
        self.assertNotIn('id="overviewCharts"',html)
        self.assertNotIn('class="process"',html)
        self.assertIn('id="tankPh"',html)
        self.assertNotIn('id="diagramTotal"',html)
        self.assertTrue((base/'process-overview.webp').stat().st_size>10000)
        # Help page: a tab everyone can open, with a section for every page of the dashboard
        self.assertIn('data-view="help"',html)
        for topic in ('help-overview','help-trends','help-alarms','help-reports','help-audit','help-settings','help-email','help-rights','help-trouble'):
            self.assertIn(f'id="{topic}"',html);self.assertIn(f'href="#{topic}"',html)

    def test_two_operators_switch_and_lockout(self):
        self._login(web.AUTH,'admin','admin-pass')
        A=('admin','admin-pass')
        for name in ('anna','ben'):
            self.assertEqual(self.call('POST','/api/accounts/operator',*A,{'username':name,'password':name+'-pw'})[0],200)
        self.assertEqual(json.loads(self.call('GET','/api/summary','ben','ben-pw')[1])['user'],'ben')
        # Switching from one Operator to another Operator works (compares usernames)
        self.assertEqual(self.call('GET','/switch','ben','ben-pw',query='from=anna')[0],302)
        # The same wrong password repeated (old browser tab) counts once
        for _ in range(20):self.assertEqual(self.call('GET','/api/summary','anna','old-pw')[0],401)
        self.assertEqual(self.call('GET','/api/summary','anna','anna-pw')[0],200)
        # Ten different wrong passwords lock this client, even with the right password
        for i in range(10):self.call('GET','/api/summary','anna',f'guess-{i}')
        self.assertEqual(self.call('GET','/api/summary','anna','anna-pw')[0],429)
        self.assertEqual(self.call('GET','/api/summary','ben','ben-pw')[0],200)   # other users unaffected
        with web.appdb() as db:
            self.assertTrue(db.execute("SELECT 1 FROM audit WHERE action='Sign-in locked'").fetchone())
            db.execute('UPDATE login_failures SET stamp=?',(web.iso(datetime.now(timezone.utc)-timedelta(minutes=16)),))
        self.assertEqual(self.call('GET','/api/summary','anna','anna-pw')[0],200)  # unlocks after 15 minutes

    def test_data_level_flow_alarms(self):
        cfg=dict(web.DEFAULTS,level_alarm_enabled=True,level_high_ft=3.0,flow_alarm_enabled=True,flow_low_lpm=1.0,flow_low_minutes=30,
                 alarm_confirm_samples=2,alarm_recovery_samples=1)
        now=datetime.now(web.ZoneInfo('America/Vancouver'))
        lvl=lambda v,m:{'name':'EQ_Tank_Level_Ft','value':v,'status':'current','stamp':f'2026-11-16T12:{m:02d}:00Z','age_seconds':20}
        with web.appdb() as db:
            web.check_conditions(db,cfg,[lvl(3.5,0)],now)
            web.check_conditions(db,cfg,[lvl(3.6,1)],now)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='EQ level high' AND state='open'").fetchone()[0],1)
            web.check_conditions(db,cfg,[lvl(2.0,2)],now)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='EQ level recovered'").fetchone()[0],1)
            # Data stopped: newest reading older than the limit
            web.check_conditions(db,cfg,[dict(lvl(2.0,2),age_seconds=20*60)],now+timedelta(minutes=1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='Data stopped' AND state='open'").fetchone()[0],1)
            web.check_conditions(db,cfg,[lvl(2.0,3)],now+timedelta(minutes=2))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='Data restored'").fetchone()[0],1)
        # No discharge flow: 30 minutes of near-zero flow
        with sqlite3.connect(web.DB) as h:
            h.execute("DELETE FROM samples WHERE name='FLOW_RATE'")
            for m in range(0,31,5):
                h.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('FLOW_RATE',web.iso(datetime.now(timezone.utc)-timedelta(minutes=m)),0.01))
        with web.appdb() as db:
            web.check_conditions(db,cfg,[lvl(2.0,4)],now+timedelta(minutes=3))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='No discharge flow' AND state='open'").fetchone()[0],1)
            # Turning the alarm off closes the open incident
            web.check_conditions(db,dict(cfg,flow_alarm_enabled=False),[lvl(2.0,5)],now+timedelta(minutes=4))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='No discharge flow' AND state='open'").fetchone()[0],0)

    def test_plc_alarms_latch_auto_ack_and_email(self):
        cfg=dict(web.DEFAULTS)
        bit=lambda v,m:{'value':v,'status':'current','stamp':f'2026-11-16T12:{m:02d}:00Z'}
        with web.appdb() as db:
            web.check_plc_alarms(db,cfg,{'E_STOP_STATUS':bit(1,0),'SLUDGE_TANK_LEVEL':bit(1,0),'MIXER_PUMP_FAULTED':bit(0,0)})
            ev={r['kind']:r for r in db.execute("SELECT * FROM events")}
            self.assertEqual(set(ev),{'E-Stop active','Sludge tank FULL'})
            self.assertTrue(all(r['state']=='open' for r in ev.values()))
            self.assertIn('N7:1/0',ev['E-Stop active']['detail'])
            web.check_plc_alarms(db,cfg,{'E_STOP_STATUS':bit(1,0),'SLUDGE_TANK_LEVEL':bit(1,0)})   # same stamps: no duplicates
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
            # Both clear: the sludge alarm acknowledges itself, the E-Stop waits for an operator
            web.check_plc_alarms(db,cfg,{'E_STOP_STATUS':bit(0,1),'SLUDGE_TANK_LEVEL':bit(0,1)})
            sludge=db.execute("SELECT * FROM events WHERE id=?",(ev['Sludge tank FULL']['id'],)).fetchone()
            self.assertEqual(sludge['state'],'closed');self.assertIn('auto-acknowledged',sludge['actor']);self.assertTrue(sludge['ack_at'])
            estop=db.execute("SELECT * FROM events WHERE id=?",(ev['E-Stop active']['id'],)).fetchone()
            self.assertEqual(estop['state'],'open');self.assertTrue(estop['cleared_at'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind IN ('E-Stop active cleared','Sludge tank FULL cleared')").fetchone()[0],2)
            # An acknowledged fault closes as soon as it clears
            web.check_plc_alarms(db,cfg,{'RAKE_MOTOR_FAULTED':bit(1,2)})
            rid=db.execute("SELECT id FROM events WHERE kind='Rake motor faulted'").fetchone()[0]
            db.execute("UPDATE events SET state='acknowledged',actor='op (Operator)' WHERE id=?",(rid,))
            web.check_plc_alarms(db,cfg,{'RAKE_MOTOR_FAULTED':bit(0,3)})
            self.assertEqual(db.execute('SELECT state FROM events WHERE id=?',(rid,)).fetchone()[0],'closed')
            # Stale readings are not judged
            web.check_plc_alarms(db,cfg,{'MIXER_PUMP_FAULTED':dict(bit(1,4),status='stale')})
            self.assertFalse(db.execute("SELECT 1 FROM events WHERE kind='Mixer pump faulted'").fetchone())
            report=web.alarm_report(datetime(2000,1,1,tzinfo=timezone.utc),datetime.now(timezone.utc)+timedelta(days=1))
        self.assertIn('Alarms in this period: 3',report);self.assertIn('E-Stop active',report);self.assertNotIn('cleared:',report)
        # Acknowledging the cleared E-Stop closes it
        self._login(web.AUTH,'admin','admin-pass')
        self.assertEqual(self.call('POST','/api/ack','admin','admin-pass',{'id':estop['id']})[0],200)
        with web.appdb() as db:
            self.assertEqual(db.execute('SELECT state FROM events WHERE id=?',(estop['id'],)).fetchone()[0],'closed')
        # Emails go out for a new PLC alarm
        web.EMAIL_CFG=Path(self.tmp.name)/'fake-email.json'
        web.EMAIL_CFG.write_text(json.dumps({'host':'example.invalid','sender':'t@example.com','username':'u','password':'p'}))
        with web.appdb() as db:
            for k,v in dict(email_enabled=True,recipients=['t@example.com']).items():
                db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(k,json.dumps(v)))
        current=web.summary();current['extras']['FLOC_COAG_PUMP_FAULTED']=dict(bit(1,5),name='FLOC_COAG_PUMP_FAULTED')
        with patch.object(web,'summary',return_value=current),patch.object(web,'send_mail') as mail:
            web.scheduler();web.scheduler()
        subjects=[c.args[1] for c in mail.call_args_list]
        self.assertEqual(subjects.count('Flocculant and Coagulant Pump Motor Stop Faulted (FLOC_COAG_PUMP_FAULTED)'),1)
        self.assertEqual(set(web.PLC_EMAIL_WORDING),{t for t,*_ in web.PLC_ALARMS})   # every PLC alarm has its wording

    def test_sensor_calibration_log(self):
        import base64,zipfile
        web.OP_AUTH=Path(self.tmp.name)/'operator.json'
        self._login(web.AUTH,'admin','admin-pass');self._login(web.OP_AUTH,'op','op-pass')
        pdf=b'%PDF-1.4 test certificate'
        ok={'sensor':'ph','date':'2026-11-01','name':'Jane Tech','certificate':'CAL-123','note':'pH 4/7/10 buffers',
            'file':{'name':'cert nov.pdf','data':base64.b64encode(pdf).decode()}}
        self.assertEqual(self.call('POST','/api/calibrations','op','op-pass',ok)[0],403)   # Operator: no audit right
        self.assertEqual(self.call('POST','/api/calibrations','admin','admin-pass',ok)[0],200)
        self.assertEqual(self.call('POST','/api/calibrations','admin','admin-pass',{'sensor':'level','date':'2026-10-15','name':'Bob','certificate':'LV-9'})[0],200)
        bad=[dict(ok,sensor='flow'),dict(ok,date='2099-01-01'),dict(ok,name=' '),dict(ok,certificate='',file=None),
             dict(ok,file={'name':'x.exe','data':base64.b64encode(b'MZ...').decode()}),
             dict(ok,file={'name':'big.pdf','data':base64.b64encode(b'%PDF'+b'0'*web.CAL_FILE_MAX).decode()})]
        for body in bad[:-1]:self.assertEqual(self.call('POST','/api/calibrations','admin','admin-pass',body)[0],400,body)
        self.assertIn(self.call('POST','/api/calibrations','admin','admin-pass',bad[-1])[0],(400,))
        code,body=self.call('GET','/api/calibrations','admin','admin-pass');rows=json.loads(body)['rows']
        self.assertEqual(code,200);self.assertEqual([r['sensor'] for r in rows],['ph','level'])
        self.assertEqual(rows[0]['file_name'],'cert nov.pdf');self.assertNotIn('file_data',rows[0])
        code,data=self.call('GET','/api/calibrations/certificate','admin','admin-pass',query=f"id={rows[0]['id']}")
        self.assertEqual((code,data),(200,pdf))
        self.assertEqual(self.call('GET','/api/calibrations/certificate','admin','admin-pass',query=f"id={rows[1]['id']}")[0],404)
        self.assertEqual(self.call('GET','/api/calibrations','op','op-pass')[0],403)
        code,book=self.call('GET','/api/export','admin','admin-pass',query='tag=calibration')
        self.assertEqual(code,200)
        sheet=zipfile.ZipFile(io.BytesIO(book)).read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('Jane Tech',sheet);self.assertIn('CAL-123',sheet);self.assertIn('EQ tank height (level) sensor',sheet)
        code,text=self.call('GET','/api/export','admin','admin-pass',query='tag=calibration&format=csv')
        self.assertEqual(code,200);self.assertIn('LV-9',text.decode('utf-8-sig'))
        self.assertEqual(self.call('POST','/api/calibrations/remove','admin','admin-pass',{'id':rows[1]['id']})[0],200)
        with web.appdb() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calibrations').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action LIKE 'Sensor calibration%'").fetchone()[0],3)

    def test_calibration_due_status_and_reminder_email(self):
        cfg=dict(web.DEFAULTS)
        today=datetime.now(web.ZoneInfo('America/Vancouver')).date()
        with web.appdb() as db:
            self.assertEqual(web.calibration_status(db,cfg)['ph']['state'],'never')
            ins=lambda s,d:db.execute("INSERT INTO calibrations(sensor,calibrated_on,calibrated_by,logged_by,logged_at) VALUES (?,?,?,?,?)",(s,d.isoformat(),'Jane','x','2026-01-01T00:00:00Z'))
            ins('ph',today-timedelta(days=25));ins('level',today-timedelta(days=400))
            st=web.calibration_status(db,cfg)
        self.assertEqual((st['ph']['state'],st['ph']['days_left']),('soon',5))
        self.assertEqual(st['level']['state'],'overdue')
        web.EMAIL_CFG=Path(self.tmp.name)/'fake-email.json'
        web.EMAIL_CFG.write_text(json.dumps({'host':'example.invalid','sender':'t@example.com','username':'u','password':'p'}))
        hhmm=datetime.now(web.ZoneInfo('America/Vancouver')).strftime('%H:%M')
        with web.appdb() as db:
            for k,v in dict(recipients=['t@example.com'],daily_time=hhmm).items():
                db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(k,json.dumps(v)))
        with patch.object(web,'send_mail') as mail:
            web.scheduler();web.scheduler()
        subjects=[c.args[1] for c in mail.call_args_list]
        self.assertIn('Wastewater calibration reminder: pH sensor is due in 5 days',subjects)
        self.assertIn('Wastewater calibration reminder: EQ tank height (level) sensor is OVERDUE',subjects)
        self.assertEqual(len([s for s in subjects if 'calibration reminder' in s]),2)   # once each

    def test_shelving_holds_alarm_email_and_sms_goes_out(self):
        web.EMAIL_CFG=Path(self.tmp.name)/'fake-email.json'
        web.EMAIL_CFG.write_text(json.dumps({'host':'example.invalid','sender':'t@example.com','username':'u','password':'p'}))
        with web.appdb() as db:
            for k,v in dict(email_enabled=True,recipients=['t@example.com'],sms_enabled=True,sms_recipients=['6045550000@sms.example.com']).items():
                db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(k,json.dumps(v)))
            db.execute("INSERT INTO shelves(key,until,reason,shelved_by,shelved_at) VALUES ('plc:E_STOP_STATUS',?,'repair','a','b')",
                       (web.iso(datetime.now(timezone.utc)+timedelta(hours=2)),))
        current=web.summary()
        current['extras']['E_STOP_STATUS']={'value':1,'status':'current','stamp':'2026-11-16T12:00:00Z'}
        with patch.object(web,'summary',return_value=current),patch.object(web,'send_mail') as mail:
            web.scheduler()
            self.assertFalse([c for c in mail.call_args_list if 'Emergency Stop' in c.args[1]])   # shelved
            with web.appdb() as db:db.execute('DELETE FROM shelves')
            web.scheduler();web.scheduler()
        calls=[c for c in mail.call_args_list if 'Emergency Stop' in c.args[1]]
        self.assertEqual(len(calls),2)   # one email, one text, sent once
        self.assertEqual({tuple(c.args[3]) for c in calls},{('t@example.com',),('6045550000@sms.example.com',)})
        self.assertTrue(any('See the dashboard' in c.args[2] for c in calls))
        # Shelving API: needs the settings right and a reason
        web.OP_AUTH=Path(self.tmp.name)/'operator.json'
        self._login(web.AUTH,'admin','admin-pass');self._login(web.OP_AUTH,'op','op-pass')
        self.assertEqual(self.call('POST','/api/shelves','op','op-pass',{'key':'flow','hours':4,'reason':'x'})[0],403)
        self.assertEqual(self.call('POST','/api/shelves','admin','admin-pass',{'key':'flow','hours':4,'reason':''})[0],400)
        self.assertEqual(self.call('POST','/api/shelves','admin','admin-pass',{'key':'flow','hours':99,'reason':'x'})[0],400)
        self.assertEqual(self.call('POST','/api/shelves','admin','admin-pass',{'key':'flow','hours':4,'reason':'pump repair'})[0],200)
        info=json.loads(self.call('GET','/api/summary','op','op-pass')[1])
        self.assertEqual([s['key'] for s in info['shelved']],['flow'])
        self.assertEqual(self.call('POST','/api/shelves/remove','admin','admin-pass',{'key':'flow'})[0],200)

    def test_chemical_stock_estimate_and_low_alarm(self):
        now=datetime.now(timezone.utc)
        with sqlite3.connect(web.DB) as h:   # coagulant pump at 60 ml/h (= 1 ml/min) for the last 10 hours: 0.6 L used
            h.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('Coag_Pump_Run',web.iso(now-timedelta(hours=10)),60.0))
        when=(now-timedelta(hours=10)).astimezone(web.ZoneInfo('America/Vancouver')).strftime('%Y-%m-%dT%H:%M')
        with web.appdb() as db:
            web.save_stock(db,{'chemical':'coag','kind':'count','litres':10,'when':when},'a')
            st=web.stock_status(db)
            self.assertAlmostEqual(st['coag']['litres'],10-0.6,delta=0.02)
            self.assertIsNone(st['floc']['litres'])
            cfg=dict(web.DEFAULTS,stock_low_coag_l=20.0)
            web.check_stock(db,cfg)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='Coagulant (PROTECT 7000) stock low' AND state='open'").fetchone()[0],1)
            web.save_stock(db,{'chemical':'coag','kind':'delivery','litres':50,'when':when},'a')   # same time as the count: not added
            self.assertAlmostEqual(web.stock_status(db)['coag']['litres'],10-0.6,delta=0.02)
            later=(now-timedelta(hours=1)).astimezone(web.ZoneInfo('America/Vancouver')).strftime('%Y-%m-%dT%H:%M')
            web.save_stock(db,{'chemical':'coag','kind':'delivery','litres':50,'when':later},'a')
            self.assertAlmostEqual(web.stock_status(db)['coag']['litres'],60-0.6,delta=0.02)
            web.check_stock(db,cfg,now+timedelta(minutes=1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='Coagulant (PROTECT 7000) stock restored'").fetchone()[0],1)
            for bad in ({'chemical':'x','kind':'count','litres':1,'when':when},{'chemical':'coag','kind':'delivery','litres':0,'when':when},
                        {'chemical':'coag','kind':'count','litres':1,'when':'2099-01-01T00:00'}):
                with self.assertRaises(web.AccountError):web.save_stock(db,bad,'a')

    def test_compliance_pumps_and_alarm_history_reports(self):
        zone=web.ZoneInfo('America/Vancouver')
        day=datetime(2026,11,10,tzinfo=zone)
        u=lambda h,m=0:web.iso(day+timedelta(hours=h,minutes=m))
        with sqlite3.connect(web.DB) as h:
            for hh,mm,v in ((1,0,7.0),(1,10,9.5),(1,20,7.2),(5,0,10.0)):   # 10 min out at 01:10, then 15 min (capped) at 05:00
                h.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('pH_Reading',u(hh,mm),v))
            for hh,v in ((0,1000.0),(12,1500.0),(13,10.0),(14,40.0)):   # +500, reset (skipped), +30
                h.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('Total_Flow_Li',u(hh),v))
            for hh,v in ((0,0),(2,1),(4,0),(6,1),(7,0)):   # sump pump: 2 starts, 3 h running
                h.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('SUMP_PUMP_STATUS',u(hh),v))
        a=day.astimezone(timezone.utc);b=(day+timedelta(days=1)).astimezone(timezone.utc)
        x=web.daily_compliance(a,b,zone,dict(web.DEFAULTS))[day.date()]
        self.assertAlmostEqual(x['out_min'],25,places=3);self.assertAlmostEqual(x['litres'],530);self.assertEqual(len(x['ph']),4)
        p=web.pump_usage(a,b,zone)['SUMP_PUMP_STATUS'][day.date()]
        self.assertAlmostEqual(p[0],3.0);self.assertEqual(p[1],2)
        self.assertIn('NOT COMPLIANT',web.compliance_line(day.date(),dict(web.DEFAULTS)))
        with web.appdb() as db:
            db.execute("INSERT INTO events(stamp,kind,detail,state,actor,ack_at) VALUES (?,?,?,?,?,?)",(u(3),'EQ level high','x','closed','op (Operator)',u(3,30)))
            eid=db.execute('SELECT MAX(id) FROM events').fetchone()[0]
            db.execute("INSERT INTO events(stamp,kind,detail,state) VALUES (?,?,?,?)",(u(4),'EQ level recovered',f'Incident {eid}; level 2.0 ft','closed'))
        hist=web.alarm_history(a,b)
        self.assertEqual(len(hist),1);self.assertEqual(hist[0]['cleared'],u(4))
        self.assertEqual(self._report_names(),None)

    def _report_names(self):
        import zipfile
        self._login(web.AUTH,'admin','admin-pass')
        for tag,sheet in (('alarms','Alarm History'),('compliance','Discharge Compliance'),('pumps','Pump Run Hours')):
            code,data=self.call('GET','/api/export','admin','admin-pass',query=f'tag={tag}&startDate=2026-11-10&endDate=2026-11-10')
            self.assertEqual(code,200,tag)
            self.assertIn(f'name="{sheet}"',zipfile.ZipFile(io.BytesIO(data)).read('xl/workbook.xml').decode())
            code,data=self.call('GET','/api/export','admin','admin-pass',query=f'tag={tag}&startDate=2026-11-10&endDate=2026-11-10&format=csv')
            self.assertEqual(code,200,tag);self.assertTrue(data.decode('utf-8-sig').strip())

    def test_chemical_purchases_receive_stock_and_report(self):
        zone=web.ZoneInfo('America/Vancouver');now=datetime.now(zone)
        drum=web.CHEM_PRODUCTS['caustic']['container_l'];pail=web.CHEM_PRODUCTS['sa93']['container_l']
        bought=now-timedelta(hours=3)   # purchase date 3 h back, so 'received 2 h ago' is never before it (safe just after midnight)
        today=bought.date().isoformat();earlier=(now-timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        web.OP_AUTH=Path(self.tmp.name)/'operator.json'
        self._login(web.AUTH,'admin','admin-pass');self._login(web.OP_AUTH,'op','op-pass')
        post=lambda path,body,user='op':self.call('POST',path,user,user+'-pass' if user=='op' else 'admin-pass',body)
        with web.appdb() as db:   # tank count of caustic 3 hours ago: 100 L
            web.save_stock(db,{'chemical':'caustic','kind':'count','litres':100,'when':(now-timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M')},'a')
        # received straight away, with a cost
        self.assertEqual(post('/api/purchases',{'chemical':'caustic','purchased_on':today,'litres':2*drum,'cost':900,'po':'PO-1','supplier':'CESCO','received':earlier})[0],200)
        # ordered, not received yet, no cost (estimated from the price per container)
        self.assertEqual(post('/api/purchases',{'chemical':'sa93','purchased_on':today,'litres':4*pail,'po':'PO-2'})[0],200)
        with web.appdb() as db:
            st=web.stock_status(db)
            self.assertAlmostEqual(st['caustic']['litres'],100+2*drum);self.assertIsNone(st['sa93']['litres'])   # not received: not in stock
            sa=db.execute("SELECT id FROM chemical_purchases WHERE chemical='sa93'").fetchone()[0]
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('chemical_prices',?)",(json.dumps({'caustic':None,'floc':None,'coag':None,'sa93':60.0}),))
        for bad in ({'chemical':'bleach','purchased_on':today,'litres':1},{'chemical':'sa93','purchased_on':'2099-01-01','litres':1},
                    {'chemical':'sa93','purchased_on':today,'litres':0},{'chemical':'sa93','purchased_on':today,'litres':1,'cost':-5},
                    {'chemical':'sa93','purchased_on':today,'litres':1,'received':'2020-01-01T00:00'}):   # received before purchased
            self.assertEqual(post('/api/purchases',bad)[0],400,bad)
        self.assertEqual(post('/api/purchases/receive',{'id':sa,'received':now.strftime('%Y-%m-%dT%H:%M')})[0],200)
        self.assertEqual(post('/api/purchases/receive',{'id':sa,'received':now.strftime('%Y-%m-%dT%H:%M')})[0],400)   # only once
        with web.appdb() as db:
            self.assertAlmostEqual(web.stock_status(db)['sa93']['litres'],4*pail)
            year=bought.year;cfg=web.settings(db)
        rows=web.purchase_rows(year,cfg);flat=[[c[0] if isinstance(c,tuple) else c for c in r] for r in rows]
        qtr=f'{year} Q{(bought.month-1)//3+1}'
        caustic=[r for r in flat if r[:2]==[qtr,'CESCO NEUTRO 5045 50% Caustic']][0]
        self.assertEqual((caustic[2],caustic[3],caustic[5]),(1,2.0,900.0))
        sa93=[r for r in flat if r[:2]==[f'{bought.month:02d}/{year}','CESCO SA-93']][0]
        self.assertEqual(sa93[5],'240.00 (estimated)')   # 4 pails x $60
        self.assertIn([str(year),'CESCO SA-93',1,4.0,round(4*pail,2),'240.00 (estimated)'],flat)
        code,book=self.call('GET','/api/export','admin','admin-pass',query=f'tag=purchases&year={year}')
        self.assertEqual(code,200)
        import zipfile
        sheet=zipfile.ZipFile(io.BytesIO(book)).read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('PO-1',sheet);self.assertIn('FSA93-P5',sheet)
        self.assertEqual(self.call('GET','/api/export','admin','admin-pass',query=f'tag=purchases&year={year}&format=csv')[0],200)
        rows=json.loads(self.call('GET','/api/purchases','op','op-pass')[1])['rows']
        self.assertEqual(len(rows),2)
        self.assertEqual(post('/api/purchases/remove',{'id':sa})[0],200)
        with web.appdb() as db:
            self.assertIsNone(web.stock_status(db)['sa93']['litres'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action LIKE 'Chemical purchase%'").fetchone()[0],4)

    def test_chemical_consumption_month_and_year(self):
        zone=web.ZoneInfo('America/Vancouver');year=datetime.now(zone).year-1   # last year: complete, nothing in the future
        at=lambda m,d:web.iso(datetime(year,m,d,9,0,tzinfo=zone))
        drum=web.CHEM_PRODUCTS['caustic']['container_l']
        with web.appdb() as db:
            for chem,kind,litres,when in (('caustic','count',300.0,at(1,5)),('caustic','delivery',drum,at(2,1)),('caustic','count',250.0,at(3,1)),
                                          ('caustic','count',100.0,at(3,20)),('sa93','delivery',18.0,at(6,3))):
                db.execute('INSERT INTO chemical_stock(chemical,kind,litres,at,note,logged_by,logged_at) VALUES (?,?,?,?,?,?,?)',(chem,kind,litres,when,'','a','x'))
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('chemical_prices',?)",(json.dumps({'caustic':500.0,'floc':None,'coag':None,'sa93':None}),))
            cfg=web.settings(db)
        data=web.chemical_consumption(year,cfg)
        march=data['caustic'][3]
        self.assertAlmostEqual(march['counted'],(300+drum-250)+(250-100))   # both intervals end in March
        self.assertAlmostEqual(march['used'],march['counted'])               # no pump tag: tank counts are the usage
        self.assertAlmostEqual(march['cost_used'],march['counted']/drum*500)
        self.assertAlmostEqual(data['caustic'][2]['delivered'],drum);self.assertAlmostEqual(data['caustic'][2]['cost_delivered'],500)
        self.assertAlmostEqual(data['sa93'][6]['delivered'],18.0);self.assertIsNone(data['sa93'][6]['cost_delivered'])   # no price yet
        self.assertIsNone(data['caustic'][3]['pump'])
        self._login(web.AUTH,'admin','admin-pass')
        import zipfile
        code,book=self.call('GET','/api/export','admin','admin-pass',query=f'tag=consumption&year={year}')
        self.assertEqual(code,200)
        sheet=zipfile.ZipFile(io.BytesIO(book)).read('xl/worksheets/sheet1.xml').decode()
        for text in ('CESCO NEUTRO 5045 50% Caustic','FPF875AH-P5','FPROT7000-Dr55','FSA93-P5',f'03/{year}'):self.assertIn(text,sheet)
        code,csvdata=self.call('GET','/api/export','admin','admin-pass',query=f'tag=consumption&year={year}&format=csv')
        self.assertEqual(code,200);self.assertIn('CESCO SA-93',csvdata.decode('utf-8-sig'))
        self.assertEqual(self.call('GET','/api/export','admin','admin-pass',query='tag=consumption&year=1999')[0],400)
        with web.appdb() as db:   # prices: validated, unknown products refused
            with self.assertRaises(ValueError):web.val_settings({'chemical_prices':{'caustic':-1}},web.settings(db))
            with self.assertRaises(ValueError):web.val_settings({'chemical_prices':{'bleach':5}},web.settings(db))
            # the Settings page always sends all four prices; any left out are saved as not set
            self.assertEqual(web.val_settings({'chemical_prices':{'floc':42.5}},web.settings(db))['chemical_prices'],
                             {'caustic':None,'floc':42.5,'coag':None,'sa93':None})

    def test_every_shelvable_alarm_has_an_email_subject(self):
        for key in web.shelf_choices():
            if key!='ph':self.assertIn(key,web.CONDITION_SUBJECTS)

    def _collector(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('collector',Path(__file__).parent/'collector'/'collector.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        return mod

    def test_collector_saves_every_tag_the_dashboard_uses(self):
        # A tag missing from the collector's list is silently dropped (the SYSTEM_ENABLED circle stayed grey).
        needed=set(web.TAGS)|set(web.DIAGRAM_TAGS)|{t for t,*_ in web.PLC_ALARMS}|{t for t,_ in web.PUMP_TAGS}|{t for _,t,_ in web.STOCK_CHEMS.values() if t}
        missing=needed-set(self._collector().TAGS)
        self.assertFalse(missing,f'Add these tags to TAGS in collector/collector.py: {sorted(missing)}')

    def test_collector_save_page(self):
        col=self._collector()
        db=col.connect(Path(self.tmp.name)/'collector.sqlite3')
        page={'success':True,'transactionId':42,'moreDataAvailable':False,'ewons':[
            {'id':745123,'lastSynchroDate':'2026-11-20T18:00:00Z','tags':[
                {'id':1,'name':'SYSTEM_ENABLED','history':[{'date':'2026-11-20T17:59:00Z','value':1,'quality':'good'}]},
                {'id':2,'name':'NOT_A_DASHBOARD_TAG','history':[{'date':'2026-11-20T17:59:00Z','value':5}]}]},
            {'id':999,'tags':[{'id':3,'name':'pH_Reading','history':[{'date':'2026-11-20T17:59:00Z','value':7}]}]}]}
        self.assertEqual(col.save_page(db,page),1)
        self.assertEqual(db.execute('SELECT name,value FROM samples').fetchall(),[('SYSTEM_ENABLED',1.0)])
        self.assertEqual(db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0],'42')
        with self.assertRaises(ValueError):col.save_page(db,{'success':False})
        db.close()

    def test_monthly_email_has_excel_attachment(self):
        web.EMAIL_CFG=Path(self.tmp.name)/'fake-email.json'
        web.EMAIL_CFG.write_text(json.dumps({'host':'example.invalid','sender':'t@example.com','username':'u','password':'p'}))
        fixed=datetime(2026,10,1,8,0,tzinfo=web.ZoneInfo('America/Vancouver'))
        with web.appdb() as db:
            for k,v in dict(monthly_enabled=True,quarterly_enabled=True,monthly_day=1,monthly_time='08:00',recipients=['t@example.com']).items():
                db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(k,json.dumps(v)))
        class FixedDT(datetime):
            @classmethod
            def now(cls,tz=None):return fixed if tz is None or tz!=timezone.utc else fixed.astimezone(timezone.utc)
        with patch.object(web,'datetime',FixedDT),patch.object(web,'send_mail') as mail:
            web.scheduler()
        subjects={c.args[1]:c.args[4] for c in mail.call_args_list}
        self.assertEqual(set(subjects),{'Wastewater Monthly Report 2026-10','Wastewater Quarterly Report 2026 Q3 (Jul-Sep)'})
        (name,data),=subjects['Wastewater Monthly Report 2026-10']
        self.assertEqual(name,'wastewater-report-all-2026-09-01-to-2026-09-30.xlsx')
        import zipfile
        book=zipfile.ZipFile(io.BytesIO(data)).read('xl/workbook.xml').decode()
        self.assertIn('name="Summary"',book);self.assertIn('name="Total_Flow_Li"',book);self.assertNotIn('name="FLOW_RATE"',book)
        (qname,_),=subjects['Wastewater Quarterly Report 2026 Q3 (Jul-Sep)']
        self.assertEqual(qname,'wastewater-report-all-2026-07-01-to-2026-09-30.xlsx')

    def test_nightly_backup_keeps_newest(self):
        dest=Path(self.tmp.name)/'backups'
        for i in range(3):(dest/f'2026010{i}-000000').mkdir(parents=True)
        folder=web.backup(dest,keep=2)
        self.assertTrue((folder/'history.sqlite3').exists() and (folder/'dashboard.sqlite3').exists())
        self.assertEqual(len([p for p in dest.iterdir()]),2)

if __name__=='__main__':unittest.main(verbosity=2)
