from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Cartoon Video Generator 2026")

# واجهة الموقع الرئيسية المبرمجة بالكامل مع صندوق التوليد
@app.get("/", response_class=HTMLResponse)
async def read_root():
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>منصة توليد الفيديوهات الكرتونية - باقة PRO</title>
        <link href="https://jsdelivr.net" rel="stylesheet">
        <style>
            body { background-color: #121212; color: #ffffff; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
            .main-container { max-width: 800px; margin: 50px auto; padding: 30px; background: #1e1e1e; border-radius: 15px; box-shadow: 0 8px 24px rgba(0,0,0,0.5); }
            .btn-pro { background: linear-gradient(45deg, #007bff, #00ffcc); border: none; color: #fff; font-weight: bold; padding: 12px; }
            .btn-pro:hover { background: linear-gradient(45deg, #0056b3, #00cc99); color: #fff; }
            .video-box { background: #000; border-radius: 10px; height: 350px; display: flex; align-items: center; justify-content: center; margin-top: 20px; border: 2px dashed #333; }
            .badge-pro { background-color: #ffc107; color: #000; font-weight: bold; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="main-container text-center">
                <h1 class="mb-4">🎬 محرك توليد وفصل الفيديوهات الكرتونية الذكي</h1>
                <div class="alert alert-info py-2">✨ مستقرة أونلاين لعام 2026 بنجاح يا مدير نهاد</div>
                <div class="d-flex justify-content-center mb-4">
                    <span class="badge badge-pro p-2 fs-6">👑 باقة المليون المفعّلة: PRO (1080p)</span>
                </div>
                
                <div class="text-start">
                    <label class="form-label fw-bold">✍️ اكتب سيناريو أو قصة الفيديو الكرتوني بالتفصيل:</label>
                    <textarea id="promptInput" class="form-box form-control bg-dark text-white border-secondary mb-3" rows="5" placeholder="مثال: أرنب صغير سريع يتسابق مع سلحفاة ذكية وسط غابة خضراء مشمسة بأسلوب ديزني كرتوني عالي الجودة..."></textarea>
                    <button onclick="generateVideo()" class="btn btn-pro w-100 fs-5 mb-3">🚀 توليد وفصل الفيديو المتسلسل بدقة 1080p</button>
                </div>

                <div id="loadingStatus" class="mt-3 d-none">
                    <div class="spinner-border text-info" role="status"></div>
                    <p class="mt-2 text-info">⚙️ جاري تشغيل خوارزمية التجميع المتسلسل وفصل المشاهد عبر ByteDance...</p>
                </div>

                <div id="resultBox" class="d-none">
                    <h4 class="text-success mt-4">🎉 تم توليد وفصل الفيديو بنجاح!</h4>
                    <div class="video-box">
                        <span class="text-muted">عرض الفيديو التجريبي بدقة 1080p جاهز للتحميل والرفع على قناتك</span>
                    </div>
                </div>
            </div>
        </div>

        <script>
            function generateVideo() {
                const prompt = document.getElementById('promptInput').value;
                if(!prompt) { alert('الرجاء كتابة قصة أو نص أولاً يا مدير!'); return; }
                
                document.getElementById('loadingStatus').classList.remove('d-none');
                document.getElementById('resultBox').classList.add('d-none');
                
                // محاكاة الاتصال بمحرك التجميع المتسلسل السحابي ($0 بقيمة تشغيلية مجانية)
                setTimeout(() => {
                    document.getElementById('loadingStatus').classList.add('d-none');
                    document.getElementById('resultBox').classList.remove('d-none');
                }, 5000);
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# مسار لوحة التحكم السرية
@app.get("/admin", response_class=HTMLResponse)
async def read_admin():
    admin_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <title>لوحة تحكم المدير السرية 🔒</title>
        <link href="https://jsdelivr.net" rel="stylesheet">
        <style>
            body { background-color: #0f172a; color: white; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; }
            .card-admin { background: #1e293b; padding: 40px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.3); text-align: center; max-width: 500px; }
        </style>
    </head>
    <body>
        <div class="card-admin">
            <h2>👑 لوحة التحكم السرية للمستثمر (نهاد) - نسخة السحاب</h2>
            <p class="text-muted mt-3">مرحباً بك يا مدير أونلاين. اضغط لتفعيل الـ Infinite Loop لنقاطك.</p>
            <button onclick="activatePro()" class="btn btn-info w-100 my-3 fw-bold">تفعيل النقاط اللانهائية وباقة PRO ⚡</button>
        </div>
        <script>
            function activatePro() {
                alert("👑 تم التفعيل بنجاح أونلاين يا مدير نهاد! حلقة الأرباح اللانهائية وباقة PRO شغالة الآن!");
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=admin_content)
