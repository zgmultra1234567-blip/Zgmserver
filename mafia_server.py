"""
mafia_server.py — 브레드 킬러 서버 v8.3
[수정] baguette_attack → baguette_hit 으로 변경
       서버에서 히트 카운트 누적, 2타 시 킬 (서버권위)
[유지] 집 배정 랜덤화, AI 이동 벽 관통 방지
"""
import asyncio, json, os, random, time, math
from contextlib import asynccontextmanager
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
    "dusk":    30,
}
PHASE_CYCLE = ["night", "morning", "day", "dusk"]
MAX_ROUNDS  = 10
SESSION_TTL = 3600

POSITION_SYNC_INTERVAL = 0.1
DOORLOCK_HACK_DURATION = 15.0
DOORLOCK_SUCCESS_RATE  = 0.67

# ★ 바게트 설정
BAGUETTE_RANGE         = 2.5
BAGUETTE_HIT_COOLDOWN  = 0.8   # 타격 간 최소 간격(초) — 클라이언트 ATTACK_COOLDOWN과 맞춤
BAGUETTE_HITS_TO_KILL  = 2     # 몇 타에 죽는지
BAGUETTE_HIT_RESET     = 8.0   # 이 시간(초) 내에 다음 타격 없으면 카운트 리셋

MORNING_BREAD          = 500
BATTERY_PRICE          = 500
BATTERY_DURATION_SEC   = 60.0
POTION_PRICE           = 700
POTION_DURATION        = 20.0

AI_IDLE_CHANCE       = 0.20
AI_MOVE_SPEED        = 3.5
AI_MOVE_UPDATE_RATE  = 0.5
AI_ACTION_DELAY_MIN  = 3
AI_ACTION_DELAY_MAX  = 10

LOD_NEAR_DIST = 20.0

WORLD_BOUND   = 48.0
HOUSE_SPACING = 14.0

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

print(f"=== 브레드 킬러 서버 v8.3 | httpx={_USE_HTTPX} | redis={bool(REDIS_URL)} ===", flush=True)

sessions:         dict = {}
_mem_store:       dict = {}
_http_client           = None
player_positions: dict = {}
doorlock_states:  dict = {}
attack_cooldowns: dict = {}
ai_move_targets:  dict = {}

# ★ 바게트 히트 카운트 저장소
# 구조: {room_code: {killer_uid: {target_uid: {count, last_hit}}}}
baguette_hits: dict = {}


# ================================================================
# HTTP 헬퍼
# ================================================================
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


# ================================================================
# Redis / 인메모리
# ================================================================
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


# ================================================================
# 역할 계산
# ================================================================
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


# ================================================================
# AI 충돌 방지 유틸
# ================================================================
def _clamp_to_world(x: float, z: float) -> tuple:
    x = max(-WORLD_BOUND, min(WORLD_BOUND, x))
    z = max(-WORLD_BOUND, min(WORLD_BOUND, z))
    return x, z


def _is_inside_house(x: float, z: float) -> bool:
    for hx, hz in _HOUSE_CENTERS:
        if abs(x - hx) < HOUSE_HALF and abs(z - hz) < HOUSE_HALF:
            return True
    return False


def _push_out_of_houses(x: float, z: float) -> tuple:
    for hx, hz in _HOUSE_CENTERS:
        dx = x - hx
        dz = z - hz
        if abs(dx) < HOUSE_HALF and abs(dz) < HOUSE_HALF:
            push_x = HOUSE_HALF - abs(dx) + 0.1
            push_z = HOUSE_HALF - abs(dz) + 0.1
            if push_x < push_z:
                x = hx + math.copysign(HOUSE_HALF + 0.1, dx)
            else:
                z = hz + math.copysign(HOUSE_HALF + 0.1, dz) if dz != 0 else hz + HOUSE_HALF + 0.1
    return x, z


def _safe_move(cur_x, cur_z, target_x, target_z, step):
    dx = target_x - cur_x
    dz = target_z - cur_z
    dist = math.sqrt(dx*dx + dz*dz)
    if dist < 0.01: return cur_x, cur_z
    ratio = min(step / dist, 1.0)
    nx = cur_x + dx * ratio
    nz = cur_z + dz * ratio
    if _is_inside_house(nx, nz):
        nx, nz = _push_out_of_houses(nx, nz)
    return _clamp_to_world(nx, nz)


def _safe_target(tx, tz):
    tx, tz = _clamp_to_world(tx, tz)
    if _is_inside_house(tx, tz):
        tx, tz = _push_out_of_houses(tx, tz)
    return tx, tz


# ================================================================
# 브로드캐스트
# ================================================================
async def broadcast(room_code, msg, exclude=None):
    if room_code not in sessions: return
    data = json.dumps(msg, ensure_ascii=False)
    async def _s(uid, p):
        if p.get("isAI"): return
        try: await p["ws"].send_text(data)
        except: pass
    await asyncio.gather(*[_s(uid,p) for uid,p in list(sessions[room_code]["players"].items()) if uid!=exclude], return_exceptions=True)

async def send_to(room_code, uid, msg):
    p = sessions.get(room_code,{}).get("players",{}).get(uid)
    if not p or p.get("isAI"): return
    try: await p["ws"].send_text(json.dumps(msg, ensure_ascii=False))
    except: pass


# ================================================================
# ★ 바게트 히트 카운트 처리 (서버권위)
# ================================================================
def _get_hit_state(room_code: str, killer: str, target: str) -> dict:
    baguette_hits.setdefault(room_code, {})
    baguette_hits[room_code].setdefault(killer, {})
    baguette_hits[room_code][killer].setdefault(target, {"count": 0, "last_hit": 0.0})
    return baguette_hits[room_code][killer][target]


def _reset_hit_state(room_code: str, killer: str, target: str):
    try:
        baguette_hits[room_code][killer].pop(target, None)
    except KeyError:
        pass


def _reset_all_hits_for_room(room_code: str):
    baguette_hits.pop(room_code, None)


async def process_baguette_hit(room_code: str, killer_uid: str, target_uid: str, ws) -> bool:
    """
    바게트 히트를 처리한다.
    반환값: True = 킬 발생, False = 히트만 누적
    """
    gs = await load_gs(room_code)
    if not gs:
        return False

    # ── 기본 검증 ──────────────────────────────────────────────
    if gs.get("phase") != "night":
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "not_night"})
        return False

    if gs["roles"].get(killer_uid) != "mafia":
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "not_mafia"})
        return False

    if not gs["alive"].get(killer_uid):
        return False

    if not gs["alive"].get(target_uid):
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "target_dead"})
        return False

    # ── 거리 검증 ──────────────────────────────────────────────
    mp = player_positions.get(room_code, {}).get(killer_uid, {})
    tp = player_positions.get(room_code, {}).get(target_uid, {})
    if not mp or not tp:
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "no_position"})
        return False

    dx   = mp["x"] - tp["x"]
    dz   = mp["z"] - tp["z"]
    dist = math.sqrt(dx*dx + dz*dz)
    if dist > BAGUETTE_RANGE:
        await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "too_far", "dist": round(dist, 2)})
        return False

    # ── 타격 쿨다운 검증 ───────────────────────────────────────
    now      = time.time()
    last_atk = attack_cooldowns.get(room_code, {}).get(killer_uid, 0.0)
    if now - last_atk < BAGUETTE_HIT_COOLDOWN:
        return False
    attack_cooldowns.setdefault(room_code, {})[killer_uid] = now

    # ── 히트 카운트 누적 ───────────────────────────────────────
    hs = _get_hit_state(room_code, killer_uid, target_uid)

    # 마지막 타격 이후 BAGUETTE_HIT_RESET 초 초과 시 카운트 리셋
    if now - hs["last_hit"] > BAGUETTE_HIT_RESET:
        hs["count"] = 0

    hs["count"]   += 1
    hs["last_hit"]  = now

    hit_count = hs["count"]
    print(f"[Baguette] {killer_uid} → {target_uid} | {hit_count}/{BAGUETTE_HITS_TO_KILL}타", flush=True)

    # ── 타격 피드백 전송 (피해자에게 화면 흔들림 등 알림용) ───
    await send_to(room_code, target_uid, {
        "t":    "baguette_hit_received",
        "from": killer_uid,
        "hp":   BAGUETTE_HITS_TO_KILL - hit_count,  # 남은 체력 힌트
    })

    # ── 킬 판정 ───────────────────────────────────────────────
    if hit_count >= BAGUETTE_HITS_TO_KILL:
        _reset_hit_state(room_code, killer_uid, target_uid)
        gs["alive"][target_uid] = False
        await save_gs(room_code, gs)

        await broadcast(room_code, {
            "t":      "baguette_kill",
            "killer": killer_uid,
            "victim": target_uid,
            "pos_x":  tp.get("x", 0),
            "pos_z":  tp.get("z", 0),
        })
        print(f"[Baguette] 킬 확정: {killer_uid} → {target_uid}", flush=True)

        winner = check_win(gs)
        if winner:
            await end_game(room_code, gs, winner, "baguette")
        return True

    # 아직 킬 아님 — 현재 히트 수 피드백
    await send_to(room_code, killer_uid, {
        "t":         "baguette_hit_ok",
        "target":    target_uid,
        "hit_count": hit_count,
        "hits_need": BAGUETTE_HITS_TO_KILL,
    })
    return False


# ================================================================
# LOD 위치 동기화 루프
# ================================================================
async def position_sync_loop(room_code):
    slow_tick = 0
    try:
        while room_code in sessions and sessions[room_code]["status"] == "playing":
            await asyncio.sleep(POSITION_SYNC_INTERVAL)
            pos = player_positions.get(room_code, {})
            if not pos:
                continue

            slow_tick += 1
            send_slow = (slow_tick % 5 == 0)

            sess = sessions.get(room_code, {})
            for uid, pinfo in list(sess.get("players", {}).items()):
                if pinfo.get("isAI"):
                    continue
                my_pos = pos.get(uid)
                if not my_pos:
                    try:
                        await pinfo["ws"].send_text(
                            json.dumps({"t": "pos_sync", "positions": pos, "ts": int(time.time()*1000)}, ensure_ascii=False))
                    except:
                        pass
                    continue

                mx, mz = my_pos.get("x", 0), my_pos.get("z", 0)
                near_pos = {}
                far_pos  = {}
                for other_uid, other_p in pos.items():
                    if other_uid == uid:
                        continue
                    dx   = other_p.get("x", 0) - mx
                    dz   = other_p.get("z", 0) - mz
                    dist = math.sqrt(dx*dx + dz*dz)
                    if dist <= LOD_NEAR_DIST:
                        near_pos[other_uid] = other_p
                    else:
                        far_pos[other_uid]  = other_p

                payload = dict(near_pos)
                if send_slow:
                    payload.update(far_pos)

                if payload:
                    try:
                        await pinfo["ws"].send_text(
                            json.dumps({"t": "pos_sync", "positions": payload, "ts": int(time.time()*1000)}, ensure_ascii=False))
                    except:
                        pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[PosSync] 오류: {e}", flush=True)


# ================================================================
# AI 서버권위 이동 루프
# ================================================================
async def ai_move_loop(room_code):
    print(f"[AiMove] {room_code} 시작", flush=True)
    ai_local: dict = {}

    try:
        while room_code in sessions and sessions[room_code]["status"] == "playing":
            await asyncio.sleep(AI_MOVE_UPDATE_RATE)

            gs = await load_gs(room_code)
            if not gs:
                continue

            phase       = gs.get("phase", "meet")
            roles       = gs.get("roles", {})
            alive       = gs.get("alive", {})
            players     = gs.get("players", {})
            house_asgn  = gs.get("house_assignments", {})
            night_votes = gs.get("night_votes", {})

            pos_store = player_positions.get(room_code, {})
            dl_store  = doorlock_states.get(room_code, {})

            for ai_uid, pinfo in players.items():
                if not pinfo.get("isAI"): continue
                if not alive.get(ai_uid):  continue

                cur = pos_store.get(ai_uid)
                if cur is None: continue

                role = roles.get(ai_uid, "citizen")

                if ai_uid not in ai_local:
                    ai_local[ai_uid] = {
                        "target_x": 0, "target_z": 0, "timer": 0,
                        "chase_uid": "", "spy_house_x": 0, "spy_house_z": 0,
                        "protect_uid": "", "stuck_count": 0
                    }

                st = ai_local[ai_uid]
                st["timer"] -= AI_MOVE_UPDATE_RATE

                if random.random() < AI_IDLE_CHANCE:
                    pos_store[ai_uid]["anim"] = "idle"
                    continue

                raw_tx, raw_tz = _ai_role_target(
                    ai_uid, role, phase,
                    alive, roles, house_asgn,
                    pos_store, dl_store, night_votes, st
                )
                tx, tz = _safe_target(raw_tx, raw_tz)
                st["target_x"] = tx
                st["target_z"] = tz

                cx   = cur.get("x", 0.0)
                cz   = cur.get("z", 0.0)
                dx   = tx - cx
                dz   = tz - cz
                dist = math.sqrt(dx*dx + dz*dz)
                step = AI_MOVE_SPEED * AI_MOVE_UPDATE_RATE

                if dist < 0.3:
                    anim = "idle"
                    if _is_inside_house(cx, cz):
                        cx, cz = _push_out_of_houses(cx, cz)
                        st["stuck_count"] = 0
                else:
                    prev_cx, prev_cz = cx, cz
                    cx, cz = _safe_move(cx, cz, tx, tz, step)
                    anim   = "run" if dist > 5.0 else "walk"

                    moved = math.sqrt((cx-prev_cx)**2 + (cz-prev_cz)**2)
                    if moved < 0.05:
                        st["stuck_count"] = st.get("stuck_count", 0) + 1
                        if st["stuck_count"] > 3:
                            ea = random.uniform(0, math.pi*2)
                            ex = cx + math.cos(ea) * 2.5
                            ez = cz + math.sin(ea) * 2.5
                            st["target_x"], st["target_z"] = _safe_target(ex, ez)
                            st["stuck_count"] = 0
                    else:
                        st["stuck_count"] = 0

                rot_y = math.atan2(dx, dz) if dist > 0.1 else cur.get("rot_y", 0.0)
                pos_store[ai_uid] = {
                    "x": round(cx, 3), "y": 0.0, "z": round(cz, 3),
                    "rot_y": round(rot_y, 3), "anim": anim
                }

                # AI 마피아 근접 킬 — baguette_hits 동일하게 처리
                if role == "mafia" and phase == "night":
                    chase_uid = st.get("chase_uid", "")
                    if chase_uid and alive.get(chase_uid, False) and dist < BAGUETTE_RANGE:
                        hid   = house_asgn.get(chase_uid, "")
                        house = dl_store.get(hid, {})
                        locked = house.get("is_locked", False) and not house.get("hack_success", False)
                        if not locked:
                            now      = time.time()
                            last_atk = attack_cooldowns.get(room_code, {}).get(ai_uid, 0)
                            if now - last_atk >= BAGUETTE_HIT_COOLDOWN:
                                attack_cooldowns.setdefault(room_code, {})[ai_uid] = now
                                hs = _get_hit_state(room_code, ai_uid, chase_uid)
                                if now - hs["last_hit"] > BAGUETTE_HIT_RESET:
                                    hs["count"] = 0
                                hs["count"]  += 1
                                hs["last_hit"] = now
                                print(f"[AI-Hit] {ai_uid}→{chase_uid} {hs['count']}/{BAGUETTE_HITS_TO_KILL}", flush=True)
                                if hs["count"] >= BAGUETTE_HITS_TO_KILL:
                                    _reset_hit_state(room_code, ai_uid, chase_uid)
                                    gs["alive"][chase_uid] = False
                                    await save_gs(room_code, gs)
                                    await broadcast(room_code, {
                                        "t":      "baguette_kill",
                                        "killer": ai_uid,
                                        "victim": chase_uid,
                                        "pos_x":  cx, "pos_z": cz,
                                    })
                                    print(f"[AI-킬] {ai_uid} → {chase_uid}", flush=True)
                                    winner = check_win(gs)
                                    if winner:
                                        await end_game(room_code, gs, winner, "baguette")
                                    st["chase_uid"] = ""

    except asyncio.CancelledError:
        pass
    except Exception as e:
        import traceback
        print(f"[AiMove] 오류: {e}\n{traceback.format_exc()}", flush=True)
    print(f"[AiMove] {room_code} 종료", flush=True)


def _ai_role_target(ai_uid, role, phase, alive, roles, house_asgn, pos_store, dl_store, night_votes, st):
    alive_others = [u for u, a in alive.items() if a and u != ai_uid]

    if role == "mafia":
        if phase == "night":
            if not st.get("chase_uid") or not alive.get(st.get("chase_uid", "")):
                cands = [u for u in alive_others if roles.get(u) != "mafia"]
                st["chase_uid"] = random.choice(cands) if cands else ""
            tgt = st.get("chase_uid", "")
            if tgt:
                tp  = pos_store.get(tgt, {})
                hid = house_asgn.get(tgt, "")
                h   = dl_store.get(hid, {})
                if h.get("is_locked", False) and not h.get("hack_success", False):
                    return h.get("world_x", 0), h.get("world_z", 0) + 3.5
                return float(tp.get("x", 0)), float(tp.get("z", 0))
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
                if tp:
                    return float(tp.get("x", 0)) + 2.5, float(tp.get("z", 0)) + 2.5
        a = random.uniform(0, math.pi*2); r = random.uniform(1, 5)
        return math.cos(a)*r, math.sin(a)*r

    else:
        if phase == "night":
            hid = house_asgn.get(ai_uid, "")
            h   = dl_store.get(hid, {})
            if h:
                return h.get("world_x", 0), h.get("world_z", 0) - 3.5
        if phase in ("meet", "morning", "dusk"):
            a = random.uniform(0, math.pi*2); r = random.uniform(2, 6)
            return math.cos(a)*r, math.sin(a)*r
        a = random.uniform(0, math.pi*2); r = random.uniform(3, 10)
        return math.cos(a)*r, math.sin(a)*r


# ================================================================
# 도어락
# ================================================================
def init_doorlocks(room_code, gs):
    uids  = list(gs["players"].keys())
    total = len(uids)
    houses = {}

    house_indices = list(range(min(total, 12)))
    random.shuffle(house_indices)

    house_assignments = {}
    for i, uid in enumerate(uids):
        idx = house_indices[i] if i < len(house_indices) else i
        row = idx // 4
        col = idx % 4
        hid = f"h_{row}_{col}"

        if idx < len(_HOUSE_CENTERS):
            wx, wz = _HOUSE_CENTERS[idx]
        else:
            wx = (col * 14.0) - 21.0
            wz = (row * 14.0) - 14.0

        houses[hid] = {
            "owner": uid, "is_locked": False,
            "hack_in_progress": False, "hacker_uid": "",
            "hack_start": 0.0, "hack_success": False,
            "world_x": wx, "world_z": wz,
        }
        house_assignments[uid] = hid

    doorlock_states[room_code] = houses
    gs["house_assignments"] = house_assignments
    print(f"[Doorlock] {room_code} {len(houses)}채 초기화 (랜덤 배정)", flush=True)


def lock_all_doors(rc):
    for h in doorlock_states.get(rc, {}).values():
        h["is_locked"]        = True
        h["hack_in_progress"] = False
        h["hacker_uid"]       = ""
        h["hack_success"]     = False


def unlock_all_doors(rc):
    for h in doorlock_states.get(rc, {}).values():
        h["is_locked"]        = False
        h["hack_in_progress"] = False
        h["hacker_uid"]       = ""
        h["hack_success"]     = False


# ================================================================
# 경제
# ================================================================
def give_morning_bread(gs):
    if "economy" not in gs:
        gs["economy"] = {}
    for uid, alive in gs["alive"].items():
        if alive:
            gs["economy"].setdefault(uid, {"bread": 0, "batteries": 1, "potions": 0, "flashlight_on": False})
            gs["economy"][uid]["bread"] += MORNING_BREAD
    return gs


def get_economy(gs, uid):
    return gs.get("economy", {}).get(uid, {"bread": 0, "batteries": 1, "potions": 0, "flashlight_on": False})


# ================================================================
# 밤 행동 처리
# ================================================================
async def process_night(room_code, gs):
    roles = gs["roles"]
    alive = gs["alive"]

    mafia_votes = {
        v: t for v, t in gs["night_votes"].items()
        if roles.get(v) == "mafia" and alive.get(v) and alive.get(t)
    }
    mafia_target = ""
    if mafia_votes:
        count = {}
        for t in mafia_votes.values():
            count[t] = count.get(t, 0) + 1
        mx    = max(count.values())
        cands = [t for t, c in count.items() if c == mx]
        mafia_target = random.choice(cands)

    protected = list(gs["doctor_protect"].values())

    police_results = {}
    for cop_id, tgt in gs["police_investigate"].items():
        if roles.get(cop_id) != "police": continue
        police_results[cop_id] = {
            "target": tgt,
            "result": "mafia" if roles.get(tgt) == "mafia" else "citizen"
        }

    actual_elim = ""
    if mafia_target and mafia_target not in protected:
        alive[mafia_target] = False
        actual_elim = mafia_target

    mr = {
        "round":           gs["day"],
        "eliminated":      actual_elim,
        "eliminated_real": actual_elim,
        "protected":       mafia_target != "" and actual_elim == "",
        "police_results":  police_results,
    }

    gs["alive"] = alive
    gs["morning_result"] = mr
    for k in ["night_votes", "doctor_protect", "police_investigate"]:
        gs[k] = {}
    unlock_all_doors(room_code)
    return mr


# ================================================================
# 낮 투표 처리
# ================================================================
async def process_votes(room_code, gs):
    alive_list = [u for u, a in gs["alive"].items() if a]
    votes  = gs["day_votes"]
    count  = {}
    for voter, tgt in votes.items():
        if not gs["alive"].get(tgt): continue
        count[tgt] = count.get(tgt, 0) + 1

    eliminated = ""
    if count:
        mx    = max(count.values())
        cands = [t for t, c in count.items() if c == mx]
        if len(cands) == 1:
            eliminated = cands[0]
            gs["consecutive_ties"] = 0
            gs["tie_pool"] = []
        else:
            for c in cands:
                if c not in gs["tie_pool"]:
                    gs["tie_pool"].append(c)
            gs["consecutive_ties"] += 1
            valid = [c for c in gs["tie_pool"] if c in alive_list]
            if valid:
                eliminated = random.choice(valid)
                gs["consecutive_ties"] = 0
                gs["tie_pool"] = []
    else:
        if alive_list:
            eliminated = random.choice(alive_list)

    if eliminated:
        gs["alive"][eliminated] = False
    gs["day_votes"] = {}
    return eliminated


def check_win(gs):
    roles = gs["roles"]
    alive = gs["alive"]
    m = sum(1 for u, a in alive.items() if a and roles.get(u) == "mafia")
    c = sum(1 for u, a in alive.items() if a and roles.get(u) != "mafia")
    if m == 0: return "citizen"
    if m >= c: return "mafia"
    return None


# ================================================================
# AI 자동 행동
# ================================================================
async def ai_do_night_actions(room_code):
    gs = await load_gs(room_code)
    if not gs or gs.get("phase") != "night": return
    roles   = gs["roles"]
    alive   = gs["alive"]
    ai_uids = [uid for uid, p in gs["players"].items() if p.get("isAI") and alive.get(uid)]
    if not ai_uids: return

    async def _act(uid):
        if random.random() < AI_IDLE_CHANCE: return
        delay = random.uniform(AI_ACTION_DELAY_MIN, AI_ACTION_DELAY_MAX)
        await asyncio.sleep(delay)
        cgs = await load_gs(room_code)
        if not cgs or cgs.get("phase") != "night": return
        if not cgs["alive"].get(uid): return
        role  = cgs["roles"].get(uid, "citizen")
        cal   = [u for u, a in cgs["alive"].items() if a]
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
            fgs[field][uid] = tgt
            await save_gs(room_code, fgs)

    await asyncio.gather(*[_act(uid) for uid in ai_uids], return_exceptions=True)


async def ai_do_vote_actions(room_code):
    gs = await load_gs(room_code)
    if not gs or gs.get("phase") != "vote": return
    ai_uids = [uid for uid, p in gs["players"].items() if p.get("isAI") and gs["alive"].get(uid)]
    if not ai_uids: return

    async def _vote(uid):
        if random.random() < AI_IDLE_CHANCE: return
        delay = random.uniform(AI_ACTION_DELAY_MIN, AI_ACTION_DELAY_MAX)
        await asyncio.sleep(delay)
        cgs = await load_gs(room_code)
        if not cgs or cgs.get("phase") != "vote": return
        if not cgs["alive"].get(uid): return
        role = cgs["roles"].get(uid, "citizen")
        cal  = [u for u, a in cgs["alive"].items() if a]
        c2   = [u for u in cal if u != uid and (cgs["roles"].get(u) != "mafia" if role == "mafia" else True)]
        if not c2: return
        fgs = await load_gs(room_code)
        if not fgs or fgs.get("phase") != "vote": return
        fgs["day_votes"][uid] = random.choice(c2)
        await save_gs(room_code, fgs)

    await asyncio.gather(*[_vote(uid) for uid in ai_uids], return_exceptions=True)


# ================================================================
# 세션 초기화
# ================================================================
async def init_session(room_code, room_data):
    pdata    = room_data.get("players", {})
    host_uid = room_data.get("hostId", "")
    uids     = list(pdata.keys())
    total    = len(uids)

    rc  = calc_roles(total)
    rl  = []
    for role, cnt in rc.items():
        rl.extend([role] * cnt)
    shuffled = uids[:]
    random.shuffle(shuffled)
    random.shuffle(rl)
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
    init_doorlocks(room_code, gs)

    player_positions[room_code] = {}
    for i, uid in enumerate(uids):
        angle = (2 * math.pi / max(total, 1)) * i
        player_positions[room_code][uid] = {
            "x": round(math.cos(angle)*4, 2), "y": 0.0,
            "z": round(math.sin(angle)*4, 2), "rot_y": 0.0, "anim": "idle"
        }
    attack_cooldowns[room_code] = {}
    ai_move_targets[room_code]  = {}

    # ★ 바게트 히트 카운트 초기화
    _reset_all_hits_for_room(room_code)

    await save_gs(room_code, gs)

    old_ws = {}
    if room_code in sessions:
        for tk in ("phase_task", "pos_task", "ai_move_task"):
            t = sessions[room_code].get(tk)
            if t and not t.done(): t.cancel()
        for uid, p in sessions[room_code]["players"].items():
            if not p.get("isAI") and p.get("ws"):
                old_ws[uid] = p

    sessions[room_code] = {
        "players": {}, "phase_task": None, "pos_task": None,
        "ai_move_task": None, "status": "waiting"
    }
    for uid, p in old_ws.items():
        if uid in gs["players"] and not gs["players"][uid].get("isAI"):
            pi = gs["players"][uid]
            sessions[room_code]["players"][uid] = {
                "ws": p["ws"], "nickname": pi["nickname"], "tag": pi["tag"],
                "isHost": pi.get("isHost", False), "isAI": False
            }
    print(f"[Init] {room_code} | {total}명 | {rc}", flush=True)
    return gs


# ================================================================
# 페이즈 루프
# ================================================================
async def phase_loop(room_code):
    print(f"[Phase] {room_code} 루프 시작", flush=True)
    try:
        gs = await load_gs(room_code)
        phase_queue = ["meet"] + PHASE_CYCLE * (MAX_ROUNDS + 2)

        for phase in phase_queue:
            if room_code not in sessions or sessions[room_code]["status"] != "playing":
                return

            gs["phase"] = phase
            await save_gs(room_code, gs)
            wait = PHASE_TIMES.get(phase, 60)

            if phase == "night":
                # ★ 밤 시작 시 바게트 히트 카운트 초기화
                _reset_all_hits_for_room(room_code)
                lock_all_doors(room_code)
                await broadcast(room_code, {"t": "doorlock_all_locked", "msg": "모든 집이 잠깁니다."})
                asyncio.create_task(ai_do_night_actions(room_code))

            elif phase == "morning":
                mr = await process_night(room_code, gs)
                gs["morning_result"] = mr
                gs = give_morning_bread(gs)
                await save_gs(room_code, gs)

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
                    if room_code not in sessions: return
                    await asyncio.sleep(1)
                    await broadcast(room_code, {"t": "tick", "time": rem})

                winner = check_win(gs)
                if winner or gs["day"] >= MAX_ROUNDS:
                    await end_game(room_code, gs, winner or "citizen", "elimination")
                    return
                continue

            elif phase == "vote":
                asyncio.create_task(ai_do_vote_actions(room_code))

            elif phase == "meet":
                await _send_house_hints(room_code, gs)

            if phase != "morning":
                await broadcast(room_code, {
                    "t": "phase", "phase": phase, "day": gs["day"], "time": wait,
                    "house_assignments": gs.get("house_assignments", {}),
                    "doorlock_states": _dl_payload(room_code),
                })
                for rem in range(wait-1, -1, -1):
                    if room_code not in sessions: return
                    await asyncio.sleep(1)
                    await broadcast(room_code, {"t": "tick", "time": rem})

            if room_code not in sessions: return
            gs = await load_gs(room_code)

            if phase == "vote":
                elim = await process_votes(room_code, gs)
                await broadcast(room_code, {
                    "t": "vote_result", "eliminated": elim,
                    "alive": gs["alive"], "tie_pool": gs["tie_pool"]
                })
                winner = check_win(gs)
                if winner or gs["day"] >= MAX_ROUNDS:
                    await end_game(room_code, gs, winner or "citizen", "vote")
                    return
                gs["day"] += 1
                await save_gs(room_code, gs)

    except asyncio.CancelledError:
        print(f"[Phase] {room_code} 취소", flush=True)
    except Exception as e:
        import traceback
        print(f"[Phase] {room_code} 오류: {e}\n{traceback.format_exc()}", flush=True)


def _dl_payload(room_code):
    return {
        hid: {
            "world_x": v["world_x"], "world_z": v["world_z"],
            "owner": v["owner"], "is_locked": v["is_locked"]
        }
        for hid, v in doorlock_states.get(room_code, {}).items()
    }


async def _send_house_hints(room_code, gs):
    dl       = doorlock_states.get(room_code, {})
    all_hids = list(dl.keys())
    for uid in gs.get("alive", {}):
        if not gs["alive"].get(uid): continue
        hid = gs.get("house_assignments", {}).get(uid, "")
        h   = dl.get(hid, {})
        if not h: continue
        num = all_hids.index(hid) + 1 if hid in all_hids else "?"
        await send_to(room_code, uid, {
            "t": "house_hint", "house_id": hid, "house_num": num,
            "world_x": h.get("world_x", 0), "world_z": h.get("world_z", 0),
            "msg": f"당신의 집은 {num}번 집입니다!\n밤에 [E]키로 문을 잠그세요!"
        })


async def end_game(room_code, gs, winner, reason):
    gs["phase"] = "result"; gs["winner"] = winner
    await save_gs(room_code, gs)
    await broadcast(room_code, {
        "t": "game_result", "winner": winner, "reason": reason,
        "roles": gs["roles"], "alive": gs["alive"], "day": gs["day"]
    })
    if room_code in sessions:
        sessions[room_code]["status"] = "ended"
        for tk in ("pos_task", "ai_move_task"):
            t = sessions[room_code].get(tk)
            if t and not t.done(): t.cancel()
    _reset_all_hits_for_room(room_code)
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "ended"})
    asyncio.create_task(_delete_ai_rtdb(room_code, gs))
    asyncio.create_task(_cleanup(room_code, 60))
    print(f"[End] {room_code} winner={winner}", flush=True)


async def _delete_ai_rtdb(room_code, gs):
    ai_uids = [uid for uid, p in gs.get("players", {}).items() if p.get("isAI")]
    if not ai_uids: return
    await asyncio.gather(
        *[_http_delete(_rtdb(f"rooms/{room_code}/players/{uid}")) for uid in ai_uids],
        return_exceptions=True
    )


async def _cleanup(room_code, delay=60):
    await asyncio.sleep(delay)
    if room_code in sessions and sessions[room_code]["status"] == "ended":
        sessions.pop(room_code, None)
        player_positions.pop(room_code, None)
        doorlock_states.pop(room_code, None)
        attack_cooldowns.pop(room_code, None)
        ai_move_targets.pop(room_code, None)
        baguette_hits.pop(room_code, None)
        await redis_del(_gs_key(room_code))
        print(f"[Cleanup] {room_code} 삭제", flush=True)


# ================================================================
# 유틸
# ================================================================
def _should_start(room_code, gs):
    s = sessions.get(room_code)
    if not s or s["status"] != "waiting": return False
    t = s.get("phase_task")
    if t and not t.done(): return False
    real = [u for u, p in gs.get("players", {}).items() if not p.get("isAI")]
    conn = [u for u in s["players"] if not s["players"][u].get("isAI")]
    return len(real) > 0 and set(real) == set(conn)


def _start_loops(room_code):
    s = sessions[room_code]; s["status"] = "playing"
    s["phase_task"]   = asyncio.create_task(phase_loop(room_code))
    s["pos_task"]     = asyncio.create_task(position_sync_loop(room_code))
    s["ai_move_task"] = asyncio.create_task(ai_move_loop(room_code))


def _ensure_loop(room_code):
    s = sessions.get(room_code)
    if not s or s["status"] != "playing": return False
    t = s.get("phase_task")
    if t and not t.done(): return False
    _start_loops(room_code); return True


# ================================================================
# lifespan + app
# ================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    if _USE_HTTPX:
        _http_client = httpx.AsyncClient(timeout=5.0)
    print("[Startup] 브레드 킬러 서버 v8.3 준비 완료", flush=True)
    yield
    if _http_client:
        await _http_client.aclose()

app = FastAPI(lifespan=lifespan)


# ================================================================
# HTTP 엔드포인트
# ================================================================
@app.post("/room/start")
async def http_start(request: Request):
    try: body = await request.json()
    except: return JSONResponse({"ok": False, "error": "invalid json"}, 400)
    room_code  = body.get("room_code", "").upper().strip()
    host_uid   = body.get("host_uid", "")
    room_data  = body.get("room_data", {})
    if not room_code:
        return JSONResponse({"ok": False, "error": "room_code required"}, 400)
    if room_code in sessions:
        s = sessions[room_code]
        if s["status"] == "playing": return JSONResponse({"ok": True, "already": True})
        if s["status"] == "ended":   return JSONResponse({"ok": False, "error": "game_ended"}, 400)
    if not room_data:
        room_data = await rtdb_get(f"rooms/{room_code}")
        if not room_data:
            return JSONResponse({"ok": False, "error": "room not found"}, 404)
    if room_data.get("hostId", "") != host_uid:
        return JSONResponse({"ok": False, "error": "not host"}, 403)
    gs = await init_session(room_code, room_data)
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "playing"})
    real = [uid for uid, p in gs["players"].items() if not p.get("isAI")]
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


# ================================================================
# WebSocket
# ================================================================
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

            if t == "join":
                uid       = msg.get("uid", "")
                room_code = msg.get("room_code", "").upper().strip()
                if room_code not in sessions:
                    await ws.send_text(json.dumps({"t": "error", "r": "no_session"})); continue
                gs = await load_gs(room_code)
                if uid not in gs.get("players", {}):
                    await ws.send_text(json.dumps({"t": "error", "r": "not_in_room"})); continue
                pinfo = gs["players"][uid]
                ex = sessions[room_code]["players"].get(uid)
                if ex and ex.get("ws") is not ws:
                    try: await ex["ws"].close()
                    except: pass
                sessions[room_code]["players"][uid] = {
                    "ws": ws, "nickname": pinfo["nickname"], "tag": pinfo["tag"],
                    "isHost": pinfo.get("isHost", False), "isAI": False
                }
                my_role  = gs["roles"].get(uid, "citizen")
                ha       = gs.get("house_assignments", {})
                my_house = ha.get(uid, "")
                hi       = doorlock_states.get(room_code, {}).get(my_house, {})
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
                    dl       = doorlock_states.get(room_code, {})
                    all_hids = list(dl.keys())
                    num      = all_hids.index(my_house) + 1 if my_house in all_hids else "?"
                    await send_to(room_code, uid, {
                        "t": "house_hint", "house_id": my_house, "house_num": num,
                        "world_x": hi.get("world_x", 0), "world_z": hi.get("world_z", 0),
                        "msg": f"당신의 집은 {num}번 집입니다! 집 위치를 확인하세요."
                    })
                await broadcast(room_code, {"t": "player_joined", "uid": uid}, exclude=uid)

            elif t == "pos_update":
                if not (uid and room_code): continue
                if room_code not in player_positions:
                    player_positions[room_code] = {}
                player_positions[room_code][uid] = {
                    "x": float(msg.get("x", 0)), "y": float(msg.get("y", 0)),
                    "z": float(msg.get("z", 0)), "rot_y": float(msg.get("rot_y", 0)),
                    "anim": str(msg.get("anim", "idle"))
                }

            # ★ baguette_hit: 클라이언트가 타격 알림 → 서버가 카운트/킬 처리
            elif t == "baguette_hit":
                if not (uid and room_code and room_code in sessions): continue
                target_uid = msg.get("target", "")
                if not target_uid: continue
                await process_baguette_hit(room_code, uid, target_uid, ws)

            # 하위 호환: 기존 baguette_attack 메시지도 수신 (1타 즉시 킬 → 이제 baguette_hit과 동일하게 처리)
            elif t == "baguette_attack":
                if not (uid and room_code and room_code in sessions): continue
                target_uid = msg.get("target", "")
                if not target_uid: continue
                await process_baguette_hit(room_code, uid, target_uid, ws)

            elif t == "doorlock_toggle":
                if not (uid and room_code): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                hid   = gs.get("house_assignments", {}).get(uid, "")
                house = doorlock_states.get(room_code, {}).get(hid)
                if not house:
                    await send_to(room_code, uid, {"t": "doorlock_result", "ok": False, "r": "no_house"}); continue
                if house["owner"] != uid:
                    await send_to(room_code, uid, {"t": "doorlock_result", "ok": False, "r": "not_owner"}); continue
                new_state         = not house["is_locked"]
                house["is_locked"]    = new_state
                house["hack_success"] = False
                await send_to(room_code, uid, {
                    "t": "doorlock_result", "ok": True,
                    "house_id": hid, "is_locked": new_state,
                    "action": "lock" if new_state else "unlock"
                })
                await broadcast(room_code, {
                    "t": "doorlock_state", "house_id": hid, "is_locked": new_state
                }, exclude=uid)

            elif t == "hack_start":
                if not (uid and room_code): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid) or gs.get("phase") != "night": continue
                if gs["roles"].get(uid) != "mafia": continue
                thid  = msg.get("house_id", "")
                house = doorlock_states.get(room_code, {}).get(thid)
                if not house or not house["is_locked"] or house["hack_in_progress"]: continue
                mp = player_positions.get(room_code, {}).get(uid, {})
                dx = mp.get("x", 0) - house["world_x"]
                dz = mp.get("z", 0) - house["world_z"]
                if math.sqrt(dx*dx + dz*dz) > 4.0:
                    await send_to(room_code, uid, {"t": "hack_failed", "r": "too_far"}); continue
                house["hack_in_progress"] = True
                house["hacker_uid"]       = uid
                house["hack_start"]       = time.time()
                await send_to(room_code, uid, {
                    "t": "hack_started", "house_id": thid, "duration": DOORLOCK_HACK_DURATION
                })
                await send_to(room_code, house["owner"], {"t": "hack_alert", "house_id": thid})
                asyncio.create_task(_process_hack(room_code, uid, thid))

            elif t == "hack_cancel":
                if not (uid and room_code): continue
                hid   = msg.get("house_id", "")
                house = doorlock_states.get(room_code, {}).get(hid)
                if house and house.get("hacker_uid") == uid:
                    house["hack_in_progress"] = False
                    house["hacker_uid"]       = ""
                    await send_to(room_code, uid, {"t": "hack_cancelled", "house_id": hid})

            elif t == "shop_buy":
                if not (uid and room_code): continue
                item = msg.get("item", "")
                gs   = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                eco  = get_economy(gs, uid)

                if item == "battery":
                    if eco["bread"] >= BATTERY_PRICE:
                        eco["bread"] -= BATTERY_PRICE; eco["batteries"] += 1
                        gs.setdefault("economy", {})[uid] = eco
                        await save_gs(room_code, gs)
                        await send_to(room_code, uid, {
                            "t": "shop_result", "ok": True, "item": "battery",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco.get("potions", 0),
                            "msg": f"배터리 구매 완료! 남은 브래드: {eco['bread']}🍞"
                        })
                    else:
                        await send_to(room_code, uid, {
                            "t": "shop_result", "ok": False, "item": "battery",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco.get("potions", 0),
                            "msg": f"브래드 부족! (필요:{BATTERY_PRICE}, 보유:{eco['bread']})"
                        })

                elif item == "potion":
                    if eco["bread"] >= POTION_PRICE:
                        eco["bread"] -= POTION_PRICE; eco["potions"] = eco.get("potions", 0) + 1
                        gs.setdefault("economy", {})[uid] = eco
                        await save_gs(room_code, gs)
                        await send_to(room_code, uid, {
                            "t": "shop_result", "ok": True, "item": "potion",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco["potions"],
                            "msg": f"🧪 마법의 약 구매 완료! 남은 브래드: {eco['bread']}🍞"
                        })
                    else:
                        await send_to(room_code, uid, {
                            "t": "shop_result", "ok": False, "item": "potion",
                            "bread": eco["bread"], "batteries": eco["batteries"],
                            "potions": eco.get("potions", 0),
                            "msg": f"브래드 부족! (필요:{POTION_PRICE}, 보유:{eco['bread']})"
                        })

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
                        "t": "economy_update",
                        "bread": eco["bread"], "batteries": eco["batteries"],
                        "msg": msg_text
                    })

            elif t == "night_action":
                if not (uid and room_code and room_code in sessions): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid) or gs["phase"] != "night": continue
                my_role = gs["roles"].get(uid, "")
                action  = msg.get("action", "")
                target  = msg.get("target", "")
                fm = {
                    "mafia_kill":         ("night_votes", "mafia"),
                    "doctor_protect":     ("doctor_protect", "doctor"),
                    "police_investigate": ("police_investigate", "police"),
                }
                if action in fm:
                    field, req = fm[action]
                    if my_role == req:
                        gs[field][uid] = target
                        await save_gs(room_code, gs)
                        await ws.send_text(json.dumps({"t": "action_ok", "action": action}))

            elif t == "day_vote":
                if not (uid and room_code and room_code in sessions): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid) or gs["phase"] != "vote": continue
                gs["day_votes"][uid] = msg.get("target", "")
                await save_gs(room_code, gs)
                await ws.send_text(json.dumps({"t": "vote_ok"}))

            elif t == "chat":
                if not (uid and room_code and room_code in sessions): continue
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
                    "text": text, "ts": int(time.time()*1000)
                }
                if channel == "general":
                    await broadcast(room_code, cm)
                else:
                    for pid, r in gs["roles"].items():
                        if r == "mafia":
                            await send_to(room_code, pid, cm)

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
        if uid and room_code and room_code in sessions:
            sessions[room_code]["players"].pop(uid, None)
            await broadcast(room_code, {"t": "player_left", "uid": uid})
        print(f"[WS] 해제: {uid}", flush=True)


async def _process_hack(room_code, hacker_uid, house_id):
    await asyncio.sleep(DOORLOCK_HACK_DURATION)
    house = doorlock_states.get(room_code, {}).get(house_id)
    if not house or not house.get("hack_in_progress") or house.get("hacker_uid") != hacker_uid:
        return
    success = random.random() < DOORLOCK_SUCCESS_RATE
    house["hack_in_progress"] = False
    house["hacker_uid"]       = ""
    house["hack_success"]     = success
    if success:
        house["is_locked"] = False
    await send_to(room_code, hacker_uid, {"t": "hack_result", "house_id": house_id, "success": success})
    gs    = await load_gs(room_code)
    owner = house["owner"]
    if success:
        await send_to(room_code, owner, {"t": "door_breached", "house_id": house_id})
        await broadcast(room_code, {"t": "doorlock_state", "house_id": house_id, "is_locked": False})
    else:
        await send_to(room_code, owner, {"t": "hack_repelled", "house_id": house_id})


@app.get("/")
async def health():
    return {
        "status": "ok", "sessions": len(sessions),
        "rooms": list(sessions.keys()),
        "httpx": _USE_HTTPX, "redis": bool(REDIS_URL)
    }

@app.head("/")
async def health_head():
    return Response(status_code=200)


if __name__ == "__main__":
    for var, name in [(RTDB_SECRET, "RTDB_SECRET"), (REDIS_URL, "REDIS_URL"), (REDIS_TOKEN, "REDIS_TOKEN")]:
        if not var:
            print(f"[경고] {name} 없음", flush=True)
    print(f"[Server] 포트 {WS_PORT}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=WS_PORT, ws_ping_interval=20, ws_ping_timeout=30)
