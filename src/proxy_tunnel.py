"""Proxy tunnel helpers — reach blocked TCP services (IMAP/SMTP) via a local proxy.

Why this exists
---------------
In networks where external mail or approval APIs are blocked, the native
asyncio TCP paths in `aioimaplib` / `aiosmtplib` fail with SSL EOF because
those libraries do NOT read system proxy env vars. We open a tunnel through
a locally-running proxy and hand the raw socket to the mail library:

  - SMTP: HTTP `CONNECT` tunnel (works against Clash/FlClash mixed-port) +
          `aiosmtplib.send(..., sock=..., use_tls=True)` on port 465.
  - IMAP: SOCKS5 tunnel (python-socks) handed to aioimaplib's
          `loop.create_connection(sock=..., ssl=...)`.

The proxy is opt-in: if no proxy env var is set, callers fall back to the
default direct connect, so behaviour outside blocked networks is unchanged.

Env vars (standard names, already set in this dev env):
  HTTPS_PROXY / HTTP_PROXY   — HTTP CONNECT proxy (e.g. http://127.0.0.1:7890)
  SOCKS_PROXY                 — SOCKS5 proxy (e.g. socks5h://127.0.0.1:7891)
                               Falls back to parsing the proxy from
                               HTTPS_PROXY if it is a socks5(h):// URL.
"""

from __future__ import annotations

import os
import socket
import ssl
from typing import Optional

import asyncio


def _http_proxy_url() -> Optional[str]:
    """Return the HTTP(S) CONNECT proxy URL from env, or None."""
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        v = os.environ.get(key)
        if v and v.startswith("http://"):
            return v
    return None


def _socks_proxy_url() -> Optional[str]:
    """Return a SOCKS5 proxy URL (SOCKS_PROXY, else a socks:// in HTTP_PROXY)."""
    v = os.environ.get("SOCKS_PROXY") or os.environ.get("socks_proxy")
    if v and v.startswith("socks"):
        return v
    # Fall back: some setups put socks5h://127.0.0.1:7891 in HTTP_PROXY
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        u = os.environ.get(key)
        if u and u.startswith("socks"):
            return u
    return None


def open_http_connect_tunnel(dest_host: str, dest_port: int, timeout: int = 15) -> socket.socket:
    """Open a raw TCP socket tunneled through an HTTP CONNECT proxy.

    Returns a *blocking* socket already connected (tunneled) to
    (dest_host, dest_port). The caller wraps it for TLS as needed.
    Raises RuntimeError if no HTTP proxy is configured or the CONNECT fails.
    """
    proxy = _http_proxy_url()
    if not proxy:
        raise RuntimeError("No HTTP(S) proxy configured (HTTPS_PROXY/HTTP_PROXY).")
    # Parse host:port out of the URL.
    body = proxy.split("://", 1)[1]
    if "@" in body:  # strip userinfo
        body = body.split("@", 1)[1]
    phost, pport = body.rsplit(":", 1)

    raw = socket.create_connection((phost, int(pport)), timeout=timeout)
    req = (
        f"CONNECT {dest_host}:{dest_port} HTTP/1.1\r\n"
        f"Host: {dest_host}:{dest_port}\r\n"
        f"Proxy-Connection: keep-alive\r\n\r\n"
    ).encode()
    raw.sendall(req)
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = raw.recv(1024)
        if not chunk:
            break
        resp += chunk
    status_line = resp.split(b"\r\n")[0].decode(errors="replace")
    if "200" not in status_line:
        raw.close()
        raise RuntimeError(f"HTTP CONNECT failed: {status_line}")
    return raw


async def open_socks5_tunnel(
    dest_host: str, dest_port: int, timeout: float = 15.0
) -> socket.socket:
    """Open a SOCKS5-tunneled raw socket via the SOCKS5 proxy.

    Returns a non-blocking socket connected (tunneled) to (dest_host, dest_port).
    Raises RuntimeError if no SOCKS proxy is configured.
    """
    from python_socks import ProxyType
    from python_socks.async_.asyncio import Proxy

    url = _socks_proxy_url()
    if not url:
        raise RuntimeError("No SOCKS5 proxy configured (SOCKS_PROXY).")
    # Parse socks5://host:port or socks5h://host:port
    body = url.split("://", 1)[1]
    if "@" in body:
        body = body.split("@", 1)[1]
    shost, sport = body.rsplit(":", 1)
    proxy = Proxy(ProxyType.SOCKS5, host=shost, port=int(sport))
    raw = await proxy.connect(dest_host=dest_host, dest_port=dest_port, timeout=timeout)
    raw.setblocking(False)
    return raw


def smtp_tunnel_socket(dest_host: str, dest_port: int = 465, timeout: int = 15) -> socket.socket:
    """Blocking raw socket tunneled to the SMTP server (use with use_tls=True)."""
    return open_http_connect_tunnel(dest_host, dest_port, timeout=timeout)


async def imap_tunnel_socket(dest_host: str, dest_port: int = 993, timeout: float = 15.0) -> socket.socket:
    """Non-blocking raw socket tunneled to the IMAP server (use with ssl ctx).

    Prefers SOCKS5 (most stable for TLS-over-tunnel; a SOCKS5 proxy such as
    Clash/FlClash's socks-port must be configured via SOCKS_PROXY or a
    socks5:// URL in HTTPS_PROXY). Falls back to an HTTP CONNECT tunnel
    through HTTPS_PROXY/HTTP_PROXY.
    """
    try:
        return await open_socks5_tunnel(dest_host, dest_port, timeout=timeout)
    except RuntimeError:
        # No SOCKS5 proxy → try HTTP CONNECT (less stable for IMAP TLS)
        raw = open_http_connect_tunnel(dest_host, dest_port, timeout=int(timeout))
        raw.setblocking(False)
        return raw
