"""
main.py — FastAPI 앱 진입점
의존: config, state, storage, world, network, game
순환참조 없음: 모든 모듈의 최상위 소비자
"""
import asyncio, json, math, time
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, JSONResponse
import uvicorn

try:
    import httpx
    _USE_HTTPX = True
except ImportError:
    _USE_HTTPX = False

from config import (
    WS_PORT, TICK_RATE, LOD_NEAR_DIST,
    BATTERY_PRICE, POTION_PRICE,
    DOORLOCK_HACK_DURATION,
)
from state import rooms, pre_connections
from storage import set_http_client, load_gs, save_gs, rtdb_get, rtdb_patch
from world import validate_move
from network import broadcast, send_to
from game import (
    init_session, send_joined_to_all, send_house_hints,
    phase_loop, position_sync_loop, global_ai_scheduler,
    process_baguette_hit, process_hack,
    dl_payload, get_economy, end_game, check_win,
    lock_all_doors, unlock_all_doors, reset_all_hits,
)

_ai_scheduler_task = None


# ─────────────────────────────────────────────────────────────────────────────
# 루프 시작 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
def _should_start(room_code: str, gs: dict) -> bool:
    rs = rooms.get(room_code)
    if not rs or rs.status != "waiting": return False
    if rs.phase_task and not rs.phase_task.done(): return False
    real = [u for u, p in gs.get("players", {}).items() if not p.get("isAI")]
    conn = [u for u in rs.players if not rs.players[u].get("isAI")]
    return len(real) > 0 and set(real) == set(conn)


def _start_loops(room_code: str):
    rs = rooms[room_code]
    rs.status     = "playing"
    rs.phase_task = asyncio.create_task(phase_loop(room_code))
    rs.pos_task   = asyncio.create_task(position_sync_loop(room_code))


def _ensure_loop(room_code: str) -> bool:
    rs = rooms.get(room_code)
    if not rs or rs.status != "playing": return False
    if rs.phase_task and not rs.phase_task.done(): return False
    _start_loops(room_code); return True


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ai_scheduler_task
    client = None
    if _USE_HTTPX:
        client = httpx.AsyncClient(timeout=5.0)
        set_http_client(client)
    _ai_scheduler_task = asyncio.create_task(global_ai_scheduler())
    print(
        f"[Startup] 브레드 킬러 서버 v9.0 | "
        f"틱={TICK_RATE*1000:.1f}ms | LOD={LOD_NEAR_DIST}m | AI=global_scheduler",
        flush=True,
    )
    yield
    if _ai_scheduler_task and not _ai_scheduler_task.done():
        _ai_scheduler_task.cancel()
    if client:
        await client.aclose()


app = FastAPI(lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP 엔드포인트
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/room/start")
async def http_start(request: Request):
    try:
        body = await request.json()
    except:
        return JSONResponse({"ok": False, "error": "invalid json"}, 400)

    room_code = body.get("room_code", "").upper().strip()
    host_uid  = body.get("host_uid", "")
    room_data = body.get("room_data", {})

    if not room_code:
        return JSONResponse({"ok": False, "error": "room_code required"}, 400)

    rs = rooms.get(room_code)
    if rs:
        if rs.status == "playing": return JSONResponse({"ok": True, "already": True})
        if rs.status == "ended":   return JSONResponse({"ok": False, "error": "game_ended"}, 400)

    if not room_data:
        room_data = await rtdb_get(f"rooms/{room_code}")
        if not room_data:
            return JSONResponse({"ok": False, "error": "room not found"}, 404)

    if room_data.get("hostId", "") != host_uid:
        return JSONResponse({"ok": False, "error": "not host"}, 403)

    gs = await init_session(room_code, room_data, pre_connections)
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "playing"})
    await send_joined_to_all(room_code, gs)

    real = [uid for uid, p in gs["players"].items() if not p.get("isAI")]
    if len(real) == 0 or _should_start(room_code, gs):
        _start_loops(room_code)
        print(f"[HTTP] {room_code} 루프 시작", flush=True)

    return JSONResponse({"ok": True, "room_code": room_code})


@app.get("/room/{room_code}/state")
async def http_state(room_code: str):
    gs = await load_gs(room_code.upper())
    if not gs:
        return JSONResponse({"ok": False, "exists": False})
    return JSONResponse({
        "ok": True, "phase": gs.get("phase"),
        "day": gs.get("day", 1), "alive": gs.get("alive", {}),
    })


@app.get("/")
async def health():
    return {
        "status": "ok", "version": "v9.0",
        "tick_ms": round(TICK_RATE * 1000, 1),
        "lod_dist": LOD_NEAR_DIST,
        "sessions": len(rooms),
        "rooms": list(rooms.keys()),
        "httpx": _USE_HTTPX,
        "move_validation": True,
        "broadcast_batching": True,
        "ai_global_scheduler": True,
    }


@app.head("/")
async def health_head():
    return Response(status_code=200)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/ws/mafia")
async def ws_mafia(ws: WebSocket):
    await ws.accept()
    uid = None
    room_code = None

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except:
                continue
            t = msg.get("t", "")

            # ── join ──────────────────────────────────────────────────────────
            if t == "join":
                uid       = msg.get("uid", "")
                room_code = msg.get("room_code", "").upper().strip()

                rs = rooms.get(room_code)
                if not rs:
                    pre_connections.setdefault(room_code, {})[uid] = ws
                    print(f"[PreConn] {room_code}/{uid} 대기", flush=True)
                    await ws.send_text(json.dumps({"t": "waiting", "r": "session_not_ready"}))
                    continue

                gs = await load_gs(room_code)
                if uid not in gs.get("players", {}):
                    await ws.send_text(json.dumps({"t": "error", "r": "not_in_room"}))
                    continue

                pinfo = gs["players"][uid]
                ex = rs.players.get(uid)
                if ex and ex.get("ws") is not ws:
                    try: await ex["ws"].close()
                    except: pass

                rs.players[uid] = {
                    "ws": ws, "nickname": pinfo["nickname"], "tag": pinfo["tag"],
                    "isHost": pinfo.get("isHost", False), "isAI": False,
                }

                my_role  = gs["roles"].get(uid, "citizen")
                ha       = gs.get("house_assignments", {})
                my_house = ha.get(uid, "")
                hi       = rs.doorlocks.get(my_house, {})
                eco      = get_economy(gs, uid)

                await ws.send_text(json.dumps({
                    "t": "joined", "uid": uid, "role": my_role,
                    "roles": gs["roles"], "phase": gs["phase"], "day": gs["day"],
                    "alive": gs["alive"], "players": gs["players"],
                    "tie_pool": gs["tie_pool"],
                    "house_assignments": ha, "my_house_id": my_house, "my_house_info": hi,
                    "doorlock_states": dl_payload(room_code),
                    "economy": eco,
                    "mafia_team": {pid: r for pid, r in gs["roles"].items() if r == "mafia"}
                        if my_role == "mafia" else {},
                }, ensure_ascii=False))

                if _should_start(room_code, gs):
                    _start_loops(room_code)
                elif _ensure_loop(room_code):
                    print(f"[WS] {room_code} 루프 재시작", flush=True)

                if my_house and hi:
                    all_hids = list(rs.doorlocks.keys())
                    num = all_hids.index(my_house) + 1 if my_house in all_hids else "?"
                    await send_to(room_code, uid, {
                        "t": "house_hint", "house_id": my_house, "house_num": num,
                        "world_x": hi.get("world_x", 0), "world_z": hi.get("world_z", 0),
                        "msg": f"당신의 집은 {num}번 집입니다! 집 위치를 확인하세요.",
                    })

                await broadcast(room_code, {
                    "t": "player_joined", "uid": uid,
                    "player_data": {
                        "nickname": pinfo["nickname"], "tag": pinfo["tag"],
                        "isAI": False, "isHost": pinfo.get("isHost", False),
                    },
                }, exclude=uid)

            # ── pos_update ────────────────────────────────────────────────────
            elif t == "pos_update":
                if not (uid and room_code): continue
                rs = rooms.get(room_code)
                if not rs: continue

                raw_x = float(msg.get("x", 0))
                raw_z = float(msg.get("z", 0))
                accepted, corr_x, corr_z = validate_move(room_code, uid, raw_x, raw_z)

                old = rs.positions.get(uid)
                if old:
                    rs.prev_positions[uid] = dict(old)

                rs.positions[uid] = {
                    "x": round(corr_x, 3), "y": float(msg.get("y", 0)),
                    "z": round(corr_z, 3), "rot_y": float(msg.get("rot_y", 0)),
                    "anim": str(msg.get("anim", "idle")),
                }

                if not accepted or abs(corr_x - raw_x) > 0.05 or abs(corr_z - raw_z) > 0.05:
                    try:
                        await ws.send_text(json.dumps({
                            "t": "pos_correction",
                            "x": round(corr_x, 3), "z": round(corr_z, 3),
                        }))
                    except: pass

            # ── baguette_hit ──────────────────────────────────────────────────
            elif t in ("baguette_hit", "baguette_attack"):
                if not (uid and room_code and room_code in rooms): continue
                target_uid = msg.get("target", "")
                if target_uid:
                    await process_baguette_hit(room_code, uid, target_uid)

            # ── doorlock_toggle ───────────────────────────────────────────────
            elif t == "doorlock_toggle":
                if not (uid and room_code): continue
                rs = rooms.get(room_code)
                if not rs: continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                hid   = gs.get("house_assignments", {}).get(uid, "")
                house = rs.doorlocks.get(hid)
                if not house:
                    await send_to(room_code, uid, {"t": "doorlock_result", "ok": False, "r": "no_house"}); continue
                if house["owner"] != uid:
                    await send_to(room_code, uid, {"t": "doorlock_result", "ok": False, "r": "not_owner"}); continue
                new_state = not house["is_locked"]
                house["is_locked"] = new_state
                house["hack_success"] = False
                await send_to(room_code, uid, {
                    "t": "doorlock_result", "ok": True, "house_id": hid,
                    "is_locked": new_state, "action": "lock" if new_state else "unlock",
                })
                await broadcast(room_code, {"t": "doorlock_state", "house_id": hid, "is_locked": new_state}, exclude=uid)

            # ── hack_start ────────────────────────────────────────────────────
            elif t == "hack_start":
                if not (uid and room_code): continue
                rs = rooms.get(room_code)
                if not rs: continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid) or gs.get("phase") != "night": continue
                if gs["roles"].get(uid) != "mafia": continue
                thid  = msg.get("house_id", "")
                house = rs.doorlocks.get(thid)
                if not house or not house["is_locked"] or house["hack_in_progress"]: continue
                mp = rs.positions.get(uid, {})
                dx = mp.get("x", 0) - house["world_x"]
                dz = mp.get("z", 0) - house["world_z"]
                if math.sqrt(dx*dx + dz*dz) > 4.0:
                    await send_to(room_code, uid, {"t": "hack_failed", "r": "too_far"}); continue
                house["hack_in_progress"] = True
                house["hacker_uid"] = uid
                house["hack_start"] = time.time()
                await send_to(room_code, uid, {"t": "hack_started", "house_id": thid, "duration": DOORLOCK_HACK_DURATION})
                await send_to(room_code, house["owner"], {"t": "hack_alert", "house_id": thid})
                asyncio.create_task(process_hack(room_code, uid, thid))

            # ── hack_cancel ───────────────────────────────────────────────────
            elif t == "hack_cancel":
                if not (uid and room_code): continue
                rs = rooms.get(room_code)
                if not rs: continue
                hid   = msg.get("house_id", "")
                house = rs.doorlocks.get(hid)
                if house and house.get("hacker_uid") == uid:
                    house["hack_in_progress"] = False
                    house["hacker_uid"] = ""
                    await send_to(room_code, uid, {"t": "hack_cancelled", "house_id": hid})

            # ── shop_buy ──────────────────────────────────────────────────────
            elif t == "shop_buy":
                if not (uid and room_code): continue
                item = msg.get("item", "")
                gs   = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                eco = get_economy(gs, uid)
                price = BATTERY_PRICE if item == "battery" else POTION_PRICE if item == "potion" else None
                if price is None: continue
                if eco["bread"] >= price:
                    eco["bread"] -= price
                    if item == "battery":
                        eco["batteries"] += 1
                        ok_msg = f"배터리 구매 완료! 남은 브래드: {eco['bread']}🍞"
                    else:
                        eco["potions"] = eco.get("potions", 0) + 1
                        ok_msg = f"🧪 마법의 약 구매 완료! 남은 브래드: {eco['bread']}🍞"
                    gs.setdefault("economy", {})[uid] = eco
                    await save_gs(room_code, gs)
                    await send_to(room_code, uid, {
                        "t": "shop_result", "ok": True, "item": item,
                        "bread": eco["bread"], "batteries": eco["batteries"],
                        "potions": eco.get("potions", 0), "msg": ok_msg,
                    })
                else:
                    await send_to(room_code, uid, {
                        "t": "shop_result", "ok": False, "item": item,
                        "bread": eco["bread"], "batteries": eco["batteries"],
                        "potions": eco.get("potions", 0),
                        "msg": f"브래드 부족! (필요:{price}, 보유:{eco['bread']})",
                    })

            # ── flashlight_dead ───────────────────────────────────────────────
            elif t == "flashlight_dead":
                if not (uid and room_code): continue
                gs  = await load_gs(room_code)
                eco = get_economy(gs, uid)
                if eco["batteries"] > 0:
                    eco["batteries"] -= 1
                    gs.setdefault("economy", {})[uid] = eco
                    await save_gs(room_code, gs)
                    msg_text = "배터리가 모두 소진됐습니다!" if eco["batteries"] == 0 else f"배터리 남은 수: {eco['batteries']}"
                    await send_to(room_code, uid, {
                        "t": "economy_update", "bread": eco["bread"],
                        "batteries": eco["batteries"], "msg": msg_text,
                    })

            # ── night_action ──────────────────────────────────────────────────
            elif t == "night_action":
                if not (uid and room_code and room_code in rooms): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid) or gs["phase"] != "night": continue
                my_role = gs["roles"].get(uid, "")
                action  = msg.get("action", "")
                target  = msg.get("target", "")
                fm = {
                    "mafia_kill":          ("night_votes",        "mafia"),
                    "doctor_protect":      ("doctor_protect",     "doctor"),
                    "police_investigate":  ("police_investigate", "police"),
                }
                if action in fm:
                    field, req = fm[action]
                    if my_role == req:
                        gs[field][uid] = target
                        await save_gs(room_code, gs)
                        await ws.send_text(json.dumps({"t": "action_ok", "action": action}))

            # ── day_vote ──────────────────────────────────────────────────────
            elif t == "day_vote":
                if not (uid and room_code and room_code in rooms): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid) or gs["phase"] != "vote": continue
                gs["day_votes"][uid] = msg.get("target", "")
                await save_gs(room_code, gs)
                await ws.send_text(json.dumps({"t": "vote_ok"}))

            # ── chat ──────────────────────────────────────────────────────────
            elif t == "chat":
                if not (uid and room_code and room_code in rooms): continue
                gs      = await load_gs(room_code)
                channel = msg.get("channel", "general")
                text    = msg.get("text", "").strip()
                if not text: continue
                if gs["phase"] == "night" and channel == "general": continue
                my_role = gs["roles"].get(uid, "")
                if channel == "mafia" and my_role != "mafia": continue
                pi = gs["players"].get(uid, {})
                cm = {
                    "t": "chat", "channel": channel, "uid": uid,
                    "nickname": pi.get("nickname", "?"), "tag": pi.get("tag", "0000"),
                    "text": text, "ts": int(time.time() * 1000),
                }
                if channel == "general":
                    await broadcast(room_code, cm)
                else:
                    for pid, r in gs["roles"].items():
                        if r == "mafia":
                            await send_to(room_code, pid, cm)

            # ── emotion ───────────────────────────────────────────────────────
            elif t == "emotion":
                if not (uid and room_code): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                await broadcast(room_code, {"t": "emotion", "uid": uid, "emotion": msg.get("emotion", "")}, exclude=uid)

            elif t == "ping":
                await ws.send_text(json.dumps({"t": "pong"}))

            elif t == "leave":
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS 오류] uid={uid}: {e}", flush=True)
    finally:
        if uid and room_code:
            if room_code in pre_connections:
                pre_connections[room_code].pop(uid, None)
            rs = rooms.get(room_code)
            if rs:
                rs.players.pop(uid, None)
            await broadcast(room_code, {"t": "player_left", "uid": uid})
        print(f"[WS] 해제: {uid}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from config import RTDB_SECRET, REDIS_URL, REDIS_TOKEN
    for var, name in [(RTDB_SECRET, "RTDB_SECRET"), (REDIS_URL, "REDIS_URL"), (REDIS_TOKEN, "REDIS_TOKEN")]:
        if not var:
            print(f"[경고] {name} 없음", flush=True)
    print(f"[Server] 포트 {WS_PORT} | 15Hz | LOD={LOD_NEAR_DIST}m | v9.0", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=WS_PORT, ws_ping_interval=20, ws_ping_timeout=30)
