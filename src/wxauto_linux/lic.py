"""许可兼容层: Linux 原生驱动无需激活, 提供与 wxautox4.utils.useful 相同的函数签名。"""


def check_license() -> bool:
    return True


def authenticate(key: str = '') -> bool:
    return True
