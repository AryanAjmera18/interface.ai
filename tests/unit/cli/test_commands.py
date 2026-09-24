"""Keep reviewer-facing CLI examples bound to real command flags."""

from typer.testing import CliRunner

from cua.cli.__main__ import app


def test_replay_exposes_manual_and_scripted_handoff_modes() -> None:
    result = CliRunner().invoke(app, ["replay", "--help"])
    assert result.exit_code == 0
    assert "--escalate-on-failu" in result.output
    assert "--scripted-operator" in result.output
    assert "--operator-port" in result.output
    assert "--override" in result.output


def test_off_tree_compile_verify_approve_flags() -> None:
    runner = CliRunner()
    compile_help = runner.invoke(app, ["compile", "--help"])
    verify_help = runner.invoke(app, ["verify", "--help"])
    approve_help = runner.invoke(app, ["approve", "--help"])
    assert compile_help.exit_code == verify_help.exit_code == approve_help.exit_code == 0
    assert "--output" in compile_help.output
    assert "--run-dir" in verify_help.output
    assert "--run-dir" in approve_help.output
