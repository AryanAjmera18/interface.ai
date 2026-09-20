"""Statically check exhaustive caller branching; forbid live-service imports."""

from typing import assert_never

from cua.domain.results import BusinessOutcome, HardFailure, ReplayResult, Success


def branch(result: ReplayResult) -> str:
    match result:
        case Success():
            return "success"
        case BusinessOutcome():
            return "business"
        case HardFailure():
            return "hard"
        case unreachable:
            assert_never(unreachable)
