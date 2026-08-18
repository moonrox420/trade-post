"""Quick end-to-end smoke test of the auth flow (against a running server)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

base = os.environ.get("DROX_BASE_URL", "http://localhost:8065")

# Test 1: /api/v1/me without auth (should be 401)
r = httpx.get(f"{base}/api/v1/me")
print(f"GET /api/v1/me (no auth)  -> {r.status_code} {r.text[:80]}")

# Test 2: Login with the password from the environment (never hard-coded)
password = os.environ.get("DROX_ADMIN_PASSWORD", "")
username = os.environ.get("DROX_ADMIN_USERNAME", "admin")
if not password:
    print("DROX_ADMIN_PASSWORD not set; set it to the admin password to run this smoke test.")
    sys.exit(1)
r = httpx.post(
    f"{base}/api/v1/auth/login",
    json={"username": username, "password": password},
)
print(f"POST /api/v1/auth/login   -> {r.status_code} {r.text[:120]}")
if r.status_code != 200:
    sys.exit(1)

# Test 3: /api/v1/me with the cookie from login
cookies = r.cookies
r = httpx.get(f"{base}/api/v1/me", cookies=cookies)
print(f"GET /api/v1/me (with cookie) -> {r.status_code} {r.text}")

print("\nAuth flow OK")
