from fastapi import APIRouter

from . import (
    ambient,
    assets,
    auth,
    avatars,
    brand_kits,
    content,
    generation,
    ingredients,
    models_api,
    project_audio,
    projects,
    prompts,
    providers,
    settings_keys,
    spend,
    studio,
    system,
    training,
    webhooks,
    youtube,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/api", tags=["auth"])
api_router.include_router(avatars.router, prefix="/api", tags=["avatars"])
api_router.include_router(ingredients.router, prefix="/api", tags=["ingredients"])
api_router.include_router(brand_kits.router, prefix="/api", tags=["brand-kits"])
api_router.include_router(assets.router, prefix="/api", tags=["assets"])
api_router.include_router(projects.router, prefix="/api", tags=["projects"])
api_router.include_router(project_audio.router, prefix="/api", tags=["project-audio"])
api_router.include_router(generation.router, prefix="/api", tags=["generation"])
api_router.include_router(generation.public_router, prefix="/api", tags=["public"])
api_router.include_router(studio.router, prefix="/api/studio", tags=["studio"])
api_router.include_router(providers.router, prefix="/api/providers", tags=["providers"])
api_router.include_router(system.router, prefix="/api/system", tags=["system"])
api_router.include_router(training.router, prefix="/api", tags=["training"])
api_router.include_router(ambient.router, prefix="/api/ambient", tags=["ambient"])
api_router.include_router(models_api.router, prefix="/api/models", tags=["models"])
api_router.include_router(youtube.router, prefix="/api", tags=["publishing"])
api_router.include_router(prompts.router, prefix="/api/prompts", tags=["prompts"])
api_router.include_router(spend.router, prefix="/api/spend", tags=["spend"])
api_router.include_router(settings_keys.router, prefix="/api/settings/keys", tags=["settings"])
api_router.include_router(content.router, prefix="/api/settings/content", tags=["settings"])
api_router.include_router(webhooks.router, prefix="/api", tags=["webhooks"])
api_router.include_router(ambient.public_router, prefix="/api", tags=["public"])

__all__ = ["api_router"]
