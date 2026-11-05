# wxauto-linux

在 Linux (KDE Plasma / Wayland) 上驱动**官方微信 Linux 客户端**,并提供与
[wxauto-mcp](https://github.com/cluic/wxauto-mcp) 兼容的 MCP 服务器。
无需 Windows、无需 wine、无需修改微信本体。

> 背景:wxauto-mcp 依赖的 wxautox4 闭源且 Windows-only。本项目保留其上层
> (MCP 协议、工具定义、wrapper),把底层换成自研 `wxauto_linux` 驱动,
> 接口对齐 wxautox4 的兼容子集。

## 架构

```
MCP 客户端 (Claude / ZCode / 任意 MCP 宿主)
   └─ stdio → bin/wxauto-mcp-linux
        └─ wxauto_mcp (vendored)
             └─ wxauto_linux.WeChat
                  ├─ winman.py  窗口几何/激活/托盘恢复: KWin D-Bus
                  ├─ wdt.py     输入注入: wdotool (libei + portal 授权)
                  ├─ ocr.py     spectacle 截图 → PP-OCRv6 (tesseract 兜底)
                  └─ wechat.py  会话/切换/收发/联系人 (OCR 锚点 + 固定逻辑坐标)
```

可靠性要点:所有操作带 24s 截止与互斥锁,超时/失败返回明确原因而不是卡死;
界面残留状态自动恢复;切换与发送前后多重验证目标会话,不匹配即拒绝,
绝不盲发。

## 安装

```bash
# 依赖 (Manjaro/Arch)
sudo pacman -S kwin spectacle wl-clipboard kscreen tesseract tesseract-data-chi_sim

# Python 环境 (venv): wxauto-mcp==1.0.2 mcp==1.9.1 pydantic==2.11.7
#   psutil requests rapidocr pywayland
#   (rapidocr=PP-OCRv6 引擎, pywayland=文件/图片剪贴板; 模型首次运行自动下载)
python -m venv venv
venv/bin/pip install wxauto-mcp==1.0.2 "mcp==1.9.1" pydantic==2.11.7 psutil requests rapidocr pywayland

# wdotool 0.5.3: 从 https://github.com/wjatgdhx/wdotool/releases 下载,
# 解压到 wdotool-bin/wdotool-x86_64-unknown-linux-gnu/wdotool
# (或用环境变量 WXAUTO_WDTOOL 指定其它位置)
# 首次输入注入时, 在弹出的 portal 授权框点一次「允许」即可
```

MCP 客户端配置 (stdio):命令填 `<项目根>/bin/wxauto-mcp-linux`
(脚本相对自身定位,项目放哪都行)。

只读冒烟测试 (不发送任何消息):

```bash
./venv/bin/python examples/mcp_smoke_test.py
```

## 已验证

2026-11-04,真实微信实例,MCP 全链路;窗口 841×545 / 最大化 1707×1023、
DPI scale 1.0 / 1.5 各跑一轮。

| 工具 | 实测 |
|---|---|
| wechat_status / get_chat_info | 0.1-2s |
| get_sessions | 4-6s |
| switch_chat(已在目标 / 会话列表 / 搜索) | 2-25s |
| send_message | 6-19s |
| get_messages / get_history | 5-20s |
| get_friends | 7-9s |
| 不存在的联系人 | ~17s 安全失败并自恢复 |

## 已知限制

- 一切读取皆 OCR:名称可能黏连或丢字(表情/生僻字名尤甚),匹配用字符集
  模糊兜底;消息读取只覆盖当前可见区域。
- 发送图片走剪贴板粘贴;发送普通文件走「发送文件」对话框(~20s)——
  微信 Linux 输入框的粘贴只认文本和图片,不支持文件。
- 文件/图片气泡读取时 attr 可能误判为 friend(卡片非绿底,侧别启发式失效)。
- 不支持:发送链接卡片、消息监听回调(用 GetNextNewMessage 轮询替代)、
  好友详细信息(仅昵称列表)。
- 切换不在可视会话列表中的联系人走搜索路径,耗时 ~15-25s,保证安全返回。

## 致谢与许可

- 上层 MCP 服务器来自 [cluic/wxauto-mcp](https://github.com/cluic/wxauto-mcp)(MIT),
  本仓库在其基础上接入 Linux 原生驱动并修复若干问题。
- 输入注入基于 [wdotool](https://github.com/wjatgdhx/wdotool)。
- 许可见 [LICENSE](LICENSE)(MIT)。
