"""嘉立创 Gerber/Excellon 层命名与文件功能映射表。

设计要点：
  * 输出文件名一律用"板名 + 嘉立创习惯后缀"，因为嘉立创下单系统主要靠
    后缀 + 文件内容识别层号；EasyEDA 导出的包就是这么命名的（见样例）。
  * KiCad 自身的 Protel 扩展名（`--no-protel-extension` 取反）在轮廓层与
    内层命名上与嘉立创习惯不完全一致，因此插件不依赖 KiCad 的命名，而是
    在打包阶段统一改名（见 package.py）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LayerInfo:
    kicad: str          # KiCad 规范层名
    suffix: str         # 嘉立创习惯后缀（大写，含点）
    function: str       # Gerber X2 FileFunction（KiCad 侧写入，供参考）
    required: bool      # 是否必需
    side: str = ""      # T / B / ""
    kind: str = "copper"  # copper / silk / mask / paste / outline / other

    @property
    def jlc_name(self) -> str:
        return self.suffix.lstrip(".")


# 顺序即打包顺序；内层按需展开（见 inner_layer_entries）。
LAYERS: list[LayerInfo] = [
    LayerInfo("F.Cu",      ".GTL", "Copper,L1,Top",           True,  "T", "copper"),
    LayerInfo("In1.Cu",    ".G1",  "Copper,L2,Inr",           False, "",  "copper"),
    LayerInfo("In2.Cu",    ".G2",  "Copper,L3,Inr",           False, "",  "copper"),
    LayerInfo("In3.Cu",    ".G3",  "Copper,L4,Inr",           False, "",  "copper"),
    LayerInfo("In4.Cu",    ".G4",  "Copper,L5,Inr",           False, "",  "copper"),
    LayerInfo("In5.Cu",    ".G5",  "Copper,L6,Inr",           False, "",  "copper"),
    LayerInfo("In6.Cu",    ".G6",  "Copper,L7,Inr",           False, "",  "copper"),
    LayerInfo("B.Cu",      ".GBL", "Copper,L2,Bot",           True,  "B", "copper"),
    LayerInfo("F.SilkS",   ".GTO", "Legend,Top",              True,  "T", "silk"),
    LayerInfo("B.SilkS",   ".GBO", "Legend,Bot",              False, "B", "silk"),
    LayerInfo("F.Mask",    ".GTS", "Soldermask,Top",          True,  "T", "mask"),
    LayerInfo("B.Mask",    ".GBS", "Soldermask,Bot",          False, "B", "mask"),
    LayerInfo("F.Paste",   ".GTP", "Paste,Top",               False, "T", "paste"),
    LayerInfo("B.Paste",   ".GBP", "Paste,Bot",               False, "B", "paste"),
    LayerInfo("Edge.Cuts", ".GKO", "Profile,NP",              True,  "",  "outline"),
    LayerInfo("F.Fab",     ".GDL", "Other,Drawing",           False, "T", "other"),
    LayerInfo("B.Fab",     ".GDB", "Other,Drawing",           False, "B", "other"),
]

# 钻孔文件：嘉立创支持 .DRL / .TXT，EasyEDA 用 .DRL
DRILL_SUFFIX_PTH = "-PTH.DRL"
DRILL_SUFFIX_NPTH = "-NPTH.DRL"
DRILL_SUFFIX_MERGED = ".DRL"

# 允许但不必需的附加文件后缀（嘉立创会忽略，保留可方便人工核对）
OPTIONAL_SUFFIXES = {".GDD", ".GDL", ".GDB", ".GBR", ".GBRJOB", ".TXT.gbr"}

# 后缀 -> LayerInfo 的反查表（含 EasyEDA 与 KiCad 两种写法）
_SUFFIX_TO_LAYER: dict[str, LayerInfo] = {}
for _l in LAYERS:
    _SUFFIX_TO_LAYER[_l.suffix.upper()] = _l

# KiCad 默认（非 Protel）命名: board-F_Cu.gbr 之类
_KICAD_NAME_TO_LAYER = {l.kicad: l for l in LAYERS}
# EasyEDA 命名: Gerber_TopLayer.GTL 之类
_EASYEDA_PREFIX_TO_LAYER = {
    "TopLayer": "F.Cu",
    "BottomLayer": "B.Cu",
    "TopSilkscreenLayer": "F.SilkS",
    "BottomSilkscreenLayer": "B.SilkS",
    "TopSolderMaskLayer": "F.Mask",
    "BottomSolderMaskLayer": "B.Mask",
    "TopPasteMaskLayer": "F.Paste",
    "BottomPasteMaskLayer": "B.Paste",
    "BoardOutlineLayer": "Edge.Cuts",
    "DocumentLayer": "F.Fab",
    "DrillDrawingLayer": None,   # 钻孔图，非实体层
}


def layer_by_suffix(suffix: str) -> LayerInfo | None:
    return _SUFFIX_TO_LAYER.get(suffix.upper())


def layer_by_name(name: str) -> LayerInfo | None:
    return _KICAD_NAME_TO_LAYER.get(name)


def classify(filename: str) -> tuple[LayerInfo | None, str]:
    """从文件名猜测层。

    返回 (LayerInfo 或 None, 说明)。支撑三种命名来源：
      KiCad 规范名（F_Cu.gbr）、KiCad Protel 扩展名（.GTL）、EasyEDA 名
      （Gerber_TopLayer.GTL）。
    """
    import os
    import re

    base = os.path.basename(filename)
    stem, ext = os.path.splitext(base)
    ext_u = ext.upper()

    # 1) 扩展名直接命中
    lay = layer_by_suffix(ext_u)
    if lay is not None:
        return lay, f"按扩展名 {ext_u} 识别"
    if ext_u in (".DRL", ".TXT", ".NCD"):
        return None, "钻孔文件（需按内容/文件名判断 PTH/NPTH）"

    # 2) KiCad 规范名：xxx-F_Cu.gbr / F_Cu.gbr
    m = re.search(r"(?:^|[-_.])(F_Cu|B_Cu|In\d+_Cu|F_SilkS|B_SilkS|F_Mask|B_Mask|"
                  r"F_Paste|B_Paste|Edge_Cuts|F_Fab|B_Fab)(?:[-_.].*)?$", stem)
    if m:
        # F_Cu -> F.Cu  /  In1_Cu -> In1.Cu  /  Edge_Cuts -> Edge.Cuts
        kicad = m.group(1).replace("_", ".", 1)
        lay = layer_by_name(kicad)
        if lay is not None:
            return lay, f"按 KiCad 层名 {kicad} 识别"

    # 3) EasyEDA 命名
    for prefix, kicad in _EASYEDA_PREFIX_TO_LAYER.items():
        if prefix.lower() in stem.lower():
            if kicad is None:
                return None, "EasyEDA 钻孔图（非制造层）"
            return layer_by_name(kicad), f"按 EasyEDA 名 {prefix} 识别"

    return None, "无法识别"


def inner_layer_entries(layer_count: int) -> list[LayerInfo]:
    """按板层数返回需要的层表（含内层）。layer_count=2 时只保留 F/B 铜。"""
    out = []
    for l in LAYERS:
        if l.kicad.startswith("In"):
            n = int(l.kicad[2:-3])
            if n + 2 > layer_count:      # In1 是第 2 层内层（4 层板）
                continue
        out.append(l)
    return out
