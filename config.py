"""
config.py — 모든 상수 정의
의존: 없음
"""
import os

# 디버그 모드 (False = print 억제)
DEBUG = os.environ.get("DEBUG", "0") == "1"

# Firebase / Redis
RTDB_URL    = "https://zgm-base-default-rtdb.asia-southeast1.firebasedatabase.app/"
RTDB_SECRET = os.environ.get("RTDB_SECRET", "")
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
WS_PORT     = int(os.environ.get("PORT", 7860))

# 페이즈
PHASE_TIMES = {"meet": 60, "night": 120, "morning": 15, "day": 180, "dusk": 20}
PHASE_CYCLE = ["night", "morning", "day", "dusk"]
MAX_ROUNDS  = 10
SESSION_TTL = 3600

# 틱레이트
TICK_RATE              = 1.0 / 15.0
POSITION_SYNC_INTERVAL = TICK_RATE
AI_MOVE_UPDATE_RATE    = TICK_RATE

# 도어락
DOORLOCK_HACK_DURATION = 15.0
DOORLOCK_SUCCESS_RATE  = 0.67

# 바게트 전투
BAGUETTE_RANGE        = 2.5
BAGUETTE_HIT_COOLDOWN = 0.8
BAGUETTE_HITS_TO_KILL = 2
BAGUETTE_HIT_RESET    = 8.0

# 경제
MORNING_BREAD        = 500
BATTERY_PRICE        = 500
BATTERY_DURATION_SEC = 60.0
POTION_PRICE         = 700
POTION_DURATION      = 20.0

# AI
AI_IDLE_CHANCE      = 0.05
AI_MOVE_SPEED       = 3.5
AI_ACTION_DELAY_MIN = 3
AI_ACTION_DELAY_MAX = 10
AI_COSINE_AMPLITUDE = 1.8
AI_COSINE_FREQUENCY = 0.9

# 월드 / LOD
LOD_NEAR_DIST = 60.0
WORLD_BOUND   = 48.0
HOUSE_SPACING = 14.0
HOUSE_HALF    = 2.8
DOOR_OFFSET_Z = 2.52

# 이동 검증
PLAYER_MAX_SPEED         = 6.0
PLAYER_MAX_DIST_PER_TICK = PLAYER_MAX_SPEED * TICK_RATE * 2.5
MAX_TELEPORT_DIST        = 8.0
MOVE_WARN_THRESHOLD      = 5
MOVE_WARN_DECAY_SEC      = 10.0

# 집 중심 좌표 목록
HOUSE_CENTERS: list[tuple[float, float]] = []
for _r in (-1, 0, 1):
    for _c in (-1, 0, 1):
        if _r == 0 and _c == 0:
            continue
        HOUSE_CENTERS.append((_c * 14.0, _r * 14.0))
for _v in [(-28,-14),(28,-14),(-28,14),(28,14),(-14,-28),(14,-28),(-14,28),(14,28)]:
    HOUSE_CENTERS.append(_v)
