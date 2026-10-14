# wallet-connect-lib

`ConnectTimeoutGuard` wraps a TCP connect attempt with a monotonic-clock deadline. If the handshake does not finish before the deadline, the socket is closed and `ConnectTimeoutError` is raised instead of the call hanging indefinitely.

```python
from wallet_connect_lib import ConnectTimeoutGuard, ConnectTimeoutError

guard = ConnectTimeoutGuard("127.0.0.1", 8080, timeout=5.0)
try:
    sock = guard.connect()
    # sock is a connected blocking socket; use it as usual
    sock.close()
except ConnectTimeoutError:
    # The TCP handshake did not complete within the budget.
    pass
except OSError:
    # A non-timeout socket error (e.g. connection refused) is raised as-is.
    pass
```

## Why this exists

Outbound `connect(2)` can stall for a long time under packet loss, black-holed routes, or an unresponsive peer. `socket.setdefaultdefaulttimeout` applies globally and changes the behaviour of every socket in the process, including ones you do not own; wrapping each connection in a deadline gives local, explicit control over the budget for a single handshake.

The trade-off is that this library only guards the TCP handshake, not the full TLS or application-layer handshake. If you need end-to-end deadlines, compose this with an explicit read/write timeout on the returned socket.

## Edge cases

- `timeout` must be strictly positive. `0` and negatives raise `ValueError` at construction. A guard that silently accepted zero would contradict the purpose of the library.
- The guard uses a non-blocking connect internally and polls with `select(2)`. On Unix loopback this typically returns immediately; on a remote peer the connect returns `EWOULDBLOCK` and the guard waits for writability or the deadline, whichever comes first.
- The socket is closed on every non-success path, including asynchronous errors surfaced via `SO_ERROR`. Callers never receive an `fd` they believe is valid.

## Running the tests

```
PYTHONPATH=src python -m unittest discover -s tests
```
