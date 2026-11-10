"""Small, local SQLite telemetry store and loopback dashboard server."""
from __future__ import annotations

import http.server
import json
import math
import os
import re
import sqlite3
import statistics
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {'retention_days': 14, 'slow_request_seconds': 180, 'stuck_request_seconds': 360,
            'max_node_visits': 3, 'process_rss_limit_mib': 512, 'system_memory_percent_limit': 90}
RUN_RE = re.compile(r'^[0-9a-f]{32}$')
ID_RE = re.compile(r'^[0-9a-f]{16}$')


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class TelemetryStore:
    def __init__(self, project_dir, settings=None, clock=time.time):
        self.project_dir = Path(project_dir).resolve()
        self.runs_dir = (self.project_dir / 'runs').resolve()
        if not self.runs_dir.is_relative_to(self.project_dir):
            raise ValueError('runs path escapes project')
        self.telemetry_dir = self.runs_dir / 'telemetry'
        self.spool_dir = self.telemetry_dir / 'spool'
        for directory, parent in ((self.telemetry_dir, self.runs_dir), (self.spool_dir, self.telemetry_dir)):
            if directory.exists() and (directory.is_symlink() or not directory.resolve().is_relative_to(parent.resolve())):
                raise ValueError('telemetry path escapes runs directory')
        self.telemetry_dir.mkdir(parents=True, exist_ok=True)
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.telemetry_dir / 'observability.sqlite3'
        if self.db_path.is_symlink() or not self.db_path.resolve().is_relative_to(self.telemetry_dir.resolve()):
            raise ValueError('database path escapes telemetry directory')
        self.settings = dict(DEFAULTS, **(settings or {}))
        self.clock = clock
        self._init_db()

    @contextmanager
    def _connect(self):
        if self.telemetry_dir.is_symlink() or not self.telemetry_dir.resolve().is_relative_to(self.runs_dir.resolve()):
            raise ValueError('telemetry path escaped runs directory')
        if self.spool_dir.is_symlink() or not self.spool_dir.resolve().is_relative_to(self.telemetry_dir.resolve()):
            raise ValueError('spool path escaped telemetry directory')
        if self.db_path.is_symlink() or not self.db_path.resolve().is_relative_to(self.telemetry_dir.resolve()):
            raise ValueError('database path escaped telemetry directory')
        con = sqlite3.connect(str(self.db_path), timeout=5)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA busy_timeout=5000')
        con.execute('PRAGMA foreign_keys=ON')
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _init_db(self):
        with self._connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS traces(run_id TEXT PRIMARY KEY, created_ns INTEGER NOT NULL, updated_ns INTEGER NOT NULL, status TEXT NOT NULL, root_span_id TEXT, question TEXT, lecture TEXT, session_id TEXT, attributes TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS spans(span_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, trace_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL, kind TEXT NOT NULL, start_ns INTEGER NOT NULL, end_ns INTEGER, status TEXT, attributes TEXT NOT NULL DEFAULT '{}', exported INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(run_id) REFERENCES traces(run_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT, ts_ns INTEGER NOT NULL, name TEXT NOT NULL, level TEXT NOT NULL, attributes TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES traces(run_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, message TEXT NOT NULL, run_id TEXT, severity TEXT NOT NULL, created_at REAL NOT NULL, fingerprint TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, attributes TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ingested(source_key TEXT PRIMARY KEY, event TEXT NOT NULL, ts_ns INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS spool_offsets(path TEXT PRIMARY KEY, offset INTEGER NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS spans_run ON spans(run_id,start_ns);
            CREATE INDEX IF NOT EXISTS logs_run ON logs(run_id,ts_ns);
            CREATE INDEX IF NOT EXISTS traces_created ON traces(created_ns);
            ''')

    def ingest(self, event, source_key: str) -> bool:
        if not isinstance(event, dict) or not isinstance(source_key, str) or len(source_key) > 512:
            return False
        try:
            if event.get('v') != 1 or event.get('event') not in ('trace_start','span_start','span_end','log','trace_end'): return False
            run = event.get('run_id'); trace = event.get('trace_id'); ts = event.get('ts_ns')
            if not isinstance(run,str) or not RUN_RE.fullmatch(run) or trace != run or type(ts) is not int or ts < 0: return False
            typ = event['event']; attrs = event.get('attributes', {})
            if not isinstance(attrs,dict) or len(_json(attrs)) > 65536: return False
            def ident(key, required=True):
                val=event.get(key)
                if required and (not isinstance(val,str) or not ID_RE.fullmatch(val)): raise ValueError
                if val is not None and (not isinstance(val,str) or not ID_RE.fullmatch(val)): raise ValueError
                return val
            with self._connect() as c:
                if c.execute('SELECT 1 FROM ingested WHERE source_key=?',(source_key,)).fetchone(): return False
                if typ == 'trace_start':
                    sid=ident('span_id'); name=event.get('name'); kind=event.get('kind')
                    if name!='lecture_request' or kind!='CHAIN': return False
                    c.execute('INSERT OR IGNORE INTO traces(run_id,created_ns,updated_ns,status,root_span_id,question,lecture,session_id,attributes) VALUES(?,?,?,?,?,?,?,?,?)',
                              (run,ts,ts,'active',sid,str(attrs.get('question',''))[:4000],str(attrs.get('lecture',''))[:500],str(attrs.get('session_id',''))[:256],_json(attrs)))
                    if c.execute('SELECT changes()').fetchone()[0]:
                        c.execute('INSERT INTO spans(span_id,run_id,trace_id,parent_id,name,kind,start_ns,attributes) VALUES(?,?,?,?,?,?,?,?)',(sid,run,trace,None,name,kind,ts,_json(attrs)))
                else:
                    if not c.execute('SELECT 1 FROM traces WHERE run_id=?',(run,)).fetchone(): return False
                    if typ == 'span_start':
                        sid=ident('span_id'); parent=ident('parent_id',False); name=event.get('name'); kind=event.get('kind')
                        if not isinstance(name,str) or len(name)>256 or kind not in ('CHAIN','LLM','TOOL','RETRIEVER','ROUTER'): return False
                        if parent and not c.execute('SELECT 1 FROM spans WHERE span_id=? AND run_id=?',(parent,run)).fetchone(): return False
                        c.execute('INSERT OR IGNORE INTO spans(span_id,run_id,trace_id,parent_id,name,kind,start_ns,attributes) VALUES(?,?,?,?,?,?,?,?)',(sid,run,trace,parent,name,kind,ts,_json(attrs)))
                    elif typ == 'span_end':
                        sid=ident('span_id'); status=event.get('status')
                        if status not in ('OK','ERROR'): return False
                        row=c.execute('SELECT attributes FROM spans WHERE span_id=? AND run_id=?',(sid,run)).fetchone()
                        if not row: return False
                        merged=json.loads(row['attributes']); merged.update(attrs)
                        c.execute('UPDATE spans SET end_ns=?,status=?,attributes=? WHERE span_id=?',(ts,status,_json(merged),sid))
                    elif typ == 'log':
                        sid=ident('span_id',False); name=event.get('name'); level=event.get('level')
                        if not isinstance(name,str) or len(name)>256 or not isinstance(level,str) or len(level)>32: return False
                        c.execute('INSERT INTO logs(run_id,trace_id,span_id,ts_ns,name,level,attributes) VALUES(?,?,?,?,?,?,?)',(run,trace,sid,ts,name,level,_json(attrs)))
                    elif typ == 'trace_end':
                        sid=ident('span_id'); status=event.get('status')
                        if status not in ('answered','insufficient_information','technical_error','interrupted'): return False
                        tr=c.execute('SELECT root_span_id,attributes FROM traces WHERE run_id=?',(run,)).fetchone()
                        if not tr: return False
                        ta=json.loads(tr['attributes']); ta.update(attrs)
                        c.execute('UPDATE traces SET status=?,updated_ns=?,attributes=? WHERE run_id=?',(status,ts,_json(ta),run))
                        if sid==tr['root_span_id']:
                            sp=c.execute('SELECT attributes FROM spans WHERE span_id=?',(sid,)).fetchone()
                            if sp:
                                sa=json.loads(sp['attributes']); sa.update(attrs)
                                c.execute('UPDATE spans SET end_ns=?,status=?,attributes=? WHERE span_id=?',(ts,'ERROR' if status in ('technical_error','interrupted') else 'OK',_json(sa),sid))
                c.execute('INSERT INTO ingested(source_key,event,ts_ns) VALUES(?,?,?)',(source_key,_json(event),ts))
            return True
        except (ValueError, TypeError, OverflowError, sqlite3.Error):
            return False

    def scan_spool(self) -> int:
        count=0
        try: files=sorted(self.spool_dir.glob('*.jsonl'))[:1000]
        except OSError: return 0
        for path in files:
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(self.spool_dir.resolve()) or path.stat().st_size>16*1024*1024: continue
                with path.open('rb') as f:
                    with self._connect() as c:
                        row=c.execute('SELECT offset FROM spool_offsets WHERE path=?',(path.name,)).fetchone(); offset=row['offset'] if row else 0
                    f.seek(offset); pos=offset
                    while True:
                        line=f.readline(512*1024+1)
                        if not line or not line.endswith(b'\n'): break
                        end=pos+len(line); key=f'{path.name}:{pos}'
                        if len(line)>512*1024:
                            self.alert('telemetry_parse_error','Строка телеметрии превышает лимит',path.stem if RUN_RE.fullmatch(path.stem) else None)
                        else:
                            try:
                                obj=json.loads(line.decode('utf-8'))
                                if self.ingest(obj,key): count+=1
                                elif not self._source_exists(key): self.alert('telemetry_parse_error','Некорректное событие телеметрии',path.stem if RUN_RE.fullmatch(path.stem) else None)
                            except (UnicodeDecodeError,json.JSONDecodeError): self.alert('telemetry_parse_error','Некорректный JSON в телеметрии',path.stem if RUN_RE.fullmatch(path.stem) else None)
                        with self._connect() as c: c.execute('INSERT INTO spool_offsets(path,offset,updated_at) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET offset=excluded.offset,updated_at=excluded.updated_at',(path.name,end,self.clock()))
                        pos=end
            except OSError: continue
        return count

    def _source_exists(self,key):
        with self._connect() as c: return c.execute('SELECT 1 FROM ingested WHERE source_key=?',(key,)).fetchone() is not None

    def alert(self, code, message, run_id=None, severity='WARNING', dedup_key=None) -> bool:
        if not isinstance(code,str) or not code or len(code)>100: return False
        if dedup_key is not None and (not isinstance(dedup_key,str) or not dedup_key or len(dedup_key)>256): return False
        fingerprint=f'custom:{code}:{dedup_key}' if dedup_key else (f'{run_id}:{code}' if run_id else f'global:{code}:{int(self.clock()//3600)}')
        try:
            with self._connect() as c:
                cur=c.execute('INSERT OR IGNORE INTO alerts(code,message,run_id,severity,created_at,fingerprint) VALUES(?,?,?,?,?,?)',(code,str(message)[:2000],run_id,severity,self.clock(),fingerprint))
                made=cur.rowcount>0
            if made: print(f'[telemetry:{severity}] {code}: {message}')
            return made
        except sqlite3.Error: return False

    def alert_exists(self, code, dedup_key) -> bool:
        """Confirm a previously delivered transition without emitting a duplicate."""
        with self._connect() as c:
            return c.execute('SELECT 1 FROM alerts WHERE fingerprint=?',
                             (f'custom:{code}:{dedup_key}',)).fetchone() is not None

    def sample_resources(self, data: dict) -> None:
        if not isinstance(data,dict): return
        clean={k:v for k,v in data.items() if isinstance(k,str) and isinstance(v,(str,int,float,bool,type(None))) and (not isinstance(v,float) or math.isfinite(v))}
        with self._connect() as c: c.execute('INSERT INTO samples(ts,attributes) VALUES(?,?)',(self.clock(),_json(clean)))
        rss=clean.get('memory.peak_rss_mib',clean.get('monitor_rss_mib',clean.get('peak_rss_mib')))
        if isinstance(rss,(int,float)) and rss>self.settings['process_rss_limit_mib']: self.alert('process_rss_high',f'Память процесса: {rss:.1f} МиБ')
        mem=clean.get('system_memory_percent')
        if isinstance(mem,(int,float)) and mem>self.settings['system_memory_percent_limit']: self._global_memory_alert(mem)

    def _global_memory_alert(self, percent):
        now=self.clock(); bucket=int(now//3600)
        try:
            with self._connect() as c:
                prior=c.execute("SELECT created_at FROM alerts WHERE code='system_memory_high' AND run_id IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
                if prior and now-prior['created_at']<3600: return False
                cur=c.execute('INSERT OR IGNORE INTO alerts(code,message,run_id,severity,created_at,fingerprint) VALUES(?,?,?,?,?,?)',('system_memory_high',f'Использование памяти системы: {percent:.1f}%',None,'WARNING',now,f'global:system_memory_high:{bucket}'))
            if cur.rowcount: print(f'[telemetry:WARNING] system_memory_high: {percent:.1f}%')
            return cur.rowcount>0
        except sqlite3.Error: return False

    def check_alerts(self):
        with self._connect() as c:
            rows=c.execute('SELECT run_id,status,created_ns,attributes FROM traces').fetchall()
            for r in rows:
                age=self.clock()-r['created_ns']/1e9
                if r['status']=='active' and age>self.settings['stuck_request_seconds']: self.alert('stuck_request','Запрос выполняется слишком долго',r['run_id'])
                if r['status'] in ('technical_error','interrupted'):
                    code='technical_failure' if r['status']=='technical_error' else 'interrupted_request'
                    msg='Техническая ошибка запроса' if r['status']=='technical_error' else 'Запрос прерван'
                    self.alert(code,msg,r['run_id'],'ERROR')
                rss=json.loads(r['attributes']).get('memory.peak_rss_mib')
                outcome = json.loads(r['attributes']).get('outcome', {})
                routing = outcome.get('routing', {}) if isinstance(outcome, dict) else {}
                if isinstance(routing, dict) and routing.get('warning'):
                    self.alert('routing_fallback', str(routing['warning']), r['run_id'])
                if isinstance(rss,(int,float)) and rss>self.settings['process_rss_limit_mib']: self.alert('process_rss_high',f'Память процесса: {rss:.1f} МиБ',r['run_id'])
                if r['status']!='active' and age>self.settings['slow_request_seconds']:
                    ends=c.execute('SELECT max(end_ns) FROM spans WHERE run_id=?',(r['run_id'],)).fetchone()[0]
                    if ends and (ends-r['created_ns'])/1e9>self.settings['slow_request_seconds']: self.alert('slow_request','Медленный запрос',r['run_id'])
                visits={}
                for sp in c.execute("SELECT attributes FROM spans WHERE run_id=? AND kind='CHAIN'",(r['run_id'],)).fetchall():
                    node=json.loads(sp['attributes']).get('app.node')
                    if isinstance(node,str): visits[node]=visits.get(node,0)+1
                if any(v>self.settings['max_node_visits'] for v in visits.values()): self.alert('repeated_node','Узел графа запускался слишком часто',r['run_id'])
        with self._connect() as c: sample=c.execute('SELECT attributes FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
        if sample:
            mem=json.loads(sample['attributes']).get('system_memory_percent')
            if isinstance(mem,(int,float)) and mem>self.settings['system_memory_percent_limit']: self._global_memory_alert(mem)

    @staticmethod
    def _utc(ns):
        return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(ns/1e9))

    def summary(self) -> dict:
        with self._connect() as c:
            rows=c.execute('SELECT * FROM traces').fetchall(); samples=c.execute('SELECT attributes FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
            durations=[(e-s)/1e9 for (s,e) in c.execute('SELECT t.created_ns,max(sp.end_ns) FROM traces t JOIN spans sp USING(run_id) WHERE t.status<>"active" GROUP BY t.run_id').fetchall() if e and e>=s]
            toks=c.execute("SELECT attributes FROM spans WHERE kind='LLM'").fetchall()
        total=len(rows); completed=sum(r['status']!='active' for r in rows); answered=sum(r['status']=='answered' for r in rows); refused=sum(r['status']=='insufficient_information' for r in rows); errors=sum(r['status']=='technical_error' for r in rows)
        pt=ot=measure=0
        for row in toks:
            a=json.loads(row['attributes']); p=a.get('llm.token_count.prompt',a.get('prompt_tokens',a.get('llm.usage.prompt_tokens'))); o=a.get('llm.token_count.completion',a.get('output_tokens',a.get('llm.usage.completion_tokens')))
            if isinstance(p,int) or isinstance(o,int): pt+=p if isinstance(p,int) else 0; ot+=o if isinstance(o,int) else 0; measure+=1
        ds=sorted(durations); p95=ds[min(len(ds)-1,math.ceil(.95*len(ds))-1)] if ds else None
        return {'total_requests':total,'completed_requests':completed,'answered':answered,'insufficient_information':refused,'technical_error':errors,'active':total-completed,'error_rate':errors/completed if completed else 0,'refusal_rate':refused/completed if completed else 0,'mean_seconds':statistics.mean(ds) if ds else None,'p95_seconds':p95,'total_prompt_tokens':pt if measure else None,'total_output_tokens':ot if measure else None,'token_measurements':measure,'latest_resources':json.loads(samples['attributes']) if samples else None}

    def traces(self, limit=100) -> list:
        limit=max(1,min(int(limit),500))
        with self._connect() as c: rows=c.execute('SELECT * FROM traces ORDER BY created_ns DESC LIMIT ?',(limit,)).fetchall()
        out=[]
        for r in rows:
            with self._connect() as c: end=c.execute('SELECT max(end_ns) FROM spans WHERE run_id=?',(r['run_id'],)).fetchone()[0]
            a=json.loads(r['attributes']); duration=(self.clock()*1e9-r['created_ns'])/1e9 if r['status']=='active' else ((end-r['created_ns'])/1e9 if end else None); out.append({'run_id':r['run_id'],'created_at':self._utc(r['created_ns']),'created_ns':r['created_ns'],'status':r['status'],'duration_seconds':duration,'question':r['question'],'lecture':r['lecture'],'model':a.get('app.writer_model') or a.get('model') or a.get('llm.model_name'),'route':a.get('route') or a.get('app.route'),'peak_rss_mib':a.get('memory.peak_rss_mib')})
        return out

    def detail(self, run_id):
        if not isinstance(run_id,str) or not RUN_RE.fullmatch(run_id): return None
        with self._connect() as c:
            t=c.execute('SELECT * FROM traces WHERE run_id=?',(run_id,)).fetchone()
            if not t:return None
            spans=c.execute('SELECT * FROM spans WHERE run_id=? ORDER BY start_ns',(run_id,)).fetchall(); logs=c.execute('SELECT * FROM logs WHERE run_id=? ORDER BY ts_ns',(run_id,)).fetchall(); alerts=c.execute('SELECT * FROM alerts WHERE run_id=? ORDER BY created_at DESC',(run_id,)).fetchall()
        trace={'run_id':run_id,'created_ns':t['created_ns'],'created_at':self._utc(t['created_ns']),'status':t['status'],'question':t['question'],'lecture':t['lecture'],'session_id':t['session_id'],'attributes':json.loads(t['attributes'])}
        return {'trace':trace,'spans':[{'span_id':s['span_id'],'parent_id':s['parent_id'],'name':s['name'],'kind':s['kind'],'start_ns':s['start_ns'],'end_ns':s['end_ns'],'status':s['status'],'attributes':json.loads(s['attributes'])} for s in spans],'logs':[{'span_id':l['span_id'],'ts_ns':l['ts_ns'],'name':l['name'],'level':l['level'],'attributes':json.loads(l['attributes'])} for l in logs],'alerts':[dict(a) for a in alerts]}

    def alerts(self, limit=100):
        limit=max(1,min(int(limit),500))
        with self._connect() as c: rows=c.execute('SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?',(limit,)).fetchall()
        return [dict(r) for r in rows]

    def pending_spans(self, limit=100):
        with self._connect() as c: rows=c.execute('SELECT * FROM spans WHERE end_ns IS NOT NULL AND exported=0 ORDER BY end_ns LIMIT ?',(max(1,min(int(limit),1000)),)).fetchall()
        return [{'run_id':r['run_id'],'trace_id':r['trace_id'],'span_id':r['span_id'],'parent_id':r['parent_id'],'name':r['name'],'kind':r['kind'],'start_ns':r['start_ns'],'end_ns':r['end_ns'],'status':r['status'],'attributes':json.loads(r['attributes'])} for r in rows]

    def mark_exported(self, span_ids: list[str]) -> None:
        with self._connect() as c: c.executemany('UPDATE spans SET exported=1 WHERE span_id=?',[(x,) for x in span_ids if isinstance(x,str)])

    def prune(self):
        cutoff=self.clock()-self.settings['retention_days']*86400; n=0
        with self._connect() as c:
            old=[r[0] for r in c.execute("SELECT run_id FROM traces WHERE created_ns<? AND status<>'active'",(int(cutoff*1e9),))]
            c.executemany('DELETE FROM traces WHERE run_id=?',[(x,) for x in old]); n=len(old)
            c.execute('DELETE FROM alerts WHERE created_at<?',(cutoff,)); c.execute('DELETE FROM samples WHERE ts<?',(cutoff,))
            active_ids={r[0] for r in c.execute("SELECT run_id FROM traces WHERE status='active'")}
            stale=c.execute('SELECT source_key,event FROM ingested WHERE ts_ns<?',(int(cutoff*1e9),)).fetchall()
            stale_keys=[]
            for row in stale:
                try: event=json.loads(row['event'])
                except (ValueError,TypeError): event={}
                if event.get('run_id') not in active_ids: stale_keys.append((row['source_key'],))
            c.executemany('DELETE FROM ingested WHERE source_key=?',stale_keys)
            active_files={r[0]+'.jsonl' for r in c.execute("SELECT run_id FROM traces WHERE status='active'")}
            c.executemany('DELETE FROM spool_offsets WHERE path=?',[(r[0],) for r in c.execute('SELECT path FROM spool_offsets WHERE updated_at<?',(cutoff,)).fetchall() if r[0] not in active_files])
        for p in self.spool_dir.glob('*.jsonl'):
            try:
                if p.is_symlink() or not p.resolve().is_relative_to(self.spool_dir.resolve()) or p.stat().st_mtime>=cutoff: continue
                run=p.stem
                if run in old:
                    p.unlink()
                    with self._connect() as c:
                        c.execute('DELETE FROM spool_offsets WHERE path=?',(p.name,))
                        c.execute('DELETE FROM ingested WHERE source_key LIKE ?', (p.name+':%',))
            except OSError: pass
        return {'traces':n}


class ObservabilityServer(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1','localhost'): raise ValueError('dashboard must bind loopback')
        self.store=store
        super().__init__(address,self.Handler)
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def _send(self,code,body,ctype='application/json; charset=utf-8'):
            data=body.encode('utf-8') if isinstance(body,str) else _json(body).encode('utf-8')
            self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data))); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            p=urlsplit(self.path).path; store=self.server.store
            try:
                if p=='/':
                    html=(Path(__file__).resolve().parent.parent / 'web' / 'telemetry_dashboard.html').read_text(encoding='utf-8'); self._send(200,html,'text/html; charset=utf-8')
                elif p=='/health': self._send(200,{'ok':True,'service':'lecture-observability'})
                elif p=='/api/summary': self._send(200,store.summary())
                elif p=='/api/traces': self._send(200,store.traces())
                elif p=='/api/alerts': self._send(200,store.alerts())
                elif p.startswith('/api/trace/'):
                    detail=store.detail(p.rsplit('/',1)[-1]); self._send(200,detail) if detail else self._send(404,{'error':'trace_not_found'})
                else: self._send(404,{'error':'not_found'})
            except Exception: self._send(500,{'error':'internal_error'})

