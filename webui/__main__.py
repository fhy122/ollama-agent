"""python -m webui 启动 Web UI。"""
from __future__ import annotations

import argparse
from pathlib import Path

from agent.config import DEFAULT_CTX, DEFAULT_HOST, DEFAULT_MODEL, MAX_TOOL_STEPS, \
    THINK_VALUES, AgentConfig
from agent.toolpolicy import parse_spec
from webui.server import WebUIApp


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="webui", description="Ollama Agent Web UI")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机）")
    ap.add_argument("--port", type=int, default=8888, help="端口，默认 8888")
    ap.add_argument("--ollama", default=DEFAULT_HOST, help=f"Ollama 地址，默认 {DEFAULT_HOST}")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名，默认 {DEFAULT_MODEL}")
    ap.add_argument("--think", default="off", choices=list(THINK_VALUES),
                    help="思考模式，默认 off")
    ap.add_argument("--ctx", type=int, default=DEFAULT_CTX,
                    help=f"num_ctx，默认 {DEFAULT_CTX}；0=服务端默认")
    ap.add_argument("--repeat-penalty", type=float, default=None,
                    help="options.repeat_penalty，留空=不下发（用服务端默认）")
    ap.add_argument("--repeat-last-n", type=int, default=None,
                    help="options.repeat_last_n，留空=不下发；0=禁用、-1=num_ctx")
    ap.add_argument("--workspace", "-w", default=None, help="工作空间目录，默认当前目录")
    ap.add_argument("--max-steps", type=int, default=MAX_TOOL_STEPS)
    ap.add_argument("--tools", default=None, metavar="MODE|名称,...",
                    help="工具启用策略：all（默认）/ chat（只对话）/ none / "
                         "逗号分隔的工具名。界面里也能改，这里只是启动时的初始值")
    ap.add_argument("--no-hotkey", action="store_true", help="不注册全局热键 Alt+V")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-warm", action="store_true",
                    help="启动时不预热模型（默认会先把模型载入显存/内存）")
    args = ap.parse_args(argv)
    tool_mode, tool_names = parse_spec(args.tools)

    cfg = AgentConfig(
        host=args.ollama,
        model=args.model,
        think=args.think,
        num_ctx=(args.ctx if args.ctx > 0 else None),
        repeat_penalty=args.repeat_penalty,
        repeat_last_n=args.repeat_last_n,
        max_steps=args.max_steps,
        tool_mode=tool_mode,
        tools_enabled=tool_names,
        use_color=False,
        warm=not args.no_warm,
        workspace=str(Path(args.workspace).expanduser().resolve()) if args.workspace else "",
    )
    # 用户这次启动显式指定了 --model：它高于「上次选的模型」，
    # 否则每次带参数启动都会被持久化的选择顶掉。
    cfg._model_explicit = args.model != DEFAULT_MODEL
    # 同理：显式 --tools 高于配置文件里存的那个策略
    cfg._tools_explicit = args.tools is not None
    # 思考模式也存在 ui 段：显式 --think 时别被上次的选择顶掉
    cfg._think_explicit = args.think != "off"
    # 同理：显式 --ctx 高于持久化的上一次选择（默认值不算「显式」）
    cfg._ctx_explicit = args.ctx != DEFAULT_CTX
    app = WebUIApp(cfg, host=args.host, port=args.port)
    if args.no_hotkey:
        app.start_hotkey = lambda: False  # type: ignore
    app.serve(open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
