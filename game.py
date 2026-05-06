"""
game.py — 게임 로직 (페이즈/전투/AI/도어락/경제)
의존: config, state, storage, world, network
순환참조 방지: main.py 를 절대 import 하지 않음
"""
import asyncio, math, random, time
from config import (
    TICK_RATE, POSITION_SYNC_INTERVAL, AI_MOVE_UPDATE_RATE,
    PHASE_TIMES, PHASE_CYCLE, MAX_ROUNDS,
    BAGUETTE_RANGE, BAGUETTE_HIT_COOLDOWN, BAGUETTE_HITS_TO_KILL, BAGUETTE_HIT_RESET,
    MORNING_BREAD, BATTERY_PRICE, POTION_PRICE,
    AI_IDLE_CHANCE, AI_MOVE_SPEED, AI_ACTION_DELAY_MIN, AI_ACTION_DELAY_MAX,
    AI_COSINE_AMPLITUDE, AI_COSINE_FREQUENCY,
    LOD_NEAR_DIST, HOUSE_CENTERS, HOUSE_HALF,
    DOORLOCK_HACK_DURATION, DOORLOCK_SUCCESS_RATE,
    DEBUG,
)
from state import RoomState, rooms, ai_local_state, get_room_lock
from storage import load_gs, save_gs, delete_gs, rtdb_patch, http_delete
from world import safe_move, safe_target, is_inside_house, push_out_of_houses, clamp_to_world
from network import broadcast, send_to


# ─────────────────────────────────────────────────────────────────────────────
# 역할 계산
# ─────────────────────────────────────────────────────────────────────────────
def calc_roles(total: int) -> dict:
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


# ─────────────────────────────────────────────────────────────────────────────
# 경제
# ─────────────────────────────────────────────────────────────────────────────
def get_economy(gs: dict, uid: str) -> dict:
    return gs.get("economy", {}).get(uid, {"bread": 0, "batteries": 1, "potions": 0, "flashlight_on": False})


def give_morning_bread(gs: dict) -> dict:
    if "economy" not in gs:
        gs["economy"] = {}
    for uid, alive in gs["alive"].items():
        if alive:
            gs["economy"].setdefault(uid, {"bread": 0, "batteries": 1, "potions": 0, "flashlight_on": False})
            gs["economy"][uid]["bread"] += MORNING_BREAD
    return gs


# ─────────────────────────────────────────────────────────────────────────────
# gs 캐시 헬퍼 (A급: Redis 과호출 방지)
# ─────────────────────────────────────────────────────────────────────────────
def _gs_from_cache(room_code: str) -> dict:
    """RoomState 캐시에서 gs를 읽음. 없으면 빈 dict."""
    rs = rooms.get(room_code)
    if rs and rs.gs:
        return rs.gs
    return {}


def _gs_update_cache(room_code: str, gs: dict):
    """gs를 RoomState 캐시에 저장."""
    rs = rooms.get(room_code)
    if rs:
        rs.gs = gs


async def _load_gs_cached(room_code: str) -> dict:
    """캐시 우선 로드. 캐시 없으면 Redis에서 가져와 캐시에 저장."""
    rs = rooms.get(room_code)
    if rs and rs.gs:
        return rs.gs
    gs = await load_gs(room_code)
    if rs:
        rs.gs = gs
    return gs


async def _save_gs_cached(room_code: str, gs: dict):
    """캐시 갱신 + Redis 저장."""
    _gs_update_cache(room_code, gs)
    await save_gs(room_code, gs)


# ─────────────────────────────────────────────────────────────────────────────
# 도어락
# ─────────────────────────────────────────────────────────────────────────────
def init_doorlocks(room_code: str, gs: dict, rs: RoomState):
    uids  = list(gs["players"].keys())
    total = len(uids)
    houses: dict = {}
    house_indices = list(range(min(total, 12)))
    random.shuffle(house_indices)
    house_assignments: dict = {}
    for i, uid in enumerate(uids):
        idx = house_indices[i] if i < len(house_indices) else i
        row = idx // 4; col = idx % 4
        hid = f"h_{row}_{col}"
        if idx < len(HOUSE_CENTERS):
            wx, wz = HOUSE_CENTERS[idx]
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
    rs.doorlocks = houses
    gs["house_assignments"] = house_assignments
    print(f"[Doorlock] {room_code} {len(houses)}채 초기화", flush=True)


def lock_all_doors(rs: RoomState):
    for h in rs.doorlocks.values():
        h["is_locked"] = True
        h["hack_in_progress"] = False
        h["hacker_uid"] = ""
        h["hack_success"] = False


def unlock_all_doors(rs: RoomState):
    for h in rs.doorlocks.values():
        h["is_locked"] = False
        h["hack_in_progress"] = False
        h["hacker_uid"] = ""
        h["hack_success"] = False


def dl_payload(room_code: str) -> dict:
    rs = rooms.get(room_code)
    if not rs:
        return {}
    return {
        hid: {
            "world_x": v["world_x"], "world_z": v["world_z"],
            "owner": v["owner"], "is_locked": v["is_locked"],
        }
        for hid, v in rs.doorlocks.items()
    }


async def process_hack(room_code: str, hacker_uid: str, house_id: str):
    await asyncio.sleep(DOORLOCK_HACK_DURATION)
    rs = rooms.get(room_code)
    if not rs:
        return
    house = rs.doorlocks.get(house_id)
    if not house or not house.get("hack_in_progress") or house.get("hacker_uid") != hacker_uid:
        return
    success = random.random() < DOORLOCK_SUCCESS_RATE
    house["hack_in_progress"] = False
    house["hacker_uid"] = ""
    house["hack_success"] = success
    if success:
        house["is_locked"] = False
    await send_to(room_code, hacker_uid, {"t": "hack_result", "house_id": house_id, "success": success})
    owner = house["owner"]
    if success:
        await send_to(room_code, owner, {"t": "door_breached", "house_id": house_id})
        await broadcast(room_code, {"t": "doorlock_state", "house_id": house_id, "is_locked": False})
    else:
        await send_to(room_code, owner, {"t": "hack_repelled", "house_id": house_id})


# ─────────────────────────────────────────────────────────────────────────────
# 바게트 전투
# ─────────────────────────────────────────────────────────────────────────────
def _get_hit_state(rs: RoomState, killer: str, target: str) -> dict:
    rs.baguette_hits.setdefault(killer, {})
    rs.baguette_hits[killer].setdefault(target, {"count": 0, "last_hit": 0.0})
    return rs.baguette_hits[killer][target]


def _reset_hit_state(rs: RoomState, killer: str, target: str):
    try:
        rs.baguette_hits[killer].pop(target, None)
    except KeyError:
        pass


def reset_all_hits(rs: RoomState):
    rs.baguette_hits.clear()


async def process_baguette_hit(room_code: str, killer_uid: str, target_uid: str) -> bool:
    rs = rooms.get(room_code)
    if not rs:
        return False

    # S급: room lock으로 gs 레이스 방지
    async with get_room_lock(room_code):
        gs = await _load_gs_cached(room_code)
        if not gs:
            return False

        if gs.get("phase") != "night":
            await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "not_night"}); return False
        if gs["roles"].get(killer_uid) != "mafia":
            await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "not_mafia"}); return False
        if not gs["alive"].get(killer_uid):
            return False
        if not gs["alive"].get(target_uid):
            await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "target_dead"}); return False

        mp = rs.positions.get(killer_uid, {})
        tp = rs.positions.get(target_uid, {})
        if not mp or not tp:
            await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "no_position"}); return False

        dist = math.sqrt((mp["x"] - tp["x"]) ** 2 + (mp["z"] - tp["z"]) ** 2)
        if dist > BAGUETTE_RANGE:
            await send_to(room_code, killer_uid, {"t": "attack_failed", "r": "too_far", "dist": round(dist, 2)}); return False

        now      = time.time()
        last_atk = rs.attack_cooldowns.get(killer_uid, 0.0)
        if now - last_atk < BAGUETTE_HIT_COOLDOWN:
            return False
        rs.attack_cooldowns[killer_uid] = now

        hs = _get_hit_state(rs, killer_uid, target_uid)
        if now - hs["last_hit"] > BAGUETTE_HIT_RESET:
            hs["count"] = 0
        hs["count"]  += 1
        hs["last_hit"] = now
        hit_count = hs["count"]
        if DEBUG:
            print(f"[Baguette] {killer_uid} -> {target_uid} | {hit_count}/{BAGUETTE_HITS_TO_KILL}타", flush=True)

        await send_to(room_code, target_uid, {
            "t": "baguette_hit_received", "from": killer_uid,
            "hp": BAGUETTE_HITS_TO_KILL - hit_count,
        })

        if hit_count >= BAGUETTE_HITS_TO_KILL:
            _reset_hit_state(rs, killer_uid, target_uid)
            gs["alive"][target_uid] = False
            await _save_gs_cached(room_code, gs)
            await broadcast(room_code, {
                "t": "baguette_kill", "killer": killer_uid, "victim": target_uid,
                "pos_x": tp.get("x", 0), "pos_z": tp.get("z", 0),
            })
            print(f"[Baguette] 킬 확정: {killer_uid} -> {target_uid}", flush=True)
            winner = check_win(gs)
            if winner:
                await end_game(room_code, gs, winner, "baguette")
            return True

        await send_to(room_code, killer_uid, {
            "t": "baguette_hit_ok", "target": target_uid,
            "hit_count": hit_count, "hits_need": BAGUETTE_HITS_TO_KILL,
        })
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 승리 조건
# ─────────────────────────────────────────────────────────────────────────────
def check_win(gs: dict) -> str | None:
    roles = gs["roles"]; alive = gs["alive"]
    m = sum(1 for u, a in alive.items() if a and roles.get(u) == "mafia")
    c = sum(1 for u, a in alive.items() if a and roles.get(u) != "mafia")
    if m == 0: return "citizen"
    if m >= c: return "mafia"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 밤 처리 / 투표
# ─────────────────────────────────────────────────────────────────────────────
async def process_night(room_code: str, gs: dict, rs: RoomState) -> dict:
    roles = gs["roles"]; alive = gs["alive"]
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
        if roles.get(cop_id) != "police":
            continue
        police_results[cop_id] = {
            "target": tgt,
            "result": "mafia" if roles.get(tgt) == "mafia" else "citizen",
        }
    actual_elim = ""
    if mafia_target and mafia_target not in protected:
        alive[mafia_target] = False
        actual_elim = mafia_target
    mr = {
        "round": gs["day"], "eliminated": actual_elim, "eliminated_real": actual_elim,
        "protected": mafia_target != "" and actual_elim == "",
        "police_results": police_results,
    }
    gs["alive"] = alive
    gs["morning_result"] = mr
    for k in ["night_votes", "doctor_protect", "police_investigate"]:
        gs[k] = {}
    unlock_all_doors(rs)
    return mr


async def process_votes(room_code: str, gs: dict) -> str:
    alive_list = [u for u, a in gs["alive"].items() if a]
    votes = gs["day_votes"]; count = {}
    for voter, tgt in votes.items():
        if not gs["alive"].get(tgt):
            continue
        count[tgt] = count.get(tgt, 0) + 1
    eliminated = ""
    if count:
        mx    = max(count.values())
        cands = [t for t, c in count.items() if c == mx]
        if len(cands) == 1:
            eliminated = cands[0]; gs["consecutive_ties"] = 0; gs["tie_pool"] = []
        else:
            for c in cands:
                if c not in gs["tie_pool"]:
                    gs["tie_pool"].append(c)
            gs["consecutive_ties"] += 1
            valid = [c for c in gs["tie_pool"] if c in alive_list]
            if valid:
                eliminated = random.choice(valid); gs["consecutive_ties"] = 0; gs["tie_pool"] = []
    else:
        if alive_list:
            eliminated = random.choice(alive_list)
    if eliminated:
        gs["alive"][eliminated] = False
    gs["day_votes"] = {}
    return eliminated


# ─────────────────────────────────────────────────────────────────────────────
# AI 행동 (밤 / 낮 투표)
# ─────────────────────────────────────────────────────────────────────────────
async def ai_do_night_actions(room_code: str):
    gs = await _load_gs_cached(room_code)
    if not gs or gs.get("phase") != "night":
        return
    ai_uids = [uid for uid, p in gs["players"].items() if p.get("isAI") and gs["alive"].get(uid)]
    if not ai_uids:
        return

    async def _act(uid):
        if random.random() < AI_IDLE_CHANCE:
            return
        await asyncio.sleep(random.uniform(AI_ACTION_DELAY_MIN, AI_ACTION_DELAY_MAX))
        async with get_room_lock(room_code):
            cgs = await _load_gs_cached(room_code)
            if not cgs or cgs.get("phase") != "night" or not cgs["alive"].get(uid):
                return
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
                cgs[field][uid] = tgt
                await _save_gs_cached(room_code, cgs)

    await asyncio.gather(*[_act(uid) for uid in ai_uids], return_exceptions=True)


async def ai_do_vote_actions(room_code: str):
    gs = await _load_gs_cached(room_code)
    if not gs or gs.get("phase") != "vote":
        return
    ai_uids = [uid for uid, p in gs["players"].items() if p.get("isAI") and gs["alive"].get(uid)]
    if not ai_uids:
        return

    async def _vote(uid):
        if random.random() < AI_IDLE_CHANCE:
            return
        await asyncio.sleep(random.uniform(AI_ACTION_DELAY_MIN, AI_ACTION_DELAY_MAX))
        async with get_room_lock(room_code):
            cgs = await _load_gs_cached(room_code)
            if not cgs or cgs.get("phase") != "vote" or not cgs["alive"].get(uid):
                return
            role = cgs["roles"].get(uid, "citizen")
            cal  = [u for u, a in cgs["alive"].items() if a]
            c2   = [u for u in cal if u != uid and (cgs["roles"].get(u) != "mafia" if role == "mafia" else True)]
            if not c2:
                return
            cgs["day_votes"][uid] = random.choice(c2)
            await _save_gs_cached(room_code, cgs)

    await asyncio.gather(*[_vote(uid) for uid in ai_uids], return_exceptions=True)


# ─────────────────────────────────────────────────────────────────────────────
# AI 이동 (전역 스케줄러에서 호출)
# ─────────────────────────────────────────────────────────────────────────────
async def tick_ai_for_room(room_code: str, rs: RoomState):
    # A급: Redis 대신 캐시에서 읽음
    gs = _gs_from_cache(room_code)
    if not gs:
        gs = await _load_gs_cached(room_code)
    if not gs:
        return

    phase       = gs.get("phase", "meet")
    roles       = gs.get("roles", {})
    alive       = gs.get("alive", {})
    players     = gs.get("players", {})
    house_asgn  = gs.get("house_assignments", {})
    night_votes = gs.get("night_votes", {})
    pos_store   = rs.positions
    dl_store    = rs.doorlocks

    ai_local_state.setdefault(room_code, {})
    local = ai_local_state[room_code]

    for ai_uid, pinfo in players.items():
        if not pinfo.get("isAI") or not alive.get(ai_uid):
            continue
        cur = pos_store.get(ai_uid)
        if cur is None:
            continue

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
            pos_store[ai_uid]["anim"] = "idle"
            continue

        cx = cur.get("x", 0.0)
        cz = cur.get("z", 0.0)

        if role == "mafia" and phase == "night":
            anim, cx, cz = _ai_mafia_cosine_move(
                ai_uid, cx, cz, alive, roles, house_asgn,
                pos_store, dl_store, room_code, gs, st, rs,
            )
            rot_dx = st["target_x"] - cx
            rot_dz = st["target_z"] - cz
            rot_y  = math.atan2(rot_dx, rot_dz)
            pos_store[ai_uid] = {"x": round(cx, 3), "y": 0.0, "z": round(cz, 3),
                                  "rot_y": round(rot_y, 3), "anim": anim}
            continue

        raw_tx, raw_tz = _ai_role_target(
            ai_uid, role, phase, alive, roles, house_asgn, pos_store, dl_store, night_votes, st,
        )
        tx, tz = safe_target(raw_tx, raw_tz)
        st["target_x"] = tx; st["target_z"] = tz
        dx = tx - cx; dz = tz - cz
        dist = math.sqrt(dx * dx + dz * dz)
        step = AI_MOVE_SPEED * AI_MOVE_UPDATE_RATE

        if dist < 0.3:
            anim = "idle"
            if is_inside_house(cx, cz):
                cx, cz = push_out_of_houses(cx, cz)
                st["stuck_count"] = 0
        else:
            prev_cx, prev_cz = cx, cz
            cx, cz = safe_move(cx, cz, tx, tz, step)
            anim   = "run" if dist > 5.0 else "walk"
            moved  = math.sqrt((cx - prev_cx) ** 2 + (cz - prev_cz) ** 2)
            if moved < 0.05:
                st["stuck_count"] = st.get("stuck_count", 0) + 1
                if st["stuck_count"] > 3:
                    ea = random.uniform(0, math.pi * 2)
                    st["target_x"], st["target_z"] = safe_target(
                        cx + math.cos(ea) * 2.5, cz + math.sin(ea) * 2.5)
                    st["stuck_count"] = 0
            else:
                st["stuck_count"] = 0

        rot_y = math.atan2(dx, dz) if dist > 0.1 else cur.get("rot_y", 0.0)
        pos_store[ai_uid] = {"x": round(cx, 3), "y": 0.0, "z": round(cz, 3),
                              "rot_y": round(rot_y, 3), "anim": anim}


async def _do_ai_baguette_hit(room_code, ai_uid, target_uid, cx, cz, gs, st):
    rs = rooms.get(room_code)
    if not rs:
        return False
    now      = time.time()
    last_atk = rs.attack_cooldowns.get(ai_uid, 0)
    if now - last_atk < BAGUETTE_HIT_COOLDOWN:
        return False
    rs.attack_cooldowns[ai_uid] = now

    hs = _get_hit_state(rs, ai_uid, target_uid)
    if now - hs["last_hit"] > BAGUETTE_HIT_RESET:
        hs["count"] = 0
    hs["count"]  += 1
    hs["last_hit"] = now
    if DEBUG:
        print(f"[AI-Hit] {ai_uid}->{target_uid} {hs['count']}/{BAGUETTE_HITS_TO_KILL}", flush=True)

    if hs["count"] >= BAGUETTE_HITS_TO_KILL:
        _reset_hit_state(rs, ai_uid, target_uid)
        gs["alive"][target_uid] = False
        await _save_gs_cached(room_code, gs)
        tp = rs.positions.get(target_uid, {})
        await broadcast(room_code, {
            "t": "baguette_kill", "killer": ai_uid, "victim": target_uid,
            "pos_x": tp.get("x", cx), "pos_z": tp.get("z", cz),
        })
        print(f"[AI-킬] {ai_uid} -> {target_uid}", flush=True)
        winner = check_win(gs)
        if winner:
            await end_game(room_code, gs, winner, "baguette")
        st["chase_uid"] = ""
        return True
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
            angle = random.uniform(0, math.pi * 2); r = random.uniform(2, 7)
            st["target_x"] = math.cos(angle) * r; st["target_z"] = math.sin(angle) * r
            st["timer"] = random.uniform(3, 8)
        tx, tz = safe_target(st["target_x"], st["target_z"])
        cx, cz = safe_move(cx, cz, tx, tz, step)
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
    dist = math.sqrt(dx * dx + dz * dz)

    if dist < BAGUETTE_RANGE and not locked:
        asyncio.create_task(_do_ai_baguette_hit(room_code, ai_uid, target_uid, cx, cz, gs, st))
        return "idle", cx, cz

    fwd_x = (dx / dist) if dist > 0.01 else 1.0
    fwd_z = (dz / dist) if dist > 0.01 else 0.0
    perp_x = -fwd_z; perp_z = fwd_x
    st["cosine_travel"] = st.get("cosine_travel", 0.0) + step
    phase_val  = st["cosine_travel"] * AI_COSINE_FREQUENCY + st.get("cosine_phase", 0.0)
    sin_offset = math.sin(phase_val) * AI_COSINE_AMPLITUDE
    tx, tz = safe_target(raw_tx + perp_x * sin_offset, raw_tz + perp_z * sin_offset)
    prev_cx, prev_cz = cx, cz
    cx, cz = safe_move(cx, cz, tx, tz, step)
    moved = math.sqrt((cx - prev_cx) ** 2 + (cz - prev_cz) ** 2)
    if moved < 0.05:
        st["stuck_count"] = st.get("stuck_count", 0) + 1
        if st["stuck_count"] > 5:
            ea  = random.uniform(0, math.pi * 2)
            cx  = clamp_to_world(cx + math.cos(ea) * 1.5, cz)[0]
            cz  = clamp_to_world(cx, cz + math.sin(ea) * 1.5)[1]
            st["stuck_count"] = 0
    else:
        st["stuck_count"] = 0
    return ("run" if dist > 5.0 else "walk"), cx, cz


def _ai_role_target(ai_uid, role, phase, alive, roles, house_asgn,
                    pos_store, dl_store, night_votes, st):
    if role == "police":
        if phase == "night":
            if st["timer"] <= 0:
                all_h = list(dl_store.values())
                if all_h:
                    h = random.choice(all_h)
                    st["spy_house_x"] = h.get("world_x", 0)
                    st["spy_house_z"] = h.get("world_z", 0) + 3.5
                    st["timer"] = random.uniform(15, 40)
            return st.get("spy_house_x", 0), st.get("spy_house_z", 0)
        a = random.uniform(0, math.pi * 2); r = random.uniform(3, 8)
        return math.cos(a) * r, math.sin(a) * r
    elif role == "doctor":
        if phase == "night":
            vote_targets = list(night_votes.values())
            if vote_targets:
                tgt = random.choice(vote_targets)
                tp  = pos_store.get(tgt, {})
                if tp:
                    return float(tp.get("x", 0)) + 2.5, float(tp.get("z", 0)) + 2.5
        a = random.uniform(0, math.pi * 2); r = random.uniform(1, 5)
        return math.cos(a) * r, math.sin(a) * r
    elif role == "citizen":
        if phase == "night":
            hid = house_asgn.get(ai_uid, "")
            h   = dl_store.get(hid, {})
            if h:
                return h.get("world_x", 0), h.get("world_z", 0) - 3.5
        if phase in ("meet", "morning", "dusk"):
            a = random.uniform(0, math.pi * 2); r = random.uniform(2, 6)
            return math.cos(a) * r, math.sin(a) * r
        a = random.uniform(0, math.pi * 2); r = random.uniform(3, 10)
        return math.cos(a) * r, math.sin(a) * r
    else:  # mafia (non-night 랜덤 이동)
        a = random.uniform(0, math.pi * 2); r = random.uniform(2, 6)
        return math.cos(a) * r, math.sin(a) * r


# ─────────────────────────────────────────────────────────────────────────────
# 위치 동기화 루프
# ─────────────────────────────────────────────────────────────────────────────
def _calc_velocity(rs: RoomState, uid: str, cur: dict) -> tuple[float, float]:
    prev = rs.prev_positions.get(uid)
    if not prev:
        return 0.0, 0.0
    dt = TICK_RATE
    if dt <= 0:
        return 0.0, 0.0
    return round((cur.get("x", 0) - prev.get("x", 0)) / dt, 3), \
           round((cur.get("z", 0) - prev.get("z", 0)) / dt, 3)


def _build_pos_with_vel(rs: RoomState, pos: dict) -> dict:
    result = {}
    for uid, p in pos.items():
        vx, vz = _calc_velocity(rs, uid, p)
        entry = dict(p); entry["vx"] = vx; entry["vz"] = vz
        result[uid] = entry
    return result


async def position_sync_loop(room_code: str):
    import json, math as _math
    try:
        while room_code in rooms and rooms[room_code].status == "playing":
            await asyncio.sleep(POSITION_SYNC_INTERVAL)
            rs  = rooms.get(room_code)
            if not rs:
                break
            pos = rs.positions
            if not pos:
                continue

            for uid, pinfo in list(rs.players.items()):
                if pinfo.get("isAI"):
                    continue
                my_pos = pos.get(uid)

                if not my_pos:
                    try:
                        payload = _build_pos_with_vel(rs, pos)
                        await pinfo["ws"].send_text(
                            json.dumps({"t": "pos_sync", "positions": payload,
                                        "ts": int(time.time() * 1000)}, ensure_ascii=False))
                    except:
                        pass
                    continue

                mx, mz = my_pos.get("x", 0), my_pos.get("z", 0)
                payload = {}
                for other_uid, other_p in pos.items():
                    if other_uid == uid:
                        continue
                    dx   = other_p.get("x", 0) - mx
                    dz   = other_p.get("z", 0) - mz
                    dist = _math.sqrt(dx * dx + dz * dz)
                    if dist <= LOD_NEAR_DIST:
                        vx, vz = _calc_velocity(rs, other_uid, other_p)
                        entry = dict(other_p); entry["vx"] = vx; entry["vz"] = vz
                        payload[other_uid] = entry

                if payload:
                    try:
                        await pinfo["ws"].send_text(
                            json.dumps({"t": "pos_sync", "positions": payload,
                                        "ts": int(time.time() * 1000)}, ensure_ascii=False))
                    except:
                        pass

        rs = rooms.get(room_code)
        if rs:
            rs.prev_positions.clear()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[PosSync] 오류: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# 전역 AI 스케줄러 (S급: 중복 실행 방지)
# ─────────────────────────────────────────────────────────────────────────────
_AI_SCHEDULER_RUNNING = False


async def global_ai_scheduler():
    global _AI_SCHEDULER_RUNNING
    # S급: 이미 실행 중이면 즉시 리턴
    if _AI_SCHEDULER_RUNNING:
        print("[AI-Sched] 이미 실행 중 — 중복 방지로 종료", flush=True)
        return
    _AI_SCHEDULER_RUNNING = True
    print("[AI-Sched] 전역 AI 스케줄러 시작", flush=True)
    try:
        while True:
            await asyncio.sleep(AI_MOVE_UPDATE_RATE)
            for room_code, rs in list(rooms.items()):
                if rs.status != "playing":
                    continue
                try:
                    await tick_ai_for_room(room_code, rs)
                except Exception as e:
                    import traceback
                    print(f"[AI-Sched] {room_code} 오류: {e}\n{traceback.format_exc()}", flush=True)
    except asyncio.CancelledError:
        print("[AI-Sched] 종료", flush=True)
    finally:
        _AI_SCHEDULER_RUNNING = False


# ─────────────────────────────────────────────────────────────────────────────
# 세션 초기화
# ─────────────────────────────────────────────────────────────────────────────
async def init_session(room_code: str, room_data: dict, pre_connections: dict) -> dict:
    pdata    = room_data.get("players", {})
    host_uid = room_data.get("hostId", "")
    uids     = list(pdata.keys())
    total    = len(uids)

    rc  = calc_roles(total)
    rl  = []
    for role, cnt in rc.items():
        rl.extend([role] * cnt)
    shuffled = uids[:]
    random.shuffle(shuffled); random.shuffle(rl)
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
    old_ws: dict = {}
    if room_code in rooms:
        old_rs = rooms[room_code]
        if old_rs.phase_task and not old_rs.phase_task.done(): old_rs.phase_task.cancel()
        if old_rs.pos_task   and not old_rs.pos_task.done():   old_rs.pos_task.cancel()
        # 준버그: 남은 task도 전부 정리
        for t in old_rs.tasks:
            if not t.done():
                t.cancel()
        old_rs.tasks.clear()
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
                "tag": pi.get("tag", "0000"), "isHost": pi.get("isHost", False), "isAI": False,
            }
    pre_connections.pop(room_code, None)

    # 새 RoomState 생성
    rs = RoomState()
    init_doorlocks(room_code, gs, rs)

    # 초기 위치
    for i, uid in enumerate(uids):
        angle = (2 * math.pi / max(total, 1)) * i
        rs.positions[uid] = {
            "x": round(math.cos(angle) * 4, 2), "y": 0.0,
            "z": round(math.sin(angle) * 4, 2), "rot_y": 0.0, "anim": "idle",
        }

    rooms[room_code] = rs
    ai_local_state.pop(room_code, None)

    # WS 복구
    for uid, p in old_ws.items():
        if uid in gs["players"] and not gs["players"][uid].get("isAI"):
            pi = gs["players"][uid]
            rs.players[uid] = {
                "ws": p["ws"], "nickname": pi["nickname"], "tag": pi["tag"],
                "isHost": pi.get("isHost", False), "isAI": False,
            }

    # gs 캐시 초기화
    rs.gs = gs
    await save_gs(room_code, gs)
    print(f"[Init] {room_code} | {total}명 | {rc} | WS복구={len(rs.players)}명", flush=True)
    return gs


async def send_joined_to_all(room_code: str, gs: dict):
    rs = rooms.get(room_code)
    if not rs:
        return
    ha = gs.get("house_assignments", {})
    for uid, pinfo in list(rs.players.items()):
        if pinfo.get("isAI"):
            continue
        try:
            my_role  = gs["roles"].get(uid, "citizen")
            my_house = ha.get(uid, "")
            hi       = rs.doorlocks.get(my_house, {})
            eco      = get_economy(gs, uid)
            await pinfo["ws"].send_text(__import__("json").dumps({
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
            if DEBUG:
                print(f"[Init] joined 전송: {uid}", flush=True)
        except Exception as e:
            print(f"[Init] joined 전송 실패 {uid}: {e}", flush=True)


async def send_house_hints(room_code: str, gs: dict):
    rs = rooms.get(room_code)
    if not rs:
        return
    all_hids = list(rs.doorlocks.keys())
    for uid in gs.get("alive", {}):
        if not gs["alive"].get(uid):
            continue
        hid = gs.get("house_assignments", {}).get(uid, "")
        h   = rs.doorlocks.get(hid, {})
        if not h:
            continue
        num = all_hids.index(hid) + 1 if hid in all_hids else "?"
        await send_to(room_code, uid, {
            "t": "house_hint", "house_id": hid, "house_num": num,
            "world_x": h.get("world_x", 0), "world_z": h.get("world_z", 0),
            "msg": f"당신의 집은 {num}번 집입니다!\n밤에 [E]키로 문을 잠그세요!",
        })


# ─────────────────────────────────────────────────────────────────────────────
# 게임 종료
# ─────────────────────────────────────────────────────────────────────────────
async def end_game(room_code: str, gs: dict, winner: str, reason: str):
    gs["phase"] = "result"; gs["winner"] = winner
    await _save_gs_cached(room_code, gs)
    await broadcast(room_code, {
        "t": "game_result", "winner": winner, "reason": reason,
        "roles": gs["roles"], "alive": gs["alive"], "day": gs["day"],
    })
    rs = rooms.get(room_code)
    if rs:
        rs.status = "ended"
        rs.phase_running = False
        if rs.pos_task and not rs.pos_task.done(): rs.pos_task.cancel()
        # 준버그: 남은 task 전부 정리
        for t in rs.tasks:
            if not t.done():
                t.cancel()
        rs.tasks.clear()
    ai_local_state.pop(room_code, None)
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "ended"})
    asyncio.create_task(_delete_ai_rtdb(room_code, gs))
    asyncio.create_task(_cleanup(room_code, 60))
    print(f"[End] {room_code} winner={winner}", flush=True)


async def _delete_ai_rtdb(room_code: str, gs: dict):
    from config import RTDB_URL, RTDB_SECRET
    ai_uids = [uid for uid, p in gs.get("players", {}).items() if p.get("isAI")]
    if not ai_uids:
        return
    def _rtdb_url(path): return f"{RTDB_URL}{path}.json?auth={RTDB_SECRET}"
    await asyncio.gather(
        *[http_delete(_rtdb_url(f"rooms/{room_code}/players/{uid}")) for uid in ai_uids],
        return_exceptions=True,
    )


async def _cleanup(room_code: str, delay: int = 60):
    await asyncio.sleep(delay)
    rs = rooms.get(room_code)
    if rs and rs.status == "ended":
        rooms.pop(room_code, None)
        ai_local_state.pop(room_code, None)
        from state import room_locks
        room_locks.pop(room_code, None)
        await delete_gs(room_code)
        print(f"[Cleanup] {room_code} 삭제", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# 페이즈 루프 (S급: phase_running으로 중복 실행 방지)
# ─────────────────────────────────────────────────────────────────────────────
async def phase_loop(room_code: str):
    rs = rooms.get(room_code)
    if not rs:
        return

    # S급: 중복 실행 방지
    if rs.phase_running:
        print(f"[Phase] {room_code} 이미 실행 중 — 중복 방지로 종료", flush=True)
        return
    rs.phase_running = True

    print(f"[Phase] {room_code} 루프 시작", flush=True)
    try:
        gs = await _load_gs_cached(room_code)
        phase_queue = ["meet"] + PHASE_CYCLE * (MAX_ROUNDS + 2)

        for phase in phase_queue:
            rs = rooms.get(room_code)
            if not rs or rs.status != "playing":
                return
            gs["phase"] = phase
            _gs_update_cache(room_code, gs)
            await save_gs(room_code, gs)
            wait = PHASE_TIMES.get(phase, 60)

            if phase == "night":
                reset_all_hits(rs)
                lock_all_doors(rs)
                await broadcast(room_code, {
                    "t": "lighting_change", "phase": "night",
                    "house_lights": False, "plaza_lights": True, "street_lights_energy": 3.5,
                })
                await broadcast(room_code, {"t": "doorlock_all_locked", "msg": "모든 집이 잠깁니다."})
                # 준버그: task 등록
                t = asyncio.create_task(ai_do_night_actions(room_code))
                rs.tasks.append(t)

            elif phase == "morning":
                async with get_room_lock(room_code):
                    mr = await process_night(room_code, gs, rs)
                    gs["morning_result"] = mr
                    gs = give_morning_bread(gs)
                    await _save_gs_cached(room_code, gs)
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
                            "msg": f"아침이 밝았습니다. 500브래드 지급! (보유: {eco['bread']}🍞)",
                        })
                await send_house_hints(room_code, gs)
                await broadcast(room_code, {
                    "t": "phase", "phase": "morning", "day": gs["day"],
                    "time": wait, "result": mr,
                    "house_assignments": gs.get("house_assignments", {}),
                    "doorlock_states": dl_payload(room_code),
                })
                for rem in range(wait - 1, -1, -1):
                    if room_code not in rooms: return
                    await asyncio.sleep(1)
                    await broadcast(room_code, {"t": "tick", "time": rem})
                winner = check_win(gs)
                if winner or gs["day"] >= MAX_ROUNDS:
                    await end_game(room_code, gs, winner or "citizen", "elimination"); return
                continue

            elif phase == "vote":
                t = asyncio.create_task(ai_do_vote_actions(room_code))
                rs.tasks.append(t)
            elif phase == "meet":
                await send_house_hints(room_code, gs)
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
                    "doorlock_states": dl_payload(room_code),
                })
                for rem in range(wait - 1, -1, -1):
                    if room_code not in rooms: return
                    await asyncio.sleep(1)
                    await broadcast(room_code, {"t": "tick", "time": rem})

            if room_code not in rooms: return
            async with get_room_lock(room_code):
                gs = await _load_gs_cached(room_code)

            if phase == "vote":
                async with get_room_lock(room_code):
                    elim = await process_votes(room_code, gs)
                    await broadcast(room_code, {
                        "t": "vote_result", "eliminated": elim,
                        "alive": gs["alive"], "tie_pool": gs["tie_pool"],
                    })
                    winner = check_win(gs)
                    if winner or gs["day"] >= MAX_ROUNDS:
                        await end_game(room_code, gs, winner or "citizen", "vote"); return
                    gs["day"] += 1
                    await _save_gs_cached(room_code, gs)

    except asyncio.CancelledError:
        print(f"[Phase] {room_code} 취소", flush=True)
    except Exception as e:
        import traceback
        print(f"[Phase] {room_code} 오류: {e}\n{traceback.format_exc()}", flush=True)
    finally:
        # S급: 루프 종료 시 반드시 플래그 해제
        rs = rooms.get(room_code)
        if rs:
            rs.phase_running = False
