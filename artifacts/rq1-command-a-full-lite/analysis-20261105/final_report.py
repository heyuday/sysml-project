"""Build the final research report from immutable campaign outputs."""
import json, statistics as st
from collections import defaultdict
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import simpleSplit

P=Path(r'D:\Projects\sysml-project'); A=P/'artifacts'; B=A/'rq1-command-a-full-lite'; L=A/'low-concurrency-20261104'
def read(f): return json.loads(f.read_bytes())
rs=[read(f) for f in (L/'trials').glob('*/result.json')]
assert len(rs)==216 and sum(r['requests'] for r in rs)==432000
groups=defaultdict(list)
for r in rs: groups[r['job']['scenario'],r['job']['policy']].append(r)
assert all(len(group)==12 for group in groups.values())
q=read(B/'query-variation.json'); acc=read(B/'controlled-accuracy.json')
review_dir=Path(__file__).resolve().parent/'full-rq1-analysis'
audit=read(review_dir/'summary.json')
assert q['attempts']==1500 and audit['pairs']['included']==439

def metrics(rr):
    h=[r['metrics']['heldout'] for r in rr]; b=[r['broker'] for r in rr]
    demand=st.mean(r['metrics']['all']['demand_db_ms'] for r in rr)/1000
    refresh=st.mean(v.get('refresh_db_ms',0) for v in b)/1000
    return dict(hit=sum(t['hits'] for t in h)*100/sum(t['reads'] for t in h),
                p95=st.mean(t['latency_ms']['p95'] for t in h),db=demand+refresh,demand=demand,
                refresh=refresh,wait=st.mean(t['cumulative_wait_ms'] for t in h)/1000,
                inv=st.mean(v.get('invalidations',0) for v in b))
def m(sc,pol):return metrics(groups[sc,pol])
def gain(sc,a,b,client,seed):
    def db(pol):
        rr=[r for r in groups[sc,pol] if r['job']['clients']==client and r['job']['seed']==seed]
        return metrics(rr)['db']
    return 100*(1-db(a)/db(b))
for n,f in [('Reg','segoeui.ttf'),('Bold','segoeuib.ttf')]:pdfmetrics.registerFont(TTFont(n,'C:/Windows/Fonts/'+f))
out=Path('outputs/final-analysis');out.mkdir(parents=True,exist_ok=True)
c=canvas.Canvas(str(out/'SQL-cache-experiment-report.pdf'),pagesize=(612,792))
c.setTitle('SQL result caching for concurrent agents | Final experiment report')
c.setAuthor('SysML project')
ink='#172F42'; teal='#087F79'; blue='#3574AC'; gold='#BB6A27'; muted='#465F70'; light='#E9F1F3'; grid='#D4E0E4'
W,H=612,792; pg=0; boxes=[]
def fill(col):c.setFillColor(HexColor(col))
def rect(x,y,w,h,col):fill(col);c.rect(x,H-y-h,w,h,fill=1,stroke=0)
def txt(x,y,v,size=11,b=False,col=ink,right=False):
    fill(col);c.setFont('Bold' if b else 'Reg',size);(c.drawRightString if right else c.drawString)(x,H-y,str(v))
def para(y,v,size=11.1,w=522,x=45,line=None,col=ink):
    lh=line or size*1.47
    for s in simpleSplit(v,'Reg',size,w):
        txt(x,y,s,size,col=col);y+=lh
    return y
def title(tag,name,sub):
    global pg
    pg+=1;c.bookmarkPage('p'+str(pg));c.addOutlineEntry(name,'p'+str(pg))
    rect(0,0,612,7,teal);txt(45,38,'SYSTEMS RESEARCH   /   SQL RESULT CACHING',8.5,True,teal)
    txt(567,38,'05 NOV 2026     /     '+tag,8.5,True,muted,True)
    txt(45,88,name,24,True);para(118,sub,11.5,line=16,col=muted)
def end():
    rect(45,750,522,1,grid);txt(45,770,'SysML project  /  BIRD-Interact Lite and controlled CH workloads',8,col=muted)
    txt(567,770,str(pg).zfill(2),8,True,muted,True);c.showPage()
def section(y,s):txt(45,y,s,14,True,teal)
def caption(y,s):return para(y,s,9.2,line=13,col=muted)
def table(y,headers,rows,widths,rh=24):
    x=45;rect(x,y,sum(widths),rh,ink)
    for label,w in zip(headers,widths):txt(x+7,y+rh*.69,label,9,True,'#FFFFFF');x+=w
    y+=rh
    for i,row in enumerate(rows):
        if i%2==0:rect(45,y,sum(widths),rh,'#F0F5F6')
        x=45
        for j,(s,w) in enumerate(zip(row,widths)):
            txt(x+7,y+rh*.68,s,9.6,j==0);x+=w
        y+=rh
    return y
def bars(y,name,labels,values,maximum,unit,step,colors=None,second=None,fmt='.1f'):
    start=y-21;txt(45,y,name,12,True); x=175; width=318; row=25
    for j in range(int(maximum/step)+1):
        pos=x+width*j*step/maximum;rect(pos,y+12,.6,len(labels)*row,grid)
        txt(pos-2,y+len(labels)*row+24,f'{j*step:g}',8,col=muted)
    for i,(label,v) in enumerate(zip(labels,values)):
        yy=y+16+i*row;txt(45,yy+11,label,10)
        rect(x,yy,width*v/maximum,13,(colors or [teal]*len(labels))[i])
        v2=second[i] if second else 0
        if v2:rect(x+width*v/maximum,yy,width*v2/maximum,13,gold)
        txt(x+width*(v+v2)/maximum+5,yy+11,format(v+v2,fmt)+unit,9,True)
    boxes.append([pg,[34,start,577,y+len(labels)*row+33],name])
def metricrow(y,items):
    for i,(num,label) in enumerate(items):
        x=45+177*i;rect(x,y,166,72,light);txt(x+11,y+32,num,22,True,teal);txt(x+11,y+55,label,9,True)

title('RESEARCH BRIEF','What did the cache experiments show?',
      'Analysis of all 300 BIRD-Interact Lite tasks and the completed SQL cache-policy experiments. RQ1 studies query variation and normalization. RQ2 and RQ3 study cache behavior under replay.')
metricrow(158,[('1,500','BIRD task attempts'),('216','cache-policy trials'),('432,000','replayed requests')])
section(267,'RQ1: normalization could recover some repeated work')
para(291,'Among 336 reads with an earlier comparable attempt on the same task, 23 repeated earlier SQL exactly and 22 more had equivalent SQL with different text. Recognizing those rewrites could raise reuse opportunities from 6.8% to 13.4%. The current broker accepted only 47 of 730 qualifying reads, so all four methods delivered the same three chronological matches.')
section(410,'RQ2: selective invalidation saved database work')
para(434,'Checking whether a write could affect a query’s row filter used 4.19 SQL seconds per trial versus 5.33 for table invalidation, a 21.4% reduction. It won on SQL work in all 12 matched settings. Client p95 rose from 29.8 to 30.9 ms. Proactive refresh showed a tradeoff: in bursts, combined refresh raised cache hits by 9.0 percentage points but used 2.9% more SQL time than lazy refresh.')
section(567,'RQ3: eviction by cost and reuse beat LRU on this trace')
para(591,'With limited cache space and a mid-trial popularity shift, combined eviction used 8.44 SQL seconds versus 8.99 for least recently used eviction, a 6.1% reduction. It won in 10 of 12 settings and lowered mean trial p95 from 52.0 to 46.1 ms. This result covers one cache-capacity setting and three request seeds.')
caption(699,'Reading guide    02 Workloads and metrics   03-08 RQ1   09 Invalidation   10 Refresh\n11 Eviction   12 Consistency   13 Policy comparisons   14 Conclusions   15-16 Details')
end()

title('METHOD','What the workloads and times mean','For RQ2 and RQ3, each trial replays 2,000 scheduled requests on a fresh database. Policies in a comparison receive the same SQL and arrival schedule for a given seed and client count.')
section(173,'Three ways requests arrive')
para(197,'Stable. The same items stay popular throughout. Requests arrive at random intervals averaging 25 per second. Of the 2,000 requests, 100 update stock quantities. This tests how much cached data a write forces the system to discard.')
para(267,'Burst. The same read/write mix now includes a write at the start of every 40-request block. In the first eight requests of each block, operations that use an item key target the same item, with arrivals grouped into tenths of a second. Refresh gets a chance to prepare data that readers may ask for soon.')
para(351,'Popularity shift. All 2,000 requests are reads. Halfway through, a different set of items becomes popular. The cache can hold 25% of the peak result bytes seen during the first 800 requests. This tests which results to keep when space runs out.')
section(437,'Two clocks answer different questions')
para(461,'Read SQL time (seconds per trial, lower is better) adds the time spent waiting for PostgreSQL to execute reads, including any background refreshes, across all 2,000 requests. Calls can overlap. A bar of 5 seconds means 5 accumulated seconds of SQL calls. Writes and other cache operations are outside this measure.')
para(548,'Read p95 (milliseconds per request, lower is better) measures the client experience from sending a read to parsing its reply. A trial p95 of 30 ms means about 95% of its measured reads finished in 30 ms or less. Hit rate (higher is better) is the share served from cache. Both use reads in the final 1,200 requests, after 800 warm-up requests.')
caption(664,'Each policy/workload average covers 12 trials: three random request sequences (seeds 11, 27 and 43), each replayed with 1, 8, 32 and 64 clients. The client count limits overlapping requests. Overall SQL savings compare summed times before rounding. The 216 trials ran one at a time.')
end()

title('RQ1   /   COLLECTION','What the full BIRD run contains','Every Lite task received five attempts from Command A at temperature zero. All 1,500 attempts finished. “Finished” means a recorded agent outcome; task correctness is shown separately below.')
table(172,['Measure','Qwen simulated user','Command A simulated user'],[
 ['Attempts',589,911],
 ['Tasks represented',300,290],
 ['Passed the first task phase',9,21],
 ['Passed the follow-up phase',0,7],
 ['Successful exploratory calls',295,499],
 ['Successful single SELECTs before submission',269,461],
 ['Attempts contributing those SELECTs',242,418],
 ['Tasks with reads in at least two attempts',73,141]],
 [255,122,145],27)
section(439,'The simulator changed partway through collection')
para(463,'The first 589 attempts used Qwen3.5-4B as the simulated user. The remaining 911 used Command A. Each comparison in this report stays within the same task and simulator. The ten pilot tasks have five Qwen attempts. The other 290 tasks have either one Qwen plus four Command A attempts, or two Qwen plus three Command A attempts.')
section(573,'Why 1,500 attempts yield 730 qualifying reads')
para(597,'The exploratory endpoint received 1,530 calls: 736 failed and 794 succeeded. Of those successes, 59 followed a submission. Five more fall outside single-query read analysis: three statements contain writes, one requests an execution plan and one contains two SELECTs. This leaves 730 successful single SELECTs on the starting state.')
caption(699,'Both columns use Command A as the SQL-writing agent. Completion and success counts describe the observed run. The simulator groups differ in task mix and number of attempts, so their percentages cannot establish which simulator performs better.')
end()

title('RQ1   /   VARIATION','How often did new SQL mean the same thing?','We read attempts in run order. A read is comparable when an earlier attempt on the same task, using the same simulator, produced at least one qualifying read. This gives 336 later reads.')
txt(45,177,'Composition of later reads  |  each row totals 100%',12,True)
colors=[blue,teal,'#D7E0E5']
for i,(sim,label) in enumerate([('qwen3.5-4b','Qwen user'),('command-a-03-2025','Command A user')]):
    counts=audit['cohorts'][sim]['counts']
    values=[counts[k] for k in ['exact_reuses','equivalent_reuses','different_reads']]
    x=170;y=205+i*57
    txt(45,y+13,label,10,True)
    for value,color in zip(values,colors):
        width=360*value/sum(values);rect(x,y,width,22,color)
        label_width=pdfmetrics.stringWidth(str(value),'Bold',10)
        txt(x+(width-label_width)/2,y+15,str(value),10,True,'#FFFFFF' if color!=colors[2] else ink)
        x+=width
    txt(170,y+37,f"{sum(values)} comparable reads",9,col=muted)
for x,color,label in [(45,blue,'Exact repeat'),(185,teal,'Equivalent rewrite'),(365,colors[2],'Different result contract')]:
    rect(x,328,10,10,color);txt(x+15,338,label,9)
boxes.append([pg,[34,155,577,350],'Query variation within each simulator'])
table(365,['Later-read outcome','Qwen user','Command A user','Total'],[
 ['Earlier SQL repeated exactly','5 / 89','18 / 247','23 / 336 (6.8%)'],
 ['New text, equivalent to earlier SQL','4 / 89','18 / 247','22 / 336 (6.5%)'],
 ['Different from every earlier read','80 / 89','211 / 247','291 / 336 (86.6%)']],
 [240,80,90,112],27)
section(514,'Normalization added 22 potential reuse opportunities')
para(538,'Exact matching could reuse 23 of these later reads. The reviewed equivalences add another 22, for 45 of 336 (13.4%). Across all 730 qualifying reads, including first observations, the additional 22 equal 3.0 percentage points. These offline opportunities require matching database and session/time context. The implemented policies recovered fewer because they bypass most SQL.')
para(634,'Variation alone overstates the need for normalization. New SQL text appeared in 313 of 336 later reads (93.2%), but only 22 of those 313 preserved an earlier result contract. Changes to selected values, column names, joins, filters or ordering generally require a separate entry.',10.8)
end()

title('RQ1   /   COVERAGE','Most reads were bypassed before normalization','All four broker policies share the same conservative SQL-admission rules. A bypass sends the read to the database without creating a result-cache key. Broader query coverage is the largest observed obstacle.')
bars(177,'Qualifying read calls  /  730 total',['Accepted for keys','Bypassed'],[47,683],750,'',150,[teal,gold],fmt='.0f')
caption(277,'Accepted coverage is 47 / 730 = 6.4% for every method. Bypassed coverage is 93.6%. These bars describe the implementation’s admitted SQL subset.')
table(321,['First rejection recorded','Calls','Share of 730 reads'],[
 ['Relation outside the admitted catalog',285,'39.0%'],
 ['LIMIT, DISTINCT or window restriction',200,'27.4%'],
 ['Unsupported function',95,'13.0%'],
 ['Unsupported operator',93,'12.7%'],
 ['Other restrictions',10,'1.4%']],
 [345,60,117],26)
section(516,'Why ordinary BIRD queries hit this filter')
para(540,'The catalog builder excludes an entire table if any column uses an unsupported type, including custom enums. The relation check also treats CTE names as untracked relations. Other rules reject constructs such as window functions, LIMIT and JSON operators. The reason counts report the first rejection, so one query can have additional unsupported features.')
section(646,'What the accepted subset achieved')
para(670,'Each method admitted 47 reads into 44 distinct keys and recovered three chronological repeats. Every reviewed equivalent pair had at least one bypassed statement. The current results therefore support expanding and validating admission before drawing a broad ranking of the normalizers.',10.8)
end()

title('RQ1   /   CONTROLLED','Which SQL pairs share a cache key?','Six labeled pairs should share a key. Ten should stay separate because their SQL, parameters, or session scope give them different cache identities.')
bars(174,'Equivalent pairs merged  /  6 positives  /  longer is better',['N0   Raw SQL','N1   Lexical','N2   Parsed SQL','N3   Command A'],[r['scores']['true_merges'] for r in acc],6,'',1,[blue,teal,teal,gold],fmt='.0f')
rows=[]
for result in acc:
    s=result['scores']
    precision='Undefined' if s['precision'] is None else f"{100*s['precision']:g}%"
    rows.append([result['policy'],f"{s['true_merges']} / 6",f"{s['true_separations']} / 10",
                 f"{s['false_merges']} / 10",f"{100*s['recall']:.1f}%",precision])
table(335,['Method','Recovered pairs','Correct splits','False merges','Recall','Precision'],
      rows,[72,110,110,90,65,75])
caption(475,'N0 kept every pair separate. It correctly separated all 10 negative pairs and missed all 6 equivalent pairs. Recall measures recovered equivalents. Precision is undefined when a method emits zero merges. These 16 examples give limited resolution; N3 leads N2 by one pair.')
section(524,'What each method recovered')
para(548,'N1 and N2 merged the case/whitespace variant and the unquoted-name variant. N3 also merged redundant parentheses. All three missed predicate reordering, the primary-key non-null condition and the PostgreSQL-specific equivalent. N0 made zero merges, so its precision is undefined.')
para(621,'A bypass counts as a decision to keep a pair separate. All four bypassed the negative LIMIT pair and the positive DISTINCT pair. This scoring credits the first and counts the second as a miss. N3 accepts model proposals through a PostgreSQL syntax-tree check. Its recall advantage over N2 rests on one pair.')
caption(712,'Source: controlled-accuracy.json. The controlled score compares key decisions with labels. The saved run contains no execution-based snapshot checks for these pairs.')
end()
title('RQ1   /   REVIEW','Accuracy on the collected SQL pairs','We reviewed 439 pairs of distinct SQL texts from different attempts on the same task and simulator. The labels identify 22 equivalent pairs and 417 pairs with different observable behavior.')
table(174,['Method','Recovered / 22','False merges / 417','Recall','Precision'],[
 ['N0 Raw SQL','0 / 22','0 / 417','0%','Undefined'],
 ['N1 Lexical','0 / 22','0 / 417','0%','Undefined'],
 ['N2 Parsed SQL','0 / 22','0 / 417','0%','Undefined'],
 ['N3 Command A','0 / 22','0 / 417','0%','Undefined']],
 [130,105,125,65,97],28)
caption(333,'Recall asks how many known equivalent pairs were merged. Precision asks how many emitted merges were correct. With zero merges, precision is undefined. Three of 442 candidate pairs were excluded because they contain calls outside the single-SELECT analysis; the remaining 439 include 22 missed equivalences.')
section(411,'How the labels were established')
para(435,'Thirteen positive pairs have identical PostgreSQL syntax trees after source locations are removed. SQL inspection with schema constraints established nine more positives. Of the negatives, 349 differ in column names, types or type modifiers; 46 differ in returned row multisets on the same template snapshot; 22 differ in other inspected semantics, such as ordering or error behavior.')
para(535,'Replay used read-only transactions with UTC and a fixed search path. Unordered results were compared as multisets, preserving duplicates. A matching snapshot alone was insufficient to label a pair equivalent. These are report-review labels, separate from the authors’ task-completion ground truth.')
section(636,'The accuracy comparison is limited by admission')
para(660,'All 22 positive pairs include a bypassed query. The result shows zero recovered equivalences in this broker configuration. It provides little evidence about how N1, N2 or N3 would compare after broader SQL support is implemented.',10.8)
end()

title('RQ1   /   EXAMPLES','Which differences can share a cached result?','Normalization is useful when SQL changes while output values, column names and types, ordering guarantees, and relevant error behavior stay equivalent.')
section(174,'Internal aliases changed, while the result stayed equivalent')
para(198,'For credit_1, one attempt called the two tables c and e. Another used t1 and t2. The selected customer ID, assets, liabilities, net worth and rank were the same, as were the join condition and final sort. A normalizer that handles bound aliases could connect this pair.')
section(292,'A schema fact justified a different COUNT expression')
para(316,'For cross_db_2, a denominator changed from COUNT(*) to COUNT(DISTINCT vendortrace). The schema declares vendortrace as a non-null primary key, so both count every vendor exactly once. That equivalence depends on checking the schema constraint.')
section(410,'Output names and sort rules also matter')
para(434,'Two queries returning the same values can require different cache entries when output-column names differ. The review found 349 pairs with different output metadata. Separately, virtual_4 changed sorting from text to numeric values. Text order places 9 above 10; numeric order places 10 above 9. That pair requires separate entries.')
section(555,'How much distinct-key redundancy did the review find?')
para(579,'The 730 qualifying reads contained 703 distinct task/simulator/SQL texts. Grouping the reviewed equivalent texts reduces that count to 682, a reduction of 21 keys (3.0%). This is a supported reduction under the review labels; further equivalent pairs may exist among texts seen only within one attempt, which this comparison did not enumerate.')
caption(699,'The 439 pairs are comparisons between distinct texts, and the 336 later reads are chronological reuse opportunities. They have different denominators. A pair can support more than one later reuse, and three equivalent texts can generate three pairs.')
end()

title('RQ2A   /   INVALIDATION','Which results should a write discard?','Stable workload: the same items stay popular, and 5% of requests update stock. Each policy decides which cached reads may be stale. Discarding too many forces the database to repeat work.')
pol=['direct','I0','I1','I2','I3','I4']
bars(175,'Seconds spent executing reads per trial  |  shorter is better',['Direct   No cache','I0   Clear all','I1   By table','I2   By column','I3   By row condition','I4   Check revisions'],
     [m('stable',p)['db'] for p in pol],15,' s',3,[blue,blue,blue,blue,teal,teal],fmt='.2f')
section(396,'Checking the row condition saved 1.14 SQL seconds per trial')
para(420,'Table invalidation (I1) discards cached results when a table they read changes. Predicate invalidation (I3) checks whether the changed row could satisfy the query’s filter. I3 discarded 315 entries per trial versus 803 for I1. That raised hits from 49.8% to 75.2% and cut read SQL time from 5.33 to 4.19 seconds (21.4%). Client p95 went from 29.8 to 30.9 ms.')
section(530,'Why more entries survived the same writes')
para(554,'In each 240-operation pilot arm, an oracle compared previously seen eligible query results before and after writes. For quantity updates, I1 selected 452 query/write pairs. Of these, 57 results changed and 395 selections were unnecessary. I3 selected 111, catching the same 57 with 54 unnecessary selections. For updates to an unreferenced year-to-date column, I1 selected 452 unnecessary pairs versus 62 for I2. Both arms used seed 11 and eight clients.')
caption(680,'Scope: I3 handles a restricted set of integer predicates and falls back conservatively. The oracle pilot directly measures unnecessary selections; the larger live experiment measures cache hits, database work and response times.')
end()
title('RQ2B   /   REFRESH','When does refreshing a result pay off?','After a write, lazy refresh waits until someone requests the result again. Proactive refresh runs a read in advance. It can turn a future miss into a hit, but every refresh uses database time.')
pol=['I1','R1','R2','R7']
bars(175,'Stable demand  |  SQL seconds per trial  |  shorter is better',['R0   Lazy','R1   Refresh all','R2   Frequency','R7   Combined'],
     [m('stable',p)['demand'] for p in pol],15,' s',3,second=[m('stable',p)['refresh'] for p in pol],fmt='.2f')
rect(45,315,11,10,teal);txt(62,324,'Demand reads',9);rect(160,315,11,10,gold);txt(177,324,'Background refresh',9)
section(364,'With stable demand, most eager refreshes went unused')
para(388,'Only 14.8% of R1’s refreshes were used. Its read SQL time rose from 5.33 seconds with lazy refresh to 13.90 seconds. R2 selects by request frequency. R7 also weighs execution cost, recent use and predicted reuse before another write. Both select at most four candidates per decision; their total SQL times were 6.72 and 6.25 seconds per trial.')
txt(45,475,'Burst demand: a write followed by a cluster targeting the same item',11,True)
table(489,['Policy','Hit rate ↑','p95 ↓','Demand SQL ↓','Refresh SQL','Total SQL ↓'],
      [[p+{'R0':'  Lazy','R2':'  Frequency','R7':'  Combined'}[p],f"{m('burst',p)['hit']:.1f}%",f"{m('burst',p)['p95']:.1f} ms",
        f"{m('burst',p)['demand']:.2f} s",f"{m('burst',p)['refresh']:.2f} s",f"{m('burst',p)['db']:.2f} s"] for p in ['R0','R2','R7']],
      [113,72,80,84,86,87],25)
para(623,f"In bursts, R7 gained 9.0 hit-rate points over R0 and lowered mean p95 by 1.6 ms. Its demand plus refresh SQL took {100*(m('burst','R7')['db']/m('burst','R0')['db']-1):.1f}% longer overall. R7 beat R0 on read SQL time in 3 of 12 matched settings. The small latency difference should be interpreted against single-host timing variability.")
end()
title('RQ3   /   EVICTION','Which results earn their cache space?','Halfway through a read-only trial, a different set of items becomes popular. When space runs out, LRU evicts the least recently used result. E8 also weighs the cost to fetch a result again and how much space it takes.')
p3=['direct','E1','E2','E3','E6','E8']
bars(175,'Seconds spent executing reads per trial  |  shorter is better',['Direct   No cache','E1   LRU','E2   LFU','E3   Decayed freq.','E6   GDSF','E8   Combined / byte'],
     [m('shift_ro',p)['db'] for p in p3],16,' s',4,[blue,blue,blue,blue,blue,teal],fmt='.2f')
section(393,'E8 saved 0.55 SQL seconds per trial versus LRU')
para(418,'Across 14,400 held-out reads per policy, E8 provided 9,964 hits versus 9,833 for LRU (69.2% versus 68.3%). Mean read SQL fell from 8.99 to 8.44 seconds and mean p95 from 52.0 to 46.1 ms. E6 also did well at 8.55 seconds. This GreedyDual-Size-Frequency variant uses frequency, cost, value size and an aging term. E8 used 1.3% less read SQL time than E6.')
section(524,'Response to the popularity shift')
para(549,'In the 200 requests before the shift, E8 and LRU hit 71.2% and 69.8%. In the first 200 requests after it, both hit 63.9% after rounding. The run supports a lower overall database cost for E8; the immediate post-shift hit rates offer little evidence for faster adaptation.')
para(638,'In the separate short offline eviction pilot, E8 and LRU produced the same modeled database work at 1% capacity on all four read-only scenarios. At 50% they differed. Those single-seed modeled results suggest capacity matters; the live ranking applies to the 25% setting.',10.5)
end()
title('ROBUSTNESS','Who used less SQL time in each trial?','Read each cell as a sentence about the first policy. For example, “15% less” means it used 8.5 SQL seconds for every 10 used by the baseline. “15% more” means it used 11.5 seconds and the baseline won.')
def heat(y,sc,a,b,label):
    txt(45,y,label,11.4,True);seeds=[11,27,43];cs=[1,8,32,64]
    for j,cl in enumerate(cs):txt(175+94*j,y+25,f'{cl} client'+('s' if cl>1 else ''),9,True)
    for i,seed in enumerate(seeds):
        yy=y+33+22*i;txt(47,yy+15,f'Seed {seed}',10)
        for j,cl in enumerate(cs):
            saved=gain(sc,a,b,cl,seed);x=161+94*j
            rect(x,yy,88,19,'#D9EBE8' if saved>=0 else '#F5DCC6')
            txt(x+10,yy+14,f'{abs(saved):.1f}% '+('less' if saved>=0 else 'more'),10,True)
    boxes.append([pg,[34,y-21,577,y+103],label])
heat(180,'stable','I3','I1','Stable: row-condition invalidation used less SQL time than table invalidation in 12/12')
heat(315,'burst','R7','R0','Burst: combined refresh used less SQL time than lazy refresh in 3/12')
heat(450,'shift_ro','E8','E1','Popularity shift: combined eviction used less SQL time than LRU in 10/12')
section(578,'Correctness and timing boundaries')
para(601,'Of 432,000 live requests, 14,400 were writes and 417,600 were reads. The replay checker compared 414,157 reads at matching reference versions and observed zero digest mismatches. The other 3,443 fell outside its comparable-version rule. Their freshness is unresolved. The checked count includes both cache hits and reads executed by PostgreSQL.')
para(682,'In one separate host test, running two isolated stacks together raised read p95 from 38.0 to 41.9 ms (32 clients) and 39.7 to 45.4 ms (64 clients). The 216 campaign trials ran one at a time. Other laptop activity was unmeasured, and small latency differences remain uncertain.',10)
end()
title('POLICY COMPARISONS','Compare policies against the same baseline','Each block asks a separate question. SQL seconds measure database read work per trial. The last column gives the percent more or less work than the named baseline. Higher hit rate and lower p95 are preferable.')
names={'direct':'Direct / no cache','I0':'Clear all (I0)','I1':'By table (I1)','I2':'By column (I2)','I3':'By row condition (I3)','I4':'Check revisions (I4)','R0':'Lazy (R0)','R1':'Refresh all (R1)','R2':'By frequency (R2)','R7':'Combined (R7)','E1':'Least recently used (E1)','E2':'Least frequently used (E2)','E3':'Decayed frequency (E3)','E6':'Cost, size, frequency (E6)','E8':'Combined per byte (E8)'}
def comparison(y,heading,sc,baseline,policies):
    txt(45,y,heading,11.4,True,teal)
    rows=[]
    for policy in policies:
        v=m(sc,policy)
        change=100*(v['db']/m(sc,baseline)['db']-1)
        label='Baseline' if policy==baseline else f'{abs(change):.1f}% '+('less' if change<0 else 'more')
        name='Lazy (same as I1)' if sc=='stable' and baseline=='I1' and policy=='I1' and 'R1' in policies else names[policy]
        rows.append([name,f"{v['hit']:.1f}%",f"{v['p95']:.1f} ms",f"{v['db']:.2f} s",label])
    table(y+6,['Policy','Hit rate ↑','Read p95 ↓','SQL time ↓','SQL vs baseline'],rows,[175,69,85,81,112],18)
comparison(173,'Stable demand: which invalidation rule beats discarding by table?', 'stable','I1',['direct','I0','I1','I2','I3','I4'])
comparison(324,'Stable demand: does refreshing in advance beat waiting for a read?', 'stable','I1',['I1','R1','R2','R7'])
comparison(435,'Burst demand: does refreshing just after a write pay off?', 'burst','R0',['R0','R2','R7'])
comparison(530,'Popularity shift: which eviction rule beats least recently used?', 'shift_ro','E1',p3)
caption(682,'Each row averages 12 trials for timing and pools held-out reads for hit rate. Stable I1 is shown twice because it is also the lazy refresh baseline. Use page 12 for variation across seeds and clients and page 16 for the full time breakdown.')
end()

title('INTERPRETATION','What the results support','The findings below apply to the implemented broker, the observed agent trajectories, and the three controlled workloads. Each live-policy comparison uses three request seeds and four client counts.')
section(174,'RQ1: equivalent rewrites occur, but admission limits reuse')
para(198,'Among 336 later comparable reads, 22 were equivalent rewrites of earlier SQL. The reviewed classes remove 21 of 703 distinct texts. All four implemented methods emitted the same 44 keys and recovered the same three chronological matches. Broader SQL support is needed before the real trajectories can distinguish their normalization accuracy.')
section(303,'RQ2: selective invalidation helped; refresh had a tradeoff')
para(327,'Predicate-aware invalidation preserved more reusable results than table invalidation and reduced read SQL time in every matched setting. Its p95 was slightly higher. Combined refresh raised the burst hit rate and slightly lowered p95, while using 2.9% more read SQL time overall. Higher hit rate by itself is therefore insufficient evidence of lower database work.')
section(432,'RQ3: combined eviction helped at the tested capacity')
para(456,'Combined eviction used 6.1% less read SQL time than LRU and won in 10 of 12 settings. The margin over the cost/size/frequency policy was 1.3%. The immediate post-shift hit rates for combined eviction and LRU both rounded to 63.9%, so evidence for faster adaptation is limited.')
section(561,'Limits that affect the conclusions')
para(585,'Only 30 of 1,500 agent attempts passed phase 1, and seven passed phase 2. The read sample reflects many unsuccessful task trajectories. The simulator changed during collection, so repeats are compared within each simulator. Pair labels use SQL review and a single template snapshot per database, with no randomized state testing. The live policy trials used one laptop and unmeasured background activity.',10.8)
caption(699,'Evidence is saved under artifacts/rq1-command-a-full-lite/analysis-20261105/. The source trial results remain under artifacts/low-concurrency-20261104/. Pair-review evidence records a reason for every label and all five excluded exploratory calls.')
end()

title('APPENDIX   /   LATENCY','Read response time by client count','Lower is faster for the reader. Each point is the mean of three per-trial p95 read latencies, one per seed, over the held-out portion. Both panels share the same vertical scale.')
def lines(y,sc,name,pols):
    txt(45,y,name,12,True);x0,x1=96,500;top=y+22;bottom=y+145
    Y=lambda v:bottom-(v-20)*(bottom-top)/40
    for v in [20,30,40,50,60]:
        yy=Y(v);rect(x0,yy,x1-x0,.65,grid);txt(x0-9,yy+3,str(v),8,col=muted,right=True)
    txt(46,top-3,'ms',9,True,muted)
    for j,n in enumerate([1,8,32,64]):txt(x0+j*(x1-x0)/3-3,bottom+15,str(n),9,col=muted)
    txt(513,bottom+15,'clients',8,col=muted)
    for i,(p,label,color) in enumerate(pols):
        vals=[metrics([r for r in groups[sc,p] if r['job']['clients']==n])['p95'] for n in [1,8,32,64]]
        pts=[(x0+j*(x1-x0)/3,H-Y(v)) for j,v in enumerate(vals)]
        c.setStrokeColor(HexColor(color));c.setLineWidth(1.8)
        for a,b in zip(pts,pts[1:]):c.line(*a,*b)
        for x,z in pts:fill(color);c.circle(x,z,3.1,fill=1,stroke=0)
        rect(95+i*140,bottom+33,12,4,color);txt(112+i*140,bottom+38,label,9,True)
    boxes.append([pg,[34,y-20,577,bottom+52],name])
lines(176,'stable','Stable workload with writes',[('direct','Direct',blue),('I1','I1  Table',gold),('I3','I3  Predicate',teal)])
lines(409,'shift_ro','Read-only popularity shift',[('direct','Direct',blue),('E1','E1  LRU',gold),('E8','E8  Combined',teal)])
para(649,'On the stable workload, both invalidation policies lowered p95 versus direct execution at every client count. On the read-only shift, E8 had lower p95 than LRU at all four counts. At 64 clients, direct execution averaged 44.2 ms versus 44.9 ms for E8. The offered rate was fixed, so these curves do not measure maximum throughput.',10.6)
end()
title('APPENDIX   /   METRICS','Detailed time breakdown for all 18 policies','Twelve trials per row. Hit rates are pooled over held-out reads. Time columns show means per trial. Lower time and higher hit rate are preferable. Page 13 gives the baseline comparisons.')
rows=[]
for sc,ps in [('stable',['direct','I0','I1','I2','I3','I4','R1','R2','R7']),('burst',['R0','R2','R7']),('shift_ro',p3)]:
    for p in ps:
        v=m(sc,p);rows.append([{'stable':'Stable','shift_ro':'Shift, read-only','burst':'Burst'}[sc],p,f"{v['hit']:.1f}%",f"{v['p95']:.1f}",f"{v['wait']:.2f}",f"{v['demand']:.2f}",f"{v['refresh']:.2f}",f"{v['db']:.2f}"])
table(160,['Workload','Policy','Hit rate','p95 ms','Wait s','Demand s','Refresh s','Read SQL s'],rows,[94,52,52,61,56,66,66,75],24)
caption(645,'Held-out read counts per row: stable 13,668; burst 13,720; shift 14,400. Wait sums client waits, including overlapping requests. Read SQL adds unrounded demand and refresh times. Zero hits on the direct baseline follow from disabling caching. Stable I1 also serves as the lazy refresh baseline.')
caption(701,'I0 global, I1 table, I2 column, I3 predicate, I4 revisions. R0 lazy, R1 eager, R2 frequency, R7 combined. E1 LRU, E2 LFU, E3 decayed frequency, E6 GDSF, E8 combined score per byte.')
end()
c.save()
(out/'figure-index.tmp').write_text(json.dumps(boxes),encoding='utf8')
