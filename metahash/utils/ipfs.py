# metahash/utils/ipfs.py
# Lightweight IPFS helpers (HTTP API -> CLI -> gateways), with async wrappers.

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

from metahash.config import DEFAULT_API_URL, DEFAULT_GATEWAYS, IPFS_CMD

try:
    import requests as _requests  # optional
    _HAVE_REQUESTS = True
except Exception:
    _requests = None
    _HAVE_REQUESTS = False


class IPFSError(Exception):
    pass


def minidumps(obj: Any, *, sort_keys: bool = True) -> str:
    """Deterministic, compact JSON for hashing."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=sort_keys)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------- add (bytes / json) ---------------------------

def _post_add_via_api(data: bytes, filename: str, api_url: str, *, pin: bool = True, cid_version: int = 1) -> str:
    if not _HAVE_REQUESTS:
        raise IPFSError("requests is not available to use the IPFS HTTP API")
    url = f"{api_url}/add"
    params = {
        "cid-version": str(cid_version),
        "hash": "sha2-256",
        "pin": "true" if pin else "false",
        "wrap-with-directory": "false",
        "quieter": "true",
    }
    files = {"file": (filename, data)}
    resp = _requests.post(url, params=params, files=files, timeout=30)
    resp.raise_for_status()
    lines = [ln for ln in resp.text.strip().splitlines() if ln.strip()]
    last = json.loads(lines[-1])
    cid = last.get("Hash") or last.get("Cid") or last.get("Key")
    if not cid:
        raise IPFSError(f"IPFS API /add returned no CID: {last}")
    return str(cid)


def _add_via_cli(data: bytes, filename_hint: str, *, pin: bool = True, cid_version: int = 1) -> str:
    if not shutil.which(IPFS_CMD):
        raise IPFSError("ipfs CLI not found on PATH")
    tmpdir = tempfile.mkdtemp(prefix="ipfs_add_")
    try:
        path = Path(tmpdir) / (filename_hint or "data.json")
        path.write_bytes(data)
        cmd = [
            IPFS_CMD,
            "add",
            "-Q",
            "--cid-version",
            str(cid_version),
            "--hash",
            "sha2-256",
            "--pin",
            "true" if pin else "false",
            str(path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        cid = res.stdout.strip().splitlines()[-1].strip()
        if not cid:
            raise IPFSError(f"ipfs add returned empty CID: {res.stdout} {res.stderr}")
        return cid
    except subprocess.CalledProcessError as e:
        raise IPFSError(f"ipfs add CLI failed: {e.stderr or e.stdout}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ipfs_add_bytes(
    data: bytes,
    *,
    filename: str = "commit.json",
    api_url: Optional[str] = None,
    pin: bool = True,
    prefer: str = "api",  # "api" or "cli"
) -> str:
    api = (api_url or DEFAULT_API_URL).rstrip("/") if (api_url or DEFAULT_API_URL) else ""
    last_err: Optional[Exception] = None
    for method in ([prefer, "api", "cli"] if prefer == "api" else [prefer, "cli", "api"]):
        try:
            if method == "api" and api:
                return _post_add_via_api(data, filename, api, pin=pin)
            if method == "cli":
                return _add_via_cli(data, filename, pin=pin)
        except Exception as e:
            last_err = e
            continue
    raise IPFSError(f"Failed to add to IPFS via all methods: {last_err}")


def ipfs_add_json(
    obj: Any,
    *,
    filename: str = "commit.json",
    api_url: Optional[str] = None,
    pin: bool = True,
    sort_keys: bool = True,
) -> Tuple[str, str, int]:
    text = minidumps(obj, sort_keys=sort_keys)
    b = text.encode("utf-8")
    cid = ipfs_add_bytes(b, filename=filename, api_url=api_url, pin=pin)
    return cid, sha256_hex(b), len(b)


# --------------------------- cat / get json ---------------------------

def ipfs_cat(
    cid: str,
    *,
    api_url: Optional[str] = None,
    gateways: Optional[Sequence[str]] = None,
    prefer: str = "api",
    timeout: float = 20.0,
) -> bytes:
    last_err: Optional[Exception] = None
    api = (api_url or DEFAULT_API_URL).rstrip("/") if (api_url or DEFAULT_API_URL) else ""

    # HTTP API
    if prefer == "api" and api and _HAVE_REQUESTS:
        try:
            url = f"{api}/cat"
            resp = _requests.post(url, params={"arg": cid}, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            last_err = e

    # CLI fallback
    if shutil.which(IPFS_CMD):
        res = subprocess.run([IPFS_CMD, "cat", cid], capture_output=True)
        if res.returncode == 0:
            return res.stdout
        last_err = IPFSError(f"ipfs cat failed: {res.stderr.decode('utf-8', 'ignore')}")

    # Gateway fallback
    import urllib.request
    for gw in (gateways or DEFAULT_GATEWAYS):
        try:
            with urllib.request.urlopen(f"{gw.rstrip('/')}/{cid}", timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last_err = e
            continue

    raise IPFSError(f"Failed to fetch CID {cid}: {last_err}")


def ipfs_get_json(
    cid: str,
    *,
    api_url: Optional[str] = None,
    gateways: Optional[Sequence[str]] = None,
    expected_sha256_hex: Optional[str] = None,
) -> Tuple[Any, bytes, str]:
    raw = ipfs_cat(cid, api_url=api_url, gateways=gateways)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise IPFSError(f"CID {cid} did not contain JSON: {e}")
    norm = minidumps(obj).encode("utf-8")
    h = sha256_hex(norm)
    if expected_sha256_hex and h.lower() != expected_sha256_hex.lower():
        raise IPFSError(f"Hash mismatch for CID {cid}: expected {expected_sha256_hex}, got {h}")
    return obj, norm, h


# --------------------------- async wrappers ---------------------------

async def aadd_json(
    obj: Any,
    *,
    filename: str = "commit.json",
    api_url: Optional[str] = None,
    pin: bool = True,
    sort_keys: bool = True,
) -> Tuple[str, str, int]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: ipfs_add_json(obj, filename=filename, api_url=api_url, pin=pin, sort_keys=sort_keys)
    )


async def aget_json(
    cid: str,
    *,
    api_url: Optional[str] = None,
    gateways: Optional[Sequence[str]] = None,
    expected_sha256_hex: Optional[str] = None,
) -> Tuple[Any, bytes, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: ipfs_get_json(cid, api_url=api_url, gateways=gateways, expected_sha256_hex=expected_sha256_hex)
    )
