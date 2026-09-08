from dataclasses import dataclass


@dataclass
class Evidence:
    phase: str = "RUNNING"
    authenticated: bool = False
    block_reason: str = ""


def classify(text: str) -> Evidence:
    """Conservative hints from reviewed upstream logs; not proof of task success."""
    login = max(text.rfind("请使用手机米游社 APP 扫描二维码登录"), text.rfind("等待扫码"))
    entered = text.rfind("进入云游戏成功")
    result = Evidence(authenticated=entered >= 0 and entered > login)
    if login >= 0 and login > entered:
        result.phase = "WAITING_LOGIN"
    if "等待云游戏登录超时" in text:
        result.block_reason = "需要重新扫码登录"
    if "云游戏剩余时长为 0" in text or "云游戏付费时长已耗尽" in text:
        result.block_reason = "云游戏可用时长不足"
    return result
