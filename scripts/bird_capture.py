"""Optional ASGI wrapper loaded by BIRD's Python only when collecting future real traces."""

import hashlib
import json
import os
import threading
import time
import uuid
from db_environment.server import app as bird

lock = threading.Lock()
run = uuid.uuid4().hex
states = {}


async def app(scope, receive, send):
    route = scope.get("path")
    if scope["type"] != "http" or route not in {
        "/execute",
        "/submit",
        "/init_task",
        "/cleanup_task",
        "/schema",
    }:
        return await bird(scope, receive, send)
    req = bytearray()
    res = bytearray()
    status = None

    async def watched_receive():
        msg = await receive()
        req.extend(msg.get("body", b""))
        return msg

    async def watched_send(msg):
        nonlocal status
        if "status" in msg:
            status = msg["status"]
        res.extend(msg.get("body", b""))
        await send(msg)

    started = time.time()
    clock = time.perf_counter()
    try:
        await bird(scope, watched_receive, watched_send)
    finally:
        if req:
            payload = json.loads(req)
            task = payload.get("task_id")
            with lock:
                if route == "/init_task":
                    states[task] = [
                        payload["task_data"]["selected_database"],
                        uuid.uuid4().hex,
                        0,
                    ]
                if task in states and route in {"/execute", "/submit", "/schema"}:
                    database, session, epoch = states[task]
                    row = {
                        "source": "bird-capture",
                        "kind": {
                            "/execute": "read",
                            "/submit": "submission",
                            "/schema": "schema",
                        }[route],
                        "id": uuid.uuid4().hex,
                        "client": task,
                        "database": database,
                        "scope": f"{run}/{session}/{epoch}",
                        "at": started,
                        "sql": payload.get("sql"),
                        "params": [],
                        "http_status": status,
                        "endpoint_ms": 1000 * (time.perf_counter() - clock),
                        "response_bytes": len(res),
                        "response_digest": hashlib.sha256(res).hexdigest(),
                    }
                    if route == "/execute":
                        try:
                            row["successful"] = json.loads(res).get("success", False)
                        except ValueError:
                            row["successful"] = False
                    with open(
                        os.environ["SYSML_BIRD_TRACE"], "a", encoding="utf8"
                    ) as f:
                        f.write(json.dumps(row) + "\n")
                    if route == "/submit":
                        states[task][2] += 1
                if route == "/cleanup_task":
                    states.pop(task, None)
