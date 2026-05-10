from core.rpc_service import RPCPermission
from core.rpc_handlers import RPCHandlerBase, register_handler_class


@register_handler_class
class ChainHandler(RPCHandlerBase):
    domain = "chain"

    def register_methods(self):
        self.register(
            "chain_getHeight", self.svc._chain_get_height,
            "Get current chain height",
            RPCPermission.PUBLIC
        )
        self.register(
            "chain_getInfo", self.svc._chain_get_info,
            "Get chain information",
            RPCPermission.PUBLIC
        )
        self.register(
            "chain_getConsensusStatus", self.svc._chain_get_consensus_status,
            "Get consensus and finality status",
            RPCPermission.PUBLIC
        )
        self.register(
            "chain_updateMechanismStrategy", self.svc._chain_update_mechanism_strategy,
            "Update mechanism strategy",
            RPCPermission.ADMIN
        )
        self.register(
            "chain_setConsensusFallbackPolicy", self.svc._chain_set_consensus_fallback_policy,
            "Set consensus fallback policy",
            RPCPermission.ADMIN
        )
        self.register(
            "chain_getConsensusFallbackAudit", self.svc._chain_get_consensus_fallback_audit,
            "Get consensus fallback audit log",
            RPCPermission.ADMIN
        )
        self.register(
            "sbox_getEncryptionPolicy", self.svc._sbox_get_encryption_policy,
            "Get S-Box encryption policy",
            RPCPermission.PUBLIC
        )
        self.register(
            "sbox_setEncryptionPolicy", self.svc._sbox_set_encryption_policy,
            "Set S-Box encryption policy",
            RPCPermission.ADMIN
        )
        self.register(
            "sbox_getDowngradeAudit", self.svc._sbox_get_downgrade_audit,
            "Get S-Box downgrade audit events",
            RPCPermission.PUBLIC
        )
