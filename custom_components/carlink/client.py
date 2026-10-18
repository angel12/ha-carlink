"""Privacy-safe observation of the pinned client's command-status boundary."""

import logging

from pycarlink import PyCarlink

_LOGGER = logging.getLogger(__name__)


def _status_description(status: object) -> str:
    """Emit only fixed labels; never stringify arbitrary values or type names."""
    if type(status) is not str:
        return {
            type(None): "unknown:null",
            bool: "unknown:boolean",
            int: "unknown:number",
            float: "unknown:number",
            dict: "unknown:object",
            list: "unknown:array",
        }.get(type(status), "unknown:other")
    length = len(status)
    if length > 128:
        return "unknown:string:over-128"
    # Normalization is diagnostic only, bounded, and never fed back to polling.
    stripped = status.strip()
    for token in ("Success", "Pending", "Processing"):
        if status == token:
            return "exact:" + token
        if stripped == token:
            return "whitespace:" + token
        if stripped.lower() == token.lower():
            prefix = "case:" if stripped == status else "case+whitespace:"
            return prefix + token
    if not length:
        return "unknown:string:empty"
    return "unknown:string:1-32" if length <= 32 else "unknown:string:33-128"


class DiagnosticCarlink(PyCarlink):
    """Retain upstream commands/polling; observe only their status reads."""

    def command_status(self, command_id):
        """Return the upstream value unchanged, without extra requests."""
        try:
            status = super().command_status(command_id)
        except Exception:
            try:
                _LOGGER.debug("Command status: read-error")
            except Exception:
                pass  # Diagnostics must not replace the original exception.
            raise
        try:
            _LOGGER.debug("Command status: %s", _status_description(status))
        except Exception:
            pass  # Diagnostics must not turn a completed read into a failure.
        return status
