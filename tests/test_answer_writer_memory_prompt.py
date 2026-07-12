from __future__ import annotations

import unittest

from z_apply_core.agents.prompts import load_prompt


class AnswerWriterMemoryPromptTests(unittest.TestCase):
    def test_answer_writer_retrieves_then_escalates_once(self) -> None:
        prompt = load_prompt("answer_writer.md")

        self.assertIn("lookup_candidate_memory", prompt)
        self.assertIn("call `ask_human` yourself exactly once", prompt)
        self.assertIn("automatically stored in\ncandidate memory", prompt)


if __name__ == "__main__":
    unittest.main()
