"""API验收脚本"""

import atexit
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from snla.ui import server as server_module
from snla.ui.launch import prepare_loopback_server


def _skip_test_config_persistence():
    """Keep this verification script from changing the developer's .env file."""


def _skip_test_session_persistence(_session):
    """Keep this verification script from changing persisted user state."""


server_module._save_env_file = _skip_test_config_persistence
server_module.save_session = _skip_test_session_persistence
server_module.session.reset()
server_module.planner._pending.clear()
app = server_module.app

waitress_server, launch = prepare_loopback_server(app)


def run_flask():
    waitress_server.run()


t = threading.Thread(target=run_flask, daemon=True)
t.start()
time.sleep(1.0)
atexit.register(waitress_server.close)

BASE = launch.origin
bootstrap_token = parse_qs(urlsplit(launch.bootstrap_url).query)["bootstrap_token"][0]
bootstrap_body = json.dumps({"bootstrap_token": bootstrap_token}).encode()
bootstrap_request = urllib.request.Request(
    f"{BASE}/api/bootstrap",
    data=bootstrap_body,
    headers={"Content-Type": "application/json"},
)
bootstrap_response = urllib.request.urlopen(bootstrap_request, timeout=5)
SESSION_TOKEN = json.loads(bootstrap_response.read().decode())["session_token"]
AUTHORIZATION = {"Authorization": f"Bearer {SESSION_TOKEN}"}
passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"  [PASS] {label}")
        passed += 1
    else:
        print(f"  [FAIL] {label}")
        failed += 1


def api_get(path):
    request = urllib.request.Request(f"{BASE}{path}", headers=AUTHORIZATION)
    r = urllib.request.urlopen(request, timeout=5)
    return r.status, json.loads(r.read().decode())


def api_post(path, data):
    body = json.dumps(data).encode()
    rq = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        headers={**AUTHORIZATION, "Content-Type": "application/json"},
    )
    r = urllib.request.urlopen(rq, timeout=5)
    return r.status, json.loads(r.read().decode())


print("SNLA API 验收")
print("=" * 40)

# 1. Status
s, d = api_get("/api/status")
check("GET /api/status → HTTP 200", s == 200)
check("  ok=True", bool(d.get("ok")))

# 2. Variables (empty)
s, d = api_get("/api/variables")
check("GET /api/variables → HTTP 200", s == 200)

# 3. Index page
r = urllib.request.urlopen(f"{BASE}/", timeout=5)
body = r.read().decode()
check("GET / → HTTP 200", r.status == 200)
check(f"  serves index.html ({len(body)} bytes)", len(body) > 1000 and "<!DOCTYPE html>" in body)

# 4. Settings
s, d = api_post("/api/settings", {"LLM_API_KEY": "sk-test", "LLM_MODEL": "test"})
check("POST /api/settings → HTTP 200", s == 200)
check("  changed keys present", "LLM_API_KEY" in d.get("changed", []))

# 5. Analyze without data
try:
    s, d = api_post("/api/analyze", {"text": "test"})
    check("POST /api/analyze (no data) → HTTP 400", False)
except urllib.error.HTTPError as e:
    check("POST /api/analyze (no data) → HTTP 400", e.code == 400)

# 6. Export without analysis
try:
    s, d = api_get("/api/export")
    check("GET /api/export (no analysis) → HTTP 400", False)
except urllib.error.HTTPError as e:
    check("GET /api/export (no analysis) → HTTP 400", e.code == 400)

# 7. File upload (test data)
test_data_path = os.path.join(PROJECT, "data", "fixtures", "test_data.sav")
if os.path.exists(test_data_path):
    boundary = "----testboundary"
    with open(test_data_path, "rb") as f:
        file_data = f.read()
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="test_data.sav"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + file_data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    rq = urllib.request.Request(
        f"{BASE}/api/upload",
        data=body,
        headers={
            **AUTHORIZATION,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    r = urllib.request.urlopen(rq, timeout=10)
    d = json.loads(r.read().decode())
    check("POST /api/upload .sav → HTTP 200", r.status == 200)
    check("  variables extracted", d.get("ok") and len(d.get("variables", [])) > 0)
    if d.get("variables"):
        check(f"  row_count={d.get('row_count')}", d.get("row_count", 0) > 0)
        check(f"  vars: {', '.join(v['name'] for v in d['variables'][:5])}", True)
else:
    print("  [SKIP] test_data.sav not found")

print()
print(f"结果: {passed} passed, {failed} failed, {passed + failed} total")
if failed == 0:
    print("验收通过!")
else:
    print("验收失败!")
    sys.exit(1)
