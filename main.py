import os
from dotenv import load_dotenv

load_dotenv()

from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import List
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pymongo.errors import PyMongoError

# ── Config ────────────────────────────────────────────────────
LOCAL_MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME         = "bazaar_db"
SECRET_KEY      = os.getenv("JWT_SECRET", "change-this-in-production")
ALGORITHM       = "HS256"
TOKEN_TTL       = 60 * 8   # 8 hours
ADMIN_USERNAME  = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD  = os.getenv("ADMIN_PASSWORD", "changeme")
CROAKER_INVENTORY_ID = "croaker-fish"
INVENTORY_ITEMS = {
    "guinness-small": {"name": "Guinness Small", "limit": 12},
    "malta-guinness": {"name": "Malta Guinness", "limit": 24},
    "amstel-malta": {"name": "Amstel Malta", "limit": 24},
    "desperados": {"name": "Desperados", "limit": 24},
    "smirnoff-ice": {"name": "Smirnoff Ice", "limit": 48},
    "heineken": {"name": "Heineken", "limit": 24},
    "water": {"name": "Bottle Water", "limit": 60},
    "soft-drinks": {"name": "Soft Drinks", "limit": 36},
    "star-radler": {"name": "Star Radler", "limit": 24},
    "gulder": {"name": "Gulder", "limit": 12},
    "chicken-laps": {"name": "Chicken Laps", "limit": 34},
    "croaker-fish": {"name": "Croaker Fish", "limit": 13},
    "full-chicken": {"name": "Full Chicken", "limit": 27},
    "catfish-pepper-soup": {"name": "Catfish Pepper Soup", "limit": 15},
    "guinea-fowl": {"name": "Guinea Fowl", "limit": 40},
    "special-fried-rice": {"name": "Special Fried Rice", "limit": 30},
}
CROAKER_STOCK_LIMIT = INVENTORY_ITEMS[CROAKER_INVENTORY_ID]["limit"]
INVENTORY_ALIASES = {
    "jollof-rice-chicken": "chicken-laps",
    "chef-adas-special-fried-rice": "special-fried-rice",
}

# ── JWT helpers ───────────────────────────────────────────────
security = HTTPBearer()

def create_token(username: str) -> str:
    """Creates a signed JWT for the given admin username, valid for 8 hours."""
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL),
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    print(f"[Auth] Token created for user '{username}', expires in {TOKEN_TTL} minutes")
    return token

def require_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    """Dependency — rejects any request that doesn't carry a valid admin JWT."""
    try:
        jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        print("[Auth] Token verified — admin access granted")
    except JWTError:
        print("[Auth] Token verification failed — access denied")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ── App setup ─────────────────────────────────────────────────

app = FastAPI(title="Bazaar Pre-order API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bazaar-frontend-one.vercel.app",
        "http://localhost:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database lifecycle ────────────────────────────────────────

db_client: AsyncIOMotorClient = None

@app.on_event("startup")
async def startup_db_client():
    """Opens the MongoDB connection when the FastAPI server starts."""
    global db_client
    db_client = AsyncIOMotorClient(LOCAL_MONGO_URI)
    print(f"[DB] Connected to MongoDB at {LOCAL_MONGO_URI}, using database '{DB_NAME}'")
    await initialize_inventory(get_db())

@app.on_event("shutdown")
async def shutdown_db_client():
    """Closes the MongoDB connection cleanly when the server shuts down."""
    if db_client:
        db_client.close()
        print("[DB] MongoDB connection closed")

def get_db():
    """Returns the active database handle. Call this inside every endpoint instead of using db directly."""
    return db_client[DB_NAME]

def inventory_id(item):
    item_id = item.get("id", "") if isinstance(item, dict) else item.id
    name = item.get("name", "") if isinstance(item, dict) else item.name
    for value in (item_id, name):
        normalized = value.strip().lower().replace(" ", "-").replace("&", "and")
        normalized = normalized.replace("'", "")
        candidate = INVENTORY_ALIASES.get(normalized, normalized)
        if candidate in INVENTORY_ITEMS:
            return candidate
    return None

def inventory_quantities(items):
    quantities = {}
    for item in items:
        item_key = inventory_id(item)
        if item_key:
            quantity = item.get("quantity", 0) if isinstance(item, dict) else item.quantity
            if int(quantity) > 0:
                quantities[item_key] = quantities.get(item_key, 0) + int(quantity)
    return quantities

async def initialize_inventory(db):
    committed = {item_id: 0 for item_id in INVENTORY_ITEMS}
    async for order in db.orders.find({"status": {"$ne": "cancelled"}}, {"items": 1}):
        for item_id, quantity in inventory_quantities(order.get("items", [])).items():
            committed[item_id] += quantity

    for item_id, item in INVENTORY_ITEMS.items():
        inventory = await db.inventory.find_one({"_id": item_id})
        if inventory and inventory.get("limit") == item["limit"]:
            await db.inventory.update_one(
                {"_id": item_id},
                {"$set": {"name": item["name"]}},
            )
            continue

        await db.inventory.update_one(
            {"_id": item_id},
            {"$set": {
                "name": item["name"],
                "limit": item["limit"],
                "remaining": max(0, item["limit"] - committed[item_id]),
            }},
            upsert=True,
        )

async def reserve_inventory(db, quantities):
    reserved = {}
    try:
        for item_id, quantity in quantities.items():
            result = await db.inventory.update_one(
                {"_id": item_id, "remaining": {"$gte": quantity}},
                {"$inc": {"remaining": -quantity}},
            )
            if result.modified_count == 0:
                inventory = await db.inventory.find_one({"_id": item_id})
                remaining = inventory["remaining"] if inventory else 0
                raise HTTPException(
                    status_code=409,
                    detail=f"Only {remaining} {INVENTORY_ITEMS[item_id]['name']} remaining.",
                )
            reserved[item_id] = quantity
    except Exception:
        await release_inventory(db, reserved)
        raise

async def release_inventory(db, quantities):
    for item_id, quantity in quantities.items():
        if quantity > 0:
            await db.inventory.update_one(
                {"_id": item_id},
                {"$inc": {"remaining": quantity}},
            )

# ── Pydantic schemas ──────────────────────────────────────────

class OrderItem(BaseModel):
    id: str
    name: str
    price: float
    quantity: int = Field(gt=0)

class OrderRequest(BaseModel):
    customer_name: str
    customer_phone: str
    items: List[OrderItem]

class LoginRequest(BaseModel):
    username: str
    password: str

# ── Customer endpoints ────────────────────────────────────────

@app.post("/api/orders")
async def process_order(order: OrderRequest):
    """
    Receives a new order from the customer-facing app.
    Submitted item prices already include applicable item discounts. Sums those
    prices without applying an additional order-level discount, saves the order
    to MongoDB as 'pending', and returns the order ID + receipt summary.
    """
    if not order.items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    subtotal = sum(item.price * item.quantity for item in order.items)
    final_total = subtotal

    print(f"[Order] New order from '{order.customer_name}' ({order.customer_phone}) — "
          f"item total: ₦{subtotal:,.0f}, amount due: ₦{final_total:,.0f}")

    order_document = {
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "items": [item.dict() for item in order.items],
        "total": final_total,
        "pricing": {
            "subtotal": subtotal,
            "discount_applied": False,
            "discount_amount": 0,
            "amount_due": final_total,
        },
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    }

    db = get_db()
    reserved = inventory_quantities(order.items)
    await reserve_inventory(db, reserved)

    try:
        result = await db.orders.insert_one(order_document)
    except PyMongoError:
        await release_inventory(db, reserved)
        raise
    print(f"[Order] Saved to MongoDB with id {result.inserted_id}")

    return {
        "status": "success",
        "order_id": str(result.inserted_id),
        "receipt": {
            "customer": order.customer_name,
            "subtotal": subtotal,
            "discount_applied": False,
            "discount_amount": 0,
            "amount_due": final_total,
        },
    }

# ── Admin endpoints ───────────────────────────────────────────

@app.get("/api/inventory/croaker-fish")
async def get_croaker_inventory():
    inventory = await get_db().inventory.find_one({"_id": CROAKER_INVENTORY_ID})
    return {"limit": CROAKER_STOCK_LIMIT, "remaining": inventory["remaining"] if inventory else 0}

@app.get("/api/inventory")
async def get_inventory():
    db = get_db()
    items = []
    for item_id, item in INVENTORY_ITEMS.items():
        inventory = await db.inventory.find_one({"_id": item_id})
        items.append({
            "id": item_id,
            "name": item["name"],
            "limit": item["limit"],
            "remaining": inventory["remaining"] if inventory else item["limit"],
        })
    return items

@app.post("/api/admin/login")
async def admin_login(creds: LoginRequest):
    """
    Handles admin login requests.
    Protected — requires a valid admin JWT in the Authorization header.
    """
    print(f"[Debug] Got: '{creds.username}' / '{creds.password}'")
    print(f"[Debug] Env: '{ADMIN_USERNAME}' / '{ADMIN_PASSWORD}'")
    if creds.username != ADMIN_USERNAME or creds.password != ADMIN_PASSWORD:
        print(f"[Auth] Failed login attempt for username '{creds.username}'")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    print(f"[Auth] Admin '{creds.username}' logged in successfully")
    return {"access_token": create_token(creds.username), "token_type": "bearer"}


@app.get("/api/orders")
async def get_orders(_=Depends(require_admin)):
    """
    Returns all orders from MongoDB, newest first.
    Protected — requires a valid admin JWT in the Authorization header.
    Converts each MongoDB ObjectId to a plain string so the response is JSON-serialisable.
    """
    db = get_db()
    orders = []
    async for doc in db.orders.find().sort("created_at", -1):
        doc["_id"] = str(doc["_id"])
        orders.append(doc)
    print(f"[Orders] Returning {len(orders)} orders to admin dashboard")
    return orders


@app.patch("/api/orders/{order_id}/status")
async def update_order_status(order_id: str, body: dict, _=Depends(require_admin)):
    """
    Updates the status of a single order (pending → confirmed or cancelled).
    Protected — requires a valid admin JWT.
    Accepts the order's MongoDB ObjectId as a path parameter.
    """
    status = body.get("status")
    if status not in ("pending", "confirmed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"Invalid status '{status}' — must be pending, confirmed, or cancelled")

    try:
        oid = ObjectId(order_id)
    except Exception:
        raise HTTPException(status_code=400, detail=f"'{order_id}' is not a valid MongoDB ObjectId")

    db = get_db()
    order = await db.orders.find_one({"_id": oid})
    if not order:
        raise HTTPException(status_code=404, detail=f"No order found with id '{order_id}'")

    previous_status = order.get("status", "pending")
    if previous_status == status:
        return {"ok": True}

    quantities = inventory_quantities(order.get("items", []))
    reactivating = previous_status == "cancelled" and status != "cancelled"
    if reactivating:
        await reserve_inventory(db, quantities)

    status_filter = (
        {"status": {"$exists": False}}
        if "status" not in order
        else {"status": previous_status}
    )
    result = await db.orders.update_one(
        {"_id": oid, **status_filter},
        {"$set": {"status": status}},
    )
    if result.matched_count == 0:
        if reactivating:
            await release_inventory(db, quantities)
        raise HTTPException(status_code=409, detail="Order status changed. Refresh and try again.")

    if previous_status != "cancelled" and status == "cancelled":
        await release_inventory(db, quantities)

    print(f"[Orders] Order {order_id} status updated to '{status}'")
    return {"ok": True}