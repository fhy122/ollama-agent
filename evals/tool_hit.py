"""evals/tool_hit.py —— 工具命中率评测。

只问模型"这种情况你会调哪个工具"，不执行工具、不改任何文件。
用法（在仓库根目录跑）：python evals/tool_hit.py [模型名]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter

sys.path.insert(0, ".")          # 让脚本能 import 项目内的 agent / tools

import tools                     # noqa: F401  触发 15 个 @tool 注册，删了就是 0 个工具
from agent.config import DEFAULT_HOST, DEFAULT_MODEL, SYSTEM_PROMPT
from agent.registry import all_specs

# ---- 评测集：真实用户话术 → 期望工具 --------------------------------
CASES: list[tuple[str, str]] = [
    ("report.xlsx 里第三行写了什么", "read_file"),
    ("这表格做得对不对，帮我看看排版有没有错位", "render_sheet"),
    ("我刚截图了，你看一下", "clipboard_image"),
    ("assets/logo.png 这张图里是什么", "load_image"),
    ("项目里有哪些测试文件", "glob_file"),
    ("谁调用了 run_turn 这个函数", "grep_file"),
    ("src 这个目录下面有什么", "list_dir"),
    ("把 logs 目录整个删掉", "delete_file"),
    ("跑一下 tests 里那个测试", "run_command"),
    ("把 config.py 里的 8888 改成 9000", "edit_file"),
    ("新建 notes.md，写三行使用说明", "write_file"),
    ("查一下 Ollama 现在最新版本是多少", "web_search"),
    ("https://example.com/a 这篇讲什么", "browse_url"),
    ("a.docx 的正文读出来", "read_file"),
    ("把 data.csv 转成 Excel", "write_file"),
    ("现在有哪些技能可用", "skill"),
    ("这个结论我不确定，找个更强的模型复核", "web_chat"),
    ("a.pptx 第 3 页写了什么", "read_file"),
    ("看看 models 文件夹下面有哪些文件", "list_dir"),
    ("把 data 目录下所有 json 里含 timeout 的找出来", "grep_file"),
]

# web_chat 是可选私有模块（公开仓库不含），没有就从评测里剔除
import importlib.util as _il  # noqa: E402
if _il.find_spec("tools.web_chat") is None:
    CASES = [c for c in CASES if c[1] != "web_chat"]


def first_tool(model: str, text: str) -> str:
    """返回模型第一个工具调用的名字；没调工具返回 "(未调用)"。"""
    payload = {
        "model": model,
        "stream": False,                       # 评测不需要流式，省一半代码
        "options": {"temperature": 0},         # 必须 0，否则同一条用例结果会飘
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "tools": all_specs(),                  # 跟真实回合发的是同一份
    }
    req = urllib.request.Request(
        f"{DEFAULT_HOST}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    calls = (data.get("message") or {}).get("tool_calls") or []
    return calls[0]["function"]["name"] if calls else "(未调用)"


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    hit = 0
    confusion: Counter[str] = Counter()

    for text, expect in CASES:
        got = first_tool(model, text)
        ok = got == expect
        hit += ok
        if not ok:
            confusion[f"{got} → 应为 {expect}"] += 1
        print(f"{'PASS' if ok else 'FAIL'}  {got:<16} {text}", flush=True)

    total = len(CASES)
    print(f"\n命中率 {hit}/{total} = {hit / total:.0%}   模型 {model}")
    if confusion:
        print("混淆对（按出现次数排序）：")
        for pair, n in confusion.most_common():
            print(f"  {n}×  {pair}")


if __name__ == "__main__":
    main()
