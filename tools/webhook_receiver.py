"""A tiny webhook receiver for testing (phase 4).

Prints each call the bridge makes, and whether its X-TakBridge-Signature
checks out against the shared secret. This is also the reference for how a
bot verifies a call.

Usage: python tools/webhook_receiver.py PORT SECRET_FILE
       (SECRET_FILE holds the webhook secret on one line, so it never
        appears in a process list)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def main() -> int:
    port = int(sys.argv[1])
    with open(sys.argv[2], encoding="utf-8") as f:
        secret = f.read().strip().encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            sig = self.headers.get("X-TakBridge-Signature", "")
            good = hmac.compare_digest(hmac.new(secret, body, hashlib.sha256).hexdigest(), sig)
            try:
                payload = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
            except ValueError:
                payload = body.decode("utf-8", "replace")
            print(f"--- webhook {self.path} signature={'OK' if good else 'BAD'}\n{payload}",
                  flush=True)
            self.send_response(204 if good else 401)
            self.end_headers()

        def log_message(self, *args):
            pass

    print(f"listening on 127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
