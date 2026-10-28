"""3B-2.3 / P0 —— Delivery & ACK 可观测性（**纯观测；零语义变更**）。

背景：生产实测「Agent 已答、用户收不到」的根因位于投递最后一公里——
`aibot_respond_msg` 的 **ACK 等待超时**。而该路径此前**零投递日志**，
ACK 兑现路径（`fulfil_ack`）也不留痕，导致「超时究竟是未送达、还是已送达
但回执丢失」无法从证据判定，也无法测量 ACK 延迟分布。

本文件的覆盖边界（P0 = 只观测 + 配置化）：

    A. outcome_known 语义：默认 True；**仅** ACK 超时为 False（结果未知）。
       与既有 `retryable` **正交**、与既有错误码/消息文本**零变化**。
    B. 八类投递日志：attempt / ack ok / rejected by channel / ack timeout /
       send failed / not sent / rejected before send / late ack observed。
    C. ACK 等待窗口配置化：默认 **10.0s（与既有常量逐字相同）**，可配置，
       非法值启动即失败；组合根（main.py）确实接线。
    D. 迟到 ACK 观测**不破坏**既有 ACK 匹配/发送语义：
       不被误兑现、只记录一次、不改任何状态、不触发任何重投。
    E. 既有语义回归：错误码映射、retryable 裁决、状态持久化行为**未变化**。

明确不在本文件覆盖（属 P1/P2 或其它事项）：重投机制、投递状态机、
`delivery_status` 新取值、任何数据库字段/迁移/新 API。
"""

import json
import logging
import time
from pathlib import Path

import pytest

from app.adapters.channels.wecom_bot import (
    BOT_REPLY_METADATA_KEY,
    DEFAULT_ACK_WAIT_TIMEOUT_S,
    WeComBotAdapter,
    WeComBotConnectionRegistry,
)
from app.application.errors import ChannelDeliveryFailedError
from app.application.interaction_recording import STAGE_FAILURE_MAP
from app.core.config import Settings
from app.domain.outbound_response import OutboundResponse

BACKEND = Path(__file__).resolve().parents[1]
LOGGER_NAME = "app.adapters.channels.wecom_bot"

#: 用于「日志不得泄露消息内容」的探针（出现在内容里，但不应出现在日志里）。
_CONTENT_MARKER = "SECRET-CONTENT-MARKER-XYZ"


def _outbound(*, channel_id: int = 7, req_id: str = "REQ-9",
              content: str = _CONTENT_MARKER) -> OutboundResponse:
    return OutboundResponse(
        channel_id=channel_id,
        external_conversation_id="bot-user-1",
        content=content,
        metadata={BOT_REPLY_METADATA_KEY: {"req_id": req_id}},
    )


def _acking_sender(registry: WeComBotConnectionRegistry, channel_id: int,
                    sent: list[str], *, errcode: int = 0,
                    echo_req_id: bool = True) -> None:
    """模拟 transport 线程：记录发出的帧，并按服务端行为兑现等待槽。"""
    def sender(text: str) -> None:
        sent.append(text)
        req_id = json.loads(text)["headers"]["req_id"]
        registry.fulfil_ack(
            channel_id, req_id if echo_req_id else "SOME-OTHER-REQ",
            {"errcode": errcode, "errmsg": "ok" if errcode == 0 else "rejected"})
    registry.register(channel_id, sender)


def _silent_sender(registry: WeComBotConnectionRegistry, channel_id: int,
                   sent: list[str]) -> None:
    """只记录发送、**永不**回 ACK（模拟 ACK 超时）。"""
    registry.register(channel_id, sent.append)


# ================================================================== A. outcome_known

def test_a1_error_defaults_and_message_unchanged() -> None:
    """默认语义与消息文本**逐字不变**（不新增错误码、不改既有可读性）。"""
    err = ChannelDeliveryFailedError(
        channel_id=7, external_conversation_id="XiangDong")
    assert err.retryable is True
    assert err.outcome_known is True
    assert str(err) == (
        "Channel Delivery 失败（Fail Closed）："
        "channel_id=7, external_conversation_id='XiangDong'")


def test_a2_failure_code_mapping_unchanged() -> None:
    """既有 STAGE_FAILURE_MAP 一行未改（错误码仍是 CHANNEL_DELIVERY_FAILED）。"""
    from app.domain.activity_record import STAGE_DELIVERY

    assert STAGE_FAILURE_MAP[ChannelDeliveryFailedError] == (
        STAGE_DELIVERY, "CHANNEL_DELIVERY_FAILED", True)


def test_a3_ack_timeout_is_unknown_but_retryable_semantics_unchanged(
        caplog) -> None:
    """ACK 超时 = **结果未知**（outcome_known=False）；retryable 仍为既有 True。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _silent_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=0.2)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError) as excinfo:
            adapter.deliver(_outbound())
    err = excinfo.value
    assert err.outcome_known is False          # 结果未知（可能已送达）
    assert err.retryable is True               # 既有语义未改（P0 不改 retryable）
    assert len(sent) == 1                      # 帧确实写出去了


def test_a4_explicit_rejection_is_known_and_not_retryable(caplog) -> None:
    """企微明确拒绝 = 结果**已知**（outcome_known=True）且 retryable=False。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _acking_sender(registry, 7, sent, errcode=60140)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError) as excinfo:
            adapter.deliver(_outbound())
    assert excinfo.value.outcome_known is True
    assert excinfo.value.retryable is False
    assert len(sent) == 1


def test_a5_no_live_connection_is_known_and_retryable(caplog) -> None:
    """无活连接 = 明确**未发出**（结果已知），retryable 仍为既有 True。"""
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError) as excinfo:
            adapter.deliver(_outbound())
    assert excinfo.value.outcome_known is True
    assert excinfo.value.retryable is True


def test_a6_missing_reply_handle_is_known_and_not_retryable(caplog) -> None:
    """缺回信句柄 = 契约类失败（结果已知、不可重试）——既有语义未变。"""
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError) as excinfo:
            adapter.deliver(OutboundResponse(
                channel_id=7, external_conversation_id="bot-user-1", content="hi"))
    assert excinfo.value.outcome_known is True
    assert excinfo.value.retryable is False


def test_a7_outcome_known_is_independent_of_retryable() -> None:
    """两个轴正交：outcome_known=False **不是**"可以安全重试"的依据。"""
    unknown = ChannelDeliveryFailedError(channel_id=1, retryable=False,
                                         outcome_known=False)
    known = ChannelDeliveryFailedError(channel_id=1, retryable=True,
                                       outcome_known=True)
    assert (unknown.retryable, unknown.outcome_known) == (False, False)
    assert (known.retryable, known.outcome_known) == (True, True)


# ================================================================== B. 投递日志

def test_b1_ack_success_logs_attempt_and_ack(caplog) -> None:
    """ACK 成功：attempt + ack ok 两条日志，带 channel_id/req_id/attempt/耗时。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _acking_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        adapter.deliver(_outbound())
    text = caplog.text
    assert "wecom bot delivery attempt" in text
    assert "wecom bot delivery ack ok" in text
    assert "channel_id=7" in text
    assert "req_id=REQ-9" in text
    assert "attempt=1" in text
    assert "errcode=0" in text
    assert "waited_ms=" in text


def test_b2_explicit_rejection_log_carries_errcode(caplog) -> None:
    """明确拒绝：日志带 errcode / errmsg / outcome_known。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _acking_sender(registry, 7, sent, errcode=60140)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver(_outbound())
    text = caplog.text
    assert "wecom bot delivery rejected by channel" in text
    assert "errcode=60140" in text
    assert "outcome_known=True" in text


def test_b3_ack_timeout_log_marks_outcome_unknown(caplog) -> None:
    """ACK 超时：日志显式标记 outcome_known=False（**不**推断未送达）。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _silent_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=0.2)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver(_outbound())
    text = caplog.text
    assert "wecom bot delivery ack timeout" in text
    assert "outcome_known=False" in text
    assert "waited_ms=" in text


def test_b4_send_failure_log(caplog) -> None:
    """发送异常：日志记录异常**类型**（不记录内容）。"""
    registry = WeComBotConnectionRegistry()

    def boom(_text: str) -> None:
        raise RuntimeError("socket gone")

    registry.register(7, boom)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError) as excinfo:
            adapter.deliver(_outbound())
    assert "wecom bot delivery send failed" in caplog.text
    assert "error=RuntimeError" in caplog.text
    assert excinfo.value.retryable is True


def test_b5_no_live_connection_log(caplog) -> None:
    """无活连接：日志说明「未发出」且结果已知。"""
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver(_outbound())
    assert "wecom bot delivery not sent" in caplog.text
    assert "no_live_connection" in caplog.text


def test_b6_contract_class_failures_log_before_send(caplog) -> None:
    """契约类失败（缺句柄 / 空内容 / 非法 outbound）：发送前即留痕。"""
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver(OutboundResponse(
                channel_id=7, external_conversation_id="u", content="hi"))
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver(OutboundResponse(
                channel_id=7, external_conversation_id="u", content="   ",
                metadata={BOT_REPLY_METADATA_KEY: {"req_id": "r"}}))
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver("not-an-outbound")   # type: ignore[arg-type]
    text = caplog.text
    assert "wecom bot delivery rejected before send" in text
    assert "missing_reply_handle" in text
    assert "empty_content" in text
    assert "outbound_not_an_outbound_response" in text


def test_b7_logs_never_contain_message_content(caplog) -> None:
    """日志只带长度与关联 id，**绝不**记录消息正文。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _acking_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        adapter.deliver(_outbound(content=_CONTENT_MARKER))
    assert _CONTENT_MARKER not in caplog.text
    assert "frame_bytes=" in caplog.text


def test_b8_attempt_number_increases_per_delivery(caplog) -> None:
    """进程内单调 attempt 号：两次投递 → attempt=1 / attempt=2（可关联）。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _acking_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        adapter.deliver(_outbound())
        adapter.deliver(_outbound())
    assert "attempt=1" in caplog.text
    assert "attempt=2" in caplog.text


# ================================================================== C. 配置化

def test_c1_ack_wait_default_is_ten_seconds() -> None:
    """默认值仍为 **10 秒**（与既有模块常量逐字相同 → 零行为变化）。"""
    assert Settings(_env_file=None).WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS == 10.0
    assert Settings(_env_file=None).WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS \
        == DEFAULT_ACK_WAIT_TIMEOUT_S
    assert DEFAULT_ACK_WAIT_TIMEOUT_S == 10.0


def test_c2_ack_wait_is_configurable_and_reaches_adapter() -> None:
    """配置值可覆盖，并真实传到 Adapter（构造参数生效）。"""
    settings = Settings(_env_file=None, WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS=12.5)
    assert settings.WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS == 12.5
    adapter = WeComBotAdapter(
        registry=WeComBotConnectionRegistry(),
        ack_wait_timeout_s=settings.WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS)
    assert adapter._ack_wait_timeout_s == 12.5


def test_c3_invalid_ack_wait_fails_fast() -> None:
    """非正值会让 ACK 校验形同虚设 → 进程启动即失败（pydantic 约束）。"""
    from pydantic import ValidationError

    for bad in (0.0, -1.0):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS=bad)


def test_c4_main_wiring_passes_setting_to_adapter() -> None:
    """组合根确实把配置注入 Adapter（接线存在 → 配置真正生效）。"""
    source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    assert "ack_wait_timeout_s=settings.WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS" \
        in source


def test_c5_env_example_documents_the_knob() -> None:
    """开发模板与 Settings 字段面对齐（新增键被记录）。"""
    text = (BACKEND / ".env.example").read_text(encoding="utf-8")
    assert "WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS=10.0" in text


# ================================================================== D. 迟到 ACK 观测

def test_d1_late_ack_is_observed_but_not_credited_as_delivery(caplog) -> None:
    """超时后才到的 ACK：被**记录**（WARNING），但不被当成交付成功。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _silent_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=0.2)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver(_outbound())
        # 迟到回执（同一 req_id）到达 —— transport 线程视角
        credited = registry.fulfil_ack(
            7, "REQ-9", {"errcode": 0, "errmsg": "ok"})
    assert credited is False          # **不**兑现任何在途交付（语义未变）
    assert "wecom bot late ack observed" in caplog.text
    assert "errcode=0" in caplog.text
    assert len(sent) == 1             # 未发生任何第二次发送（无自动重投）


def test_d2_late_ack_observation_is_recorded_once(caplog) -> None:
    """迟到 ACK 记录被消费一次：第二次不再算迟到（避免重复噪音）。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _silent_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=0.2)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver(_outbound())
        registry.fulfil_ack(7, "REQ-9", {"errcode": 0, "errmsg": "ok"})
        registry.fulfil_ack(7, "REQ-9", {"errcode": 0, "errmsg": "ok"})
    late_records = [r for r in caplog.records
                    if "wecom bot late ack observed" in r.getMessage()]
    assert len(late_records) == 1


def test_d3_unmatched_ack_is_silent_and_not_a_late_ack(caplog) -> None:
    """与任何投递无关的响应（如 ping 回执）：静默忽略，**不**冒充迟到 ACK。"""
    registry = WeComBotConnectionRegistry()
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert registry.fulfil_ack(1, "never-sent", {"errcode": 0}) is False
    assert "wecom bot late ack observed" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_d4_in_flight_ack_matching_semantics_unchanged(caplog) -> None:
    """既有 ACK 匹配语义零变化：只有匹配 req_id 的在途槽才被兑现。"""
    registry = WeComBotConnectionRegistry()
    slot = registry.expect_ack(1, "req-x")
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert registry.fulfil_ack(1, "other-req", {"errcode": 0}) is False
        assert not slot.event.is_set()
        assert registry.fulfil_ack(1, "req-x", {"errcode": 60140}) is True
    assert slot.event.wait(0.5)
    assert slot.payload == {"errcode": 60140}
    registry.forget_ack(1, "req-x")
    assert registry.fulfil_ack(1, "req-x", {"errcode": 0}) is False


def test_d5_late_ack_does_not_trigger_any_resend(caplog) -> None:
    """迟到 ACK 观测**不触发**任何重投（帧只发过一次）。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _silent_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=0.2)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(ChannelDeliveryFailedError):
            adapter.deliver(_outbound())
        registry.fulfil_ack(7, "REQ-9", {"errcode": 0, "errmsg": "ok"})
        time.sleep(0.05)
    assert len(sent) == 1


# ================================================================== E. 既有语义回归

def test_e1_delivery_failure_error_code_and_stage_unchanged() -> None:
    """投递失败的错误码 / stage 映射未变（不新造错误码）。"""
    from app.domain.activity_record import STAGE_DELIVERY

    stage, code, default_retryable = STAGE_FAILURE_MAP[ChannelDeliveryFailedError]
    assert stage == STAGE_DELIVERY
    assert code == "CHANNEL_DELIVERY_FAILED"
    assert default_retryable is True


def test_e2_delivery_state_and_schema_are_now_the_p2_set() -> None:
    """投递状态取值与 schema：**P2 之后**的冻结面（P0 的「零新增」已被 P2 取代）。

    P0（可观测性）曾断言「不引入投递状态机/新字段」；**P2 Delivery Recovery**
    经批准扩展了两个状态取值（`delivering` 在途 / `unknown` 结果未知）与三列
    （投递 attempt 计数 / 持有起点 / 协议世代）⇒ 本守卫改为锁定**当前**的冻结面，
    防止再次无声扩张。
    """
    from app.domain.interaction_record import (
        DELIVERY_DELIVERED,
        DELIVERY_DELIVERING,
        DELIVERY_FAILED,
        DELIVERY_PENDING,
        DELIVERY_UNKNOWN,
    )

    assert (DELIVERY_PENDING, DELIVERY_DELIVERING, DELIVERY_DELIVERED,
            DELIVERY_FAILED, DELIVERY_UNKNOWN) == (
        "pending", "delivering", "delivered", "failed", "unknown")

    model_source = (BACKEND / "app" / "persistence" / "models"
                    / "interaction.py").read_text(encoding="utf-8")
    # P2 的三列（投递围栏 / 持有起点 / 协议世代）——**恰好**这三列
    assert "delivery_attempts" in model_source
    assert "delivery_started_at" in model_source
    assert "delivery_protocol" in model_source

    # 恢复机制已落地（P2）：独立的恢复驱动模块 + 独立的恢复工具
    assert (BACKEND / "app" / "application" / "delivery_recovery.py").is_file()
    assert (BACKEND / "tools" / "delivery_backfill.py").is_file()


def test_e3_agent_call_path_untouched_by_p0() -> None:
    """P0 只加日志：Agent 请求/超时/重试/调度行为未变（源码守卫）。"""
    source = (BACKEND / "app" / "adapters" / "agents" / "acp.py").read_text(
        encoding="utf-8")
    # 分段耗时日志存在，但超时预算常量与「无重试」纪律不变
    assert "acp tencent sse stage timing" in source
    assert "acp tencent sse stage exceeded budget" in source
    assert "DEFAULT_TIMEOUT_S = 30.0" in source
    assert source.count("for attempt in") == 0    # 无重试循环被引入
