# wxauto-linux — 微信 Linux 客户端自动化驱动 + wxauto-mcp 适配

在 Linux (KDE Plasma / Wayland) 上驱动**官方微信 Linux 客户端**,并提供与
[wxauto-mcp](https://github.com/cluic/wxauto-mcp) 完全兼容的 MCP 服务器。
无需 Windows、无需 wine、无需修改微信本体。

## 背景:为什么不能直接用 wxautox4

- **wxautox4 是闭源的**(PyPI wheel 为 Cython 编译产物,无 .py 源码),
  且 Windows-only:内部依赖 `pywinauto`/`win32gui`/`uiautomation`,
  在 Linux 上导入即失败,没有可移植路径。
- 因此本项目的做法是:**保留 wxauto-mcp 上层(MCP 协议、工具定义、wrapper),
  把底下的 wxautox4 换成自研的 `wxauto_linux` 驱动**,接口对齐 wxautox4
  的兼容子集(`WeChat` / `Chat` / `WxResponse` / `authenticate` / `check_license`)。

## 架构

```
MCP 客户端 (Claude / Cherry Studio / 任意 MCP 宿主)
   └─ stdio → bin/wxauto-mcp-linux 启动器
        └─ wxauto_mcp (vendored, 1.0.2 + mcp 1.9.1)
             └─ core/wechat_wrapper.py
                  └─ wxauto_linux.WeChat   ← 本项目核心
                       ├─ winman.py  窗口几何/激活/托盘恢复: KWin Scripting D-Bus
                       ├─ wdt.py     输入注入: wdotool (libei + xdg-desktop-portal)
                       ├─ ocr.py     读取: spectacle 截图 → 裁剪 → tesseract(chi_sim, psm3)
                       └─ wechat.py  业务: 会话列表/切换聊天/收发消息 (OCR 锚点定位)
```

读取路径全部基于 **OCR 锚点**(搜索框「搜索」、聊天标题、绿色选中行),
不依赖固定像素比例,窗口拖动/缩放/最大化后依然可用;
找不到锚点时自动重取几何+重截图(窗口正被拖动时的竞态)。

## 安装与验证

```bash
# 依赖(Manjaro/Arch): kwin spectacle tesseract tesseract-data-chi_sim wl-clipboard qdbus kscreen
#   sudo pacman -S kwin spectacle tesseract tesseract-data-chi_sim wl-clipboard kscreen

# wdotool 0.5.3 (libei 输入注入): 从 release 下载放入 wdotool-bin/
#   https://github.com/wjatgdhx/wdotool/releases  (或自编译, 见其仓库)
#   解压后路径: wdotool-bin/wdotool-x86_64-unknown-linux-gnu/wdotool
#   (可用环境变量 WXAUTO_WDTOOL 指定其它位置)

# Python 环境: venv 内需 wxauto-mcp==1.0.2 + mcp==1.9.1 + pydantic==2.11.7
#   + psutil + requests (建议 python -m venv venv 后 pip 安装)

# 首次运行 wdotool 输入时, 弹出的 xdg-desktop-portal 授权框点一次「允许」,
# 之后 token 缓存在 ~/.local/state/wdotool/portal.token

# MCP 客户端配置(stdio):
#   命令: <项目根>/bin/wxauto-mcp-linux   (脚本相对自身定位, 项目放哪都行)
```

验证(只读,不发送任何消息):

```bash
./venv/bin/python examples/mcp_smoke_test.py
# initialize → 27 tools → wechat_status / get_sessions / get_chat_info / get_messages
```

## 已验证(2026-10-31,真实微信实例)

| 能力 | 状态 |
|---|---|
| 窗口几何/激活/拖动缩放后自适应 | ✅ |
| 关闭到托盘后 D-Bus 恢复(StatusNotifierItem) | ✅ |
| GetSession 会话列表(名称/时间/未读数/免打扰) | ✅ |
| ChatInfo 当前聊天(绿色选中行匹配) | ✅ |
| GetAllMessage 读当前聊天(气泡左右侧/群成员昵称) | ✅ |
| ChatWith 搜索切换(会话列表快路径 + 搜索下拉) | ✅ 实测通过 |
| SendMsg 发送(双重安全验证) | ✅ 实测送达文件传输助手 |
| SendFiles(输入框定位+粘贴+回车) | 代码就绪,未实测 |
| GetHistoryMessage(滚轮上翻) | 代码就绪,未实测 |
| MCP stdio 握手 + 27 工具 + 只读调用 | ✅ |

### 发送安全设计(踩坑后加固)

搜索下拉里「网络搜索结果」卡片会把联系人行顶下去,而绿色搜索入口行与
联系人行**文本完全相同**,早期版本曾因此把消息误发进当前群聊。现行为:

1. `ChatWith` 优先走**会话列表快路径**(目标在列表内直接点行,绕开下拉 OCR);
2. 搜索下拉用 psm3+psm6 双通道 OCR,按信任级选行:
   分区头(「功能」)后首行 > 精确匹配 > 字符集模糊 ≥0.7 > 子串;
   **同级取更靠下的行**(搜索入口行总在联系人上方);找不到则下滚重试;
3. 点击后**必须验证**当前聊天标题/绿色选中行与目标一致(`_name_similar`
   容忍 OCR 噪声),不一致立即 Escape 并报错;
4. `SendMsg`/`SendFiles` 粘贴前**再验证一次**(`_verify_target`),不匹配拒绝发送。

## 已知限制(v1)

- 一切读取皆 OCR:名称可能黏连、免打扰铃铛偶尔漏判、选中会话行可能漏读;
  群聊发送人按昵称行启发式识别;宽气泡的左右侧判定可能反(首词块位置法);
  偶尔把时间戳行读成空消息。
- 不支持:发送链接卡片(SendUrlCard)、消息监听回调(AddListenChat,
  用 GetNextNewMessage 轮询替代)、好友列表(GetFriendDetails 返回空)。
- 每次读取 ≈3–6s(截图+OCR);高频轮询会与人工操作互相干扰。

## 踩坑记录(重要)

1. **KWin Scripting D-Bus**:`loadScript` 不传 pluginName 返回 -1;
   脚本入队后要 `Scripting.start()` 冲刷;KWin 在 `Script.run` 时才读文件,
   temp 文件不能提前删;输出先读 journalctl 再 stop(stop 丢未落盘输出)。
2. **wdotool** 窗口搜索/激活走它自己的 KWin 脚本后端(KWin 卡时一并失效),
   输入走 libei portal(token 缓存于 ~/.local/state/wdotool/)。
3. **AT-SPI 几何是陈旧的**,只能用 KWin frameGeometry;
   截图是物理像素,逻辑坐标 × scale(1.5)换算。
4. **tesseract psm6 在深色 UI 上对浅色占位字(「搜索」)极不稳定**,
   必须 psm3;TSV 模式需要 TESSDATA_PREFIX 下有 configs/
   (`ln -s /usr/share/tessdata/configs ~/.local/share/tessdata/configs`)。
5. **MCP 宿主会过滤环境变量**(只留 HOME/PATH 等):驱动内所有子进程
   统一走 `winman.session_env()`,推断补齐 XDG_RUNTIME_DIR /
   DBUS_SESSION_BUS_ADDRESS / WAYLAND_DISPLAY。
6. **/tmp 满(8G tmpfs)会让 bash 输出静默丢失**,截图临时文件留意清理;
   调试截图开关:`WXAUTO_DEBUG_SHOT=/path/prefix`(每张截图存 prefix.N.png)。

## 目录

- `src/wxauto_linux/` — Linux 原生驱动(KWin + wdotool + OCR)
- `src/wxauto_mcp/`  — vendored wxauto-mcp(已改:Linux 驱动接入、枚举序列化修复)
- `bin/wxauto-mcp-linux` — MCP stdio 启动器(位置无关)
- `examples/mcp_smoke_test.py` — MCP 握手 + 只读工具冒烟测试

## 致谢与许可

- 上层 MCP 服务器来自 [cluic/wxauto-mcp](https://github.com/cluic/wxauto-mcp)(MIT),
  本仓库在其基础上接入 Linux 原生驱动并修复若干问题。
- 输入注入基于 [wdotool](https://github.com/wjatgdhx/wdotool)(libei + xdg-desktop-portal)。
- 许可见 [LICENSE](LICENSE)(MIT)。
