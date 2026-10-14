import socket
import time
import unittest

from wallet_connect_lib import ConnectTimeoutError, ConnectTimeoutGuard


class _FakeClock:
    """Deterministic monotonic clock for tests.

    Tests cannot rely on wall-clock timing, so each guard is constructed with
    one of these. The clock only advances when a test calls advance();
    nothing else mutates it.
    """

    def __init__(self):
        self._t = 0.0

    def __call__(self):
        return self._t

    def advance(self, dt):
        self._t += float(dt)


from errno import EAGAIN, EWOULDBLOCK


class _MockSocket:
    """Stand-in for socket.socket whose connect behaviour is scripted.

    Supports exactly the calls the guard makes: connect_ex, getsockopt,
    setblocking, close, fileno. We do not model read/write because the guard
    never touches them.
    """

    _next_fd = 3

    def __init__(self, af, type, **kw):
        self.family = af
        self.type = type
        self.fileno_ = _MockSocket._next_fd
        _MockSocket._next_fd += 1
        self.blocking = True
        self.closed = False
        self.connect_calls = []
        self.connect_return = EWOULDBLOCK
        self.so_error = 0
        self.last_select_sock = None

    def connect_ex(self, addr):
        self.connect_calls.append(addr)
        return self.connect_return

    def getsockopt(self, level, opt):
        return self.so_error

    def setblocking(self, flag):
        self.blocking = bool(flag)

    def fileno(self):
        return self.fileno_

    def close(self):
        self.closed = True


class _MockSocketModule:
    """Minimal socket module replacement for tests."""
    AF_INET = socket.AF_INET
    SOCK_STREAM = socket.SOCK_STREAM
    SOL_SOCKET = socket.SOL_SOCKET
    SO_ERROR = socket.SO_ERROR
    socket = _MockSocket

    @staticmethod
    def error(*args, **kw):
        return socket.error(*args, **kw)


_HANDLERS = []


def _socket_factory(af, type, **kw):
    handler = _HANDLERS[-1]
    return handler(af, type, **kw)


class _Monkey:
    def __init__(self):
        self._undo = []
    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)
    def addCleanup(self, fn, *a, **kw):
        self._undo.append((lambda: fn(*a, **kw)))
    def undo(self):
        for step in reversed(self._undo):
            if isinstance(step, tuple):
                obj, name, value = step
                setattr(obj, name, value)
            else:
                step()


def _install_mock_socket(monkey, handler=None):
    """Replace core.socket with _MockSocketModule.

    If handler is provided, it is pushed onto _HANDLERS so the next call to
    _socket_factory (set as _MockSocketModule.socket) will call it.
    """
    import wallet_connect_lib.core as core
    saved = core.socket
    mock_mod = _MockSocketModule()
    monkey.setattr(core, "socket", mock_mod)
    if handler is not None:
        _HANDLERS.append(handler)
        monkey.setattr(mock_mod, "socket", _socket_factory)


class InitTests(unittest.TestCase):

    def test_positive_timeout_required(self):
        with self.assertRaises(ValueError):
            ConnectTimeoutGuard("127.0.0.1", 1, 0.0)

    def test_zero_timeout_rejected(self):
        with self.assertRaises(ValueError):
            ConnectTimeoutGuard("127.0.0.1", 1, 0)

    def test_negative_timeout_rejected(self):
        with self.assertRaises(ValueError):
            ConnectTimeoutGuard("127.0.0.1", 1, -1.0)

    def test_attributes(self):
        g = ConnectTimeoutGuard("127.0.0.1", 8080, 5.0)
        self.assertEqual(g.host, "127.0.0.1")
        self.assertEqual(g.port, 8080)
        self.assertEqual(g.timeout, 5.0)


class RemainingTests(unittest.TestCase):

    def test_remaining_before_connect(self):
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 1, 2.0, clock=clk)
        self.assertEqual(g.remaining(), 0.0)

    def test_remaining_drops_after_advance(self):
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 1, 4.0, clock=clk)
        # Simulate having called connect(): start_time would be captured.
        g._start_time = clk()
        clk.advance(1.0)
        self.assertEqual(g.remaining(), 3.0)

    def test_remaining_clamped_to_zero(self):
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 1, 1.0, clock=clk)
        g._start_time = clk()
        clk.advance(5.0)
        self.assertEqual(g.remaining(), 0.0)


class ImmediateSuccessTests(unittest.TestCase):

    def setUp(self):
        self._monkey = _Monkey()
        _install_mock_socket(self._monkey)
        self.addCleanup(self._monkey.undo)

    def test_immediate_connect_success(self):
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 8080, 2.0, clock=clk)
        # Patch the just-allocated socket to report immediate success.
        def make_socket(af, type, **kw):
            s = _MockSocket(af, type, **kw)
            s.connect_return = 0
            s.so_error = 0
            return s
        import wallet_connect_lib.core as core
        self._monkey.setattr(core.socket, "socket", make_socket)

        sock = g.connect()
        self.assertIsInstance(sock, _MockSocket)
        self.assertTrue(sock.blocking)
        self.assertFalse(sock.closed)
        self.assertEqual(sock.connect_calls, [("127.0.0.1", 8080)])
        sock.close()


class AsyncSuccessTests(unittest.TestCase):

    def setUp(self):
        self._monkey = _Monkey()
        _install_mock_socket(self._monkey)
        self.addCleanup(self._monkey.undo)

    def test_async_connect_success_after_one_select(self):
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 8080, 2.0, clock=clk)

        call_count = {"n": 0}
        def make_socket(af, type, **kw):
            s = _MockSocket(af, type, **kw)
            s.connect_return = EWOULDBLOCK
            s.so_error = 0
            return s
        import wallet_connect_lib.core as core
        self._monkey.setattr(core.socket, "socket", make_socket)

        saved_select = core.select
        def fake_select(r, w, x, timeout):
            call_count["n"] += 1
            return ([r[0]], [], [])
        self._monkey.setattr(core, "select", fake_select)

        sock = g.connect()
        self.assertEqual(call_count["n"], 1)
        self.assertTrue(sock.blocking)
        self.assertFalse(sock.closed)
        sock.close()

    def test_async_connect_so_error_raised(self):
        from errno import ECONNREFUSED
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 8080, 2.0, clock=clk)

        def make_socket(af, type, **kw):
            s = _MockSocket(af, type, **kw)
            s.connect_return = EWOULDBLOCK
            s.so_error = ECONNREFUSED
            return s
        import wallet_connect_lib.core as core
        self._monkey.setattr(core.socket, "socket", make_socket)

        def fake_select(r, w, x, timeout):
            return ([r[0]], [], [])
        self._monkey.setattr(core, "select", fake_select)

        with self.assertRaises(socket.error) as ctx:
            g.connect()
        self.assertEqual(ctx.exception.errno, ECONNREFUSED)


class TimeoutTests(unittest.TestCase):

    def setUp(self):
        self._monkey = _Monkey()
        _install_mock_socket(self._monkey)
        self.addCleanup(self._monkey.undo)

    def test_timeout_select_empty_raises(self):
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 8080, 1.0, clock=clk)

        def make_socket(af, type, **kw):
            s = _MockSocket(af, type, **kw)
            s.connect_return = EWOULDBLOCK
            return s
        import wallet_connect_lib.core as core
        self._monkey.setattr(core.socket, "socket", make_socket)

        def fake_select(r, w, x, timeout):
            clk.advance(timeout + 1.0)
            return ([], [], [])
        self._monkey.setattr(core, "select", fake_select)

        with self.assertRaises(ConnectTimeoutError):
            g.connect()

    def test_socket_closed_on_timeout(self):
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 8080, 1.0, clock=clk)

        created = {"s": None}
        def make_socket(af, type, **kw):
            s = _MockSocket(af, type, **kw)
            s.connect_return = EWOULDBLOCK
            created["s"] = s
            return s
        import wallet_connect_lib.core as core
        self._monkey.setattr(core.socket, "socket", make_socket)

        def fake_select(r, w, x, timeout):
            clk.advance(timeout + 1.0)
            return ([], [], [])
        self._monkey.setattr(core, "select", fake_select)

        with self.assertRaises(ConnectTimeoutError):
            g.connect()
        self.assertTrue(created["s"].closed)

    def test_socket_closed_on_so_error(self):
        from errno import ECONNREFUSED
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 8080, 1.0, clock=clk)

        created = {"s": None}
        def make_socket(af, type, **kw):
            s = _MockSocket(af, type, **kw)
            s.connect_return = EWOULDBLOCK
            s.so_error = ECONNREFUSED
            created["s"] = s
            return s
        import wallet_connect_lib.core as core
        self._monkey.setattr(core.socket, "socket", make_socket)

        def fake_select(r, w, x, timeout):
            return ([r[0]], [], [])
        self._monkey.setattr(core, "select", fake_select)

        with self.assertRaises(socket.error):
            g.connect()
        self.assertTrue(created["s"].closed)

    def test_pre_connect_error_closed(self):
        # connect_ex returns an errno other than EWOULDBLOCK/EAGAIN.
        from errno import EACCES
        clk = _FakeClock()
        g = ConnectTimeoutGuard("127.0.0.1", 8080, 1.0, clock=clk)

        created = {"s": None}
        def make_socket(af, type, **kw):
            s = _MockSocket(af, type, **kw)
            s.connect_return = EACCES
            created["s"] = s
            return s
        import wallet_connect_lib.core as core
        self._monkey.setattr(core.socket, "socket", make_socket)

        with self.assertRaises(socket.error):
            g.connect()
        self.assertTrue(created["s"].closed)


if __name__ == "__main__":
    unittest.main()
