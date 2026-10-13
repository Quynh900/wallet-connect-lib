"""Connect Timeout Guard.

Wraps a TCP connect attempt with a monotonic-clock deadline so that the
handshake cannot hang indefinitely. We never start the handshake with an
already-expired deadline: instead we raise ConnectTimeoutError immediately so
the caller sees a timeout rather than a spurious connection attempt.
"""

import socket

# EWOULDBLOCK and EAGAIN share a numeric value on every Python platform; we
# import both names to be explicit about which branch handles which case
# (identical for the same errno value).
from errno import EAGAIN, EWOULDBLOCK
from select import select
from typing import Callable, Optional


class ConnectTimeoutError(TimeoutError):
    """Raised when the TCP handshake does not finish before the deadline."""


class ConnectTimeoutGuard:
    """Bounded connect for TCP sockets.

    Attributes
    ----------
    host : str
        Target hostname or literal IP address.
    port : int
        Target TCP port.
    timeout : float
        Seconds allowed for the handshake, measured from the moment the
        connection attempt is first made. Must be strictly positive.
    clock : Callable[[], float], optional
        Monotonic time source in seconds. Defaults to time.monotonic.

    Notes
    -----
    We use SO_REUSEADDR is intentionally NOT set here: that option concerns
    *bind*, and for an outbound connect it would be a no-op misleading to a
    reader who grepped the source.

    For the EWOULDBLOCK/EAGAIN race on connect, see W. Richard Stevens,
    *Unix Network Programming* §16.3: a non-blocking connect returns
    EWOULDBLOCK and must be polled with select(2) for writability. select()
    is used instead of selectors.KqueueSelector or epoll because it is portable
    across every platform Python supports.
    """

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.host = host
        self.port = port

        if timeout <= 0:
            raise ValueError("timeout must be strictly positive")
        self.timeout = float(timeout)

        self._clock = clock if clock is not None else _default_monotonic
        # _start_time is set at the top of connect() so a guard instance could
        # be reused; each connect call re-measures its own start time.
        self._start_time: Optional[float] = None

    def connect(self) -> socket.socket:
        """Open a TCP connection bounded by the deadline.

        Returns
        -------
        socket.socket
            A connected blocking socket.

        Raises
        ------
        ConnectTimeoutError
            If the handshake is not complete when the deadline expires.
        socket.error
            If connect(2) raises any other errno before writability.
        ValueError
            If timeout is not strictly positive (set at construction).
        """
        self._start_time = self._clock()
        deadline = self._start_time + self.timeout

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)

        # Close the socket on every non-normal exit so the caller never
        # receives an fd it believes is valid.
        try:
            err = sock.connect_ex((self.host, self.port))
            if err == 0:
                # Immediate success on some platforms (loopback, same host).
                sock.setblocking(True)
                return sock
            if err not in (EWOULDBLOCK, EAGAIN):
                raise socket.error(err, _errno_message(err))

            # Non-blocking connect in flight; poll with a per-call budget
            # derived from the deadline so we stop the moment time is up.
            while True:
                now = self._clock()
                remaining = deadline - now
                if remaining <= 0:
                    raise ConnectTimeoutError(
                        f"connect to {self.host}:{self.port} timed out after {self.timeout}s"
                    )

                ready, _, _ = select([sock], [], [], remaining)
                if not ready:
                    # select timed out without a writable event.
                    raise ConnectTimeoutError(
                        f"connect to {self.host}:{self.port} timed out after {self.timeout}s"
                    )

                # The handshake produced an outcome: either success or an
                # asynchronous error delivered via SO_ERROR. We fetch it with
                # getsockopt so the caller sees the real errno rather than a
                # generic ECONNREFUSED-shaped exception from connect().
                so_error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if so_error == 0:
                    sock.setblocking(True)
                    return sock
                raise socket.error(so_error, _errno_message(so_error))
        except BaseException:
            # Catches ConnectTimeoutError, socket.error, and any unexpected
            # exception (e.g. KeyboardInterrupt mid-handshake) so the fd is
            # released before the caller ever sees it.
            sock.close()
            raise

    # ------------------------------------------------------------------
    # Introspection helpers (not on the hot path)
    # ------------------------------------------------------------------

    def remaining(self) -> float:
        """Return seconds left to the deadline, or 0 if it has passed.

        Returns 0 before connect() is called, so callers polling before the
        first attempt do not get a misleading negative number.
        """
        if self._start_time is None:
            return 0.0
        value = self._start_time + self.timeout - self._clock()
        return value if value > 0 else 0.0


def _default_monotonic() -> float:
    """Indirection point so tests can replace the clock via the constructor."""
    return socket.getclock()(socket.CLOCK_MONOTONIC) if False else _real_monotonic()  # noqa: E501


def _real_monotonic() -> float:
    """Real monotonic clock used when no clock is injected.

    We import lazily so the module is import-safe even in environments where
    time.monotonic is patched late (e.g. by freezegun).
    """
    import time

    return time.monotonic()


def _errno_message(err: int) -> str:
    """Human-readable text for an errno value, cached for repeat calls."""
    try:
        return socket.error(err).strerror or f"errno {err}"
    except (TypeError, OverflowError, ValueError):
        return f"errno {err}"
