"""evals/penalty_probe.py —— repeat_penalty 的副作用评测。

起因：有人把 repeat_penalty 开到 1.5，回答丢了 markdown、还只出一半。
这里不猜原因，直接把「结构完整度」量化出来，让你在自己机器上复现阈值。

测三件事（都不执行工具、不写任何文件）：
  1. markdown 结构完整度：标题 / 表行 / 表分隔行 / 粗体 / 分隔线 / 字面 HTML 标签数
  2. 生成是否被截断：done_reason（stop = 自然结束，length = 撞 num_ctx 或 num_predict）
  3. 工具参数是否被污染：让模型调一个带 content 参数的 write_file，解析 JSON 后
     看 path 是否还是那段正常路径、content 里的表格是否还在

用法（仓库根目录）：
    python evals/penalty_probe.py                    # 用配置里的默认模型
    python evals/penalty_probe.py qwen35-2b-local:latest
    python evals/penalty_probe.py --runs 2           # 每档跑几次（看方差）

判读：某档开始出现字面 HTML、表分隔行归零、或 path 参数串进别的标签，
就是这档已经不安全了。参考值见 README「实测的安全区间」。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, ".")

from agent.config import DEFAULT_HOST, DEFAULT_MODEL  # noqa: E402

LEVELS = (None, 1.05, 1.1, 1.2, 1.3, 1.5)

MD_PROMPT = ("用 markdown 回答，必须包含：一个三级标题、一个三行两列的表、"
             "一段加粗结论、一条分隔线、一个三条的列表。主题：9B 与 4B 模型怎么选。")

TOOL_PROMPT = ("在工作区创建 docs/plan.md，写一段 markdown：一个二级标题、"
               "一个三行两列的表格、一句加粗结论。调用 write_file 工具完成，不要解释。")

TOOLS = [{"type": "function", "function": {
    "name": "write_file", "description": "把内容写入工作区文件",
    "parameters": {"type": "object",
                   "properties": {
                       "path": {"type": "string", "description": "相对路径"},
                       "content": {"type": "string", "description": "文件正文，markdown"}},
                   "required": ["path", "content"]}}}]


def call(host: str, model: str, messages: list, penalty, tools=None, num_predict=None):
    opt = {"temperature": 0.7}
    if penalty is not None:
        opt["repeat_penalty"] = penalty
    if num_predict:
        opt["num_predict"] = num_predict
    payload = {"model": model, "stream": False, "think": False,
               "messages": messages, "options": opt}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(host.rstrip("/") + "/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def safe_call(host: str, model: str, messages: list, penalty, tools=None):
    """不吞错误：Ollama 500 也是结论（惩罚把生成撑爆时它会直接报错）。"""
    try:
        return call(host, model, messages, penalty, tools=tools), ""
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:160]
        except Exception:  # noqa: BLE001
            pass
        return None, f"HTTP {e.code}: {body}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def md_score(t: str) -> dict:
    return {
        "chars": len(t),
        "h": len(re.findall(r"(?m)^#{1,4}\s", t)),
        "tbl": len([l for l in t.splitlines() if l.strip().startswith("|")]),
        "tbl_sep": len(re.findall(r"(?m)^\|[\s:|-]+\|$", t)),
        "bold": len(re.findall(r"\*\*[^*\n]+\*\*", t)),
        "hr": len(re.findall(r"(?m)^\s*-{3,}\s*$", t)),
        "html": len(re.findall(r"</?(?:br|b|i|u|span|div|p|a|ul|ol|li|strong)\b", t)),
    }


def judge(s: dict) -> str:
    """一句话结论，够用就行。"""
    if s["html"] >= 5:
        return "塌：改用字面 HTML"
    if s["tbl"] and not s["tbl_sep"]:
        return "表格无分隔行"
    if s["tbl"] == 0:
        return "没有表格"
    if s["h"] == 0:
        return "没有标题"
    return "完整"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    runs = 2
    for a in sys.argv[1:]:
        if a.startswith("--runs="):
            runs = max(1, int(a.split("=", 1)[1]))
    model = args[0] if args else DEFAULT_MODEL
    host = DEFAULT_HOST

    print(f"模型: {model}   档位: {['不下发' if l is None else l for l in LEVELS]}   "
          f"每档 {runs} 次\n")

    print("=== 1. markdown 结构 ===")
    hdr = ("penalty | run | 字数 | 标题 | 表行 | 表分隔 | 粗体 | 分隔线 | 字面HTML | "
           "结束原因 | 结论")
    print(hdr)
    print("-" * len(hdr))
    for p in LEVELS:
        for i in range(runs):
            d, err = safe_call(host, model, [{"role": "user", "content": MD_PROMPT}], p)
            tag = "不下发" if p is None else str(p)
            if err:
                print(f"{tag:>7} | {i + 1:3} | 请求失败 | {'':4} | {'':4} | {'':6} | "
                      f"{'':4} | {'':6} | {'':8} | {'-':>8} | {err}")
                continue
            s = md_score(d["message"]["content"])
            print(f"{tag:>7} | {i + 1:3} | {s['chars']:4} | {s['h']:4} | {s['tbl']:4} | "
                  f"{s['tbl_sep']:6} | {s['bold']:4} | {s['hr']:6} | {s['html']:8} | "
                  f"{d.get('done_reason') or '?':>8} | {judge(s)}")

    print("\n=== 2. 工具参数 ===")
    for p in LEVELS:
        d, err = safe_call(host, model, [{"role": "user", "content": TOOL_PROMPT}], p,
                           tools=TOOLS)
        tag = "不下发" if p is None else str(p)
        if err:
            print(f"  {tag:>7}: 请求失败 {err}")
            continue
        m = d["message"]
        calls = m.get("tool_calls") or []
        if not calls:
            print(f"  {tag:>7}: 没有 tool_calls（content={ (m.get('content') or '')[:50]!r}）")
            continue
        args = calls[0].get("function", {}).get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception as e:  # noqa: BLE001
                print(f"  {tag:>7}: 参数 JSON 解析失败 -> {e}; 原始前 80 字 {(args or '')[:80]!r}")
                continue
        args = args or {}
        path = str(args.get("path") or "")
        content = str(args.get("content") or "")
        s = md_score(content)
        path_ok = bool(re.fullmatch(r"[\w./\\-]+", path)) and path.endswith(".md")
        print(f"  {tag:>7}: path={'OK' if path_ok else '被污染'} {path[:60]!r} | "
              f"正文{len(content)}字 表行{s['tbl']} 表分隔{s['tbl_sep']} "
              f"字面HTML{s['html']}")
    e_ph = ("\n判读：path 被污染 / 表分隔归零 / 字面HTML 起量 = 这一档已经不安全。\n"
            "结论若与本机 README 的参考表不同，说明阈值跟模型走的，按你自己这份为准。")
    print(e_ph)
    return 0


if __name__ == "__main__":
    sys.exit(main())
