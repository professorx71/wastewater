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
        assert s['revision']=='1.2.1'
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

    def test_static_html_has_no_old_embed_sections(self):
        base=Path(web.__file__).parent/'static'
        html=(base/'index.html').read_text()
        self.assertNotIn('id="overviewCharts"',html)
        self.assertNotIn('class="process"',html)
        self.assertIn('id="tankPh"',html)
        self.assertIn('id="diagramTotal"',html)
        self.assertTrue((base/'process-overview.webp').stat().st_size>10000)

if __name__=='__main__':unittest.main(verbosity=2)
