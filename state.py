"""
state.py — RoomState 정의 + 전역 저장소
의존: config
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RoomState:
    status: str = "waiting"               # waiting / playing / ended
    players: dict = field(default_factory=dict)         # uid -> {ws, nickname, tag, isHost, isAI}
    positions: dict = field(default_factory=dict)       # uid -> {x,y,z,rot_y,anim}
    prev_positions: dict = field(default_factory=dict)
    doorlocks: dict = field(default_factory=dict)       # hid -> {...}
    attack_cooldowns: dict = field(default_factory=dict)
    baguette_hits: dict = field(default_factory=dict)
    move_warnings: dict = field(default_factory=dict)   # uid -> {count, last_ts}
    phase_task: Optional[asyncio.Task] = None
    pos_task: Optional[asyncio.Task] = None

    # ── 버그 수정 추가 필드 ──────────────────────────────────────────────────
    # S급: phase_loop 중복 실행 방지
    phase_running: bool = False

    # A급: Redis 과호출 방지용 인메모리 gs 캐시
    gs: dict = field(default_factory=dict)

    # 준버그: 게임 종료 시 정리할 task 목록
    tasks: list = field(default_factory=list)


# ── 전역 저장소 ───────────────────────────────────────────────────────────────
# 모든 모듈이 여기서 import해서 사용. 절대 다른 모듈에서 재정의하지 않음.
rooms: dict[str, RoomState] = {}
pre_connections: dict[str, dict] = {}   # room_code -> {uid -> ws}
ai_local_state: dict[str, dict] = {}   # room_code -> {uid -> local_state}

# S급: room별 lock (save_gs 레이스 방지)
room_locks: dict[str, asyncio.Lock] = {}

def get_room_lock(room_code: str) -> asyncio.Lock:
    if room_code not in room_locks:
        room_locks[room_code] = asyncio.Lock()
    return room_locks[room_code]
