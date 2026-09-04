"""飞书 WebSocket 长连接诊断脚本。

用法：
  python test_feishu_ws.py <app_id> <app_secret>

作用：
  用 lark-oapi 官方 SDK 建立长连接并打印日志。
  连接成功后保持运行，此时去飞书开放平台点「验证」即可通过。

注意：
  此脚本会阻塞前台运行，Ctrl+C 退出。连接成功后请保持运行，
  再去飞书开放平台「事件订阅」页面点「验证」按钮。
"""
import sys
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)


def on_msg(data: P2ImMessageReceiveV1):
    print(f"[收到消息] message_id={data.event.message.message_id}")


def on_card(data: P2CardActionTrigger):
    print(f"[卡片回调] value={data.event.action.value if data.event.action else None}")
    return P2CardActionTriggerResponse()


def main():
    if len(sys.argv) < 3:
        print("用法: python test_feishu_ws.py <app_id> <app_secret>")
        print("示例: python test_feishu_ws.py cli_xxxx xxxxxxxx")
        sys.exit(1)
    app_id, app_secret = sys.argv[1], sys.argv[2]
    print(f"App ID: {app_id}")
    print("正在建立 WebSocket 长连接...")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_msg)
        .register_p2_card_action_trigger(on_card)
        .build()
    )
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.DEBUG,   # DEBUG 日志，方便排查
        auto_reconnect=True,
    )
    print("长连接已启动，正在监听事件...")
    print(">>> 现在去飞书开放平台「事件订阅」页面点「验证」按钮 <<<")
    print(">>> 验证通过后，Ctrl+C 可退出本脚本 <<<")
    client.start()


if __name__ == "__main__":
    main()
