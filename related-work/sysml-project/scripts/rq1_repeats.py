"""Collect repeated BIRD Lite trajectories. Run with BIRD's venv on Windows."""
import asyncio
from collections import defaultdict
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parents[1]
BIRD = PROJECT.parent / "BIRD-Interact"
ADK = BIRD / "BIRD-Interact-ADK"
PILOT = PROJECT / "artifacts" / "rq1-command-a"
FULL = os.environ.get("SYSML_RQ1_SCOPE") == "lite" or (__name__ == "__main__" and "--full-lite" in sys.argv)
ROOT = PILOT.with_name("rq1-command-a-full-lite") if FULL else PILOT
UPSTREAM = PROJECT / "artifacts" / "low-concurrency-20261104"
DATA = ADK / "bird-interact-lite" / "bird_interact_data.jsonl"
MODEL = "command-a-03-2025"
API = "https://api.cohere.ai/compatibility/v1"
PORTS = {"system_agent": 6310, "user_simulator": 6311, "db_environment": 6312}
PYTHON = ADK / ".venv/Scripts/python.exe"
LOCAL_MODEL = "rq1-bird-qwen35"
CONCURRENCY = 16 if FULL else 10
CAPTURE = ROOT / "db-events.jsonl"


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf8")
    tmp.replace(path)


def plan():
    raw = DATA.read_bytes()
    by_db = defaultdict(list)
    for line in raw.decode("utf-8-sig").splitlines():
        if line.strip():
            row = json.loads(line)
            by_db[row["selected_database"]].append(row)
    rng = random.Random(11)
    tasks = [rng.choice(sorted(by_db[db], key=lambda t: t["instance_id"]))
             for db in rng.sample(sorted(by_db), 10)]
    manifest = dict(agent=MODEL, n3=MODEL, simulator="qwen3.5-4b", temperature=0,
                    sampling_seed=11, waves=5, concurrency=10, mode="a-interact",
                    dataset_sha256=hashlib.sha256(raw).hexdigest(),
                    tasks=[{k: t[k] for k in ("instance_id","selected_database","difficulty_tier")} for t in tasks])
    if FULL:
        pilot_manifest = json.loads((PILOT/"manifest.json").read_bytes())
        if manifest != pilot_manifest:
            raise ValueError("Pilot dataset or experiment settings differ")
        tasks = [t for db in sorted(by_db) for t in by_db[db]]
        if len(tasks) != 300 or len({t["instance_id"].lower() for t in tasks}) != 300:
            raise ValueError("Expected 300 distinct BIRD Lite task IDs")
        rng.shuffle(tasks)
        manifest.update(scope="all-lite", tasks=[
            {k:t[k] for k in ("instance_id","selected_database","difficulty_tier")} for t in tasks])
    return tasks, manifest


def import_pilot():
    manifest = json.loads((PILOT/"manifest.json").read_bytes())
    for wave in range(1, manifest["waves"]+1):
        for task in manifest["tasks"]:
            rel = Path("attempts")/f"wave-{wave}"/(task["instance_id"]+".json")
            obj = json.loads((PILOT/rel).read_bytes())
            if (obj["wave"],obj["result"]["task_id"],obj["result"]["database"]) != (
                wave,task["instance_id"],task["selected_database"]):
                raise ValueError(f"Pilot attempt mismatch: {rel}")
            if not (ROOT/rel).exists():
                save(ROOT/rel,obj)


def gate():
    state = json.loads((UPSTREAM / "status.json").read_bytes())
    if (state["state"], state["completed"], state["total"]) != ("complete", 216, 216):
        return False
    jobs = json.loads((UPSTREAM / "manifest.json").read_bytes())["stages"]
    ids = {j["id"] for j in jobs}
    return len(ids) == 216 and all((UPSTREAM / "trials" / i / "result.json").is_file() for i in ids)


def credential():
    command = ("$ErrorActionPreference='Stop'; $s=Get-Content -Raw "
               "(Join-Path $env:USERPROFILE '.sysml/command-a.dpapi'); "
               "[Console]::Write(([pscredential]::new('cohere',(ConvertTo-SecureString "
               "$s.Trim()))).GetNetworkCredential().Password)")
    # A parent PowerShell 7 session can leave incompatible modules on PS5's search path.
    env={k:v for k,v in os.environ.items() if k.upper()!="PSMODULEPATH"}
    return subprocess.check_output(["powershell","-NoProfile","-NonInteractive","-Command",command],
                                   env=env,text=True).strip()


def run(*args, **kw):
    return subprocess.run(list(map(str,args)), check=True, **kw)


def local_json(url):
    with urlopen(url, timeout=3) as response:
        return json.load(response)


def log_service(pipe, path, secret):
    with pipe, path.open("a", encoding="utf8") as stream:
        for line in pipe:
            stream.write(line.replace(secret, "[redacted]"))
            stream.flush()


def stop_service(proc):
    if proc.poll() is None:
        # The Windows venv launcher and its real Python process are a tree.
        subprocess.run(["taskkill","/pid",str(proc.pid),"/t","/f"], capture_output=True)
        proc.wait(timeout=30)


def local_simulator(stack):
    lms = shutil.which("lms") or str(Path.home()/".cache/lm-studio/bin/lms.exe")
    status = json.loads(run(lms,"server","status","--json",capture_output=True,text=True).stdout)
    if not status["running"]:
        run(lms,"server","start","--port","1234","--bind","127.0.0.1")
        stack.callback(run,lms,"server","stop")
    sim_port = status.get("port",1234) if status["running"] else 1234
    sim_base = f"http://127.0.0.1:{sim_port}/v1"
    if LOCAL_MODEL not in {m["id"] for m in local_json(sim_base+"/models")["data"]}:
        run(lms,"load","qwen3.5-4b","--identifier",LOCAL_MODEL,"--context-length","16384")
        stack.callback(run,lms,"unload",LOCAL_MODEL)
    return sim_base


def services(stack, secret):
    compose = ["docker","compose","-f",BIRD/"env/docker-compose.yml",
               "-f",BIRD/"env/docker-compose.override.yml"]
    for port in PORTS.values():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",port))
    active = run(*compose, "ps","--status","running","--services", capture_output=True,text=True).stdout.split()
    run(*compose,"up","-d","--no-deps","postgresql")
    if "postgresql" not in active:
        stack.callback(run,*compose,"stop","postgresql")
    for _ in range(60):
        if subprocess.run(["docker","exec","bird_interact_postgresql","pg_isready","-U","root"],
                          capture_output=True).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("BIRD PostgreSQL did not become ready")
    sim_base = API if FULL else local_simulator(stack)

    base = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", DATASET="lite",
                PG_HOST="127.0.0.1",PG_PORT="5432",NO_PROXY="127.0.0.1,localhost",
                SYSTEM_AGENT_PORT="6310",USER_SIM_PORT="6311",DB_ENV_PORT="6312",
                SYSTEM_AGENT_MODEL="openai/"+MODEL, USER_SIM_MODEL="openai/"+(MODEL if FULL else LOCAL_MODEL),
                LITELLM_LOCAL_MODEL_COST_MAP="True",SYSML_BIRD_TRACE=str(CAPTURE),
                PYTHONPATH=os.pathsep.join(map(str,[ADK,PROJECT/"scripts"])),
                PATH=str(BIRD/".local/pgsql/bin")+os.pathsep+os.environ["PATH"])
    # The orchestrator reads its service URLs at import time.
    os.environ.update({k:base[k] for k in ("SYSTEM_AGENT_PORT","USER_SIM_PORT","DB_ENV_PORT","DATASET","NO_PROXY","PG_PORT")})
    for name in ("db_environment","user_simulator","system_agent"):
        env = base.copy()
        if name == "system_agent":
            env.update(LITELLM_API_BASE=API, LITELLM_API_KEY=secret)
        elif name == "user_simulator":
            env.update(LITELLM_API_BASE=sim_base,LITELLM_API_KEY=secret if FULL else "lm-studio")
        module = "bird_capture:app" if name == "db_environment" else name+".server:app"
        port = PORTS[name]
        proc = subprocess.Popen([str(PYTHON),"-B","-m","uvicorn",module,"--host","127.0.0.1","--port",str(port)],
                                cwd=ADK,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                text=True,encoding="utf8",errors="replace")
        stack.callback(stop_service,proc)
        threading.Thread(target=log_service,args=(proc.stdout,ROOT/(name+".log"),secret),daemon=True).start()
        for _ in range(150):
            if proc.poll() is not None:
                raise RuntimeError(name+" exited during startup")
            try:
                health = local_json(f"http://127.0.0.1:{port}/health")
            except OSError:
                time.sleep(1)
                continue
            if name=="system_agent" and (not health.get("adk_available") or health.get("model")!=base["SYSTEM_AGENT_MODEL"]):
                raise RuntimeError("System-agent health/model mismatch")
            break
        else:
            raise RuntimeError(name+" did not become healthy")


async def collect(tasks):
    sys.path.insert(0,str(ADK))
    from orchestrator.ainteract import run_single_task
    if FULL:
        from orchestrator import ainteract
        post = ainteract._post
        async def post_with_startup_wait(url, payload, timeout=120.0):
            # Busy parallel sessions can delay admission. The model turn keeps its own timeout.
            if url.endswith("/init_session"):
                timeout = max(timeout, 600.0)
            return await post(url, payload, timeout=timeout)
        ainteract._post = post_with_startup_wait
    done = len(list((ROOT/"attempts").glob("*/*.json")))
    total = len(tasks)*5
    slots = asyncio.Semaphore(CONCURRENCY)
    async def bounded(task,wave):
        async with slots:
            await one(task,wave)
    async def one(task, wave):
        nonlocal done
        task_id=task["instance_id"]
        dest=ROOT/"attempts"/f"wave-{wave}"/(task_id+".json")
        if dest.exists():
            return
        start=CAPTURE.stat().st_size if CAPTURE.exists() else 0
        try:
            result=await run_single_task(task)
        except Exception as exc:
            if FULL:
                # Preserve incomplete sessions separately so a resume can retry them.
                save(ROOT/"interrupted"/f"wave-{wave}"/(task_id+".json"),
                     dict(error=type(exc).__name__,time=time.time()))
                raise
            # No retry of a model attempt: failed and partial attempts remain observable.
            result={"task_id":task_id,"error":type(exc).__name__,"database":task["selected_database"]}
        events=[]
        if CAPTURE.exists():
            with CAPTURE.open("rb") as file:
                file.seek(start)
                for line in file:
                    if not line.endswith(b"\n"):
                        break
                    event=json.loads(line)
                    if event["client"]==task_id:
                        events.append(event)
        save(dest,dict(wave=wave,simulator=MODEL if FULL else "qwen3.5-4b",result=result,events=events))
        done+=1
        save(ROOT/"status.json",dict(state="collecting",completed=done,total=total,wave=wave))
    for wave in range(1,6):
        await asyncio.gather(*(bounded(t,wave) for t in tasks))
        saved=[json.loads(f.read_bytes()) for f in (ROOT/"attempts"/f"wave-{wave}").glob("*.json")]
        if all(a["result"].get("error") for a in saved):
            raise RuntimeError(f"All attempts in wave {wave} failed; inspect service logs before continuing")


def main():
    tasks, manifest = plan()
    total=len(tasks)*manifest["waves"]
    existing=ROOT/"manifest.json"
    original=dict(manifest)
    if FULL:
        manifest.update(simulator=MODEL, concurrency=CONCURRENCY,
                        simulator_history="simulator-transition.json")
    if "--plan" in sys.argv:
        print(json.dumps(manifest))
        return
    previous=json.loads(existing.read_bytes()) if existing.exists() else None
    if previous is not None and previous not in (original,manifest):
        raise ValueError("RQ1 manifest mismatch")
    if FULL:
        os.environ["SYSML_RQ1_SCOPE"]="lite"
        import_pilot()
        history=ROOT/"simulator-transition.json"
        if not history.exists():
            if previous==manifest:
                raise ValueError("Missing simulator history")
            paths=sorted(f.relative_to(ROOT).as_posix() for f in (ROOT/"attempts").glob("*/*.json"))
            save(history,dict(previous=original["simulator"],new=MODEL,changed_at=time.time(),
                              previous_concurrency=original["concurrency"],new_concurrency=CONCURRENCY,
                              previous_attempts=paths))
    save(existing,manifest)
    completed=len(list((ROOT/"attempts").glob("*/*.json")))
    if "--prepare" in sys.argv or not gate():
        if (ROOT/"status.json").exists():
            return
        save(ROOT/"status.json",dict(state="prepared",completed=completed,total=total))
        return
    secret=credential()
    with ExitStack() as stack:
        save(ROOT/"status.json",dict(state="starting_services",completed=completed,total=total))
        services(stack,secret)
        if FULL:
            templates = run("docker","exec","bird_interact_postgresql","psql","-U","root","-d","postgres",
                            "-Atc","SELECT datname FROM pg_database",capture_output=True,text=True).stdout.splitlines()
            missing = {t["selected_database"]+"_template" for t in tasks}-set(templates)
            if missing:
                raise RuntimeError(f"Missing database templates: {sorted(missing)}")
        save(ROOT/"status.json",dict(state="collecting",completed=completed,total=total))
        asyncio.run(collect(tasks))
        if len(list((ROOT/"attempts").glob("*/*.json")))!=total:
            raise RuntimeError("Attempt inventory incomplete")
        save(ROOT/"status.json",dict(state="analyzing",completed=total,total=total))
        from shared.config import settings
        env=dict(os.environ,PGHOST=settings.pg_host,PGUSER=settings.pg_user,PGPASSWORD=settings.pg_password,
                 PGPORT=str(settings.pg_port),SYSML_N3_URL=API,SYSML_N3_MODEL=MODEL,SYSML_N3_KEY=secret)
        run(PROJECT/".venv/Scripts/python.exe","-B",PROJECT/"scripts/rq1_accuracy.py",env=env)
    save(ROOT/"status.json",dict(state="complete",completed=total,total=total))


if __name__=="__main__":
    try:
        main()
    except BaseException as exc:
        save(ROOT/"status.json",dict(state="failed",error=str(exc)[:600],
                                     completed=len(list((ROOT/"attempts").glob("*/*.json"))),total=1500 if FULL else 50))
        raise
