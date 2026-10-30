"""MCP stdio 冒烟测试: initialize → tools/list → 只读工具调用。

不发送任何消息、不点击任何界面 (仅读取会话/消息时窗口会被激活置前)。
用法: <项目根>/venv/bin/python examples/mcp_smoke_test.py
"""
import asyncio
from pathlib import Path

LAUNCHER = str(Path(__file__).resolve().parent.parent / 'bin' / 'wxauto-mcp-linux')


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=LAUNCHER)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await asyncio.wait_for(s.initialize(), timeout=60)
            print('== initialize ==')
            print('server:', init.serverInfo.name, init.serverInfo.version)
            print('protocol:', init.protocolVersion)

            tools = await asyncio.wait_for(s.list_tools(), timeout=15)
            print('== tools/list ==', len(tools.tools), 'tools')

            for name in ('wechat_status', 'get_sessions',
                         'get_chat_info', 'get_messages'):
                print(f'== tools/call {name} ==')
                try:
                    res = await asyncio.wait_for(s.call_tool(name, {}), timeout=120)
                except Exception as e:
                    print('CALL ERROR:', type(e).__name__, str(e)[:300])
                    continue
                for c in res.content:
                    txt = getattr(c, 'text', None)
                    if txt:
                        print(txt[:1500])


asyncio.run(main())
