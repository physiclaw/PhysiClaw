"""Local keyword veto for observed targets, independent of model answers.

This is a backstop, not a safety classifier: OCR may miss a label, and an
unlabelled icon or an innocent label can still have consequential effects.
No match is never permission to execute.
"""

import re
import unicodedata

from physiclaw.contract.decision import DecisionContext, DecisionElement

# Simplified Chinese only. Include generic confirmations whose effects
# cannot be established from their labels alone.
_CHINESE_CONTROLS = frozenset(
    "发送 传送 转发 支付 付款 转账 转帐 汇款 购买 下单 订单 结算 订阅 续费 收货 "
    "删除 清空 清除 重置 恢复出厂 确认 确定 提交 同意 取消 授权 允许 登录 登入 登出 "
    "发布 发表 分享 上传 拨打 呼叫 按住说话 录音 录制 加入购物车 加购 "
    "知道了 明白了 好的 开通".split()
)
_ENGLISH_CONTROLS = frozenset(
    "send resend sending pay payment purchase buy checkout subscribe unsubscribe "
    "renew delete remove erase clear reset discard confirm submit agree cancel "
    "authorize allow grant login logout publish post share upload call dial "
    "record recording transfer donate ok okay".split()
)
_ENGLISH_PHRASES = (
    "sign in",
    "sign out",
    "log in",
    "log out",
    "place order",
    "place an order",
    "add to cart",
    "add to bag",
    "got it",
    "hold to talk",
    "hold to record",
)
_COMPACT_PHRASES = frozenset(phrase.replace(" ", "") for phrase in _ENGLISH_PHRASES)
# Label association is intentionally conservative: intersect the target or
# sit within 2% of screen width/height. It is not a proven widget boundary.
_LABEL_MARGIN = 0.02


def _consequential_label(text: str) -> bool:
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKC", text).casefold()
        if unicodedata.category(char) != "Cf"
    )
    compact = "".join(normalized.split())
    words = re.findall(r"[a-z]+", normalized)
    phrases = " " + " ".join(words) + " "
    return (
        any(word in compact for word in _CHINESE_CONTROLS)
        or bool(_ENGLISH_CONTROLS.intersection(words))
        or any(f" {phrase} " in phrases for phrase in _ENGLISH_PHRASES)
        or compact in _ENGLISH_CONTROLS
        or compact in _COMPACT_PHRASES
    )


def blocked_controls(context: DecisionContext) -> dict[str, DecisionElement]:
    """Map each vetoed target to its first nearby consequential label."""
    elements = (*context.ocr, *context.icons)
    labels = tuple(e for e in elements if e.text and _consequential_label(e.text))
    blocked = {}
    for target in elements:
        left, top, right, bottom = target.bbox
        for label in labels:
            label_left, label_top, label_right, label_bottom = label.bbox
            if (
                left <= label_right + _LABEL_MARGIN
                and right >= label_left - _LABEL_MARGIN
                and top <= label_bottom + _LABEL_MARGIN
                and bottom >= label_top - _LABEL_MARGIN
            ):
                blocked[target.ref] = label
                break
    return blocked
