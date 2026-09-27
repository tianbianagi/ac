import json
import unittest

from ac.chat import build_messages
from ac.store import Attachment, Message, first_message_title as make_title


def msg(role, content, status="complete", thinking=None):
    return Message(id=0, session_id="s", seq=0, role=role, content=content, status=status,
                   thinking=thinking)


class BuildMessagesTest(unittest.TestCase):
    def test_no_system_message_when_nothing_attached(self):
        self.assertEqual(build_messages(None, [msg("user", "hi")]),
                         [{"role": "user", "content": "hi"}])

    def test_thinking_never_sent_and_errors_skipped(self):
        payload = build_messages("sys", [
            msg("user", "q1"), msg("assistant", "a1", thinking="secret reasoning"),
            msg("user", "q2"), msg("assistant", "broken", status="error"),
            msg("assistant", "", status="interrupted", thinking="only thought"),
            msg("user", "q3"), msg("assistant", "partial", status="interrupted")])
        self.assertEqual(payload, [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2\n\nq3"},  # neighbours merged after the skip
            {"role": "assistant", "content": "partial"}])
        self.assertNotIn("secret", json.dumps(payload))

    def test_attachments_in_payload(self):
        note = Attachment("/n.md", "text", content="persimmons")
        pic = Attachment("/p.png", "image", data=b"\x89PNG")
        first = msg("user", "what is this?")
        first.attachments = [note, pic]
        payload = build_messages(None, [first, msg("assistant", "a note"), msg("user", "and?")])
        self.assertEqual(payload[0], {
            "role": "user", "images": ["iVBORw=="],
            "content": '<file path="/n.md">\npersimmons\n</file>\n\n<image path="/p.png"/>'
                       "\n\nwhat is this?"})
        self.assertEqual(payload[2], {"role": "user", "content": "and?"})  # sent once, not per turn

        blind = build_messages(None, [first], vision=False)
        self.assertEqual(blind, [{"role": "user", "content":
                                  '<file path="/n.md">\npersimmons\n</file>\n\nwhat is this?'}])

        second = msg("user", "one more")
        second.attachments = [pic]
        merged = build_messages(None, [first, second])  # neighbours merge, images included
        self.assertEqual((len(merged), len(merged[0]["images"])), (1, 2))

    def test_make_title(self):
        self.assertEqual(make_title("  hello\n world "), "hello world")
        self.assertEqual(len(make_title("x" * 200)), 60)


if __name__ == "__main__":
    unittest.main()
