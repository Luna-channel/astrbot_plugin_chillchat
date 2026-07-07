from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register("chillchat", "柯尔", "上下文字符数裁剪插件（多档位）", "1.1.2")
class ChillChat(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        # 基础设置
        basic = config.get("basic_settings") or {}
        self.max_chars = self._safe_int(basic.get("max_chars"), 8000)
        self.apply_to = str(basic.get("apply_to", "private")).lower()
        if self.apply_to not in ("private", "group", "all"):
            self.apply_to = "private"
        # VIP设置
        vip = config.get("vip_settings") or {}
        self.vip_max_chars = self._safe_int(vip.get("vip_max_chars"), 0)
        self.vip_users = self._to_str_set(vip.get("vip_users", []))
        self.vip_groups = self._to_str_set(vip.get("vip_groups", []))
        # 白名单设置
        wl = config.get("whitelist_settings") or {}
        self.whitelist_users = self._to_str_set(wl.get("whitelist_users", []))
        self.whitelist_groups = self._to_str_set(wl.get("whitelist_groups", []))

    def _safe_int(self, value, default: int) -> int:
        """安全转换为整数"""
        try:
            return int(value) if value is not None else default
        except (ValueError, TypeError):
            return default

    def _to_str_set(self, items) -> set:
        """将列表转换为字符串集合，兼容数字和字符串混合输入"""
        if not items:
            return set()
        if not isinstance(items, (list, tuple, set)):
            return {str(items)}
        return {str(item).strip() for item in items if item is not None}

    def _in_whitelist(self, id_value: str, whitelist: set) -> bool:
        """检查ID是否在白名单中，兼容类型"""
        if not id_value:
            return False
        return str(id_value).strip() in whitelist

    @filter.on_llm_request()
    async def trim_context(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest,
        *args,
        **kwargs,
    ):
        """在LLM请求前检测历史记录字符数，超限则裁剪"""
        try:
            # 安全检查：contexts 必须存在且为列表
            if not request.contexts or not isinstance(request.contexts, list):
                return

            is_private = event.is_private_chat()

            # 检查生效范围
            if self.apply_to == "private" and not is_private:
                return
            if self.apply_to == "group" and is_private:
                return

            # 获取身份信息
            sender_id = event.get_sender_id()
            group_id = None if is_private else event.get_group_id()

            # 检查白名单用户
            if self._in_whitelist(sender_id, self.whitelist_users):
                return

            # 检查白名单群聊
            if group_id and self._in_whitelist(group_id, self.whitelist_groups):
                return

            # 确定该用户/群适用的档位上限（优先级：VIP > 默认）
            effective_max = self._resolve_tier(sender_id, group_id)

            # 计算历史记录总字符数
            total_chars = self._calc_context_chars(request.contexts)

            # 如果未超限，直接返回
            if total_chars <= effective_max:
                return

            # 裁剪历史记录：从最旧的消息开始移除，保留至少一条
            original_count = len(request.contexts)
            while total_chars > effective_max and len(request.contexts) > 1:
                request.contexts.pop(0)
                total_chars = self._calc_context_chars(request.contexts)

            # 修复裁剪后可能产生的非法消息序列
            # （孤儿 tool、开头非 user、不完整的 tool_calls 配对等）
            request.contexts = self._fix_contexts(request.contexts)

            total_chars = self._calc_context_chars(request.contexts)
            trimmed_count = original_count - len(request.contexts)
            if trimmed_count > 0:
                logger.info(
                    f"[ChillChat] 裁剪了 {trimmed_count} 条历史记录，"
                    f"剩余 {len(request.contexts)} 条，当前字符数: {total_chars}"
                )
        except Exception as e:
            logger.error(f"[ChillChat] 裁剪上下文时出错: {e}")

    def _resolve_tier(self, sender_id: str, group_id: str | None) -> int:
        """根据用户/群确定适用的上下文上限，优先级：VIP > 默认"""
        if self.vip_max_chars > 0:
            if self._in_whitelist(sender_id, self.vip_users):
                return self.vip_max_chars
            if group_id and self._in_whitelist(group_id, self.vip_groups):
                return self.vip_max_chars
        return self.max_chars

    def _calc_context_chars(self, contexts: list) -> int:
        """计算上下文列表的总字符数"""
        if not contexts:
            return 0
        total = 0
        for ctx in contexts:
            if not isinstance(ctx, dict):
                continue
            content = ctx.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                # 处理多模态消息（图片+文字）
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        total += len(str(item.get("text", "")))
        return total

    def _fix_contexts(self, contexts: list) -> list:
        """修复裁剪后的上下文列表，确保符合 LLM API 要求。

        1. 确保首条消息是 user 角色（跳过开头的孤儿 tool / 落单 assistant）
        2. 确保 assistant(tool_calls) 与 tool 响应配对完整，丢弃不完整的配对
        """
        if not contexts:
            return contexts

        # --- 第一步：跳过开头所有非 user 的消息 ---
        start = 0
        for i, msg in enumerate(contexts):
            if isinstance(msg, dict) and msg.get("role") == "user":
                start = i
                break
        else:
            # 整个列表里没有 user 消息，返回空列表
            return []

        trimmed = contexts[start:]

        # --- 第二步：确保 tool 配对完整 ---
        fixed: list = []
        pending_assistant: dict | None = None
        pending_tools: list = []

        def flush():
            nonlocal pending_assistant, pending_tools
            if pending_assistant is not None and pending_tools:
                fixed.append(pending_assistant)
                fixed.extend(pending_tools)
            pending_assistant = None
            pending_tools = []

        for msg in trimmed:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")

            if role == "tool":
                if pending_assistant is not None:
                    pending_tools.append(msg)
                # 没有 pending_assistant 的孤儿 tool 直接丢弃
                continue

            if role == "assistant" and msg.get("tool_calls"):
                flush()
                pending_assistant = msg
                continue

            # 普通消息（user / 无 tool_calls 的 assistant）
            flush()
            fixed.append(msg)

        flush()
        return fixed
