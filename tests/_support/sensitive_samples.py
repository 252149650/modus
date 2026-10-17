"""只在测试中构造凭据形状，避免把样本误读为可用的认证信息。"""

from string import ascii_lowercase

SYNTHETIC_TOKEN = ascii_lowercase[:16]
SYNTHETIC_JWT = ".".join(
    ("eyJ" + ascii_lowercase[:8], ascii_lowercase[8:16], ascii_lowercase[16:24])
)


def synthetic_source_assignment(name: str = "token") -> str:
    """构造测试源码赋值；值始终是字母序列，不接收真实凭据。"""
    return " = ".join((name, '"' + SYNTHETIC_TOKEN + '"'))
