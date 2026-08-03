from fastapi import APIRouter

from app.core.relay import relay

router = APIRouter()


@router.get("/providers")
def list_providers():
    return {
        "providers": [
            {
                "name": provider.name,
                "enabled": provider.enabled,
                "priority": provider.priority,
                "models": provider.models,
            }
            for provider in relay.provider_manager.all()
        ]
    }