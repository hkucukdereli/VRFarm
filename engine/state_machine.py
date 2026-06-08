"""
engine/state_machine.py

Generic YAML-driven state machine. Paradigm as data, not code.
States, transitions, entry/exit actions, and event handlers
are all defined in the task YAML.
"""

from __future__ import annotations
import random
import re
import time
from dataclasses import dataclass, field


@dataclass
class EventHandler:
    condition: str          # e.g. "is_go and level in [1, 3]"
    action: str             # action name to execute
    transition: str = ""    # state to transition to ("" = stay)


@dataclass
class StateDef:
    name: str
    duration: float | dict | None = None  # fixed, {"random": [min,max]}, or None
    entry_actions: list[str] = field(default_factory=list)
    exit_actions: list[str] = field(default_factory=list)
    events: dict[str, list[EventHandler]] = field(default_factory=dict)
    on_timeout: str = ""    # next state when duration expires


class StateMachine:
    """Execute a state machine defined by StateDef objects."""

    def __init__(self, states: dict[str, StateDef],
                 actions: dict[str, callable],
                 context: dict):
        self.states = states
        self.actions = actions
        self.ctx = context
        self.current: str | None = None
        self.entered_at: float = 0.0
        self.duration: float = 0.0

    def enter(self, name: str):
        """Enter a state: resolve duration, run entry actions."""
        if self.current and self.current in self.states:
            for action_name in self.states[self.current].exit_actions:
                self._run_action(action_name)

        self.current = name
        state = self.states[name]
        self.entered_at = time.time()
        self.duration = self._resolve_duration(state.duration)
        for action_name in state.entry_actions:
            self._run_action(action_name)

    def tick(self, events: list[str]) -> str | None:
        """Called each cycle. Check events and timeout.
        Returns new state name if transition occurred, None if staying.
        """
        if self.current is None:
            return None

        state = self.states[self.current]
        elapsed = time.time() - self.entered_at

        # Check event handlers
        for event_name in events:
            if event_name in state.events:
                for handler in state.events[event_name]:
                    if self._eval_condition(handler.condition):
                        if handler.action:
                            self._run_action(handler.action)
                        if handler.transition:
                            self.enter(handler.transition)
                            return handler.transition

        # Check timeout
        if self.duration is not None and self.duration > 0:
            if elapsed >= self.duration:
                if state.on_timeout:
                    self.enter(state.on_timeout)
                    return state.on_timeout
        elif self.duration is None or self.duration == 0:
            # Immediate transition (no duration)
            if state.on_timeout:
                self.enter(state.on_timeout)
                return state.on_timeout

        return None

    def elapsed(self) -> float:
        """Seconds since entering current state."""
        return time.time() - self.entered_at

    def _resolve_duration(self, spec) -> float | None:
        if spec is None:
            return None
        if isinstance(spec, (int, float)):
            return float(spec)
        if isinstance(spec, dict) and "random" in spec:
            lo, hi = spec["random"]
            return random.uniform(lo, hi)
        return None

    def _run_action(self, name: str):
        if name in self.actions:
            self.actions[name]()

    def _eval_condition(self, cond: str) -> bool:
        """Safe condition evaluator. No eval()."""
        if not cond or cond.strip() == "":
            return True
        return _safe_eval(cond, self.ctx, self.elapsed())


# ── Safe condition evaluator ──

def _safe_eval(expr: str, ctx: dict, elapsed: float) -> bool:
    """Evaluate a condition string against a context dict.
    Supports: "is_go", "level == 3", "level in [1, 3]",
              "elapsed > 0.6", "lick_count > max_licks",
              "a and b", "a or b", "not a"
    """
    expr = expr.strip()

    # Handle "and" / "or" (split on outermost)
    # Simple approach: split on " and " and " or " (not inside brackets)
    if " and " in expr:
        parts = expr.split(" and ", 1)
        return _safe_eval(parts[0], ctx, elapsed) and \
               _safe_eval(parts[1], ctx, elapsed)
    if " or " in expr:
        parts = expr.split(" or ", 1)
        return _safe_eval(parts[0], ctx, elapsed) or \
               _safe_eval(parts[1], ctx, elapsed)
    if expr.startswith("not "):
        return not _safe_eval(expr[4:], ctx, elapsed)

    # "elapsed > N" / "elapsed >= N"
    m = re.match(r"elapsed\s*(>=?|<=?)\s*([0-9.]+)", expr)
    if m:
        op, val = m.group(1), float(m.group(2))
        return _compare(elapsed, op, val)

    # "var op value" patterns
    m = re.match(r"(\w+)\s*(==|!=|>=?|<=?)\s*(.+)", expr)
    if m:
        var_name, op, rhs_str = m.group(1), m.group(2), m.group(3).strip()
        lhs = _resolve_var(var_name, ctx)
        rhs = _parse_value(rhs_str, ctx)
        return _compare(lhs, op, rhs)

    # "var in [a, b, c]"
    m = re.match(r"(\w+)\s+in\s+\[(.+)\]", expr)
    if m:
        var_name = m.group(1)
        items_str = m.group(2)
        lhs = _resolve_var(var_name, ctx)
        items = [_parse_value(s.strip(), ctx) for s in items_str.split(",")]
        return lhs in items

    # Bare variable name (truthy check)
    return bool(_resolve_var(expr, ctx))


def _resolve_var(name: str, ctx: dict):
    """Look up a variable in context. Supports dot notation (trial.is_go)."""
    if "." in name:
        parts = name.split(".")
        val = ctx
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                return None
        return val
    return ctx.get(name)


def _parse_value(s: str, ctx: dict):
    """Parse a value string: number, string, or context variable."""
    s = s.strip()
    # Number
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    # Quoted string
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    # Context variable
    val = _resolve_var(s, ctx)
    if val is not None:
        return val
    return s


def _compare(lhs, op: str, rhs) -> bool:
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    return False


# ── YAML parsing ──

def parse_states(raw: dict) -> dict[str, StateDef]:
    """Parse states: section from task YAML into StateDef objects."""
    states = {}
    for name, sdef in raw.items():
        events = {}
        for evt_name, handlers_raw in (sdef.get("events") or {}).items():
            if isinstance(handlers_raw, dict):
                handlers_raw = [handlers_raw]
            events[evt_name] = [
                EventHandler(
                    condition=h.get("condition", ""),
                    action=h.get("action", ""),
                    transition=h.get("transition", ""),
                )
                for h in handlers_raw
            ]
        states[name] = StateDef(
            name=name,
            duration=sdef.get("duration"),
            entry_actions=sdef.get("entry_actions", []),
            exit_actions=sdef.get("exit_actions", []),
            events=events,
            on_timeout=sdef.get("on_timeout", ""),
        )
    return states
