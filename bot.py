"""JET Algeria field tracker. Python 3.12+, SQLite, Telegram Bot API."""
import csv
import io
import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Africa/Algiers')
REASONS = ['Pas le temps / plus tard', 'Pas de fonds en ligne', 'Trop cher',
           'Ne sait pas rouler / peur', 'Mémoire téléphone insuffisante',
           'Pas de smartphone / Internet', 'Pas de moyen de paiement compatible', 'Autre']
PAYMENTS = ['Espèces', 'Carte EDAHABIA', 'BaridiMob / BaridiWeb', 'Carte CIB',
            'Application bancaire', 'SlickPay', 'Visa / Mastercard', 'Aucun', 'Autre']
STAGES = {
 'interest': ('La personne souhaite essayer JET ?', [('Oui', 'yes'), ('Non / impossible', 'no')]),
 'phone': ('Numéro utilisé pour JET, avec son accord : commencez par +213 ou 213. Exemple : +2130101283986.', []),
 'jet': ('Aidez la personne à installer JET et à créer son compte.', [('JET installé / compte prêt', 'next')]),
 'slick': ('Aidez la personne à installer et configurer SlickPay pour le paiement JET.', [('SlickPay prêt', 'next')]),
 'attempt': ('Demandez à la personne de tenter de recharger son portefeuille JET.', [('Tentative effectuée', 'next')]),
 'paid': ('Vérifiez le résultat du paiement.', [('Recharge réussie', 'next'), ('Échec du paiement', 'failure')]),
 'ride': ('Le portefeuille est rechargé. Aidez la personne à démarrer sa première course.', [('Course effectuée', 'next')]),
}
NEXT = {'jet':'slick', 'slick':'attempt', 'attempt':'paid', 'paid':'ride'}
MILESTONES = {'jet':'jet_ready', 'slick':'slick_ready', 'attempt':'topup_attempted',
              'paid':'topup_success', 'ride':'ride_done'}


def phone(value):
    value = re.sub(r'[\s()\-]', '', value)
    if not re.fullmatch(r'\+?213[0-9]+', value):
        raise ValueError('Format requis : numéro commençant par +213 ou 213, suivi uniquement de chiffres.')
    return '+' + value.lstrip('+')


def amount(value):
    try:
        n = Decimal(value.replace(',', '.'))
        if not n.is_finite() or n <= 0 or n > 1000000 or n.as_tuple().exponent < -2:
            raise InvalidOperation
        return str(n)
    except InvalidOperation:
        raise ValueError('Indiquez un montant positif en DZD, avec au maximum 2 décimales.')


def stamp(t):
    return datetime.fromtimestamp(t, TZ).strftime('%d/%m/%Y %H:%M')


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS managers(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
          started REAL NOT NULL, ended REAL, status TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, contact_id INTEGER,
          user_id INTEGER NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_at ON events(at);
        CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY, contact_id INTEGER,
          user_id INTEGER NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
          at REAL NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS processed(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, method TEXT NOT NULL,
          payload TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0);
        ''')
        self.db.commit()

    def q(self, sql, args=()):
        return self.db.execute(sql, args)

    def queue(self, method, **payload):
        self.q('INSERT INTO outbox(method,payload) VALUES(?,?)', (method, json.dumps(payload)))

    def say(self, chat, text, buttons=None):
        # Telegram limits messages to 4096 characters. Split long reports safely.
        for i in range(0, len(text), 3500):
            args = {'chat_id':chat, 'text':text[i:i+3500]}
            if buttons and i + 3500 >= len(text):
                args['reply_markup'] = {'inline_keyboard':[[{'text':label, 'callback_data':key}] for label,key in buttons]}
            self.queue('sendMessage', **args)

    def event(self, uid, cid, kind, data=None):
        self.q('INSERT INTO events(contact_id,user_id,at,kind,data) VALUES(?,?,?,?,?)',
               (cid,uid,time.time(),kind,json.dumps(data or {}, ensure_ascii=False)))


class Bot:
    def __init__(self, store, owner, group):
        self.s, self.owner, self.group = store, owner, group

    def manager(self, uid):
        return uid == self.owner or self.s.q('SELECT 1 FROM managers WHERE id=?',(uid,)).fetchone() is not None

    def save(self, uid, state):
        self.s.q('UPDATE users SET state=?,revision=revision+1 WHERE id=?', (json.dumps(state),uid))

    def prompt(self, uid, st):
        mode = st.get('mode','menu')
        buttons = []
        if mode == 'menu':
            text = 'JET • Terrain\nChoisissez une action.'
            buttons = [('Nouvelle personne approchée','new'), ('Demander une recharge manuelle','topup')]
            if self.manager(uid):
                buttons += [('Rapport : tout l’historique','report'), ('Exporter CSV','export')]
        elif mode == 'name':
            text = 'Bienvenue. Envoyez votre prénom et nom pour activer votre profil promoteur.'
        elif mode == 'flow':
            stage = st['stage']
            if stage in STAGES:
                text, buttons = STAGES[stage]
                buttons = list(buttons)
            elif stage == 'reason':
                text = 'Pourquoi la personne ne souhaite-t-elle pas essayer ?'
                buttons = [(x, 'reason:'+str(i)) for i,x in enumerate(REASONS)]
            elif stage == 'other_reason':
                text = 'Décrivez la raison du refus.'
            elif stage == 'payments':
                text = 'Quels moyens utilise-t-elle habituellement ? Plusieurs choix possibles. Puis Terminer.'
                selected = st['data'].get('payments',[])
                buttons = [('✓ '+x if x in selected else x, 'pay:'+str(i)) for i,x in enumerate(PAYMENTS)]
                buttons += [('Terminer la sélection','paydone')]
            elif stage == 'payment_other':
                text = 'Quel autre moyen de paiement utilise-t-elle ?'
            elif stage == 'price':
                text = 'Quel montant en DZD serait acceptable pour une première course ?'
                buttons = [('Pas de réponse','noanswer')]
            elif stage == 'followup':
                text = ('Que pourrait-on changer pour vous donner envie d’essayer ? Qu’est-ce qui vous bloque vraiment ?\n'
                        'Script : « Votre réponse nous aide à améliorer le service. Une réponse courte suffit, vous pouvez refuser. »\n'
                        'Notez sa réponse.')
                buttons = [('Pas de réponse complémentaire','noanswer')]
            else:
                text = 'Commentaire complémentaire sur ce contact ? Envoyez un texte ou passez.'
                buttons = [('Sans commentaire / terminer','skip')]
            if stage != 'comment':
                buttons += [('Signaler un incident','incident'), ('Interrompre ce contact','abort')]
        elif mode == 'incident':
            text = ('Incident • étape : '+st['stage']+'\nEnvoyez une description puis des photos, vidéos ou documents. '
                    'Les pièces jointes sont regroupées dans ce dossier. Aucun code secret ni donnée de carte.\n'
                    'Appuyez sur Envoyer lorsque toutes les pièces sont reçues.')
            buttons = [('Envoyer le dossier','submit'), ('Annuler le brouillon','cancelincident')]
        elif mode == 'abort':
            text = 'Pourquoi interrompez-vous ce contact ? Motif obligatoire.'
            buttons = [('Continuer le contact','resume')]
        elif mode == 'topup':
            step = st['step']
            if step == 'phone':
                text = 'Numéro JET à recharger : commencez par +213 ou 213, puis saisissez les chiffres du numéro.'
            elif step == 'reason':
                text = 'Motif de la recharge manuelle ?'
                buttons = [('Compensation : paiement non reçu','compensation'), ('Bonus promotionnel','promo')]
            elif step == 'amount':
                text = 'Montant demandé en DZD ? La validation et la recharge restent manuelles.'
            else:
                text = 'Commentaire complémentaire ?'
                buttons = [('Sans commentaire / envoyer','skip')]
            buttons += [('Annuler la demande','cancel')]
        else:
            raise ValueError('Unknown state')
        rev = self.s.q('SELECT revision FROM users WHERE id=?',(uid,)).fetchone()[0]
        self.s.say(uid,text,[(label,f'{rev}|{key}') for label,key in buttons])

    def contact_save(self, uid, st, status='active'):
        self.s.q('UPDATE contacts SET data=?,status=?,ended=? WHERE id=? AND user_id=?',
                 (json.dumps(st['data'],ensure_ascii=False),status,None if status=='active' else time.time(),st['cid'],uid))

    def close(self, uid, st):
        status = st.get('outcome','refused')
        self.contact_save(uid,st,status)
        self.s.event(uid,st['cid'],'closed',{'status':status})
        st.clear()
        st['mode']='menu'

    def report(self, start=0, end=None):
        end = end or time.time()
        rows = self.s.q('''SELECT u.name,e.user_id,e.kind,COUNT(*) n FROM events e JOIN users u ON u.id=e.user_id
          WHERE e.at>=? AND e.at<? GROUP BY e.user_id,e.kind ORDER BY e.user_id,e.kind''',(start,end)).fetchall()
        text = f'JET • activité {stamp(start) if start else "depuis le début"} → {stamp(end)} (Alger)\n'
        text += 'Événements de la période, pas une cohorte : les étapes peuvent concerner des contacts plus anciens.\n'
        names = {'approached':'Approchés','interested':'Intéressés','phone_saved':'Téléphone saisi',
                 'jet_ready':'JET prêt','slick_ready':'SlickPay prêt','topup_attempted':'Tentatives recharge',
                 'topup_success':'Recharges réussies','ride_done':'Courses','incident':'Incidents',
                 'refused':'Refus','aborted':'Interruptions','manual_topup':'Demandes recharge', 'closed':'Contacts terminés'}
        for r in rows:
            if r['kind'] in names:
                text += f"{r['name']} ({r['user_id']}) • {names[r['kind']]} : {r['n']}\n"
        if not rows:
            text += 'Aucune activité.\n'
        reasons = self.s.q("SELECT data FROM events WHERE kind='refused' AND at>=? AND at<?",(start,end)).fetchall()
        counts={}
        for r in reasons:
            reason=json.loads(r[0])['reason']; counts[reason]=counts.get(reason,0)+1
        if counts:
            text += '\nMotifs de refus :\n'+'\n'.join(f'{k}: {v}' for k,v in counts.items())+'\n'
        active=self.s.q("SELECT COUNT(*) FROM contacts WHERE status='active'").fetchone()[0]
        text += f'\nContacts encore ouverts (total actuel) : {active}'
        cohorts=self.s.q('''SELECT c.user_id,u.name,c.status,c.data FROM contacts c JOIN users u ON u.id=c.user_id
                            WHERE c.started>=? AND c.started<?''',(start,end)).fetchall()
        stats={}
        for c in cohorts:
            key=(c['user_id'],c['name']); entry=stats.setdefault(key,{'total':0,'rides':0,'paid':0,'active':0})
            entry['total']+=1; data=json.loads(c['data'])
            entry['rides']+=int('ride_done' in data); entry['paid']+=int('topup_success' in data)
            entry['active']+=int(c['status']=='active')
        if stats:
            text += '\n\nCohorte : contacts commencés pendant la période, progression connue au moment du rapport.\n'
            for (pid,name),v in stats.items():
                text += f"{name} ({pid}) : {v['total']} contacts, {v['paid']} payés, {v['rides']} courses, conversion course {v['rides']/v['total']:.0%}, {v['active']} ouverts.\n"

        for c in cohorts:
            d=json.loads(c['data'])
            details={k:d[k] for k in ['reason','reason_other','payments','acceptable_price_dzd','change_decision','abort_reason','comment'] if d.get(k) is not None and d.get(k) != ''}
            if details:
                text += '\n'+c['name']+' • '+json.dumps(details,ensure_ascii=False)
        return text

    def export(self, uid):
        out=io.StringIO(); w=csv.writer(out)
        w.writerow(['event_id','time_alger','promoter_id','name','contact_id','event','details','contact_status','contact_data'])
        for r in self.s.q('''SELECT e.*,u.name,c.status,c.data contact_data FROM events e JOIN users u ON u.id=e.user_id
                            LEFT JOIN contacts c ON c.id=e.contact_id ORDER BY e.id'''):
            values=[r['id'],stamp(r['at']),r['user_id'],r['name'],r['contact_id'],r['kind'],r['data'],r['status'],r['contact_data']]
            w.writerow([("'"+str(v)) if str(v).startswith(('=','+','-','@','\t','\r')) else v for v in values])
        self.s.queue('_csv',chat_id=uid,content=out.getvalue())

    def admin_command(self, uid, text):
        parts=text.split(); cmd=parts[0].split('@')[0]
        if cmd=='/id':
            self.s.say(uid,f'Votre identifiant Telegram : {uid}'); return True
        if cmd in ['/report','/export']:
            if not self.manager(uid): self.s.say(uid,'Accès réservé aux responsables.'); return True
            if cmd=='/report': self.s.say(uid,self.report())
            else: self.export(uid)
            return True
        if cmd in ['/grant','/revoke','/invite','/resolve']:
            if uid!=self.owner: self.s.say(uid,'Action réservée à l’administrateur principal.'); return True
            if len(parts)!=2 or not parts[1].isdigit():
                self.s.say(uid,f'Usage : {cmd} identifiant_numérique'); return True
            target=int(parts[1])
            if cmd=='/grant':
                self.s.q('INSERT OR IGNORE INTO managers VALUES(?)',(target,))
                self.s.event(uid,None,'manager_granted',{'id':target})
                self.s.say(uid,'Accès analytique accordé. /invite ID pour demander une invitation au groupe.')
            elif cmd=='/revoke':
                if target==self.owner: self.s.say(uid,'Impossible de retirer le propriétaire.'); return True
                self.s.q('DELETE FROM managers WHERE id=?',(target,))
                self.s.queue('banChatMember',chat_id=self.group,user_id=target)
                self.s.event(uid,None,'manager_revoked',{'id':target})
                self.s.say(uid,'Accès bot retiré. Retrait du groupe mis en file d’attente.')
            elif cmd=='/invite':
                if not self.manager(target): self.s.say(uid,'Accordez d’abord /grant ID.'); return True
                self.s.queue('unbanChatMember',chat_id=self.group,user_id=target,only_if_banned=True)
                self.s.queue('_invite',chat_id=self.group,target=target,owner=uid)
            else:
                row=self.s.q("SELECT user_id FROM topups WHERE id=? AND status='pending'",(target,)).fetchone()
                if not row: self.s.say(uid,'Demande absente ou déjà traitée.'); return True
                self.s.q("UPDATE topups SET status='done' WHERE id=?",(target,))
                self.s.event(uid,None,'manual_topup_resolved',{'request':target})
                self.s.say(self.group,f'Recharge manuelle #{target} marquée traitée par {uid}.')
                self.s.say(row[0],f'Recharge manuelle #{target} marquée traitée.')
            return True
        return False

    def handle(self, update):
        s=self.s
        with s.db:
            if s.q('SELECT 1 FROM processed WHERE id=?',(update['update_id'],)).fetchone(): return
            join=update.get('chat_join_request')
            if join:
                if join['chat']['id']==self.group:
                    s.queue('approveChatJoinRequest' if self.manager(join['from']['id']) else 'declineChatJoinRequest',
                            chat_id=self.group,user_id=join['from']['id'])
                s.q('INSERT INTO processed VALUES(?)',(update['update_id'],)); return
            cb=update.get('callback_query'); msg=(cb or {}).get('message') or update.get('message')
            if not msg or msg.get('chat',{}).get('type')!='private':
                s.q('INSERT INTO processed VALUES(?)',(update['update_id'],)); return
            uid=(cb or msg)['from']['id']; text=msg.get('text','') if not cb else ''
            if cb: s.queue('answerCallbackQuery',callback_query_id=cb['id'])
            s.q("INSERT OR IGNORE INTO users(id,name,state) VALUES(?,?,'{\"mode\":\"name\"}')",(uid,''))
            row=s.q('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); st=json.loads(row['state'])
            action=''
            if cb:
                bits=cb.get('data','').split('|',1)
                if len(bits)!=2 or bits[0]!=str(row['revision']):
                    s.say(uid,'Ce bouton a expiré. Utilisez le dernier message ou /start.')
                    s.q('INSERT INTO processed VALUES(?)',(update['update_id'],)); return
                action=bits[1]
            try:
                if text.startswith('/') and self.admin_command(uid,text): pass
                elif text in ['/start','/menu']:
                    self.save(uid,st); self.prompt(uid,st)
                else:
                    self.advance(uid,st,action,text,msg)
                    self.save(uid,st); self.prompt(uid,st)
            except ValueError as e:
                s.say(uid,str(e))
            s.q('INSERT INTO processed VALUES(?)',(update['update_id'],))

    def advance(self, uid, st, action, text, msg):
        s=self.s; mode=st.get('mode','menu'); media=[]
        if msg.get('photo'): media=[{'type':'photo','file_id':msg['photo'][-1]['file_id']}]
        for kind in ['video','document','voice','video_note']:
            if msg.get(kind): media=[{'type':kind,'file_id':msg[kind]['file_id']}]
        content=text or msg.get('caption','')
        if len(content)>3000: raise ValueError('Maximum 3000 caractères par message.')
        if mode=='name':
            if len(text.split())<2 or len(text)>120: raise ValueError('Envoyez votre prénom et votre nom (120 caractères maximum).')
            s.q('UPDATE users SET name=? WHERE id=?',(text.strip(),uid)); st.clear(); st['mode']='menu'
            s.event(uid,None,'registered'); return
        if mode=='menu':
            if action=='new':
                cur=s.q("INSERT INTO contacts(user_id,started,status,data) VALUES(?,?,'active','{}')",(uid,time.time()))
                st.update(mode='flow',stage='interest',cid=cur.lastrowid,data={})
                s.event(uid,st['cid'],'approached')
            elif action=='topup': st.update(mode='topup',step='phone',data={})
            elif action=='report' and self.manager(uid): s.say(uid,self.report())
            elif action=='export' and self.manager(uid): self.export(uid)
            else: raise ValueError('Choisissez une action dans le menu.')
            return
        if mode=='topup':
            if action=='cancel': st.clear(); st['mode']='menu'; return
            step=st['step']
            if step=='phone': st['data']['phone']=phone(text); st['step']='reason'
            elif step=='reason':
                if action not in ['promo','compensation']: raise ValueError('Choisissez un motif.')
                st['data']['reason']=action; st['step']='amount'
            elif step=='amount': st['data']['amount_dzd']=amount(text); st['step']='comment'
            elif step=='comment':
                if not text and action!='skip': raise ValueError('Envoyez un commentaire ou passez.')
                st['data']['comment']=text
                cur=s.q("INSERT INTO topups(user_id,at,status,data) VALUES(?,?,'pending',?)",(uid,time.time(),json.dumps(st['data'])))
                rid=cur.lastrowid
                s.event(uid,None,'manual_topup',dict(st['data'],request_id=rid))
                self.structured(uid,'RECHARGE MANUELLE',rid,st['data'],f'Validation manuelle uniquement. /resolve {rid} après traitement.')
                s.say(uid,f'Demande #{rid} enregistrée. Elle ne crédite pas automatiquement le portefeuille.')
                st.clear(); st['mode']='menu'
            return
        if mode=='abort':
            if action=='resume': st['mode']='flow'; return
            if not text.strip(): raise ValueError('Le motif est obligatoire.')
            st['data']['abort_reason']=text; st['outcome']='aborted'; st['mode']='flow'; st['stage']='comment'
            s.event(uid,st['cid'],'aborted',{'reason':text}); self.contact_save(uid,st); return
        if mode=='incident':
            if action=='cancelincident': st.pop('draft'); st['mode']='flow'; return
            if action=='submit':
                draft=st['draft']
                if not draft['text']: raise ValueError('Ajoutez une courte description avant l’envoi.')
                data={'stage':st['stage'],'phone':st['data'].get('phone'),'description':'\n'.join(draft['text']), 'media':draft['media']}
                cur=s.q('INSERT INTO incidents(contact_id,user_id,at,data) VALUES(?,?,?,?)',(st['cid'],uid,time.time(),json.dumps(data)))
                rid=cur.lastrowid
                s.event(uid,st['cid'],'incident',dict(data,incident_id=rid))
                self.structured(uid,'INCIDENT',rid,data,f'Contact #{st["cid"]}')
                for item in draft['media']:
                    kind=item['type']; method={'photo':'sendPhoto','video':'sendVideo','document':'sendDocument','voice':'sendVoice','video_note':'sendVideoNote'}[kind]
                    args={'chat_id':self.group,kind:item['file_id']}
                    if kind!='video_note': args['caption']=f'Incident #{rid} • contact #{st["cid"]}'
                    s.queue(method,**args)
                st.pop('draft'); st['mode']='flow'; return
            if not content and not media: raise ValueError('Envoyez un texte, une photo ou une vidéo.')
            if len(st['draft']['media'])+len(media)>20 or sum(map(len,st['draft']['text']))+len(content)>12000:
                raise ValueError('Limite du dossier : 20 pièces et 12 000 caractères. Envoyez ce dossier avant d’en ouvrir un autre.')
            if content: st['draft']['text'].append(content)
            st['draft']['media']+=media
            s.say(uid,f'Pièces reçues : {len(st["draft"]["media"])}. Description : {len(st["draft"]["text"])} message(s).'); return
        stage=st['stage']
        if action=='abort': st['mode']='abort'; return
        if action in ['incident','failure'] or media or (content and stage in STAGES and stage!='phone'):
            st['mode']='incident'; st['draft']={'text':[content] if content else [],'media':media}
            if action=='failure': st['draft']['text']=['Échec de recharge du portefeuille JET.']; s.event(uid,st['cid'],'payment_failed')
            return
        if stage=='interest':
            if action=='yes': st['stage']='phone'; s.event(uid,st['cid'],'interested')
            elif action=='no': st['stage']='reason'
            else: raise ValueError('Choisissez Oui ou Non.')
        elif stage=='phone':
            st['data']['phone']=phone(text); st['stage']='jet'; s.event(uid,st['cid'],'phone_saved')
        elif stage in MILESTONES:
            if action!='next': raise ValueError('Confirmez l’étape avec le bouton.')
            s.event(uid,st['cid'],MILESTONES[stage]); st['data'][MILESTONES[stage]]=stamp(time.time())
            if stage=='ride': st['stage']='comment'; st['outcome']='completed'
            else: st['stage']=NEXT[stage]
        elif stage=='reason':
            if not action.startswith('reason:'): raise ValueError('Choisissez une raison.')
            i=int(action.split(':')[1]); reason=REASONS[i]; st['data']['reason']=reason
            if reason=='Autre': st['stage']='other_reason'
            else:
                s.event(uid,st['cid'],'refused',{'reason':reason})
                st['stage']='payments' if i in [1,6] else 'price' if i==2 else 'followup'
        elif stage=='other_reason':
            if not text.strip(): raise ValueError('Décrivez la raison.')
            st['data']['reason_other']=text; s.event(uid,st['cid'],'refused',{'reason':'Autre','detail':text}); st['stage']='followup'
        elif stage=='payments':
            selected=st['data'].setdefault('payments',[])
            if action=='paydone':
                if not selected: raise ValueError('Choisissez au moins une réponse, y compris Aucun si nécessaire.')
                st['stage']='followup'
            elif action.startswith('pay:'):
                option=PAYMENTS[int(action.split(':')[1])]
                if option=='Autre': st['stage']='payment_other'
                elif option in selected: selected.remove(option)
                elif option=='Aucun': selected[:]=['Aucun']
                else:
                    if 'Aucun' in selected: selected.remove('Aucun')
                    selected.append(option)
            else: raise ValueError('Choisissez les moyens de paiement.')
        elif stage=='payment_other':
            if not text.strip(): raise ValueError('Indiquez le moyen de paiement.')
            selected=st['data'].setdefault('payments',[])
            if 'Aucun' in selected: selected.remove('Aucun')
            selected.append('Autre : '+text); st['stage']='payments'
        elif stage=='price':
            st['data']['acceptable_price_dzd']=None if action=='noanswer' else amount(text); st['stage']='followup'
        elif stage=='followup':
            if not text and action!='noanswer': raise ValueError('Notez la réponse ou choisissez Pas de réponse.')
            st['data']['change_decision']=text or None; st['stage']='comment'
        elif stage=='comment':
            if not text and action!='skip': raise ValueError('Envoyez un commentaire ou passez.')
            st['data']['comment']=text; self.close(uid,st); return
        self.contact_save(uid,st)

    def structured(self, uid, title, rid, data, extra):
        name=self.s.q('SELECT name FROM users WHERE id=?',(uid,)).fetchone()[0]
        details='\n'.join(f'{k}: {v}' for k,v in data.items() if k!='media')
        self.s.say(self.group,f'{title} #{rid}\n{stamp(time.time())} (Alger)\nPromoteur : {name} • {uid}\n{extra}\n{details}')

    def scheduled(self, now=None):
        now=now or time.time(); hour=int(now//3600)
        with self.s.db:
            row=self.s.q("SELECT value FROM meta WHERE key='hour'").fetchone()
            last=int(row[0]) if row else hour
            for h in range(last,hour):
                self.s.say(self.group,self.report(h*3600,(h+1)*3600))
                local=datetime.fromtimestamp((h+1)*3600,TZ)
                if local.hour==0:
                    self.s.say(self.group,'RÉCAPITULATIF QUOTIDIEN\n'+self.report((h+1)*3600-86400,(h+1)*3600))
            self.s.q("INSERT OR REPLACE INTO meta VALUES('hour',?)",(str(hour),))


class Telegram:
    def __init__(self, token): self.base='https://api.telegram.org/bot'+token+'/'
    def call(self, method, **payload):
        data=json.dumps(payload).encode()
        req=urllib.request.Request(self.base+method,data=data,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=40) as r: result=json.load(r)
        if not result.get('ok'): raise RuntimeError('Telegram API request failed')
        return result['result']

    def csv(self, chat_id, content):
        boundary='jet_csv_boundary_8ad795'
        data=(f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="jet_events.csv"\r\n'
              'Content-Type: text/csv; charset=utf-8\r\n\r\n\ufeff'+content+f'\r\n--{boundary}--\r\n').encode()
        req=urllib.request.Request(self.base+'sendDocument',data=data,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        with urllib.request.urlopen(req,timeout=40) as r:
            if not json.load(r).get('ok'): raise RuntimeError('CSV upload failed')


def deliver(store, tg):
    # Per destination ordering keeps incident summaries before their attachments.
    blocked=set()
    for row in store.q('SELECT * FROM outbox ORDER BY id LIMIT 100').fetchall():
        payload=json.loads(row['payload']); dest=payload.get('chat_id',row['id'])
        if dest in blocked: continue
        if row['due']>time.time(): blocked.add(dest); continue
        try:
            if row['method']=='_csv': tg.csv(**payload)
            elif row['method']=='_invite':
                link=tg.call('createChatInviteLink',chat_id=payload['chat_id'],creates_join_request=True,expire_date=int(time.time())+86400)
                with store.db: store.say(payload['owner'],f'Invitation pour {payload["target"]} (valable 24 h, admission réservée aux responsables autorisés) : '+link['invite_link'])
            else: tg.call(row['method'],**payload)
            with store.db: store.q('DELETE FROM outbox WHERE id=?',(row['id'],))
        except Exception as exc:
            logging.warning('Delivery failed: outbox=%s error_type=%s',row['id'],type(exc).__name__)
            with store.db:
                store.q('UPDATE outbox SET tries=tries+1,due=? WHERE id=?',(time.time()+min(3600,2**min(row['tries']+1,11)),row['id']))
            blocked.add(dest)


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    token=os.environ['BOT_TOKEN']; owner=int(os.environ['OWNER_ID']); group=int(os.environ['MANAGER_CHAT_ID'])
    path=os.getenv('DB_PATH','data/jet.sqlite3'); os.makedirs(os.path.dirname(path) or '.',exist_ok=True)
    # One process per database: avoid duplicate polling and outbox sends.
    import fcntl
    lock=open(path+'.lock','w'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store=Store(path); bot=Bot(store,owner,group); tg=Telegram(token)
    tg.call('getMe')
    if tg.call('getWebhookInfo').get('url'): raise RuntimeError('Remove existing webhook before polling.')
    while True:
        try:
            bot.scheduled(); deliver(store,tg)
            row=store.q("SELECT value FROM meta WHERE key='offset'").fetchone(); offset=int(row[0]) if row else 0
            updates=tg.call('getUpdates',offset=offset,timeout=20,allowed_updates=['message','callback_query','chat_join_request'])
            for update in updates:
                bot.handle(update)
                with store.db: store.q("INSERT OR REPLACE INTO meta VALUES('offset',?)",(str(update['update_id']+1),))
            deliver(store,tg)
        except Exception as exc:
            # Never log Telegram request URLs (they contain the bot token) or personal data.
            logging.error('Worker error: %s',type(exc).__name__); time.sleep(3)

if __name__=='__main__': main()
