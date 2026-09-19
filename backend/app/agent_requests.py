"""Separate execution commands from new requirement facts before calling the model."""
import re
from typing import Literal

RESUME_COMMANDS = {
    "继续", "请继续", "继续报价", "请继续报价", "继续查价", "请继续查价",
    "继续生成报价", "重试", "请重试", "重试报价", "再试一次", "重新尝试",
    "恢复报价", "接着报价", "重新报价", "重新计价", "重新计算报价",
    "continue", "resume", "retry",
}


def is_resume_command(text: str) -> bool:
    normalized = re.sub(r"[\s。.!！?？,，;；]+", "", text).casefold()
    return normalized in RESUME_COMMANDS


def resolve_agent_intent(has_requirements: bool, text: str, replace: bool,
                         intent: Literal["auto", "resume", "extract"] = "auto") -> str:
    if intent == "resume":
        if text.strip() and not is_resume_command(text):
            raise ValueError("继续执行包含新的需求信息，请使用重新识别以免忽略修改")
        if replace:
            raise ValueError("继续执行不能同时要求替换需求")
        # Appending a file clears requirements; never skip the newly uploaded source.
        return "resume" if has_requirements else "extract"
    if intent == "extract" or not has_requirements:
        return "extract"
    # Older clients set replace_existing for every nonempty message, including 'continue'.
    if is_resume_command(text):
        return "resume"
    return "extract" if text.strip() or replace else "resume"
