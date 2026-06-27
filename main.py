from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

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

    async def initialize(self) -> None:
        self._bot_nickname = await self._detect_nickname()
        self._config_path = await self._resolve_config_path()
        self._sync_wake_prefixes()

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
                            logger.info(
                                f"[CallMe] 检测到机器人昵称: {nick}"
                            )
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

    def _sync_wake_prefixes(self) -> None:
        try:
            names = self._get_names()
            if not names:
                return

            core_config = self.context._config
            wake_prefixes = core_config.get("wake_prefix", ["/"])

            for name in names:
                if name not in wake_prefixes:
                    wake_prefixes.append(name)

            core_config["wake_prefix"] = wake_prefixes
            if names:
                logger.info(f"[CallMe] 已同步唤醒名字到系统配置: {names}")
        except Exception as e:
            logger.error(f"[CallMe] 同步唤醒前缀失败: {e}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        original_text = ""
        for comp in event.get_messages():
            if isinstance(comp, (Plain,)) and comp.text:
                original_text += comp.text

        original_text = original_text.strip()
        if not original_text:
            return

        matched_name = self._match_name(original_text)
        if not matched_name:
            return

        remaining = original_text[len(matched_name):].strip()
        if not remaining:
            event.stop_event()
            return

        self_id = event.get_self_id()
        if self_id:
            req.prompt = f"[CQ:at,qq={self_id}] {remaining}"
        else:
            req.prompt = remaining

        logger.debug(
            f"[CallMe] 名字唤醒「{matched_name}」→ 注入 @mention"
        )

    @filter.command("callme")
    async def callme_cmd(self, event: AstrMessageEvent):
        parts = (event.message_str or "").strip().split(maxsplit=2)

        if len(parts) == 1:
            names = self._get_names()
            if names:
                yield event.plain_result(
                    f"当前唤醒名字：{'、'.join(names)}"
                )
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
            self._sync_wake_prefixes()
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
                yield event.plain_result(
                    f"「{name}」不在唤醒列表中"
                )
                return
            names.remove(name)
            self.config["name_list"] = names
            self._save_config()
            self._sync_wake_prefixes()
            logger.info(f"[CallMe] 移除唤醒名字: {name}")
            yield event.plain_result(f"已移除唤醒名字「{name}」")

        else:
            yield event.plain_result(
                f"未知操作「{action}」。可用操作：add、remove、list"
            )

    async def terminate(self) -> None:
        try:
            core_config = self.context._config
            names = self._get_names()
            if not names:
                return
            wake_prefixes = core_config.get("wake_prefix", ["/"])
            core_config["wake_prefix"] = [
                p for p in wake_prefixes if p not in names
            ]
            logger.info("[CallMe] 已清理唤醒名字")
        except Exception as e:
            logger.debug(f"[CallMe] 清理时异常: {e}")
