"""Regression checks using only temporary databases. Does not contact live VPS or SMTP."""
import io
import json
import os
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
        assert s['revision']=='1.2.8'
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

    def test_excel_report(self):
        import zipfile
        today=datetime.now(web.ZoneInfo('America/Vancouver')).date()
        params={'startDate':[(today-timedelta(days=1)).isoformat()],'endDate':[today.isoformat()]}
        fname,data=web.report_xlsx({**params,'tag':['all']})
        self.assertTrue(fname.endswith('.xlsx'))
        z=zipfile.ZipFile(io.BytesIO(data))
        book=z.read('xl/workbook.xml').decode()
        for name in ('Summary','EQ_Tank_Level_Ft','FLOW_RATE','Total_Flow_Li'):self.assertIn(f'name="{name}"',book)
        self.assertNotIn('pH_Reading',book)
        flow=z.read('xl/worksheets/sheet3.xml').decode()
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
        self.assertEqual(values,[524714.25,525014.37,524714.25,525014.37,524714.25,524714.25,300.12])
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
        # Switch user: current role is refused with a sign-in prompt; a different role returns to '/'
        self.assertEqual(self.call('GET','/switch','op','op-pass',query='from=Operator')[0],401)
        self.assertEqual(self.call('GET','/switch','admin','admin-pass',query='from=Operator')[0],302)
        self.assertEqual(self.call('GET','/switch','admin','admin-pass',query='from=Admin')[0],401)

    def test_accounts_admin_and_operator_logins(self):
        web.OP_AUTH=Path(self.tmp.name)/'operator.json'
        self._login(web.AUTH,'admin','admin-pass-1')
        self._login(web.OP_AUTH,'op','op-pass-123')
        A=('admin','admin-pass-1')
        # Only Admin may see or manage accounts
        self.assertEqual(self.call('GET','/api/accounts',*A)[0],200)
        self.assertEqual(self.call('GET','/api/accounts','op','op-pass-123')[0],403)
        self.assertEqual(self.call('POST','/api/accounts/operator','op','op-pass-123',{'username':'x','password':'long-enough-pw'})[0],403)
        # Admin sets a new Operator login: it replaces the original operator file login
        self.assertEqual(self.call('POST','/api/accounts/operator',*A,{'username':'shift','password':'abc'})[0],400)  # Operator minimum 4
        self.assertEqual(self.call('POST','/api/accounts/operator',*A,{'username':'ADMIN','password':'long-enough-pw'})[0],400)
        self.assertEqual(self.call('POST','/api/accounts/operator',*A,{'username':'shift','password':'shift-pass-99'})[0],200)
        self.assertEqual(self.call('GET','/api/summary','shift','shift-pass-99')[0],200)
        self.assertEqual(self.call('GET','/api/summary','op','op-pass-123')[0],401)
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
        self.assertEqual(info['operator']['username'],'shift')
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

if __name__=='__main__':unittest.main(verbosity=2)
