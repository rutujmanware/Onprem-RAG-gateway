try:
    from gateway.auth.guard import UserIdentity, require_iisc_user
    from gateway.auth.oidc import OIDCValidator
    __all__ = ["UserIdentity", "require_iisc_user", "OIDCValidator"]
except ImportError:
    __all__ = []
