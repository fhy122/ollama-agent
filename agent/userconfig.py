"""用户配置文件：把「模型配置」存到 ~/.ollama-agent/config.json。

为什么需要这个文件（而不是只有会话存档）：
    会话存档（webui/store.py）记的是「聊了什么」；这里记的是「连哪个模型怎么连」。
    后者以前只活在内存里 —— 每次重启都要重新填地址、Key、模型名。
    两者都在 ~/.ollama-agent/ 下，但职责不同，不合并。

文件结构：
{
  "version": 1,
  "active": "ollama-local",              # 当前用哪个模型配置的 id
  "profiles": [
    {
      "id": "ollama-local",              # 内部唯一键（不可改）
      "name": "本地 Ollama",              # 显示名（可改）
      "provider": "ollama",              # ollama | openai
      "host": "http://localhost:11434",
      "api_key": "",
      "model": "qwen3.5:9b",
      "think": "off",
      "num_ctx": 65536,
      "temperature": 0.7
    }
  ],
  "ui": { "confirm": true, "max_steps": 12, "workspace": "" }
}

为什么把模型相关参数放进 profile、而 confirm/workspace 放外面：
    「切到一个模型」隐含的是地址 + Key + 模型名 + 那套推理参数一起换。
    confirm（执行前确认）和 workspace 属于「我用这个工具的习惯」，
    换模型不该把它们一起重置。

安全：
    API Key 明文存在本文件，仅靠文件权限保护（POSIX 下 0600）。
    接口回传一律走 mask_key() 打码，前端拿不到完整 Key。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from agent import HIDDEN_DIRNAME

ENV_CONFIG = "OLLAMA_AGENT_CONFIG"
DEFAULT_PATH = Path.home() / HIDDEN_DIRNAME / "config.json"
VERSION = 1

# 一个 profile 里跟「连哪个模型」有关的字段。顺序即前端表单顺序。
PROFILE_FIELDS = ("name", "provider", "host", "api_key", "model",
                  "think", "num_ctx", "temperature")

DEFAULT_PROFILE = {
    "name": "本地 Ollama",
    "provider": "ollama",
    "host": "http://localhost:11434",
    "api_key": "",
    "model": "qwen3.5:9b",
    "think": "off",
    "num_ctx": 65536,
    "temperature": 0.7,
}

BUILTIN_ID = "ollama-local"     # 内置条目：从 Ollama /api/tags 实时读，不存在文件里


def new_profile_id() -> str:
    return "p-" + uuid.uuid4().hex[:8]


def mask_key(key: str) -> str:
    """Key 打码：只留前 4 后 4，短 Key 全遮。

    前端永远不该拿到完整 Key —— 打码值是给人「确认是不是这把」用的，
    不是给程序用的。
    """
    k = str(key or "")
    if not k:
        return ""
    if len(k) <= 8:
        return "*" * len(k)
    return f"{k[:4]}…{k[-4:]}"


class ConfigStore:
    """用户配置的读写。写操作走临时文件 + 原子替换，避免写一半留下坏 JSON。"""

    def __init__(self, path: str | Path | None = None):
        raw = path or os.environ.get(ENV_CONFIG) or DEFAULT_PATH
        self.path = Path(raw).expanduser()
        self._lock = threading.Lock()

    # ---------- 读 ----------

    def load(self) -> dict:
        """读配置。文件不存在 / 损坏都返回默认值，不抛异常。

        配置文件坏了不该让应用起不来 —— 但也不能静默吞掉：
        调用方通过返回值的 `_recovered` 字段知道「这次是重来的」。
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._defaults()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._defaults(_recovered="配置文件损坏，已按默认配置启动")

        if not isinstance(data, dict):
            return self._defaults(_recovered="配置文件格式不对，已按默认配置启动")
        return self._normalize(data)

    def _defaults(self, _recovered: str = "") -> dict:
        out = {
            "version": VERSION,
            "active": BUILTIN_ID,
            "profiles": [],          # 内置 Ollama 不落盘，见 BUILTIN_ID
            "ui": {"confirm": True, "max_steps": 12, "workspace": ""},
        }
        if _recovered:
            out["_recovered"] = _recovered
        return out

    def _normalize(self, data: dict) -> dict:
        """把任意文件内容收敛成合法结构：缺字段补默认，脏数据丢掉。

        这里宽松是有意的：手改配置文件的人（和旧版本留下的文件）
        不该因为少一个字段就整个配置作废。
        """
        profiles: list[dict] = []
        seen: set[str] = set()
        for p in data.get("profiles") or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip() or new_profile_id()
            if pid in seen:
                continue
            if pid == BUILTIN_ID:
                continue          # 内置条目是虚拟的，旧文件里若残留就丢掉
            seen.add(pid)
            row = {"id": pid}
            for k, v in DEFAULT_PROFILE.items():
                if k == "model":
                    continue      # model 不填默认值，见下
                row[k] = p.get(k, v)
            row["provider"] = row["provider"] if row["provider"] in ("ollama", "openai") else "ollama"
            row["host"] = str(row.get("host") or "").strip()
            row["api_key"] = str(row.get("api_key") or "")
            # model 不拿默认值兜底：默认值是本机 Ollama 的模型名，
            # 塞给一个 OpenAI 兼容站只会换来「模型不存在」。缺了就让它是空的，
            # 强制用户自己填 —— 空模型名在应用层会明确报错，比一个假名字好查。
            row["model"] = str(p.get("model") or "").strip()
            row["name"] = str(row.get("name") or "").strip() or row["model"] or "未命名模型"
            profiles.append(row)
        if not profiles:
            profiles = []            # 允许一条自定义都没有：内置 Ollama 兜底

        active = str(data.get("active") or "")
        if not active:
            # 没写 active：只有一条自定义就默认用它（手写配置的常见情况，
            # 指向内置会让人以为「我配了却没生效」）；否则回内置。
            active = profiles[0]["id"] if len(profiles) == 1 else BUILTIN_ID
        elif active != BUILTIN_ID and active not in {p["id"] for p in profiles}:
            active = BUILTIN_ID       # 指不到就回内置，不留悬空引用

        ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
        return {
            "version": VERSION,
            "active": active,
            "profiles": profiles,
            "ui": {
                "confirm": bool(ui.get("confirm", True)),
                "max_steps": int(ui.get("max_steps") or 12),
                "workspace": str(ui.get("workspace") or ""),
            },
        }

    # ---------- 写 ----------

    def save(self, data: dict) -> Path:
        payload = dict(data)
        payload.pop("_recovered", None)
        payload["version"] = VERSION
        payload["updated"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with self._lock:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)      # 里面有 Key；Windows 上会失败，忽略
            except OSError:
                pass
            os.replace(tmp, self.path)
        return self.path

    # ---------- profile 操作 ----------

    def get_profile(self, data: dict, pid: str) -> dict | None:
        for p in data.get("profiles") or []:
            if p.get("id") == pid:
                return p
        return None

    def upsert_profile(self, data: dict, profile: dict) -> dict:
        """新增或按 id 更新一个 profile，返回写进列表的那份。"""
        pid = str(profile.get("id") or "").strip()
        if pid == BUILTIN_ID:
            # 内置条目由 Ollama /api/tags 实时提供，不接受写入
            pid = ""
        row = {"id": pid or new_profile_id()}
        for k, v in DEFAULT_PROFILE.items():
            if k == "model":
                continue          # 同上：模型名绝不能凭空造一个
            row[k] = profile.get(k, v)
        row["provider"] = row["provider"] if row["provider"] in ("ollama", "openai") else "ollama"
        row["host"] = str(row.get("host") or "").strip()
        row["api_key"] = str(row.get("api_key") or "")
        row["model"] = str(profile.get("model") or "").strip()
        row["name"] = str(row.get("name") or "").strip() or row["model"] or "未命名模型"

        rows = list(data.get("profiles") or [])
        for i, p in enumerate(rows):
            if p.get("id") == row["id"]:
                rows[i] = row
                break
        else:
            rows.append(row)
        data["profiles"] = rows
        return row

    def delete_profile(self, data: dict, pid: str) -> dict:
        """删一个 profile。内置条目不许删，其余删到空也允许（内置永远在）。

        返回 {"ok": bool, "error": str}：错误信息要能指路，
        不能只说「删不掉」。
        """
        if pid == BUILTIN_ID:
            return {"ok": False,
                    "error": "「本地 Ollama」是内置条目，不能删除。"
                             "不想用它的话，切换到别的模型配置即可。"}
        rows = [p for p in (data.get("profiles") or []) if p.get("id") != pid]
        if len(rows) == len(data.get("profiles") or []):
            return {"ok": False, "error": "找不到这个模型配置，可能已被删除（刷新页面看看）"}
        data["profiles"] = rows
        if data.get("active") == pid:
            # 删掉当前生效的 → 回到内置 Ollama，而不是随便挑一条别人的站
            data["active"] = BUILTIN_ID
        return {"ok": True, "active": data["active"]}


def to_agent_patch(profile: dict, ui: dict) -> dict:
    """profile + ui → 一份可以直接喂给 AgentSession.update_config 的补丁。

    这样「切模型」和「改设置」走同一条代码路径，不必维护两套应用逻辑。
    """
    return {
        "provider": profile.get("provider") or "ollama",
        "host": profile.get("host") or "",
        "api_key": profile.get("api_key") or "",
        "model": profile.get("model") or "",
        "think": profile.get("think") or "off",
        "num_ctx": profile.get("num_ctx", 65536),
        "temperature": profile.get("temperature", 0.7),
        "confirm": (ui or {}).get("confirm", True),
        "max_steps": (ui or {}).get("max_steps", 12),
        "workspace": (ui or {}).get("workspace", ""),
    }
