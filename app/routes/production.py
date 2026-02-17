"""
Ishlab chiqarish — retseptlar, buyurtmalar, xom ashyo, bosqichlar, tasdiq/revert.
"""
import json
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text

from app.core import templates
from app.models.database import (
    get_db,
    User,
    Warehouse,
    Product,
    Recipe,
    RecipeItem,
    RecipeStage,
    Production,
    ProductionItem,
    ProductionStage,
    Stock,
    Machine,
    Employee,
    PRODUCTION_STAGE_NAMES,
)
from app.deps import require_auth, require_admin, get_current_user
from app.utils.notifications import check_low_stock_and_notify

router = APIRouter(prefix="/production", tags=["production"])


def _recipe_max_stage(recipe) -> int:
    if not recipe or not recipe.stages:
        return 2
    return max(s.stage_number for s in recipe.stages)


def _do_complete_production_stock(db, production, recipe):
    """Kerak=0 bo'lsa o'tkazib yuboriladi; yetmasa borini tortadi (min(kerak, mavjud))."""
    if production.production_items:
        items_to_use = [(pi.product_id, pi.quantity) for pi in production.production_items]
    else:
        items_to_use = [(item.product_id, item.quantity * production.quantity) for item in recipe.items]
    items_actual = []
    for product_id, required in items_to_use:
        if required is None or required <= 0:
            items_actual.append((product_id, 0.0))
            continue
        stock = db.query(Stock).filter(
            Stock.warehouse_id == production.warehouse_id,
            Stock.product_id == product_id,
        ).first()
        available = (stock.quantity if stock else 0) or 0
        actual_use = min(required, available)
        items_actual.append((product_id, actual_use))
    for product_id, actual_use in items_actual:
        if actual_use <= 0:
            continue
        stock = db.query(Stock).filter(
            Stock.warehouse_id == production.warehouse_id,
            Stock.product_id == product_id,
        ).first()
        if stock:
            stock.quantity -= actual_use
    total_material_cost = 0.0
    for product_id, actual_use in items_actual:
        product = db.query(Product).filter(Product.id == product_id).first()
        if product and getattr(product, "purchase_price", None) is not None:
            total_material_cost += actual_use * (product.purchase_price or 0)
    output_units = production.quantity * (recipe.output_quantity or 1)
    cost_per_unit = (total_material_cost / output_units) if output_units > 0 else 0
    out_wh_id = production.output_warehouse_id if production.output_warehouse_id else production.warehouse_id
    product_stock = db.query(Stock).filter(
        Stock.warehouse_id == out_wh_id,
        Stock.product_id == recipe.product_id,
    ).first()
    if product_stock:
        product_stock.quantity += output_units
    else:
        db.add(Stock(warehouse_id=out_wh_id, product_id=recipe.product_id, quantity=output_units))
    output_product = db.query(Product).filter(Product.id == recipe.product_id).first()
    if output_product:
        product_stock = db.query(Stock).filter(
            Stock.warehouse_id == out_wh_id,
            Stock.product_id == recipe.product_id,
        ).first()
        old_price = output_product.purchase_price or 0
        old_qty = (product_stock.quantity - output_units) if product_stock else 0
        if old_qty > 0 and old_price > 0 and output_units > 0:
            output_product.purchase_price = (old_qty * old_price + output_units * cost_per_unit) / (old_qty + output_units)
        elif cost_per_unit > 0:
            output_product.purchase_price = cost_per_unit
    return None


@router.get("", response_class=HTMLResponse)
async def production_index_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    # Default qiymatlar
    warehouses = []
    recipes = []
    total_recipes = 0
    today_quantity = 0
    pending_productions = 0
    recent_productions = []
    
    try:
        # Omborlar
        try:
            warehouses = db.query(Warehouse).all()
        except Exception as e:
            print(f"Warehouses query error: {e}")
            warehouses = []
        
        # Recipes
        try:
            recipes_raw = db.query(Recipe).filter(Recipe.is_active == True).all()
            from app.models.database import Unit
            recipes = []
            for recipe in recipes_raw:
                try:
                    if recipe.product_id:
                        product = db.query(Product).filter(Product.id == recipe.product_id).first()
                        if product and product.unit_id:
                            unit = db.query(Unit).filter(Unit.id == product.unit_id).first()
                            if unit:
                                product.unit = unit
                        recipe.product = product
                    recipes.append(recipe)
                except Exception as recipe_error:
                    print(f"Recipe {recipe.id} yuklashda xatolik: {recipe_error}")
                    recipes.append(recipe)
        except Exception as e:
            print(f"Recipes query error: {e}")
            recipes = []
        
        # Total recipes
        try:
            total_recipes = db.query(Recipe).filter(Recipe.is_active == True).count()
        except Exception as e:
            print(f"Total recipes count error: {e}")
            total_recipes = len(recipes)
        
        today = datetime.now().date()
        
        # Bugungi ishlab chiqarishlar - raw SQL bilan (machine_id va operator_id muammosini oldini olish)
        try:
            from sqlalchemy import text
            today_productions_result = db.execute(
                text("""
                    SELECT id, number, date, recipe_id, warehouse_id, output_warehouse_id, 
                           quantity, status, current_stage, max_stage, user_id, note, created_at
                    FROM productions
                    WHERE date >= :today_date AND status = :status
                """),
                {"today_date": today, "status": "completed"}
            ).fetchall()
            today_quantity = sum(float(row.quantity or 0) for row in today_productions_result) if today_productions_result else 0
        except Exception as e:
            today_quantity = 0
            print(f"Today productions query error: {e}")
        
        # Kutilmoqdagi buyurtmalar - raw SQL bilan
        try:
            from sqlalchemy import text
            pending_count = db.execute(
                text("SELECT COUNT(*) as count FROM productions WHERE status = :status"),
                {"status": "draft"}
            ).scalar()
            pending_productions = pending_count or 0
        except Exception as e:
            pending_productions = 0
            print(f"Pending productions query error: {e}")
        
        # Oxirgi ishlab chiqarishlar - raw SQL bilan (machine_id va operator_id muammosini oldini olish)
        try:
            from sqlalchemy import text
            recent_productions_result = db.execute(
                text("""
                    SELECT id, number, date, recipe_id, warehouse_id, output_warehouse_id, 
                           quantity, status, current_stage, max_stage, user_id, note, created_at
                    FROM productions
                    ORDER BY date DESC
                    LIMIT :limit
                """),
                {"limit": 10}
            ).fetchall()
            
            # Production obyektlarini yaratish (faqat mavjud ustunlar bilan)
            recent_productions_raw = []
            for row in recent_productions_result:
                prod = Production()
                prod.id = row.id
                prod.number = row.number
                prod.date = row.date
                prod.recipe_id = row.recipe_id
                prod.warehouse_id = row.warehouse_id
                prod.output_warehouse_id = row.output_warehouse_id
                prod.quantity = row.quantity
                prod.status = row.status
                prod.current_stage = row.current_stage
                prod.max_stage = row.max_stage
                prod.user_id = row.user_id
                prod.note = row.note
                prod.created_at = row.created_at
                recent_productions_raw.append(prod)
            
            from app.models.database import Unit
            recent_productions = []
            for prod in recent_productions_raw:
                try:
                    # Recipe ni yuklash (lazy loading muammosini oldini olish)
                    if prod.recipe_id:
                        recipe = db.query(Recipe).filter(Recipe.id == prod.recipe_id).first()
                        if recipe:
                            # Product ni yuklash
                            if recipe.product_id:
                                product = db.query(Product).filter(Product.id == recipe.product_id).first()
                                if product:
                                    # Unit ni yuklash
                                    if product.unit_id:
                                        unit = db.query(Unit).filter(Unit.id == product.unit_id).first()
                                        if unit:
                                            product.unit = unit
                                    recipe.product = product
                            # Recipe ni Production ga biriktirish
                            prod.recipe = recipe
                    recent_productions.append(prod)
                except Exception as prod_error:
                    print(f"Production {prod.id if hasattr(prod, 'id') else 'unknown'} yuklashda xatolik: {prod_error}")
                    import traceback
                    traceback.print_exc()
                    continue
        except Exception as e:
            recent_productions = []
            print(f"Recent productions query error: {e}")
            import traceback
            traceback.print_exc()
    
    except Exception as e:
        import traceback
        error_msg = str(e)
        traceback.print_exc()
        print(f"Production index page error: {error_msg}")
    
    try:
        resp = templates.TemplateResponse("production/index.html", {
            "request": request,
            "current_user": current_user,
            "total_recipes": total_recipes,
            "today_quantity": today_quantity,
            "pending_productions": pending_productions,
            "recent_productions": recent_productions,
            "recipes": recipes,
            "warehouses": warehouses,
            "page_title": "Ishlab chiqarish",
            "now": datetime.now(),
        })
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp
    except Exception as template_error:
        import traceback
        error_msg = str(template_error)
        traceback.print_exc()
        # Xavfsiz fallback
        resp = templates.TemplateResponse("production/index.html", {
            "request": request,
            "current_user": current_user,
            "total_recipes": 0,
            "today_quantity": 0,
            "pending_productions": 0,
            "recent_productions": [],
            "recipes": [],
            "warehouses": [],
            "page_title": "Ishlab chiqarish",
            "now": datetime.now(),
            "error": f"Xatolik: {error_msg[:200]}",
        })
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp


@router.get("/recipes", response_class=HTMLResponse)
async def production_recipes(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    warehouses = db.query(Warehouse).all()
    recipes = db.query(Recipe).all()
    products = db.query(Product).filter(Product.type.in_(["tayyor", "yarim_tayyor"])).all()
    materials = db.query(Product).filter(Product.type == "hom_ashyo").all()
    recipe_products_json = json.dumps([
        {"id": p.id, "name": (p.name or ""), "unit": (p.unit.name if p.unit else "kg")}
        for p in products
    ]).replace("<", "\\u003c")
    return templates.TemplateResponse("production/recipes.html", {
        "request": request,
        "current_user": current_user,
        "recipes": recipes,
        "products": products,
        "recipe_products_json": recipe_products_json,
        "materials": materials,
        "warehouses": warehouses,
        "page_title": "Retseptlar",
    })


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
async def production_recipe_detail(
    request: Request,
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Retsept topilmadi")
    materials = db.query(Product).filter(Product.type.in_(["hom_ashyo", "yarim_tayyor", "tayyor"])).all()
    recipe_stages = sorted(recipe.stages, key=lambda s: s.stage_number) if recipe.stages else []
    return templates.TemplateResponse("production/recipe_detail.html", {
        "request": request,
        "current_user": current_user,
        "recipe": recipe,
        "materials": materials,
        "recipe_stages": recipe_stages,
        "page_title": f"Retsept: {recipe.name}",
    })


@router.post("/recipes/add")
async def add_recipe(
    request: Request,
    name: str = Form(...),
    product_id: int = Form(...),
    output_quantity: float = Form(1),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    recipe = Recipe(
        name=name,
        product_id=product_id,
        output_quantity=output_quantity,
        description=description,
        is_active=True,
    )
    db.add(recipe)
    db.commit()
    return RedirectResponse(url=f"/production/recipes/{recipe.id}", status_code=303)


@router.post("/recipes/{recipe_id}/add-item")
async def add_recipe_item(
    recipe_id: int,
    product_id: int = Form(...),
    quantity: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Retsept topilmadi")
    db.add(RecipeItem(recipe_id=recipe_id, product_id=product_id, quantity=quantity))
    db.commit()
    return RedirectResponse(url=f"/production/recipes/{recipe_id}", status_code=303)


@router.post("/recipes/{recipe_id}/edit-item/{item_id}")
async def edit_recipe_item(
    recipe_id: int,
    item_id: int,
    product_id: int = Form(...),
    quantity: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    item = db.query(RecipeItem).filter(
        RecipeItem.id == item_id,
        RecipeItem.recipe_id == recipe_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Tarkib qatori topilmadi")
    item.product_id = product_id
    item.quantity = quantity
    db.commit()
    return RedirectResponse(url=f"/production/recipes/{recipe_id}", status_code=303)


@router.post("/recipes/{recipe_id}/add-stage")
async def add_recipe_stage(
    recipe_id: int,
    stage_number: int = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Retsept topilmadi")
    db.add(RecipeStage(recipe_id=recipe_id, stage_number=stage_number, name=(name or "").strip()))
    db.commit()
    return RedirectResponse(url=f"/production/recipes/{recipe_id}", status_code=303)


@router.post("/recipes/{recipe_id}/delete-stage/{stage_id}")
async def delete_recipe_stage(
    recipe_id: int,
    stage_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    stage = db.query(RecipeStage).filter(
        RecipeStage.id == stage_id,
        RecipeStage.recipe_id == recipe_id,
    ).first()
    if not stage:
        raise HTTPException(status_code=404, detail="Bosqich topilmadi")
    db.delete(stage)
    db.commit()
    return RedirectResponse(url=f"/production/recipes/{recipe_id}", status_code=303)


@router.post("/recipes/{recipe_id}/delete-item/{item_id}")
async def delete_recipe_item(
    recipe_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    item = db.query(RecipeItem).filter(
        RecipeItem.id == item_id,
        RecipeItem.recipe_id == recipe_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Tarkib qatori topilmadi")
    db.delete(item)
    db.commit()
    return RedirectResponse(url=f"/production/recipes/{recipe_id}", status_code=303)


@router.get("/{prod_id}/materials", response_class=HTMLResponse)
async def production_edit_materials(
    prod_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    production = db.query(Production).filter(Production.id == prod_id).first()
    if not production:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if production.status == "completed" and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Yakunlangan buyurtmani faqat administrator ko'ra oladi")
    if production.status not in ("draft", "completed"):
        raise HTTPException(status_code=400, detail="Faqat kutilmoqdagi yoki yakunlangan buyurtmani ko'rish mumkin")
    recipe = db.query(Recipe).filter(Recipe.id == production.recipe_id).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Retsept topilmadi")
    if not production.production_items:
        for item in recipe.items:
            db.add(ProductionItem(
                production_id=production.id,
                product_id=item.product_id,
                quantity=item.quantity * production.quantity,
            ))
        db.commit()
        db.refresh(production)
    read_only = production.status == "completed"
    return templates.TemplateResponse("production/edit_materials.html", {
        "request": request,
        "current_user": current_user,
        "production": production,
        "recipe": recipe,
        "read_only": read_only,
        "page_title": f"Xom ashyo: {production.number}",
    })


@router.post("/{prod_id}/materials")
async def production_save_materials(
    prod_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    production = db.query(Production).filter(Production.id == prod_id).first()
    if not production or production.status != "draft":
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi yoki tahrirlab bo'lmaydi")
    form = await request.form()
    for key, value in form.items():
        if key.startswith("qty_"):
            try:
                item_id = int(key.replace("qty_", ""))
                qty = float(value.replace(",", "."))
            except (ValueError, TypeError):
                continue
            pi = db.query(ProductionItem).filter(
                ProductionItem.id == item_id,
                ProductionItem.production_id == prod_id,
            ).first()
            if pi and qty >= 0:
                pi.quantity = qty
    db.commit()
    return RedirectResponse(url="/production/orders", status_code=303)


@router.get("/orders", response_class=HTMLResponse)
async def production_orders(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from urllib.parse import unquote
    productions = (
        db.query(Production)
        .options(joinedload(Production.recipe).joinedload(Recipe.stages))
        .order_by(Production.date.desc())
        .all()
    )
    machines = db.query(Machine).filter(Machine.is_active == True).all()
    employees = db.query(Employee).filter(Employee.is_active == True).all()
    error = request.query_params.get("error")
    detail = unquote(request.query_params.get("detail", "") or "")
    return templates.TemplateResponse("production/orders.html", {
        "request": request,
        "current_user": current_user,
        "productions": productions,
        "machines": machines,
        "employees": employees,
        "page_title": "Ishlab chiqarish buyurtmalari",
        "error": error,
        "error_detail": detail,
        "stage_names": PRODUCTION_STAGE_NAMES,
    })


@router.get("/new", response_class=HTMLResponse)
async def production_new(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    warehouses = db.query(Warehouse).all()
    recipes = db.query(Recipe).filter(Recipe.is_active == True).all()
    machines = db.query(Machine).filter(Machine.is_active == True).all()
    employees = db.query(Employee).filter(Employee.is_active == True).all()
    return templates.TemplateResponse("production/new_order.html", {
        "request": request,
        "current_user": current_user,
        "recipes": recipes,
        "warehouses": warehouses,
        "machines": machines,
        "employees": employees,
        "page_title": "Yangi ishlab chiqarish",
    })


@router.post("/create")
async def create_production(
    request: Request,
    recipe_id: int = Form(...),
    warehouse_id: int = Form(...),
    output_warehouse_id: Optional[int] = Form(None),
    quantity: float = Form(...),
    note: str = Form(""),
    machine_id: Optional[int] = Form(None),
    operator_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if output_warehouse_id is None:
        output_warehouse_id = warehouse_id
    recipe = db.query(Recipe).options(joinedload(Recipe.stages)).filter(Recipe.id == recipe_id).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Retsept topilmadi")
    max_stage = _recipe_max_stage(recipe)
    today = datetime.now()
    count = db.query(Production).filter(
        Production.date >= today.replace(hour=0, minute=0, second=0)
    ).count()
    number = f"PR-{today.strftime('%Y%m%d')}-{str(count + 1).zfill(3)}"
    production = Production(
        number=number,
        recipe_id=recipe_id,
        warehouse_id=warehouse_id,
        output_warehouse_id=output_warehouse_id,
        quantity=quantity,
        note=note,
        status="draft",
        current_stage=1,
        max_stage=max_stage,
        user_id=current_user.id if current_user else None,
        machine_id=int(machine_id) if machine_id else None,
        operator_id=int(operator_id) if operator_id else None,
    )
    db.add(production)
    db.commit()
    db.refresh(production)
    for stage_num in range(1, max_stage + 1):
        db.add(ProductionStage(production_id=production.id, stage_number=stage_num))
    db.commit()
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if recipe:
        for item in recipe.items:
            db.add(ProductionItem(
                production_id=production.id,
                product_id=item.product_id,
                quantity=item.quantity * quantity,
            ))
        db.commit()
    return RedirectResponse(url="/production/orders", status_code=303)


@router.post("/{prod_id}/complete-stage")
async def complete_production_stage(
    prod_id: int,
    stage_number: int = Form(...),
    machine_id: Optional[int] = Form(None),
    operator_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    production = db.query(Production).filter(Production.id == prod_id).first()
    if not production:
        raise HTTPException(status_code=404, detail="Topilmadi")
    recipe = (
        db.query(Recipe)
        .options(joinedload(Recipe.stages))
        .filter(Recipe.id == production.recipe_id)
        .first()
    )
    if not recipe:
        raise HTTPException(status_code=404, detail="Retsept topilmadi")
    max_stage = _recipe_max_stage(recipe)
    if stage_number < 1 or stage_number > max_stage:
        raise HTTPException(status_code=400, detail=f"Bosqich 1–{max_stage} oralig'ida bo'lishi kerak")
    if production.status == "completed":
        return RedirectResponse(url="/production/orders", status_code=303)
    current = getattr(production, "current_stage", None) or 1
    if current > max_stage:
        err = _do_complete_production_stock(db, production, recipe)
        if err:
            return err
        production.status = "completed"
        production.current_stage = max_stage
        db.commit()
        check_low_stock_and_notify(db)
        return RedirectResponse(url="/production/orders", status_code=303)
    if stage_number != current:
        return RedirectResponse(
            url=f"/production/orders?error=stage&detail=Keyingi bosqich {current}",
            status_code=303,
        )
    stage_row = db.query(ProductionStage).filter(
        ProductionStage.production_id == prod_id,
        ProductionStage.stage_number == stage_number,
    ).first()
    now = datetime.now()
    if stage_row:
        if not stage_row.started_at:
            stage_row.started_at = now
        stage_row.completed_at = now
        stage_row.machine_id = int(machine_id) if machine_id else None
        stage_row.operator_id = int(operator_id) if operator_id else None
    if stage_number < max_stage:
        production.current_stage = stage_number + 1
        production.status = "in_progress"
        db.commit()
        return RedirectResponse(url="/production/orders", status_code=303)
    err = _do_complete_production_stock(db, production, recipe)
    if err:
        return err
    production.status = "completed"
    production.current_stage = max_stage
    db.commit()
    check_low_stock_and_notify(db)
    return RedirectResponse(url="/production/orders", status_code=303)


@router.post("/{prod_id}/complete")
async def complete_production(
    prod_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    production = db.query(Production).filter(Production.id == prod_id).first()
    if not production:
        raise HTTPException(status_code=404, detail="Topilmadi")
    recipe = db.query(Recipe).filter(Recipe.id == production.recipe_id).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Retsept topilmadi")
    err = _do_complete_production_stock(db, production, recipe)
    if err:
        return err
    production.status = "completed"
    production.current_stage = _recipe_max_stage(recipe)
    db.commit()
    check_low_stock_and_notify(db)
    return RedirectResponse(url="/production/orders", status_code=303)


@router.post("/{prod_id}/revert")
async def production_revert(
    prod_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    production = db.query(Production).filter(Production.id == prod_id).first()
    if not production:
        raise HTTPException(status_code=404, detail="Topilmadi")
    if production.status != "completed":
        return RedirectResponse(
            url="/production/orders?error=revert&detail=" + quote("Faqat yakunlangan buyurtmaning tasdiqini bekor qilish mumkin."),
            status_code=303,
        )
    recipe = db.query(Recipe).filter(Recipe.id == production.recipe_id).first()
    if not recipe:
        return RedirectResponse(
            url="/production/orders?error=revert&detail=" + quote("Retsept topilmadi."),
            status_code=303,
        )
    items_to_use = (
        [(pi.product_id, pi.quantity) for pi in production.production_items]
        if production.production_items
        else [(item.product_id, item.quantity * production.quantity) for item in recipe.items]
    )
    output_units = production.quantity * (recipe.output_quantity or 1)
    out_wh_id = production.output_warehouse_id if production.output_warehouse_id else production.warehouse_id
    product_stock = db.query(Stock).filter(
        Stock.warehouse_id == out_wh_id,
        Stock.product_id == recipe.product_id,
    ).first()
    if not product_stock or product_stock.quantity < output_units:
        return RedirectResponse(
            url="/production/orders?error=revert&detail=" + quote("Omborda tayyor mahsulot yetarli emas yoki o'zgargan. Tasdiqni bekor qilish mumkin emas."),
            status_code=303,
        )
    product_stock.quantity -= output_units
    for product_id, required in items_to_use:
        stock = db.query(Stock).filter(
            Stock.warehouse_id == production.warehouse_id,
            Stock.product_id == product_id,
        ).first()
        if stock:
            stock.quantity += required
        else:
            db.add(Stock(warehouse_id=production.warehouse_id, product_id=product_id, quantity=required))
    production.status = "draft"
    db.commit()
    return RedirectResponse(url="/production/orders", status_code=303)


@router.post("/{prod_id}/cancel")
async def cancel_production(prod_id: int, db: Session = Depends(get_db)):
    production = db.query(Production).filter(Production.id == prod_id).first()
    if not production:
        raise HTTPException(status_code=404, detail="Topilmadi")
    production.status = "cancelled"
    db.commit()
    return RedirectResponse(url="/production/orders", status_code=303)


@router.post("/{prod_id}/delete")
async def delete_production(
    prod_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    production = db.query(Production).filter(Production.id == prod_id).first()
    if not production:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    db.delete(production)
    db.commit()
    return RedirectResponse(url="/production/orders", status_code=303)
