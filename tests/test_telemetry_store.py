import json
import tempfile
import unittest
from pathlib import Path

from lecture_assistant.telemetry_store import TelemetryStore


class TelemetryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000.0
        self.store = TelemetryStore(self.tmp.name, clock=lambda: self.now)
        self.run = 'a' * 32

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, typ, ts=1_800_000_000_000_000_000, **kwargs):
        return {'v': 1, 'event': typ, 'run_id': self.run, 'trace_id': self.run, 'ts_ns': ts, **kwargs}

    def start(self):
        return self.event('trace_start', span_id=self.run[:16], name='lecture_request', kind='CHAIN', attributes={'question':'Q','lecture':'L','session_id':'S'})

    def test_partial_line_is_preserved_then_ingested_once(self):
        path = Path(self.tmp.name) / 'runs/telemetry/spool' / (self.run + '.jsonl')
        complete = self.start()
        extra = self.event('span_start', span_id='2'*16, parent_id=self.run[:16], name='node', kind='CHAIN', attributes={})
        path.write_bytes((json.dumps(complete)+'\n'+json.dumps(extra)[:-1]).encode())
        self.assertEqual(self.store.scan_spool(), 1)
        self.assertEqual(self.store.scan_spool(), 0)
        with path.open('ab') as f: f.write(b'}\n')
        self.assertEqual(self.store.scan_spool(), 1)
        self.assertEqual(self.store.scan_spool(), 0)
        self.assertEqual(self.store.summary()['total_requests'], 1)

    def test_parent_hierarchy_and_pending_export(self):
        self.assertTrue(self.store.ingest(self.start(), 'manual:0'))
        self.assertTrue(self.store.ingest(self.event('span_start', span_id='2'*16, parent_id=self.run[:16], name='retrieve', kind='RETRIEVER', attributes={'app.node':'retrieve'}), 'manual:1'))
        self.assertTrue(self.store.ingest(self.event('span_end', span_id='2'*16, status='OK', attributes={'documents':2}), 'manual:2'))
        pending=self.store.pending_spans()
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]['parent_id'],self.run[:16])
        self.assertEqual(pending[0]['attributes']['documents'],2)
        self.store.mark_exported([pending[0]['span_id']])
        self.assertEqual(self.store.pending_spans(),[])

    def test_refusal_is_distinct_from_technical_failure_and_alert_dedupes(self):
        for i,rid,status in [(1,'b'*32,'insufficient_information'),(2,'c'*32,'technical_error')]:
            self.run=rid
            self.assertTrue(self.store.ingest(self.start(),f'{i}:start'))
            self.assertTrue(self.store.ingest(self.event('trace_end',span_id=rid[:16],status=status,attributes={'app.status':status}),f'{i}:end'))
        metrics=self.store.summary()
        self.assertEqual(metrics['insufficient_information'],1)
        self.assertEqual(metrics['technical_error'],1)
        self.store.check_alerts(); self.store.check_alerts()
        self.assertEqual(len([a for a in self.store.alerts() if a['code']=='technical_failure']),1)

    def test_malformed_complete_event_creates_parse_alert(self):
        path=Path(self.tmp.name)/'runs/telemetry/spool'/(self.run+'.jsonl')
        path.write_bytes(b'{bad json}\n')
        self.assertEqual(self.store.scan_spool(),0)
        self.assertEqual(self.store.alerts()[0]['code'],'telemetry_parse_error')

    def test_retention_keeps_unrelated_files_and_active_spool(self):
        old=self.now-20*86400
        self.store.ingest(self.start(), 'retention:start')
        outside=Path(self.tmp.name)/'runs'/'keep.jsonl'; outside.write_text('keep')
        spool=Path(self.tmp.name)/'runs/telemetry/spool'/(self.run+'.jsonl')
        spool.write_text('{}\n')
        import os
        os.utime(spool,(old,old))
        with self.store._connect() as c:
            c.execute('UPDATE traces SET created_ns=? WHERE run_id=?',(int(old*1e9),self.run))
        # An active trace and its spool remain available for the next scan.
        self.store.prune()
        self.assertTrue(outside.exists())
        self.assertTrue(spool.exists())
        self.assertEqual(self.store.summary()['total_requests'],1)


if __name__ == '__main__':
    unittest.main()
