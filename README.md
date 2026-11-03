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
MCP 客户端 (Claude / ZCode / 任意 MCP 宿主)
   └─ stdio → bin/wxauto-mcp-linux 启动器
        └─ wxauto_mcp (vendored, 1.0.2 + mcp 1.9.1)
             └─ core/wechat_wrapper.py
                  └─ wxauto_linux.WeChat   ← 本项目核心
                       ├─ winman.py  窗口几何/激活/托盘恢复: KWin Scripting D-Bus
                       ├─ wdt.py     输入注入: wdotool (libei + xdg-desktop-portal)
                       ├─ ocr.py     读取: spectacle 截图 → 裁剪 → PP-OCRv6 (rapidocr/onnxruntime, tesseract 兜底)
                       └─ wechat.py  业务: 会话列表/切换聊天/收发消息/联系人 (OCR 锚点)
```

可靠性设计(长期真实使用踩坑后固化):

- **固定逻辑坐标骨架**:实测证明微信 Linux 的左栏/面板/输入区是
  **固定逻辑尺寸**(导航图标 (30,115)/(30,163)、会话面板宽 300、
  输入区高 140、搜索下拉 358×470),不随窗口大小与 DPI 缩放变化 ——
  所有点击/区域判据以它为准 (窗口比例坐标在最大化窗口下会点错图标、
  裁错区域,是 v2 前最大的稳定性杀手)。
- **单操作截止时间(24s)+ 操作互斥锁**:所有公开操作保证在 MCP 客户端
  30s 超时前返回明确错误;客户端超时后残留的操作不会卡死后续调用
  (新操作最多等 6s 即报「忙」);工具函数在工作线程执行,事件循环不再被阻塞。
- **截图/OCR/几何多级缓存 + OCR 框去重**:一次操作内复用同一张截图,
  读操作 ~2-6s;PP-OCRv6 偶发的同文本偏移双框统一去重 (名称黏连/
  时间双读的根源)。
- **快慢两级读取 + 新帧验证**:标题条带 (~1.2s) 先行,匹配即采信;
  不匹配才全窗权威复核。切换点击后的验证强制等新帧 (≥1.1s + 刷新截图),
  复用切换前旧快照会误判 —— 误判方向只会「拒真」,不会「纳假」。
- **状态自恢复**:搜索下拉残留 / 全窗网络结果页 / 联系人页,统一
  「聚焦搜索框→清空关键词→点聊天图标」拉回主界面(全程不按 Escape,见踩坑 7)。
- **发送双保险**:切换聊天后验证当前会话名(模糊匹配容忍 OCR 噪声),
  粘贴前再次校验;窗口消失时拒绝发送(不会盲发到别的应用)。

## 安装与验证

```bash
# 依赖(Manjaro/Arch): kwin spectacle wl-clipboard qdbus kscreen
#   sudo pacman -S kwin spectacle wl-clipboard kscreen
#   (tesseract 仅作兜底, 建议也装: tesseract tesseract-data-chi_sim)

# OCR 引擎: PP-OCRv6 (rapidocr>=3.10, onnxruntime CPU, 首次运行自动下载模型)
#   venv/bin/pip install rapidocr

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

## 已验证(2026-11-04,真实微信实例,MCP 全链路,PP-OCRv6 引擎,
## 最大化 1707×1023 与 scale 1.0/1.5 双 DPI、双窗口尺寸下各跑一轮)

| 能力 | 实测 |
|---|---|
| wechat_status | 0.1s |
| get_sessions(含选中行,绿色行条带 OCR 补齐) | 4-6s |
| get_chat_info | 0.2s(缓存命中) |
| switch_chat(已在目标 → 免点击直接返回) | 2-5s |
| switch_chat(会话列表快路径 / 搜索下拉) | 7-25s |
| send_message(带 who,切换+双验证+发送) | 6-19s |
| get_messages(发送后读回,含自己发的绿气泡) | 5-20s |
| get_history(向上翻页读取旧消息) | ~8s |
| get_friends(联系人页 OCR,两轮结果完全一致) | 7-9s |
| 错误路径(不存在的联系人) | ~17s 安全失败+自恢复 |
| 窗口被关进托盘 | 下次操作自动恢复 |

### 发送安全设计(踩坑后加固)

搜索下拉里「网络搜索结果」分区/卡片会把联系人行顶下去,而网络结果子行
**文本与联系人完全相同**,早期版本曾因此把消息误发进当前群聊。现行为:

1. `ChatWith` 先查**当前会话是否已是目标**(注意:绝不能点已选中的会话行,
   微信 Linux 会把聊天收起);再走**会话列表快路径**;
2. 搜索下拉用裁剪区域 OCR(psm3+psm6),按信任级选行:
   分区头后首行 > 精确 > 模糊 > 子串;同级**取更靠上的行**(网络结果总在底部);
   「搜索网络结果」分区的子行整段排除;找不到则下滚重试;
3. 点击后**必须验证**当前聊天与目标一致(字符集模糊 ≥0.7 / 长名放宽),
   不一致立即恢复主界面并报错;
4. `SendMsg`/`SendFiles` 粘贴前**再验证一次**(窗口必须在主界面),不匹配拒绝发送。

## 已知限制(v2)

- 一切读取皆 OCR:名称可能黏连或丢字(表情/生僻字名尤甚),
  匹配用字符集模糊(≥0.7,长名放宽)兜底;消息读取只覆盖**可见区域**;
  超短自己消息(≤3字)在个别帧可能漏读;群聊发送人按昵称行启发式识别。
- KDE 通知 toast 悬浮在窗口上方时(输入注入后几秒内),聊天区 OCR
  可能混入通知文字、新气泡可能被遮挡读残 —— 瞬态现象,稍后重读即正常。
- 不支持:发送链接卡片(SendUrlCard)、消息监听回调(AddListenChat,
  用 GetNextNewMessage 轮询替代)、好友详细信息(仅昵称列表)。
- 切换不在可视会话列表中的联系人走搜索路径,耗时 ~15-25s,保证安全返回。

## 踩坑记录(重要)

1. **KWin Scripting D-Bus**:`loadScript` 不传 pluginName 返回 -1;
   脚本入队后要 `Scripting.start()` 冲刷;KWin 在 `Script.run` 时才读文件,
   temp 文件不能提前删;输出先读 journalctl 再 stop(stop 丢未落盘输出)。
   激活+取几何合并成一次 D-Bus 往返(~0.7s,分开两次 ~1.8s)。
2. **wdotool** 窗口搜索/激活走它自己的 KWin 脚本后端(KWin 卡时一并失效),
   输入走 libei portal(token 缓存于 ~/.local/state/wdotool/)。
3. **AT-SPI 几何是陈旧的**,只能用 KWin frameGeometry;
   截图是物理像素,逻辑坐标 × scale(1.5)换算。
4. **OCR 引擎已从 tesseract 换成 PP-OCRv6**(rapidocr 3.x, onnxruntime CPU):
   质量碾压级提升(此前会话名/标题经常读错),CPU 单帧 ~1.6s。
   三个关键坑:① PaddleOCR 系按 OpenCV 惯例**吃 BGR**,喂 RGB 时深色 UI
   检测会整块崩掉;② **二值化反而有害**——v6 是自然图像模型,原样裁剪最准,
   只有极低对比的灰色占位字(「搜索」)需要灰度拉伸;③ 自己发的消息是
   「深色字+绿底」,检测模型漏检 → 绿掩码找框+深色像素定文字 bbox+原样紧裁
   rec-only(use_det=False)。
5. **MCP 宿主会过滤环境变量**(只留 HOME/PATH 等):驱动内所有子进程
   统一走 `winman.session_env()`,推断补齐 XDG_RUNTIME_DIR /
   DBUS_SESSION_BUS_ADDRESS / WAYLAND_DISPLAY。
6. **/tmp 满(8G tmpfs)会让 bash 输出静默丢失**,截图临时文件留意清理;
   调试截图开关:`WXAUTO_DEBUG_SHOT=/path/prefix`(每张截图存 prefix.N.png)。
7. **Escape 会把微信 Linux 主窗口整个藏进托盘** —— 失败路径里按 Escape,
   之后所有读取全部变空(本系列 bug 的最大元凶)。任何取消/恢复动作
   都改用「点输入框区域 + 清空搜索框 + 点聊天图标」。
8. **再点一次已选中的会话行会把聊天收起**(toggle):切换前先查当前会话,
   已在目标直接返回,绝不能点选中行。
9. **psm3 常把标题拆成单字 token**(微/信/团/队):标题锚点必须按词块聚合,
   单 token len>=2 的老写法在窗口尺寸/标题变化时随机全挂。
10. **UI 骨架是固定逻辑坐标,别用窗口比例**:导航图标/面板宽/输入区高
    在 841×545 与 1707×1023 两种窗口下完全相同;按 0.21H/0.30H/0.42W
    定位在最大化窗口全部漂移 (0.21×1023=215 会点到收藏图标)。
    同理,绿色选中行掩码必须裁到面板固定宽 (~315 逻辑px),聊天区
    绿气泡在宽窗口会越界污染;导航图标与第一条会话行 y 重叠 (图标
    选中时也是绿色),掩码须再裁掉左侧 52 逻辑px 的导航栏。
11. **PP-OCRv6 检测器对贴裁剪边的文本整块漏检**:标题距面板右缘只有
    ~13px,条带从 300 起裁标题永远读不到 (曾让切换验证全部误拒);
    裁剪 OCR 的目标文本两侧要留 ≥100px 边距。
12. **切换点击后的验证必须拿新帧**:点击后 0.8s 截图可能还是切换前的
    旧渲染,且全读兜底若复用同一条 TTL 缓存会把旧帧再读一遍 → 误拒
    + recover 把状态搅乱。等 ≥1.1s + 强制刷新再读。
13. **`_shot` 只存图不存行**:4s TTL 内任何全窗 OCR 必须在缓存路径里
    补做「逻辑坐标转换 + 去重」,旧版漏转换,原始图像坐标流进锚点
    解析曾被裸 except 静默吞掉 (验证悄悄退化)。
14. **联系人页分区头自带小图标**,「头像列像素判据」跨主题/跨窗口
    全崩 (深色头像好友与图标头几乎不可分) → 只用文字判据:箭头
    token / 分区头词 / 单字母索引 / 纯数字人数。
15. **窗口在后台时,libei 点击会落到前台别的应用上** (实测点进了
    其他软件):所有 SwitchTo*/恢复动作先 `activate_wechat()` 再点击。

## 目录

- `src/wxauto_linux/` — Linux 原生驱动(KWin + wdotool + OCR)
- `src/wxauto_mcp/`  — vendored wxauto-mcp(已改:Linux 驱动接入、枚举序列化、
  错误透传、工具线程执行)
- `bin/wxauto-mcp-linux` — MCP stdio 启动器(位置无关)
- `examples/mcp_smoke_test.py` — MCP 握手 + 只读工具冒烟测试

## 致谢与许可

- 上层 MCP 服务器来自 [cluic/wxauto-mcp](https://github.com/cluic/wxauto-mcp)(MIT),
  本仓库在其基础上接入 Linux 原生驱动并修复若干问题。
- 输入注入基于 [wdotool](https://github.com/wjatgdhx/wdotool)(libei + xdg-desktop-portal)。
- 许可见 [LICENSE](LICENSE)(MIT)。
