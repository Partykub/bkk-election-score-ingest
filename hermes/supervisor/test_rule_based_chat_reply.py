import unittest

from hermes.supervisor.intake_server import build_rule_based_chat_reply


class RuleBasedChatReplyTests(unittest.TestCase):
    def test_identity_question_returns_identity_reply(self) -> None:
        reply = build_rule_based_chat_reply("\u0e19\u0e32\u0e22\u0e0a\u0e37\u0e48\u0e2d\u0e2d\u0e30\u0e44\u0e23")
        self.assertIsNotNone(reply)
        self.assertIn("BKK Election", reply)

    def test_getting_started_question_returns_starting_reply(self) -> None:
        reply = build_rule_based_chat_reply("\u0e40\u0e23\u0e34\u0e48\u0e21\u0e08\u0e32\u0e01\u0e2a\u0e48\u0e07\u0e23\u0e39\u0e1b\u0e40\u0e2b\u0e23\u0e2d")
        self.assertIsNotNone(reply)
        self.assertIn("\u0e40\u0e23\u0e34\u0e48\u0e21\u0e08\u0e32\u0e01\u0e2a\u0e48\u0e07\u0e23\u0e39\u0e1b", reply)


if __name__ == "__main__":
    unittest.main()
