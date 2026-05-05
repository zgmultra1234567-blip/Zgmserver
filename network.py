"""
network.py — WebSocket 브로드캐스트 / 단일 전송
의존: state
순환참조 방지: game.py 를 절대 import 하지 않음
"""
import asyncio, json
from state import rooms


async def broadcast(room_code: str, msg: dict, exclude: str | None = None):
    """room 내 모든 실제 플레이어에게 전송 (json.dumps 1회)"""
    rs = rooms.get(room_code)
    if not rs:
        return
    data = json.dumps(msg, ensure_ascii=False)

    async def _send(ws):
        try:
            await ws.send_text(data)
        except:
            pass

    targets = [
        p["ws"]
        for uid, p in list(rs.players.items())
        if not p.get("isAI") and uid != exclude
    ]
    if targets:
        await asyncio.gather(*[_send(ws) for ws in targets], return_exceptions=True)


async def send_to(room_code: str, uid: str, msg: dict):
    """특정 플레이어 1명에게 전송"""
    rs = rooms.get(room_code)
    if not rs:
        return
    p = rs.players.get(uid)
    if not p or p.get("isAI"):
        return
    try:
        await p["ws"].send_text(json.dumps(msg, ensure_ascii=False))
    except:
        pass
