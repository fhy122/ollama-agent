"""Agent 主循环：消息历史、工具派发、多步执行。

输出走 emitter（见 agent/events.py），控制台/HTTP 流共用本循环。
"""
from __future__ import annotations

import json
import re
import time

from agent.backends import ChatClient
from agent import longdoc
from agent.config import (SYSTEM_PROMPT_ASK_RULE, SYSTEM_PROMPT_CORE,
                         SYSTEM_PROMPT_FILE_RULE, AgentConfig)
from agent import context_view
from agent import compaction
from agent.events import ConsoleEmitter
from agent.registry import ToolContext, ToolResult
from agent.repeat_guard import RepeatGuard
from agent.retry import (LIMITED, RATE_LIMIT, RETRYABLE, backoff_delay,
                         classify_error, interruptible_sleep)
from agent import timectx, usage, toolpolicy

import tools


# ---------- 外脑委派：用户点名时补一条硬指令 ----------

_EXPERT_ASK = re.compile(r"(外脑|豆包|文心|问问\s*(网页|外|别的|其它|其他)?\s*(AI|ai))")
_EXPERT_DENY = re.compile(r"(不要|别|禁止|无需|不需要|不用)\s*(用|让|找)?\s*(外脑|豆包|文心)")

_EXPERT_HINT = (
    "\n\n[系统指令] 用户点名要用外脑（web_chat）。下一步必须调用 web_chat："
    "把项目背景、你已经读到的结构或关键代码片段、具体问题、期望的输出格式，"
    "合并成一个自包含的 question 发过去（代码片段直接粘，这是预期用法）。"
    "已经拿到的信息直接复用，不要再用 list_dir/read_file 反复采集。"
    "拿到外脑的回答后，直接按它的建议继续本地工作，不要停下来问「要不要继续」。"
)


def _expert_available() -> bool:
    """web_chat 是可选私有模块（公开仓库不含），没注册时外脑委派整体关闭。"""
    return tools.get("web_chat") is not None


def _stats_with_step(emitter):
    """返回一个可以安全调用的 stats(st, step=...) 函数。

    stats 的 step 参数是后加的。教程/用户自建的前端可能还是 stats(st) 的老签名，
    直接传 step 会 TypeError —— 而这种错误发生在**回合中间**，用户看到的是「回答
    跑到一半炸了」，比少标一个步号严重得多。所以这里先看签名，不支持就退化成
    不带 step 调用。
    """
    import inspect

    try:
        params = inspect.signature(emitter.stats).parameters
    except (TypeError, ValueError):
        return lambda st, step=0: emitter.stats(st)
    if "step" in params:
        return lambda st, step=0: emitter.stats(st, step=step)
    return lambda st, step=0: emitter.stats(st)


def expert_requested(user_text: str) -> bool:
    """用户是否点名要用外脑（"不要用外脑"这类否定说法不算）。"""
    if not user_text or _EXPERT_DENY.search(user_text):
        return False
    return bool(_EXPERT_ASK.search(user_text))


def expert_hint(user_text: str) -> str:
    """点名用外脑时，把下一步动作写死补在消息末尾。

    为什么要这么硬：9B 这类小模型面对「用外脑分析项目」会一直 list_dir/read_file
    采集（实测连续三次 list_dir 也不肯委派），光靠系统提示里的抽象规则不管用。
    """
    if not _expert_available() or not expert_requested(user_text):
        return user_text
    return user_text + _EXPERT_HINT


_ASK_QUESTION = (
    "[系统指令] 现在只做一件事：写出要发给外脑的问题正文。"
    "不要调用任何工具，不要解释你在做什么，不要复述上下文，直接输出问题本身。"
    "问题里必须说清：①这个项目/文件是什么、用什么技术栈；"
    "②要它帮你分析什么问题；③希望它按什么结构回答。"
)


def _delegate_to_expert(client, cfg, ctx, history, emitter, user_text) -> None:
    """把当前任务交给外脑：先让模型写问题，再代它调 web_chat，把回答注入历史。

    为什么由代码代劳而不是等模型自己调工具：实测 9B 连续三轮 list_dir 也不肯委派；
    把工具列表裁到只剩 web_chat，它反而幻觉出被裁掉的工具名（调一个不存在的 list_dir）。
    小模型擅长的是「写字」而不是「选工具」，所以让它只负责写问题，调用由这里完成。
    """
    if not _expert_available():
        return
    emitter.notice("[按你的要求，把任务交给外脑；拿到回答后继续本地工作]")
    question = ""
    try:
        # 走同一份请求视图：否则长会话压缩/技能去重省下的 token 会在
        # 这个「旁路子请求」里被全量重发回去。
        probe = context_view.project_messages(history, cfg=cfg) + [
            {"role": "user", "content": _ASK_QUESTION}]
        with usage.scope(client, "delegate"):
            resp = client.chat(probe, tools=None, on_think=emitter.think)
        question = (resp.get("content") or "").strip()
    except Exception:
        question = ""

    if len(question) < 20:
        # 模型没按要求写 → 用用户原话 + 最近一次工具结果兜底，别让委派失败
        last_tool = next((m.get("content", "") for m in reversed(history)
                          if m.get("role") == "tool"), "")
        question = (user_text + "\n\n已知项目信息：\n" + (last_tool or "（无）") +
                    "\n\n请分析并给出建议，按「结论 / 问题 / 改进建议」三段回答。")

    args = {"question": question, "site": "doubao"}
    emitter.tool_call("web_chat", args)
    t0 = time.time()
    res = tools.dispatch("web_chat", args, ctx)
    emitter.tool_result("web_chat", res.text, time.time() - t0)
    history.append({"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "web_chat", "arguments": args}}]})
    history.append({"role": "tool", "content": res.text, "tool_name": "web_chat"})
    if "[错误]" in res.text:
        # 外脑没连上就别让它反复试同一个工具把步数耗光
        history.append({"role": "user", "content":
                        "[系统] 外脑这次没连通，不要再调用 web_chat。"
                        "改用本地工具（read_file/grep_file 等）自己完成分析，"
                        "并在回答里说清「外脑本次不可用」和失败原因。"})


def strip_old_images(history: list) -> None:
    """图片只在当前回合保留；历史里的图片剥掉省 token。"""
    for msg in history:
        if msg.get("images"):
            msg.pop("images", None)
            msg["content"] += "\n[图片已从上下文剥离以节省 token]"


def pad_unanswered_tool_calls(history: list) -> int:
    """打断收尾：末条 assistant 声明了 tool_calls 但缺响应时，补齐占位消息。

    返回补了几条。缺响应的 tool_calls 会让下一轮请求被 Ollama 拒绝
    （assistant.tool_calls 必须跟对应的 tool 消息），所以必须补。
    """
    idx = len(history) - 1
    answered = 0
    while idx >= 0 and history[idx].get("role") == "tool":
        answered += 1
        idx -= 1
    if idx < 0 or history[idx].get("role") != "assistant":
        return 0
    calls = history[idx].get("tool_calls") or []
    missing = calls[answered:]
    for tc in missing:
        msg = {"role": "tool", "content": "[已被用户打断，未执行]",
               "tool_name": (tc.get("function") or {}).get("name", "?")}
        if tc.get("id"):
            msg["tool_call_id"] = tc["id"]
        history.append(msg)
    return len(missing)


def _policy_hint(cfg) -> str:
    """把「这次有哪些工具」写进系统提示词。

    基础 SYSTEM_PROMPT 写的是「需要读文件/联网时必须调用工具」，工具被关掉后
    这句话就成了误导：纯聊天模式下模型会声称「我去读一下」然后编一个工具结果，
    或者反复调一个不存在的工具把步数耗光。所以这里按实际下发的集合改口径。
    """
    if cfg is None:
        return ""
    names = toolpolicy.allowed_names(cfg)
    if names is None:
        return ""
    if not names:
        return ("\n本次对话没有启用任何工具：不要声称调用工具、不要编造文件内容或"
                "网页结果。需要工具才能完成的事，直接说明当前模式不提供工具。")
    return ("\n本次对话只启用了这些工具：" + "、".join(sorted(names)) +
            "。其他工具不可用，不要调用它们，也不要假装调用过。")


def new_history(workspace: str | None = None, cfg=None) -> list:
    """新建对话历史（system 消息 + 技能索引 + 工具口径）。

    cfg 决定三件事：
      - cfg=None（老调用方/测试）：照旧注入全部技能，行为不变
      - skill 工具被策略关掉：索引一并省略 —— 名字+描述常驻是 token 开销，
        而模型已经没有任何办法加载正文，留着只会让它编造技能内容
      - skills_disabled 里的技能：从索引里剔除
    """
    prompt = SYSTEM_PROMPT_CORE
    # 「生成文件必须用 write_file」这条只在真有 write_file 时才成立
    if toolpolicy.is_allowed(cfg, "write_file"):
        prompt += SYSTEM_PROMPT_FILE_RULE
    # 同理：「该问就问」只在 ask_user 可用时才成立。没有这个工具还教它去问，
    # 它只会说一句「请问您想选哪种」然后结束回合，用户答完还得重讲背景。
    if toolpolicy.is_allowed(cfg, "ask_user"):
        prompt += SYSTEM_PROMPT_ASK_RULE
    if workspace:
        prompt += f"\n当前工作空间目录: {workspace}"
    prompt += _policy_hint(cfg)
    try:  # 技能索引（名称+短描述）常驻；正文由 skill 工具按需加载
        from tools.skill import skill_index
        if toolpolicy.skill_tool_on(cfg):
            idx = skill_index(workspace, toolpolicy.disabled_skills(cfg))
            if idx:
                prompt += "\n" + idx
    except Exception:
        pass
    return [{"role": "system", "content": prompt}]


def run_turn(client: ChatClient, cfg: AgentConfig, ctx: ToolContext,
             history: list, user_text: str, images: list[str] | None = None,
             emitter=None, cancel=None, steer=None) -> str:
    """跑一个用户回合（含多轮工具调用），返回最终回答。

    emitter: 事件发射器；缺省用 ConsoleEmitter（终端输出）。
    cancel:  可选 threading.Event；置位后打断本轮（停流式生成、跳过未执行工具）。
    steer:   可选可调用，返回待注入的引导消息列表（输入队列「立即发送」）。
             每个步边界消费一次：消息以 user 身份追加进历史，模型不中断。
    """
    if emitter is None:
        emitter = ConsoleEmitter(cfg)

    _stats = _stats_with_step(emitter)      # 兼容老签名的 stats(st)

    # 回合级用量：账本挂在 client 上，ChatClient.chat 逐次记账；这里只记下
    # 起点，收尾时（finally，覆盖正常/打断/异常所有出口）取增量发一条事件。
    _ledger = getattr(client, "usage_ledger", None)
    usage_before = _ledger.snapshot() if _ledger is not None else None

    # 需要自己发子请求的工具（read_long_doc 逐段精读）从 ctx 拿运行时依赖。
    # 在这里统一注入，而不是各前端各写一遍：多一处遗漏就是一处「工具静默失效」。
    ctx.cfg = cfg
    ctx.client = client
    ctx.notice = getattr(emitter, "notice", None)
    # 本回合允许的工具：与下面下发的 schema 同源。schema 决定「模型看得见什么」，
    # 这个集合决定「真调了会不会被执行」—— 小模型会照旧对话里的工具名硬调，
    # 只靠不下发 schema 挡不住。
    ctx.tools_allowed = toolpolicy.allowed_names(cfg)
    if cancel is not None:
        ctx.cancel = cancel

    strip_old_images(history)

    # 防呆循环 guard：新用户消息清零连击（跨指令的重复不是循环）
    guard = RepeatGuard()
    guard.reset()

    user_msg = {"role": "user",
                "content": expert_hint(user_text) + longdoc.attachment_hint(
                    user_text, ctx.workspace, cfg.num_ctx)}
    if images:
        user_msg["images"] = images
    turn_start = len(history)          # 本轮 user 消息下标 = 请求视图的轮次边界
    history.append(user_msg)
    turn_start_ts = time.time()     # 时间读数的第 1 步基准（用户消息时刻）

    tools_spec = toolpolicy.specs_for(cfg)
    # 长会话压缩（DSH agent/pre-step 的等价点）：每步请求前做压力检查。
    # 默认本地确定性摘要零额外请求；llm 模式另发一次摘要请求，失败自动降级。
    summarize_fn = (compaction.make_llm_summarizer(client, cfg, cancel=cancel)
                    if getattr(cfg, "compaction_summarizer", "local") == "llm"
                    else None)
    compacted_this_turn = 0
    want_expert = (_expert_available() and expert_requested(user_text)
                   and toolpolicy.is_allowed(cfg, "web_chat"))  # 用户点名要用外脑
    expert_done = False                         # 本回合是否已经问过外脑
    final_text = ""
    resp: dict = {}
    pending_reminders: list[dict] = []          # 命中重复阈值待注入的提醒
    last_inject_ts: float | None = None         # 本回合上一次时间读数的采样时刻

    def _stopped() -> bool:
        return cancel is not None and cancel.is_set()

    # ---- 每步时间窗（开始/结束真实时刻，毫秒精度）----
    # 设计参考 DeepSeek Harness：数据层记录真实时刻，视图只做投影。
    # 步窗口 = 「第 N 步」分隔线到下一条分隔线（含本步全部工具执行）。
    # 在下一步开始时闭合上一步；回合收尾由 finally 兜住所有出口
    # （打断 / 报错 / 手动停止 / 触顶 / Ctrl+C），保证最后一步也有时刻。
    _cur: dict = {}

    def _close_step() -> None:
        if not _cur:
            return
        fn = getattr(emitter, "step_time", None)   # 老发射器没有此方法，跳过
        if fn:
            resp = _cur.get("resp") or {}
            fn(_cur["step"], _cur["t0"], time.time(), ttft=resp.get("ttft"))
        _cur.clear()

    try:
        for step in range(1, cfg.max_steps + 1):
            if _stopped():
                emitter.notice("[⏹ 已打断本轮回答]")
                break
            _close_step()
            _cur.update(step=step, t0=time.time())
            emitter.step(step)
            # 点名用外脑：先给一步时间采集，之后由代码直接委派给外脑
            # （小模型自己不会停手，详见 _delegate_to_expert 里的实测说明）
            if want_expert and not expert_done and step >= 2:
                expert_done = True
                _delegate_to_expert(client, cfg, ctx, history, emitter, user_text)
                continue
            # 上一工具步命中的重复提醒在这里注入（tool 结果之后、请求之前）。
            # 为什么不紧跟 tool 消息 append：打断收尾的 pad_unanswered_tool_calls
            # 依赖「末尾连续 tool 消息」的结构，中间插 user 消息会破坏配对。
            if pending_reminders:
                history.extend(pending_reminders)
                pending_reminders.clear()
            # 输入队列的「立即发送」（steering，参考 DSH next-step 队列）：
            # 步边界消费，user 身份入历史 + 前端气泡事件——模型不中断，
            # 下一次请求自然看到引导。步边界 = 当前步工具已执行完、请求未发出。
            if steer:
                for s in steer():
                    if not s:
                        continue
                    history.append({"role": "user", "content": s})
                    emitter.user_message(s)
            # ---- 调用模型（断流自动重试，设计参考 DSH dsh-llm-retry）----
            # 空输出失败（没吐一个字就断）→ 历史未动，退避后干净重跑；
            # 半截输出失败 → 已生成部分先入历史，注入「从中断处继续」后自动续写
            #（用户已看到的字绝不回滚，也绝不重复生成）；
            # FATAL（Key 错/参数错）与次数用尽走原收尾，不打无意义的转。
            # 时间读数（time-context）：请求时临时追加、不入 history——
            # 历史保持干净，模型每步都看到新鲜时刻。消息每步构造一次
            #（采样时刻冻结），但请求快照在**每次尝试内**重拼：半截断流的
            # 续写指令会先进 history，重试请求必须带上它，不能用旧快照。
            now_ts = time.time()
            time_msg = None
            if timectx.should_inject(step, last_inject_ts, now_ts,
                                     cfg.time_context, cfg.time_refresh_sec):
                baseline = turn_start_ts if step == 1 else (last_inject_ts or turn_start_ts)
                time_msg = timectx.time_message(step, baseline, now_ts)
                last_inject_ts = now_ts
            retry_no = 0
            while True:
                # 请求视图投影（参考 DSH「模型可见 ⟺ 已记录」）：折叠只发生在
                # 派生视图里，落盘 history 一行不改。放在 while 内是必须的——
                # 半截断流会把续写指令追加进 history，每次尝试都得重算快照。
                view = context_view.project_messages(history, user_text=user_text,
                                                     turn_start=turn_start, cfg=cfg)
                # 压缩只在请求视图超阈值时发生；checkpoint 以「只追加」方式写进
                # 日志（旧消息一条不删），随后重算视图。失败/不够短/超次数一律
                # 静默跳过，绝不因为压缩打断本轮（DSH index.ts:165-172）。
                ckpt = compaction.compact_if_needed(
                    history, cfg, view=view, summarize_fn=summarize_fn,
                    compacted_this_turn=compacted_this_turn)
                if ckpt:
                    history.append(ckpt)
                    compacted_this_turn += 1
                    view = context_view.project_messages(
                        history, user_text=user_text, turn_start=turn_start, cfg=cfg)
                    emitter.notice(
                        f"[长会话压缩] {ckpt['shadowed_messages']} 条旧消息"
                        f"（约 {ckpt['shadowed_tokens']} tokens）已折叠为 1 条检查点；"
                        "原始记录仍在会话日志，可完整回放。")
                req_messages = view + [time_msg] if time_msg else view
                try:
                    resp = client.chat(
                        req_messages,
                        tools=tools_spec,
                        on_text=emitter.text,
                        on_think=emitter.think,
                        cancel=cancel,
                        on_wait=getattr(emitter, "thinking", None),
                        # 前端不显示思考内容时，think 块对用户等于不存在，不能算「有进展」，
                        # 否则心跳会被这些看不见的块一路抑制掉。
                        think_hidden=getattr(emitter, "think_hidden", True),
                    )
                except Exception as e:
                    # 请求阶段失败。不能只捕 RuntimeError：http.client / ssl 层的
                    # 瞬态错误是裸抛的 OSError 家族（实测 ssl.SSLError「EOF occurred
                    # in violation of protocol」直接穿透重试网把回合弄死）。
                    # 识别不出的错误 classify_error 会归为 FATAL，照样 raise，语义不变。
                    if _stopped():
                        # 打断与断连的竞态：按打断收尾，别把主动停止报成错误
                        resp = {"thinking": "", "content": "", "tool_calls": [],
                                "stats": {}, "cancelled": True, "error": None,
                                "ttft": None}
                        break
                    kind = classify_error(str(e))
                    if kind in RETRYABLE and retry_no < cfg.stream_retries:
                        retry_no += 1
                        delay = backoff_delay(retry_no)
                        # 错误摘要给够整句：以前 [:80] 会把「或减小上下文」砍成
                        # 「或减小上」，用户看到半句话加一句「N 秒后自动重试」。
                        emitter.notice(f"[⚠ {str(e)[:140]}，{delay:.1f} 秒后自动重试"
                                       f"（{retry_no}/{cfg.stream_retries}）]")
                        if not interruptible_sleep(delay, cancel):
                            # 退避期间用户打断：立即收尾，不留隐形等待
                            resp = {"thinking": "", "content": "", "tool_calls": [],
                                    "stats": {}, "cancelled": True, "error": None,
                                    "ttft": None}
                            break
                        continue
                    raise     # Key 错 / 参数错：重试无用，照原样报错
                _cur["resp"] = resp      # 步时间窗闭合时取 ttft 用
                err = resp.get("error")
                if (err and not resp.get("cancelled")
                        and retry_no < cfg.stream_retries
                        and classify_error(err) in RETRYABLE):
                    retry_no += 1
                    partial = resp.get("content") or ""
                    if partial:
                        # 半截：保住用户已看到的字，再注入续写指令（自动恢复对话）
                        note = f"[回答中断：{err}]"
                        history.append({"role": "assistant",
                                        "content": partial + "\n" + note})
                        history.append({"role": "user", "content":
                                        "[系统] 上一次回答在生成途中断开了。上面那条 "
                                        "assistant 消息就是已生成的部分。"
                                        "请从中断处自然继续，不要重复已输出内容，"
                                        "也不要提中断这件事。"})
                        emitter.notice(f"[⚠ 回答中断，已保留已生成部分，自动续写"
                                       f"（{retry_no}/{cfg.stream_retries}）]")
                    else:
                        emitter.notice(f"[⚠ {err[:140]}，自动重试"
                                       f"（{retry_no}/{cfg.stream_retries}）]")
                    continue
                break     # 成功 / 打断 / 不可重试 / 次数用尽 → 交给下方原判定收尾

            emitter.newline()

            # 每一步单独报一次耗时。以前只在回合末尾发一次，用的是**最后一次**
            # resp 的 stats —— 于是「生成 9000 字符工具参数花了两百秒」那一步
            # 的耗时在界面上完全不存在，用户只看到最后一步的小数字，永远对不上。
            st = resp.get("stats") or {}
            if st:
                _stats(st, step=step)

            content = resp["content"]
            tool_calls = resp["tool_calls"]

            if resp.get("cancelled"):
                # 流式生成被打断：部分回答入历史（保持对话结构合法），不再执行工具
                assistant_msg = {"role": "assistant",
                                 "content": (content + "\n[已被用户打断]").strip()
                                 if content else "[已被用户打断]"}
                history.append(assistant_msg)
                final_text = content
                emitter.notice("[⏹ 已打断本轮回答]")
                break

            if resp.get("error"):
                # 断流（读超时 / 连接重置 / 没收到结束帧）：已生成的部分必须入历史，
                # 否则用户看到的字会凭空消失，接着说「继续」时模型也不知道自己说到哪。
                err = resp["error"]
                note = f"[回答中断：{err}]"
                history.append({"role": "assistant",
                                "content": (content + "\n" + note) if content else note})
                final_text = content
                # 收尾建议按类别给：模型受限（并发超限）重试是空转，直接提醒换模型；
                # 瞬态限流重试用尽才到这里，等几秒常可恢复。
                kind = classify_error(err)
                advice = ""
                if kind == LIMITED:
                    advice = ("（该模型当前使用受限（并发超限），重试无意义——"
                              "请切换其他模型再试）")
                elif kind == RATE_LIMIT:
                    advice = ("（账号限流：等几秒再说「继续」通常即可恢复；"
                              "若反复出现，可切换模型，或先让其他并行会话跑完）")
                emitter.notice(f"[⚠ 回答中断：{err}] 已保留生成到的部分，"
                               f"可直接说「继续」{advice}")
                break

            assistant_msg: dict = {"role": "assistant", "content": content}
            if tool_calls:
                # id 在这里统一生成，供随后 tool 消息的 tool_call_id 配对
                # （Ollama 忽略它，OpenAI 兼容后端没它直接 400）
                ids = [f"call_{step}_{i}" for i in range(len(tool_calls))]
                assistant_msg["tool_calls"] = [
                    {"id": ids[i],
                     "function": {"name": tc["function"]["name"],
                                  "arguments": tc["function"]["arguments"]}}
                    for i, tc in enumerate(tool_calls)
                ]
            history.append(assistant_msg)

            if not tool_calls:
                final_text = content
                break

            for i, tc in enumerate(tool_calls):
                name = tc["function"]["name"]
                if name == "web_chat":
                    expert_done = True      # 问过外脑了，后面恢复正常工具集
                args = tc["function"].get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                if _stopped():
                    # 已声明未执行的 tool_calls 补占位结果，保持历史合法
                    for j in range(i, len(tool_calls)):
                        history.append({"role": "tool",
                                        "content": "[已被用户打断，未执行]",
                                        "tool_name": tool_calls[j]["function"]["name"],
                                        "tool_call_id": ids[j]})
                    emitter.notice("[⏹ 已打断，跳过未执行的工具]")
                    final_text = content
                    return final_text
                emitter.tool_call(name, args)
                t0 = time.time()
                result: ToolResult = tools.dispatch(name, args, ctx)
                dt = time.time() - t0
                emitter.tool_result(name, result.text, dt, images=result.images)

                tool_msg: dict = {"role": "tool", "content": result.text,
                                  "tool_name": name, "tool_call_id": ids[i]}
                if result.images:
                    tool_msg["images"] = result.images
                history.append(tool_msg)

                # 防呆循环观察：执行后计数（失败的调用同样计数），命中阈值
                # 生成提醒，下一次模型请求前注入。只提醒不否决。
                reminder = guard.observe(name, args)
                if reminder:
                    emitter.notice(f"[⚠ {name} 连续第 "
                                   f"{guard.count} 次相同调用，已提醒模型换方法]")
                    pending_reminders.append({"role": "user", "content": reminder})
        else:
            final_text = "[已达到最大工具步数，强制结束]"
            emitter.notice(final_text)
    except KeyboardInterrupt:
        # 终端 Ctrl+C 落在工具执行/步骤切换处：补占位后回到输入提示（不退出程序）
        pad_unanswered_tool_calls(history)
        emitter.newline()
        emitter.notice("[⏹ 已打断本轮回答]")
        return resp.get("content") or ""
    finally:
        _close_step()      # 最后一步的结束时刻：任何出口都要闭合时间窗
        if _ledger is not None:
            _fn = getattr(emitter, "usage", None)
            if _fn:
                try:
                    _delta = _ledger.delta(usage_before)
                    if _delta["requests"]:
                        _fn(_delta, _ledger.snapshot())
                except Exception:
                    pass      # 统计链路出错绝不能影响回结果

    # 这里不再补报统计：每一轮 chat 之后都已经按步发过了（见上面循环里的
    # emitter.stats(_stats, step=step)）。留着这里只会把最后一步的耗时打印两遍。
    return final_text
