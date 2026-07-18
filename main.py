import os
import sqlite3
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# تفعيل أمان الشبكة لفك حظر المتصفح نهائياً
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="C:\\video-app\\static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("C:\\video-app\\static\\index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_dashboard():
    with open("C:\\video-app\\static\\admin.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/admin/activate-infinite-points/")
async def activate_infinite_points(username: str):
    if username == "نهاد":
        conn = sqlite3.connect("C:\\video-app\\app_data.db")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                points INTEGER,
                plan_type TEXT
            )
        """)
        cursor.execute("""
            INSERT INTO users (username, points, plan_type) 
            VALUES ('نهاد', 999999, 'pro')
            ON CONFLICT(username) 
            DO UPDATE SET points = 999999, plan_type = 'pro'
        """)
        conn.commit()
        conn.close()
        return {"status": "success", "message": "تم تفعيل حلقة الأرباح اللانهائية ونظام الـ Pro بنجاح يا مدير!"}
    raise HTTPException(status_code=403, detail="غير مسموح بالدخول!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
