"""
storage.py — Redis / Firebase RTDB I/O
의존: config
"""
import asyncio, json
from typing import Any, Optional
from config import (
    RTDB_URL, RTDB_SECRET,
    REDIS_URL, REDIS_TOKEN,
    SESSION_TTL,
)

try:
    import httpx
    _USE_HTTPX = True
except ImportError:
    _USE_HTTPX = False

# 앱 시작 시 main.py 에서 주입
_http_client: Optional[Any] = None
_mem_store: dict = {}


def set_http_client(client):
    global _http_client
    _http_client = client


# ── HTTP 저수준 ───────────────────────────────────────────────────────────────
async def _http_get(url: str):
    try:
        if _USE_HTTPX and _http_client:
            r = await _http_client.get(url)
            return r.json() if r.status_code == 200 else None
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, lambda: ur.urlopen(url, timeout=5).read())
        return json.loads(raw)
    except Exception as e:
        print(f"[GET 오류] {e}", flush=True)
        return None


async def _http_patch(url: str, data: dict) -> bool:
    try:
        body = json.dumps(data)
        if _USE_HTTPX and _http_client:
            r = await _http_client.patch(url, content=body, headers={"Content-Type": "application/json"})
            return r.status_code == 200
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        req = ur.Request(url, data=body.encode(), headers={"Content-Type": "application/json"}, method="PATCH")
        await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
        return True
    except Exception as e:
        print(f"[PATCH 오류] {e}", flush=True)
        return False


async def http_delete(url: str):
    try:
        if _USE_HTTPX and _http_client:
            await _http_client.delete(url)
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            req = ur.Request(url, method="DELETE")
            await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
    except Exception as e:
        print(f"[DELETE 오류] {e}", flush=True)


# ── RTDB ─────────────────────────────────────────────────────────────────────
def _rtdb(path: str) -> str:
    return f"{RTDB_URL}{path}.json?auth={RTDB_SECRET}"


async def rtdb_get(path: str):
    return await _http_get(_rtdb(path))


async def rtdb_patch(path: str, data: dict) -> bool:
    return await _http_patch(_rtdb(path), data)


# ── Redis ─────────────────────────────────────────────────────────────────────
def _redis_headers() -> dict:
    return {"Authorization": f"Bearer {REDIS_TOKEN}", "Content-Type": "application/json"}


async def _redis_cmd(*args):
    if not REDIS_URL:
        return None
    try:
        body = json.dumps(list(args))
        if _USE_HTTPX and _http_client:
            r = await _http_client.post(REDIS_URL, content=body, headers=_redis_headers())
            return r.json().get("result")
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        req = ur.Request(REDIS_URL, data=body.encode(), headers=_redis_headers(), method="POST")
        raw = await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5).read())
        return json.loads(raw).get("result")
    except Exception as e:
        print(f"[Redis 오류] {e}", flush=True)
        return None


async def redis_set(key: str, value, ex: int = SESSION_TTL):
    if not REDIS_URL:
        _mem_store[key] = json.dumps(value)
        return "OK"
    return await _redis_cmd("SET", key, json.dumps(value), "EX", ex)


async def redis_get(key: str):
    if not REDIS_URL:
        raw = _mem_store.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except:
            return None
    raw = await _redis_cmd("GET", key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except:
        return None


async def redis_del(key: str):
    if not REDIS_URL:
        _mem_store.pop(key, None)
        return
    await _redis_cmd("DEL", key)


# ── Game State 래퍼 ───────────────────────────────────────────────────────────
def _gs_key(room_code: str) -> str:
    return f"mafia:gs:{room_code}"


async def load_gs(room_code: str) -> dict:
    return (await redis_get(_gs_key(room_code))) or {}


async def save_gs(room_code: str, gs: dict):
    await redis_set(_gs_key(room_code), gs)


async def delete_gs(room_code: str):
    await redis_del(_gs_key(room_code))
