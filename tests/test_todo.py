"""todo_write 任务清单工具测试。

契约（借鉴 DSH dsh-tool-todo）：
- 整表替换：每次提交完整清单，写 .ollama-agent/todo.json
- 校验 fail-loud：空/重复描述、非法状态、多个 in_progress 都报错且不落盘
- 成功回复计数：N pending, M in progress, K completed
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools  # noqa: F401  触发工具注册
from agent.registry import ToolContext, dispatch
from tools.todo import TODO_PATH


def _ctx(tmp_path):
    return ToolContext(workspace=tmp_path, cfg=None)


def _write(ctx, todos):
    return dispatch("todo_write", {"todos": todos}, ctx)


def test_valid_write_persists_and_counts(tmp_path):
    """合法清单：落盘规范化结果，回复计数。"""
    r = _write(_ctx(tmp_path), [
        {"content": " 读文档 ", "status": "in_progress"},
        {"content": "写文档", "status": "pending"},
    ])
    assert r.text == "Updated todo list: 1 pending, 1 in progress, 0 completed.", r.text
    saved = json.loads((tmp_path / TODO_PATH).read_text(encoding="utf-8"))
    assert saved == [{"content": "读文档", "status": "in_progress"},
                     {"content": "写文档", "status": "pending"}], "应去空格并保序"


def test_whole_list_replacement(tmp_path):
    """第二次提交整表替换旧清单（last-write-wins）。"""
    ctx = _ctx(tmp_path)
    _write(ctx, [{"content": "旧任务", "status": "pending"}])
    _write(ctx, [{"content": "新任务A", "status": "completed"},
                 {"content": "新任务B", "status": "in_progress"}])
    saved = json.loads((tmp_path / TODO_PATH).read_text(encoding="utf-8"))
    assert [i["content"] for i in saved] == ["新任务A", "新任务B"]


def test_invalid_rejected_and_nothing_written(tmp_path):
    """空/重复/坏状态/多活跃：报错，不落盘。"""
    ctx = _ctx(tmp_path)
    bad = [
        [],                                                  # 空清单
        [{"content": "  ", "status": "pending"}],            # 空描述
        [{"content": "a", "status": "pending"},
         {"content": "a", "status": "pending"}],             # 重复
        [{"content": "a", "status": "done"}],                # 非法状态
        [{"content": "a", "status": "in_progress"},
         {"content": "b", "status": "in_progress"}],         # 多个活跃
        [{"content": "a", "status": "pending", "note": "x"}],  # 多余字段
    ]
    for todos in bad:
        r = _write(ctx, todos)
        assert r.text.startswith("[错误] invalid todos:"), (todos, r.text)
        assert not (tmp_path / TODO_PATH).exists(), f"失败不应落盘: {todos}"


def test_error_reply_guides_model_to_retry(tmp_path):
    """报错文案带「未更新，请修正后重提」指引，模型能自我纠正。"""
    r = _write(_ctx(tmp_path), [{"content": "a", "status": "done"}])
    assert "重提完整清单" in r.text and "pending" in r.text, r.text
