import redis.asyncio as aioredis

from app.config import settings

redis_client: aioredis.Redis | None = None


def _client_kwargs() -> dict:
    """Connection kwargs, tuned to keep the wire protocol clean.

    redis-py >= 8 defaults maintenance notifications to 'auto', which
    probes ``CLIENT MAINT_NOTIFICATIONS`` on every new connection. OSS
    Redis servers don't implement the subcommand and reply ERR — harmless
    to the client (it logs a debug line and moves on), but every probe
    shows up as an error span in wire-level tracing (eBPF/groundcover)
    and pollutes the dashboard. The feature only exists on Redis
    Enterprise/Cloud, which we don't target — disable the probe outright.
    Guarded so older redis-py versions (no such kwarg) still work.
    """
    kwargs: dict = {"decode_responses": True}
    try:
        from redis.maint_notifications import MaintNotificationsConfig

        kwargs["maint_notifications_config"] = MaintNotificationsConfig(enabled=False)
    except ImportError:
        pass
    return kwargs


async def get_redis() -> aioredis.Redis:
    global redis_client
    if redis_client is None:
        redis_client = aioredis.from_url(
            settings.REDIS_URL.get_secret_value(), **_client_kwargs()
        )
    return redis_client


async def close_redis():
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None
