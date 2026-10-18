"""Executor adapter for the synchronous Carlink client."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from pycarlink.PyCarLink import MalformedResponseError, NoAssetsError
from requests.exceptions import HTTPError

from .client import DiagnosticCarlink as PyCarlink


def _error(key: str) -> HomeAssistantError:
    return HomeAssistantError(translation_domain="carlink", translation_key=key)


def _unauthorized(error: Exception) -> bool:
    return (
        isinstance(error, HTTPError)
        and error.response is not None
        and error.response.status_code == 401
    )


def canonical_identity(client: PyCarlink) -> tuple[str, str]:
    """Canonicalize library-validated identifiers, without normalizing strings."""
    return str(client.account_id), str(client.asset_id)


async def async_create_client(
    hass: HomeAssistant, email: str, password: str
) -> PyCarlink:
    """Construct including network initialization off-loop, with default timeouts."""
    try:
        return await hass.async_add_executor_job(PyCarlink, email, password)
    except Exception as error:
        if _unauthorized(error):
            raise ConfigEntryAuthFailed(
                translation_domain="carlink", translation_key="auth_failed"
            ) from None
        if isinstance(error, NoAssetsError):
            raise _error("no_assets") from None
        if isinstance(error, MalformedResponseError):
            raise _error("invalid_response") from None
        raise _error("cannot_connect") from None


class CarlinkRuntime:
    """Own one initialized client."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PyCarlink,
        *,
        on_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._client = client
        self._identity = canonical_identity(client)
        self._on_auth_failed = on_auth_failed
        self._listeners: set[Callable[[], None]] = set()
        self._auth_failed = False
        self._closing = False
        self._busy = False
        self._task: asyncio.Task[HomeAssistantError | None] | None = None
        self.platforms: set[Platform] = set()
        self.cleanup_task: asyncio.Task[bool] | None = None

    @property
    def identity(self) -> tuple[str, str]:
        """Return the account and first asset bound at construction."""
        return self._identity

    @property
    def available(self) -> bool:
        """Indicate readiness, not vehicle reachability."""
        return not self._auth_failed and not self._closing

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a synchronous loop callback; return idempotent unsubscribe."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _notify_listeners(self) -> None:
        """Notify cached readiness changes without compromising command safety."""
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                # A broken entity must not block other entities or reauth/drain.
                pass

    async def async_command(self, action: str) -> None:
        """Submit one explicitly mapped action."""
        methods = {
            "lock": self._client.lock_doors,
            "unlock": self._client.unlock_doors,
            "start": self._client.start_engine,
            "stop": self._client.stop_engine,
        }
        if action not in methods:
            raise _error("invalid_action")
        if self._closing:
            raise _error("closed")
        if self._auth_failed:
            raise _error("auth_failed")
        if self._busy:
            raise _error("busy")
        # No await before reservation; concurrent presses never queue work.
        self._busy = True
        task = self._hass.async_create_task(
            self._async_run(methods[action]), "carlink command", eager_start=False
        )
        self._task = task
        task.add_done_callback(self._consume_result)
        error = await asyncio.shield(task)
        if error is not None:
            raise error from None

    def _consume_result(self, task: asyncio.Task[HomeAssistantError | None]) -> None:
        """Release only this reservation, including cancellation before entry."""
        if self._task is task:
            self._busy = False
            self._task = None
        if not task.cancelled():
            task.exception()

    async def _async_run(self, method: Callable[[], str]) -> HomeAssistantError | None:
        """Keep ownership until the executor worker has really completed."""
        try:
            await self._async_execute(method)
        except HomeAssistantError as error:
            # Python 3.14 shield logs detached exceptions even if retrieved.
            # Carry sanitized failures as values until a live caller receives them.
            return error

    async def _async_execute(self, method: Callable[[], str]) -> None:
        try:
            # HA tracks (and can directly cancel) the executor's asyncio Future.
            # A separate concurrent Future arbitrates start vs cancellation and
            # retains the physical result even when HA discards its wrapper.
            outcome: Future[str | Exception] = Future()
            worker = asyncio.wrap_future(outcome)
            tracked = self._hass.async_add_executor_job(
                self._call_worker, method, outcome
            )

            def executor_done(future: asyncio.Future[None]) -> None:
                if future.cancelled():
                    # Succeeds only before our wrapper claims the operation.
                    # A queued work item may never run; do not wait for it.
                    outcome.cancel()

            tracked.add_done_callback(executor_done)
            # Internal owner cancellation must still drain a running operation.
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(worker)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if worker.cancelled():
                        raise
            if isinstance(result, Exception):
                raise result
            if cancelled:
                raise asyncio.CancelledError

        except Exception as error:
            if _unauthorized(error):
                self._auth_failed = True
                self._notify_listeners()
                if self._on_auth_failed is not None:
                    try:
                        self._on_auth_failed()
                    except Exception:
                        # Owner failures must not expose raw data or enable replay.
                        pass
                raise _error("auth_failed") from None
            raise _error("unknown_outcome") from None
        if result == "Command Failed":
            raise _error("command_failed")
        if result != "Success":
            raise _error("unknown_outcome")

    @staticmethod
    def _call_worker(
        method: Callable[[], str], outcome: Future[str | Exception]
    ) -> None:
        """Claim execution atomically; retain raw failures only as result values."""
        if not outcome.set_running_or_notify_cancel():
            return
        try:
            result = method()
        except Exception as error:
            outcome.set_result(error)
        except BaseException:
            # Even an abnormal worker exit must settle the drain signal without
            # putting a raw exception on HA's separately tracked Future.
            outcome.set_result(RuntimeError())
        else:
            outcome.set_result(result)

    async def async_close(self) -> None:
        """Permanently reject new work and drain; cancellation leaves us closing.

        The entry owner must retain this runtime until this await completes,
        including retrying it after an interrupted unload, before replacement.
        """
        if not self._closing:
            self._closing = True
            self._notify_listeners()
        task = self._task
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A cancelled, fully drained owner is not an interrupted close.
                # Preserve cancellation directed at this close caller itself.
                current = asyncio.current_task()
                if not task.cancelled() or (
                    current is not None and current.cancelling()
                ):
                    raise
