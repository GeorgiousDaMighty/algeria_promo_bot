import json
import tempfile
import unittest
from pathlib import Path
from bot import Bot, Store, phone, amount, deliver

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'db')
        self.s=Store(self.path); self.b=Bot(self.s,1,-100123); self.n=0
        self.send('/start'); self.send('Amine Benali')
    def tearDown(self): self.s.db.close(); self.tmp.cleanup()
    def state(self): return json.loads(self.s.q('SELECT state FROM users WHERE id=2').fetchone()[0])
    def send(self,text='',action=None,uid=2,**extra):
        self.n+=1
        msg={'chat':{'id':uid,'type':'private'},'from':{'id':uid},'text':text,**extra}
        up={'update_id':self.n,'message':msg}
        if action:
            rev=self.s.q('SELECT revision FROM users WHERE id=?',(uid,)).fetchone()[0]
            up={'update_id':self.n,'callback_query':{'id':str(self.n),'from':{'id':uid},'message':msg,'data':f'{rev}|{action}'}}
        self.b.handle(up); return up
    def start(self): self.send(action='new')
    def test_success_restart_idempotency(self):
        up=self.send(action='new'); self.b.handle(up)
        self.assertEqual(self.s.q('SELECT count(*) FROM contacts').fetchone()[0],1)
        self.send(action='yes'); self.send('+213555123456'); self.send(action='next')
        self.s.db.close(); self.s=Store(self.path); self.b=Bot(self.s,1,-100123)
        self.assertEqual(self.state()['stage'],'slick')
        for _ in range(4): self.send(action='next')
        self.send(action='skip')
        self.assertEqual(self.s.q('SELECT status FROM contacts').fetchone()[0],'completed')
        events=[r[0] for r in self.s.q('SELECT kind FROM events')]
        self.assertEqual(events.count('ride_done'),1); self.assertEqual(events.count('topup_success'),1)
    def test_payment_failure_does_not_mark_success(self):
        self.start(); self.send(action='yes'); self.send('+213555123456')
        for _ in range(3): self.send(action='next')
        self.assertEqual(self.state()['stage'],'paid')
        self.send(action='failure'); self.send(action='submit')
        self.assertEqual(self.state()['stage'],'paid')
        self.assertEqual(self.s.q("SELECT count(*) FROM events WHERE kind='topup_success'").fetchone()[0],0)
        self.send(action='abort'); self.send('Paiement impossible'); self.send(action='skip')
        self.assertEqual(self.s.q('SELECT status FROM contacts').fetchone()[0],'aborted')
    def test_midnight_daily_report(self):
        # 23:00 UTC is midnight in Algeria.
        from datetime import datetime, timezone
        t=datetime(2026,9,23,22,tzinfo=timezone.utc).timestamp()
        self.b.scheduled(t); self.b.scheduled(t+3600)
        texts=[json.loads(r[0]).get('text','') for r in self.s.q('SELECT payload FROM outbox')]
        self.assertEqual(sum('RÉCAPITULATIF QUOTIDIEN' in t for t in texts),1)
    def test_cohort_conversion(self):
        self.start(); self.send(action='yes'); self.send('+213555123456')
        for _ in range(5): self.send(action='next')
        self.send(action='skip'); self.start()
        self.assertIn('conversion course 50%',self.b.report())
    def test_stale_button(self):
        up=self.send(action='new'); up['update_id']=1000; self.b.handle(up)
        self.assertEqual(self.state()['stage'],'interest')
        self.assertEqual(self.s.q('SELECT count(*) FROM contacts').fetchone()[0],1)
    def test_incident_media_and_resume(self):
        self.start(); self.send('Bug observé',photo=[{'file_id':'photo-a'}]); self.send('',video={'file_id':'video-b'}); self.send(action='submit')
        self.assertEqual(self.state()['stage'],'interest'); self.assertEqual(self.state()['mode'],'flow')
        data=json.loads(self.s.q('SELECT data FROM incidents').fetchone()[0]); self.assertEqual(len(data['media']),2)
        methods=[r[0] for r in self.s.q('SELECT method FROM outbox')]
        self.assertIn('sendPhoto',methods); self.assertIn('sendVideo',methods)
    def test_incident_description_required(self):
        self.start(); self.send('',photo=[{'file_id':'a'}]); self.send(action='submit')
        self.assertEqual(self.state()['mode'],'incident'); self.assertEqual(self.s.q('SELECT count(*) FROM incidents').fetchone()[0],0)
    def test_refusal_multiselect(self):
        self.start(); self.send(action='no'); self.send(action='reason:1')
        self.send(action='pay:1'); self.send(action='pay:2'); self.send(action='pay:8'); self.send('Banque locale')
        self.send(action='paydone'); self.send(action='noanswer'); self.send('Rappel demain')
        r=self.s.q('SELECT * FROM contacts').fetchone(); data=json.loads(r['data'])
        self.assertEqual(r['status'],'refused'); self.assertEqual(len(data['payments']),3)
        self.assertEqual(data['comment'],'Rappel demain'); self.assertIsNone(data['change_decision'])
    def test_price_abort(self):
        self.start(); self.send(action='no'); self.send(action='reason:2'); self.send('50,50')
        self.assertEqual(self.state()['data']['acceptable_price_dzd'],'50.50')
        self.send(action='abort'); self.send(''); self.assertEqual(self.state()['mode'],'abort')
        self.send('Client parti'); self.send(action='skip')
        self.assertEqual(self.s.q('SELECT status FROM contacts').fetchone()[0],'aborted')
    def test_manual_topup(self):
        self.send(action='topup'); self.send('0555123456'); self.assertEqual(self.state()['step'],'phone')
        self.send('213555123456'); self.send(action='promo'); self.send('100'); self.send(action='skip')
        self.assertEqual(self.s.q('SELECT status FROM topups').fetchone()[0],'pending')
        self.send('/resolve 1'); self.assertEqual(self.s.q('SELECT status FROM topups').fetchone()[0],'pending')
        self.send('/resolve 1',uid=1); self.assertEqual(self.s.q('SELECT status FROM topups').fetchone()[0],'done')
    def test_permissions_join_requests(self):
        self.send('/grant 3'); self.assertFalse(self.b.manager(3))
        self.send('/grant 3',uid=1); self.assertTrue(self.b.manager(3))
        self.b.handle({'update_id':100,'chat_join_request':{'chat':{'id':-100123},'from':{'id':3}}})
        self.assertEqual(self.s.q('SELECT method FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0],'approveChatJoinRequest')
        self.send('/revoke 3',uid=1); self.assertFalse(self.b.manager(3))
        self.b.handle({'update_id':101,'chat_join_request':{'chat':{'id':-100123},'from':{'id':3}}})
        self.assertEqual(self.s.q('SELECT method FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0],'declineChatJoinRequest')
    def test_schedule_once_catchup(self):
        self.b.scheduled(360000); self.b.scheduled(367200); self.b.scheduled(367200)
        reports=[json.loads(r[0]) for r in self.s.q('SELECT payload FROM outbox') if 'activité' in json.loads(r[0]).get('text','')]
        self.assertEqual(len(reports),2)
    def test_export_permissions(self):
        self.send('/export'); self.assertEqual(self.s.q("SELECT count(*) FROM outbox WHERE method='_csv'").fetchone()[0],0)
        self.send('/export',uid=1); self.assertEqual(self.s.q("SELECT count(*) FROM outbox WHERE method='_csv'").fetchone()[0],1)
    def test_delivery_retry(self):
        class Failing:
            def call(self,*a,**k): raise OSError('offline')
        count=self.s.q('SELECT count(*) FROM outbox').fetchone()[0]; deliver(self.s,Failing())
        self.assertEqual(self.s.q('SELECT count(*) FROM outbox').fetchone()[0],count)
        self.assertGreater(self.s.q('SELECT max(tries) FROM outbox').fetchone()[0],0)

class ValidationTests(unittest.TestCase):
    def test_phone(self):
        accepted = {
            '+213555123456': '+213555123456',
            '213 555 123 456': '+213555123456',
            '+213101283986': '+213101283986',
            '213101283986': '+213101283986',
            '+2130001010101': '+2130001010101',
            '213010121211': '+213010121211',
            '+2131': '+2131',
            '+2135551234567': '+2135551234567',
        }
        for raw, normalized in accepted.items():
            self.assertEqual(phone(raw), normalized)
        for p in ['0555123456','+33123456789','+213','213','+213abc']:
            with self.assertRaises(ValueError): phone(p)
    def test_amount(self):
        for p in ['NaN','Infinity','-1','0','1.234','hello']:
            with self.assertRaises(ValueError): amount(p)

if __name__=='__main__': unittest.main()
