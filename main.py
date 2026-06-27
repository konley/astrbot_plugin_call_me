from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain


class CallMePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._bot_nickname = ""
        self._config_path: Path | None = None
        self._dedup: Dict[str, float] = {}
        self._dedup_window = 60

    async def initialize(self) -> None:
        self._bot_nickname = await self._detect_nickname()
        self._config_path = await self._resolve_config_path()
        self._clean_stale_wake_prefixes()

    async def _resolve_config_path(self) -> Path | None:
        try:
            data_dir = self.context.get_data_dir()
            candidate = data_dir / "config" / "astrbot_plugin_call_me_config.json"
            if candidate.exists():
                return candidate
            parent = candidate.parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            return None

    def _save_config(self) -> None:
        if self._config_path is None:
            return
        try:
            data = {"name_list": self.config.get("name_list", [])}
            self._config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"[CallMe] 保存配置失败: {e}")

    def _clean_stale_wake_prefixes(self) -> None:
        try:
            core_config = self.context._config
            names = self._get_names()
            wake_prefixes = core_config.get("wake_prefix", ["/"])
            cleaned = [p for p in wake_prefixes if p not in names]
            if len(cleaned) != len(wake_prefixes):
                core_config["wake_prefix"] = cleaned
                logger.info("[CallMe] 已清理 core config 中残留的唤醒名字")
        except Exception as e:
            logger.debug(f"[CallMe] 清理残留前缀失败: {e}")

    async def _detect_nickname(self) -> str:
        try:
            pm = getattr(self.context, "platform_manager", None)
            if pm and hasattr(pm, "platform_insts"):
                for plat in pm.platform_insts:
                    adapter_name = str(
                        getattr(plat, "adapter_name", "") or ""
                    ).lower()
                    if "aiocqhttp" not in adapter_name:
                        continue
                    bot = getattr(plat, "bot_instance", None) or getattr(
                        plat, "bot", None
                    )
                    if bot and hasattr(bot, "get_login_info"):
                        info = await bot.get_login_info()
                        nick = info.get("nickname", "")
                        if nick:
                            logger.info(f"[CallMe] 检测到机器人昵称: {nick}")
                            return nick
        except Exception as e:
            logger.debug(f"[CallMe] 自动检测昵称失败: {e}")
        return ""

    def _get_names(self) -> List[str]:
        names = self.config.get("name_list", [])
        if isinstance(names, str):
            names = [names]
        names = [n.strip() for n in names if n and n.strip()]
        if not names and self._bot_nickname:
            names = [self._bot_nickname]
        return names[:3]

    def _match_name(self, text: str) -> str | None:
        for name in self._get_names():
            if text.startswith(name):
                return name
        return None

    def _dedup_key(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}_{event.get_sender_id()}"

    def _is_dedup(self, event: AstrMessageEvent) -> bool:
        key = self._dedup_key(event)
        now = time.time()
        last = self._dedup.get(key, 0)
        if now - last < self._dedup_window:
            return True
        self._dedup[key] = now
        return False

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def on_message(self, event: AstrMessageEvent):
        if event.get_self_id() == event.get_sender_id():
            return

        text = (event.message_str or "").strip()
        if not text:
            return

        matched_name = self._match_name(text)
        if not matched_name:
            return

        setattr(event, "_call_me_name", matched_name)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        matched_name = getattr(event, "_call_me_name", None)
        if not matched_name:
            if not event.is_at_or_wake_command:
                event.stop_event()
            return

        original_text = ""
        for comp in event.get_messages():
            if isinstance(comp, Plain) and comp.text:
                original_text += comp.text
        original_text = original_text.strip()

        remaining = original_text[len(matched_name):].strip()
        if not remaining:
            event.stop_event()
            return

        if self._is_dedup(event):
            logger.debug(
                f"[CallMe] 去重触发，跳过 {self._dedup_key(event)}"
            )
            event.stop_event()
            return

        self_id = event.get_self_id()
        if self_id:
            req.prompt = f"[CQ:at,qq={self_id}] {remaining}"
        else:
            req.prompt = remaining

        logger.info(f"[CallMe] 名字唤醒「{matched_name}」→ 注入 @mention")

    @filter.command("callme")
    async def callme_cmd(self, event: AstrMessageEvent):
        parts = (event.message_str or "").strip().split(maxsplit=2)

        if len(parts) == 1:
            names = self._get_names()
            if names:
                yield event.plain_result(f"当前唤醒名字：{'、'.join(names)}")
            else:
                yield event.plain_result(
                    "未配置唤醒名字。可用「callme add <名字>」添加。"
                )
            return

        action = parts[1]

        if action == "help":
            yield event.plain_result(
                "callme 指令：\n"
                "  callme          — 查看当前唤醒名字\n"
                "  callme list     — 列出唤醒名字\n"
                "  callme add <名> — 添加唤醒名字（最多 3 个）\n"
                "  callme remove   — 移除唤醒名字\n"
                "在群聊中直接以唤醒名字开头即可触发机器人，无需 @"
            )

        elif action == "list":
            names = self._get_names()
            yield event.plain_result(
                f"当前唤醒名字：{'、'.join(names) if names else '无'}"
            )

        elif action == "add":
            if len(parts) < 3:
                yield event.plain_result(
                    "请指定要添加的名字。用法：callme add <名字>"
                )
                return
            name = parts[2]
            names = self._get_names()
            if name in names:
                yield event.plain_result(f"「{name}」已在唤醒列表中")
                return
            if len(names) >= 3:
                yield event.plain_result("最多只能设置 3 个唤醒名字")
                return
            names.append(name)
            self.config["name_list"] = names
            self._save_config()
            logger.info(f"[CallMe] 添加唤醒名字: {name}")
            yield event.plain_result(f"已添加唤醒名字「{name}」")

        elif action == "remove":
            if len(parts) < 3:
                yield event.plain_result(
                    "请指定要移除的名字。用法：callme remove <名字>"
                )
                return
            name = parts[2]
            names = self._get_names()
            if name not in names:
                yield event.plain_result(f"「{name}」不在唤醒列表中")
                return
            names.remove(name)
            self.config["name_list"] = names
            self._save_config()
            logger.info(f"[CallMe] 移除唤醒名字: {name}")
            yield event.plain_result(f"已移除唤醒名字「{name}」")

        else:
            yield event.plain_result(
                f"未知操作「{action}」。可用操作：add、remove、list"
            )

    async def terminate(self) -> None:
        self._clean_stale_wake_prefixes()
