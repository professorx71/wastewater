"""Regression checks using only temporary databases. Does not contact live VPS or SMTP."""
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
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
        # never read the server's real live readings (/var/lib/pbf-wastewater-live) during tests
        web.LIVE_DB=Path(self.tmp.name)/'live.sqlite3'
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
        assert s['revision']=='1.2.63'
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
            self.assertEqual(db.execute('SELECT state FROM events WHERE id=?',(first,)).fetchone()[0],'acknowledged')   # auto-acknowledged, still active
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
            db.execute("DELETE FROM samples WHERE name IN ('Coag_Pump_Run','Floc_Pump_Rate')")
            for name,stamp,v in [('Coag_Pump_Run',at(16,23),10.0),('Coag_Pump_Run',at(17,1),0.0),
                                 ('Floc_Pump_Rate',at(17,12),2.0),('Floc_Pump_Rate',at(18,12),0.0),
                                 ('Caustic_Pump_Run',at(17,8),60.0),('Caustic_Pump_Run',at(17,9),0.0)]:   # 60 mL/min for 1 h = 3.6 L
                db.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',(name,stamp,v))
        q={'tag':['chemical'],'startDate':['2026-11-17'],'endDate':['2026-11-18']}
        rows=web.report_csv(q)[1].decode('utf-8-sig').splitlines()
        self.assertEqual(rows[1:],['2026-11-17,0.60,1.44,3.60,0.00','2026-11-18,0.00,1.44,0.00,0.00','Total,0.60,2.88,3.60,0.00'])
        self.assertEqual(rows[0],'Date (America/Vancouver),Coagulant used (L),Flocculant used (L),Caustic used (L),Acid used (L)')
        book=zipfile.ZipFile(io.BytesIO(web.report_xlsx(q)[1])).read('xl/workbook.xml').decode()
        self.assertEqual(re.findall(r'<sheet name="([^"]+)"',book),['Chemical Usage','Coag_Pump_Run','Floc_Pump_Rate','Caustic_Pump_Run','Acid_Pump_Run'])

    def test_acid_and_caustic_pumps(self):
        stamp=web.iso(datetime.now(timezone.utc)-timedelta(seconds=15))
        with sqlite3.connect(web.DB) as db:
            for name,v in [('Caustic_Pump_Run',284.3511),('Caustic_Pump_Rate_Percent',100.9908),('Acid_Pump_Run',0.0),('Acid_Pump_Rate_Percent',0.0)]:
                db.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',(name,stamp,v))
        ex=web.summary()['extras']
        self.assertEqual((ex['Caustic_Pump_Run']['status'],ex['Caustic_Pump_Run']['value'],ex['Caustic_Pump_Run']['raw_unit']),('current',284.3511,'mL/min'))
        self.assertEqual((ex['Acid_Pump_Rate_Percent']['status'],ex['Acid_Pump_Rate_Percent']['value']),('current',0.0))
        self.assertIn('Acid_Pump_Run',web.DOSING_TAG_CHOICES)
        html=(Path(web.__file__).parent/'static'/'index.html').read_text(encoding='utf-8')
        for i in ('causticRun','acidRun','causticState','acidState','<option value="L/hr">L/hr</option>'):self.assertIn(i,html)
        self.assertNotIn('<option value="mL/hr">',html)   # coag / floc show mL/min or L/hr like acid / caustic
        self.assertIn('id="dosingCanvas"',html);self.assertIn('id="dosingToggle"',html)

    def test_dosing_history_for_trends(self):
        now=datetime.now(timezone.utc)
        with sqlite3.connect(web.DB) as db:
            db.execute("DELETE FROM samples WHERE name IN ('Coag_Pump_Run','Caustic_Pump_Run')")
            for name,t,v in [('Caustic_Pump_Run',now-timedelta(hours=8),100.0),('Caustic_Pump_Run',now-timedelta(hours=1),284.0),
                             ('Coag_Pump_Run',now-timedelta(minutes=30),600.0)]:   # Coag is logged in mL/hr: 600 mL/hr = 10 mL/min
                db.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',(name,web.iso(t),v))
        d=web.history({'from':[web.iso(now-timedelta(hours=6))],'to':[web.iso(now)]})['dosing']
        self.assertEqual(set(d),set(web.DOSING_RUN_TAGS))
        self.assertEqual([p['value'] for p in d['Caustic_Pump_Run']],[100.0,284.0])   # the reading before the window is held into it
        self.assertEqual([p['value'] for p in d['Coag_Pump_Run']],[10.0])
        self.assertEqual(d['Acid_Pump_Run'],[])

    def test_texts_for_alarms_only(self):
        for alarm in ('ph','plc:E_STOP_STATUS','level','flow','data'):self.assertTrue(web.texted(alarm),alarm)
        for alarm in ('ph_trend_high','ph_trend_low','dosing:coag','stock:caustic','order:floc',None):self.assertFalse(web.texted(alarm),alarm)

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
        # Removing needs the person's own password again
        # Only the Admin removes records, and needs the password again
        code,body=self.call('POST','/api/pickups/remove',*O,{'id':rows[0]['id'],'password':'op-pass'})
        self.assertEqual(code,403);self.assertIn('Only the Admin',json.loads(body)['error'])
        code,body=self.call('POST','/api/pickups/remove',*A,{'id':rows[0]['id']})
        self.assertEqual(code,400);self.assertIn('password',json.loads(body)['error'])
        code,body=self.call('POST','/api/pickups/remove',*A,{'id':rows[0]['id'],'password':'op-pass'})
        self.assertEqual(code,400);self.assertIn('Wrong password',json.loads(body)['error'])
        self.assertEqual(self.call('POST','/api/pickups/remove',*A,{'id':rows[0]['id'],'password':'admin-pass'})[0],200)
        with web.appdb() as conn:
            acts=[r['action'] for r in conn.execute('SELECT action FROM audit')]
        self.assertIn('Sludge pickup logged',acts);self.assertIn('Sludge pickup removed',acts)
        # Rights saved before the pickup right existed still give Operator the pickup right
        with web.appdb() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('role_permissions',?)",(json.dumps({'Engineering':['audit'],'Operator':['export'],'Guest':[]}),))
        self.assertEqual(sorted(web.role_permissions()['Operator']),['alarms','export','pickup','view'])
        self.assertEqual(sorted(web.role_permissions()['Guest']),['alarms','view'])
        self.assertEqual(sorted(web.role_permissions()['Engineering']),['alarms','audit','email','pickup','settings_view','shelve','view'])
        # ...and once the Admin saves rights without it, it is off
        self.assertEqual(self.call('POST','/api/accounts/permissions',*A,{'Engineering':['audit'],'Operator':['export'],'Guest':[]})[0],200)
        self.assertEqual(sorted(web.role_permissions()['Operator']),['export','view'])

    def test_coag_unit_change_to_ml_per_hour(self):
        import zipfile
        # Coag_Pump_Run switched from mL/min to mL/hr at 2026-11-19 18:01 UTC
        with sqlite3.connect(web.DB) as db:
            for stamp,v,q in [('2026-11-19T17:00:00.000000Z',10.0,None),('2026-11-19T18:01:02.000000Z',600.0,'initialGood')]:
                db.execute('INSERT INTO samples(name,stamp,value,quality) VALUES(?,?,?,?)',('Coag_Pump_Run',stamp,v,q))
        self.assertEqual(web.ml_per_min('Coag_Pump_Run','2026-11-19T18:00:59.000000Z',600.0),600.0)
        self.assertEqual(web.ml_per_min('Coag_Pump_Run','2026-11-19T18:01:02.000000Z',600.0),10.0)
        self.assertEqual(web.ml_per_min('Floc_Pump_Rate','2026-11-20T00:00:00.000000Z',1.5),1.5)
        # Litres: 10 mL/min held 61 min 2 s, then 600 mL/hr (= 10 mL/min) to the end of the Vancouver day
        start=datetime(2026,11,19,17,tzinfo=timezone.utc);end=datetime(2026,11,19,19,tzinfo=timezone.utc)
        # Summary sends mL/min; 'initialGood' counts as a good reading
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
        self.assertEqual((item['status'],item['value'],item['raw_value'],item['raw_unit']),('current',10.0,600.0,'mL/hr'))
        # Worksheet rows show the unit used at the time
        q={'tag':['Coag_Pump_Run'],'from':[web.iso(start)],'to':[web.iso(end)]}
        rows=web.report_csv(q)[1].decode('utf-8-sig').splitlines()[1:]
        self.assertEqual([r.split(',')[-1] for r in rows],['mL/min','mL/hr'])

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
        self.assertIn('GPM',flow)
        summary=z.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('EOD Daily Usage (L)',summary);self.assertNotIn('FLOW_RATE',summary);self.assertIn('Total_Flow_Li',summary)
        self.assertIn('Total Quarterly Usage',summary);self.assertIn('Total Monthly Usage',summary)
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
        # quarters (Q3, Q4), months (Sep, Oct), days (09-30, 10-01 carried, 10-02): EOD daily usage only
        # Q3 = Sep = 09-30; Q4 = Oct = 10-01 (carried forward from 09-30) + 10-02
        self.assertEqual(values,[524714.25,525014.37, 524714.25,525014.37, 524714.25,524714.25,300.12])
        self.assertIn('carried forward from 09/30/2026 18:00',summary)
        self.assertNotIn('Meter reading dropped',summary);self.assertNotIn('usage not calculated',summary)
        self.assertIn('2026 Q3 (Jul-Sep)',summary);self.assertIn('2026 Q4 (Oct-Dec)',summary)
        self.assertEqual(str(web.truncate2(524714.2551)),'524714.25')
        # Column C: cubic metres (3 decimals, style 5); column D: pH average per day / month / quarter
        m3=[float(v) for v in __import__('re').findall(r's="5"><v>([^<]+)</v>',summary)]
        self.assertAlmostEqual(m3[0],524.71425);self.assertAlmostEqual(m3[-1],0.30012)
        with sqlite3.connect(web.DB) as db:
            for local,v in (('2026-09-30 08:00',7.0),('2026-09-30 20:00',8.0),('2026-10-02 09:00',6.5)):
                t=datetime.strptime(local,'%Y-%m-%d %H:%M').replace(tzinfo=zone)
                db.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('pH_Reading',web.iso(t),v))
        _,data=web.report_xlsx({'tag':['FLOW_RATE'],'startDate':['2026-09-30'],'endDate':['2026-10-02']})
        summary=zipfile.ZipFile(io.BytesIO(data)).read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('EOD Daily Usage (L)',summary);self.assertIn('EOD Daily Usage (m³)',summary);self.assertIn('pH Average',summary)
        self.assertIn('Total Monthly Usage (L)',summary);self.assertNotIn('Value (L)',summary)
        self.assertRegex(summary,r'<c r="D\d+" s="2"><v>7\.5</v></c>')    # 09-30 (and Sep, Q3): (7 + 8) / 2
        self.assertRegex(summary,r'<c r="D\d+" s="2"><v>6\.5</v></c>')    # 10-02
        self.assertNotRegex(summary,r'<c r="F\d+" s="5">')     # the old usage columns E and F are gone
        self.assertNotIn('>Daily Usage (L)<',summary);self.assertNotIn('Quarterly Usage (L)<',summary.replace('Total Quarterly Usage (L)',''))

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
        self.assertEqual(self.call('POST','/api/ack','op','op-pass',{'id':1})[0],404)   # already auto-acknowledged
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
            self.assertEqual(db.execute('SELECT actor FROM events WHERE id=1').fetchone()[0],web.AUTO_ACK_ACTOR)
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
        self.assertEqual(guest['permissions'],['alarms','view'])
        self.assertEqual(self.call('GET','/api/export','visitor','gst1',query='startDate=2026-11-01&endDate=2026-11-02')[0],403)
        self.assertEqual(self.call('GET','/api/settings','visitor','gst1')[0],403)
        # Admin changes the rights: Guest may download, Engineering loses Settings
        self.assertEqual(self.call('POST','/api/accounts/permissions',*A,{'Engineering':['export','audit'],'Operator':['export'],'Guest':['nope']})[0],400)
        self.assertEqual(self.call('POST','/api/accounts/permissions',*A,{'Engineering':['export','audit'],'Operator':['export'],'Guest':['export']})[0],200)
        self.assertEqual(json.loads(self.call('GET','/api/summary','visitor','gst1')[1])['permissions'],['export','view'])
        self.assertEqual(self.call('GET','/api/settings','eng','eng-pass-1')[0],403)
        self.assertEqual(self.call('GET','/api/audit','eng','eng-pass-1')[0],200)
        # Removing a login stops its sign-in
        self.assertEqual(self.call('POST','/api/accounts/user/remove',*A,{'role':'Guest','username':'visitor','password':A[1]})[0],200)
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
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='EQ level high' AND state='acknowledged'").fetchone()[0],1)
            web.check_conditions(db,cfg,[lvl(2.0,2)],now)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='EQ level recovered'").fetchone()[0],1)
            # Data stopped: newest reading older than the limit
            web.check_conditions(db,cfg,[dict(lvl(2.0,2),age_seconds=20*60)],now+timedelta(minutes=1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='Data stopped' AND state='acknowledged'").fetchone()[0],1)
            web.check_conditions(db,cfg,[lvl(2.0,3)],now+timedelta(minutes=2))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='Data restored'").fetchone()[0],1)
        # No discharge flow: 30 minutes of near-zero flow
        with sqlite3.connect(web.DB) as h:
            h.execute("DELETE FROM samples WHERE name='FLOW_RATE'")
            for m in range(0,31,5):
                h.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('FLOW_RATE',web.iso(datetime.now(timezone.utc)-timedelta(minutes=m)),0.01))
        with web.appdb() as db:
            web.check_conditions(db,cfg,[lvl(2.0,4)],now+timedelta(minutes=3))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='No discharge flow' AND state='acknowledged'").fetchone()[0],1)
            # Turning the alarm off closes the open incident
            web.check_conditions(db,dict(cfg,flow_alarm_enabled=False),[lvl(2.0,5)],now+timedelta(minutes=4))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='No discharge flow' AND state IN ('open','acknowledged')").fetchone()[0],0)

    def test_plc_alarms_latch_auto_ack_and_email(self):
        cfg=dict(web.DEFAULTS)
        bit=lambda v,m:{'value':v,'status':'current','stamp':f'2026-11-16T12:{m:02d}:00Z'}
        with web.appdb() as db:
            web.check_plc_alarms(db,cfg,{'E_STOP_STATUS':bit(1,0),'SLUDGE_TANK_LEVEL':bit(1,0),'MIXER_PUMP_FAULTED':bit(0,0)})
            ev={r['kind']:r for r in db.execute("SELECT * FROM events")}
            self.assertEqual(set(ev),{'E-Stop active','Sludge tank FULL'})
            self.assertTrue(all(r['state']=='acknowledged' and r['actor']==web.AUTO_ACK_ACTOR for r in ev.values()))
            self.assertEqual(ev['E-Stop active']['detail'],'E-Stop active. Check the E-Stop buttons before restarting the system.')
            web.check_plc_alarms(db,cfg,{'E_STOP_STATUS':bit(1,0),'SLUDGE_TANK_LEVEL':bit(1,0)})   # same stamps: no duplicates
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
            # Both clear: every alarm was auto-acknowledged, so both close
            web.check_plc_alarms(db,cfg,{'E_STOP_STATUS':bit(0,1),'SLUDGE_TANK_LEVEL':bit(0,1)})
            sludge=db.execute("SELECT * FROM events WHERE id=?",(ev['Sludge tank FULL']['id'],)).fetchone()
            self.assertEqual(sludge['state'],'closed');self.assertIn('auto-acknowledged',sludge['actor']);self.assertTrue(sludge['ack_at'])
            estop=db.execute("SELECT * FROM events WHERE id=?",(ev['E-Stop active']['id'],)).fetchone()
            self.assertEqual(estop['state'],'closed');self.assertTrue(estop['cleared_at'])
            cleared=db.execute("SELECT detail FROM events WHERE kind='E-Stop active cleared'").fetchone()[0]
            self.assertEqual(cleared,f"Incident {estop['id']}; back to normal")
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
        # Nothing is left to acknowledge
        self._login(web.AUTH,'admin','admin-pass')
        self.assertEqual(self.call('POST','/api/ack','admin','admin-pass',{'id':estop['id']})[0],404)
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
        self.assertEqual(subjects.count('Flocculant and Coagulant Pump Motor Stop Faulted'),1)
        # No incident numbers and no tag names / alarm codes in the emails or their subjects
        texts=[c.args[1]+'\n'+c.args[2] for c in mail.call_args_list]
        self.assertEqual(web.RECOVERY_SUBJECTS['plc:E_STOP_STATUS'],'CLEARED: Emergency Stop Activated')
        for t in texts:
            self.assertNotRegex(t,r'(?i)incident')
            self.assertNotRegex(t,r'[A-Z]+_[A-Z_]+')
        self.assertEqual(web.mail_detail('Incident 99; back to normal'),'back to normal')
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
        self.assertEqual(self.call('POST','/api/calibrations/remove','admin','admin-pass',{'id':rows[1]['id'],'password':'admin-pass'})[0],200)
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
        calls=[c for c in mail.call_args_list if 'Emergency Stop' in c.args[1]+c.args[2]]
        self.assertEqual(len(calls),2)   # one email, one text, sent once
        sms=next(c for c in calls if c.args[3]==['6045550000@sms.example.com'])
        self.assertEqual(sms.args[1],'')   # no subject: phones would show "Subj:" and repeat the text
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
        with sqlite3.connect(web.DB) as h:   # coagulant pump at 60 mL/hr (= 1 mL/min) for the last 10 hours: 0.6 L used
            h.execute('INSERT INTO samples(name,stamp,value) VALUES(?,?,?)',('Coag_Pump_Run',web.iso(now-timedelta(hours=10)),60.0))
        when=(now-timedelta(hours=10)).astimezone(web.ZoneInfo('America/Vancouver')).strftime('%Y-%m-%dT%H:%M')
        with web.appdb() as db:
            web.save_stock(db,{'chemical':'coag','kind':'count','litres':10,'when':when},'a')
            st=web.stock_status(db)
            self.assertAlmostEqual(st['coag']['litres'],10-0.6,delta=0.02)
            self.assertIsNone(st['floc']['litres'])
            cfg=dict(web.DEFAULTS,stock_low_coag_l=20.0)
            web.check_stock(db,cfg)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='Coagulant (PROTECT 7000) stock low' AND state='acknowledged'").fetchone()[0],1)
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
        P=web.products(None);drum=P['caustic']['container_l'];pail=P['sa93']['container_l']
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
        self.assertIn([str(year),'CESCO SA-93',1,4.0,round(4*pail,2),'240.00 (estimated)','CAD'],flat)
        code,book=self.call('GET','/api/export','admin','admin-pass',query=f'tag=purchases&year={year}')
        self.assertEqual(code,200)
        import zipfile
        sheet=zipfile.ZipFile(io.BytesIO(book)).read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('PO-1',sheet);self.assertIn('FSA93-P5',sheet)
        self.assertEqual(self.call('GET','/api/export','admin','admin-pass',query=f'tag=purchases&year={year}&format=csv')[0],200)
        rows=json.loads(self.call('GET','/api/purchases','op','op-pass')[1])['rows']
        self.assertEqual(len(rows),2)
        self.assertEqual(post('/api/purchases/remove',{'id':sa,'password':'op-pass'})[0],403)   # Admin only
        self.assertEqual(post('/api/purchases/remove',{'id':sa,'password':'admin-pass'},'admin')[0],200)
        with web.appdb() as db:
            self.assertIsNone(web.stock_status(db)['sa93']['litres'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action LIKE 'Chemical purchase%'").fetchone()[0],4)

    def test_chemical_consumption_month_and_year(self):
        zone=web.ZoneInfo('America/Vancouver');year=datetime.now(zone).year-1   # last year: complete, nothing in the future
        at=lambda m,d:web.iso(datetime(year,m,d,9,0,tzinfo=zone))
        drum=web.products(None)['caustic']['container_l']
        with web.appdb() as db:
            for chem,kind,litres,when in (('caustic','count',300.0,at(1,5)),('caustic','delivery',drum,at(2,1)),('caustic','count',250.0,at(3,1)),
                                          ('caustic','count',100.0,at(3,20)),('sa93','delivery',18.0,at(6,3))):
                db.execute('INSERT INTO chemical_stock(chemical,kind,litres,at,note,logged_by,logged_at) VALUES (?,?,?,?,?,?,?)',(chem,kind,litres,when,'','a','x'))
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('chemical_prices',?)",(json.dumps({'caustic':500.0,'floc':None,'coag':None,'sa93':None}),))
            cfg=web.settings(db)
        cfg['chemical_products']=[dict(p,tag='') if p['key']=='caustic' else p for p in cfg['chemical_products']]   # caustic by tank counts here
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
            cfg=web.settings(db);items=cfg['chemical_products']
            self.assertEqual(web.products(cfg)['caustic']['price'],500.0)   # price saved before 1.2.63 carried over
            bad=lambda **kw:[dict(items[0],**kw)]+items[1:]
            for b in (bad(prices=[{'from':'2026-01-01','price':-1}]),bad(prices=[{'from':'not a date','price':5}]),
                      bad(prices=[{'from':'2026-01-01','price':5},{'from':'2026-01-01','price':6}]),bad(container_l=0),bad(name=''),bad(key='Bad Key'),bad(tag='FLOW_RATE'),bad(lead_days=1.5),
                      bad(tag='Floc_Pump_Rate'),[]):   # Floc_Pump_Rate already measures the flocculant
                with self.assertRaises(ValueError):web.val_settings({'chemical_products':b},cfg)
            # an old page sending the pre-1.2.63 keys is not refused (they are ignored)
            web.val_settings({'chemical_prices':{'floc':42.5},'stock_low_coag_l':3},cfg)
            # caustic has tank counts: it cannot be deleted, only taken out of use; sa93 has entries too
            with self.assertRaises(ValueError):web.val_settings({'chemical_products':items[1:]},cfg,{'caustic'})
            new=web.val_settings({'chemical_products':[dict(items[0],active=False)]+items[1:]+[dict(key='bleach',name='Bleach 12%',container='20 L jug',container_l=20,prices=[{'from':'2026-02-01','price':30}])]},cfg,{'caustic'})
            self.assertEqual([p['key'] for p in new['chemical_products']],['caustic','floc','coag','sa93','bleach'])
            self.assertEqual(new['chemical_products'][-1]['label'],'Bleach 12%')
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('chemical_products',?)",(json.dumps(new['chemical_products']),))
            cfg=web.settings(db)
            self.assertNotIn('caustic',web.products(cfg,active_only=True));self.assertIn('caustic',web.products(cfg))   # still in the reports
            self.assertNotIn('caustic',web.stock_status(db,cfg));self.assertIn('order:bleach',web.shelf_choices(cfg))
            with self.assertRaises(web.AccountError):web.save_stock(db,{'chemical':'caustic','kind':'count','litres':1,'when':'2026-01-01T00:00'},'a',cfg)

    def test_every_shelvable_alarm_has_an_email_subject(self):
        for key in web.shelf_choices():
            if key!='ph':self.assertFalse(web.condition_subject(key,web.DEFAULTS).startswith('Wastewater alarm:'),key)

    def _collector(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('collector',Path(__file__).parent/'collector'/'collector.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        return mod

    def test_collector_saves_every_tag_the_dashboard_uses(self):
        # A tag missing from the collector's list is silently dropped (the SYSTEM_ENABLED circle stayed grey).
        needed=set(web.TAGS)|set(web.DIAGRAM_TAGS)|{t for t,*_ in web.PLC_ALARMS}|{t for t,_ in web.PUMP_TAGS}|set(web.DOSING_TAG_CHOICES)
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

    def test_old_alarms_cleaned_and_auto_acknowledged(self):
        with web.appdb() as db:
            db.execute("INSERT INTO events(stamp,kind,detail,state) VALUES ('2026-11-20T04:58:00Z','Rake motor faulted',"
                       "'Rake motor faulted (PLC N7:1/8, tag RAKE_MOTOR_FAULTED = 1). Check the DAF rake motor and its VFD.','open')")
            db.execute("INSERT INTO events(stamp,kind,detail,state) VALUES ('2026-11-20T07:39:00Z','Rake motor faulted cleared',"
                       "'Incident 1; RAKE_MOTOR_FAULTED back to 0','closed')")
            db.execute("DELETE FROM settings WHERE key='alarm_text_v2'")
        with web.appdb() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id')]
        self.assertEqual(rows[0]['detail'],'Rake motor faulted. Check the DAF rake motor and its VFD.')
        self.assertEqual((rows[0]['state'],rows[0]['actor']),('acknowledged',web.AUTO_ACK_ACTOR))
        self.assertEqual(rows[1]['detail'],'Incident 1; back to normal')

    def _samples(self,rows):
        with sqlite3.connect(web.DB) as db:
            for name,t,v in rows:db.execute('INSERT INTO samples(name,stamp,value,quality) VALUES(?,?,?,?)',(name,web.iso(t),v,None))

    def test_live_readings_from_m2web(self):
        import importlib.util,os,tempfile
        here=os.path.join(os.path.dirname(web.__file__),'collector')
        spec=importlib.util.spec_from_file_location('live_mod',os.path.join(here,'live.py'))
        live=importlib.util.module_from_spec(spec);spec.loader.exec_module(live)
        reply=('"TagId";"TagName";"Value";"AlStatus";"AlType";"Quality"\n'
               '1;"pH_Reading";7.31;0;0;65472\n2;"FLOW_RATE";4.0;0;0;192\n3;"Floc_Pump_Rate";1.72;0;0;0\n4;"Other_Tag";5;0;0;192\n')
        vals=live.parse(reply)
        self.assertEqual(set(vals),{'pH_Reading','FLOW_RATE','Floc_Pump_Rate'})
        self.assertTrue(vals['pH_Reading'][2]);self.assertFalse(vals['Floc_Pump_Rate'][2])   # quality 0 = bad
        with self.assertRaises(ValueError):live.parse('{"success":false,"message":"Invalid credentials"}')
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path=os.path.join(d,'live.sqlite3');db=live.connect(path);live.save(db,vals);db.close()
            with patch.object(web,'LIVE_DB',web.Path(path)):
                s=web.summary()
                ph=next(t for t in s['tags'] if t['name']=='pH_Reading');fl=next(t for t in s['tags'] if t['name']=='FLOW_RATE')
                self.assertEqual((ph['source'],ph['value'],ph['status']),('live',7.31,'current'));self.assertTrue(s['live'])
                self.assertAlmostEqual(fl['value'],4.0*3.78541)
                self.assertEqual(s['extras']['Floc_Pump_Rate']['source'],'history')   # bad quality: not used
                # an old live value is ignored
                with sqlite3.connect(path) as c:c.execute("UPDATE live SET read_at='2026-01-01T00:00:00.000000Z'")
                self.assertFalse(web.summary()['live'])
        # without live readings set up, nothing changes
        with patch.object(web,'LIVE_DB',web.Path(tempfile.gettempdir())/'no-such-live.sqlite3'):
            self.assertFalse(web.summary()['live'])

    def test_floc_tag_renamed(self):
        # Saved products still naming the old Ewon tag switch to Floc_Pump_Rate
        self.assertEqual(web.norm_product({'key':'floc','tag':'Floc_Pump_Run','prices':[]})['tag'],'Floc_Pump_Rate')
        import importlib.util,tempfile,os
        spec=importlib.util.spec_from_file_location('collector_mod',os.path.join(os.path.dirname(web.__file__),'collector','collector.py'))
        col=importlib.util.module_from_spec(spec);spec.loader.exec_module(col)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path=os.path.join(d,'h.sqlite3');db=col.connect(path)
            with db:db.execute("INSERT INTO samples VALUES ('1','Floc_Pump_Run','2026-11-20T00:00:00.000000Z',1.5,NULL,'x')")
            db.close();db=col.connect(path)
            self.assertEqual(db.execute('SELECT name FROM samples').fetchall(),[('Floc_Pump_Rate',)]);db.close()

    def test_card_forecast_arrow_and_percent(self):
        now=datetime.now(timezone.utc)
        with sqlite3.connect(web.DB) as db:db.execute("DELETE FROM samples WHERE name IN ('EQ_Tank_Level_Ft','FLOW_RATE')")
        # Level falling 0.01 ft per minute from 3.30 to 3.00: about 2.40 ft in an hour, -20%
        self._samples([('EQ_Tank_Level_Ft',now-timedelta(minutes=30-m),3.3-0.01*m) for m in range(31)])
        # Flow falling fast: the prediction stops at 0
        self._samples([('FLOW_RATE',now-timedelta(minutes=30-m),3.0-0.1*m) for m in range(31)])
        tags=[{'name':'EQ_Tank_Level_Ft','status':'current','value':3.0},{'name':'FLOW_RATE','status':'current','value':0.0},
              {'name':'Total_Flow_Li','status':'stale','value':5.0}]
        f=web.card_forecasts(tags,now)
        lvl=f['EQ_Tank_Level_Ft'];self.assertAlmostEqual(lvl['predicted'],2.4,delta=0.02);self.assertAlmostEqual(lvl['change_pct'],-20,delta=0.5)
        self.assertEqual(lvl['ahead_min'],60)
        self.assertEqual(f['FLOW_RATE']['predicted'],0)
        self.assertNotIn('Total_Flow_Li',f)   # not live: no prediction
        # Flow is stored in GPM and shown in L/min: the prediction is in L/min too
        with sqlite3.connect(web.DB) as db:db.execute("DELETE FROM samples WHERE name='FLOW_RATE'")
        self._samples([('FLOW_RATE',now-timedelta(minutes=30-m),10.0+0.01*m) for m in range(31)])
        f=web.card_forecasts([{'name':'FLOW_RATE','status':'current','value':10.3*3.78541}],now)['FLOW_RATE']
        self.assertAlmostEqual(f['predicted'],(10.3+0.6)*3.78541,delta=0.05);self.assertAlmostEqual(f['rate_per_h'],0.6*3.78541,delta=0.05)

    def test_ph_trend_warns_before_the_limit(self):
        now=datetime.now(timezone.utc)
        with sqlite3.connect(web.DB) as db:db.execute("DELETE FROM samples WHERE name='pH_Reading'")
        cfg=dict(web.DEFAULTS)
        # Steady pH: no warning
        self._samples([('pH_Reading',now-timedelta(minutes=40),7.0)])
        tr=web.ph_trend(cfg,now)
        self.assertIsNone(tr['direction']);self.assertAlmostEqual(tr['rate_per_h'],0,places=6)
        # Rising 0.02 pH per minute (1.2 per hour) to 8.60: reaches 9.00 in about 20 minutes
        self._samples([('pH_Reading',now-timedelta(minutes=30-m),8.0+0.02*m) for m in range(31)])
        tr=web.ph_trend(cfg,now)
        self.assertEqual(tr['direction'],'high');self.assertAlmostEqual(tr['rate_per_h'],1.2,delta=0.15);self.assertLess(tr['eta_min'],35)
        self.assertNotIn('pH_Reading',web.trend_text(tr))
        # Too far away to matter with a 10-minute look-ahead
        self.assertIsNone(web.ph_trend(dict(cfg,ph_trend_lookahead_min=10),now)['direction'])
        # As an alarm: confirmed over two minutes, auto-acknowledged, cleared when the trend stops
        ph={'name':'pH_Reading','status':'current','value':8.6,'stamp':web.iso(now),'age_seconds':5}
        local=now.astimezone(web.ZoneInfo('America/Vancouver'))
        with web.appdb() as db:
            web.check_conditions(db,cfg,[ph],local)
            web.check_conditions(db,cfg,[ph],local+timedelta(minutes=1))
            row=db.execute("SELECT * FROM events WHERE kind='pH trending high'").fetchone()
            self.assertEqual(row['state'],'acknowledged');self.assertIn('reaches the 9.00 limit',row['detail'])
            self.assertIn('ph_trend_high',web.shelf_choices());self.assertIn('ph_trend_high',web.CONDITION_SUBJECTS)
        self._samples([('pH_Reading',now+timedelta(seconds=1),8.6)])
        with patch.object(web,'ph_trend',return_value={'value':8.6,'rate_per_h':0.0,'direction':None,'limit':None,'eta_min':None}),web.appdb() as db:
            web.check_conditions(db,cfg,[ph],local+timedelta(minutes=2));web.check_conditions(db,cfg,[ph],local+timedelta(minutes=3))
            self.assertEqual(db.execute("SELECT state FROM events WHERE kind='pH trending high'").fetchone()[0],'closed')
        with self.assertRaises(ValueError):web.val_settings({'ph_trend_window_min':5},dict(web.DEFAULTS))

    def test_compliance_litres_during_excursion(self):
        zone=web.ZoneInfo('America/Vancouver')
        t0=datetime(2026,11,10,12,0,tzinfo=zone).astimezone(timezone.utc)
        self._samples([('pH_Reading',t0,7.0),('pH_Reading',t0+timedelta(minutes=10),9.5),('pH_Reading',t0+timedelta(minutes=14),9.8),
                       ('pH_Reading',t0+timedelta(minutes=20),7.1),('FLOW_RATE',t0-timedelta(minutes=5),10.0),
                       ('Total_Flow_Li',t0-timedelta(hours=1),1000.0),('Total_Flow_Li',t0+timedelta(hours=1),5000.0)])
        cfg=dict(web.DEFAULTS)
        a=datetime(2026,11,10,tzinfo=zone).astimezone(timezone.utc);b=a+timedelta(days=1)
        days,exc=web.daily_compliance(a,b,zone,cfg,with_excursions=True)
        self.assertEqual(len(exc),1)
        e=exc[0];self.assertEqual((e['side'],e['extreme']),('high',9.8));self.assertAlmostEqual(e['minutes'],10)
        self.assertAlmostEqual(e['litres'],10*web.FLOW_FACTOR*10,places=3)
        d=days[datetime(2026,11,10).date()]
        self.assertAlmostEqual(d['out_litres'],10*web.FLOW_FACTOR*10,places=3);self.assertAlmostEqual(d['litres'],4000)
        rows=web.compliance_rows(a,b,zone,'11/10/2026',cfg)
        flat=[c[0] if isinstance(c,tuple) else c for r in rows for c in r]
        self.assertIn('Litres discharged while pH outside limits',flat);self.assertIn('Volume discharged inside pH limits (%)',flat)
        self.assertIn(round(100*(1-378.541/4000),2),flat)
        self.assertIn('379 L while pH was outside the limits',web.compliance_line(datetime(2026,11,10).date(),cfg))

    def test_dosing_pump_stopped_while_discharging(self):
        now=datetime.now(timezone.utc)
        with sqlite3.connect(web.DB) as db:db.execute("DELETE FROM samples WHERE name='FLOW_RATE'")
        self._samples([('FLOW_RATE',now-timedelta(minutes=m),10.0) for m in (14,10,5,1)]+
                      [('Coag_Pump_Run',now-timedelta(minutes=30),0.2),('Floc_Pump_Rate',now-timedelta(minutes=30),5.0)])   # 0.2 mL/hr = 0.003 mL/min shows 0.00: stopped
        cfg=dict(web.DEFAULTS,dosing_alarm_enabled=True,dosing_alarm_minutes=15)
        local=now.astimezone(web.ZoneInfo('America/Vancouver'))
        with web.appdb() as db:
            web.check_conditions(db,cfg,[],local)
            kinds=[r[0] for r in db.execute('SELECT kind FROM events')]
        self.assertIn('Coagulant pump not dosing',kinds);self.assertNotIn('Flocculant pump not dosing',kinds)
        # Off by default
        self.assertFalse(web.DEFAULTS['dosing_alarm_enabled'])
        # Chemical usage report: discharged m3 and mL per m3
        zone=web.ZoneInfo('America/Vancouver')
        rows=web.chemical_rows(now-timedelta(days=1),now,zone,'x')
        flat=[c[0] if isinstance(c,tuple) else c for r in rows for c in r]
        self.assertIn('Discharged (m³)',flat);self.assertIn('Coagulant (mL per m³ discharged)',flat)

    def test_cost_per_m3_and_days_of_stock_left(self):
        zone=web.ZoneInfo('America/Vancouver');now=datetime.now(timezone.utc);today=now.astimezone(zone).date();y=today-timedelta(days=1)
        at=lambda h,d=y:datetime.combine(d,web.time(h),zone).astimezone(timezone.utc)
        with sqlite3.connect(web.DB) as h:h.execute("DELETE FROM samples WHERE name IN ('Coag_Pump_Run','Floc_Pump_Rate','Total_Flow_Li')")
        # coagulant pump at 60 mL/hr (1 mL/min) all of yesterday; 10,000 L discharged yesterday
        self._samples([('Coag_Pump_Run',at(0),60.0),('Coag_Pump_Run',at(0,today),0.0),('Total_Flow_Li',at(9),100.0),('Total_Flow_Li',at(18),10100.0)])
        with web.appdb() as db:
            cfg=web.settings(db);items=[dict(p,tag='' if p['key']=='caustic' else p['tag'],prices=[{'from':'2020-01-01','price':v}] if (v:={'coag':100.0,'caustic':500.0}.get(p['key'])) else []) for p in cfg['chemical_products']]   # caustic by tank counts here
            cfg=web.val_settings({'chemical_products':items},cfg)
            # caustic: 300 L counted 20 days ago, 200 L 10 days ago: 10 L/day, so about 10 days left now
            for litres,days in ((300.0,20),(200.0,10)):
                db.execute('INSERT INTO chemical_stock(chemical,kind,litres,at,note,logged_by,logged_at) VALUES (?,?,?,?,?,?,?)',
                           ('caustic','count',litres,web.iso(now-timedelta(days=days)),'','a','x'))
            st=web.stock_status(db,cfg,now)['caustic']
            self.assertAlmostEqual(st['per_day'],10.0,places=3);self.assertAlmostEqual(st['days_left'],10.0,places=2)
            self.assertFalse(st['order_now'])   # delivery takes 7 days
            cfg['chemical_products']=[dict(p,lead_days=14) if p['key']=='caustic' else p for p in cfg['chemical_products']]
            self.assertTrue(web.stock_status(db,cfg,now)['caustic']['order_now'])
            web.check_stock(db,cfg,now)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='Caustic (NEUTRO 5045) order now'").fetchone()[0],1)
            self.assertIn('ORDER NOW',web.condition_subject('order:caustic',cfg))
            # tank-count usage is spread over the days between the counts: 10 L per day
            use=web.daily_chemical_use(db,now-timedelta(days=15),now-timedelta(days=12),zone,cfg)
            mid=(now-timedelta(days=13)).astimezone(zone).date()
            self.assertAlmostEqual(use[mid]['caustic'],10.0,places=3)
        days,unpriced=web.chemical_cost(at(0),at(0,today),zone,cfg)
        x=days[y];hours=(at(0,today)-at(0)).total_seconds()/3600
        self.assertAlmostEqual(x['m3'],10.0)
        self.assertAlmostEqual(x['litres']['coag'],0.06*hours,places=3)
        self.assertAlmostEqual(x['cost'],0.06*hours/(55*web.GAL_L)*100,places=4)
        self.assertAlmostEqual(x['per_m3'],x['cost']/10,places=6)
        self.assertEqual(unpriced,[])
        self.assertIn('per m³',web.cost_line(at(0),at(0,today),cfg,'yesterday'))
        with web.appdb() as db:db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('chemical_products',?)",(json.dumps(cfg['chemical_products']),))
        self._login(web.AUTH,'admin','admin-pass')
        code,data=self.call('GET','/api/export','admin','admin-pass',query=f'tag=cost&startDate={y}&endDate={y}')
        self.assertEqual(code,200)
        import zipfile
        self.assertIn('Chemical cost per m',zipfile.ZipFile(io.BytesIO(data)).read('xl/worksheets/sheet1.xml').decode())
        summary=json.loads(self.call('GET','/api/cost','admin','admin-pass')[1])
        self.assertEqual(len(summary['periods']),3);self.assertTrue(summary['priced'])

    def test_acid_and_caustic_in_cost(self):
        zone=web.ZoneInfo('America/Vancouver');now=datetime.now(timezone.utc);start=now-timedelta(hours=5)
        self._samples([('Acid_Pump_Run',start,10.0),('Caustic_Pump_Run',start,20.0)])
        with web.appdb() as db:cfg=web.settings(db)
        cfg['chemical_products']=[dict(p,prices=[{'from':'2020-01-01','price':200.0}],tag='' if p['key'] in ('caustic','sa93') else p['tag']) for p in web.default_products()]
        days,unpriced=web.chemical_cost(start,now,zone,cfg)
        acid=sum(x['litres'].get('pump:Acid_Pump_Run',0) for x in days.values())
        self.assertAlmostEqual(acid,10*5*60/1000,places=1)   # 10 mL/min for 5 h = 3 L, shown but no product: not in the cost
        self.assertEqual(sum(x['cost'] for x in days.values()),0.0);self.assertEqual(unpriced,[])
        s=web.cost_summary(cfg);self.assertIn('Acid pump',s['periods'][2]['unlinked'])
        self.assertIn('Acid pump',web.cost_line(start,now,cfg,'today'))
        # caustic linked to its pump in Settings: counted in the cost at its price
        cfg['chemical_products']=[dict(p,tag='Caustic_Pump_Run') if p['key']=='caustic' else p for p in cfg['chemical_products']]
        days,_=web.chemical_cost(start,now,zone,cfg)
        litres=sum(x['litres'].get('caustic',0) for x in days.values())
        self.assertAlmostEqual(litres,20*5*60/1000,places=1)
        self.assertAlmostEqual(sum(x['cost'] for x in days.values()),litres/(55*web.GAL_L)*200,places=4)
        self.assertNotIn('Caustic pump',web.cost_summary(cfg)['periods'][2]['unlinked'])
        flat=[c for r in web.cost_rows(start,now,zone,'x',cfg) for c in r]
        self.assertTrue(any('Acid pump (L, not in the cost)' in str(c) for c in flat))

    def test_plant_products_11_25(self):
        js=(Path(web.__file__).parent/'static/app.js').read_text(encoding='utf-8')
        for tag in web.DOSING_TAG_CHOICES:self.assertIn(f"'{tag}':",js.split('const MEASURED=')[1].split('};')[0])   # every pump in Settings > Measured by
        P=web.products(None)
        self.assertEqual((P['caustic']['tag'],P['caustic']['part'],P['caustic']['container']),('Caustic_Pump_Run','FNEUT5045-Bulk','55 gal drum (700 lb)'))
        self.assertEqual((P['sa93']['tag'],P['sa93']['label'],P['sa93']['kg_l']),('Acid_Pump_Run','Acid (SA-93)',1.678))
        self.assertEqual((P['floc']['kg_l'],P['coag']['kg_l']),(1.078,1.338))
        # a product list saved before: pumps linked and weights set once, prices kept; a pump already in use is not taken
        saved=[dict(p,tag='',prices=[{'from':'2026-01-01','price':450.0}]) if p['key']=='caustic' else dict(p,tag='') if p['key']=='sa93' else p for p in web.default_products()]
        saved=[dict(p,container='55 gal drum',kg_l=1.52) if p['key']=='caustic' else p for p in saved]
        with web.appdb() as db:
            db.execute("DELETE FROM settings WHERE key='products_v1125'")
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('chemical_products',?)",(json.dumps(saved),))
            web.migrate_products(db);P=web.products(web.settings(db))
            self.assertEqual((P['caustic']['tag'],P['caustic']['price'],P['caustic']['kg_l']),('Caustic_Pump_Run',450.0,1.525))
            self.assertEqual(P['sa93']['tag'],'Acid_Pump_Run')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='Chemical products updated'").fetchone()[0],1)
            db.execute("UPDATE settings SET value=? WHERE key='chemical_products'",(json.dumps(saved),))
            web.migrate_products(db)   # only once: a later change in Settings is not undone
            self.assertEqual(web.products(web.settings(db))['caustic']['tag'],'')

    def test_price_history_uses_the_price_of_each_day(self):
        p=web.norm_product(dict(web.products(None)['coag'],prices=[{'from':'2026-03-01','price':120.0},{'from':'2026-01-01','price':100.0}]))
        self.assertIsNone(web.price_on(p,'2025-12-31'))
        self.assertEqual(web.price_on(p,'2026-02-28'),100.0);self.assertEqual(web.price_on(p,date(2026,3,1)),120.0)
        # a single price saved before the price history keeps applying to everything
        self.assertEqual(web.norm_product(dict(web.products(None)['coag'],prices=None,price=80.0))['prices'],[{'from':'2020-01-01','price':80.0}])
        with web.appdb() as db:
            cfg=web.settings(db);items=cfg['chemical_products']
            new=web.val_settings({'chemical_products':[dict(x,prices=[{'from':'2026-03-01','price':120},{'from':'2026-01-01','price':100}]) if x['key']=='caustic' else x for x in items]},cfg)
            caustic=web.products(new)['caustic']
            self.assertEqual(caustic['prices'],[{'from':'2026-01-01','price':100.0},{'from':'2026-03-01','price':120.0}])   # sorted
            # purchases: estimated at the price of the purchase date
            drum=caustic['container_l'];prods=web.products(new)
            self.assertAlmostEqual(web.purchase_cost({'cost':None,'chemical':'caustic','litres':drum,'purchased_on':'2026-02-10'},prods)[0],100.0)
            self.assertAlmostEqual(web.purchase_cost({'cost':None,'chemical':'caustic','litres':drum,'purchased_on':'2026-03-10'},prods)[0],120.0)
            self.assertIsNone(web.purchase_cost({'cost':None,'chemical':'caustic','litres':drum,'purchased_on':'2025-12-10'},prods)[0])
            # saving the settings audits the price change
            self._login(web.AUTH,'admin','admin-pass')
        code,_=self.call('POST','/api/settings','admin','admin-pass',{'chemical_products':new['chemical_products']})
        self.assertEqual(code,200)
        with web.appdb() as db:
            row=db.execute("SELECT detail FROM audit WHERE action='Chemical prices changed'").fetchone()
        self.assertIn('$120.00 from 2026-03-01',row[0])

    def test_orphan_ph_alarms_are_closed(self):
        # pH high alarms left open by an older version (not the tracked incident) close at the first normal reading
        now=datetime.now(timezone.utc);t=lambda m:web.iso(now-timedelta(minutes=m))
        self._samples([('pH_Reading',now-timedelta(minutes=50),10.5),('pH_Reading',now-timedelta(minutes=40),7.0)])
        with web.appdb() as db:
            old=[db.execute("INSERT INTO events(stamp,kind,detail,state,actor,ack_at) VALUES (?,?,?,?,?,?)",
                            (t(m),'pH high','pH 10.50; limits 5.80–9.00','acknowledged',web.AUTO_ACK_ACTOR,t(m))).lastrowid for m in (55,50)]
            cfg=dict(web.settings(db),alarms_enabled=True)
            web.update_ph_incidents(db,cfg,{'value':7.0,'stamp':t(1),'status':'current'})
            rows=db.execute(f"SELECT state,cleared_at FROM events WHERE id IN ({old[0]},{old[1]})").fetchall()
            self.assertEqual([r['state'] for r in rows],['closed','closed'])
            self.assertEqual({r['cleared_at'] for r in rows},{t(40)})   # when pH came back, not now
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='Old pH alarms closed'").fetchone()[0],1)
            # monitoring switched off: an incident that is still being tracked is ended too
            ev=web.new_alarm_event(db,t(5),'pH high','Confirmed over 2 readings; pH 10.00').lastrowid
            db.execute('UPDATE ph_alarm_v2 SET active_kind=?,active_event=? WHERE id=1',('high',ev))
            web.update_ph_incidents(db,dict(cfg,alarms_enabled=False),None)
            self.assertEqual(db.execute('SELECT state FROM events WHERE id=?',(ev,)).fetchone()[0],'closed')
            self.assertIsNone(db.execute('SELECT active_event FROM ph_alarm_v2').fetchone()[0])

    def test_ph_alarm_follow_up_emails_say_how_long(self):
        self.assertEqual([web.reminder_step(m,5) for m in (0,4,5,9,10,59,60,61,119,125)],[0,0,5,5,10,55,60,60,60,120])
        self.assertEqual(web.reminder_step(30,0),0)
        self.assertEqual([web.minutes_text(m) for m in (0,10,60,75,1440,1590)],['0 min','10 min','1 h','1 h 15 min','1 d','1 d 2 h'])
        now=datetime.now(web.ZoneInfo('America/Vancouver'))
        with web.appdb() as db:
            cfg=dict(web.settings(db),alarms_enabled=True)
            self.assertEqual(cfg['alarm_repeat_min'],30)
            cfg['alarm_repeat_min']=5
            ev=web.new_alarm_event(db,web.iso(datetime.now(timezone.utc)-timedelta(minutes=12)),'pH high','x').lastrowid
            tracker={'active_event':ev,'active_kind':'high'}
            key,subject,body,reading=web.ph_alarm_job(db,cfg,tracker,{'value':9.6},now)
            self.assertEqual(reading,'pH now 9.60, high limit 9.00')
            self.assertEqual(key,f'alarm:incident:{ev}:m10')
            self.assertEqual(subject,'Wastewater pH HIGH alarm — still active 10 min')
            self.assertIn('active for 12 min',body);self.assertIn('9.60 pH',body)
            key,subject,_,_=web.ph_alarm_job(db,dict(cfg,alarm_repeat_min=0),tracker,{'value':9.6},now)
            self.assertEqual((key,subject),(f'alarm:incident:{ev}','Wastewater pH HIGH alarm'))
            self.assertIsNone(web.ph_alarm_emailed(db,ev))
            db.execute('INSERT INTO delivery(key,stamp,outcome) VALUES (?,?,?)',(f'alarm:incident:{ev}:m10','x','sent'))
            self.assertIsNotNone(web.ph_alarm_emailed(db,ev))
            self.assertIsNone(web.ph_alarm_emailed(db,ev*10+1))
        for bad in (3,61,-1,'5'):
            with self.assertRaises(ValueError):web.val_settings({'alarm_repeat_min':bad},dict(web.DEFAULTS))

    def test_every_alarm_gets_still_active_follow_ups(self):
        now=datetime.now(web.ZoneInfo('America/Vancouver'))
        with web.appdb() as db:
            cfg=web.settings(db)
            started=lambda m:web.new_alarm_event(db,web.iso(datetime.now(timezone.utc)-timedelta(minutes=m)),'x','y').lastrowid
            self.assertEqual(web.still_active(db,cfg,started(10),'plc:E_STOP_STATUS',now)[:2],('',''))
            suffix,tail,line=web.still_active(db,cfg,started(31),'plc:E_STOP_STATUS',now)
            self.assertEqual((suffix,tail),(':m30',' — still active 30 min'));self.assertIn('Still active for 31 min',line)
            self.assertEqual(web.still_active(db,cfg,started(75),'data',now)[:2],(':m60',' — still active 1 h'))
            self.assertEqual(web.still_active(db,cfg,started(185),'level',now)[:2],(':m180',' — still active 3 h'))
            for warning in ('ph_trend_high','dosing:coag','stock:acid','order:acid'):   # warnings and supply notices: one email
                self.assertEqual(web.still_active(db,cfg,started(90),warning,now)[:2],('',''))
            ev=started(40)
            db.execute('INSERT INTO delivery(key,stamp,outcome) VALUES (?,?,?)',(f'alarm:incident:{ev}:m30','x','sent'))
            self.assertIsNotNone(web.alarm_emailed(db,ev))   # recovery email follows a follow-up-only alarm too

    def test_text_message_has_the_reading(self):
        now=datetime(2026,11,22,17,19,tzinfo=web.ZoneInfo('America/Vancouver'))
        self.assertEqual(web.sms_text('Wastewater pH LOW alarm — active 15 min',52,now,'pH now 5.42, low limit 5.80'),
            'Wastewater pH LOW alarm — active 15 min. pH now 5.42, low limit 5.80. At Nov 22 17:19. See the dashboard Alarms page.')
        self.assertEqual(web.sms_text('X',1,now),'X. At Nov 22 17:19. See the dashboard Alarms page.')
        self.assertEqual(web.sms_text('pH trending high',3,now,'pH 8.60 is rising 1.20 pH per hour; it reaches 9.00 in about 20 minutes. Check the acid dosing.'),
            'pH trending high. pH 8.60 is rising 1.20 pH per hour; it reaches 9.00 in about 20 minutes. At Nov 22 17:19. See the dashboard Alarms page.')
        self.assertLessEqual(len(web.sms_text('X'*100,1,now,'y'*400)),300)

    def test_list_periods(self):
        d=date(2026,11,23)   # a Monday
        r=lambda **q:web.period_range(q,d)
        self.assertEqual(r(period='today'),(d,date(2026,11,24)))
        self.assertEqual(r(period='yesterday'),(date(2026,11,22),d))
        self.assertEqual(r(period='week'),(d,date(2026,11,24)))
        self.assertEqual(r(period='7d'),(date(2026,11,17),date(2026,11,24)))
        self.assertEqual(r(period='lastmonth'),(date(2026,10,1),date(2026,11,1)))
        self.assertEqual(r(period='3m'),(date(2026,9,1),date(2026,11,24)))
        self.assertEqual(r(period='pickmonth',month='2026-02'),(date(2026,2,1),date(2026,3,1)))
        self.assertEqual(r(period='day',date='2026-11-05'),(date(2026,11,5),date(2026,11,6)))
        self.assertEqual(r(period='all'),(None,None))
        for bad in (dict(period='x'),dict(period='day',date='nope'),dict(period='pickmonth',month='2026')):
            with self.assertRaises(ValueError):r(**bad)
        with web.appdb() as db:
            now=datetime.now(timezone.utc)
            for h in (1,30,24*40):web.audit(db,'t','Test entry',f'{h} h ago')
            db.execute("UPDATE audit SET stamp=? WHERE detail='960 h ago'",(web.iso(now-timedelta(days=40)),))
            db.execute("UPDATE audit SET stamp=? WHERE detail='30 h ago'",(web.iso(now-timedelta(hours=30)),))
            rows,info=web.period_rows(db,'audit','stamp',{'period':'7d'})
            self.assertNotIn('960 h ago',[r['detail'] for r in rows]);self.assertIn('30 h ago',[r['detail'] for r in rows])
            self.assertEqual(info['shown'],len(rows))
            rows,_=web.period_rows(db,'audit','stamp',{'period':'all'})
            self.assertIn('960 h ago',[r['detail'] for r in rows])
        self._login(web.AUTH,'admin','admin-pass')
        code,body=self.call('GET','/api/events','admin','admin-pass',query='period=month')
        self.assertEqual(code,200);self.assertIn('period',json.loads(body))
        code,_=self.call('GET','/api/audit','admin','admin-pass',query='period=bogus');self.assertEqual(code,400)
        with web.appdb() as db:
            now=datetime.now(timezone.utc)
            for d in (40,10,2):db.execute('INSERT INTO sludge_pickups(picked_at,note,logged_by,logged_at) VALUES (?,?,?,?)',(web.iso(now-timedelta(days=d)),'','t','x'))
            db.commit()
        d=json.loads(self.call('GET','/api/pickups','admin','admin-pass',query='period=7d')[1])
        self.assertEqual([r['gap_days'] for r in d['rows']],[8.0])   # gap to the pickup before the period
        d=json.loads(self.call('GET','/api/calibrations','admin','admin-pass',query='period=year')[1])
        self.assertIn('list',d);self.assertIn('period',d)

    def test_purchase_currency_weight_and_entered_quantity(self):
        with web.appdb() as db:
            cfg=web.settings(db)
            self.assertEqual(cfg['currency'],'CAD')
            caustic=web.products(cfg)['caustic'];self.assertEqual(caustic['kg_l'],1.525)   # 700 lb per 55 gal drum
            self.assertEqual(web.products(cfg)['floc']['kg_l'],1.078)   # 45 lb per 5 gal pail
            body=dict(chemical='caustic',purchased_on='2026-11-01',litres=328.95,cost=500.0,currency='USD',entered='500 kg')
            pid=web.save_purchase(db,body,'t',cfg)[0]
            web.save_purchase(db,dict(body,currency=None,cost=900.0,entered=None),'t',cfg)
            with self.assertRaises(web.AccountError):web.save_purchase(db,dict(body,currency='EUR'),'t',cfg)
            row=db.execute('SELECT currency,entered FROM chemical_purchases WHERE id=?',(pid,)).fetchone()
            self.assertEqual((row['currency'],row['entered']),('USD','500 kg'))
            sid=web.save_stock(db,dict(chemical='caustic',kind='count',litres=100.0,when=datetime.now(web.ZoneInfo('America/Vancouver')).strftime('%Y-%m-%dT%H:%M'),entered='334 lb'),'t',cfg)[0]
            self.assertEqual(db.execute('SELECT entered FROM chemical_stock WHERE id=?',(sid,)).fetchone()[0],'334 lb')
            db.commit()
        rows=web.purchase_rows(2026,cfg)
        totals=[r for r in rows if len(r)>6 and isinstance(r[1],tuple) and str(r[1][0]).startswith('Total ')]
        self.assertIn(('Total USD',3),[r[1] for r in totals]);self.assertIn(('Total CAD',3),[r[1] for r in totals])
        # weight per litre is validated in Settings
        items=[dict(p) for p in cfg['chemical_products']];items[1]['kg_l']=1.1
        out=web.val_products(items,cfg['chemical_products']);self.assertEqual(out[1]['kg_l'],1.1)
        items[1]['kg_l']=9
        with self.assertRaises(ValueError):web.val_products(items,cfg['chemical_products'])
        with self.assertRaises(ValueError):web.val_settings({'currency':'EUR'},dict(web.DEFAULTS))

    def test_separate_alarm_email_and_settings_rights(self):
        A=('admin','admin-pass');self._login(web.AUTH,*A)
        self.assertEqual(self.call('POST','/api/accounts/user',*A,{'role':'Engineering','username':'eng','password':'eng-pass-1'})[0],200)
        # Engineering: may change email settings and shelve alarms, may view settings, may not change other settings
        self.assertEqual(self.call('POST','/api/accounts/permissions',*A,{'Engineering':['alarms','shelve','email','settings_view'],'Operator':[],'Guest':[]})[0],200)
        E=('eng','eng-pass-1')
        self.assertEqual(self.call('GET','/api/settings',*E)[0],200)
        self.assertEqual(self.call('POST','/api/settings',*E,{'recipients':['ops@example.com'],'daily_enabled':True})[0],200)
        with web.appdb() as db:self.assertEqual(web.settings(db)['recipients'],['ops@example.com'])
        code,body=self.call('POST','/api/settings',*E,{'ph_high':9.5})
        self.assertEqual(code,403);self.assertIn('only the email',json.loads(body)['error'])
        self.assertEqual(self.call('POST','/api/shelves',*E,{'key':'flow','hours':2,'reason':'pump repair'})[0],200)
        self.assertEqual(self.call('GET','/api/events',*E)[0],200)
        # without the alarms right the Alarms page data is refused; without shelve, shelving is refused
        self.assertEqual(self.call('POST','/api/accounts/permissions',*A,{'Engineering':['settings_view'],'Operator':[],'Guest':[]})[0],200)
        self.assertEqual(self.call('GET','/api/events',*E)[0],403)
        self.assertEqual(self.call('POST','/api/shelves',*E,{'key':'flow','hours':2,'reason':'x'})[0],403)
        self.assertEqual(self.call('POST','/api/settings',*E,{'recipients':[]})[0],403)
        self.assertEqual(self.call('GET','/api/settings',*E)[0],200)   # still read-only view
        self.assertIn('delete',web.role_permissions()['Admin']);self.assertNotIn('delete',web.PERMISSION_CHOICES)

    def test_insights_find_repeated_alarms(self):
        zone=web.ZoneInfo('America/Vancouver')
        today=datetime.now(zone).date()
        with web.appdb() as db:
            for d in range(1,5):
                t=datetime.combine(today-timedelta(days=d),datetime.min.time(),zone).replace(hour=7)
                web.new_alarm_event(db,web.iso(t),'Rake motor faulted','Rake motor faulted. Check the DAF rake motor and its VFD.')
        out=web.insights(dict(web.DEFAULTS))
        rake=next(i for i in out if i['title']=='Repeated alarm: Rake motor faulted')
        self.assertIn('4 times',rake['detail']);self.assertIn('between 06:00 and 09:00',rake['detail']);self.assertIn('VFD',rake['action'])
        self._login(web.AUTH,'admin','admin-pass')
        code,body=self.call('GET','/api/insights','admin','admin-pass')
        self.assertEqual(code,200);self.assertEqual(json.loads(body)['days'],30)

if __name__=='__main__':unittest.main(verbosity=2)
