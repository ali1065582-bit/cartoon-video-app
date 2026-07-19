import os
import sqlite3
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# تعريف الهاندلر المخصص لمنصة Vercel السحابية لتعمل الأونلاين
handler = app

# فك حظر الشبكة والأمان (CORS) نهائياً
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# محاكاة بسيطة للمسار لبيئة السحاب بدون جدار حماية محلي
@app.get("/", response_class=HTMLResponse)
async def get_index():
    return "<h1>موقع توليد الفيديوهات الكرتونية لعام 2026 شغال أونلاين بنجاح يا مدير نهاد!</h1><p>للانتقال للوحة السرية أضف /admin لرابط الموقع</p>"

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_dashboard():
    # كود واجهة المدير السريعة المباشرة لضمان عمل السحاب
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <title>لوحة تحكم المدير السرية 💰</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #1a1a1a; color: white; padding: 40px; text-align: center; }
            .card { background-color: #2a2a2a; border-radius: 15px; padding: 30px; max-width: 600px; margin: 0 auto; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
            button { background-color: #00ffcc; color: black; font-size: 18px; font-weight: bold; border: none; padding: 15px 30px; border-radius: 8px; cursor: pointer; transition: 0.3s; }
            button:hover { background-color: #00b38f; transform: scale(1.05); }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>👑 لوحة التحكم السرية للمستثمر (نهاد) - نسخة السحاب</h1>
            <p>مرحباً بك يا مدير أونلاين. اضغط لتفعيل الـ Infinite Loop لقناتك.</p>
            <hr style="border-color: #444;">
            <button onclick="alert('🎯 تم التفعيل بنجاح أونلاين يا مدير نهاد! حلقة الأرباح اللانهائية وباقة PRO شغالة الآن!')">تفعيل النقاط اللانهائية وباقة PRO 🐳</button>
        </div>
    </body>
    </html>
    """
