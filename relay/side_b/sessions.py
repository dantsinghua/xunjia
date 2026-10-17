"""会话状态(session = 意向 × 对手 × 券)。内存 + 落盘 state/sessions.json 便于观察;留痕 state/trace.jsonl 只增不改。

方向符号写死(docs/05 §7):bid 起始=估值+起始、边界=估值+边界,让步=偏离往下走;ofr 镜像取负。
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("B_STATE_DIR") or (HERE / "state"))

# 默认策略模板(起始/探价边界/步幅,单位 BP,绝对值;符号按方向在代码里加)。可用 B_TEMPLATE="10,5,2" 覆盖。
_tpl = (os.environ.get("B_TEMPLATE") or "10,5,2").split(",")
DEFAULT_START_BP, DEFAULT_BOUNDARY_BP, DEFAULT_STEP_BP = (float(x) for x in _tpl)
DEFAULT_VOL_WAN = int(os.environ.get("B_VOL_WAN", "3000"))

# 状态(docs/05 §1)
PENDING, ROUND_SENT, ASK_VOL, TO_HUMAN, CLOSED_NOPRICE, SUSPENDED = (
    "PENDING", "ROUND_SENT", "ASK_VOL", "TO_HUMAN", "CLOSED_NOPRICE", "SUSPENDED")
OPEN_STATES = (PENDING, ROUND_SENT, ASK_VOL, SUSPENDED)


def sign_of(direction: str) -> int:
    """bid: +1(收益率越高越有利);ofr: -1。写死,不配置。"""
    return 1 if direction == "bid" else -1


@dataclass
class Session:
    key: str
    thread: str
    windcode: str
    简称: str = ""
    direction: str = "bid"            # bid / ofr
    start_bp: float = DEFAULT_START_BP       # 绝对值
    boundary_bp: float = DEFAULT_BOUNDARY_BP  # 绝对值
    step_bp: float = DEFAULT_STEP_BP
    vol_wan: int = DEFAULT_VOL_WAN           # 本次询价量
    investable_wan: int | None = None        # 本方可投量(内部字段,绝不外发)
    valuation: float | None = None           # 中债估值(绝对价换算用)
    round: int = 1                           # 第 N 轮(默认视为 A 侧已发出第 1 轮起始价)
    current_bp: float = DEFAULT_START_BP     # 当前报价偏离(绝对值)
    state: str = ROUND_SENT
    state_reason: str = ""
    asked_vol: bool = False                  # 追问量每会话一次
    pending_price: dict | None = None        # 追问量时挂起的对方价
    agreed_bp: float | None = None           # 对方还价落在边界内时锁定的偏离
    history: list = field(default_factory=list)
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    @property
    def max_rounds(self) -> int:
        return int(math.ceil((self.start_bp - self.boundary_bp) / self.step_bp)) + 1

    @property
    def signed_current(self) -> float:
        return sign_of(self.direction) * self.current_bp

    @property
    def signed_boundary(self) -> float:
        return sign_of(self.direction) * self.boundary_bp

    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def at_boundary(self) -> bool:
        return self.current_bp <= self.boundary_bp + 1e-9

    def concede(self) -> float:
        """让一步幅,越界钳到边界;返回新的 current_bp(绝对值)。"""
        nxt = self.current_bp - self.step_bp
        self.current_bp = max(nxt, self.boundary_bp)
        self.round += 1
        return self.current_bp


class SessionStore:
    def __init__(self, state_dir: Path = STATE_DIR) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "sessions.json"
        self.trace_file = self.dir / "trace.jsonl"
        self.processed_file = self.dir / "processed.json"
        self.sessions: dict[str, Session] = {}
        self.processed: set[str] = set()
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if self.file.exists():
            try:
                for k, v in json.loads(self.file.read_text("utf-8")).items():
                    self.sessions[k] = Session(**v)
            except Exception:
                pass
        if self.processed_file.exists():
            try:
                self.processed = set(json.loads(self.processed_file.read_text("utf-8")))
            except Exception:
                pass

    def save(self) -> None:
        self.file.write_text(json.dumps({k: asdict(s) for k, s in self.sessions.items()},
                                        ensure_ascii=False, indent=1), "utf-8")
        self.processed_file.write_text(json.dumps(sorted(self.processed)[-5000:], ensure_ascii=False), "utf-8")

    def trace(self, **rec) -> None:
        rec.setdefault("ts", time.time())
        with self.trace_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- 归属(L1 券锚 → L3 单会话) ----
    @staticmethod
    def key_of(thread: str, windcode: str) -> str:
        return f"{thread}|{windcode}"

    def open_sessions_of(self, thread: str) -> list[Session]:
        return [s for s in self.sessions.values() if s.thread == thread and s.is_open()]

    def get_or_create(self, thread: str, hint: dict) -> Session:
        """按 session_hint 券锚建/取会话。hint 可带 方向/起始_bp/边界_bp/步幅_bp/量_wan/估值/可投量_wan/当前_bp。"""
        windcode = str(hint["windcode"])
        key = self.key_of(thread, windcode)
        s = self.sessions.get(key)
        if s is None:
            s = Session(key=key, thread=thread, windcode=windcode, 简称=str(hint.get("简称") or ""))
            s.direction = "ofr" if str(hint.get("方向", "bid")).lower() in ("ofr", "卖出", "sell") else "bid"
            if hint.get("起始_bp") is not None:
                s.start_bp = abs(float(hint["起始_bp"]))
            if hint.get("边界_bp") is not None:
                s.boundary_bp = abs(float(hint["边界_bp"]))
            if hint.get("步幅_bp") is not None:
                s.step_bp = abs(float(hint["步幅_bp"]))
            s.current_bp = abs(float(hint["当前_bp"])) if hint.get("当前_bp") is not None else s.start_bp
            if hint.get("量_wan") is not None:
                s.vol_wan = int(hint["量_wan"])
            if hint.get("可投量_wan") is not None:
                s.investable_wan = int(hint["可投量_wan"])
            if hint.get("估值") is not None:
                s.valuation = float(hint["估值"])
            self.sessions[key] = s
        else:
            if hint.get("简称") and not s.简称:
                s.简称 = str(hint["简称"])
            if hint.get("估值") is not None:
                s.valuation = float(hint["估值"])
        return s
