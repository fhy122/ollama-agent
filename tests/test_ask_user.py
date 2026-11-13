"""ask_user：规划阶段向人提问并等待答复。

判据（写功能的同时写「怎么才算对」）：
  1. 有交互通道时，question / options / allow_free_text 原样传给通道，
     答复原样回填给模型（不加工、不截断）；
  2. 没有交互通道时返回「[不支持]」并明确叫模型别重试 —— 不能静默编一个答案；
  3. 空 question、选项超上限、既无选项又不许自由输入 → 明确报错，不发起提问；
  4. 答复为空（超时/跳过）→ 文案里既不能说「同意」也不能说「拒绝」，
     否则模型会把沉默当否决去改方案；
  5. 本轮被打断 → 返回「[已被用户打断]」，模型据此收尾而不是继续追问；
  6. ConfirmGate：超时返回空串且 last_timeout=True；reject_all 能唤醒；
     「本回合全部允许」不能替用户回答问题（这是最容易写错的边界）；
  7. 提问的回答不会被当成「全部允许」的授权；
  8. 系统提示词只在 ask_user 真的可用时才教模型「该问就问」。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 别碰真实配置：session 会落盘，写到用户目录里就污染了
_TMP = Path(tempfile.mkdtemp(prefix="oa-ask-"))
os.environ["OLLAMA_AGENT_SESSIONS"] = str(_TMP / "sessions")
os.environ["OLLAMA_AGENT_CONFIG"] = str(_TMP / "config.json")

import tools  # noqa: E402,F401  触发工具注册
from agent import toolpolicy  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.loop import new_history  # noqa: E402
from agent.registry import ToolContext, dispatch  # noqa: E402
from tools.ask_user import MAX_OPTIONS  # noqa: E402
from webui.session import ConfirmGate  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok  {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def _ctx(answer=None, stopped=False):
    """造一个只驱动 ask_user 的上下文：通道返回 answer，可模拟打断。"""
    seen: dict = {}

    def ask_choice(question, options, free_text):
        seen.update(question=question, options=list(options), free_text=free_text)
        return answer

    ctx = ToolContext(ask_choice=ask_choice)
    if stopped:
        ctx.cancel = threading.Event()
        ctx.cancel.set()
    return ctx, seen


def run_tool():
    print("== 工具层 ==")
    ctx, seen = _ctx("A 缓存到磁盘")
    r = dispatch("ask_user", {
        "question": "缓存放哪？",
        "options": ["A 缓存到磁盘", "B 只放内存"],
        "allow_free_text": True,
    }, ctx)
    check(seen.get("question") == "缓存放哪？", "question 原样传给通道")
    check(seen.get("options") == ["A 缓存到磁盘", "B 只放内存"], "options 原样传给通道")
    check(seen.get("free_text") is True, "allow_free_text 原样传给通道")
    check("用户的答复：A 缓存到磁盘" == r.text, f"答复原样回填：{r.text!r}")

    # 去重 + 空项剔除：模型常把同一方案改几个字重复列
    ctx2, seen2 = _ctx("x")
    dispatch("ask_user", {"question": "q", "options": ["A", "A", " ", "B"]}, ctx2)
    check(seen2.get("options") == ["A", "B"], f"选项去重去空：{seen2.get('options')}")

    # 没有通道：必须明确叫模型别重试，不能编答案
    r = dispatch("ask_user", {"question": "选哪个？"}, ToolContext())
    check(r.text.startswith("[不支持]"), f"无通道明确说不支持：{r.text[:20]!r}")
    check("不要重试" in r.text, "无通道时提示不要重试")

    # 参数校验
    r = dispatch("ask_user", {"question": "   "}, ToolContext(ask_choice=lambda *a: "x"))
    check(r.text.startswith("[错误]"), "空 question 报错")
    too_many = [f"方案{i}" for i in range(MAX_OPTIONS + 1)]
    r = dispatch("ask_user", {"question": "q", "options": too_many},
                 ToolContext(ask_choice=lambda *a: "x"))
    check(r.text.startswith("[错误]") and "收敛" in r.text,
          "选项超上限报错，并提示先收敛方案")
    r = dispatch("ask_user", {"question": "q", "options": [], "allow_free_text": False},
                 ToolContext(ask_choice=lambda *a: "x"))
    check(r.text.startswith("[错误]"), "既无选项又不许自由输入 → 报错")

    # 空答复：沉默不是否决
    r = dispatch("ask_user", {"question": "q"}, _ctx("")[0])
    check(r.text.startswith("[未拿到用户答复]"), "空答复有专门文案")
    check("不等于同意也不等于否决" in r.text, "空答复文案明确不表态")

    # 打断
    r = dispatch("ask_user", {"question": "q"}, _ctx("", stopped=True)[0])
    check(r.text.startswith("[已被用户打断]"), "打断有专门文案")

    # 策略层：关掉后即使模型硬调也不执行
    cfg = AgentConfig(tool_mode="none")
    ctx3 = ToolContext(ask_choice=lambda *a: "不该被调用")
    ctx3.tools_allowed = toolpolicy.allowed_names(cfg)
    r = dispatch("ask_user", {"question": "q"}, ctx3)
    check(r.text.startswith("[已禁用]"), f"关掉的工具被拦：{r.text[:16]!r}")


def _answer_later(gate: ConfirmGate, text: str, delay: float = 0.05):
    """起一个线程，稍后替用户应答（模拟 HTTP 线程回来）。"""
    def run():
        time.sleep(delay)
        with gate._lock:
            cid = next(iter(gate._pending), "")
        if cid:
            gate.answer(cid, True, text=text)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def run_gate():
    print("== ConfirmGate ==")
    gate = ConfirmGate()
    says: list[tuple] = []
    _answer_later(gate, "B 只放内存")
    ans = gate.ask_choice("缓存放哪？", ["A", "B"], True,
                          lambda cid, q, opts, ft: says.append((q, opts, ft)))
    check(ans == "B 只放内存", f"正常回答拿到文本：{ans!r}")
    check(says and says[0] == ("缓存放哪？", ["A", "B"], True), "notify 收到完整参数")

    # 超时：返回空串，且不冒充「拒绝」
    gate = ConfirmGate()
    gate.timeout = 0.15
    t0 = time.time()
    ans = gate.ask_choice("q", ["A"], True, lambda *a: None)
    check(ans == "" and gate.last_timeout, f"超时返回空串且标记超时（{time.time()-t0:.2f}s）")

    # 「本回合全部允许」不能替用户回答
    gate = ConfirmGate()
    gate.allow_rest = True
    says.clear()
    _answer_later(gate, "A")
    ans = gate.ask_choice("q", ["A", "B"], True,
                          lambda cid, q, opts, ft: says.append(q))
    check(bool(says), "allow_rest 打开时仍然弹窗问人（不能拿授权替用户选）")
    check(ans == "A", f"allow_rest 打开时回答照常生效：{ans!r}")

    # 打断：reject_all 唤醒等待中的提问
    gate = ConfirmGate()
    gate.timeout = 5
    threading.Timer(0.05, gate.reject_all).start()
    t0 = time.time()
    ans = gate.ask_choice("q", ["A"], True, lambda *a: None)
    check(ans == "" and time.time() - t0 < 1, f"reject_all 立即唤醒（{time.time()-t0:.2f}s）")
    check(not gate.last_timeout, "打断不算超时")

    # 回答不等于「全部允许」
    gate = ConfirmGate()
    _answer_later(gate, "A")
    gate.ask_choice("q", ["A"], True, lambda *a: None)
    check(gate.allow_rest is False, "回答一次提问不会打开「全部允许」")

    # 老式布尔应答仍然可用（回归）
    gate = ConfirmGate()
    threading.Timer(0.05, lambda: gate.answer(
        (list(gate._pending) or [""])[0], True)).start()
    check(gate.ask("写文件?", lambda cid, q: None) is True, "布尔确认路径不变")


def run_prompt():
    print("== 系统提示词 ==")
    all_cfg = AgentConfig(tool_mode="all")
    p_all = new_history("", all_cfg)[0]["content"]
    check("ask_user" in p_all and "该问就问" not in p_all,
          "全部模式：提示词点了 ask_user 的名")
    check("调用 ask_user 把选项摆出来" in p_all, "全部模式：有该问就问的规则")
    p_chat = new_history("", AgentConfig(tool_mode="chat"))[0]["content"]
    check("调用 ask_user 把选项摆出来" not in p_chat,
          "只对话模式（无 ask_user）：不教它去问，免得只说一句「请问」就结束")


def run_session_wiring():
    """接线：session 把「前端应答」接到闸门。

    这是整条链路唯一没法靠单测替身绕开的缝 —— emitter.confirm 的四个参数、
    answer_confirm 的 text 透传，任何一处写错，功能就是死的。
    """
    print("== session 接线 ==")
    from webui.session import AgentSession
    from webui.store import SessionStore

    cfg = AgentConfig(use_color=False, warm=False, workspace=str(_TMP / "ws2"))
    (_TMP / "ws2").mkdir(exist_ok=True)
    s = AgentSession(cfg, store=SessionStore(_TMP / "sessions"))

    pushed: list[dict] = []

    class Em:
        def confirm(self, cid, question, options=None, free_text=None):
            pushed.append({"cid": cid, "q": question,
                           "options": options, "free_text": free_text})

        def __getattr__(self, _n):
            return lambda *a, **k: None

    box: dict = {}

    def worker():
        box["ans"] = s._ask_choice("缓存放哪？", ["A", "B"], True, Em())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    for _ in range(150):
        if pushed:
            break
        time.sleep(0.02)
    check(bool(pushed), "问题被推给前端")
    check(pushed and pushed[0]["options"] == ["A", "B"],
          f"推送里带选项：{pushed[0]['options'] if pushed else None}")
    check(pushed and pushed[0]["free_text"] is True, "推送里带 free_text")

    ok = s.answer_confirm(pushed[0]["cid"], True, text="B")
    t.join(2)
    check(ok, "answer_confirm 认这个 id")
    check(box.get("ans") == "B", f"回答经 HTTP 路径回到工具调用方：{box.get('ans')!r}")

    # 打断：cancel 置位 + 唤醒等待 → 返空串（工具据此说「被打断」而不是「超时」）
    pushed.clear()
    box.clear()
    t2 = threading.Thread(target=worker, daemon=True)
    t2.start()
    for _ in range(150):
        if pushed:
            break
        time.sleep(0.02)
    s.request_stop()
    t2.join(2)
    check(box.get("ans") == "", "打断时返空串")
    check(s.cancel_event.is_set(), "cancel 已置位（工具据此区分打断与超时）")


def main():
    run_tool()
    run_gate()
    run_prompt()
    run_session_wiring()
    if FAILS:
        print(f"\nFAIL: {len(FAILS)} 条未通过")
        for m in FAILS:
            print("  -", m)
        raise SystemExit(1)
    print("\nPASS: ask_user 工具层 / 闸门 / 提示词 全部通过")


if __name__ == "__main__":
    main()
