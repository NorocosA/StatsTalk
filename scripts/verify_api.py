"""API验收脚本"""

import sys, os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

import threading, time, json, urllib.request, urllib.error

from snla.ui.server import app


def run_flask():
    app.run(host="127.0.0.1", port=18503, debug=False, use_reloader=False)


t = threading.Thread(target=run_flask, daemon=True)
t.start()
time.sleep(1.5)

BASE = "http://127.0.0.1:18503"
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
    r = urllib.request.urlopen(f"{BASE}{path}", timeout=5)
    return r.status, json.loads(r.read().decode())


def api_post(path, data):
    body = json.dumps(data).encode()
    rq = urllib.request.Request(
        f"{BASE}{path}", data=body, headers={"Content-Type": "application/json"}
    )
    r = urllib.request.urlopen(rq, timeout=5)
    return r.status, json.loads(r.read().decode())


print("SNLA API 验收")
print("=" * 40)

# 1. Status
s, d = api_get("/api/status")
check(f"GET /api/status → HTTP 200", s == 200)
check(f"  ok=True", d.get("ok") == True)

# 2. Variables (empty)
s, d = api_get("/api/variables")
check(f"GET /api/variables → HTTP 200", s == 200)

# 3. Index page
r = urllib.request.urlopen(f"{BASE}/", timeout=5)
body = r.read().decode()
check(f"GET / → HTTP 200", r.status == 200)
check(f"  serves index.html ({len(body)} bytes)", len(body) > 1000 and "<!DOCTYPE html>" in body)

# 4. Settings
s, d = api_post("/api/settings", {"LLM_API_KEY": "sk-test", "LLM_MODEL": "test"})
check(f"POST /api/settings → HTTP 200", s == 200)
check(f"  changed keys present", "LLM_API_KEY" in d.get("changed", []))

# 5. Analyze without data
try:
    s, d = api_post("/api/analyze", {"text": "test"})
    check(f"POST /api/analyze (no data) → HTTP 400", False)
except urllib.error.HTTPError as e:
    check(f"POST /api/analyze (no data) → HTTP 400", e.code == 400)

# 6. Export without analysis
try:
    s, d = api_get("/api/export")
    check(f"GET /api/export (no analysis) → HTTP 400", False)
except urllib.error.HTTPError as e:
    check(f"GET /api/export (no analysis) → HTTP 400", e.code == 400)

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
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    r = urllib.request.urlopen(rq, timeout=10)
    d = json.loads(r.read().decode())
    check(f"POST /api/upload .sav → HTTP 200", r.status == 200)
    check(f"  variables extracted", d.get("ok") and len(d.get("variables", [])) > 0)
    if d.get("variables"):
        check(f"  row_count={d.get('row_count')}", d.get("row_count", 0) > 0)
        check(f"  vars: {', '.join(v['name'] for v in d['variables'][:5])}", True)
else:
    print(f"  [SKIP] test_data.sav not found")

print()
print(f"结果: {passed} passed, {failed} failed, {passed + failed} total")
if failed == 0:
    print("验收通过!")
else:
    print("验收失败!")
    sys.exit(1)
