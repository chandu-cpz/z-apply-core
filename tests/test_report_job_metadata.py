from __future__ import annotations

import pytest

from z_apply_core.agents.orchestrator import make_report_job_metadata
from z_apply_core.agents.prompts import ORCHESTRATOR_PROMPT, load_prompt
from z_apply_core.integrations import CoreIntegrationConfig, StartRunRequest, ZApplyCore
from z_apply_core.integrations.service import (
    _metadata_reporter,
    _role_company_from_title,
    _Run,
)
from z_apply_core.text_utils import JOB_METADATA_MAX_LENGTH


class _FakeSession:
    def __init__(self, title: str) -> None:
        self._title = title

    async def page_title(self) -> str:
        return self._title


def _run() -> _Run:
    return _Run(StartRunRequest(job_url="https://example.test/job"), "run-1")


@pytest.mark.asyncio
async def test_report_job_metadata_updates_the_run_view() -> None:
    run = _run()
    tool = make_report_job_metadata(_metadata_reporter(run))

    result = await tool.ainvoke(
        {"company": "  Acme Corp ", "role": " Senior Engineer ", "location": " Berlin "}
    )

    assert run.view.company == "Acme Corp"
    assert run.view.role == "Senior Engineer"
    assert "company=Acme Corp" in result
    assert "role=Senior Engineer" in result
    assert "(location accepted, not persisted yet)" in result


@pytest.mark.asyncio
async def test_report_job_metadata_without_reporter_still_succeeds() -> None:
    tool = make_report_job_metadata(None)

    result = await tool.ainvoke({"company": "Acme", "role": "Engineer"})

    assert "unavailable" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"company": "   ", "role": "Engineer"},
        {"company": "Acme", "role": ""},
        {"company": "", "role": ""},
    ],
)
async def test_report_job_metadata_returns_guidance_for_blank_values(
    payload: dict[str, str],
) -> None:
    run = _run()
    tool = make_report_job_metadata(_metadata_reporter(run))

    result = await tool.ainvoke(payload)

    assert result == (
        "report_job_metadata rejected: company and role are required "
        "non-empty strings read from the page. Re-call with real values."
    )
    assert run.view.company is None
    assert run.view.role is None


@pytest.mark.asyncio
async def test_report_job_metadata_caps_overlong_values() -> None:
    run = _run()
    tool = make_report_job_metadata(_metadata_reporter(run))
    long_company = "A" * (JOB_METADATA_MAX_LENGTH + 50)

    await tool.ainvoke({"company": long_company, "role": "Engineer"})

    assert run.view.company == "A" * JOB_METADATA_MAX_LENGTH


@pytest.mark.asyncio
async def test_prompt_instructs_metadata_before_any_filling() -> None:
    prompt = load_prompt(ORCHESTRATOR_PROMPT)

    assert "report_job_metadata" in prompt
    workflow = prompt.split("<application_workflow>", 1)[1]
    assert workflow.index("report_job_metadata") < workflow.index("Autofill this page")


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Senior Engineer | Acme Corp", ("Senior Engineer", "Acme Corp")),
        ("Senior Engineer – Acme Corp", ("Senior Engineer", "Acme Corp")),
        ("Senior Engineer — Acme Corp", ("Senior Engineer", "Acme Corp")),
        ("Senior Engineer - Acme Corp", ("Senior Engineer", "Acme Corp")),
        ("Full-Stack Engineer | Acme Corp", ("Full-Stack Engineer", "Acme Corp")),
        ("Just A Page Title", None),
        ("Role |   ", None),
    ],
)
def test_role_company_from_title(title: str, expected: tuple[str, str] | None) -> None:
    assert _role_company_from_title(title) == expected


@pytest.mark.asyncio
async def test_title_capture_seeds_view_before_the_agent_acts() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _run()

    await core._capture_title_metadata(run, _FakeSession("Backend Engineer | Globex"))

    assert run.view.company == "Globex"
    assert run.view.role == "Backend Engineer"


@pytest.mark.asyncio
async def test_title_capture_never_overwrites_tool_set_values() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _run()
    reporter = _metadata_reporter(run)
    reporter("Initech", "Platform Engineer")

    await core._capture_title_metadata(run, _FakeSession("Backend Engineer | Globex"))

    assert run.view.company == "Initech"
    assert run.view.role == "Platform Engineer"


@pytest.mark.asyncio
async def test_title_capture_survives_session_failures() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _run()

    class _BrokenSession:
        async def page_title(self) -> str:
            raise RuntimeError("browser gone")

    await core._capture_title_metadata(run, _BrokenSession())  # type: ignore[arg-type]

    assert run.view.company is None
    assert run.view.role is None
