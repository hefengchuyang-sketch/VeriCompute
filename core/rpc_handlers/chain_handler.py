from core.rpc_service import RPCPermission
from core.rpc_handlers import RPCHandlerBase, register_handler_class

@register_handler_class
class ChainHandler(RPCHandlerBase):
    domain = "chain"

    def register_methods(self):
        self.register(
            "chain_getHeight", self.svc._chain_get_height,
            "获取当前区块高度",
            RPCPermission.PUBLIC
        )
        self.register(
            "chain_getInfo", self.svc._chain_get_info,
            "获取链信息",
            RPCPermission.PUBLIC
        )
        self.register(
            "chain_getConsensusStatus", self.svc._chain_get_consensus_status,
            "获取共识与终局性状态",
            RPCPermission.PUBLIC
        )
        self.register(
            "chain_updateMechanismStrategy", self.svc._chain_update_mechanism_strategy,
            "更新机制策略（版本化/灰度/回滚）",
            RPCPermission.ADMIN
        )
        self.register(
            "sbox_getEncryptionPolicy", self.svc._sbox_get_encryption_policy,
            "获取 S-Box 加密治理策略",
            RPCPermission.PUBLIC
        )
        self.register(
            "sbox_setEncryptionPolicy", self.svc._sbox_set_encryption_policy,
            "设置 S-Box 加密治理策略",
            RPCPermission.ADMIN
        )
        self.register(
            "sbox_getDowngradeAudit", self.svc._sbox_get_downgrade_audit,
            "查询 S-Box 降级审计事件",
            RPCPermission.PUBLIC
        )
