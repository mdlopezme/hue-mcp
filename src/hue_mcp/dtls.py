"""A DTLS 1.2 client with a pre-shared key, over the system's OpenSSL, for Hue streaming.

Python's ssl module has no DTLS, so this calls libssl through ctypes. It only ever sends:
the bridge never answers a streamed frame.
"""

import ctypes
import ctypes.util
import socket
from ctypes import c_char_p, c_int, c_long, c_size_t, c_uint, c_ulong, c_void_p

import anyio

from hue_mcp.errors import HueError

CIPHER = b"PSK-AES128-GCM-SHA256"  # The only suite the bridge accepts.
DTLS1_2_VERSION = 0xFEFD
HANDSHAKE_TIMEOUT_S = 5.0

# OpenSSL macros, which are calls to SSL_CTX_ctrl, SSL_ctrl and BIO_ctrl.
SSL_CTRL_SET_MIN_PROTO_VERSION = 123
SSL_CTRL_SET_MAX_PROTO_VERSION = 124
DTLS_CTRL_GET_TIMEOUT = 73
DTLS_CTRL_HANDLE_TIMEOUT = 74
BIO_CTRL_DGRAM_SET_CONNECTED = 32
BIO_NOCLOSE = 0

SSL_ERROR_WANT_READ = 2
SSL_ERROR_WANT_WRITE = 3
SSL_ERROR_SYSCALL = 5  # A socket error, such as nothing listening on the port.

PskCallback = ctypes.CFUNCTYPE(c_uint, c_void_p, c_char_p, c_void_p, c_uint, c_void_p, c_uint)


class _Timeval(ctypes.Structure):
    _fields_ = (("tv_sec", c_long), ("tv_usec", c_long))


class DtlsError(HueError):
    pass


def _load(name: str, fallback: str) -> ctypes.CDLL:
    try:
        return ctypes.CDLL(ctypes.util.find_library(name) or fallback)
    except OSError as error:
        raise DtlsError(f"Light shows need OpenSSL's {fallback}, which isn't here.") from error


def _declare(lib: ctypes.CDLL, name: str, restype: object, *argtypes: object) -> None:
    function = getattr(lib, name)
    function.restype = restype
    function.argtypes = argtypes


class _OpenSSL:
    """The libssl and libcrypto calls used, each with its C signature: ctypes' default return
    type (int) would truncate pointers."""

    def __init__(self) -> None:
        self.ssl = _load("ssl", "libssl.so.3")
        self.crypto = _load("crypto", "libcrypto.so.3")
        ssl, crypto = self.ssl, self.crypto
        _declare(ssl, "DTLS_client_method", c_void_p)
        _declare(ssl, "SSL_CTX_new", c_void_p, c_void_p)
        _declare(ssl, "SSL_CTX_free", None, c_void_p)
        _declare(ssl, "SSL_CTX_ctrl", c_long, c_void_p, c_int, c_long, c_void_p)
        _declare(ssl, "SSL_CTX_set_cipher_list", c_int, c_void_p, c_char_p)
        _declare(ssl, "SSL_CTX_set_psk_client_callback", None, c_void_p, PskCallback)
        _declare(ssl, "SSL_new", c_void_p, c_void_p)
        _declare(ssl, "SSL_free", None, c_void_p)
        _declare(ssl, "SSL_set_bio", None, c_void_p, c_void_p, c_void_p)
        _declare(ssl, "SSL_ctrl", c_long, c_void_p, c_int, c_long, c_void_p)
        _declare(ssl, "SSL_connect", c_int, c_void_p)
        _declare(ssl, "SSL_write", c_int, c_void_p, c_char_p, c_int)
        _declare(ssl, "SSL_shutdown", c_int, c_void_p)
        _declare(ssl, "SSL_get_error", c_int, c_void_p, c_int)
        _declare(crypto, "BIO_new_dgram", c_void_p, c_int, c_int)
        _declare(crypto, "BIO_ctrl", c_long, c_void_p, c_int, c_long, c_void_p)
        _declare(crypto, "BIO_ADDR_new", c_void_p)
        _declare(crypto, "BIO_ADDR_free", None, c_void_p)
        _declare(crypto, "BIO_ADDR_rawmake", c_int, c_void_p, c_int, c_void_p, c_size_t, c_uint)
        _declare(crypto, "ERR_get_error", c_ulong)
        _declare(crypto, "ERR_clear_error", None)
        _declare(crypto, "ERR_error_string_n", None, c_ulong, c_char_p, c_size_t)

    def errors(self) -> str:
        """Drain OpenSSL's error queue (it is per thread) into one message."""
        messages = []
        while code := self.crypto.ERR_get_error():
            buffer = ctypes.create_string_buffer(256)
            self.crypto.ERR_error_string_n(code, buffer, len(buffer))
            messages.append(buffer.value.decode(errors="replace"))
        return "; ".join(messages) or "no details from OpenSSL"


_openssl: _OpenSSL | None = None


def _library() -> _OpenSSL:
    global _openssl
    if _openssl is None:
        _openssl = _OpenSSL()
    return _openssl


class DtlsClient:
    """One DTLS 1.2 session to `host:port`. Use from one event loop: every call is
    non-blocking, and the handshake waits for the socket asynchronously."""

    def __init__(self, host: str, port: int, identity: str, psk: bytes):
        self._lib = _library()
        self._identity = identity.encode()
        self._psk = psk
        # Kept referenced: OpenSSL calls it back during the handshake.
        self._callback = PskCallback(self._give_psk)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ctx: int | None = None
        self._ssl: int | None = None
        try:
            self._socket.connect((host, port))
            self._socket.setblocking(False)
            self._ctx, self._ssl = self._new_session(host, port)
        except BaseException:
            self.close()
            raise

    def _new_session(self, host: str, port: int) -> tuple[int, int]:
        ssl, crypto = self._lib.ssl, self._lib.crypto
        crypto.ERR_clear_error()
        ctx = ssl.SSL_CTX_new(ssl.DTLS_client_method())
        if not ctx:
            raise DtlsError(f"OpenSSL couldn't start DTLS: {self._lib.errors()}")
        self._ctx = ctx
        for command in (SSL_CTRL_SET_MIN_PROTO_VERSION, SSL_CTRL_SET_MAX_PROTO_VERSION):
            if ssl.SSL_CTX_ctrl(ctx, command, DTLS1_2_VERSION, None) != 1:
                raise DtlsError(f"OpenSSL can't pin DTLS 1.2: {self._lib.errors()}")
        if ssl.SSL_CTX_set_cipher_list(ctx, CIPHER) != 1:
            raise DtlsError(f"OpenSSL lacks {CIPHER.decode()}: {self._lib.errors()}")
        ssl.SSL_CTX_set_psk_client_callback(ctx, self._callback)
        session = ssl.SSL_new(ctx)
        if not session:
            raise DtlsError(f"OpenSSL couldn't make a session: {self._lib.errors()}")
        self._ssl = session
        bio = crypto.BIO_new_dgram(self._socket.fileno(), BIO_NOCLOSE)
        if not bio:
            raise DtlsError(f"OpenSSL couldn't wrap the socket: {self._lib.errors()}")
        ssl.SSL_set_bio(session, bio, bio)  # The session owns the BIO from here.
        self._connect_bio(bio, host, port)
        return ctx, session

    def _connect_bio(self, bio: int, host: str, port: int) -> None:
        """A datagram BIO sends to its peer only once told it's connected."""
        crypto = self._lib.crypto
        address = crypto.BIO_ADDR_new()
        try:
            raw = socket.inet_aton(host)
            if not crypto.BIO_ADDR_rawmake(
                address, socket.AF_INET, raw, len(raw), socket.htons(port)
            ):
                raise DtlsError(f"OpenSSL couldn't take the address: {self._lib.errors()}")
            crypto.BIO_ctrl(bio, BIO_CTRL_DGRAM_SET_CONNECTED, 0, address)
        finally:
            crypto.BIO_ADDR_free(address)

    def _give_psk(
        self,
        _ssl: int | None,
        _hint: bytes | None,
        identity: int | None,
        max_identity: int,
        psk: int | None,
        max_psk: int,
    ) -> int:
        if not identity or not psk or len(self._identity) + 1 > max_identity:
            return 0
        if len(self._psk) > max_psk:
            return 0
        ctypes.memmove(identity, self._identity + b"\0", len(self._identity) + 1)
        ctypes.memmove(psk, self._psk, len(self._psk))
        return len(self._psk)

    async def handshake(self, timeout_s: float = HANDSHAKE_TIMEOUT_S) -> None:
        """Connect, retransmitting on OpenSSL's timer, within `timeout_s`."""
        ssl, crypto = self._lib.ssl, self._lib.crypto
        session = self._session()
        deadline = anyio.current_time() + timeout_s
        while True:
            crypto.ERR_clear_error()
            result = ssl.SSL_connect(session)
            if result == 1:
                return
            error = ssl.SSL_get_error(session, result)
            if error == SSL_ERROR_SYSCALL:
                raise DtlsError("The bridge isn't taking a stream; is the area started?")
            if error not in (SSL_ERROR_WANT_READ, SSL_ERROR_WANT_WRITE):
                raise DtlsError(f"The bridge refused the stream's handshake: {self._lib.errors()}")
            left_s = deadline - anyio.current_time()
            if left_s <= 0:
                raise DtlsError("The bridge didn't finish the stream's handshake in time.")
            with anyio.move_on_after(min(left_s, self._retransmit_in())):
                await anyio.wait_readable(self._socket)
                continue
            ssl.SSL_ctrl(session, DTLS_CTRL_HANDLE_TIMEOUT, 0, None)

    def _retransmit_in(self) -> float:
        left = _Timeval()
        if self._lib.ssl.SSL_ctrl(self._session(), DTLS_CTRL_GET_TIMEOUT, 0, ctypes.byref(left)):
            return float(left.tv_sec) + float(left.tv_usec) / 1_000_000
        return 1.0

    def send(self, data: bytes) -> None:
        """One record in one datagram. A frame the socket can't take now is dropped: the next
        one follows within milliseconds."""
        ssl, crypto = self._lib.ssl, self._lib.crypto
        session = self._session()
        crypto.ERR_clear_error()
        written = ssl.SSL_write(session, data, len(data))
        if written > 0:
            return
        if ssl.SSL_get_error(session, written) in (SSL_ERROR_WANT_READ, SSL_ERROR_WANT_WRITE):
            return
        raise DtlsError(f"The stream to the bridge broke: {self._lib.errors()}")

    def close(self) -> None:
        """Say goodbye if connected, then free everything; safe to call twice."""
        ssl = self._lib.ssl
        if self._ssl is not None:
            self._lib.crypto.ERR_clear_error()
            ssl.SSL_shutdown(self._ssl)
            ssl.SSL_free(self._ssl)  # Frees the BIO too.
            self._ssl = None
        if self._ctx is not None:
            ssl.SSL_CTX_free(self._ctx)
            self._ctx = None
        self._socket.close()

    def _session(self) -> int:
        if self._ssl is None:
            raise DtlsError("The stream is closed.")
        return self._ssl
