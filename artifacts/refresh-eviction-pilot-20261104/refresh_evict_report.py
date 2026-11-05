"""Write the human-readable pilot report from the retained numerical results."""
from collections import Counter
from html import escape
import json
from pathlib import Path
import sys

root=Path(sys.argv[1])
rows=json.loads((root/'pilot-summary.json').read_bytes())
live=json.loads((root/'live-hires-summary.json').read_bytes())
assert len(live)==7
R=['Lazy','All / tied priority','Lifetime frequency','Execution cost','Recency','Decayed frequency','Reuse before invalidation','Combined']
E=['FIFO','LRU','LFU','Decayed LFU','Cost','Frequency × cost','GDSF','Reuse-aware','Combined']
parts=['''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CH refresh and eviction pilots</title>
<style>body{font:16px/1.5 system-ui,sans-serif;color:#1c2938;max-width:1120px;margin:40px auto;padding:0 20px}h1,h2,h3{line-height:1.15}h1{font-size:34px}h2{margin:38px 0 10px}p{max-width:92ch}table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px;width:100%;margin:16px 0}th,td{text-align:right;padding:7px 9px;border-bottom:1px solid #d5dee6}th{background:#e9f1f7}th:first-child,td:first-child{text-align:left}summary{cursor:pointer;font-weight:600}.scroll{overflow-x:auto}.meta{color:#465c70}</style>
<h1>CH-benCHmark refresh and eviction pilots</h1><p class="meta">4 November 2026 · one warehouse · eight clients · seed 11 · 240 operations per trace</p>''']
def p(text):parts.append('<p>'+escape(text)+'</p>')
def h(text):parts.append('<h2>'+escape(text)+'</h2>')
def table(cols,rr):
    def cell(kind,v):return f'<{kind}>'+escape(str(v))+f'</{kind}>'
    parts.append('<div class="scroll"><table><thead><tr>'+''.join(cell('th',c) for c in cols)+'</tr></thead><tbody>')
    for r in rr:parts.append('<tr>'+''.join(cell('td',c) for c in r)+'</tr>')
    parts.append('</tbody></table></div>')
def name(code):return code+' · '+(R if code[0]=='R' else E)[int(code[1:])]
p('90 refresh configurations and 280 eviction configurations used PostgreSQL-calibrated discrete-event replay. Seven representative configurations were subsequently timed through the actual PostgreSQL/Redis broker. Treat the live and replay measurements separately.')
h('Live broker measurements')
p('Refresh runs used the stationary mixed trace (228 reads, 12 writes). Eviction runs used the read-only shift trace (240 reads) and a shared 1,207-byte payload limit. SQL identity N2, table-level invalidation I1, a fresh database clone and fresh cache per trial. The p95 is request-send to response-decoded for reads; scheduled p95 also includes queueing from the scheduled arrival. DB work sums client-observed PostgreSQL read execution and proactive refresh execution; writes are excluded.')
table(['Policy','Trace','Read hits','Refreshes','Evictions','Read p95 (ms)','Scheduled p95 (ms)','DB work (ms)','Checked digests'],
      [(name(x['policy']),x['scenario'],f"{x['hits']}/{x['reads']}",x['refreshes'],x['evictions'],f"{x['latency_p95_ms']:.1f}",f"{x['schedule_p95_ms']:.1f}",round(x['db_ms']),x['digests_checked']) for x in sorted(live,key=lambda v:(v['scenario'],v['policy']))])
p('For mutable reads we compared against independently calibrated digests at the corresponding committed state when available. Read-only runs compared every read. No digest mismatch occurred in these checks. An initial instrumentation pass used a coarse Windows clock; all live numbers in this report use the repeated high-resolution pass with client connections opened before the schedule starts.')
h('RQ2B/C · Prioritizing refresh after a write')
p('The four-entry budget limits each write, not the total number of refreshes in flight. R0 waits for demand; uncapped R1 refreshes every invalidated resident entry. R2–R7 rank entries. Decay time was fixed at 2 s and prediction horizon at 3 s for these short traces. Unlimited value capacity isolates refresh from eviction. Modeled foreground wait sums only waiting for demand SQL or an in-flight fill; it excludes Python normalization overhead and live contention. Used means subsequently consumed; unused at the trace boundary is reported separately.')
for case in ['stable','shift','hotwrites','burst','rare']:
    if case!='stable':parts.append('<details><summary>'+escape(case)+'</summary>')
    else:parts.append('<h3>Stationary popularity</h3>')
    sel=[x for x in rows if x['track']=='RQ2' and x['scenario']==case and not x['ablation'] and x['admission']=='lazy' and
         (x['policy']=='R0' or x['policy']=='R1' and x['budget'] is None or x['budget']==4)]
    table(['Policy','Budget','Writes with >4 candidates','Hits','Refreshes','Used','Wasted','Unresolved','Demand wait (ms)','DB work (ms)'],
          [(name(x['policy']),x['budget'] or ('off' if x['policy']=='R0' else 'all'),f"{x['choices_gt_4']}/{x['writes']}",x['hits'],x['refreshes'],x['used'],x['wasted'],x['used_before_end_unknown'],round(x['wait_ms']),round(x['db_ms'])) for x in sel])
    if case!='stable':parts.append('</details>')
p('On stable demand the four-candidate cap bound on 10/12 writes for R2. On bursty demand R2 used 22/44 refreshes; cost-only used 9/44. On the frequent-write trace cost-only used 16/190 and incurred much more database work than lifetime frequency. Candidate counts differ because a policy changes which results are resident at the next write. Without a binding budget R2–R6 all select the whole candidate set; ranking alone cannot separate their selection behavior.')
h('RQ3A/B/C · Eviction under finite byte budgets')
p('At 25% of each trace’s unlimited peak, the table shows cumulative modeled database milliseconds (lower is better). All nine policies were tested at unlimited, 50%, 25%, 10%, 5% and 1%. All used lazy fills and the same absolute byte budget within a trace. The stable arm included 12 writes; the remaining four arms were read-only.')
cases=['stable','skew_ro','uniform_ro','shift_ro','rare_ro']
def get(c,pol,frac):
    return next(x for x in rows if x['track']=='RQ3' and x['scenario']==c and x['policy']==pol and x['capacity_fraction']==frac and not x['ablation'])
table(['Policy',*cases],[(name('E'+str(i)),*[round(get(c,'E'+str(i),.25)['db_ms']) for c in cases]) for i in range(9)])
table(['Trace','25% budget (bytes)','LRU evictions','GDSF evictions','Combined evictions','1% budget (bytes)','1% LRU peak (bytes)'],[(c,get(c,'E1',.25)['capacity_bytes'],*[get(c,x,.25)['evictions'] for x in ('E1','E6','E8')],get(c,'E1',.01)['capacity_bytes'],get(c,'E1',.01)['peak_bytes']) for c in cases])
p('Four of five traces admitted no results at 1%. Those cells measure admission failure rather than replacement quality. In the read-only cost/popularity disagreement trace, five cheap query identities accounted for 224 reads; seven expensive identities accounted for 16. The median calibrated cost was about 0.43 ms versus 8.61 ms respectively.')
parts.append('<h3>Immediately around the shift</h3>')
p('Hit rates use 30 requests before the shift, the next 30 after the shift, and the final 30. The cache remains warm across the transition. These windows expose the short-term adaptation behavior that an aggregate can conceal.')
table(['Policy','Before','Immediately after','Final 30'],[(name('E'+str(i)),*[f"{get('shift_ro','E'+str(i),.25)[key]:.1%}" for key in ('pre_shift_hit_rate','post_shift_hit_rate','later_hit_rate')]) for i in range(9)])
h('Scope and reproducibility')
p('The stable mutable arm reused the calibrated quantity-write trace from the RQ2A pilot. Other arms used the preserved CH warehouse and new calibration. RQ2 hot writes were 20%; the other mutable traces used 5%. The rare-expensive template deliberately uses recurrent stock aggregates so it can be reused and invalidated. The same generated arrivals, SQL, parameters and calibrated costs were replayed within each scenario. All modeled valid cache hits were checked against recorded read digests; stale hit count was zero. A single seed, one order of live trials, one warehouse and short traces provide pilot evidence rather than confidence intervals.')
p('The evidence archive includes the input and calibrated traces, configuration and per-write decisions, full replay summaries, fresh live broker logs, and pilot drivers. decision and result data include R0 drop/reconsider and individual feature ablations. The high-resolution live run is the reported run.')
parts.append('</html>')
(root/'pilot-report.html').write_text('\n'.join(parts),encoding='utf8')
