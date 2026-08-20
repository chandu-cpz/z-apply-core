"""Specialist middleware wiring: every browser-wielding agent must share the
run's mutation lock, and no chain may carry a duplicate serializer."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from z_apply_core.agents.browser_mutation_serializer import SerializeBrowserMutationsMiddleware
from z_apply_core.agents.specialists import build_specialists


async def _specialist_chains(lock: asyncio.Lock) -> list[list[object]]:
    specs = await build_specialists(
        MagicMock(),  # type: ignore[arg-type]  # ModelProvider protocol; never leased here
        [],
        fallback_model=MagicMock(),
        sink=None,
        mutation_lock=lock,
    )
    return [spec["middleware"] for spec in specs]  # type: ignore[index]


class SpecialistMutationLockWiring(unittest.IsolatedAsyncioTestCase):
    async def test_every_specialist_serializes_through_the_shared_lock(self) -> None:
        lock = asyncio.Lock()
        chains = await _specialist_chains(lock)
        for role, chain in zip(
            (
                "AuthenticationSpecialist",
                "VisionSpecialist",
                "AnswerWriter",
                "SubmissionReviewer",
            ),
            chains,
            strict=True,
        ):
            with self.subTest(role=role):
                serializers = [
                    middleware
                    for middleware in chain
                    if isinstance(middleware, SerializeBrowserMutationsMiddleware)
                ]
                self.assertEqual(len(serializers), 1)
                self.assertIs(serializers[0]._mutation_lock, lock)


if __name__ == "__main__":
    unittest.main()
