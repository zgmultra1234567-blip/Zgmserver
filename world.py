"""
world.py — 월드 물리 유틸 + 이동 검증
의존: config, state
"""
import math, time
from config import (
    WORLD_BOUND, HOUSE_HALF, HOUSE_CENTERS,
    PLAYER_MAX_DIST_PER_TICK, MAX_TELEPORT_DIST,
    MOVE_WARN_THRESHOLD, MOVE_WARN_DECAY_SEC,
    DEBUG,
)
from state import rooms


# ── 좌표 유틸 ─────────────────────────────────────────────────────────────────
def clamp_to_world(x: float, z: float) -> tuple[float, float]:
    return max(-WORLD_BOUND, min(WORLD_BOUND, x)), max(-WORLD_BOUND, min(WORLD_BOUND, z))


def is_inside_house(x: float, z: float) -> bool:
    for hx, hz in HOUSE_CENTERS:
        if abs(x - hx) < HOUSE_HALF and abs(z - hz) < HOUSE_HALF:
            return True
    return False


def push_out_of_houses(x: float, z: float) -> tuple[float, float]:
    for hx, hz in HOUSE_CENTERS:
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


def safe_move(cur_x: float, cur_z: float, target_x: float, target_z: float, step: float) -> tuple[float, float]:
    dx = target_x - cur_x
    dz = target_z - cur_z
    dist = math.sqrt(dx * dx + dz * dz)
    if dist < 0.01:
        return cur_x, cur_z
    ratio = min(step / dist, 1.0)
    nx = cur_x + dx * ratio
    nz = cur_z + dz * ratio
    if is_inside_house(nx, nz):
        nx, nz = push_out_of_houses(nx, nz)
    return clamp_to_world(nx, nz)


def safe_target(tx: float, tz: float) -> tuple[float, float]:
    tx, tz = clamp_to_world(tx, tz)
    if is_inside_house(tx, tz):
        tx, tz = push_out_of_houses(tx, tz)
    return tx, tz


# ── 이동 검증 ─────────────────────────────────────────────────────────────────
def validate_move(
    room_code: str, uid: str, new_x: float, new_z: float
) -> tuple[bool, float, float]:
    """
    클라가 보낸 위치를 검증.
    반환: (accepted, corrected_x, corrected_z)
    - 텔레포트 수준 → 거부, 이전 위치 반환
    - 속도 초과    → 방향 유지 + 거리 클램프
    """
    rs = rooms.get(room_code)
    if not rs:
        return True, new_x, new_z

    prev = rs.positions.get(uid)
    if not prev:
        return True, new_x, new_z

    px, pz = prev.get("x", 0.0), prev.get("z", 0.0)
    dx = new_x - px
    dz = new_z - pz
    dist = math.sqrt(dx * dx + dz * dz)

    # 텔레포트 수준 → 즉시 거부
    if dist > MAX_TELEPORT_DIST:
        _record_warning(rs, uid, dist, "텔레포트")
        return False, px, pz

    # 속도 초과 → 보정
    if dist > PLAYER_MAX_DIST_PER_TICK:
        _record_warning(rs, uid, dist, "속도초과")
        ratio  = PLAYER_MAX_DIST_PER_TICK / dist
        corr_x = clamp_to_world(px + dx * ratio, pz + dz * ratio)[0]
        corr_z = clamp_to_world(px + dx * ratio, pz + dz * ratio)[1]
        return True, corr_x, corr_z

    return True, new_x, new_z


def _record_warning(rs, uid: str, dist: float, label: str):
    now  = time.time()
    warn = rs.move_warnings.setdefault(uid, {"count": 0, "last_ts": now})
    if now - warn["last_ts"] > MOVE_WARN_DECAY_SEC:
        warn["count"] = max(0, warn["count"] - 1)
    warn["count"]  += 1
    warn["last_ts"] = now
    # 반복 의심은 항상 출력 (보안 로그이므로 DEBUG 무관)
    if warn["count"] > MOVE_WARN_THRESHOLD:
        print(f"[MoveHack] {uid} {label} 반복 의심 (dist={dist:.2f}, warn={warn['count']})", flush=True)
