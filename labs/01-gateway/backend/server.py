"""Small, dependency-free backend for a local gateway lab, not production."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def make_server(host="127.0.0.1", port=8080, service="orders", instance="orders-a"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self.send_json(200, {"status": "ok", "service": service})
            elif path.startswith(f"/{service}/"):
                self.send_json(200, {
                    "service": service,
                    "instance": instance,
                    "path": path,
                    "method": "GET",
                })
            else:
                self.send_json(404, {"error": "not_found", "path": path})

        def send_json(self, status, data):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            if os.environ.get("QUIET") != "1":
                super().log_message(fmt, *args)

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    server = make_server(
        os.environ.get("BIND_ADDRESS", "127.0.0.1"),
        int(os.environ.get("PORT", "8080")),
        os.environ.get("SERVICE_NAME", "orders"),
        os.environ.get("INSTANCE_NAME", "orders-a"),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
