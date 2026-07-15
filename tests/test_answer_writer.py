from __future__ import annotations

import asyncio
import unittest

from z_apply_core.agents.specialists.answer_writer import build_resume_evidence_tool


class ResumeEvidenceToolTests(unittest.TestCase):
    def test_returns_prepared_resume_without_path_input(self) -> None:
        resume = "# Candidate\n\nFamily name: V"
        resume_tool = build_resume_evidence_tool(resume)

        result = asyncio.run(resume_tool.ainvoke({}))

        self.assertEqual(result, resume)
        self.assertEqual(resume_tool.name, "read_candidate_resume")


if __name__ == "__main__":
    unittest.main()
