"""An in-process fake of the Ollama HTTP API for tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        fake = self.server.fake
        if self.path == "/api/tags":
            self._send_json({"models": [{"name": n, "size": 1000, "details": {
                "parameter_size": "1B", "quantization_level": "Q4"}} for n in fake.models]})
        elif self.path == "/api/ps":
            self._send_json({"models": [{"name": n, "model": n,
                                         "context_length": fake.context_length}
                                        for n in fake.models if n in fake.loaded]})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        fake = self.server.fake
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            if payload["model"] not in fake.models:
                return self._send_json({"error": f"model '{payload['model']}' not found"}, 404)
            return self._send_json({"capabilities": fake.capabilities, "parameters": fake.parameters,
                                    "model_info": {"test.context_length": fake.max_context}})
        if self.path != "/api/chat":
            return self._send_json({"error": "not found"}, 404)
        fake.requests.append(payload)
        if payload["model"] not in fake.models:
            return self._send_json({"error": f"model '{payload['model']}' not found"}, 404)
        script = fake.scripts.pop(0) if fake.scripts else [("content", "ok")]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for kind, text in script:
            if kind == "error":
                self._line({"error": text})
                return
            if kind == "drop":
                return
            self._line({"message": {"role": "assistant", kind: text}, "done": False})
        self._line({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": fake.prompt_tokens, "eval_count": 7,
                    "eval_duration": 1_000_000_000, "total_duration": 2_000_000_000})

    def _line(self, obj):
        self.wfile.write(json.dumps(obj).encode() + b"\n")
        self.wfile.flush()


class FakeOllama:
    """Queue reply scripts in `scripts`; inspect what was sent in `requests`.

    A script is a list of (kind, text): kind is "thinking" or "content", or "error" to fail
    mid-stream, or "drop" to close the connection without finishing.
    """

    def __init__(self, models=("m1", "m2:latest"), capabilities=("completion", "thinking")):
        self.models = list(models)
        self.loaded = list(models)  # what /api/ps reports as resident in memory
        self.capabilities = list(capabilities)
        self.context_length = 1000
        self.max_context = 8192     # what /api/show gives as the most the model takes
        self.parameters = ""        # the Modelfile's PARAMETER lines, as /api/show gives them
        self.prompt_tokens = 11
        self.scripts = []
        self.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.fake = self
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, args=(0.01,),
                                        daemon=True)
        self._thread.start()

    def reply(self, *chunks, thinking=None):
        script = [("thinking", thinking)] if thinking else []
        self.scripts.append(script + [("content", c) for c in chunks])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
