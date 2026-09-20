"""Minimal Ollama HTTP client (stdlib only)."""

import http.client
import json
import sys
import urllib.error
import urllib.request

from . import config


class OllamaError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, host=None):
        self.host = host or config.ollama_host()
        self._capabilities = {}

    def _open(self, path, payload=None, timeout=10):
        data = json.dumps(payload).encode() if payload is not None else None
        if config.debug() and payload is not None:
            print(f"[ac] POST {path} {json.dumps(payload, indent=2)}", file=sys.stderr)
        req = urllib.request.Request(self.host + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            with e:
                body = e.read().decode(errors="replace")
            try:
                message = json.loads(body)["error"]
            except (ValueError, KeyError, TypeError):
                message = body.strip() or e.reason
            raise OllamaError(message, status=e.code) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise OllamaError(
                f"can't reach Ollama at {self.host} ({reason}). Is it running? Try `ollama serve`."
            ) from None

    def _json(self, path, payload=None):
        with self._open(path, payload) as resp:
            return json.load(resp)

    def list_models(self):
        return self._json("/api/tags").get("models", [])

    def show(self, model):
        return self._json("/api/show", {"model": model})

    def ps(self):
        return self._json("/api/ps").get("models", [])

    def supports(self, model, capability):
        if model not in self._capabilities:
            try:
                self._capabilities[model] = self.show(model).get("capabilities") or []
            except OllamaError:
                return False
        return capability in self._capabilities[model]

    def context_length(self, model):
        """Effective context window of a loaded model, or None if it can't be determined."""
        try:
            for m in self.ps():
                if model in (m.get("name"), m.get("model")):
                    return m.get("context_length")
        except OllamaError:
            pass
        return None

    def chat(self, model, messages, *, think=None, options=None, keep_alive=None):
        """Stream a reply as ("thinking", text), ("content", text) and a final ("done", stats).

        Closing the generator drops the connection, which makes Ollama stop generating.
        """
        payload = {"model": model, "messages": messages, "stream": True}
        if think is not None and self.supports(model, "thinking"):
            payload["think"] = think
        if options:
            payload["options"] = options
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        # No timeout: loading a large model can take minutes before the first byte.
        resp = self._open("/api/chat", payload, timeout=None)
        try:
            for raw in resp:
                line = raw.strip()
                if not line:
                    continue
                data = json.loads(line)
                if "error" in data:
                    raise OllamaError(data["error"])
                message = data.get("message") or {}
                if message.get("thinking"):
                    yield "thinking", message["thinking"]
                if message.get("content"):
                    yield "content", message["content"]
                if data.get("done"):
                    yield "done", data
                    return
            raise OllamaError("Ollama closed the stream before the reply finished")
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise OllamaError(f"stream from Ollama failed: {e}") from None
        finally:
            resp.close()


def resolve_model(client, name):
    """Match a user-typed model name against installed models (exact, :latest, unique prefix)."""
    names = [m["name"] for m in client.list_models()]
    if name in names:
        return name
    if f"{name}:latest" in names:
        return f"{name}:latest"
    matches = [n for n in names if n.startswith(name)]
    if len(matches) == 1:
        return matches[0]
    installed = ", ".join(names) or "none (try `ollama pull <model>`)"
    problem = "is ambiguous" if matches else "is not installed"
    raise OllamaError(f"model '{name}' {problem}. Installed: {installed}")
