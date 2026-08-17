"""Quick smoke test for the FastAPI server."""
import json
import subprocess
import sys
import time
import urllib.request

proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "trade_post.api.server:create_app",
     "--factory", "--host", "127.0.0.1", "--port", "9099"],
    stdout=open(r"C:\Temp\srv_smoke.log", "w"),
    stderr=subprocess.STDOUT,
)
time.sleep(5)
results = {}
try:
    for path in ["/health", "/"]:
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:9099{path}", timeout=5)
            results[path] = resp.status
        except Exception as e:
            results[path] = str(e)

    try:
        data = json.dumps(
            {"username": "admin", "password": "@Superframer90"}
        ).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:9099/api/v1/auth/login",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        results["/login"] = resp.status
    except Exception as e:
        results["/login"] = str(e)
finally:
    proc.terminate()
    proc.wait(timeout=5)
print("SMOKE_RESULTS:", results)
