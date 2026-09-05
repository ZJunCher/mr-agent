MODEL_SERVICE_UNAVAILABLE_FAILURE_KIND = "provider_unavailable"
MODEL_SERVICE_UNAVAILABLE_MESSAGE = "模型服务不可用，建议稍后重试。"
_LEGACY_MODEL_UNAVAILABLE_PREFIX = "模型服务暂时不可用"


def is_model_service_unavailable(failure_kind: str, error: str = "") -> bool:
    normalized_kind = str(failure_kind or "").strip().lower()
    if normalized_kind:
        return normalized_kind == MODEL_SERVICE_UNAVAILABLE_FAILURE_KIND
    return str(error or "").strip().startswith(_LEGACY_MODEL_UNAVAILABLE_PREFIX)
