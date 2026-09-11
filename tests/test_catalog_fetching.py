from __future__ import annotations

import base64
import io
import ssl

import pytest
from marketplace_hub_core.catalogs import fetching
from marketplace_hub_core.catalogs.fetching import (
    CatalogFetchLimitError,
    CatalogFetchNetworkError,
    CatalogFetchResponseError,
    CatalogFetchSecurityError,
    CatalogFetchTimeoutError,
    CatalogFetchValidationError,
    fetch_catalog,
    fetch_catalog_to_file,
    validate_catalog_url,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


class FakeSocket:
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def getpeername(self) -> tuple[str, int]:
        return self.peer, 443


class FakeResponse:
    def __init__(
        self,
        body: bytes = b"sku;cost\nA;12.34\n",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = {key.casefold(): value for key, value in (headers or {}).items()}
        self._read_error = read_error

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.casefold(), default)

    def read(self, amount: int | None = None) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body.read(-1 if amount is None else amount)


class FakeConnection:
    def __init__(
        self,
        response: FakeResponse,
        peer: str,
        *,
        connect_error: Exception | None = None,
        request_error: Exception | None = None,
        response_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.sock = FakeSocket(peer)
        self.connect_error = connect_error
        self.request_error = request_error
        self.response_error = response_error
        self.connect_count = 0
        self.closed = False
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def connect(self) -> None:
        self.connect_count += 1
        if self.connect_error is not None:
            raise self.connect_error

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        assert body is None
        self.requests.append((method, url, dict(headers or {})))
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self) -> FakeResponse:
        if self.response_error is not None:
            raise self.response_error
        return self.response

    def close(self) -> None:
        self.closed = True


class FakeConnectionFactory:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.connections: list[FakeConnection] = []

    def __call__(self, **kwargs) -> FakeConnection:
        self.calls.append(kwargs)
        connection = FakeConnection(self.responses.pop(0), kwargs["connect_ip"])
        self.connections.append(connection)
        return connection


def public_resolver(host: str, port: int, timeout: float) -> list[str]:
    assert host
    assert port == 443
    assert timeout > 0
    return [PUBLIC_V4, PUBLIC_V6]


def response(
    body: bytes = b"sku;cost\nA;12.34\n",
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> FakeResponse:
    defaults = {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Length": str(len(body)),
    }
    defaults.update(headers or {})
    return FakeResponse(body, status=status, headers=defaults)


def fetch_with(
    url: str,
    responses: list[FakeResponse],
    **kwargs,
) -> tuple[object, FakeConnectionFactory]:
    factory = FakeConnectionFactory(responses)
    result = fetch_catalog(
        url,
        resolver=kwargs.pop("resolver", public_resolver),
        connection_factory=factory,
        **kwargs,
    )
    return result, factory


def test_large_feed_can_be_spooled_and_explicitly_cleaned_without_body_buffering():
    body = (b"<offer><product id='1'/></offer>" * 10_000)
    factory = FakeConnectionFactory([
        response(body, headers={"Content-Type": "application/xml"}),
    ])

    result = fetch_catalog_to_file(
        "https://catalog.example.com/full.xml",
        resolver=public_resolver,
        connection_factory=factory,
    )

    try:
        assert result.path.is_file()
        assert result.path.read_bytes() == body
        assert result.file_name == "full.xml"
        assert result.media_type == "application/xml"
        assert result.total_bytes == len(body)
    finally:
        path = result.path
        result.cleanup()
    assert not path.exists()


def test_validate_catalog_url_canonicalizes_host_port_and_request_target():
    parsed = validate_catalog_url(
        "https://CATALOG.Example.com.:443/listino prezzi/feed.csv?token=a%2Fb&v=1"
    )

    assert parsed.host == "catalog.example.com"
    assert parsed.port == 443
    assert parsed.target == "/listino%20prezzi/feed.csv?token=a%2Fb&v=1"
    assert parsed.origin == ("https", "catalog.example.com", 443)
    assert "token" not in repr(parsed)


@pytest.mark.parametrize(
    "url,error_type",
    [
        ("http://catalog.example.com/feed.csv", CatalogFetchSecurityError),
        ("https://catalog.example.com:8443/feed.csv", CatalogFetchSecurityError),
        ("https://user:secret@catalog.example.com/feed.csv", CatalogFetchValidationError),
        ("https://catalog.example.com/feed.csv#part", CatalogFetchValidationError),
        ("https://127.0.0.1/feed.csv", CatalogFetchSecurityError),
        ("https://[2606:4700:4700::1111]/feed.csv", CatalogFetchSecurityError),
        ("https://localhost/feed.csv", CatalogFetchSecurityError),
        ("https://catalog.example.com/feed%0A.csv", CatalogFetchValidationError),
        ("https://catalog.example.com/feed.csv\r\nX-Evil: yes", CatalogFetchValidationError),
        (" https://catalog.example.com/feed.csv", CatalogFetchValidationError),
        ("https://-catalog.example.com/feed.csv", CatalogFetchSecurityError),
        ("x" * 4097, CatalogFetchValidationError),
    ],
)
def test_validate_catalog_url_rejects_unsafe_input(url: str, error_type: type[Exception]):
    with pytest.raises(error_type):
        validate_catalog_url(url)


def test_fetch_pins_vetted_ip_sets_safe_headers_and_reports_progress():
    body = b"sku;cost\nA;12.34\n"
    progress: list[tuple[int, int | None]] = []

    result, factory = fetch_with(
        "https://catalog.example.com/folder/feed.csv?access=private",
        [response(body)],
        on_progress=lambda processed, total: progress.append((processed, total)),
    )

    assert result.content == body
    assert result.file_name == "feed.csv"
    assert result.media_type == "text/csv"
    assert result.source_host == "catalog.example.com"
    assert result.total_bytes == len(body)
    assert progress == [(0, len(body)), (len(body), len(body))]
    assert factory.calls[0]["connect_ip"] == PUBLIC_V4
    assert factory.calls[0]["host"] == "catalog.example.com"
    assert factory.calls[0]["port"] == 443
    assert factory.calls[0]["context"].check_hostname is True
    assert factory.calls[0]["context"].verify_mode != 0
    connection = factory.connections[0]
    assert connection.connect_count == 1
    assert connection.closed is True
    method, target, headers = connection.requests[0]
    assert method == "GET"
    assert target == "/folder/feed.csv?access=private"
    assert headers == {
        "Accept-Encoding": "identity",
        "Host": "catalog.example.com",
        "User-Agent": "MarketplaceHub/1.0",
    }


def test_basic_auth_survives_same_origin_redirect():
    redirects = [
        FakeResponse(status=302, headers={"Location": "/final/feed.csv"}),
        response(),
    ]
    result, factory = fetch_with(
        "https://catalog.example.com/start.csv",
        redirects,
        username="account",
        password="top-secret",
    )

    expected = "Basic " + base64.b64encode(b"account:top-secret").decode("ascii")
    assert result.file_name == "feed.csv"
    assert [item.requests[0][2].get("Authorization") for item in factory.connections] == [
        expected,
        expected,
    ]


def test_basic_auth_is_permanently_removed_after_cross_origin_redirect():
    redirects = [
        FakeResponse(
            status=302,
            headers={"Location": "https://cdn.example.net/feed.csv"},
        ),
        FakeResponse(
            status=302,
            headers={"Location": "https://catalog.example.com/final.csv"},
        ),
        response(),
    ]
    _, factory = fetch_with(
        "https://catalog.example.com/start.csv",
        redirects,
        username="account",
        password="top-secret",
    )

    headers = [item.requests[0][2] for item in factory.connections]
    assert "Authorization" in headers[0]
    assert "Authorization" not in headers[1]
    assert "Authorization" not in headers[2]


def test_redirect_destination_is_resolved_again_and_private_answer_is_rejected():
    queried: list[str] = []

    def resolver(host: str, _port: int, _timeout: float) -> list[str]:
        queried.append(host)
        return [PUBLIC_V4] if host == "catalog.example.com" else ["10.0.0.8"]

    factory = FakeConnectionFactory(
        [
            FakeResponse(
                status=302,
                headers={"Location": "https://internal.example.net/feed.csv"},
            )
        ]
    )
    with pytest.raises(CatalogFetchSecurityError, match="non è consentito"):
        fetch_catalog(
            "https://catalog.example.com/start.csv",
            resolver=resolver,
            connection_factory=factory,
        )

    assert queried == ["catalog.example.com", "internal.example.net"]
    assert len(factory.connections) == 1
    assert factory.connections[0].closed is True


def test_redirect_to_http_is_rejected_as_downgrade():
    factory = FakeConnectionFactory(
        [FakeResponse(status=302, headers={"Location": "http://cdn.example.net/feed.csv"})]
    )
    with pytest.raises(CatalogFetchSecurityError, match="HTTPS"):
        fetch_catalog(
            "https://catalog.example.com/start.csv",
            resolver=public_resolver,
            connection_factory=factory,
        )


def test_at_most_three_redirects_are_followed():
    responses = [
        FakeResponse(status=302, headers={"Location": f"/hop-{index}.csv"})
        for index in range(3)
    ] + [response()]
    result, factory = fetch_with("https://catalog.example.com/start.csv", responses)

    assert result.file_name == "hop-2.csv"
    assert len(factory.connections) == 4
    assert all(connection.closed for connection in factory.connections)


def test_fourth_redirect_is_rejected_and_all_connections_are_closed():
    responses = [
        FakeResponse(status=302, headers={"Location": f"/hop-{index}.csv"})
        for index in range(4)
    ]
    factory = FakeConnectionFactory(responses)
    with pytest.raises(CatalogFetchResponseError, match="troppi reindirizzamenti"):
        fetch_catalog(
            "https://catalog.example.com/start.csv",
            resolver=public_resolver,
            connection_factory=factory,
        )

    assert len(factory.connections) == 4
    assert all(connection.closed for connection in factory.connections)


def test_dns_rejects_entire_answer_if_any_address_is_not_public_unicast():
    factory = FakeConnectionFactory([response()])
    with pytest.raises(CatalogFetchSecurityError, match="non è consentito"):
        fetch_catalog(
            "https://catalog.example.com/feed.csv",
            resolver=lambda _host, _port, _timeout: [PUBLIC_V4, "192.168.1.20"],
            connection_factory=factory,
        )

    assert factory.connections == []


@pytest.mark.parametrize(
    "address",
    [
        "224.0.0.1",
        "239.255.255.250",
        "240.0.0.1",
        "0.0.0.0",
        "ff02::1",
        "ff0e::1",
        "fec0::1",
        "::",
        "64:ff9b::808:808",
        "64:ff9b::7f00:1",
        "64:ff9b:1::808:808",
    ],
)
def test_dns_rejects_non_public_unicast_ipv4_and_ipv6(address: str):
    factory = FakeConnectionFactory([response()])

    with pytest.raises(CatalogFetchSecurityError, match="non è consentito"):
        fetch_catalog(
            "https://catalog.example.com/feed.csv",
            resolver=lambda _host, _port, _timeout: [address],
            connection_factory=factory,
        )

    assert factory.connections == []


@pytest.mark.parametrize(
    "address",
    [
        PUBLIC_V4,
        "1.1.1.1",
        PUBLIC_V6,
        "2001:4860:4860::8888",
    ],
)
def test_dns_accepts_public_unicast_ipv4_and_ipv6(address: str):
    result, factory = fetch_with(
        "https://catalog.example.com/feed.csv",
        [response()],
        resolver=lambda _host, _port, _timeout: [address],
    )

    assert result.source_host == "catalog.example.com"
    assert factory.calls[0]["connect_ip"] == address


def test_connect_failure_falls_back_to_next_vetted_ipv4_address():
    second_v4 = "1.1.1.1"

    class MultiAddressFactory(FakeConnectionFactory):
        def __call__(self, **kwargs) -> FakeConnection:
            self.calls.append(kwargs)
            connect_error = OSError("unreachable") if not self.connections else None
            connection = FakeConnection(
                response(),
                kwargs["connect_ip"],
                connect_error=connect_error,
            )
            self.connections.append(connection)
            return connection

    factory = MultiAddressFactory([])
    result = fetch_catalog(
        "https://catalog.example.com/feed.csv",
        resolver=lambda _host, _port, _timeout: [PUBLIC_V4, second_v4],
        connection_factory=factory,
    )

    assert result.file_name == "feed.csv"
    assert [call["connect_ip"] for call in factory.calls] == [PUBLIC_V4, second_v4]
    assert [connection.closed for connection in factory.connections] == [True, True]


def test_request_timeout_falls_back_from_ipv4_to_ipv6_with_same_target_and_auth():
    class DualStackFactory(FakeConnectionFactory):
        def __call__(self, **kwargs) -> FakeConnection:
            self.calls.append(kwargs)
            request_error = TimeoutError("secret") if not self.connections else None
            connection = FakeConnection(
                response(),
                kwargs["connect_ip"],
                request_error=request_error,
            )
            self.connections.append(connection)
            return connection

    factory = DualStackFactory([])
    result = fetch_catalog(
        "https://catalog.example.com/private/feed.csv?version=4",
        username="account",
        password="password",
        resolver=public_resolver,
        connection_factory=factory,
    )

    expected_auth = "Basic " + base64.b64encode(b"account:password").decode("ascii")
    assert result.file_name == "feed.csv"
    assert [call["connect_ip"] for call in factory.calls] == [PUBLIC_V4, PUBLIC_V6]
    assert [connection.requests for connection in factory.connections] == [
        [
            (
                "GET",
                "/private/feed.csv?version=4",
                {
                    "Accept-Encoding": "identity",
                    "Authorization": expected_auth,
                    "Host": "catalog.example.com",
                    "User-Agent": "MarketplaceHub/1.0",
                },
            )
        ],
        [
            (
                "GET",
                "/private/feed.csv?version=4",
                {
                    "Accept-Encoding": "identity",
                    "Authorization": expected_auth,
                    "Host": "catalog.example.com",
                    "User-Agent": "MarketplaceHub/1.0",
                },
            )
        ],
    ]
    assert all(connection.closed for connection in factory.connections)


def test_http_response_does_not_fall_back_to_another_address():
    factory = FakeConnectionFactory([response(status=503), response()])

    with pytest.raises(CatalogFetchResponseError, match="HTTP 503"):
        fetch_catalog(
            "https://catalog.example.com/feed.csv",
            resolver=public_resolver,
            connection_factory=factory,
        )

    assert [call["connect_ip"] for call in factory.calls] == [PUBLIC_V4]
    assert factory.connections[0].closed is True


def test_connected_peer_must_equal_pinned_dns_address():
    class WrongPeerFactory(FakeConnectionFactory):
        def __call__(self, **kwargs) -> FakeConnection:
            self.calls.append(kwargs)
            connection = FakeConnection(self.responses.pop(0), "1.1.1.1")
            self.connections.append(connection)
            return connection

    factory = WrongPeerFactory([response()])
    with pytest.raises(CatalogFetchSecurityError, match="non è sicura"):
        fetch_catalog(
            "https://catalog.example.com/feed.csv",
            resolver=public_resolver,
            connection_factory=factory,
        )

    assert len(factory.connections) == 1
    assert factory.connections[0].closed is True


def test_certificate_verification_failure_stops_without_address_fallback():
    class CertificateFailureFactory(FakeConnectionFactory):
        def __call__(self, **kwargs) -> FakeConnection:
            self.calls.append(kwargs)
            connection = FakeConnection(
                response(),
                kwargs["connect_ip"],
                connect_error=ssl.SSLCertVerificationError("certificate rejected"),
            )
            self.connections.append(connection)
            return connection

    factory = CertificateFailureFactory([])
    with pytest.raises(CatalogFetchSecurityError, match="non è sicura"):
        fetch_catalog(
            "https://catalog.example.com/feed.csv",
            resolver=public_resolver,
            connection_factory=factory,
        )

    assert [call["connect_ip"] for call in factory.calls] == [PUBLIC_V4]
    assert factory.connections[0].closed is True


def test_transient_tls_network_failure_can_fall_back_to_next_vetted_address():
    class TlsFailureFactory(FakeConnectionFactory):
        def __call__(self, **kwargs) -> FakeConnection:
            self.calls.append(kwargs)
            connect_error = ssl.SSLError("handshake interrupted") if not self.connections else None
            connection = FakeConnection(
                response(),
                kwargs["connect_ip"],
                connect_error=connect_error,
            )
            self.connections.append(connection)
            return connection

    factory = TlsFailureFactory([])
    result = fetch_catalog(
        "https://catalog.example.com/feed.csv",
        resolver=public_resolver,
        connection_factory=factory,
    )

    assert result.file_name == "feed.csv"
    assert [call["connect_ip"] for call in factory.calls] == [PUBLIC_V4, PUBLIC_V6]
    assert all(connection.closed for connection in factory.connections)


def test_content_length_and_streaming_both_enforce_maximum_size():
    declared = FakeResponse(
        b"x",
        headers={
            "Content-Type": "text/csv",
            "Content-Length": "11",
        },
    )
    with pytest.raises(CatalogFetchLimitError, match="20 MiB"):
        fetch_with("https://catalog.example.com/feed.csv", [declared], maximum_bytes=10)

    streamed = FakeResponse(b"x" * 11, headers={"Content-Type": "text/csv"})
    with pytest.raises(CatalogFetchLimitError, match="20 MiB"):
        fetch_with("https://catalog.example.com/feed.csv", [streamed], maximum_bytes=10)


@pytest.mark.parametrize(
    "remote_response,message",
    [
        (FakeResponse(b"", headers={"Content-Type": "text/csv"}), "vuoto"),
        (
            FakeResponse(
                b"abc",
                headers={"Content-Type": "text/csv", "Content-Length": "4"},
            ),
            "incompleto",
        ),
        (
            FakeResponse(
                b"abc",
                headers={"Content-Type": "text/csv", "Content-Encoding": "gzip"},
            ),
            "codifica",
        ),
        (FakeResponse(b"{}", headers={"Content-Type": "application/json"}), "formato"),
        (FakeResponse(b"<html/>", headers={"Content-Type": "text/html"}), "formato"),
    ],
)
def test_response_rejects_empty_incomplete_encoded_and_unsupported_content(
    remote_response: FakeResponse,
    message: str,
):
    with pytest.raises(CatalogFetchResponseError, match=message):
        fetch_with("https://catalog.example.com/feed.csv", [remote_response])


@pytest.mark.parametrize(
    "path,headers,expected",
    [
        (
            "/download",
            {
                "Content-Type": "text/csv",
                "Content-Disposition": 'attachment; filename="../../prezzi?.CSV"',
            },
            "prezzi_.CSV",
        ),
        (
            "/download",
            {"Content-Type": "application/xml"},
            "download.xml",
        ),
        (
            "/download?name=secret.csv",
            {
                "Content-Type": "application/octet-stream",
                "Content-Disposition": "attachment; filename*=UTF-8''listino%20fornitore.xlsx",
            },
            "listino fornitore.xlsx",
        ),
    ],
)
def test_filename_is_sanitized_and_can_be_inferred_from_media_type(
    path: str,
    headers: dict[str, str],
    expected: str,
):
    result, _ = fetch_with(
        f"https://catalog.example.com{path}",
        [response(headers=headers)],
    )
    assert result.file_name == expected


def test_unknown_filename_and_media_type_are_rejected():
    with pytest.raises(CatalogFetchResponseError, match="formato"):
        fetch_with(
            "https://catalog.example.com/download",
            [response(headers={"Content-Type": "application/octet-stream"})],
        )


@pytest.mark.parametrize(
    "name,media_type",
    [
        ("feed.csv", "text/csv"),
        ("feed.txt", "text/plain"),
        ("feed.tsv", "text/tab-separated-values"),
        ("feed.xls", "application/vnd.ms-excel"),
        (
            "feed.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("feed.xml", "application/xml"),
    ],
)
def test_all_parser_file_formats_are_accepted(name: str, media_type: str):
    result, _ = fetch_with(
        f"https://catalog.example.com/{name}",
        [response(headers={"Content-Type": media_type})],
    )
    assert result.file_name == name
    assert result.media_type == media_type


def test_deadline_is_checked_after_dns_and_timeout_values_are_bounded():
    moments = iter([100.0, 100.0, 100.0, 102.1])
    seen_timeout: list[float] = []

    def resolver(_host: str, _port: int, timeout: float) -> list[str]:
        seen_timeout.append(timeout)
        return [PUBLIC_V4]

    with pytest.raises(CatalogFetchTimeoutError, match="tempo massimo"):
        fetch_catalog(
            "https://catalog.example.com/feed.csv",
            resolver=resolver,
            connection_factory=FakeConnectionFactory([response()]),
            clock=lambda: next(moments),
            dns_timeout_seconds=10,
            deadline_seconds=2,
        )

    assert seen_timeout == [2.0]


def test_address_fallback_cannot_restart_or_exceed_the_global_deadline():
    now = [100.0]

    class DeadlineConnection(FakeConnection):
        def connect(self) -> None:
            self.connect_count += 1
            now[0] += 2.1
            raise TimeoutError("first address timed out")

    class DeadlineFactory(FakeConnectionFactory):
        def __call__(self, **kwargs) -> FakeConnection:
            self.calls.append(kwargs)
            connection = DeadlineConnection(response(), kwargs["connect_ip"])
            self.connections.append(connection)
            return connection

    factory = DeadlineFactory([])
    with pytest.raises(CatalogFetchTimeoutError, match="tempo massimo"):
        fetch_catalog(
            "https://catalog.example.com/feed.csv",
            resolver=public_resolver,
            connection_factory=factory,
            clock=lambda: now[0],
            connect_timeout_seconds=10,
            deadline_seconds=2,
        )

    assert [call["connect_ip"] for call in factory.calls] == [PUBLIC_V4]
    assert factory.calls[0]["timeout"] == pytest.approx(2.0)
    assert factory.connections[0].closed is True


@pytest.mark.parametrize("failure", [TimeoutError(), OSError("https://secret/?token=abc")])
def test_network_failures_are_typed_and_do_not_leak_url_or_credentials(failure: Exception):
    class FailingFactory(FakeConnectionFactory):
        def __call__(self, **kwargs) -> FakeConnection:
            self.calls.append(kwargs)
            connection = FakeConnection(response(), kwargs["connect_ip"], connect_error=failure)
            self.connections.append(connection)
            return connection

    factory = FailingFactory([])
    error_type = (
        CatalogFetchTimeoutError
        if isinstance(failure, TimeoutError)
        else CatalogFetchNetworkError
    )
    with pytest.raises(error_type) as captured:
        fetch_catalog(
            "https://catalog.example.com/feed.csv?token=url-secret",
            username="account",
            password="password-secret",
            resolver=public_resolver,
            connection_factory=factory,
        )

    message = str(captured.value)
    assert "url-secret" not in message
    assert "password-secret" not in message
    assert "secret" not in message
    assert [call["connect_ip"] for call in factory.calls] == [PUBLIC_V4, PUBLIC_V6]
    assert all(connection.closed for connection in factory.connections)


def test_read_timeout_is_typed_and_sanitized():
    remote_response = response()
    remote_response._read_error = TimeoutError("token=secret")
    with pytest.raises(CatalogFetchTimeoutError) as captured:
        fetch_with("https://catalog.example.com/feed.csv?token=secret", [remote_response])
    assert "secret" not in str(captured.value)


def test_default_pinned_connection_dials_ip_but_uses_hostname_for_tls(monkeypatch):
    raw_socket = object()
    wrapped_socket = object()
    calls: dict[str, object] = {}

    def create_connection(address, timeout, source_address):
        calls["address"] = address
        calls["timeout"] = timeout
        calls["source_address"] = source_address
        return raw_socket

    class Context:
        def wrap_socket(self, sock, *, server_hostname):
            calls["wrapped"] = sock
            calls["server_hostname"] = server_hostname
            return wrapped_socket

    monkeypatch.setattr(fetching.socket, "create_connection", create_connection)
    connection = fetching._PinnedHTTPSConnection(
        host="catalog.example.com",
        connect_ip=PUBLIC_V4,
        port=443,
        timeout=4.0,
        context=Context(),
    )
    connection.connect()

    assert calls == {
        "address": (PUBLIC_V4, 443),
        "timeout": 4.0,
        "source_address": None,
        "wrapped": raw_socket,
        "server_hostname": "catalog.example.com",
    }
    assert connection.sock is wrapped_socket


def test_resolver_os_error_is_sanitized():
    def resolver(_host: str, _port: int, _timeout: float) -> list[str]:
        raise OSError("failed for https://catalog.example.com/?token=secret")

    with pytest.raises(CatalogFetchNetworkError) as captured:
        fetch_catalog(
            "https://catalog.example.com/feed.csv?token=secret",
            resolver=resolver,
            connection_factory=FakeConnectionFactory([response()]),
        )
    assert "catalog.example.com" not in str(captured.value)
    assert "secret" not in str(captured.value)


def test_incomplete_or_invalid_basic_credentials_are_rejected_before_dns():
    resolver_called = False

    def resolver(_host: str, _port: int, _timeout: float) -> list[str]:
        nonlocal resolver_called
        resolver_called = True
        return [PUBLIC_V4]

    for username, password in [("account", ""), ("bad:name", "password")]:
        with pytest.raises(CatalogFetchValidationError):
            fetch_catalog(
                "https://catalog.example.com/feed.csv",
                username=username,
                password=password,
                resolver=resolver,
                connection_factory=FakeConnectionFactory([response()]),
            )
    assert resolver_called is False


def test_invalid_limits_are_rejected():
    for options in [
        {"maximum_bytes": 0},
        {"dns_timeout_seconds": 0},
        {"connect_timeout_seconds": -1},
        {"read_timeout_seconds": False},
        {"deadline_seconds": 0},
        {"deadline_seconds": float("inf")},
        {"maximum_bytes": 200 * 1024 * 1024 + 1},
    ]:
        with pytest.raises(CatalogFetchValidationError, match="limiti"):
            fetch_catalog("https://catalog.example.com/feed.csv", **options)
