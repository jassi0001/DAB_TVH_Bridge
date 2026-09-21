#!/usr/bin/env python3
# Serves a local mirror of the sundtek.de/picons/* assets that the
# Sundtek Streaming Server admin UI (http://<box>:22000/) loads from the
# internet. Point a DNS override for "sundtek.de" at this box's IP (on the
# router, so it applies to every device on the LAN) and this makes the
# admin UI fully usable without internet access.
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

CACHE_DIR = "/storage/dab-tvh-bridge/sundtek-ui-cache"
LISTEN_PORT = 80

os.chdir(CACHE_DIR)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
