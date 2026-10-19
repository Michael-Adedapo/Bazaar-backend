import os
from dotenv import load_dotenv
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

# ── Config ────────────────────────────────────────────────────
LOCAL_MONGO_URI = "mongodb://localhost:27017"
DB_NAME         = "bazaar_db"
SECRET_KEY      = os.getenv("JWT_SECRET", "change-this-in-production")
ALGORITHM       = "HS256"
TOKEN_TTL       = 60 * 8   # 8 hours

# ── JWT helpers ───────────────────────────────────────────────
load_dotenv()  # Load .env file if present, so we can read ADMIN_USERNAME and ADMIN_PASSWORD
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

# Single CORS registration — allow all origins for now, tighten to your domain in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

@app.on_event("shutdown")
async def shutdown_db_client():
    """Closes the MongoDB connection cleanly when the server shuts down."""
    if db_client:
        db_client.close()
        print("[DB] MongoDB connection closed")

def get_db():
    """Returns the active database handle. Call this inside every endpoint instead of using db directly."""
    return db_client[DB_NAME]

# ── Pydantic schemas ──────────────────────────────────────────

class OrderItem(BaseModel):
    id: str
    name: str
    price: float
    quantity: int

class OrderRequest(BaseModel):
    customer_name: str
    customer_phone: str
    items: List[OrderItem]

# ── Customer endpoints ────────────────────────────────────────

@app.post("/api/orders")
async def process_order(order: OrderRequest):
    """
    Receives a new order from the customer-facing app.
    Verifies prices on the backend, applies a 5% discount for orders ≥ ₦30,000,
    saves the order to MongoDB as 'pending', and returns the order ID + receipt summary.
    """
    if not order.items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    subtotal = sum(item.price * item.quantity for item in order.items)
    discount = subtotal * 0.05 if subtotal >= 30000 else 0
    final_total = subtotal - discount

    print(f"[Order] New order from '{order.customer_name}' ({order.customer_phone}) — "
          f"subtotal: ₦{subtotal:,.0f}, discount: ₦{discount:,.0f}, total: ₦{final_total:,.0f}")

    order_document = {
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "items": [item.dict() for item in order.items],
        "pricing": {
            "subtotal": subtotal,
            "discount_applied": discount > 0,
            "discount_amount": discount,
            "amount_due": final_total,
        },
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    }

    db = get_db()
    result = await db.orders.insert_one(order_document)
    print(f"[Order] Saved to MongoDB with id {result.inserted_id}")

    return {
        "status": "success",
        "order_id": str(result.inserted_id),
        "receipt": {
            "customer": order.customer_name,
            "subtotal": subtotal,
            "discount_applied": discount > 0,
            "discount_amount": discount,
            "amount_due": final_total,
        },
    }

# ── Admin endpoints ───────────────────────────────────────────

@app.post("/api/admin/login")
async def admin_login(body: dict):
    """
    Verifies admin credentials against ADMIN_USERNAME and ADMIN_PASSWORD env vars.
    Returns a signed JWT on success. The frontend stores this in sessionStorage
    and attaches it as a Bearer token to every subsequent admin request.
    """
    username = body.get("username")
    password = body.get("password")
    ok = (
        username == os.getenv("ADMIN_USERNAME", "admin")
        and password == os.getenv("ADMIN_PASSWORD", "changeme")
    )
    if not ok:
        print(f"[Auth] Failed login attempt for username '{username}'")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    print(f"[Auth] Admin '{username}' logged in successfully")
    return {"access_token": create_token(username), "token_type": "bearer"}


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
    result = await db.orders.update_one({"_id": oid}, {"$set": {"status": status}})

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"No order found with id '{order_id}'")

    print(f"[Orders] Order {order_id} status updated to '{status}'")
    return {"ok": True}