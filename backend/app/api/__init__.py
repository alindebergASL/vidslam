from fastapi import APIRouter

from . import assets, auth, avatars, generation, ingredients, projects, providers, studio

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/api", tags=["auth"])
api_router.include_router(avatars.router, prefix="/api", tags=["avatars"])
api_router.include_router(ingredients.router, prefix="/api", tags=["ingredients"])
api_router.include_router(assets.router, prefix="/api", tags=["assets"])
api_router.include_router(projects.router, prefix="/api", tags=["projects"])
api_router.include_router(generation.router, prefix="/api", tags=["generation"])
api_router.include_router(studio.router, prefix="/api/studio", tags=["studio"])
api_router.include_router(providers.router, prefix="/api/providers", tags=["providers"])

__all__ = ["api_router"]
