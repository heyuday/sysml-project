"""Live subset fixed in advance: stable R0/R1-all/R2-four, shift E1/E2/E6/E8."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
import httpx
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sysml.common import DSN,records,quantiles
from sysml import db
from sysml.policies import Config

def child(directory,port):
    import uvicorn
    from sysml.live import app
    srv=uvicorn.Server(uvicorn.Config(app(Config(**json.loads((directory/'cfg.json').read_bytes())),str(directory/'broker.jsonl')),
                                      host='127.0.0.1',port=port,log_level='warning'))
    def stop():
        sys.stdin.buffer.read()
        srv.should_exit=True
    threading.Thread(target=stop,daemon=True).start()
    srv.run()

def main(root):
    from sysml.evaluate import live_replay
    models=json.loads((root/'pilot-summary.json').read_bytes())
    cap=next(r['capacity_bytes'] for r in models if r['scenario']=='shift_ro' and r['capacity_fraction']==.25)
    summary=[]
    # Fixed interleaved order, fresh broker/namespace and clone per trial.
    for tag in ['E6','R2','E1','R0','E8','R1','E2']:
        evict=tag.startswith('E')
        name='shift_ro' if evict else 'stable'
        d=root/'live-hires'/tag
        d.mkdir(parents=True,exist_ok=False)
        cfg={'tau':2,'horizon':3,'invalid':'I1'}
        cfg.update({'evict':tag,'capacity':cap} if evict else {'refresh':tag,'refresh_budget':None if tag=='R1' else 4})
        (d/'cfg.json').write_text(json.dumps(cfg))
        ops=list(records(root/name/'input.jsonl'))
        target='sysml_repilot_live_'+tag.lower()
        admin_dsn=make_conninfo(DSN,dbname='postgres')
        with db.connect(admin_dsn) as c:
            c.execute(sql.SQL('CREATE DATABASE {} TEMPLATE chbenchmark').format(sql.Identifier(target)))
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            url='http://127.0.0.1:'+str(port)
            with (d/'server.log').open('w') as log:
                proc=subprocess.Popen([sys.executable,'-B',__file__,'serve',str(d),str(port)],env=dict(os.environ,SYSML_DSN=make_conninfo(DSN,dbname=target)),
                                      stdin=subprocess.PIPE,stdout=log,stderr=log)
                try:
                    with httpx.Client(base_url=url,trust_env=False,timeout=10) as h:
                        for _ in range(150):
                            if proc.poll() is not None: raise RuntimeError('server exited; see '+str(d))
                            try:
                                if h.get('/stats').status_code==200:break
                            except httpx.ConnectError:pass
                            time.sleep(.1)
                        else:raise RuntimeError('server startup deadline')
                    live_replay(ops,url,d/'requests.jsonl',8)
                finally:
                    proc.stdin.close()
                    try:proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait()
            rows=list(records(d/'requests.jsonl'))
            stat=json.loads((d/'requests.jsonl.stats.json').read_bytes())
            assert len(rows)==len(ops)==len(set(r['id'] for r in rows)) and not stat['cdc_error'] and not stat.get('refresh_errors')
            rd=[r for r in rows if r['type']=='read']
            if evict:
                versions={r['id']:r['digest'] for r in records(root/name/'calibrated.jsonl')}
                assert all(r['digest']==versions[r['id']] for r in rd)
                assert stat['peak_value_bytes']<=cap
            # Verify every mutable read against its own database version, not the input order.
            else:
                reference=list(records(root/name/'calibrated.jsonl'))
                # Check only when committed write order matches calibration order. Snapshot 0
                # and snapshots after each profiled write then have independent known digests.
                livewrites=sorted((r for r in rows if r['type']=='write'),key=lambda r:r['version'])
                refwrites=[r for r in reference if r['kind']=='write']
                assert {r['id'] for r in livewrites}=={r['id'] for r in refwrites}
                same_state={0}
                observed=set()
                calibrated=set()
                for w1,w2 in zip(livewrites,refwrites):
                    observed.add(w1['id']);calibrated.add(w2['id'])
                    if observed==calibrated and w1['version']==w2['version']:same_state.add(w1['version'])
                expected={(r['raw_id'],r['version']):r['digest'] for r in reference if r['kind']=='read'}
                for w in refwrites:
                    for k,v in w['post'].items():expected[k,w['version']]=v['digest']
                raw={r['id']:r['raw_id'] for r in reference if r['kind']=='read'}
                for r in rd:
                    k=(raw[r['id']],r['version'])
                    if r['version'] in same_state and k in expected:assert r['digest']==expected[k]
                compared=sum(r['version'] in same_state and (raw[r['id']],r['version']) in expected for r in rd)
            summary.append(dict(policy=tag,scenario=name,reads=len(rd),hits=sum(r['hit'] for r in rd),
                latency_p95_ms=quantiles([r['client_wait_ms'] for r in rd])['p95'],
                total_read_latency_ms=sum(r['client_wait_ms'] for r in rd),schedule_p95_ms=quantiles([r['schedule_to_completion_ms'] for r in rd])['p95'],
                demand_db_ms=sum(r['db_ms'] for r in rd),refresh_db_ms=stat.get('refresh_db_ms',0),
                db_ms=sum(r['db_ms'] for r in rd)+stat.get('refresh_db_ms',0),refreshes=stat.get('refresh_runs',0),
                evictions=stat.get('evictions',0),digests_checked=len(rd) if evict else compared))
            (root/'live-hires-summary.json').write_text(json.dumps(summary,indent=2))
            print('live complete',tag,summary[-1],flush=True)
        finally:
            with db.connect(admin_dsn) as c:
                c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))

if __name__=='__main__':
    if sys.argv[1]=='serve':child(Path(sys.argv[2]),int(sys.argv[3]))
    else:main(Path(sys.argv[1]))
