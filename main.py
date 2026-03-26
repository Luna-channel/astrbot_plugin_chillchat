from astrbot import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import on_llm_request
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register("chillchat", "柯尔", "上下文字符数裁剪插件（多档位）", "1.1.1")
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

    @on_llm_request()
    async def trim_context(self, event: AstrMessageEvent, request: ProviderRequest):
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
            
            # 确定该用户/群适用的档位上限（优先级：三档 > 二档 > 默认）
            effective_max = self._resolve_tier(sender_id, group_id)
            
            # 计算历史记录总字符数
            total_chars = self._calc_context_chars(request.contexts)
            
            # 如果未超限，直接返回
            if total_chars <= effective_max:
                return
            
            # 裁剪历史记录，保留至少最新的一条
            original_count = len(request.contexts)
            while total_chars > effective_max and len(request.contexts) > 1:
                removed = request.contexts.pop(0)
                # 如果删除的是带 tool_calls 的 assistant 消息，
                # 必须同时删除紧跟其后的所有 tool 响应消息，避免孤立
                if (isinstance(removed, dict)
                        and removed.get("role") == "assistant"
                        and removed.get("tool_calls")):
                    while (request.contexts
                           and isinstance(request.contexts[0], dict)
                           and request.contexts[0].get("role") == "tool"
                           and len(request.contexts) > 1):
                        request.contexts.pop(0)
                total_chars = self._calc_context_chars(request.contexts)
            
            # 清理开头残留的孤立 tool 消息（没有对应的 assistant）
            while (len(request.contexts) > 1
                   and isinstance(request.contexts[0], dict)
                   and request.contexts[0].get("role") == "tool"):
                request.contexts.pop(0)
            
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
