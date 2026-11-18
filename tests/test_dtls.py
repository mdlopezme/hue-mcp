import ctypes
import shutil
import socket
import subprocess
import types
from collections.abc import AsyncIterator
from contextlib import suppress

import anyio
import pytest
from anyio.abc import Process

from hue_mcp import dtls as dtls_module
from hue_mcp.dtls import DtlsClient, DtlsError

pytestmark = pytest.mark.anyio

PSK = bytes.fromhex("00112233445566778899aabbccddeeff")
IDENTITY = "test-app-id"


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
async def server() -> AsyncIterator[tuple[int, Process]]:
    """openssl s_server as the bridge: DTLS 1.2, PSK only, with the cookie exchange."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs the openssl command")
    port = free_udp_port()
    command = [
        openssl, "s_server", "-dtls1_2", "-nocert", "-listen", "-ign_eof",
        "-psk", PSK.hex(), "-psk_identity", IDENTITY,
        "-cipher", "PSK-AES128-GCM-SHA256",
        "-accept", f"127.0.0.1:{port}", "-naccept", "1",
    ]  # fmt: skip
    async with await anyio.open_process(command, stderr=subprocess.STDOUT) as process:
        assert process.stdout is not None
        seen = b""
        while b"ACCEPT" not in seen:  # Listening.
            seen += await process.stdout.receive()
        yield port, process
        with suppress(ProcessLookupError):  # It ends by itself after one connection.
            process.kill()


async def received(process: Process) -> bytes:
    assert process.stdout is not None
    output = b""
    with anyio.move_on_after(1):
        async for chunk in process.stdout:
            output += chunk
    return output


async def test_it_connects_and_sends_records_the_server_can_read(server):
    port, process = server
    client = DtlsClient("127.0.0.1", port, IDENTITY, PSK)
    await client.handshake()
    client.send(b"HueStream-one\n")
    client.send(b"HueStream-two\n")
    await anyio.sleep(0.3)
    client.close()
    client.close()  # Twice is fine.
    output = await received(process)
    assert b"CIPHER is PSK-AES128-GCM-SHA256" in output
    assert b"HueStream-one\nHueStream-two\n" in output


async def test_a_wrong_key_is_refused(server):
    port, _ = server
    client = DtlsClient("127.0.0.1", port, IDENTITY, bytes(16))
    with pytest.raises(DtlsError, match="stream's handshake"):  # Refused, or dropped.
        await client.handshake(timeout_s=2)
    client.close()


async def test_nobody_listening_says_so():
    client = DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)
    with pytest.raises(DtlsError, match="isn't taking a stream"):
        await client.handshake(timeout_s=3)
    client.close()


async def test_a_silent_peer_times_out():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        silent.bind(("127.0.0.1", 0))
        client = DtlsClient("127.0.0.1", silent.getsockname()[1], IDENTITY, PSK)
        with pytest.raises(DtlsError, match="didn't finish the stream's handshake"):
            await client.handshake(timeout_s=0.5)
        client.close()


async def test_a_closed_client_refuses_to_send():
    client = DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)
    client.close()
    with pytest.raises(DtlsError, match="closed"):
        client.send(b"late")


def test_the_psk_callback_refuses_buffers_too_small():
    client = DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)
    identity, psk = ctypes.create_string_buffer(64), ctypes.create_string_buffer(64)
    address = ctypes.addressof
    assert client._give_psk(None, None, address(identity), 64, address(psk), 64) == 16
    assert identity.value == IDENTITY.encode()
    assert psk.raw[:16] == PSK
    assert client._give_psk(None, None, address(identity), 4, address(psk), 64) == 0
    assert client._give_psk(None, None, address(identity), 64, address(psk), 8) == 0
    assert client._give_psk(None, None, None, 64, address(psk), 64) == 0
    client.close()


class _Overriding:
    """A view of an OpenSSL library with some functions replaced."""

    def __init__(self, real: object, **overrides: object) -> None:
        self._real, self._overrides = real, overrides

    def __getattr__(self, name: str) -> object:
        return self._overrides.get(name) or getattr(self._real, name)


def failing_openssl(monkeypatch: pytest.MonkeyPatch, part: str, **overrides: object) -> None:
    real = dtls_module._library()
    fake = types.SimpleNamespace(ssl=real.ssl, crypto=real.crypto, errors=real.errors)
    setattr(fake, part, _Overriding(getattr(real, part), **overrides))
    monkeypatch.setattr(dtls_module, "_openssl", fake)


@pytest.mark.parametrize(
    ("part", "function", "error"),
    [
        ("ssl", "SSL_CTX_new", "couldn't start DTLS"),
        ("ssl", "SSL_CTX_ctrl", "can't pin DTLS 1.2"),
        ("ssl", "SSL_CTX_set_cipher_list", "lacks PSK-AES128-GCM-SHA256"),
        ("ssl", "SSL_new", "couldn't make a session"),
        ("crypto", "BIO_new_dgram", "couldn't wrap the socket"),
        ("crypto", "BIO_ADDR_rawmake", "couldn't take the address"),
    ],
)
def test_each_openssl_failure_is_named_and_cleaned_up(monkeypatch, part, function, error):
    failing_openssl(monkeypatch, part, **{function: lambda *_: 0})
    with pytest.raises(DtlsError, match=error):
        DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)


def test_openssls_own_reason_is_passed_on(monkeypatch):
    monkeypatch.setattr(dtls_module, "CIPHER", b"NO-SUCH-CIPHER")
    with pytest.raises(DtlsError, match="no cipher match"):
        DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)


def test_without_openssl_light_shows_say_so(monkeypatch):
    def missing(*_: object) -> None:
        raise OSError("not found")

    monkeypatch.setattr(dtls_module, "_openssl", None)
    monkeypatch.setattr(dtls_module.ctypes, "CDLL", missing)
    with pytest.raises(DtlsError, match="isn't here"):
        DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)


async def test_a_handshake_alert_is_reported(monkeypatch):
    failing_openssl(
        monkeypatch, "ssl", SSL_connect=lambda *_: -1, SSL_get_error=lambda *_: 1
    )  # SSL_ERROR_SSL: the peer sent an alert.
    client = DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)
    with pytest.raises(DtlsError, match="refused the stream's handshake"):
        await client.handshake()
    client.close()


def test_without_a_running_timer_retransmits_wait_a_second():
    client = DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)
    assert client._retransmit_in() == 1.0
    client.close()


@pytest.mark.parametrize(("error", "raises"), [(2, False), (1, True)])
def test_a_frame_the_socket_cant_take_is_dropped_but_a_broken_stream_raises(
    monkeypatch, error, raises
):
    failing_openssl(monkeypatch, "ssl", SSL_write=lambda *_: -1, SSL_get_error=lambda *_: error)
    client = DtlsClient("127.0.0.1", free_udp_port(), IDENTITY, PSK)
    if raises:
        with pytest.raises(DtlsError, match="broke"):
            client.send(b"frame")
    else:
        client.send(b"frame")
    client.close()
