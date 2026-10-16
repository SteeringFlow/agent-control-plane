"""Channel Management Application Service（Step 6.3）。

与 6.2（Agent）对称但贴合 Channel 的领域语义，不是机械复制：
- Channel 无 endpoint / credential_ref——入站渠道只表达「这是什么类型的渠道，
  配置是什么」，不表达与外部系统的真实连接（那属于未来 Adapter/Runtime Slice）；
- 重复性判定用 (type, name)：同一平台（type）下的同名渠道才是重复，
  不同平台同名渠道合法共存（wecom 与 feishu 都可以叫“客服台”）；
- 输入契约只含 name / type / config + 基础校验。

用例：Create / Get / List / Update + Application-level error mapping。
事务边界归 UoW（每个用例自持一个 UoW，显式 commit / 异常自动 rollback）。

11.8（Channel Management API）新增裁决：
- **config 更新 = merge 语义**：PATCH 提供的键覆写、未提供的键（尤其
  token / encoding_aes_key / corpsecret 等 credential-like material）保留——
  管理响应是脱敏回显，整段替换会把密键清空；
- PATCH 改 name / type 撞已有 (type, name) → DuplicateChannelError（与 Agent
  11.2 §5.3 的改名守卫同一先例，self 除外）。

边界（AST 守护）：不 import SQLAlchemy / FastAPI / api / schemas / adapters /
app.persistence；只依赖 app.domain、app.repositories（端口）、app.application。
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.application.errors import (
    ChannelNotFoundError,
    DuplicateBotChannelError,
    DuplicateChannelError,
    InvalidChannelInputError,
)
from app.domain.channel import Channel
from app.repositories.unit_of_work import UnitOfWork

# ---------------------------------------------------------------- 政策（15.2.3）

# type 受控集合：对齐 app/adapters/channels/ 注册表（当前实现 wecom / local /
# wecom_bot）。11.7 的「type 自由字符串（不设受控集合，留给未来 Adapter
# Registry）」deferred decision 在第二个 Channel Adapter（local）出现时到期——
# 见 15.2.2 First Success Audit §F；第三个（wecom_bot，3B-2.2 WebSocket
# transport）沿用同一裁决：受控集合始终 == 已实现的 Adapter 集合。
# 越界值（如 "console"）→ InvalidChannelInputError → HTTP 422，
# 杜绝「建了一个没有任何 Adapter 能承接的渠道却返回 201 active」。
ALLOWED_CHANNEL_TYPES: tuple[str, ...] = ("wecom", "local", "wecom_bot")

#: M7-AUI-10.6.7：重复身份防护只作用于 wecom_bot 形态（不抽象通用身份框架）。
#: 与 ALLOWED_CHANNEL_TYPES 内的字面值一致；App 层不 import adapters（AST 边界）。
WECOM_BOT_CHANNEL_TYPE: str = "wecom_bot"


def _extract_bot_id(config: dict | None) -> str | None:
    """规范化提取 wecom_bot Channel 的外部身份（config["bot_id"]）。

    规则（M7-AUI-10.6.7）：只比较规范化后有效的 bot_id——
    - 非 dict config / 键缺失 / 值非字符串 / strip 后为空 → None（无法可靠提取）；
    - None 的语义 = 「没有可用身份可比对」，**跳过**身份冲突检查并保持既有错误
      语义（config 完整性由运行时 `connection_spec_from_channel` fail-closed 把关，
      本层不新增 422、不静默造一个假身份）。
    刻意只读 `bot_id`（身份判定字段），**绝不读取/比较 bot_secret 或其它 config**。
    """
    if not isinstance(config, dict):
        return None
    value = config.get("bot_id")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _ensure_no_active_bot_identity_conflict(
    uow: UnitOfWork, *, bot_id: str, exclude_channel_id: int | None = None,
) -> None:
    """集中式 wecom_bot 身份冲突检查（M7-AUI-10.6.7，创建/更新/重启用三入口共用）。

    规则：
    - 仅对 `type == wecom_bot` 且 `status == active` 的既有 Channel 判定
      （inactive 渠道不占用身份——重新启用时会重新检查）；
    - 同一 tenant 范围内相同 bot_id 不允许第二条 active 渠道：tenant 作用域由
      UoW 注入的 `uow.channels`（tenant-scoped repository）天然保证，本函数不做
      跨租户策略（该问题保持 10.6.6 审计的「待决策」状态）；
    - `exclude_channel_id` = 当前更新对象自身（自身不算冲突对象）；
    - 经 `list_channels()` 全量内存比对，不依赖数据库特定 JSON 查询方言；
    - 冲突信息只含冲突渠道的 name / id（`DuplicateBotChannelError`），不回显
      bot_id、bot_secret 或完整 config。
    """
    for channel in uow.channels.list_channels():
        if channel.type != WECOM_BOT_CHANNEL_TYPE or channel.status != "active":
            continue
        if exclude_channel_id is not None and channel.id == exclude_channel_id:
            continue
        existing_bot_id = _extract_bot_id(channel.config)
        if existing_bot_id is not None and existing_bot_id == bot_id:
            raise DuplicateBotChannelError(channel.name, channel.id)


@dataclass(frozen=True)
class CreateChannel:
    """创建 Channel 的输入契约（应用层校验点，非 Pydantic/API schema）。

    type 目前只是分类字符串（wecom / feishu / dingtalk / webhook ...），
    不代表已实现对应外部 Adapter；状态由领域对象默认 active。
    """

    name: str
    type: str
    config: dict | None = None


@dataclass(frozen=True)
class UpdateChannel:
    """更新 Channel 的输入契约。

    None 字段表示「不修改」（本 Slice 不支持把字段显式清空为 None）。
    """

    name: str | None = None
    type: str | None = None
    status: str | None = None
    config: dict | None = None


def _require_non_blank(value: str | None, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise InvalidChannelInputError(f"{field} 不能为空")


def _require_dict_config(value: dict | None) -> None:
    if value is not None and not isinstance(value, dict):
        raise InvalidChannelInputError("config 必须是 JSON 对象（dict）")


def _require_allowed_type(value: str | None) -> None:
    """type 受控集合（15.2.3）：越界值一律拒绝（防「无 Adapter 可承接」的漂移）。

    与 Agent 的 `_require_allowed_type`（11.2 §3）对称——受控集合的唯一事实源
    是 app/adapters/channels/ 已实现的 Adapter 集合。
    """
    if value is not None and value not in ALLOWED_CHANNEL_TYPES:
        raise InvalidChannelInputError(
            f"type 必须是 {list(ALLOWED_CHANNEL_TYPES)} 之一，得到：{value!r}")


class ChannelApplicationService:
    """Channel 管理用例的编排者（依赖 UoW Port，而非具体 SQLAlchemy 实现）。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ------------------------------------------------------------ Create

    def create_channel(self, input: CreateChannel) -> Channel:
        """创建 Channel。

        校验（输入契约）→ 同平台去重守卫 → 写入 → commit。
        重复/非法输入分别抛出 DuplicateChannelError / InvalidChannelInputError。
        M7-AUI-10.6.7：wecom_bot 渠道在能可靠提取 bot_id 时，创建前做同租户
        active 身份冲突检查（命中 → DuplicateBotChannelError → HTTP 409）。
        """
        _require_non_blank(input.name, "name")
        _require_non_blank(input.type, "type")
        _require_allowed_type(input.type)
        _require_dict_config(input.config)

        channel = Channel(name=input.name, type=input.type, config=input.config)
        with self._uow_factory() as uow:
            if uow.channels.find_by_type_and_name(input.type, input.name) is not None:
                raise DuplicateChannelError(input.type, input.name)
            if input.type == WECOM_BOT_CHANNEL_TYPE:
                bot_id = _extract_bot_id(input.config)
                if bot_id is not None:
                    _ensure_no_active_bot_identity_conflict(uow, bot_id=bot_id)
            created = uow.channels.create(channel)
            uow.commit()
            return created

    # ------------------------------------------------------------ Get

    def get_channel(self, channel_id: int) -> Channel | None:
        """按 id 读取 Channel；不存在返回 None。"""
        with self._uow_factory() as uow:
            return uow.channels.get_by_id(channel_id)

    # ------------------------------------------------------------ List

    def list_channels(self) -> list[Channel]:
        """11.8：List 用例——返回全部 Channel（管理面陈列 / Connection 下拉数据源用）。

        MVP 无分页/过滤（与 11.3 list_agents 同一裁决）。
        """
        with self._uow_factory() as uow:
            return uow.channels.list_channels()

    # ------------------------------------------------------------ Update

    def update_channel(self, channel_id: int, change: UpdateChannel) -> Channel:
        """部分更新 Channel（None 字段不改；config 提供 = merge 进现有 config）。

        目标不存在抛 ChannelNotFoundError；非法输入抛 InvalidChannelInputError；
        改 name / type 撞已有 (type, name) 抛 DuplicateChannelError（self 除外）。
        """
        _require_non_blank(change.name, "name")
        _require_non_blank(change.type, "type")
        _require_non_blank(change.status, "status")
        _require_allowed_type(change.type)
        _require_dict_config(change.config)

        with self._uow_factory() as uow:
            current = uow.channels.get_by_id(channel_id)
            if current is None:
                raise ChannelNotFoundError(channel_id)
            # 11.8：改名 / 重分类撞已有 (type, name) → 409（与 Agent 11.2 §5.3
            # 同一应用级守卫；同样存在 TOCTOU 窗口，self 排除）。
            name_changed = change.name is not None and change.name != current.name
            type_changed = change.type is not None and change.type != current.type
            if name_changed or type_changed:
                target_type = change.type if type_changed else current.type
                target_name = change.name if name_changed else current.name
                clash = uow.channels.find_by_type_and_name(target_type, target_name)
                if clash is not None and clash.id != channel_id:
                    raise DuplicateChannelError(target_type, target_name)
            updated = self._apply_change(current, change)
            # M7-AUI-10.6.7：身份相关变化（type 进入 wecom_bot / 启用为 active /
            # bot_id 改写）在落库前做同租户 active 身份冲突检查（排除自身）。
            # 仅当「生效后 = wecom_bot 且 active」且身份相关字段确有变化时才检查，
            # 改名等非身份字段不会被误判（自身已排除，见 exclude_channel_id）。
            status_changed = change.status is not None and change.status != current.status
            identity_relevant = type_changed or status_changed or (
                _extract_bot_id(updated.config) != _extract_bot_id(current.config)
            )
            if (identity_relevant
                    and updated.type == WECOM_BOT_CHANNEL_TYPE
                    and updated.status == "active"):
                bot_id = _extract_bot_id(updated.config)
                if bot_id is not None:
                    _ensure_no_active_bot_identity_conflict(
                        uow, bot_id=bot_id, exclude_channel_id=channel_id)
            result = uow.channels.update(updated)
            uow.commit()
            return result

    @staticmethod
    def _apply_change(current: Channel, change: UpdateChannel) -> Channel:
        """11.8 冻结合同 #3：config 更新 = load → merge incoming → persist。

        提供的键覆写、未提供的键保留（尤其 token / encoding_aes_key /
        corpsecret 等 credential-like material）——管理响应是脱敏回显，
        整段替换会在管理员只改非密字段时把密键整段清空。
        """
        merged_config = (
            {**(current.config or {}), **change.config}
            if change.config is not None
            else current.config
        )
        return replace(
            current,
            name=change.name if change.name is not None else current.name,
            type=change.type if change.type is not None else current.type,
            status=change.status if change.status is not None else current.status,
            config=merged_config,
        )
