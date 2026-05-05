"""
state.py — RoomState 정의 + 전역 저장소
의존: config
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RoomState:
    status: str = "waiting"           # waiting / playing / ended
    players: dict = field(default_factory=dict)       # uid -> {ws, nickname, tag, isHost, isAI}
    positions: dict = field(default_factory=dict)     # uid -> {x,y,z,rot_y,anim}
    prev_positions: dict = field(default_factory=dict)
    doorlocks: dict = field(default_factory=dict)     # hid -> {...}
    attack_cooldowns: dict = field(default_factory=dict)
    baguette_hits: dict = field(default_factory=dict)
    move_warnings: dict = field(default_factory=dict) # uid -> {count, last_ts}
    phase_task: Optional[asyncio.Task] = None
    pos_task: Optional[asyncio.Task] = None


# ── 전역 저장소 ───────────────────────────────────────────────────────────────
# 모든 모듈이 여기서 import해서 사용. 절대 다른 모듈에서 재정의하지 않음.
rooms: dict[str, RoomState] = {}
pre_connections: dict[str, dict] = {}   # room_code -> {uid -> ws}
ai_local_state: dict[str, dict] = {}   # room_code -> {uid -> local_state}
