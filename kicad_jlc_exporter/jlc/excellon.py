"""Excellon 钻孔文件解析（兼容 EasyEDA 与 KiCad 两种头部写法）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class DrillTool:
    code: int
    diameter: float
    hits: int = 0


@dataclass
class DrillFile:
    path: str
    units: str = "mm"                 # mm / inch
    plated: bool | None = None
    tools: dict[int, DrillTool] = field(default_factory=dict)
    holes: list[tuple[float, float, int]] = field(default_factory=list)  # x, y, tool
    # G85 槽孔：(x1, y1, x2, y2, tool)。嘉立创EDA 把异形孔/长孔写成槽孔，
    # 不解析的话孔数会少算、孔径统计也会漏掉（见 README 的样本2 说明）。
    slots: list[tuple[float, float, float, float, int]] = field(default_factory=list)
    header_comments: list[str] = field(default_factory=list)
    file_function: str | None = None  # X2: Plated / NonPlated
    # 见到但未实现的语法（铣槽模式、模态省略歧义等）：非空即代表孔数据不可信
    unsupported: list[str] = field(default_factory=list)

    @property
    def total_holes(self) -> int:
        return len(self.holes)

    @property
    def total_slots(self) -> int:
        return len(self.slots)

    @property
    def total_features(self) -> int:
        return len(self.holes) + len(self.slots)

    def min_diameter(self) -> float | None:
        ds = [t.diameter for t in self.tools.values() if t.diameter > 0 and t.hits > 0]
        if not ds:
            ds = [t.diameter for t in self.tools.values() if t.diameter > 0]
        return min(ds) if ds else None

    def bbox(self):
        pts = [(h[0], h[1]) for h in self.holes]
        pts += [(s[0], s[1]) for s in self.slots] + [(s[2], s[3]) for s in self.slots]
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def duplicate_holes(self) -> list[tuple[float, float, int]]:
        seen: dict[tuple[float, float, int], int] = {}
        dups = []
        for (x, y, t) in self.holes:
            k = (round(x, 4), round(y, 4), t)
            seen[k] = seen.get(k, 0) + 1
            if seen[k] == 2:
                dups.append(k)
        return dups


_METRIC = re.compile(r"^(METRIC|INCH)(?:,(LZ|TZ))?(?:,([\d.]+))?", re.I)
_TOOLDEF = re.compile(r"^T(\d+)(?:[FSB][\d.]+)*C([\d.]+)", re.I)
_SEL = re.compile(r"^T(\d+)$", re.I)
_XY = re.compile(r"^(?:X([-+]?[\d.]+))?(?:Y([-+]?[\d.]+))?$")
# G85 槽孔：X1Y1G85X2Y2（也可能写成 X1Y1G85X2Y2 之外的形式，先支持这一种）
_SLOT = re.compile(r"^X([-+]?[\d.]+)Y([-+]?[\d.]+)G85X([-+]?[\d.]+)Y([-+]?[\d.]+)$", re.I)
# 铣槽移动：G00X..Y..（空移）/ G01X..Y..（铣削），坐标可省略（模态）
_ROUTE = re.compile(r"^G0?([01])(?:X([-+]?[\d.]+))?(?:Y([-+]?[\d.]+))?$", re.I)


def parse(path: str) -> DrillFile:
    df = DrillFile(path=path)
    cur_tool: int | None = None
    decimals = 3
    in_header = False
    last_x = last_y = 0.0        # 模态坐标：只有 Y 时 X 沿用上一个值
    routing = False              # 铣槽状态（M15 下刀 … M16 抬刀）
    has_decimal_point = False

    for raw in open(path, "r", encoding="utf-8", errors="replace"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith(";"):
            df.header_comments.append(s)
            low = s.lower()
            if "filefunction" in low:
                if "nonplated" in low:
                    df.plated = False
                    df.file_function = "NonPlated"
                elif "plated" in low:
                    df.plated = True
                    df.file_function = "Plated"
            if "type=plated" in low:
                df.plated = True
            if "type=nonplated" in low or "type=npth" in low:
                df.plated = False
            m = re.search(r"(METRIC|INCH)\s*,\s*(LZ|TZ)", s, re.I)
            if m:
                # 零压缩格式只在"没有小数点"的写法里有歧义；有小数点时无害
                pass
            m = re.search(r"format\s*=\s*\{\s*(\d+)\s*:\s*(\d+)", s, re.I)
            if m:
                decimals = int(m.group(2))
            continue
        if s.upper() == "M48":
            in_header = True
            continue
        if s == "%":
            in_header = False
            continue
        m = _METRIC.match(s)
        if m:
            df.units = "mm" if m.group(1).upper() == "METRIC" else "inch"
            spec = m.group(3)
            if spec and "." in spec:
                decimals = len(spec.split(".")[1])
            continue
        if s.upper() in ("FMAT,2", "G90", "G05", "M71", "M72", "M30"):
            continue
        up = s.upper()
        if up == "M15":
            routing = True
            continue
        if up == "M16":
            routing = False
            continue
        if up in ("M97", "M98", "G05"):
            routing = False
            continue
        m = _TOOLDEF.match(s)
        if m:
            code = int(m.group(1))
            df.tools[code] = DrillTool(code, float(m.group(2)))
            continue
        m = _SEL.match(s)
        if m:
            cur_tool = int(m.group(1))
            df.tools.setdefault(cur_tool, DrillTool(cur_tool, 0.0))
            continue
        m = _ROUTE.match(s)
        if m:
            scale = 10.0 ** -decimals
            mode = m.group(1)                       # 00=快速移动, 01=铣削
            x = float(m.group(2)) if m.group(2) else last_x
            y = float(m.group(3)) if m.group(3) else last_y
            if m.group(2) and "." not in m.group(2):
                x *= scale
            if m.group(3) and "." not in m.group(3):
                y *= scale
            if mode == "1" and routing:
                tool = cur_tool or 0
                df.slots.append((last_x, last_y, x, y, tool))
                df.tools.setdefault(tool, DrillTool(tool, 0.0))
            last_x, last_y = x, y
            continue
        m = _SLOT.match(s)
        if m:
            vals = []
            for g in m.groups():
                v = float(g)
                vals.append(v if "." in g else v * (10.0 ** -decimals))
            x1, y1, x2, y2 = vals
            t = cur_tool or 0
            df.slots.append((x1, y1, x2, y2, t))
            df.tools.setdefault(t, DrillTool(t, 0.0))
            continue
        m = _XY.match(s)
        if m and (m.group(1) is not None or m.group(2) is not None):
            scale = 10.0 ** -decimals

            def _val(tok, fallback):
                if tok is None:
                    return fallback               # 模态：沿用上一个坐标
                v = float(tok)
                if "." in tok:
                    return v
                return v * scale

            x = _val(m.group(1), last_x)
            y = _val(m.group(2), last_y)
            last_x, last_y = x, y
            last_x, last_y = x, y
            t = cur_tool or 0
            df.holes.append((x, y, t))
            df.tools.setdefault(t, DrillTool(t, 0.0))
            continue
    if df.plated is None:
        name = path.lower()
        if "-npth" in name or ".npth" in name or "nonplated" in name:
            df.plated = False
        elif "-pth" in name:
            df.plated = True

    # 统计每把刀孔数（圆孔 + 槽孔都算一次下刀）
    for t in df.tools.values():
        t.hits = 0
    for (_x, _y, t) in df.holes:
        if t in df.tools:
            df.tools[t].hits += 1
    for (_x1, _y1, _x2, _y2, t) in df.slots:
        if t in df.tools:
            df.tools[t].hits += 1
    return df
