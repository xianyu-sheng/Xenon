"""One error envelope: kind + retryable decided once, at the boundary."""

from __future__ import annotations

import errno

from xenon.ledger.store import LedgerError
from xenon.utils.errors import envelope
from xenon.utils.llm_errors import ResponseTruncatedError


def test_a_timeout_is_a_retryable_network_error() -> None:
    problem = envelope(TimeoutError("read timed out"))

    assert problem.kind == "network"
    assert problem.retryable is True
    assert problem.message == "read timed out"


def test_a_refused_connection_is_retryable_and_a_plain_oserror_is_not() -> None:
    refused = OSError(errno.ECONNREFUSED, "refused")
    missing = OSError(errno.ENOENT, "no such file")

    assert envelope(refused).retryable is True
    assert envelope(missing).kind == "tool"
    assert envelope(missing).retryable is False


def test_permission_is_never_retryable() -> None:
    problem = envelope(PermissionError("denied"))

    assert problem.kind == "permission"
    assert problem.retryable is False


def test_a_truncated_model_answer_is_a_retryable_llm_error() -> None:
    problem = envelope(ResponseTruncatedError("cut off"))

    assert problem.kind == "llm"
    assert problem.retryable is True


def test_a_ledger_failure_is_an_integrity_error() -> None:
    assert envelope(LedgerError("seq gap")).kind == "integrity"


def test_bad_input_is_internal() -> None:
    assert envelope(ValueError("bad")).kind == "internal"


def test_an_exception_can_declare_its_own_kind() -> None:
    class ToolFailure(Exception):
        kind = "tool"
        retryable = True

    problem = envelope(ToolFailure("boom"))

    assert problem.kind == "tool"
    assert problem.retryable is True
    assert problem.code == "ToolFailure"


def test_to_dict_is_stable_and_omits_empty_fields() -> None:
    payload = envelope(PermissionError("denied")).to_dict()

    assert payload == {
        "kind": "permission",
        "code": "PermissionError",
        "message": "denied",
        "retryable": False,
        "cause": "builtins",
    }
