"""真实飞书适配器（WebSocket 长连接 · 模式 B：一专家一机器人）。

职责：
- 每个专家绑定独立的飞书自建应用（App ID / App Secret），在通讯录中
  作为独立联系人出现。用户直接 @ 对应专家机器人即可，无需选择。
- 为每个已配置飞书凭证的专家启动一条 WebSocket 长连接。
- 收到 `im.message.receive_v1`：解析用户消息 → 直接路由到该专家的
  Hermes 执行 → 回传结果 + 审计。
- 群聊需 @机器人 才响应；白名单可限制可见用户。

执行链路：飞书消息 → 路由到该连接绑定的专家 → HermesExecutor
（DeepSeek function calling）→ 回复。DeepSeek 调用耗时较长，超过飞书
事件 3 秒 ACK 限制，故重活下到线程池异步处理，事件处理器立即返回。
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
    PatchMessageRequest,
    PatchMessageRequestBody,
    P2ImMessageReceiveV1,
)

from .database import SessionLocal
from .models import Expert, CallLog
from .services import HermesExecutor
from . import settings_store


# ====== Monkey-patch: 修复 lark-oapi CARD 帧静默丢弃 bug ======
# Issue: https://github.com/larksuite/oapi-sdk-python/issues/126
# 保留此补丁以备卡片帧到达时不致静默丢弃。
def _patch_card_frame_bug():
    import http as _http
    import time as _time
    import base64 as _b64
    import lark_oapi.ws.client as _ws_mod
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


class _ExpertConnection:
    """单个专家的飞书 WS 连接上下文。"""

    def __init__(self, expert_id: int, expert_name: str, app_id: str, app_secret: str):
        self.expert_id = expert_id
        self.expert_name = expert_name
        self.app_id = app_id
        self.app_secret = app_secret
        self.ws_client = None
        self.api_client: Optional[lark.Client] = None
        self.running = False
        self.error = ""
        self.last_event_at: Optional[str] = None


class FeishuAdapter:
    """飞书 WebSocket 长连接适配器（模式 B：一专家一机器人）。

    为每个配置了飞书 App 凭证的专家启动独立 WS 连接，消息直接路由到
    绑定的专家，无需选择卡片。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="feishu-run")
        self._connections: dict[int, _ExpertConnection] = {}
        self._allowed_users: str = ""
        self.error = ""
        # 所有 WS 客户端共享一个事件循环 / 一个后台线程
        self._ws_thread: Optional[threading.Thread] = None

    # ---------------- 生命周期 ----------------
    def start(self) -> str:
        """加载所有已配置飞书凭证的专家，构建 WS 客户端并在共享循环中启动。"""
        with self._lock:
            db = SessionLocal()
            try:
                s = settings_store.load(db)
                enabled = str(s.get("feishu_enabled", "false")).lower() == "true"
                self._allowed_users = s.get("feishu_allowed_users", "")
                if not enabled:
                    return "飞书未启用（设置中 feishu_enabled=false）"
                experts = (
                    db.query(Expert)
                    .filter(Expert.feishu_app_id.isnot(None),
                            Expert.feishu_app_secret.isnot(None),
                            Expert.status == "active")
                    .all()
                )
            finally:
                db.close()

            if not experts:
                self.error = "没有专家配置飞书 App 凭证"
                return self.error

            new_conns: list[_ExpertConnection] = []
            for e in experts:
                app_id = (e.feishu_app_id or "").strip()
                app_secret = (e.feishu_app_secret or "").strip()
                if not app_id or not app_secret:
                    continue
                # 已在运行则跳过
                existing = self._connections.get(e.id)
                if existing and existing.running:
                    continue
                conn = _ExpertConnection(e.id, e.name, app_id, app_secret)
                self._connections[e.id] = conn
                self._build_client(conn)
                new_conns.append(conn)

            if not new_conns:
                self.error = "所有专家连接已在运行或凭证不完整"
                return self.error

            # 把新连接的客户端交给共享 WS 线程统一驱动
            self._ensure_ws_thread(new_conns)

            self.error = ""
            return f"已启动 {len(new_conns)} 个专家连接"

    def _build_client(self, conn: _ExpertConnection):
        """为单个专家构建 api_client / ws_client（不启动线程）。"""
        conn.api_client = (
            lark.Client.builder()
            .app_id(conn.app_id).app_secret(conn.app_secret).build()
        )

        def _msg_handler(data: P2ImMessageReceiveV1) -> None:
            self._on_message(conn, data)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_msg_handler)
            .build()
        )
        conn.ws_client = lark.ws.Client(
            conn.app_id, conn.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.DEBUG,
            auto_reconnect=True,
        )
        conn.running = True

    def _ensure_ws_thread(self, new_conns: list[_ExpertConnection]):
        """确保共享 WS 线程在运行，并把新客户端挂上去。

        关键：lark_oapi.ws.client 模块有一个全局 `loop` 变量，所有 Client
        内部方法（_connect / _receive_message_loop / _ping_loop 等）都引用它。
        若每个 client 各开一个线程各设一个 loop，会互相覆盖全局 loop，导致
        "Future attached to a different loop"。
        修复：所有 client 共享一个事件循环 + 一个后台线程，全局 loop 只设一次。
        """
        import asyncio

        def _run_all():
            import lark_oapi.ws.client as ws_mod
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            ws_mod.loop = loop  # 全局只设一次
            try:
                # 把当前所有连接的 client 都 connect 到同一个 loop
                for c in list(self._connections.values()):
                    if c.ws_client is None:
                        continue
                    try:
                        loop.run_until_complete(c.ws_client._connect())
                        loop.create_task(c.ws_client._ping_loop())
                    except Exception as e:  # noqa: BLE001
                        c.running = False
                        c.error = f"WS 连接异常: {e}"
                loop.run_forever()
            except Exception as e:  # noqa: BLE001
                for c in self._connections.values():
                    c.running = False
                    c.error = f"WS 共享循环异常: {e}"

        if self._ws_thread is None or not self._ws_thread.is_alive():
            self._ws_thread = threading.Thread(
                target=_run_all, name="feishu-ws-shared", daemon=True)
            self._ws_thread.start()
        else:
            # 线程已在跑：把新 client 也 connect 到现有 loop
            # （需要在 loop 线程里执行，用 run_coroutine_threadsafe）
            import lark_oapi.ws.client as ws_mod
            loop = ws_mod.loop

            async def _connect_new():
                for c in new_conns:
                    if c.ws_client is None:
                        continue
                    try:
                        await c.ws_client._connect()
                        loop.create_task(c.ws_client._ping_loop())
                    except Exception as e:  # noqa: BLE001
                        c.running = False
                        c.error = f"WS 连接异常: {e}"

            asyncio.run_coroutine_threadsafe(_connect_new(), loop)

    def stop(self) -> str:
        """停止所有连接（daemon 线程无法硬终止，标记不可用）。"""
        with self._lock:
            for conn in self._connections.values():
                conn.running = False
                conn.error = "已手动停止"
            return f"已停止 {len(self._connections)} 个连接"

    def restart(self) -> str:
        self.stop()
        time.sleep(0.5)
        # 清理旧连接引用，start 会重建
        self._connections.clear()
        # 共享 WS 线程为 daemon 无法硬终止，重置引用让 start 新建一个 loop
        self._ws_thread = None
        return self.start()

    def status(self) -> dict:
        conns = []
        any_running = False
        last_event = None
        for conn in self._connections.values():
            if conn.running:
                any_running = True
            if conn.last_event_at:
                if not last_event or conn.last_event_at > last_event:
                    last_event = conn.last_event_at
            conns.append({
                "expert_id": conn.expert_id,
                "expert_name": conn.expert_name,
                "app_id": settings_store.mask_key(conn.app_id) if conn.app_id else "",
                "configured": bool(conn.app_id and conn.app_secret),
                "running": conn.running,
                "error": conn.error,
                "last_event_at": conn.last_event_at,
            })
        db = SessionLocal()
        try:
            s = settings_store.load(db)
            enabled = str(s.get("feishu_enabled", "false")).lower() == "true"
        finally:
            db.close()
        return {
            "enabled": enabled,
            "running": any_running,
            "connections": conns,
            "error": self.error,
            "last_event_at": last_event,
        }

    # ---------------- 事件处理 ----------------
    def _on_message(self, conn: _ExpertConnection, data: P2ImMessageReceiveV1) -> None:
        """接收消息事件 → 直接路由到该连接绑定的专家。立即 ACK，重活下到线程池。"""
        conn.last_event_at = dt.datetime.now().isoformat(timespec="seconds")
        try:
            msg = data.event.message
            sender_open_id = data.event.sender.sender_id.open_id
            chat_type = msg.chat_type
            msg_type = msg.message_type
            message_id = msg.message_id
            mentions = getattr(msg, "mentions", None) or []

            # 白名单
            if self._allowed_users:
                allowed_set = {x.strip() for x in self._allowed_users.split(",") if x.strip()}
                if sender_open_id not in allowed_set:
                    return

            # 群聊必须 @机器人
            if chat_type == "group" and not mentions:
                return

            if msg_type != "text":
                self._reply(conn, message_id, "目前仅支持文本消息，请直接发送任务。")
                return

            try:
                text = json.loads(msg.content).get("text", "")
            except Exception:
                text = ""
            text = re.sub(r"@_user_\d+", "", text).strip()
            if not text:
                return

            self._executor.submit(self._run_and_reply, conn, text, message_id, sender_open_id)
        except Exception as e:  # noqa: BLE001
            conn.error = f"消息处理异常: {e}"

    # ---------------- 执行 ----------------
    def _run_and_reply(self, conn: _ExpertConnection, task: str, message_id: str, open_id: str):
        """在后台线程执行：加载专家 → 调用 HermesExecutor → 回传 + 审计。

        实时展示推理过程：发送一张进度卡片，随 Agent 循环逐步更新
        （迭代开始 / 工具调用 / 工具返回 / 最终答复），让用户像使用
        其他 Agent 一样看到思考与工具调用链路。
        """
        db = SessionLocal()
        try:
            settings = settings_store.load(db)
            expert = db.get(Expert, conn.expert_id)
            if not expert or expert.status != "active":
                self._reply(conn, message_id, "该专家已下线，请联系管理员。")
                return

            # 1) 发送进度卡片，拿到 card_message_id 用于后续 PATCH 更新
            card_msg_id = self._send_progress_card(
                conn, message_id, expert.name,
                lines=["🔄 正在分析任务，准备调用工具…"],
            )

            # 2) 构建进度回调：累积步骤并实时 PATCH 卡片
            trace_lines: list[str] = []
            tool_count = [0]

            def on_progress(step: dict) -> None:
                t = step.get("type")
                if t == "iter_start":
                    trace_lines.append(f"🧠 第 {step['iteration']} 轮推理（{step.get('model', '')}）…")
                elif t == "tool_call":
                    tool_count[0] += 1
                    name = step.get("name", "")
                    args = step.get("args", {})
                    args_str = self._truncate(json.dumps(args, ensure_ascii=False), 120)
                    trace_lines.append(f"🔧 [{tool_count[0]}] 调用工具 `{name}`\n   参数: {args_str}")
                elif t == "tool_result":
                    name = step.get("name", "")
                    result = step.get("result")
                    result_str = self._truncate(json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result, 200)
                    trace_lines.append(f"   ↳ 返回: {result_str}")
                # 实时刷新卡片（final 由 _send_result 统一处理最终态）
                if card_msg_id and t != "final":
                    self._patch_card(conn, card_msg_id, expert.name, trace_lines, done=False)

            # 3) 执行（进度回调会实时更新卡片）
            result = HermesExecutor.run(expert, task, settings=settings, on_progress=on_progress)

            # 4) 审计入库
            log = CallLog(
                expert_id=expert.id, user_open_id=open_id, channel="feishu",
                message=task, response=result["response"],
                skill_used=result["skill_used"], mcp_used=result["mcp_used"],
                tokens=result["tokens"], latency_ms=result["latency_ms"],
                status=result["provider"],
            )
            db.add(log)
            db.commit()

            # 5) 把最终结果（含完整工具/Skill 链路）PATCH 到卡片
            self._send_result(conn, card_msg_id, message_id, expert, result, trace_lines)
        except Exception as e:  # noqa: BLE001
            try:
                self._reply(conn, message_id, f"处理失败：{e}")
            except Exception:
                pass
        finally:
            db.close()

    # ---------------- 回复发送 ----------------
    def _reply(self, conn: _ExpertConnection, message_id: str, text: str) -> Optional[str]:
        """用该专家的 api_client 回复消息，返回新消息的 message_id（失败返回 None）。"""
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            ).build()
        )
        resp = conn.api_client.im.v1.message.reply(req)
        if not resp.success():
            conn.error = f"reply 失败: code={resp.code} msg={resp.msg}"
            return None
        return getattr(resp.data, "message_id", None)

    def _reply_card(self, conn: _ExpertConnection, message_id: str, card: dict) -> Optional[str]:
        """回复一条交互卡片，返回新消息的 message_id。"""
        content = json.dumps(card, ensure_ascii=False)
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(content)
                .build()
            ).build()
        )
        resp = conn.api_client.im.v1.message.reply(req)
        if not resp.success():
            conn.error = f"reply card 失败: code={resp.code} msg={resp.msg}"
            print(f"[feishu] reply card 失败: code={resp.code} msg={resp.msg}", flush=True)
            return None
        return getattr(resp.data, "message_id", None)

    def _patch_card(self, conn: _ExpertConnection, card_msg_id: str, expert_name: str,
                    lines: list[str], done: bool = False, response: str = "",
                    meta: str = "", skill_used: str = "", mcp_used: str = "",
                    tool_calls: list | None = None) -> None:
        """PATCH 更新一张交互卡片的内容（实时进度 / 最终结果）。"""
        if not card_msg_id:
            return
        if done:
            template = "green"
            title = f"✅ {expert_name} 已完成"
        else:
            template = "blue"
            title = f"🤖 {expert_name} 思考中…"

        elements: list[dict] = []
        # 进度/追踪区
        if lines:
            trace_text = "\n".join(lines)
            elements.append({"tag": "markdown", "content": trace_text})

        if done:
            # 元信息
            if meta:
                elements.append({"tag": "hr"})
                elements.append({"tag": "markdown", "content": f"**📊 执行信息**\n{meta}"})
            # 已加载 Skill / MCP
            if skill_used or mcp_used:
                skills_text = f"**🧩 已加载 Skill**\n{skill_used or '(无)'}"
                mcp_text = f"**🔌 已加载 MCP**\n{mcp_used or '(无)'}"
                elements.append({"tag": "markdown", "content": f"{skills_text}\n\n{mcp_text}"})
            # 完整工具调用链路（带参数与返回）
            if tool_calls:
                tc_lines = ["**🛠 工具调用链路**"]
                for i, tc in enumerate(tool_calls, 1):
                    name = tc.get("name", "")
                    args = tc.get("args", {})
                    result = tc.get("result")
                    args_str = self._truncate(json.dumps(args, ensure_ascii=False), 300)
                    result_str = self._truncate(
                        json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result, 400
                    )
                    tc_lines.append(f"`{i}. {name}`")
                    tc_lines.append(f"   参数: `{args_str}`")
                    tc_lines.append(f"   返回: `{result_str}`")
                elements.append({"tag": "markdown", "content": "\n".join(tc_lines)})
            # 最终答复
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": f"**💬 最终答复**\n{response}"})

        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": elements,
        }
        content = json.dumps(card, ensure_ascii=False)
        self._patch_message(conn, card_msg_id, content)

    def _patch_message(self, conn: _ExpertConnection, message_id: str, content: str) -> None:
        """PATCH 更新一条消息的 content（用于实时刷新进度卡片）。"""
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content)
                .build()
            ).build()
        )
        resp = conn.api_client.im.v1.message.patch(req)
        if not resp.success():
            conn.error = f"patch 失败: code={resp.code} msg={resp.msg}"

    @staticmethod
    def _truncate(s: str, max_len: int) -> str:
        s = s.replace("\n", " ")
        return s if len(s) <= max_len else s[:max_len] + "…"

    def _send_progress_card(self, conn: _ExpertConnection, message_id: str, expert_name: str,
                            lines: list[str]) -> Optional[str]:
        """发送初始进度卡片，返回其 message_id（用于后续 PATCH）。"""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🤖 {expert_name} 思考中…"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
            ],
        }
        return self._reply_card(conn, message_id, card)

    def _send_result(self, conn: _ExpertConnection, card_msg_id: Optional[str],
                     message_id: str, expert, result: dict, trace_lines: list[str]):
        """回传执行结果：优先 PATCH 进度卡片展示完整链路；超长则额外补发文本。"""
        prov = result.get("provider", "mock")
        tag = {"deepseek": "真实 DeepSeek", "mock": "模拟回显",
               "deepseek-error": "DeepSeek 失败"}.get(prov, prov)
        meta = (f"[{tag}] 模型={result.get('model') or expert.model} "
                f"tokens={result['tokens']} latency={result['latency_ms']}ms")
        tool_calls = result.get("tool_calls", [])

        # 1) 把最终结果 PATCH 到进度卡片（含完整工具/Skill 链路）
        if card_msg_id:
            self._patch_card(
                conn, card_msg_id, expert.name, trace_lines,
                done=True,
                response=result["response"],
                meta=meta,
                skill_used=result.get("skill_used", ""),
                mcp_used=result.get("mcp_used", ""),
                tool_calls=tool_calls,
            )

        # 2) 若最终答复过长，卡片放不下，则另外补发一条文本（带引用）
        response = result["response"]
        if len(response) > 2000:
            MAX = 3800
            body = f"{meta}\n\n{response}"
            if len(body) <= MAX:
                self._reply(conn, message_id, body)
            else:
                self._reply(conn, message_id, body[:MAX] + "\n\n（续见下条）")
                rest = body[MAX:]
                for i in range(0, len(rest), MAX):
                    self._reply(conn, message_id, rest[i:i + MAX])


# 单例
_adapter: Optional[FeishuAdapter] = None
_adapter_lock = threading.Lock()


def get_adapter() -> FeishuAdapter:
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            _adapter = FeishuAdapter()
        return _adapter
