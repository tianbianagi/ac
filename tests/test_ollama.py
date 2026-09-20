import os
import unittest
from unittest import mock

from ac import config
from ac.ollama import Client, OllamaError, resolve_model
from tests.fake_ollama import FakeOllama


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.addCleanup(self.fake.stop)
        self.client = Client(self.fake.host)

    def test_streams_thinking_content_and_stats(self):
        self.fake.reply("Hel", "lo", thinking="hmm")
        events = list(self.client.chat("m1", [{"role": "user", "content": "hi"}]))
        self.assertEqual(events[:3], [("thinking", "hmm"), ("content", "Hel"), ("content", "lo")])
        kind, stats = events[3]
        self.assertEqual(kind, "done")
        self.assertEqual((stats["prompt_eval_count"], stats["eval_count"]), (11, 7))

    def test_payload(self):
        self.client.chat("m1", [{"role": "user", "content": "hi"}])  # generator not started
        self.assertEqual(self.fake.requests, [])
        list(self.client.chat("m1", [{"role": "user", "content": "hi"}], think=False,
                              options={"temperature": 0.1}))
        req = self.fake.requests[0]
        self.assertEqual(req["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual((req["stream"], req["think"], req["options"]),
                         (True, False, {"temperature": 0.1}))

    def test_think_omitted_when_unset_or_unsupported(self):
        list(self.client.chat("m1", []))
        self.assertNotIn("think", self.fake.requests[0])
        self.assertNotIn("options", self.fake.requests[0])
        self.fake.capabilities = ["completion"]
        list(Client(self.fake.host).chat("m1", [], think=True))
        self.assertNotIn("think", self.fake.requests[1])

    def test_closing_generator_early_is_clean(self):
        self.fake.reply("a", "b", "c")
        stream = self.client.chat("m1", [])
        self.assertEqual(next(stream), ("content", "a"))
        stream.close()

    def test_unknown_model(self):
        with self.assertRaises(OllamaError) as cm:
            list(self.client.chat("nope", []))
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("not found", str(cm.exception))

    def test_error_mid_stream(self):
        self.fake.scripts.append([("content", "partial"), ("error", "out of memory")])
        stream = self.client.chat("m1", [])
        self.assertEqual(next(stream), ("content", "partial"))
        with self.assertRaisesRegex(OllamaError, "out of memory"):
            next(stream)

    def test_dropped_stream(self):
        self.fake.scripts.append([("content", "partial"), ("drop", "")])
        with self.assertRaisesRegex(OllamaError, "before the reply finished"):
            list(self.client.chat("m1", []))

    def test_connection_refused_is_friendly(self):
        with self.assertRaisesRegex(OllamaError, "Is it running"):
            Client("http://127.0.0.1:1").list_models()

    def test_context_length(self):
        self.assertEqual(self.client.context_length("m1"), 1000)
        self.assertIsNone(self.client.context_length("unloaded"))
        self.assertIsNone(Client("http://127.0.0.1:1").context_length("m1"))

    def test_resolve_model(self):
        self.assertEqual(resolve_model(self.client, "m1"), "m1")
        self.assertEqual(resolve_model(self.client, "m2"), "m2:latest")
        with self.assertRaisesRegex(OllamaError, "ambiguous"):
            resolve_model(self.client, "m")
        with self.assertRaisesRegex(OllamaError, "not installed. Installed: m1, m2:latest"):
            resolve_model(self.client, "zzz")


class HostTest(unittest.TestCase):
    def test_normalization(self):
        cases = {
            "": "http://127.0.0.1:11434",
            "0.0.0.0": "http://127.0.0.1:11434",
            "0.0.0.0:8080": "http://127.0.0.1:8080",
            "box.local:1234": "http://box.local:1234",
            "http://box.local/": "http://box.local:11434",
            "https://ollama.example.com": "https://ollama.example.com:443",
        }
        for raw, expected in cases.items():
            with mock.patch.dict(os.environ, {"OLLAMA_HOST": raw}):
                self.assertEqual(config.ollama_host(), expected, raw)


if __name__ == "__main__":
    unittest.main()
