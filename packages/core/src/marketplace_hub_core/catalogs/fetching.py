from __future__ import annotations

import base64
import concurrent.futures
import http.client
import ipaddress
import math
import re
import socket
import ssl
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from email.message import Message
from pathlib import PurePath
from typing import Any, Protocol
from urllib.parse import quote, unquote, urljoin, urlsplit

from marketplace_hub_core.catalogs.schema import MAX_CATALOG_ARTIFACT_BYTES

MAX_CATALOG_URL_LENGTH = 4096
MAX_CATALOG_REDIRECTS = 3
DEFAULT_DNS_TIMEOUT_SECONDS = 5.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_DOWNLOAD_DEADLINE_SECONDS = 120.0
DOWNLOAD_CHUNK_BYTES = 64 * 1024

_SUPPORTED_SUFFIXES = frozenset({"csv", "txt", "tsv", "xls", "xlsx", "xml"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MIME_SUFFIXES = {
    "application/csv": "csv",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/xml": "xml",
    "text/csv": "csv",
    "text/plain": "txt",
    "text/tab-separated-values": "tsv",
    "text/xml": "xml",
}
_UNSUPPORTED_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/x-pickle",
        "application/x-python-pickle",
        "text/html",
    }
)
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_NON_PUBLIC_IPV6_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class CatalogFetchError(ValueError):
    """Base error safe to expose to an authenticated catalog user."""


class CatalogFetchValidationError(CatalogFetchError):
    pass


class CatalogFetchSecurityError(CatalogFetchError):
    pass


class CatalogFetchNetworkError(CatalogFetchError):
    pass


class CatalogFetchTimeoutError(CatalogFetchNetworkError):
    pass


class CatalogFetchResponseError(CatalogFetchError):
    pass


class CatalogFetchLimitError(CatalogFetchError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadedCatalog:
    content: bytes
    file_name: str
    media_type: str
    source_host: str
    total_bytes: int


CatalogFetchResult = DownloadedCatalog


@dataclass(frozen=True, slots=True)
class ParsedCatalogUrl:
    absolute: str = field(repr=False)
    host: str
    port: int
    target: str = field(repr=False)

    @property
    def origin(self) -> tuple[str, str, int]:
        return ("https", self.host, self.port)


class _Response(Protocol):
    status: int

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def read(self, amount: int | None = None) -> bytes: ...


class _Connection(Protocol):
    sock: Any

    def connect(self) -> None: ...

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> _Response: ...

    def close(self) -> None: ...


Resolver = Callable[[str, int, float], Sequence[Any]]
ConnectionFactory = Callable[..., _Connection]
Clock = Callable[[], float]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that keeps certificate SNI while dialing a vetted IP."""

    def __init__(
        self,
        *,
        host: str,
        connect_ip: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host=host, port=port, timeout=timeout, context=context)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _default_connection_factory(
    *,
    host: str,
    connect_ip: str,
    port: int,
    timeout: float,
    context: ssl.SSLContext,
) -> _Connection:
    return _PinnedHTTPSConnection(
        host=host,
        connect_ip=connect_ip,
        port=port,
        timeout=timeout,
        context=context,
    )


def _system_resolver(host: str, port: int, timeout: float) -> Sequence[Any]:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        socket.getaddrinfo,
        host,
        port,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
    )
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise CatalogFetchTimeoutError(
            "La risoluzione del server del listino ha superato il tempo massimo."
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def validate_catalog_url(value: str) -> ParsedCatalogUrl:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_CATALOG_URL_LENGTH
        or _has_control_characters(value)
    ):
        raise CatalogFetchValidationError("L'indirizzo del listino non è valido.")
    try:
        decoded = unquote(value)
    except Exception:
        raise CatalogFetchValidationError("L'indirizzo del listino non è valido.") from None
    if _has_control_characters(decoded):
        raise CatalogFetchValidationError("L'indirizzo del listino non è valido.")
    try:
        parsed = urlsplit(value)
        raw_host = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        raise CatalogFetchValidationError("L'indirizzo del listino non è valido.") from None
    if parsed.scheme.casefold() != "https":
        raise CatalogFetchSecurityError("Il listino deve usare un indirizzo HTTPS.")
    if (
        not parsed.netloc
        or raw_host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CatalogFetchValidationError("L'indirizzo del listino non è valido.")
    if port not in {None, 443}:
        raise CatalogFetchSecurityError("La porta dell'indirizzo del listino non è consentita.")
    try:
        ipaddress.ip_address(raw_host.rstrip("."))
    except ValueError:
        pass
    else:
        raise CatalogFetchSecurityError("L'indirizzo del listino non è consentito.")
    try:
        host = raw_host.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError:
        raise CatalogFetchValidationError("L'indirizzo del listino non è valido.") from None
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
    ):
        raise CatalogFetchSecurityError("L'indirizzo del listino non è consentito.")

    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&?/:@!$'()*+,;%-._~")
    target = path + (f"?{query}" if query else "")
    absolute = f"https://{host}{path}" + (f"?{query}" if query else "")
    return ParsedCatalogUrl(absolute=absolute, host=host, port=443, target=target)


def _remaining(clock: Clock, deadline: float) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise CatalogFetchTimeoutError("Il download del listino ha superato il tempo massimo.")
    return remaining


def _is_public_unicast_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or getattr(address, "is_site_local", False)
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address) and any(
        address in prefix for prefix in _NON_PUBLIC_IPV6_PREFIXES
    ):
        return False
    return True


def _resolve_public_addresses(
    url: ParsedCatalogUrl,
    *,
    resolver: Resolver,
    timeout: float,
) -> list[str]:
    try:
        raw_addresses = resolver(url.host, url.port, timeout)
    except CatalogFetchError:
        raise
    except (OSError, socket.gaierror):
        raise CatalogFetchNetworkError(
            "Non è possibile risolvere il server del listino."
        ) from None
    except Exception:
        raise CatalogFetchNetworkError(
            "Non è possibile risolvere il server del listino."
        ) from None

    addresses: list[str] = []
    for item in raw_addresses:
        candidate: Any = item
        if isinstance(item, tuple) and len(item) >= 5 and isinstance(item[4], tuple):
            candidate = item[4][0]
        if not isinstance(candidate, str):
            raise CatalogFetchNetworkError(
                "La risposta DNS del server del listino non è valida."
            )
        try:
            address = ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            raise CatalogFetchNetworkError(
                "La risposta DNS del server del listino non è valida."
            ) from None
        if not _is_public_unicast_address(address):
            raise CatalogFetchSecurityError("L'indirizzo del listino non è consentito.")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise CatalogFetchNetworkError("Il server del listino non ha indirizzi disponibili.")
    return addresses


def _set_socket_timeout(connection: _Connection, timeout: float) -> None:
    sock = getattr(connection, "sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(timeout)


def _verify_connected_peer(connection: _Connection, expected_ip: str) -> None:
    sock = getattr(connection, "sock", None)
    if sock is None or not hasattr(sock, "getpeername"):
        return
    try:
        peer = sock.getpeername()[0].split("%", 1)[0]
        if ipaddress.ip_address(peer) != ipaddress.ip_address(expected_ip):
            raise CatalogFetchSecurityError("La connessione al listino non è sicura.")
    except CatalogFetchError:
        raise
    except Exception:
        raise CatalogFetchSecurityError("La connessione al listino non è sicura.") from None


def _basic_authorization(username: str | None, password: str | None) -> str | None:
    if username is None and password is None:
        return None
    if username is None or password is None:
        raise CatalogFetchValidationError("Username e password del listino sono incompleti.")
    if (
        not username
        or not password
        or len(username) > 1024
        or len(password) > 4096
        or ":" in username
        or _has_control_characters(username)
        or _has_control_characters(password)
    ):
        raise CatalogFetchValidationError("Le credenziali del listino non sono valide.")
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _media_type(response: _Response) -> str:
    raw = (response.getheader("Content-Type", "") or "").split(";", 1)[0].strip().casefold()
    if raw in _UNSUPPORTED_MEDIA_TYPES or raw.endswith("+json"):
        raise CatalogFetchResponseError(
            "Il server non ha restituito un formato listino supportato."
        )
    return raw or "application/octet-stream"


def _safe_file_name(response: _Response, url: ParsedCatalogUrl, media_type: str) -> str:
    candidates: list[str] = []
    disposition = response.getheader("Content-Disposition", "") or ""
    if disposition:
        message = Message()
        message["Content-Disposition"] = disposition
        candidate = message.get_filename()
        if candidate:
            candidates.append(candidate)
    path_name = PurePath(unquote(urlsplit(url.absolute).path)).name
    if path_name:
        candidates.append(path_name)

    def sanitize(candidate: str) -> str:
        candidate = unicodedata.normalize("NFKC", candidate)
        candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1]
        candidate = "".join(
            character
            for character in candidate
            if not unicodedata.category(character).startswith("C")
        ).strip().strip(".")
        candidate = re.sub(r"[^\w. ()-]+", "_", candidate, flags=re.UNICODE)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        return candidate if candidate and candidate not in {".", ".."} else "listino"

    candidates = [sanitize(candidate) for candidate in candidates]
    selected = candidates[0] if candidates else "listino"

    suffix = PurePath(selected).suffix.casefold().lstrip(".")
    inferred_suffix = _MIME_SUFFIXES.get(media_type)
    if suffix not in _SUPPORTED_SUFFIXES:
        for candidate in candidates[1:]:
            candidate_suffix = PurePath(candidate).suffix.casefold().lstrip(".")
            if candidate_suffix in _SUPPORTED_SUFFIXES:
                selected = sanitize(candidate)
                suffix = candidate_suffix
                break
    if suffix not in _SUPPORTED_SUFFIXES and inferred_suffix:
        stem = PurePath(selected).stem or "listino"
        selected = f"{stem}.{inferred_suffix}"
        suffix = inferred_suffix
    if suffix not in _SUPPORTED_SUFFIXES:
        raise CatalogFetchResponseError(
            "Il server non ha restituito un formato listino supportato."
        )

    if len(selected) > 255:
        extension = f".{suffix}"
        selected = selected[: 255 - len(extension)].rstrip(". ") + extension
    return selected


def _content_length(response: _Response, maximum: int) -> int | None:
    value = (response.getheader("Content-Length", "") or "").strip()
    if not value:
        return None
    if not value.isascii() or not value.isdigit():
        raise CatalogFetchResponseError("La dimensione dichiarata dal server non è valida.")
    length = int(value)
    if length > maximum:
        raise CatalogFetchLimitError("Il listino supera il limite di 20 MiB.")
    return length


def _read_body(
    response: _Response,
    connection: _Connection,
    *,
    maximum: int,
    read_timeout: float,
    clock: Clock,
    deadline: float,
    on_progress: Callable[[int, int | None], None] | None,
) -> bytes:
    encoding = (response.getheader("Content-Encoding", "") or "").strip().casefold()
    if encoding not in {"", "identity"}:
        raise CatalogFetchResponseError(
            "Il server ha restituito una codifica del contenuto non supportata."
        )
    expected_length = _content_length(response, maximum)
    if on_progress is not None:
        on_progress(0, expected_length)
    chunks: list[bytes] = []
    total = 0
    while True:
        timeout = min(read_timeout, _remaining(clock, deadline))
        _set_socket_timeout(connection, timeout)
        try:
            chunk = response.read(min(DOWNLOAD_CHUNK_BYTES, maximum - total + 1))
        except TimeoutError:
            raise CatalogFetchTimeoutError(
                "La lettura del listino ha superato il tempo massimo."
            ) from None
        except (OSError, http.client.HTTPException):
            raise CatalogFetchNetworkError(
                "La connessione al server del listino è stata interrotta."
            ) from None
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise CatalogFetchLimitError("Il listino supera il limite di 20 MiB.")
        chunks.append(chunk)
        if on_progress is not None:
            on_progress(total, expected_length)
    if total == 0:
        raise CatalogFetchResponseError("Il server ha restituito un listino vuoto.")
    if expected_length is not None and expected_length != total:
        raise CatalogFetchResponseError("Il listino ricevuto risulta incompleto.")
    return b"".join(chunks)


def fetch_catalog(
    url: str,
    *,
    username: str | None = "",
    password: str | None = "",
    on_progress: Callable[[int, int | None], None] | None = None,
    resolver: Resolver = _system_resolver,
    connection_factory: ConnectionFactory = _default_connection_factory,
    clock: Clock = time.monotonic,
    dns_timeout_seconds: float = DEFAULT_DNS_TIMEOUT_SECONDS,
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    deadline_seconds: float = DEFAULT_DOWNLOAD_DEADLINE_SECONDS,
    maximum_bytes: int = MAX_CATALOG_ARTIFACT_BYTES,
) -> DownloadedCatalog:
    """Fetch one supported catalog while pinning each validated DNS result.

    Redirects are followed manually so every destination is independently
    validated and resolved. Basic credentials survive only same-origin hops.
    """

    time_limits = (
        dns_timeout_seconds,
        connect_timeout_seconds,
        read_timeout_seconds,
        deadline_seconds,
    )
    invalid_time_limit = any(
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
        for value in time_limits
    )
    invalid_size_limit = (
        not isinstance(maximum_bytes, int)
        or isinstance(maximum_bytes, bool)
        or maximum_bytes <= 0
        or maximum_bytes > MAX_CATALOG_ARTIFACT_BYTES
    )
    if invalid_time_limit or invalid_size_limit:
        raise CatalogFetchValidationError("I limiti del download non sono validi.")
    current = validate_catalog_url(url)
    if username == "" and password == "":
        username = password = None
    authorization = _basic_authorization(username, password)
    auth_allowed = True
    deadline = clock() + float(deadline_seconds)
    ssl_context = ssl.create_default_context()

    for redirect_count in range(MAX_CATALOG_REDIRECTS + 1):
        dns_timeout = min(float(dns_timeout_seconds), _remaining(clock, deadline))
        addresses = _resolve_public_addresses(current, resolver=resolver, timeout=dns_timeout)
        headers = {
            "Accept-Encoding": "identity",
            "Host": current.host,
            "User-Agent": "MarketplaceHub/1.0",
        }
        if authorization and auth_allowed:
            headers["Authorization"] = authorization

        connection: _Connection | None = None
        response: _Response | None = None
        last_connection_error: CatalogFetchNetworkError | None = None
        # All DNS answers were vetted above. Try them in resolver order only for
        # failures that happen before response headers are available.
        for connect_ip in addresses:
            timeout = min(float(connect_timeout_seconds), _remaining(clock, deadline))
            candidate: _Connection | None = None
            try:
                candidate = connection_factory(
                    host=current.host,
                    connect_ip=connect_ip,
                    port=current.port,
                    timeout=timeout,
                    context=ssl_context,
                )
                candidate.connect()
                _verify_connected_peer(candidate, connect_ip)
                _set_socket_timeout(
                    candidate,
                    min(float(read_timeout_seconds), _remaining(clock, deadline)),
                )
                candidate.request("GET", current.target, headers=headers)
                response = candidate.getresponse()
            except CatalogFetchSecurityError:
                raise
            except ssl.SSLCertVerificationError:
                raise CatalogFetchSecurityError(
                    "La connessione al listino non è sicura."
                ) from None
            except CatalogFetchTimeoutError:
                last_connection_error = CatalogFetchTimeoutError(
                    "La connessione al server del listino ha superato il tempo massimo."
                )
            except CatalogFetchNetworkError:
                last_connection_error = CatalogFetchNetworkError(
                    "Non è possibile collegarsi al server del listino."
                )
            except CatalogFetchError:
                raise
            except TimeoutError:
                last_connection_error = CatalogFetchTimeoutError(
                    "La connessione al server del listino ha superato il tempo massimo."
                )
            except (OSError, ssl.SSLError, http.client.HTTPException):
                last_connection_error = CatalogFetchNetworkError(
                    "Non è possibile collegarsi al server del listino."
                )
            except Exception:
                last_connection_error = CatalogFetchNetworkError(
                    "Non è possibile preparare la connessione al server del listino."
                )
            else:
                connection = candidate
                break
            finally:
                if candidate is not None and candidate is not connection:
                    candidate.close()

        if connection is None or response is None:
            if last_connection_error is None:
                raise CatalogFetchNetworkError(
                    "Non è possibile collegarsi al server del listino."
                )
            raise last_connection_error

        try:
            if response.status in _REDIRECT_STATUSES:
                if redirect_count >= MAX_CATALOG_REDIRECTS:
                    raise CatalogFetchResponseError(
                        "Il server ha restituito troppi reindirizzamenti."
                    )
                location = response.getheader("Location", "") or ""
                if not location:
                    raise CatalogFetchResponseError("Il reindirizzamento del server non è valido.")
                try:
                    destination = validate_catalog_url(urljoin(current.absolute, location))
                except CatalogFetchError:
                    raise
                same_origin = destination.origin == current.origin
                auth_allowed = auth_allowed and same_origin
                current = destination
                continue
            if response.status != 200:
                status = response.status if isinstance(response.status, int) else 0
                raise CatalogFetchResponseError(
                    f"Il server del listino ha risposto con stato HTTP {status}."
                )

            media_type = _media_type(response)
            file_name = _safe_file_name(response, current, media_type)
            content = _read_body(
                response,
                connection,
                maximum=int(maximum_bytes),
                read_timeout=float(read_timeout_seconds),
                clock=clock,
                deadline=deadline,
                on_progress=on_progress,
            )
            return DownloadedCatalog(
                content=content,
                file_name=file_name,
                media_type=media_type,
                source_host=current.host,
                total_bytes=len(content),
            )
        finally:
            connection.close()

    raise CatalogFetchResponseError("Il server ha restituito troppi reindirizzamenti.")


fetch_catalog_url = fetch_catalog


__all__ = [
    "CatalogFetchError",
    "CatalogFetchLimitError",
    "CatalogFetchNetworkError",
    "CatalogFetchResponseError",
    "CatalogFetchResult",
    "CatalogFetchSecurityError",
    "CatalogFetchTimeoutError",
    "CatalogFetchValidationError",
    "DownloadedCatalog",
    "ParsedCatalogUrl",
    "fetch_catalog",
    "fetch_catalog_url",
    "validate_catalog_url",
]
