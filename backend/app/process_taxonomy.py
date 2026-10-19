"""Shared process identities; synonyms never make different production methods equivalent."""
import re
import unicodedata
from typing import Literal

ProcessCode = Literal[
    "cutting", "welding", "inlay", "baked_paint", "spray_paint", "screen_print",
    "uv_print", "etch_fill", "etching", "engraving", "anodizing", "plating",
    "anti_oxidation", "film", "braille", "magnet", "led", "wiring",
]
PROCESS_ALIASES = {
    "cutting": ("激光切割", "切割", "镂空"),
    "welding": ("焊接",),
    "inlay": ("镶嵌",),
    "baked_paint": ("烤漆", "烤金属漆", "烤半哑光漆", "烤半哑光金属漆"),
    "spray_paint": ("喷漆", "喷涂油漆"),
    "screen_print": ("丝印", "丝网印刷"),
    "uv_print": ("UV打印", "UV印刷", "UV喷印"),
    "etch_fill": ("蚀刻填漆", "蚀刻填色"),
    "etching": ("蚀刻",),
    "engraving": ("雕刻", "凹刻"),
    "anodizing": ("阳极氧化",),
    "plating": ("电镀",),
    "anti_oxidation": ("防氧化",),
    "film": ("贴膜", "贴胶膜"),
    "braille": ("盲文",),
    "magnet": ("磁铁",),
    "led": ("LED模组", "LED灯组", "LED灯带", "LED光源", "内置LED", "内藏LED", "防水LED"),
    "wiring": ("内部布线", "内接线", "内部接线"),
}
PROCESS_LABELS = {key: aliases[0] for key, aliases in PROCESS_ALIASES.items()}
NEGATIVE = r"不能|不得|并非|不是|不包含|不含|不适用|不代表|无需|不需要|未要求"


def positive_clauses(text):
    text = unicodedata.normalize("NFKC", text or "")
    return [clause for clause in re.split(r"[。；;，,\n]", text)
            if clause.strip() and not re.search(NEGATIVE, clause)]


def process_codes(text):
    clauses = [re.sub(r"\s+", "", clause).casefold() for clause in positive_clauses(text)]
    found = {key for key, aliases in PROCESS_ALIASES.items()
             if any(alias.casefold() in clause for alias in aliases for clause in clauses)}
    if "etch_fill" in found:
        found.discard("etching")
    return sorted(found)


def process_matches(required, offered):
    """An etch-and-fill operation includes etching, but not the reverse."""
    return required == offered or required == "etching" and offered == "etch_fill"
