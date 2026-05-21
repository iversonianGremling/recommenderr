import os
import httpx
from typing import Optional

_INVIDIOUS_URL_DEFAULT = os.getenv("INVIDIOUS_URL", "http://192.168.1.173:3000")

_client: Optional[httpx.AsyncClient] = None
_client_base_url: str = ""


def get_client() -> httpx.AsyncClient:
    global _client, _client_base_url
    from backend.services.source_registry import get_credential
    url = get_credential("invidious", "INVIDIOUS_URL") or _INVIDIOUS_URL_DEFAULT
    if _client is None or _client.is_closed or url != _client_base_url:
        _client = httpx.AsyncClient(base_url=url, timeout=30.0)
        _client_base_url = url
    return _client


async def api_get(path: str, params: dict = None, token: str = None, timeout: float = None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        kwargs = {"params": params, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await get_client().get(f"/api/v1{path}", **kwargs)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise Exception(f"Invidious returned {e.response.status_code} for {path}")
    except httpx.TimeoutException:
        raise Exception(f"Invidious timed out for {path}")
    except httpx.ConnectError:
        raise Exception(f"Cannot connect to Invidious at {INVIDIOUS_URL}")


async def api_post(path: str, json: dict = None, token: str = None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = await get_client().post(f"/api/v1{path}", json=json, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise Exception(f"Invidious returned {e.response.status_code} for {path}")
    except httpx.TimeoutException:
        raise Exception(f"Invidious timed out for {path}")
    except httpx.ConnectError:
        raise Exception(f"Cannot connect to Invidious at {INVIDIOUS_URL}")


async def api_delete(path: str, token: str = None) -> httpx.Response:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = await get_client().delete(f"/api/v1{path}", headers=headers)
        resp.raise_for_status()
        return resp
    except httpx.HTTPStatusError as e:
        raise Exception(f"Invidious returned {e.response.status_code} for {path}")
    except httpx.TimeoutException:
        raise Exception(f"Invidious timed out for {path}")
    except httpx.ConnectError:
        raise Exception(f"Cannot connect to Invidious at {INVIDIOUS_URL}")
