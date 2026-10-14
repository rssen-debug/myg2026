"""Verified, read-only HTTPS JSON transport for the allowlisted public market APIs.

Windows' certificate-chain builder can validate chains that the bundled OpenSSL
cannot. Only a TLS failure permits the system curl/Schannel fallback. Neither
transport follows redirects or disables certificate checks.
"""
import json
import math
import os
from pathlib import Path
import ssl
import time
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from telemetry import NETWORK_METRICS

try:
    import truststore
except ImportError:
    truststore = None


ALLOWED_HOSTS = frozenset({"api.dexscreener.com", "api.rugcheck.xyz", "api.geckoterminal.com"})
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class PublicHTTPError(RuntimeError):
    """A bounded diagnostic safe to display in the paper dashboard."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublicHTTPError("Public API redirect rejected")


def _validated_host(url):
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise PublicHTTPError("Invalid public API URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise PublicHTTPError("Invalid public API URL") from exc
    if (parsed.scheme != "https" or host not in ALLOWED_HOSTS or
            port not in (None, 443) or parsed.username is not None or
            parsed.password is not None or parsed.fragment):
        raise PublicHTTPError("Public API URL is outside the HTTPS allowlist")
    return host


def _decode_json(raw, host):
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PublicHTTPError(f"{host}: response exceeds size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise PublicHTTPError(f"{host}: invalid JSON response") from exc


def _urllib_json(url, timeout, host):
    # Use Windows' verified chain builder when installed; otherwise OpenSSL's
    # default certificate verification remains active and can fall back below.
    context = (truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
               if os.name == "nt" and truststore is not None
               else ssl.create_default_context())
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, method="GET")
    with opener.open(req, timeout=timeout) as response:
        if not 200 <= response.status < 300:
            raise PublicHTTPError(f"{host}: HTTP {response.status}")
        return _decode_json(response.read(MAX_RESPONSE_BYTES + 1), host)


def _windows_curl_json(url, timeout, host):
    # An absolute system executable prevents a workspace curl.exe being used.
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    curl_path = Path(system_root) / "System32" / "curl.exe"
    if not curl_path.is_absolute() or not curl_path.is_file():
        raise PublicHTTPError(f"{host}: verified Windows HTTPS transport unavailable")
    args = [str(curl_path), "--disable", "--silent", "--show-error", "--fail",
            "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "0",
            "--max-time", str(timeout), "--max-filesize", str(MAX_RESPONSE_BYTES),
            "--user-agent", "Mozilla/5.0", "--header", "Accept: application/json",
            "--write-out", "\n%{http_code}", "--url", url]
    try:
        result = subprocess.run(args, capture_output=True, check=False,
                                timeout=timeout + 2, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicHTTPError(f"{host}: verified Windows HTTPS request failed") from exc
    if result.returncode:
        raise PublicHTTPError(f"{host}: verified Windows HTTPS request failed (curl {result.returncode})")
    body, separator, status = result.stdout.rpartition(b"\n")
    if not separator or len(status) != 3 or not status.isdigit():
        raise PublicHTTPError(f"{host}: invalid HTTPS response status")
    if not 200 <= int(status) < 300:
        raise PublicHTTPError(f"{host}: HTTP {int(status)} (redirects disabled)")
    return _decode_json(body, host)


def _get_public_json(url, timeout=6):
    """Return parsed JSON from an allowlisted public HTTPS GET, or raise.

    No credentials, cookies, wallet calls, writes, or automatic redirects.
    """
    host = _validated_host(url)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise PublicHTTPError("Public API timeout must be in (0, 60] seconds")
    try:
        return _urllib_json(url, timeout, host)
    except urllib.error.HTTPError as exc:
        raise PublicHTTPError(f"{host}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, ssl.SSLError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLError) and os.name == "nt":
            return _windows_curl_json(url, timeout, host)
        raise PublicHTTPError(f"{host}: HTTPS connection failed ({type(reason).__name__})") from exc
    except (TimeoutError, OSError) as exc:
        raise PublicHTTPError(f"{host}: HTTPS connection failed ({type(exc).__name__})") from exc


def get_public_json(url, timeout=6):
    host = _validated_host(url)
    started = time.perf_counter()
    try:
        payload = _get_public_json(url, timeout)
    except Exception as exc:
        NETWORK_METRICS.record(host, (time.perf_counter()-started)*1000, False, str(exc))
        raise
    NETWORK_METRICS.record(host, (time.perf_counter()-started)*1000, True)
    return payload
