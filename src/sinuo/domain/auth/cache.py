import hashlib
import hmac
import json


def authorization_cache_key(
    account_id: str,
    project_id: str,
    object_version_id: str,
    access_epoch: int,
) -> str:
    """Key for a caller-owned cache. SessionAuthorizer does not store one."""

    if type(access_epoch) is not int:
        raise TypeError("access_epoch must be an int")
    material = json.dumps(
        [account_id, project_id, object_version_id, access_epoch],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def cache_key_is_current(
    cache_key: str,
    *,
    account_id: str,
    project_id: str,
    object_version_id: str,
    access_epoch: int,
) -> bool:
    """False when the key was built for a different access epoch."""

    current = authorization_cache_key(account_id, project_id, object_version_id, access_epoch)
    if len(cache_key) != len(current):
        return False
    return hmac.compare_digest(cache_key, current)
