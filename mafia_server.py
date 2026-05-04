"""
mafia_server.py — 브레드 킬러 서버 v9.0
[v9.0 수정사항]
  1. 이동 검증 (속도/텔레포트 차단)
     → pos_update 수신 시 최대 이동 거리 검증
     → 비정상 이동 감지 시 경고 누적 + 위치 강제 보정
  2. Broadcast batching
     → broadcast() 내부 json.dumps 1회만 실행
     → asyncio.gather 병렬 전송 유지
  3. AI 공용 스케줄러 (global AI pool)
     → room당 ai_move_task 대신 전역 ai_scheduler_task 1개
     → 모든 room의 AI를 단일 루프에서 처리
  4. RoomState 캡슐화
     → 전역 dict 난립 → RoomState 객체로 통합
     → sessions / player_positions / doorlock_states 등 단일 객체 관리
  5. 나머지 게임 로직은 v8.7 그대로 유지
"""
import asyncio, json, os, random, time, math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, JSONResponse
import uvicorn

try:
    import httpx
    _USE_HTTPX = True
except ImportError:
    _USE_HTTPX = False

RTDB_URL    = "https://zgm-base-default-rtdb.asia-southeast1.firebasedatabase.app/"
RTDB_SECRET = os.environ.get("RTDB_SECRET", "")
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
WS_PORT     = int(os.environ.get("PORT", 7860))

PHASE_TIMES = {
    "meet":    60,
    "night":   120,
    "morning": 15,
    "day":     180,
    "dusk":    20,
}
PHASE_CYCLE = ["night", "morning", "day", "dusk"]
MAX_ROUNDS  = 10
SESSION_TTL = 3600

TICK_RATE              = 1.0 / 15.0
POSITION_SYNC_INTERVAL = TICK_RATE
AI_MOVE_UPDATE_RATE    = TICK_RATE

DOORLOCK_HACK_DURATION = 15.0
DOORLOCK_SUCCESS_RATE  = 0.67

BAGUETTE_RANGE         = 2.5
BAGUETTE_HIT_COOLDOWN  = 0.8
BAGUETTE_HITS_TO_KILL  = 2
BAGUETTE_HIT_RESET     = 8.0

MORNING_BREAD          = 500
BATTERY_PRICE          = 500
BATTERY_DURATION_SEC   = 60.0
POTION_PRICE           = 700
POTION_DURATION        = 20.0

AI_IDLE_CHANCE       = 0.05
AI_MOVE_SPEED        = 3.5
AI_ACTION_DELAY_MIN  = 3
AI_ACTION_DELAY_MAX  = 10

AI_COSINE_AMPLITUDE  = 1.8
AI_COSINE_FREQUENCY  = 0.9

LOD_NEAR_DIST = 60.0
WORLD_BOUND   = 48.0
HOUSE_SPACING = 14.0

# ★ v9.0: 이동 검증 상수
# 서버 틱당 최대 이동 가능 거리 = 속도 * 틱간격 * 여유배수(2.5)
PLAYER_MAX_SPEED         = 6.0          # 클라 최대 이동속도 (유니티 단위/초)
PLAYER_MAX_DIST_PER_TICK = PLAYER_MAX_SPEED * TICK_RATE * 2.5  # 여유 2.5배
MAX_TELEPORT_DIST        = 8.0          # 이 이상이면 즉시 텔레포트 의심
MOVE_WARN_THRESHOLD      = 5            # 경고 누적 횟수 초과 시 강제 보정
MOVE_WARN_DECAY_SEC      = 10.0         # 경고 decay 간격

_HOUSE_CENTERS = []
for _r in (-1, 0, 1):
    for _c in (-1, 0, 1):
        if _r == 0 and _c == 0:
            continue
        _HOUSE_CENTERS.append((_c * 14.0, _r * 14.0))
for _v in [(-28,-14),(28,-14),(-28,14),(28,14),(-14,-28),(14,-28),(-14,28),(14,28)]:
    _HOUSE_CENTERS.append(_v)

HOUSE_HALF    = 2.8
DOOR_OFFSET_Z = 2.52

print(
    f"=== 브레드 킬러 서버 v9.0 | 틱={TICK_RATE*1000:.1f}ms(15Hz) | "
    f"LOD={LOD_NEAR_DIST}m | httpx={_USE_HTTPX} | redis={bool(REDIS_URL)} ===",
    flush=True
)

# ---------------------------------------------------------------------------
# ★ v9.0: RoomState 데이터 클래스 — 전역 dict 난립 대신 단일 객체로 관리
# ---------------------------------------------------------------------------
@dataclass
class RoomState:
    status: str = "waiting"                  # waiting / playing / ended
    players: dict = field(default_factory=dict)  # uid -> {ws, nickname, tag, isHost, isAI}
    positions: dict = field(default_factory=dict) # uid -> {x,y,z,rot_y,anim}
    prev_positions: dict = field(default_factory=dict)
    doorlocks: dict = field(default_factory=dict) # hid -> {...}
    attack_cooldowns: dict = field(default_factory=dict)
    baguette_hits: dict = field(default_factory=dict)
    ai_move_targets: dict = field(default_factory=dict)
    move_warnings: dict = field(default_factory=dict) # uid -> {count, last_ts}
    phase_task: Optional[asyncio.Task] = None
    pos_task: Optional[asyncio.Task] = None

# 전역 room 저장소
rooms: dict[str, RoomState] = {}

_mem_store:  dict = {}
_http_client       = None
pre_connections: dict = {}

# ---------------------------------------------------------------------------
# HTTP 유틸
# ---------------------------------------------------------------------------
async def _http_get(url):
    try:
        if _USE_HTTPX and _http_client:
            r = await _http_client.get(url)
            return r.json() if r.status_code == 200 else None
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, lambda: ur.urlopen(url, timeout=5).read())
        return json.loads(raw)
    except Exception as e:
        print(f"[GET 오류] {e}", flush=True); return None

async def _http_patch(url, data):
    try:
        body = json.dumps(data)
        if _USE_HTTPX and _http_client:
            r = await _http_client.patch(url, content=body, headers={"Content-Type":"application/json"})
            return r.status_code == 200
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        req = ur.Request(url, data=body.encode(), headers={"Content-Type":"application/json"}, method="PATCH")
        await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
        return True
    except Exception as e:
        print(f"[PATCH 오류] {e}", flush=True); return False

async def _http_delete(url):
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

def _rtdb(path): return f"{RTDB_URL}{path}.json?auth={RTDB_SECRET}"
async def rtdb_get(path):      return await _http_get(_rtdb(path))
async def rtdb_patch(path, d): return await _http_patch(_rtdb(path), d)

# ---------------------------------------------------------------------------
# Redis / 메모리 저장소
# ---------------------------------------------------------------------------
def _redis_headers():
    return {"Authorization": f"Bearer {REDIS_TOKEN}", "Content-Type": "application/json"}

async def _redis_cmd(*args):
    if not REDIS_URL: return None
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
        print(f"[Redis 오류] {e}", flush=True); return None

async def redis_set(key, value, ex=SESSION_TTL):
    if not REDIS_URL:
        _mem_store[key] = json.dumps(value); return "OK"
    return await _redis_cmd("SET", key, json.dumps(value), "EX", ex)

async def redis_get(key):
    if not REDIS_URL:
        raw = _mem_store.get(key)
        if raw is None: return None
        try: return json.loads(raw)
        except: return None
    raw = await _redis_cmd("GET", key)
    if raw is None: return None
    try: return json.loads(raw)
    except: return None

async def redis_del(key):
    if not REDIS_URL: _mem_store.pop(key, None); return
    await _redis_cmd("DEL", key)

def _gs_key(rc): return f"mafia:gs:{rc}"
async def load_gs(rc): return (await redis_get(_gs_key(rc))) or {}
async def save_gs(rc, gs): await redis_set(_gs_key(rc), gs)

# ---------------------------------------------------------------------------
# 역할 계산
# ---------------------------------------------------------------------------
def calc_roles(total):
    c = {"mafia": 0, "police": 0, "doctor": 0, "citizen": 0}
    if total <= 3:
        c["mafia"] = 1; c["citizen"] = total - 1
    elif total <= 5:
        c["mafia"] = 1; c["police"] = 1; c["citizen"] = total - 2
    elif total <= 7:
        c["mafia"] = 1; c["police"] = 1; c["doctor"] = 1; c["citizen"] = total - 3
    elif total <= 9:
        c["mafia"] = 2; c["police"] = 1; c["doctor"] = 1; c["citizen"] = total - 4
    else:
        c["mafia"] = 3; c["police"] = 1; c["doctor"] = 1; c["citizen"] = total - 5
    return c

# ---------------------------------------------------------------------------
# 물리/월드 유틸
# ---------------------------------------------------------------------------
def _clamp_to_world(x: float, z: float) -> tuple:
    return max(-WORLD_BOUND, min(WORLD_BOUND, x)), max(-WORLD_BOUND, min(WORLD_BOUND, z))

def _is_inside_house(x: float, z: float) -> bool:
    for hx, hz in _HOUSE_CENTERS:
        if abs(x - hx) < HOUSE_HALF and abs(z - hz) < HOUSE_HALF:
            return True
    return False

def _push_out_of_houses(x: float, z: float) -> tuple:
    for hx, hz in _HOUSE_CENTERS:
        dx = x - hx; dz = z - hz
        if abs(dx) < HOUSE_HALF and abs(dz) < HOUSE_HALF:
            push_x = HOUSE_HALF - abs(dx) + 0.1
            push_z = HOUSE_HALF - abs(dz) + 0.1
            if push_x < push_z:
                x = hx + math.copysign(HOUSE_HALF + 0.1, dx)
            else:
                z = hz + math.copysign(HOUSE_HALF + 0.1, dz) if dz != 0 else hz + HOUSE_HALF + 0.1
    return x, z

def _safe_move(cur_x, cur_z, target_x, target_z, step):
    dx = target_x - cur_x; dz = target_z - cur_z
    dist = math.sqrt(dx*dx + dz*dz)
    if dist < 0.01: return cur_x, cur_z
    ratio = min(step / dist, 1.0)
    nx = cur_x + dx * ratio; nz = cur_z + dz * ratio
    if _is_inside_house(nx, nz):
        nx, nz = _push_out_of_houses(nx, nz)
    return _clamp_to_world(nx, nz)

def _safe_target(tx, tz):
    tx, tz = _clamp_to_world(tx, tz)
    if _is_inside_house(tx, tz):
        tx, tz = _push_out_of_houses(tx, tz)
    return tx, tz

# ---------------------------------------------------------------------------
# ★ v9.0: 이동 검증
# ---------------------------------------------------------------------------
def _validate_move(room_code: str, uid: str, new_x: float, new_z: float) -> tuple[bool, float, float]:
    """
    클라가 보낸 위치를 검증.
    반환: (accepted, corrected_x, corrected_z)
    - 소폭 이상 → 경고 누적, 보정 좌표 반환
    - 텔레포트 수준 → 즉시 거부, 이전 위치 반환
    """
    rs = rooms.get(room_code)
    if not rs:
        return True, new_x, new_z

    prev = rs.positions.get(uid)
    if not prev:
        return True, new_x, new_z  # 첫 위치는 그냥 수용

    px, pz = prev.get("x", 0.0), prev.get("z", 0.0)
    dx = new_x - px; dz = new_z - pz
    dist = math.sqrt(dx*dx + dz*dz)

    # 텔레포트 수준 이동 → 즉시 거부
    if dist > MAX_TELEPORT_DIST:
        now = time.time()
        warn = rs.move_warnings.setdefault(uid, {"count": 0, "last_ts": now})
        # decay
        if now - warn["last_ts"] > MOVE_WARN_DECAY_SEC:
            warn["count"] = max(0, warn["count"] - 1)
        warn["count"] += 1
        warn["last_ts"] = now
        if warn["count"] > MOVE_WARN_THRESHOLD:
            print(f"[MoveHack] {uid} 반복 텔레포트 의심 (dist={dist:.2f}, warn={warn['count']})", flush=True)
        return False, px, pz  # 이전 위치 유지

    # 속도 초과 이동 → 경고 누적, 보정
    if dist > PLAYER_MAX_DIST_PER_TICK:
        now = time.time()
        warn = rs.move_warnings.setdefault(uid, {"count": 0, "last_ts": now})
        if now - warn["last_ts"] > MOVE_WARN_DECAY_SEC:
            warn["count"] = max(0, warn["count"] - 1)
        warn["count"] += 1
        warn["last_ts"] = now
        # 보정: 방향은 유지하되 최대 허용 거리로 클램프
        ratio  = PLAYER_MAX_DIST_PER_TICK / dist
        corr_x = px + dx * ratio
        corr_z = pz + dz * ratio
        corr_x, corr_z = _clamp_to_world(corr_x, corr_z)
        if warn["count"] > MOVE_WARN_THRESHOLD:
            print(f"[SpeedHack] {uid} 속도초과 보정 (dist={dist:.2f} > max={PLAYER_MAX_DIST_PER_TICK:.2f})", flush=True)
        return True, corr_x, corr_z

    return True, new_x, new_z

# ---------------------------------------------------------------------------
# ★ v9.0: Broadcast batching — json.dumps 1회
# ---------------------------------------------------------------------------
async def broadcast(room_code: str, msg: dict, exclude=None):
    rs = rooms.get(room_code)
    if not rs: return
    data = json.dumps(msg, ensure_ascii=False)  # ★ 1회만 직렬화

    async def _s(ws):
        try: await ws.send_text(data)
        except: pass

    targets = [
        p["ws"]
        for uid, p in list(rs.players.items())
        if not p.get("isAI") and uid != exclude
    ]
    if targets:
        await asyncio.gather(*[_s(ws) for ws in targets], return_exceptions=True)

async def send_to(room_code: str, uid: str, msg: dict):
    rs = rooms.get(room_code)
    if not rs: return
    p = rs.players.get(uid)
    if not p or p.get("isAI"): return
    try: await p["ws"].send_text(json.dumps(msg, ensure_ascii=False))
    except: pass

# ---------------------------------------------------------------------------
# 바게트 히트 유틸
# ---------------------------------------------------------------------------
def _get_hit_state(rs: RoomState, killer: str, target: str) -> dict:
    rs.baguette_hits.setdefault(killer, {})
    rs.baguette_hits[killer].setdefault(target, {"count": 0, "last_hit": 0.0})
    return rs.baguette_hits[killer][target]

def _reset_hit_state(rs: RoomState, killer: str, target: str):
    try: rs.baguette_hits[killer].pop(target, None)
    except KeyError: pass

def _reset_all_hits(rs: RoomState):
    rs.baguette_hits.clear()

# ---------------------------------------------------------------------------
# 바게트 공격 처리
# ---------------------------------------------------------------------------
async def process_baguette_hit(room_code: str, killer_uid: str, target_uid: str) -> bool:
    rs = rooms.get(room_code)
    if not rs: return False
    gs = await load_gs(room_code)
    if not gs: return False

    if gs.get("phase") != "night":
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "not_night"}); return False
    if gs["roles"].get(killer_uid) != "mafia":
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "not_mafia"}); return False
    if not gs["alive"].get(killer_uid): return False
    if not gs["alive"].get(target_uid):
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "target_dead"}); return False

    mp = rs.positions.get(killer_uid, {})
    tp = rs.positions.get(target_uid, {})
    if not mp or not tp:
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "no_position"}); return False

    dist = math.sqrt((mp["x"]-tp["x"])**2 + (mp["z"]-tp["z"])**2)
    if dist > BAGUETTE_RANGE:
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "too_far", "dist": round(dist,2)}); return False

    now      = time.time()
    last_atk = rs.attack_cooldowns.get(killer_uid, 0.0)
    if now - last_atk < BAGUETTE_HIT_COOLDOWN: return False
    rs.attack_cooldowns[killer_uid] = now

    hs = _get_hit_state(rs, killer_uid, target_uid)
    if now - hs["last_hit"] > BAGUETTE_HIT_RESET: hs["count"] = 0
    hs["count"] += 1; hs["last_hit"] = now
    hit_count = hs["count"]
    print(f"[Baguette] {killer_uid} -> {target_uid} | {hit_count}/{BAGUETTE_HITS_TO_KILL}타", flush=True)

    await send_to(room_code, target_uid, {
        "t": "baguette_hit_received", "from": killer_uid,
        "hp": BAGUETTE_HITS_TO_KILL - hit_count,
    })

    if hit_count >= BAGUETTE_HITS_TO_KILL:
        _reset_hit_state(rs, killer_uid, target_uid)
        gs["alive"][target_uid] = False
        await save_gs(room_code, gs)
        await broadcast(room_code, {
            "t": "baguette_kill", "killer": killer_uid, "victim": target_uid,
            "pos_x": tp.get("x", 0), "pos_z": tp.get("z", 0),
        })
        print(f"[Baguette] 킬 확정: {killer_uid} -> {target_uid}", flush=True)
        winner = check_win(gs)
        if winner: await end_game(room_code, gs, winner, "baguette")
        return True

    await send_to(room_code, killer_uid, {
        "t": "baguette_hit_ok", "target": target_uid,
        "hit_count": hit_count, "hits_need": BAGUETTE_HITS_TO_KILL,
    })
    return False

# ---------------------------------------------------------------------------
# 속도 계산 (pos_sync용)
# ---------------------------------------------------------------------------
def _calc_velocity(rs: RoomState, uid: str, cur: dict) -> tuple:
    prev = rs.prev_positions.get(uid)
    if not prev: return 0.0, 0.0
    dt = TICK_RATE
    if dt <= 0: return 0.0, 0.0
    return round((cur.get("x",0)-prev.get("x",0))/dt, 3), round((cur.get("z",0)-prev.get("z",0))/dt, 3)

# ---------------------------------------------------------------------------
# ★ v9.0: position_sync_loop — RoomState 기반
# ---------------------------------------------------------------------------
async def position_sync_loop(room_code: str):
    try:
        while room_code in rooms and rooms[room_code].status == "playing":
            await asyncio.sleep(POSITION_SYNC_INTERVAL)
            rs  = rooms.get(room_code)
            if not rs: break
            pos = rs.positions
            if not pos: continue

            for uid, pinfo in list(rs.players.items()):
                if pinfo.get("isAI"): continue
                my_pos = pos.get(uid)

                if not my_pos:
                    try:
                        payload = _build_pos_with_vel(rs, pos)
                        await pinfo["ws"].send_text(
                            json.dumps({"t": "pos_sync", "positions": payload,
                                        "ts": int(time.time()*1000)}, ensure_ascii=False))
                    except: pass
                    continue

                mx, mz = my_pos.get("x",0), my_pos.get("z",0)
                payload = {}
                for other_uid, other_p in pos.items():
                    if other_uid == uid: continue
                    dx  = other_p.get("x",0) - mx
                    dz  = other_p.get("z",0) - mz
                    dist = math.sqrt(dx*dx + dz*dz)
                    if dist <= LOD_NEAR_DIST:
                        vx, vz = _calc_velocity(rs, other_uid, other_p)
                        entry = dict(other_p); entry["vx"] = vx; entry["vz"] = vz
                        payload[other_uid] = entry

                if payload:
                    try:
                        await pinfo["ws"].send_text(
                            json.dumps({"t": "pos_sync", "positions": payload,
                                        "ts": int(time.time()*1000)}, ensure_ascii=False))
                    except: pass

        rs = rooms.get(room_code)
        if rs: rs.prev_positions.clear()
    except asyncio.CancelledError: pass
    except Exception as e:
        print(f"[PosSync] 오류: {e}", flush=True)

def _build_pos_with_vel(rs: RoomState, pos: dict) -> dict:
    result = {}
    for uid, p in pos.items():
        vx, vz = _calc_velocity(rs, uid, p)
        entry = dict(p); entry["vx"] = vx; entry["vz"] = vz
        result[uid] = entry
    return result

# ---------------------------------------------------------------------------
# ★ v9.0: 전역 AI 스케줄러 — room당 task 대신 단일 루프
# ---------------------------------------------------------------------------
_ai_scheduler_task: Optional[asyncio.Task] = None
_ai_local_state: dict = {}   # room_code -> uid -> local_state

async def global_ai_scheduler():
    """
    모든 room의 AI를 단일 루프에서 처리.
    room당 ai_move_task를 생성하지 않아 task 폭발 방지.
    """
    print("[AI-Sched] 전역 AI 스케줄러 시작", flush=True)
    try:
        while True:
            await asyncio.sleep(AI_MOVE_UPDATE_RATE)
            for room_code, rs in list(rooms.items()):
                if rs.status != "playing": continue
                try:
                    await _tick_ai_for_room(room_code, rs)
                except Exception as e:
                    import traceback
                    print(f"[AI-Sched] {room_code} 오류: {e}\n{traceback.format_exc()}", flush=True)
    except asyncio.CancelledError:
        print("[AI-Sched] 종료", flush=True)

async def _tick_ai_for_room(room_code: str, rs: RoomState):
    gs = await load_gs(room_code)
    if not gs: return
    phase       = gs.get("phase", "meet")
    roles       = gs.get("roles", {})
    alive       = gs.get("alive", {})
    players     = gs.get("players", {})
    house_asgn  = gs.get("house_assignments", {})
    night_votes = gs.get("night_votes", {})
    pos_store   = rs.positions
    dl_store    = rs.doorlocks

    _ai_local_state.setdefault(room_code, {})
    local = _ai_local_state[room_code]

    for ai_uid, pinfo in players.items():
        if not pinfo.get("isAI"): continue
        if not alive.get(ai_uid):  continue
        cur = pos_store.get(ai_uid)
        if cur is None: continue

        rs.prev_positions[ai_uid] = dict(cur)

        role = roles.get(ai_uid, "citizen")
        if ai_uid not in local:
            local[ai_uid] = {
                "target_x": 0, "target_z": 0, "timer": 0,
                "chase_uid": "", "spy_house_x": 0, "spy_house_z": 0,
                "protect_uid": "", "stuck_count": 0,
                "cosine_phase": random.uniform(0, math.pi * 2),
                "cosine_travel": 0.0,
            }
        st = local[ai_uid]
        st["timer"] -= AI_MOVE_UPDATE_RATE
        if random.random() < AI_IDLE_CHANCE:
            pos_store[ai_uid]["anim"] = "idle"; continue
        cx = cur.get("x", 0.0); cz = cur.get("z", 0.0)

        if role == "mafia" and phase == "night":
            anim, cx, cz = _ai_mafia_cosine_move(
                ai_uid, cx, cz, alive, roles, house_asgn,
                pos_store, dl_store, room_code, gs, st, rs)
            rot_dx = st["target_x"] - cx; rot_dz = st["target_z"] - cz
            rot_y  = math.atan2(rot_dx, rot_dz)
            pos_store[ai_uid] = {"x": round(cx,3), "y": 0.0, "z": round(cz,3),
                                 "rot_y": round(rot_y,3), "anim": anim}
            continue

        raw_tx, raw_tz = _ai_role_target(
            ai_uid, role, phase, alive, roles, house_asgn,
            pos_store, dl_store, night_votes, st)
        tx, tz = _safe_target(raw_tx, raw_tz)
        st["target_x"] = tx; st["target_z"] = tz
        dx = tx - cx; dz = tz - cz
        dist = math.sqrt(dx*dx + dz*dz)
        step = AI_MOVE_SPEED * AI_MOVE_UPDATE_RATE

        if dist < 0.3:
            anim = "idle"
            if _is_inside_house(cx, cz):
                cx, cz = _push_out_of_houses(cx, cz); st["stuck_count"] = 0
        else:
            prev_cx, prev_cz = cx, cz
            cx, cz = _safe_move(cx, cz, tx, tz, step)
            anim   = "run" if dist > 5.0 else "walk"
            moved  = math.sqrt((cx-prev_cx)**2 + (cz-prev_cz)**2)
            if moved < 0.05:
                st["stuck_count"] = st.get("stuck_count", 0) + 1
                if st["stuck_count"] > 3:
                    ea = random.uniform(0, math.pi*2)
                    st["target_x"], st["target_z"] = _safe_target(
                        cx + math.cos(ea)*2.5, cz + math.sin(ea)*2.5)
                    st["stuck_count"] = 0
            else:
                st["stuck_count"] = 0

        rot_y = math.atan2(dx, dz) if dist > 0.1 else cur.get("rot_y", 0.0)
        pos_store[ai_uid] = {"x": round(cx,3), "y": 0.0, "z": round(cz,3),
                             "rot_y": round(rot_y,3), "anim": anim}

# ---------------------------------------------------------------------------
# AI 마피아 코사인 이동
# ---------------------------------------------------------------------------
async def _do_ai_baguette_hit(room_code, ai_uid, target_uid, cx, cz, gs, st):
    rs = rooms.get(room_code)
    if not rs: return False
    now = time.time()
    last_atk = rs.attack_cooldowns.get(ai_uid, 0)
    if now - last_atk < BAGUETTE_HIT_COOLDOWN: return False
    rs.attack_cooldowns[ai_uid] = now
    hs = _get_hit_state(rs, ai_uid, target_uid)
    if now - hs["last_hit"] > BAGUETTE_HIT_RESET: hs["count"] = 0
    hs["count"] += 1; hs["last_hit"] = now
    print(f"[AI-Hit] {ai_uid}->{target_uid} {hs['count']}/{BAGUETTE_HITS_TO_KILL}", flush=True)
    if hs["count"] >= BAGUETTE_HITS_TO_KILL:
        _reset_hit_state(rs, ai_uid, target_uid)
        gs["alive"][target_uid] = False
        await save_gs(room_code, gs)
        tp = rs.positions.get(target_uid, {})
        await broadcast(room_code, {
            "t": "baguette_kill", "killer": ai_uid, "victim": target_uid,
            "pos_x": tp.get("x", cx), "pos_z": tp.get("z", cz),
        })
        print(f"[AI-킬] {ai_uid} -> {target_uid}", flush=True)
        winner = check_win(gs)
        if winner: await end_game(room_code, gs, winner, "baguette")
        st["chase_uid"] = ""; return True
    return False

def _ai_mafia_cosine_move(ai_uid, cx, cz, alive, roles, house_asgn,
                           pos_store, dl_store, room_code, gs, st, rs):
    alive_others = [u for u, a in alive.items() if a and u != ai_uid]
    if not st.get("chase_uid") or not alive.get(st.get("chase_uid", "")):
        cands = [u for u in alive_others if roles.get(u) != "mafia"]
        st["chase_uid"] = random.choice(cands) if cands else ""
    target_uid = st.get("chase_uid", "")
    step = AI_MOVE_SPEED * AI_MOVE_UPDATE_RATE

    if not target_uid:
        if st["timer"] <= 0:
            angle = random.uniform(0, math.pi*2); r = random.uniform(2, 7)
            st["target_x"] = math.cos(angle)*r; st["target_z"] = math.sin(angle)*r
            st["timer"] = random.uniform(3, 8)
        tx, tz = _safe_target(st["target_x"], st["target_z"])
        cx, cz = _safe_move(cx, cz, tx, tz, step)
        return "walk", cx, cz

    tp = pos_store.get(target_uid, {})
    raw_tx = float(tp.get("x", 0)); raw_tz = float(tp.get("z", 0))
    hid = house_asgn.get(target_uid, "")
    house = dl_store.get(hid, {})
    locked = house.get("is_locked", False) and not house.get("hack_success", False)
    if locked:
        raw_tx = house.get("world_x", 0); raw_tz = house.get("world_z", 0) + 3.5
    st["target_x"] = raw_tx; st["target_z"] = raw_tz
    dx = raw_tx - cx; dz = raw_tz - cz
    dist = math.sqrt(dx*dx + dz*dz)

    if dist < BAGUETTE_RANGE and not locked:
        asyncio.create_task(_do_ai_baguette_hit(room_code, ai_uid, target_uid, cx, cz, gs, st))
        return "idle", cx, cz

    fwd_x = (dx/dist) if dist > 0.01 else 1.0
    fwd_z = (dz/dist) if dist > 0.01 else 0.0
    perp_x = -fwd_z; perp_z = fwd_x
    st["cosine_travel"] = st.get("cosine_travel", 0.0) + step
    phase_val  = st["cosine_travel"] * AI_COSINE_FREQUENCY + st.get("cosine_phase", 0.0)
    sin_offset = math.sin(phase_val) * AI_COSINE_AMPLITUDE
    tx, tz = _safe_target(raw_tx + perp_x*sin_offset, raw_tz + perp_z*sin_offset)
    prev_cx, prev_cz = cx, cz
    cx, cz = _safe_move(cx, cz, tx, tz, step)
    moved = math.sqrt((cx-prev_cx)**2 + (cz-prev_cz)**2)
    if moved < 0.05:
        st["stuck_count"] = st.get("stuck_count", 0) + 1
        if st["stuck_count"] > 5:
            ea = random.uniform(0, math.pi*2)
            cx = _clamp_to_world(cx + math.cos(ea)*1.5, cz + math.sin(ea)*1.5)[0]
            cz = _clamp_to_world(cx, cz + math.sin(ea)*1.5)[1]
            st["stuck_count"] = 0
    else:
        st["stuck_count"] = 0
    return ("run" if dist > 5.0 else "walk"), cx, cz

def _ai_role_target(ai_uid, role, phase, alive, roles, house_asgn,
                    pos_store, dl_store, night_votes, st):
    alive_others = [u for u, a in alive.items() if a and u != ai_uid]
    if role == "mafia":
        a = random.uniform(0, math.pi*2); r = random.uniform(2, 6)
        return math.cos(a)*r, math.sin(a)*r
    elif role == "police":
        if phase == "night":
            if st["timer"] <= 0:
                all_h = list(dl_store.values())
                if all_h:
                    h = random.choice(all_h)
                    st["spy_house_x"] = h.get("world_x", 0)
                    st["spy_house_z"] = h.get("world_z", 0) + 3.5
                    st["timer"] = random.uniform(15, 40)
            return st.get("spy_house_x", 0), st.get("spy_house_z", 0)
        a = random.uniform(0, math.pi*2); r = random.uniform(3, 8)
        return math.cos(a)*r, math.sin(a)*r
    elif role == "doctor":
        if phase == "night":
            vote_targets = list(night_votes.values())
            if vote_targets:
                tgt = random.choice(vote_targets)
                tp  = pos_store.get(tgt, {})
                if tp: return float(tp.get("x", 0))+2.5, float(tp.get("z", 0))+2.5
        a = random.uniform(0, math.pi*2); r = random.uniform(1, 5)
        return math.cos(a)*r, math.sin(a)*r
    else:
        if phase == "night":
            hid = house_asgn.get(ai_uid, "")
            h   = dl_store.get(hid, {})
            if h: return h.get("world_x", 0), h.get("world_z", 0) - 3.5
        if phase in ("meet", "morning", "dusk"):
            a = random.uniform(0, math.pi*2); r = random.uniform(2, 6)
            return math.cos(a)*r, math.sin(a)*r
        a = random.uniform(0, math.pi*2); r = random.uniform(3, 10)
        return math.cos(a)*r, math.sin(a)*r

# ---------------------------------------------------------------------------
# 도어락
# ---------------------------------------------------------------------------
def init_doorlocks(room_code: str, gs: dict, rs: RoomState):
    uids  = list(gs["players"].keys())
    total = len(uids)
    houses = {}
    house_indices = list(range(min(total, 12)))
    random.shuffle(house_indices)
    house_assignments = {}
    for i, uid in enumerate(uids):
        idx = house_indices[i] if i < len(house_indices) else i
        row = idx // 4; col = idx % 4
        hid = f"h_{row}_{col}"
        if idx < len(_HOUSE_CENTERS): wx, wz = _HOUSE_CENTERS[idx]
        else: wx = (col*14.0)-21.0; wz = (row*14.0)-14.0
        houses[hid] = {
            "owner": uid, "is_locked": False,
            "hack_in_progress": False, "hacker_uid": "",
            "hack_start": 0.0, "hack_success": False,
            "world_x": wx, "world_z": wz,
        }
        house_assignments[uid] = hid
    rs.doorlocks = houses
    gs["house_assignments"] = house_assignments
    print(f"[Doorlock] {room_code} {len(houses)}채 초기화", flush=True)

def lock_all_doors(rs: RoomState):
    for h in rs.doorlocks.values():
        h["is_locked"] = True; h["hack_in_progress"] = False
        h["hacker_uid"] = ""; h["hack_success"] = False

def unlock_all_doors(rs: RoomState):
    for h in rs.doorlocks.values():
        h["is_locked"] = False; h["hack_in_progress"] = False
        h["hacker_uid"] = ""; h["hack_success"] = False

def give_morning_bread(gs):
    if "economy" not in gs: gs["economy"] = {}
    for uid, alive in gs["alive"].items():
        if alive:
            gs["economy"].setdefault(uid, {"bread": 0, "batteries": 1, "potions": 0, "flashlight_on": False})
            gs["economy"][uid]["bread"] += MORNING_BREAD
    return gs

def get_economy(gs, uid):
    return gs.get("economy", {}).get(uid, {"bread": 0, "batteries": 1, "potions": 0, "flashlight_on": False})

# ---------------------------------------------------------------------------
# 밤 처리 / 투표 처리
# ---------------------------------------------------------------------------
async def process_night(room_code: str, gs: dict, rs: RoomState):
    roles = gs["roles"]; alive = gs["alive"]
    mafia_votes = {v: t for v, t in gs["night_votes"].items()
                   if roles.get(v) == "mafia" and alive.get(v) and alive.get(t)}
    mafia_target = ""
    if mafia_votes:
        count = {}
        for t in mafia_votes.values(): count[t] = count.get(t, 0) + 1
        mx    = max(count.values())
        cands = [t for t, c in count.items() if c == mx]
        mafia_target = random.choice(cands)
    protected = list(gs["doctor_protect"].values())
    police_results = {}
    for cop_id, tgt in gs["police_investigate"].items():
        if roles.get(cop_id) != "police": continue
        police_results[cop_id] = {"target": tgt, "result": "mafia" if roles.get(tgt) == "mafia" else "citizen"}
    actual_elim = ""
    if mafia_target and mafia_target not in protected:
        alive[mafia_target] = False; actual_elim = mafia_target
    mr = {
        "round": gs["day"], "eliminated": actual_elim, "eliminated_real": actual_elim,
        "protected": mafia_target != "" and actual_elim == "", "police_results": police_results,
    }
    gs["alive"] = alive; gs["morning_result"] = mr
    for k in ["night_votes", "doctor_protect", "police_investigate"]: gs[k] = {}
    unlock_all_doors(rs)
    return mr

async def process_votes(room_code: str, gs: dict):
    alive_list = [u for u, a in gs["alive"].items() if a]
    votes = gs["day_votes"]; count = {}
    for voter, tgt in votes.items():
        if not gs["alive"].get(tgt): continue
        count[tgt] = count.get(tgt, 0) + 1
    eliminated = ""
    if count:
        mx    = max(count.values())
        cands = [t for t, c in count.items() if c == mx]
        if len(cands) == 1:
            eliminated = cands[0]; gs["consecutive_ties"] = 0; gs["tie_pool"] = []
        else:
            for c in cands:
                if c not in gs["tie_pool"]: gs["tie_pool"].append(c)
            gs["consecutive_ties"] += 1
            valid = [c for c in gs["tie_pool"] if c in alive_list]
            if valid: eliminated = random.choice(valid); gs["consecutive_ties"] = 0; gs["tie_pool"] = []
    else:
        if alive_list: eliminated = random.choice(alive_list)
    if eliminated: gs["alive"][eliminated] = False
    gs["day_votes"] = {}
    return eliminated

def check_win(gs):
    roles = gs["roles"]; alive = gs["alive"]
    m = sum(1 for u, a in alive.items() if a and roles.get(u) == "mafia")
    c = sum(1 for u, a in alive.items() if a and roles.get(u) != "mafia")
    if m == 0: return "citizen"
    if m >= c: return "mafia"
    return None

# ---------------------------------------------------------------------------
# AI 밤 / 낮 행동
# ---------------------------------------------------------------------------
async def ai_do_night_actions(room_code: str):
    gs = await load_gs(room_code)
    if not gs or gs.get("phase") != "night": return
    alive   = gs["alive"]
    ai_uids = [uid for uid, p in gs["players"].items() if p.get("isAI") and alive.get(uid)]
    if not ai_uids: return
    async def _act(uid):
        if random.random() < AI_IDLE_CHANCE: return
        await asyncio.sleep(random.uniform(AI_ACTION_DELAY_MIN, AI_ACTION_DELAY_MAX))
        cgs = await load_gs(room_code)
        if not cgs or cgs.get("phase") != "night": return
        if not cgs["alive"].get(uid): return
        role = cgs["roles"].get(uid, "citizen")
        cal  = [u for u, a in cgs["alive"].items() if a]
        field = None; tgt = None
        if role == "mafia":
            c2 = [u for u in cal if u != uid and cgs["roles"].get(u) != "mafia"]
            if c2: field = "night_votes"; tgt = random.choice(c2)
        elif role == "doctor":
            if cal: field = "doctor_protect"; tgt = random.choice(cal)
        elif role == "police":
            c2 = [u for u in cal if u != uid]
            if c2: field = "police_investigate"; tgt = random.choice(c2)
        if field and tgt:
            fgs = await load_gs(room_code)
            if not fgs or fgs.get("phase") != "night": return
            fgs[field][uid] = tgt; await save_gs(room_code, fgs)
    await asyncio.gather(*[_act(uid) for uid in ai_uids], return_exceptions=True)

async def ai_do_vote_actions(room_code: str):
    gs = await load_gs(room_code)
    if not gs or gs.get("phase") != "vote": return
    ai_uids = [uid for uid, p in gs["players"].items() if p.get("isAI") and gs["alive"].get(uid)]
    if not ai_uids: return
    async def _vote(uid):
        if random.random() < AI_IDLE_CHANCE: return
        await asyncio.sleep(random.uniform(AI_ACTION_DELAY_MIN, AI_ACTION_DELAY_MAX))
        cgs = await load_gs(room_code)
        if not cgs or cgs.get("phase") != "vote": return
        if not cgs["alive"].get(uid): return
        role = cgs["roles"].get(uid, "citizen")
        cal  = [u for u, a in cgs["alive"].items() if a]
        c2   = [u for u in cal if u != uid and (cgs["roles"].get(u) != "mafia" if role == "mafia" else True)]
        if not c2: return
        fgs = await load_gs(room_code)
        if not fgs or fgs.get("phase") != "vote": return
        fgs["day_votes"][uid] = random.choice(c2); await save_gs(room_code, fgs)
    await asyncio.gather(*[_vote(uid) for uid in ai_uids], return_exceptions=True)

# ---------------------------------------------------------------------------
# 세션 초기화
# ---------------------------------------------------------------------------
async def init_session(room_code: str, room_data: dict) -> dict:
    pdata    = room_data.get("players", {})
    host_uid = room_data.get("hostId", "")
    uids     = list(pdata.keys())
    total    = len(uids)

    rc  = calc_roles(total)
    rl  = []
    for role, cnt in rc.items(): rl.extend([role] * cnt)
    shuffled = uids[:]; random.shuffle(shuffled); random.shuffle(rl)
    roles = {uid: rl[i] for i, uid in enumerate(shuffled)}

    gs = {
        "phase": "meet", "day": 1, "roles": roles,
        "alive": {uid: True for uid in uids},
        "host_uid": host_uid,
        "players": {
            uid: {
                "nickname": pdata[uid].get("nickname", "?"),
                "tag":      pdata[uid].get("tag", "0000"),
                "isAI":     pdata[uid].get("isAI", False),
                "isHost":   pdata[uid].get("isHost", False),
            } for uid in uids
        },
        "night_votes": {}, "doctor_protect": {}, "police_investigate": {},
        "day_votes": {}, "tie_pool": [], "consecutive_ties": 0,
        "morning_result": {}, "house_assignments": {},
        "economy": {uid: {"bread": 500, "batteries": 1, "potions": 0, "flashlight_on": False} for uid in uids},
    }

    # 기존 room 정리
    old_ws = {}
    if room_code in rooms:
        old_rs = rooms[room_code]
        if old_rs.phase_task and not old_rs.phase_task.done(): old_rs.phase_task.cancel()
        if old_rs.pos_task   and not old_rs.pos_task.done():   old_rs.pos_task.cancel()
        for uid, p in old_rs.players.items():
            if not p.get("isAI") and p.get("ws"):
                old_ws[uid] = p

    # pre_connections 복구
    pre = pre_connections.get(room_code, {})
    for uid, ws_obj in pre.items():
        if uid not in old_ws and uid in pdata and not pdata[uid].get("isAI", False):
            pi = pdata[uid]
            old_ws[uid] = {
                "ws": ws_obj, "nickname": pi.get("nickname", "?"),
                "tag": pi.get("tag", "0000"), "isHost": pi.get("isHost", False), "isAI": False
            }
    pre_connections.pop(room_code, None)

    # 새 RoomState 생성
    rs = RoomState()
    init_doorlocks(room_code, gs, rs)

    # 초기 위치
    for i, uid in enumerate(uids):
        angle = (2 * math.pi / max(total, 1)) * i
        rs.positions[uid] = {
            "x": round(math.cos(angle)*4, 2), "y": 0.0,
            "z": round(math.sin(angle)*4, 2), "rot_y": 0.0, "anim": "idle"
        }

    rooms[room_code] = rs

    # WS 복구
    for uid, p in old_ws.items():
        if uid in gs["players"] and not gs["players"][uid].get("isAI"):
            pi = gs["players"][uid]
            rs.players[uid] = {
                "ws": p["ws"], "nickname": pi["nickname"], "tag": pi["tag"],
                "isHost": pi.get("isHost", False), "isAI": False
            }

    # AI local state 초기화
    _ai_local_state.pop(room_code, None)

    await save_gs(room_code, gs)
    print(f"[Init] {room_code} | {total}명 | {rc} | WS복구={len(rs.players)}명", flush=True)
    return gs

async def _send_joined_to_all(room_code: str, gs: dict):
    rs = rooms.get(room_code)
    if not rs: return
    dl = rs.doorlocks
    ha = gs.get("house_assignments", {})
    for uid, pinfo in list(rs.players.items()):
        if pinfo.get("isAI"): continue
        try:
            my_role  = gs["roles"].get(uid, "citizen")
            my_house = ha.get(uid, "")
            hi       = dl.get(my_house, {})
            eco      = get_economy(gs, uid)
            await pinfo["ws"].send_text(json.dumps({
                "t": "joined", "uid": uid, "role": my_role,
                "roles": gs["roles"], "phase": gs["phase"], "day": gs["day"],
                "alive": gs["alive"], "players": gs["players"],
                "tie_pool": gs["tie_pool"],
                "house_assignments": ha, "my_house_id": my_house, "my_house_info": hi,
                "doorlock_states": _dl_payload(room_code),
                "economy": eco,
                "mafia_team": {pid: r for pid, r in gs["roles"].items() if r == "mafia"}
                    if my_role == "mafia" else {},
            }, ensure_ascii=False))
            print(f"[Init] joined 전송: {uid}", flush=True)
        except Exception as e:
            print(f"[Init] joined 전송 실패 {uid}: {e}", flush=True)

# ---------------------------------------------------------------------------
# 페이즈 루프
# ---------------------------------------------------------------------------
async def phase_loop(room_code: str):
    print(f"[Phase] {room_code} 루프 시작", flush=True)
    rs = rooms.get(room_code)
    try:
        gs = await load_gs(room_code)
        phase_queue = ["meet"] + PHASE_CYCLE * (MAX_ROUNDS + 2)

        for phase in phase_queue:
            rs = rooms.get(room_code)
            if not rs or rs.status != "playing": return
            gs["phase"] = phase
            await save_gs(room_code, gs)
            wait = PHASE_TIMES.get(phase, 60)

            if phase == "night":
                _reset_all_hits(rs)
                lock_all_doors(rs)
                await broadcast(room_code, {
                    "t": "lighting_change", "phase": "night",
                    "house_lights": False, "plaza_lights": True, "street_lights_energy": 3.5,
                })
                await broadcast(room_code, {"t": "doorlock_all_locked", "msg": "모든 집이 잠깁니다."})
                asyncio.create_task(ai_do_night_actions(room_code))

            elif phase == "morning":
                mr = await process_night(room_code, gs, rs)
                gs["morning_result"] = mr
                gs = give_morning_bread(gs)
                await save_gs(room_code, gs)
                await broadcast(room_code, {
                    "t": "lighting_change", "phase": "morning",
                    "house_lights": False, "plaza_lights": True, "street_lights_energy": 0.4,
                })
                for uid in gs["alive"]:
                    if gs["alive"][uid]:
                        eco = get_economy(gs, uid)
                        await send_to(room_code, uid, {
                            "t": "economy_update",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco.get("potions", 0),
                            "msg": f"아침이 밝았습니다. 500브래드 지급! (보유: {eco['bread']}🍞)"
                        })
                await _send_house_hints(room_code, gs)
                await broadcast(room_code, {
                    "t": "phase", "phase": "morning", "day": gs["day"],
                    "time": wait, "result": mr,
                    "house_assignments": gs.get("house_assignments", {}),
                    "doorlock_states": _dl_payload(room_code),
                })
                for rem in range(wait-1, -1, -1):
                    if room_code not in rooms: return
                    await asyncio.sleep(1)
                    await broadcast(room_code, {"t": "tick", "time": rem})
                winner = check_win(gs)
                if winner or gs["day"] >= MAX_ROUNDS:
                    await end_game(room_code, gs, winner or "citizen", "elimination"); return
                continue

            elif phase == "vote":
                asyncio.create_task(ai_do_vote_actions(room_code))

            elif phase == "meet":
                await _send_house_hints(room_code, gs)
                await broadcast(room_code, {
                    "t": "lighting_change", "phase": "meet",
                    "house_lights": False, "plaza_lights": True, "street_lights_energy": 0.8,
                })

            elif phase == "day":
                await broadcast(room_code, {
                    "t": "lighting_change", "phase": "day",
                    "house_lights": False, "plaza_lights": True, "street_lights_energy": 0.4,
                })

            elif phase == "dusk":
                await broadcast(room_code, {
                    "t": "lighting_change", "phase": "dusk",
                    "house_lights": False, "plaza_lights": True, "street_lights_energy": 1.5,
                })

            if phase != "morning":
                await broadcast(room_code, {
                    "t": "phase", "phase": phase, "day": gs["day"], "time": wait,
                    "house_assignments": gs.get("house_assignments", {}),
                    "doorlock_states": _dl_payload(room_code),
                })
                for rem in range(wait-1, -1, -1):
                    if room_code not in rooms: return
                    await asyncio.sleep(1)
                    await broadcast(room_code, {"t": "tick", "time": rem})

            if room_code not in rooms: return
            gs = await load_gs(room_code)

            if phase == "vote":
                elim = await process_votes(room_code, gs)
                await broadcast(room_code, {
                    "t": "vote_result", "eliminated": elim,
                    "alive": gs["alive"], "tie_pool": gs["tie_pool"]
                })
                winner = check_win(gs)
                if winner or gs["day"] >= MAX_ROUNDS:
                    await end_game(room_code, gs, winner or "citizen", "vote"); return
                gs["day"] += 1
                await save_gs(room_code, gs)

    except asyncio.CancelledError:
        print(f"[Phase] {room_code} 취소", flush=True)
    except Exception as e:
        import traceback
        print(f"[Phase] {room_code} 오류: {e}\n{traceback.format_exc()}", flush=True)

# ---------------------------------------------------------------------------
# 도어락 페이로드 / 힌트
# ---------------------------------------------------------------------------
def _dl_payload(room_code: str) -> dict:
    rs = rooms.get(room_code)
    if not rs: return {}
    return {
        hid: {"world_x": v["world_x"], "world_z": v["world_z"],
              "owner": v["owner"], "is_locked": v["is_locked"]}
        for hid, v in rs.doorlocks.items()
    }

async def _send_house_hints(room_code: str, gs: dict):
    rs = rooms.get(room_code)
    if not rs: return
    all_hids = list(rs.doorlocks.keys())
    for uid in gs.get("alive", {}):
        if not gs["alive"].get(uid): continue
        hid = gs.get("house_assignments", {}).get(uid, "")
        h   = rs.doorlocks.get(hid, {})
        if not h: continue
        num = all_hids.index(hid) + 1 if hid in all_hids else "?"
        await send_to(room_code, uid, {
            "t": "house_hint", "house_id": hid, "house_num": num,
            "world_x": h.get("world_x", 0), "world_z": h.get("world_z", 0),
            "msg": f"당신의 집은 {num}번 집입니다!\n밤에 [E]키로 문을 잠그세요!"
        })

# ---------------------------------------------------------------------------
# 게임 종료 / 정리
# ---------------------------------------------------------------------------
async def end_game(room_code: str, gs: dict, winner: str, reason: str):
    gs["phase"] = "result"; gs["winner"] = winner
    await save_gs(room_code, gs)
    await broadcast(room_code, {
        "t": "game_result", "winner": winner, "reason": reason,
        "roles": gs["roles"], "alive": gs["alive"], "day": gs["day"]
    })
    rs = rooms.get(room_code)
    if rs:
        rs.status = "ended"
        if rs.pos_task and not rs.pos_task.done(): rs.pos_task.cancel()
    _ai_local_state.pop(room_code, None)
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "ended"})
    asyncio.create_task(_delete_ai_rtdb(room_code, gs))
    asyncio.create_task(_cleanup(room_code, 60))
    print(f"[End] {room_code} winner={winner}", flush=True)

async def _delete_ai_rtdb(room_code: str, gs: dict):
    ai_uids = [uid for uid, p in gs.get("players", {}).items() if p.get("isAI")]
    if not ai_uids: return
    await asyncio.gather(
        *[_http_delete(_rtdb(f"rooms/{room_code}/players/{uid}")) for uid in ai_uids],
        return_exceptions=True)

async def _cleanup(room_code: str, delay: int = 60):
    await asyncio.sleep(delay)
    rs = rooms.get(room_code)
    if rs and rs.status == "ended":
        rooms.pop(room_code, None)
        _ai_local_state.pop(room_code, None)
        pre_connections.pop(room_code, None)
        await redis_del(_gs_key(room_code))
        print(f"[Cleanup] {room_code} 삭제", flush=True)

# ---------------------------------------------------------------------------
# 세션 시작 조건
# ---------------------------------------------------------------------------
def _should_start(room_code: str, gs: dict) -> bool:
    rs = rooms.get(room_code)
    if not rs or rs.status != "waiting": return False
    t = rs.phase_task
    if t and not t.done(): return False
    real = [u for u, p in gs.get("players", {}).items() if not p.get("isAI")]
    conn = [u for u in rs.players if not rs.players[u].get("isAI")]
    return len(real) > 0 and set(real) == set(conn)

def _start_loops(room_code: str):
    rs = rooms[room_code]; rs.status = "playing"
    rs.phase_task = asyncio.create_task(phase_loop(room_code))
    rs.pos_task   = asyncio.create_task(position_sync_loop(room_code))
    # AI는 global_ai_scheduler가 처리 (별도 task 없음)

def _ensure_loop(room_code: str) -> bool:
    rs = rooms.get(room_code)
    if not rs or rs.status != "playing": return False
    t = rs.phase_task
    if t and not t.done(): return False
    _start_loops(room_code); return True

# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _ai_scheduler_task
    if _USE_HTTPX: _http_client = httpx.AsyncClient(timeout=5.0)
    # ★ v9.0: 전역 AI 스케줄러 단일 시작
    _ai_scheduler_task = asyncio.create_task(global_ai_scheduler())
    print(
        f"[Startup] 브레드 킬러 서버 v9.0 준비 완료 | "
        f"틱={TICK_RATE*1000:.1f}ms | LOD={LOD_NEAR_DIST}m | AI=global_scheduler",
        flush=True
    )
    yield
    if _ai_scheduler_task and not _ai_scheduler_task.done():
        _ai_scheduler_task.cancel()
    if _http_client: await _http_client.aclose()

app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# HTTP 엔드포인트
# ---------------------------------------------------------------------------
@app.post("/room/start")
async def http_start(request: Request):
    try: body = await request.json()
    except: return JSONResponse({"ok": False, "error": "invalid json"}, 400)
    room_code  = body.get("room_code", "").upper().strip()
    host_uid   = body.get("host_uid", "")
    room_data  = body.get("room_data", {})
    if not room_code:
        return JSONResponse({"ok": False, "error": "room_code required"}, 400)
    rs = rooms.get(room_code)
    if rs:
        if rs.status == "playing": return JSONResponse({"ok": True, "already": True})
        if rs.status == "ended":   return JSONResponse({"ok": False, "error": "game_ended"}, 400)
    if not room_data:
        room_data = await rtdb_get(f"rooms/{room_code}")
        if not room_data: return JSONResponse({"ok": False, "error": "room not found"}, 404)
    if room_data.get("hostId", "") != host_uid:
        return JSONResponse({"ok": False, "error": "not host"}, 403)

    gs = await init_session(room_code, room_data)
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "playing"})

    real = [uid for uid, p in gs["players"].items() if not p.get("isAI")]
    await _send_joined_to_all(room_code, gs)

    if len(real) == 0 or _should_start(room_code, gs):
        _start_loops(room_code)
        print(f"[HTTP] {room_code} 루프 시작", flush=True)
    return JSONResponse({"ok": True, "room_code": room_code})

@app.get("/room/{room_code}/state")
async def http_state(room_code: str):
    gs = await load_gs(room_code.upper())
    if not gs: return JSONResponse({"ok": False, "exists": False})
    return JSONResponse({
        "ok": True, "phase": gs.get("phase"),
        "day": gs.get("day", 1), "alive": gs.get("alive", {})
    })

# ---------------------------------------------------------------------------
# WebSocket 엔드포인트
# ---------------------------------------------------------------------------
@app.websocket("/ws/mafia")
async def ws_mafia(ws: WebSocket):
    await ws.accept()
    uid = None; room_code = None
    try:
        while True:
            raw = await ws.receive_text()
            try: msg = json.loads(raw)
            except: continue
            t = msg.get("t", "")

            # ----------------------------------------------------------------
            if t == "join":
                uid       = msg.get("uid", "")
                room_code = msg.get("room_code", "").upper().strip()

                rs = rooms.get(room_code)
                if not rs:
                    pre_connections.setdefault(room_code, {})[uid] = ws
                    print(f"[PreConn] {room_code} / {uid} 대기 등록", flush=True)
                    await ws.send_text(json.dumps({"t": "waiting", "r": "session_not_ready"}))
                    continue

                gs = await load_gs(room_code)
                if uid not in gs.get("players", {}):
                    await ws.send_text(json.dumps({"t": "error", "r": "not_in_room"})); continue

                pinfo = gs["players"][uid]
                ex = rs.players.get(uid)
                if ex and ex.get("ws") is not ws:
                    try: await ex["ws"].close()
                    except: pass

                rs.players[uid] = {
                    "ws": ws, "nickname": pinfo["nickname"], "tag": pinfo["tag"],
                    "isHost": pinfo.get("isHost", False), "isAI": False
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
                    "doorlock_states": _dl_payload(room_code),
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
                    num      = all_hids.index(my_house) + 1 if my_house in all_hids else "?"
                    await send_to(room_code, uid, {
                        "t": "house_hint", "house_id": my_house, "house_num": num,
                        "world_x": hi.get("world_x", 0), "world_z": hi.get("world_z", 0),
                        "msg": f"당신의 집은 {num}번 집입니다! 집 위치를 확인하세요."
                    })

                await broadcast(room_code, {
                    "t": "player_joined", "uid": uid,
                    "player_data": {
                        "nickname": pinfo["nickname"], "tag": pinfo["tag"],
                        "isAI": False, "isHost": pinfo.get("isHost", False),
                    }
                }, exclude=uid)

            # ----------------------------------------------------------------
            elif t == "pos_update":
                if not (uid and room_code): continue
                rs = rooms.get(room_code)
                if not rs: continue

                raw_x   = float(msg.get("x", 0))
                raw_z   = float(msg.get("z", 0))
                # ★ v9.0: 이동 검증
                accepted, corr_x, corr_z = _validate_move(room_code, uid, raw_x, raw_z)
                final_x = corr_x; final_z = corr_z

                old = rs.positions.get(uid)
                if old:
                    rs.prev_positions[uid] = dict(old)

                rs.positions[uid] = {
                    "x": round(final_x, 3), "y": float(msg.get("y", 0)),
                    "z": round(final_z, 3), "rot_y": float(msg.get("rot_y", 0)),
                    "anim": str(msg.get("anim", "idle"))
                }

                # 보정이 있으면 클라에 알림
                if not accepted or (abs(corr_x - raw_x) > 0.05 or abs(corr_z - raw_z) > 0.05):
                    try:
                        await ws.send_text(json.dumps({
                            "t": "pos_correction",
                            "x": round(final_x, 3), "z": round(final_z, 3)
                        }))
                    except: pass

            # ----------------------------------------------------------------
            elif t in ("baguette_hit", "baguette_attack"):
                if not (uid and room_code and room_code in rooms): continue
                target_uid = msg.get("target", "")
                if not target_uid: continue
                await process_baguette_hit(room_code, uid, target_uid)

            # ----------------------------------------------------------------
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
                house["is_locked"] = new_state; house["hack_success"] = False
                await send_to(room_code, uid, {
                    "t": "doorlock_result", "ok": True,
                    "house_id": hid, "is_locked": new_state,
                    "action": "lock" if new_state else "unlock"
                })
                await broadcast(room_code, {"t": "doorlock_state", "house_id": hid, "is_locked": new_state}, exclude=uid)

            # ----------------------------------------------------------------
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
                house["hack_in_progress"] = True; house["hacker_uid"] = uid; house["hack_start"] = time.time()
                await send_to(room_code, uid, {"t": "hack_started", "house_id": thid, "duration": DOORLOCK_HACK_DURATION})
                await send_to(room_code, house["owner"], {"t": "hack_alert", "house_id": thid})
                asyncio.create_task(_process_hack(room_code, uid, thid))

            # ----------------------------------------------------------------
            elif t == "hack_cancel":
                if not (uid and room_code): continue
                rs = rooms.get(room_code)
                if not rs: continue
                hid   = msg.get("house_id", "")
                house = rs.doorlocks.get(hid)
                if house and house.get("hacker_uid") == uid:
                    house["hack_in_progress"] = False; house["hacker_uid"] = ""
                    await send_to(room_code, uid, {"t": "hack_cancelled", "house_id": hid})

            # ----------------------------------------------------------------
            elif t == "shop_buy":
                if not (uid and room_code): continue
                item = msg.get("item", "")
                gs   = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                eco  = get_economy(gs, uid)
                if item == "battery":
                    if eco["bread"] >= BATTERY_PRICE:
                        eco["bread"] -= BATTERY_PRICE; eco["batteries"] += 1
                        gs.setdefault("economy", {})[uid] = eco; await save_gs(room_code, gs)
                        await send_to(room_code, uid, {
                            "t": "shop_result", "ok": True, "item": "battery",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco.get("potions", 0),
                            "msg": f"배터리 구매 완료! 남은 브래드: {eco['bread']}🍞"})
                    else:
                        await send_to(room_code, uid, {
                            "t": "shop_result", "ok": False, "item": "battery",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco.get("potions", 0),
                            "msg": f"브래드 부족! (필요:{BATTERY_PRICE}, 보유:{eco['bread']})"})
                elif item == "potion":
                    if eco["bread"] >= POTION_PRICE:
                        eco["bread"] -= POTION_PRICE; eco["potions"] = eco.get("potions", 0) + 1
                        gs.setdefault("economy", {})[uid] = eco; await save_gs(room_code, gs)
                        await send_to(room_code, uid, {
                            "t": "shop_result", "ok": True, "item": "potion",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco["potions"],
                            "msg": f"🧪 마법의 약 구매 완료! 남은 브래드: {eco['bread']}🍞"})
                    else:
                        await send_to(room_code, uid, {
                            "t": "shop_result", "ok": False, "item": "potion",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco.get("potions", 0),
                            "msg": f"브래드 부족! (필요:{POTION_PRICE}, 보유:{eco['bread']})"})

            # ----------------------------------------------------------------
            elif t == "flashlight_dead":
                if not (uid and room_code): continue
                gs  = await load_gs(room_code)
                eco = get_economy(gs, uid)
                if eco["batteries"] > 0:
                    eco["batteries"] -= 1
                    gs.setdefault("economy", {})[uid] = eco; await save_gs(room_code, gs)
                    msg_text = "배터리가 모두 소진됐습니다!" if eco["batteries"] == 0 else f"배터리 남은 수: {eco['batteries']}"
                    await send_to(room_code, uid, {
                        "t": "economy_update", "bread": eco["bread"],
                        "batteries": eco["batteries"], "msg": msg_text})

            # ----------------------------------------------------------------
            elif t == "night_action":
                if not (uid and room_code and room_code in rooms): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid) or gs["phase"] != "night": continue
                my_role = gs["roles"].get(uid, "")
                action  = msg.get("action", ""); target = msg.get("target", "")
                fm = {"mafia_kill": ("night_votes","mafia"),
                      "doctor_protect": ("doctor_protect","doctor"),
                      "police_investigate": ("police_investigate","police")}
                if action in fm:
                    field, req = fm[action]
                    if my_role == req:
                        gs[field][uid] = target; await save_gs(room_code, gs)
                        await ws.send_text(json.dumps({"t": "action_ok", "action": action}))

            # ----------------------------------------------------------------
            elif t == "day_vote":
                if not (uid and room_code and room_code in rooms): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid) or gs["phase"] != "vote": continue
                gs["day_votes"][uid] = msg.get("target", ""); await save_gs(room_code, gs)
                await ws.send_text(json.dumps({"t": "vote_ok"}))

            # ----------------------------------------------------------------
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
                cm = {"t": "chat", "channel": channel, "uid": uid,
                      "nickname": pi.get("nickname","?"), "tag": pi.get("tag","0000"),
                      "text": text, "ts": int(time.time()*1000)}
                if channel == "general": await broadcast(room_code, cm)
                else:
                    for pid, r in gs["roles"].items():
                        if r == "mafia": await send_to(room_code, pid, cm)

            # ----------------------------------------------------------------
            elif t == "emotion":
                if not (uid and room_code): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                await broadcast(room_code, {"t": "emotion", "uid": uid, "emotion": msg.get("emotion","")}, exclude=uid)

            elif t == "ping":
                await ws.send_text(json.dumps({"t": "pong"}))

            elif t == "leave":
                break

    except WebSocketDisconnect: pass
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

# ---------------------------------------------------------------------------
# 해킹 처리
# ---------------------------------------------------------------------------
async def _process_hack(room_code: str, hacker_uid: str, house_id: str):
    await asyncio.sleep(DOORLOCK_HACK_DURATION)
    rs = rooms.get(room_code)
    if not rs: return
    house = rs.doorlocks.get(house_id)
    if not house or not house.get("hack_in_progress") or house.get("hacker_uid") != hacker_uid: return
    success = random.random() < DOORLOCK_SUCCESS_RATE
    house["hack_in_progress"] = False; house["hacker_uid"] = ""; house["hack_success"] = success
    if success: house["is_locked"] = False
    await send_to(room_code, hacker_uid, {"t": "hack_result", "house_id": house_id, "success": success})
    owner = house["owner"]
    if success:
        await send_to(room_code, owner, {"t": "door_breached", "house_id": house_id})
        await broadcast(room_code, {"t": "doorlock_state", "house_id": house_id, "is_locked": False})
    else:
        await send_to(room_code, owner, {"t": "hack_repelled", "house_id": house_id})

# ---------------------------------------------------------------------------
# 헬스체크
# ---------------------------------------------------------------------------
@app.get("/")
async def health():
    return {
        "status": "ok", "version": "v9.0",
        "tick_ms": round(TICK_RATE*1000, 1),
        "lod_dist": LOD_NEAR_DIST,
        "sessions": len(rooms),
        "rooms": list(rooms.keys()),
        "httpx": _USE_HTTPX, "redis": bool(REDIS_URL),
        "move_validation": True,
        "broadcast_batching": True,
        "ai_global_scheduler": True,
    }

@app.head("/")
async def health_head():
    return Response(status_code=200)

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    for var, name in [(RTDB_SECRET,"RTDB_SECRET"),(REDIS_URL,"REDIS_URL"),(REDIS_TOKEN,"REDIS_TOKEN")]:
        if not var: print(f"[경고] {name} 없음", flush=True)
    print(
        f"[Server] 포트 {WS_PORT} | 틱레이트 15Hz ({TICK_RATE*1000:.1f}ms) | "
        f"LOD={LOD_NEAR_DIST}m | v9.0 이동검증+AI풀+브로드캐스트배칭",
        flush=True
    )
    uvicorn.run(app, host="0.0.0.0", port=WS_PORT, ws_ping_interval=20, ws_ping_timeout=30)
