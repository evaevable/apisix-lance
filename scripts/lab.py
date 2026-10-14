"""Local APISIX lab helper. Python 3.10+, standard library only."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "labs" / "01-gateway"
ENV_FILE = LAB / ".env"
ADMIN_URL = "http://127.0.0.1:9180/apisix/admin"
GATEWAY_URL = "http://127.0.0.1:9080"
OPENER = build_opener(ProxyHandler({}))


def prepare():
    if ENV_FILE.exists():
        print("Existing .env preserved.")
        return
    content = (
        f"ADMIN_KEY={secrets.token_hex(32)}\n"
        f"CONSUMER_KEY={secrets.token_hex(32)}\n"
    )
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content)
    print("Created local .env with random keys; never commit it.")


def read_keys():
    if not ENV_FILE.exists():
        raise RuntimeError("Run: python3 scripts/lab.py prepare")
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    for key in ("ADMIN_KEY", "CONSUMER_KEY"):
        if not values.get(key):
            raise RuntimeError(f"Missing {key} in local .env")
        if os.environ.get(key) and os.environ[key] != values[key]:
            raise RuntimeError(f"Unset exported {key}: it differs from the lab .env")
    return values


def compose_command():
    docker = shutil.which("docker")
    if docker:
        result = subprocess.run(
            [docker, "compose", "version"], capture_output=True, check=False
        )
        if result.returncode == 0:
            return [docker, "compose"]
    standalone = shutil.which("docker-compose")
    if standalone:
        return [standalone]
    raise RuntimeError("Install Docker Compose v2 (docker compose or docker-compose)")


def compose(*args):
    command = compose_command() + [
        "--env-file", str(ENV_FILE), "-f", str(LAB / "compose.yaml"), *args
    ]
    subprocess.run(command, check=True, cwd=ROOT)


def request(url, method="GET", data=None, headers=None):
    status, body, _ = request_full(url, method, data, headers)
    return status, body


def request_full(url, method="GET", data=None, headers=None):
    """Like request(), but also returns response headers (lowercased keys)."""
    headers = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=body, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=3) as response:
            return (response.status,
                    response.read().decode("utf-8"),
                    {k.lower(): v for k, v in response.headers.items()})
    except HTTPError as error:
        return (error.code, error.read().decode("utf-8"),
                {k.lower(): v for k, v in error.headers.items()})


def wait_for_admin(key, timeout=90):
    deadline = time.monotonic() + timeout
    last = "not ready"
    while time.monotonic() < deadline:
        try:
            status, _ = request(ADMIN_URL + "/routes", headers={"X-API-KEY": key})
            if status == 200:
                return
            if status in (401, 403):
                raise RuntimeError(f"Admin API rejected access ({status}); check key/allow_admin")
            last = f"HTTP {status}"
        except (URLError, TimeoutError, ConnectionError) as error:
            last = str(error)
        time.sleep(1)
    raise RuntimeError(f"Admin API readiness timed out: {last}")


def initialize():
    keys = read_keys()
    wait_for_admin(keys["ADMIN_KEY"])
    headers = {"X-API-KEY": keys["ADMIN_KEY"]}
    consumer = {
        "username": "lance-lab",
        "plugins": {"key-auth": {"key": keys["CONSUMER_KEY"]}},
    }
    status, body = request(ADMIN_URL + "/consumers", "PUT", consumer, headers)
    if status not in (200, 201):
        raise RuntimeError(f"Consumer creation failed: HTTP {status}")
    for file in sorted((LAB / "routes").glob("*.json")):
        data = json.loads(file.read_text(encoding="utf-8"))
        status, body = request(ADMIN_URL + f"/routes/{file.stem}", "PUT", data, headers)
        if status not in (200, 201):
            raise RuntimeError(f"Route {file.stem} rejected: HTTP {status}: {body[:400]}")
        print(f"Applied route: {file.stem}")


def check(label, status, expected):
    if status != expected:
        raise RuntimeError(f"{label}: expected HTTP {expected}, got {status}")
    print(f"PASS {label}: HTTP {status}")


def smoke():
    keys = read_keys()
    wait_for_admin(keys["ADMIN_KEY"])
    # Config watches are asynchronous. Wait for basic route propagation first.
    deadline = time.monotonic() + 30
    while True:
        status, body = request(GATEWAY_URL + "/orders/123")
        if status == 200:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Orders route not ready; run init and inspect APISIX logs")
        time.sleep(1)
    check("orders route", status, 200)
    payload = json.loads(body)
    if payload.get("service") != "orders" or payload.get("path") != "/orders/123":
        raise RuntimeError("Orders backend response mismatch")
    instances = set()
    for _ in range(40):
        status, body = request(GATEWAY_URL + "/orders/123")
        check_status = json.loads(body) if status == 200 else {}
        if status != 200:
            raise RuntimeError(f"Round-robin request failed: HTTP {status}")
        instances.add(check_status.get("instance"))
    if instances != {"orders-a", "orders-b"}:
        raise RuntimeError(f"Expected both orders instances, got {instances}")
    print("PASS round-robin: both backend instances observed")
    status, body = request(GATEWAY_URL + "/products/789")
    check("products route", status, 200)
    if json.loads(body).get("service") != "products":
        raise RuntimeError("Products backend response mismatch")
    status, _ = request(GATEWAY_URL + "/missing")
    check("unmatched route", status, 404)
    status, _ = request(ADMIN_URL + "/routes")
    check("admin without key", status, 401)
    status, _ = request(GATEWAY_URL + "/protected/orders/123")
    check("protected route without key", status, 401)
    status, _ = request(
        GATEWAY_URL + "/protected/orders/123", headers={"apikey": "invalid-lab-key"}
    )
    check("protected route with invalid key", status, 401)
    status, body = request(
        GATEWAY_URL + "/protected/orders/123", headers={"apikey": keys["CONSUMER_KEY"]}
    )
    check("protected route with consumer key", status, 200)
    if json.loads(body).get("path") != "/orders/123":
        raise RuntimeError("proxy-rewrite did not strip /protected")
    print("PASS proxy-rewrite: backend received /orders/123")
    status, body, headers = request_full(GATEWAY_URL + "/custom/orders/123")
    check("custom plugin route", status, 200)
    if json.loads(body).get("path") != "/orders/123":
        raise RuntimeError("custom plugin route: /custom prefix was not rewritten")
    if not headers.get("x-lance-request-id"):
        raise RuntimeError("custom plugin route: response header X-Lance-Request-Id missing")
    print("PASS custom plugin: rewrite and header_filter both applied")
    # Unique client-supplied bucket key is for repeatable demos, NOT production security.
    headers = {"X-Lab-Run": secrets.token_hex(16)}
    for number, expected in enumerate((200, 200, 429), 1):
        status, _ = request(GATEWAY_URL + "/limited/orders/123", headers=headers)
        check(f"limit-count request {number}", status, expected)
    print("All gateway smoke checks passed.")


def validate():
    files = sorted((LAB / "routes").glob("*.json"))
    expected_ids = {"orders", "products", "protected-orders", "limited-orders",
                    "custom-plugin"}
    found_ids = {file.stem for file in files}
    missing = expected_ids - found_ids
    if missing:
        raise RuntimeError(f"Missing lab route files: {sorted(missing)}")
    for file in files:
        route = json.loads(file.read_text(encoding="utf-8"))
        if not route.get("uri") or not route.get("upstream", {}).get("nodes"):
            raise RuntimeError(f"Invalid route structure: {file.name}")
    custom = json.loads((LAB / "routes/custom-plugin.json").read_text(encoding="utf-8"))
    if "lance-request-id" not in custom.get("plugins", {}):
        raise RuntimeError("custom-plugin route must enable lance-request-id")
    plugin_file = LAB / "plugins/apisix/plugins/lance-request-id.lua"
    if not plugin_file.is_file():
        raise RuntimeError("Custom plugin source file is missing")
    print("PASS JSON syntax and basic route structure (not APISIX schema validation)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "up", "init", "smoke", "validate", "down", "status"))
    action = parser.parse_args().action
    if action == "prepare":
        prepare()
    elif action == "up":
        prepare()
        read_keys()
        compose("up", "-d")
        wait_for_admin(read_keys()["ADMIN_KEY"])
    elif action == "init":
        initialize()
    elif action == "smoke":
        smoke()
    elif action == "validate":
        validate()
    elif action == "down":
        compose("down")  # Keep etcd volume and keys; never silently delete lab state.
    else:
        compose("ps")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
