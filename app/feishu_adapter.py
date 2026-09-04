"""真实飞书适配器（WebSocket 长连接模式）。

职责（对应技术方案 §7）：
- 与飞书开放平台建立 WebSocket 长连接（无需公网域名/内网穿透，本地即可接收事件）。
- 收到 `im.message.receive_v1`：解析用户消息 → 路由到所选专家的 Hermes 执行 → 回传结果。
- 提供「专家选择卡片」（interactive card），按钮回调走 `card.action.trigger` 事件。
- 会话按 open_id 隔离，记住用户上次选择的专家。
- 群聊需 @机器人 才响应；白名单可限制可见用户。

执行链路：飞书消息 → 路由 → HermesExecutor（DeepSeek function calling）→ 回复。
DeepSeek 调用耗时较长，超过飞书事件 3 秒 ACK 限制，故重活下到线程池异步处理，
事件处理器立即返回，避免触发超时重推。

飞书 App 接入步骤见 README / 末尾注释。
"""
from __future__ import annotations

import json
import re
import threading
import time
import datetime as dt
import concurrent.futures
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from .database import SessionLocal
from .models import Expert
from .services import HermesExecutor
from . import settings_store


# ====== Monkey-patch: 修复 lark-oapi CARD 帧静默丢弃 bug ======
# Issue: https://github.com/larksuite/oapi-sdk-python/issues/126
# lark-oapi <= 1.7.3 的 ws.client._handle_data_frame 对 MessageType.CARD
# 直接 return，导致 register_p2_card_action_trigger 回调永远不触发。
# 修复：CARD 帧走和 EVENT 帧相同的分发路径。
def _patch_card_frame_bug():
    import http as _http
    import time as _time
    import base64 as _b64
    import lark_oapi.ws.client as _ws_mod
    # 这些常量已在 ws.client 模块作用域内，直接引用
    _MessageType = _ws_mod.MessageType
    _Response = _ws_mod.Response
    _JSON = _ws_mod.JSON
    _UTF_8 = _ws_mod.UTF_8
    _logger = _ws_mod.logger
    _HEADER_MESSAGE_ID = _ws_mod.HEADER_MESSAGE_ID
    _HEADER_TRACE_ID = _ws_mod.HEADER_TRACE_ID
    _HEADER_SUM = _ws_mod.HEADER_SUM
    _HEADER_SEQ = _ws_mod.HEADER_SEQ
    _HEADER_TYPE = _ws_mod.HEADER_TYPE
    _HEADER_BIZ_RT = _ws_mod.HEADER_BIZ_RT

    def _get_by_key(headers, key):
        for h in headers:
            if h.key == key:
                return h.value
        from lark_oapi.ws.exception import HeaderNotFoundException
        raise HeaderNotFoundException(key)

    async def _handle_data_frame(self, frame):
        hs = frame.headers
        msg_id = _get_by_key(hs, _HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, _HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, _HEADER_SUM)
        seq = _get_by_key(hs, _HEADER_SEQ)
        type_ = _get_by_key(hs, _HEADER_TYPE)
        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return
        message_type = _MessageType(type_)
        _logger.debug(self._fmt_log(
            "receive message, message_type: {}, message_id: {}, trace_id: {}, payload: {}",
            message_type.value, msg_id, trace_id, pl.decode(_UTF_8)))
        resp = _Response(code=_http.HTTPStatus.OK)
        try:
            start = int(round(_time.time() * 1000))
            # FIX: CARD 帧和 EVENT 帧走相同分发路径（原版 CARD 分支直接 return）
            if message_type in (_MessageType.EVENT, _MessageType.CARD):
                result = self._event_handler._do_without_validation(pl)
            else:
                return
            end = int(round(_time.time() * 1000))
            header = hs.add()
            header.key = _HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = _b64.b64encode(_JSON.marshal(result).encode(_UTF_8))
        except Exception as e:
            _logger.error(self._fmt_log(
                "handle message failed, message_type: {}, message_id: {}, trace_id: {}, err: {}",
                message_type.value, msg_id, trace_id, e))
            resp = _Response(code=_http.HTTPStatus.INTERNAL_SERVER_ERROR)
        frame.payload = _JSON.marshal(resp).encode(_UTF_8)
        await self._write_message(frame.SerializeToString())

    _ws_mod.Client._handle_data_frame = _handle_data_frame

_patch_card_frame_bug()


class FeishuAdapter:
    """飞书 WebSocket 长连接适配器（单例）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._ws_client = None
        self._api_client: Optional[lark.Client] = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu-run")
        # 会话记忆：open_id -> expert_id（演示用内存；生产应入 session 表 / Redis）
        self._sessions: dict[str, int] = {}
        self._sessions_lock = threading.Lock()
        # 状态
        self.running = False
        self.error = ""
        self.last_event_at: Optional[str] = None
        self._creds: dict = {}

    # ---------------- 生命周期 ----------------
    def start(self) -> str:
        """读取当前配置并启动 WS 长连接（后台线程）。"""
        with self._lock:
            if self.running:
                return "已在运行中"
            db = SessionLocal()
            try:
                s = settings_store.load(db)
            finally:
                db.close()
            app_id = (s.get("feishu_app_id") or "").strip()
            app_secret = (s.get("feishu_app_secret") or "").strip()
            enabled = str(s.get("feishu_enabled", "false")).lower() == "true"
            self._creds = {"allowed_users": s.get("feishu_allowed_users", "")}
            if not enabled:
                return "飞书未启用（设置中 feishu_enabled=false）"
            if not app_id or not app_secret:
                self.error = "缺少 App ID 或 App Secret"
                return self.error
            self.error = ""
            self._api_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_message)
                .register_p2_card_action_trigger(self._on_card_action)
                .build()
            )
            self._ws_client = lark.ws.Client(
                app_id, app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.DEBUG,
                auto_reconnect=True,
            )
            self._thread = threading.Thread(target=self._run, name="feishu-ws", daemon=True)
            self.running = True
            self._thread.start()
            return "启动中"

    def _run(self):
        """daemon 线程入口：为 SDK 建立独立事件循环后启动 WS 长连接。

        lark-oapi 的 ws.client 模块在 import 时用 asyncio.get_event_loop()
        捕获了一个全局 loop。FastAPI/uvicorn 在主线程跑 asyncio，该 loop
        已在运行，daemon 线程里再 loop.run_until_complete() 就会抛
        'This event loop is already running'。
        修复：在 daemon 线程里新建一个事件循环，覆盖 SDK 的全局 loop。
        """
        import asyncio
        import lark_oapi.ws.client as ws_mod
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            ws_mod.loop = new_loop  # 覆盖 SDK 模块级全局 loop
            self._ws_client.start()
        except Exception as e:  # noqa: BLE001
            self.running = False
            self.error = f"WS 连接异常: {e}"

    def stop(self) -> str:
        """尽力停止：标记不可用。WS 线程为 daemon，无法硬终止；改凭证需重启服务。"""
        with self._lock:
            self.running = False
            self.error = "已手动停止（WS 线程为 daemon，可能仍存活至下次重连失败）"
            return "已停止"

    def restart(self) -> str:
        self.stop()
        time.sleep(0.5)
        return self.start()

    def status(self) -> dict:
        db = SessionLocal()
        try:
            s = settings_store.load(db)
        finally:
            db.close()
        app_id = (s.get("feishu_app_id") or "").strip()
        app_secret = (s.get("feishu_app_secret") or "").strip()
        return {
            "configured": bool(app_id and app_secret),
            "enabled": str(s.get("feishu_enabled", "false")).lower() == "true",
            "running": self.running,
            "app_id": settings_store.mask_key(app_id) if app_id else "",
            "error": self.error,
            "last_event_at": self.last_event_at,
        }

    # ---------------- 事件处理 ----------------
    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """接收消息事件。立即 ACK，重活下到线程池。"""
        self.last_event_at = dt.datetime.now().isoformat(timespec="seconds")
        try:
            msg = data.event.message
            sender_open_id = data.event.sender.sender_id.open_id
            chat_type = msg.chat_type            # p2p / group
            msg_type = msg.message_type
            message_id = msg.message_id
            chat_id = msg.chat_id
            mentions = getattr(msg, "mentions", None) or []

            # 白名单
            allowed = self._creds.get("allowed_users", "")
            if allowed:
                allowed_set = {x.strip() for x in allowed.split(",") if x.strip()}
                if sender_open_id not in allowed_set:
                    return

            # 群聊必须 @ 机器人（提及列表非空）；单聊直接处理
            if chat_type == "group" and not mentions:
                return

            if msg_type != "text":
                self._reply(message_id, "目前仅支持文本消息。发送「专家」查看可用专家。")
                return

            try:
                text = json.loads(msg.content).get("text", "")
            except Exception:
                text = ""
            text = text.strip()
            # 去掉群聊 @机器人 的占位符（@_user_1 等）
            text = re.sub(r"@_user_\d+", "", text).strip()

            self._executor.submit(self._handle_text, sender_open_id, chat_type, message_id, chat_id, text)
        except Exception as e:  # noqa: BLE001
            self.error = f"消息处理异常: {e}"

    def _handle_text(self, open_id: str, chat_type: str, message_id: str, chat_id: str, text: str):
        """在后台线程执行：路由 → 调用专家 → 回复。"""
        try:
            # 路由解析
            # 1) 触发专家选择卡片 + 文本列表
            if text in {"专家", "选专家", "专家列表", "切换专家", "切换", "help", "帮助", "?"}:
                self._send_expert_selector(chat_type, chat_id, open_id, message_id)
                return
            # 2) 纯数字 = 按编号选专家（卡片按钮的文本兜底）
            if re.match(r"^\d+$", text):
                expert = self._find_expert(text)
                if expert:
                    self._set_session(open_id, expert.id)
                    self._reply(message_id, f"已选择专家：{expert.name}\n现在发送任务即可，"
                                             f"或用「用 {expert.name}：你的任务」直接执行。")
                    return
            # 3) 指令路由：用 <专家名|编号> : <任务>
            m = re.match(r"^用\s*(.+?)[：:]\s+(.+)$", text)
            if m:
                key, task = m.group(1).strip(), m.group(2).strip()
                expert = self._find_expert(key)
                if not expert:
                    self._reply(message_id, f"未找到专家「{key}」。发送「专家」查看列表。")
                    return
                self._set_session(open_id, expert.id)
                self._run_and_reply(expert, task, message_id, open_id)
                return
            # 4) 凭会话记忆执行
            expert_id = self._get_session(open_id)
            if not expert_id:
                self._send_expert_selector(chat_type, chat_id, open_id, message_id,
                                           hint="请先选择专家，再发送任务。")
                return
            expert = self._load_expert(expert_id)
            if not expert or expert.status != "active" or not expert.feishu_visible:
                self._clear_session(open_id)
                self._send_expert_selector(chat_type, chat_id, open_id, message_id,
                                           hint="你选的专家已下线，请重新选择。")
                return
            self._run_and_reply(expert, text, message_id, open_id)
        except Exception as e:  # noqa: BLE001
            try:
                self._reply(message_id, f"处理失败：{e}")
            except Exception:
                pass

    def _on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """专家选择卡片按钮回调。"""
        self.last_event_at = dt.datetime.now().isoformat(timespec="seconds")
        try:
            action = data.event.action
            value = (action.value if action else {}) or {}
            op_open_id = data.event.operator.open_id if data.event.operator else ""
            expert_id = int(value.get("expert_id", 0))
            name = self._expert_name(expert_id)
            if expert_id and name:
                self._set_session(op_open_id, expert_id)
                toast = {"type": "success", "content": {"tag": "plain_text", "content": f"已选择：{name}\n现在发送任务即可"}}
            else:
                toast = {"type": "error", "content": {"tag": "plain_text", "content": "选择失败：专家不存在"}}
            resp = P2CardActionTriggerResponse()
            resp.toast = toast
            return resp
        except Exception as e:  # noqa: BLE001
            self.error = f"卡片回调异常: {e}"
            return P2CardActionTriggerResponse()

    # ---------------- 执行 ----------------
    def _run_and_reply(self, expert, task: str, message_id: str, open_id: str):
        db = SessionLocal()
        try:
            settings = settings_store.load(db)
            # 用当前 session 重新加载 Expert，避免 detached instance 错误
            # （expert 是在别的已关闭 session 里加载的，skills/mcp_servers 等懒加载属性无法访问）
            expert = db.get(Expert, expert.id)
            if not expert or expert.status != "active" or not expert.feishu_visible:
                self._reply(message_id, "专家已下线或不可用，请重新选择。")
                return
            # 触发前回执（typing 提示）
            self._reply(message_id, f"已收到，{expert.name} 正在处理…")
            result = HermesExecutor.run(expert, task, settings=settings)
            # 审计入库
            from .models import CallLog
            log = CallLog(
                expert_id=expert.id, user_open_id=open_id, channel="feishu",
                message=task, response=result["response"],
                skill_used=result["skill_used"], mcp_used=result["mcp_used"],
                tokens=result["tokens"], latency_ms=result["latency_ms"],
                status=result["provider"],
            )
            db.add(log)
            db.commit()
            self._send_result(message_id, expert, result)
        finally:
            db.close()

    # ---------------- 回复发送 ----------------
    def _reply(self, message_id: str, text: str):
        """回复某条消息（带引用）。"""
        self._reply_raw(message_id, "text", json.dumps({"text": text}, ensure_ascii=False))

    def _reply_raw(self, message_id: str, msg_type: str, content: str):
        """回复某条消息，支持 text/interactive 等类型。"""
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .build()
            ).build()
        )
        resp = self._api_client.im.v1.message.reply(req)
        if not resp.success():
            self.error = f"reply({msg_type}) 失败: code={resp.code} msg={resp.msg}"
            print(f"[feishu] reply({msg_type}) 失败: code={resp.code} msg={resp.msg}", flush=True)

    def _send(self, chat_id: str, open_id: str, msg_type: str, content: str):
        """主动发消息：单聊用 open_id，群聊用 chat_id。"""
        if chat_id and not open_id:
            rid_type, rid = "chat_id", chat_id
        else:
            rid_type, rid = "open_id", open_id
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(rid_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(rid)
                .msg_type(msg_type)
                .content(content)
                .build()
            ).build()
        )
        resp = self._api_client.im.v1.message.create(req)
        if not resp.success():
            self.error = f"create({msg_type}) 失败: code={resp.code} msg={resp.msg}"

    def _send_result(self, message_id: str, expert, result: dict):
        """把执行结果回传：用回复消息（带引用）发文本结果。"""
        prov = result.get("provider", "mock")
        tag = {"deepseek": "真实 DeepSeek", "mock": "模拟回显", "deepseek-error": "DeepSeek 失败"}.get(prov, prov)
        meta = f"[{tag}] 模型={result.get('model') or expert.model} tokens={result['tokens']} latency={result['latency_ms']}ms"
        body = f"{meta}\n\n{result['response']}"
        # 飞书单条文本上限较大，超长则分段
        MAX = 3800
        if len(body) <= MAX:
            self._reply(message_id, body)
        else:
            self._reply(message_id, body[:MAX] + "\n\n（续见下条）")
            rest = body[MAX:]
            for i in range(0, len(rest), MAX):
                self._reply(message_id, rest[i:i + MAX])

    def _send_expert_selector(self, chat_type: str, chat_id: str, open_id: str, message_id: str, hint: str = "请选择一个专家："):
        """发送专家选择交互卡片 + 文本编号列表（双通道：卡片按钮 + 文本数字选择）。"""
        db = SessionLocal()
        try:
            qs = (db.query(Expert)
                  .filter(Expert.feishu_visible.is_(True), Expert.status == "active")
                  .order_by(Expert.id.asc()).all())
        finally:
            db.close()
        if not qs:
            self._reply(message_id, "暂无可用专家。请联系管理员在后台配置。")
            return
        # 先发一条文本编号列表（不依赖卡片回调，兜底可用）
        lines = [hint, ""]
        for i, e in enumerate(qs, 1):
            lines.append(f"  {i}. {e.name}（#{e.id}）")
        lines.append("")
        lines.append("👉 点击下方卡片按钮，或直接回复数字（如 1）选择专家")
        lines.append("👉 或发送：用 <专家名>：<任务> 一步到位")
        self._reply(message_id, "\n".join(lines))
        # 再发交互卡片（用 reply 通道发送，和文本回复走同一路径，兼容性最佳）
        actions = []
        for e in qs:
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": e.name},
                "type": "primary",
                "value": {"action": "select", "expert_id": e.id},
            })
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "选择专家"}, "template": "blue"},
            "elements": [
                {"tag": "action", "actions": actions},
            ],
        }
        content = json.dumps(card, ensure_ascii=False)
        self._reply_raw(message_id, "interactive", content)

    # ---------------- 会话 / 专家查询 ----------------
    def _set_session(self, open_id: str, expert_id: int):
        with self._sessions_lock:
            self._sessions[open_id] = expert_id

    def _get_session(self, open_id: str) -> Optional[int]:
        with self._sessions_lock:
            return self._sessions.get(open_id)

    def _clear_session(self, open_id: str):
        with self._sessions_lock:
            self._sessions.pop(open_id, None)

    def _find_expert(self, key: str) -> Optional[Expert]:
        db = SessionLocal()
        try:
            # 编号
            if key.isdigit():
                e = db.get(Expert, int(key))
                if e and e.feishu_visible and e.status == "active":
                    return e
                return None
            # 名称包含匹配
            q = db.query(Expert).filter(Expert.feishu_visible.is_(True), Expert.status == "active")
            for e in q:
                if key in e.name or e.name in key:
                    return e
            return q.filter(Expert.name == key).first()
        finally:
            db.close()

    def _load_expert(self, expert_id: int) -> Optional[Expert]:
        db = SessionLocal()
        try:
            return db.get(Expert, expert_id)
        finally:
            db.close()

    def _expert_name(self, expert_id: int) -> str:
        e = self._load_expert(expert_id)
        return e.name if e else ""


# 单例
_adapter: Optional[FeishuAdapter] = None
_adapter_lock = threading.Lock()


def get_adapter() -> FeishuAdapter:
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            _adapter = FeishuAdapter()
        return _adapter


# ====== 飞书 App 接入步骤（管理员在开放平台操作） ======
# 1. https://open.feishu.cn/app 创建「企业自建应用」，拿到 App ID / App Secret。
# 2. 「权限管理」开通：im:message（读消息）、im:message:send_as_bot（发消息）、im:chat:readonly。
# 3. 「事件订阅」选择「长连接（WebSocket）」模式（无需公网回调 URL）。
#    订阅事件：im.message.receive_v1（接收消息）、card.action.trigger（卡片按钮回调）。
# 4. 「机器人」配置：启用机器人，可设头像/名称。
# 5. 「版本管理与发布」创建版本并发布（自建应用需发布后事件才生效）。
# 6. 把 App ID / App Secret 填入本平台「平台设置」→ 保存 → 重启服务（或点「启动连接」）。
# 7. 在飞书里把机器人加入群聊（@机器人），或直接私聊机器人；发「专家」选专家，发任务即可。
