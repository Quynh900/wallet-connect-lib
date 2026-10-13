# wallet-connect-lib
# Connect Timeout Guard: wrap a socket connection attempt with a monotonic deadline.

from wallet_connect_lib.core import ConnectTimeoutGuard, ConnectTimeoutError

__all__ = ["ConnectTimeoutGuard", "ConnectTimeoutError"]
