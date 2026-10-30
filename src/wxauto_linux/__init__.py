"""wxauto_linux — 微信 Linux 客户端自动化驱动 (wxautox4 接口兼容子集)。"""
from .wechat import WeChat, Chat, Message, WxResponse, WeChatError
from .lic import authenticate, check_license

__all__ = ['WeChat', 'Chat', 'Message', 'WxResponse', 'WeChatError',
           'authenticate', 'check_license']
