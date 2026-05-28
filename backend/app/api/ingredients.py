from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import IngredientCreate, IngredientOut, IngredientUpdate
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/ingredients", response_model=list[IngredientOut])
def list_ingredients(db: Session = Depends(get_db)) -> list[models.Ingredient]:
    return db.query(models.Ingredient).order_by(models.Ingredient.created_at.desc()).all()


@router.post("/ingredients", response_model=IngredientOut, status_code=201)
def create_ingredient(body: IngredientCreate, db: Session = Depends(get_db)) -> models.Ingredient:
    i = models.Ingredient(**body.model_dump())
    db.add(i)
    db.commit()
    db.refresh(i)
    return i


@router.get("/ingredients/{ingredient_id}", response_model=IngredientOut)
def get_ingredient(ingredient_id: int, db: Session = Depends(get_db)) -> models.Ingredient:
    i = db.get(models.Ingredient, ingredient_id)
    if i is None:
        raise HTTPException(404, "ingredient not found")
    return i


@router.patch("/ingredients/{ingredient_id}", response_model=IngredientOut)
def update_ingredient(
    ingredient_id: int, body: IngredientUpdate, db: Session = Depends(get_db)
) -> models.Ingredient:
    i = db.get(models.Ingredient, ingredient_id)
    if i is None:
        raise HTTPException(404, "ingredient not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(i, k, v)
    db.commit()
    db.refresh(i)
    return i


@router.delete("/ingredients/{ingredient_id}", status_code=204)
def delete_ingredient(ingredient_id: int, db: Session = Depends(get_db)) -> None:
    i = db.get(models.Ingredient, ingredient_id)
    if i is None:
        raise HTTPException(404, "ingredient not found")
    db.delete(i)
    db.commit()
