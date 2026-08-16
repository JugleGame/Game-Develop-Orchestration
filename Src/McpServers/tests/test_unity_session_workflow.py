"""Regression contract for the self-hosted Unity PlayMode workflow."""

from pathlib import Path


WORKFLOW = (Path(__file__).parents[3] / ".github/workflows/unity-session.yml").read_text(
    encoding="utf-8"
)


def test_unity_session_waits_for_results_and_rejects_invalid_runs():
    assert "-runTests -testPlatform PlayMode" in WORKFLOW
    assert "-quit" not in WORKFLOW
    assert "-testResults $results -logFile $log" in WORKFLOW
    assert "$unityExitCode = $LASTEXITCODE" in WORKFLOW
    assert "[int]$run.total -lt 1" in WORKFLOW
    assert "[int]$run.failed -gt 0" in WORKFLOW
    assert "$unityExitCode -ne 0" in WORKFLOW


def test_unity_session_always_uploads_results_and_log():
    upload = WORKFLOW.split("- name: Upload NUnit XML and Unity log", maxsplit=1)[1]

    assert "if: always()" in upload
    assert "unity-playmode-results.xml" in upload
    assert "unity-playmode.log" in upload
