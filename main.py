"""
AI Cartoon Video Studio - نسخة الإنتاج (main.py)
مستضاف على Vercel + مستودع GitHub: ali1065582-bit/cartoon-video-app

============================================================
سبب الانهيار السابق (500 Internal Server Error) وكيف تم إصلاحه:
============================================================
كان HTML/CSS مكتوباً داخل f-strings في بايثون مباشرة. أي علامة `{` أو `}`
تخص CSS (مثل `body { margin: 0; }`) تُفسَّرها بايثون كمحاولة لإدراج متغير
داخل f-string، فيحدث خطأ SyntaxError/ValueError عند الاستيراد أو التشغيل،
وهذا يُسقط السيرفر بالكامل (500 على كل الروابط، وليس فقط الصفحة المتأثرة).

الحل الدائم: فصل الواجهات تماماً عن كود بايثون باستخدام Jinja2Templates
(templates/index.html و templates/admin.html)، بحيث لا يحتوي main.py على أي
HTML خام إطلاقاً. Jinja2 يستخدم `{{ }}` و `{% %}` بدل f-strings، فلا يوجد
أي تعارض مع أقواس CSS/JS المتعرجة بعد الآن.

============================================================
نقطتان تقنيتان حرجتان تم التعامل معهما بصراحة (بدل تجاهلهما):
============================================================
1) قاعدة البيانات: SQLite (ملف على القرص) لا تعمل بشكل موثوق على Vercel لأن
   الدوال الخادمة (Serverless Functions) تُنشأ وتُهدم مع كل طلب تقريباً، ولا
   تملك قرصاً دائماً. لذلك تم الانتقال إلى PostgreSQL سحابي حقيقي (متوافق مع
   Neon / Supabase / Vercel Postgres) عبر متغير البيئة DATABASE_URL.

2) تخزين الفيديوهات المولَّدة: نظام ملفات Vercel للقراءة فقط في الإنتاج (باستثناء
   /tmp المؤقت وغير المشترك بين الطلبات). لذلك لا يمكن حفظ الفيديو محلياً وتقديمه
   عبر StaticFiles كما في التطوير المحلي - تم رفعه إلى Cloudflare R2 (تخزين
   كائنات متوافق مع S3) والحصول على رابط عام دائم بدلاً من ذلك.

============================================================
توضيح مهم عن زر التوليد:
============================================================
هذا الزر يتصل فعلياً بمحرك Hugging Face المجاني (مساحة Upsampler/wan-2-2-14b-
text-to-video عبر gradio_client) - لا يوجد أي "محاكاة" أو نتيجة وهمية هنا.
لأن هذا النموذج محدود بـ5 ثوانٍ كحد أقصى لكل استدعاء واحد، يُقسَّم نص المستخدم
إلى عدة "مشاهد" قصيرة تُولَّد كل واحدة على حدة ثم تُدمَج بـ ffmpeg في فيديو
واحد متسلسل أطول (انظر تعليق "إعدادات محرك التوليد الجديد" أدناه للتفاصيل
والقيود الحقيقية). أي فشل حقيقي (ازدحام، تحميل بارد، تجاوز حصة GPU، فشل دمج)
يُعرض بصدق للمستخدم مع إعادة محاولة تلقائية، بدل إخفائه خلف نتيجة مزيّفة.
"""

import os
import re
import time
import uuid
import base64
import secrets
import asyncio
import pathlib
import subprocess
import tempfile
import json
import urllib.request
import urllib.parse

import boto3
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request, File, UploadFile, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from gradio_client import Client as GradioClient, handle_file

BASE_DIR = pathlib.Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="AI Cartoon Video Studio")


# ============================================================
# قفل الموقع للاستخدام الشخصي فقط (HTTP Basic Auth على كل المسارات)
# ============================================================
# طلب المستخدم: "أغلق التطبيق خلّه لي بس" - أي شخص عنده الرابط حالياً يقدر
# يفتح الموقع ويستهلك حصة GPU المحدودة (25 دقيقة/يوم حتى مع PRO). الحل
# الأبسط والمجاني (بدل Vercel Password Protection اللي يكلف $150/شهر): قفل
# بسيط بكلمة سر عبر HTTP Basic Auth على مستوى التطبيق نفسه.
#
# فعّال فقط إذا ضُبط SITE_PASSWORD كمتغير بيئة على Vercel (فشل آمن بالاتجاه
# المفتوح إن لم يُضبط، حتى لا يُقفل الموقع بالخطأ قبل ضبط كلمة السر عمداً -
# لكن بمجرد ضبطه، يُطلب تسجيل الدخول لكل صفحة وكل API بدون استثناء).
SITE_USERNAME = os.getenv("SITE_USERNAME", "admin")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "")


@app.middleware("http")
async def personal_access_lock(request: Request, call_next):
    if not SITE_PASSWORD:
        # لم يُضبط SITE_PASSWORD بعد - الموقع يبقى مفتوحاً كما كان (لا قفل).
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    is_valid = False
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[len("Basic "):]).decode("utf-8")
            sent_user, _, sent_pass = decoded.partition(":")
            is_valid = secrets.compare_digest(sent_user, SITE_USERNAME) and secrets.compare_digest(
                sent_pass, SITE_PASSWORD
            )
        except Exception:  # noqa: BLE001 - أي فشل بفك التشفير = دخول مرفوض
            is_valid = False

    if not is_valid:
        return Response(
            content="🔒 هذا التطبيق خاص - أدخل اسم المستخدم وكلمة السر للدخول.",
            status_code=401,
            headers={"WWW-Authenticate": "Basic realm=\"cartoon-video-app\""},
        )

    return await call_next(request)


# ============================================================
# إعدادات قاعدة البيانات (PostgreSQL سحابي حقيقي)
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL", "")
DEFAULT_USER_ID = "guest"  # مستخدم واحد ثابت مؤقتاً لحين بناء نظام تسجيل دخول فعلي


def get_db_connection():
    """
    يفتح اتصالاً جديداً بقاعدة بيانات Postgres السحابية في كل استدعاء.
    يرمي خطأ عربياً واضحاً إن لم يُضبط DATABASE_URL بدل الانهيار الغامض.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "متغير البيئة DATABASE_URL غير مضبوط. أنشئ قاعدة بيانات Postgres "
            "مجانية (مثل Neon: neon.tech أو Supabase)، واحصل على رابط الاتصال، "
            "ثم أضفه في إعدادات المشروع على Vercel (Settings -> Environment Variables)."
        )
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db() -> None:
    """ينشئ جدول المستخدمين إن لم يكن موجوداً، ويضمن وجود صف افتراضي واحد."""
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        user_id TEXT UNIQUE NOT NULL,
                        points INTEGER NOT NULL DEFAULT 0,
                        plan TEXT NOT NULL DEFAULT 'free',
                        is_unlimited BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute(
                    """INSERT INTO users (user_id, points, plan)
                       VALUES (%s, 0, 'free')
                       ON CONFLICT (user_id) DO NOTHING""",
                    (DEFAULT_USER_ID,),
                )
                # جدول سجلات الأخطاء والأداء (نظام المراقبة) - يُنشأ هنا أيضاً
                # حتى يعمل تلقائياً بمجرد ضبط DATABASE_URL دون أي خطوة يدوية إضافية.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS event_logs (
                        id SERIAL PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT NOW(),
                        endpoint TEXT NOT NULL,
                        method TEXT,
                        level TEXT NOT NULL,
                        status_code INTEGER,
                        duration_ms DOUBLE PRECISION,
                        message TEXT
                    )
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_event_logs_created_at ON event_logs (created_at)"
                )
    finally:
        conn.close()


def get_user_row(user_id: str) -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود في قاعدة البيانات.")
    return dict(row)


def update_user_points(user_id: str, delta: int) -> int:
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET points = points + %s WHERE user_id = %s",
                    (delta, user_id),
                )
                cur.execute("SELECT points FROM users WHERE user_id = %s", (user_id,))
                new_points = cur.fetchone()["points"]
    finally:
        conn.close()
    return new_points


def set_user_plan_and_unlimited(user_id: str, plan: str, is_unlimited: bool) -> None:
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET plan = %s, is_unlimited = %s WHERE user_id = %s",
                    (plan, is_unlimited, user_id),
                )
    finally:
        conn.close()


def get_current_user_id() -> str:
    """مؤقتاً: مستخدم واحد ثابت (guest) لحين بناء نظام تسجيل دخول فعلي."""
    return DEFAULT_USER_ID


# ============================================================
# نظام تسجيل الأخطاء والأداء (Logging System)
# ============================================================
# فكرة التصميم: تسجيل تلقائي على مستوى الـmiddleware (وليس داخل كل مسار على
# حدة) - أي مسار جديد يُضاف مستقبلاً يُسجَّل تلقائياً بدون أي تعديل إضافي.
# التسجيل نفسه "فاشل بأمان" (Fail Safe): أي خطأ أثناء الكتابة في قاعدة
# البيانات (مثلاً DATABASE_URL غير مضبوط) يُطبع تحذيراً فقط في سجلات Vercel
# الخام، ولا يُسقط أبداً الطلب الأصلي الذي كنا نراقبه.
def _log_event(
    endpoint: str,
    method: str,
    level: str,
    duration_ms: float,
    status_code,
    message: str = "",
) -> None:
    try:
        conn = get_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO event_logs (endpoint, method, level, status_code, duration_ms, message)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (endpoint, method, level, status_code, duration_ms, (message or "")[:2000]),
                    )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - التسجيل نفسه يجب ألا يُسقط أي طلب أبداً
        print(f"⚠️  تحذير: فشل تسجيل الحدث في event_logs ({endpoint}): {exc}")


# مسارات لا فائدة من تسجيل كل زيارة ناجحة لها (تصفح عادي/فحص دوري خفيف) -
# لا تزال تُسجَّل لو رجعت خطأً (4xx/5xx)، فقط النجاح الروتيني يُتجاهل لتفادي
# إغراق الجدول بسجلات لا قيمة تحليلية فيها.
_LOG_SKIP_SUCCESS_PATHS = {"/", "/admin", "/api/user-status", "/favicon.ico"}


@app.middleware("http")
async def performance_logging_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static"):
        return await call_next(request)

    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001 - استثناء غير متوقع لم يُحوَّل لاستجابة HTTP بعد
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        await run_in_threadpool(
            _log_event, path, request.method, "error", duration_ms, 500, f"استثناء غير معالَج: {exc}"
        )
        raise

    duration_ms = round((time.monotonic() - start) * 1000, 1)
    status_code = response.status_code
    if status_code >= 500:
        level = "error"
    elif status_code >= 400:
        level = "warning"
    else:
        level = "info"

    if level != "info" or path not in _LOG_SKIP_SUCCESS_PATHS:
        await run_in_threadpool(_log_event, path, request.method, level, duration_ms, status_code, "")

    return response


# تهيئة الجداول عند الإقلاع - بشكل آمن (لا يُسقط التطبيق بالكامل لو فشل الاتصال
# مؤقتاً، بل يطبع تحذيراً؛ الطلبات الفعلية التي تحتاج DB ستُظهر رسالة خطأ واضحة)
try:
    init_db()
except Exception as exc:  # noqa: BLE001 - نريد التقاط أي خطأ اتصال هنا تحديداً
    print(f"⚠️  تحذير: تعذر تهيئة قاعدة البيانات عند الإقلاع: {exc}")


# ============================================================
# إعدادات Cloudflare R2 (تخزين الفيديوهات المولَّدة)
# ============================================================
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
# رابط الوصول العام لعارضة الـ bucket (مثال: https://pub-xxxxxxxx.r2.dev أو دومين مخصص)
R2_PUBLIC_URL_BASE = os.getenv("R2_PUBLIC_URL_BASE", "")


# ============================================================
# إعدادات محرك التوليد: LTX-Video (Lightricks/ltx-video-distilled)
# ============================================================
# التبديل من Wan 2.2 14B إلى LTX-Video (نسخة "distilled" السريعة): الهدف
# توفير حصة GPU المجانية (ZeroGPU) اليومية - LTX-Video أخف وأسرع بكثير من
# Wan 2.2 14B لنفس مدة الفيديو، فيتبقى وقت GPU أكثر لعدد مشاهد أكبر بنفس
# الحصة اليومية (٣٫٥ دقيقة مجاناً، أو ٢٥ دقيقة عبر HF PRO).
#
# تم التحقق حياً (view_api) من Lightricks/ltx-video-distilled: نموذج فعلي
# نصّ-إلى-فيديو (وليس صورة-إلى-فيديو رغم أن القيمة الافتراضية لمعامل mode في
# المساحة نفسها "image-to-video" - لهذا يجب تمرير mode="text-to-video" صراحةً
# في كل استدعاء، وإلا يُنتج المشهد بوضع خاطئ تماماً). الحد الأقصى الحقيقي
# لمدة المقطع الواحد 8.5 ثانية (أعلى من حد Wan البالغ 5.0 ثانية)، لكن نُبقي
# طول المشهد عند نفس القيمة السابقة (4.8 ثانية) للحفاظ على نفس حسابات عدد
# المشاهد/الباقات الموجودة دون تغيير سلوك الواجهة والأسعار.
#
# improve_texture_flag تُترك False عمداً (خلاف الافتراضي True في المساحة
# الحية): تفعيلها يشغّل تمريرة "multi-scale" إضافية تضاعف تقريباً وقت توليد
# كل مشهد على الـGPU - إيقافها هو المصدر الرئيسي لتوفير الوقت الذي كان الهدف
# الأساسي من هذا التبديل، مقابل جودة تفاصيل أبسط قليلاً (مقايضة مقصودة).
HF_API_TOKEN = os.getenv("HF_API_TOKEN", "") or None
LTX_SPACE_ID = os.getenv("LTX_SPACE_ID", "Lightricks/ltx-video-distilled")
LTX_SPACE_API_NAME = "/text_to_video"
LTX_SPACE_API_NAME_IMAGE = "/image_to_video"  # نقطة نهاية منفصلة لوضع صورة-إلى-فيديو -
# /text_to_video لا يقبل رفع صورة فعلياً (باراميتر input_image_filepath فيها Textbox نصي فقط)،
# فيرسل مسار محلي لا تراه مساحة LTX-Video فيفشل بخطأ "Could not load image".

# ============================================================
# مساحة Wav2Lip لمزامنة الشفاه (Lip-Sync) - ميزة "صورة + برومت" تستخدمها
# حصراً حين المستخدم يفعّل خيار "الوجه يتكلم فعلياً" في الواجهة. تختلف كلياً
# عن LTX-Video: تاخذ صورة ثابتة + ملف صوتي (الرواية المولَّدة من النص) وترجع
# فيديو الشفاه فيه متزامنة فعلياً مع الصوت - تقنية Wav2Lip الحقيقية (مو محاكاة).
# تم التحقق حياً (view_api) من manavisrani07/gradio-lipsync-wav2lip: مساحة
# فعلية تعمل، endpoint فعلي /generate بمدخلات (video, audio, checkpoint...).
# ============================================================
LIPSYNC_SPACE_ID = os.getenv("LIPSYNC_SPACE_ID", "manavisrani07/gradio-lipsync-wav2lip")
LIPSYNC_API_NAME = "/generate"
LIPSYNC_CHECKPOINT = "wav2lip_gan"  # جودة أعلى من "wav2lip" العادي (نفس مدة المعالجة تقريباً)

LTX_NEGATIVE_PROMPT = (
    "worst quality, inconsistent motion, blurry, jittery, distorted, "
    "realistic human, photorealistic, live-action, real person, human face close-up, "
    "portrait photography, nudity, nsfw, sexual content, adult content"
)
LTX_HEIGHT = 512
LTX_WIDTH = 704
LTX_GUIDANCE_SCALE = 1.0  # القيمة الافتراضية الموصى بها لهذا النموذج (distilled/fast)
LTX_IMPROVE_TEXTURE = False  # معطّلة عمداً لتسريع التوليد وتوفير حصة GPU (انظر الشرح أعلاه)
SCENE_DURATION_SECONDS = 4.8  # نفس مدة المشهد السابقة (ضمن حد LTX الحقيقي 0.3-8.5s)

MAX_RETRIES = 3
RETRY_WAIT_QUEUE_BUSY = 20
RETRY_WAIT_COLD_START = 25

# كل باقة تحدد كم "مشهداً" (~4.8 ثانية للمشهد) يمكن دمجها في فيديو واحد متسلسل.
# أسماء الباقات القديمة (480p/720p/1080p) أصبحت الآن تعني عدد المشاهد/مدة
# الفيديو الإجمالية - وليست دقة بكسل حقيقية (المخرج دائماً 832x480 لكل مشهد)،
# تجنباً لأي وصف مضلِّل لدقة غير موجودة فعلياً في الفيديو الناتج.
SCENE_TIER_TO_COUNT = {"480p": 1, "720p": 2, "1080p": 8, "gold": 12}
# "gold" (سوبر ذهبي): باقة خاصة غير قابلة للشراء إطلاقاً - لا تظهر بصفحة الدفع
# ولا يوجد لها أي مسار checkout. تُمنح حصراً عبر /api/admin/grant-gold بكلمة
# سر ADMIN_SECRET، لصاحب المشروع نفسه. 12 مشهد ≈58 ثانية - تحذير: خطر تجاوز
# مهلة Vercel (300 ثانية) أعلى وضوحاً من باقة PRO (8 مشاهد)، صاحب المشروع
# اختار هذا الرقم بوعي كامل بهذا الخطر.
# ملاحظة صادقة: 8 مشاهد × ~4.8 ثانية = ~38 ثانية فيديو، لكن التوليد الفعلي
# لكل مشهد يستغرق ~30-45 ثانية على GPU مجاني (ZeroGPU) شاملاً وقت الطابور -
# أي أن طلب 8 مشاهد قد يستغرق 240-360+ ثانية إجمالاً، وهذا قريب جداً أو قد
# يتجاوز سقف Vercel (300 ثانية حتى مع Fluid Compute)، خصوصاً لو احتاج أي
# مشهد إعادة محاولة (إزدحام/تحميل بارد). المستخدم صاحب المشروع اختار هذا
# الرقم بوعي كامل بهذا الخطر (بدل قيمة أكثر أماناً كـ5 مشاهد).
QUALITY_POINTS_COST = {"480p": 5, "720p": 10, "1080p": 20, "gold": 0}
PLAN_ALLOWED_QUALITY = {
    "free": ["480p"],
    "medium": ["480p", "720p"],
    "pro": ["480p", "720p", "1080p"],
    "gold": ["480p", "720p", "1080p", "gold"],
}

# كلمة سر لوحة التحكم الإدارية - يجب ضبطها كمتغير بيئة حقيقي على Vercel.
# إن تُركت فارغة، يُرفض أي طلب ترقية إدارية تلقائياً (فشل آمن - Fail Closed).
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")


# ============================================================
# إعدادات النشر على يوتيوب (YouTube Data API v3 - OAuth 2.0)
# ============================================================
# ثلاثتها فارغة افتراضياً (فشل آمن واضح - Fail Closed): بدونها /api/youtube/publish
# يرفض أي طلب برسالة تشرح بالضبط شنو الناقص، بدل خطأ غامض من مكتبة Google.
# YOUTUBE_REFRESH_TOKEN يُستخرَج مرة واحدة يدوياً عبر Google OAuth Playground
# (تفويض حقيقي بموافقة صاحب الحساب نفسه - لا يمكن لأي كود توليده تلقائياً).
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")


# ============================================================
# بوابات الدفع العراقية - وضع Sandbox صراحةً (بدون معالج دفع حقيقي مرخّص بعد)
# كل استجابة من /api/checkout تُسمّى "sandbox" بوضوح حتى لا يُخدع أي مستخدم
# بأن هذا دفع حقيقي بمال حقيقي.
# ============================================================
PAYMENT_GATEWAYS = ["zaincash", "asiahawala", "qicard"]

PAYMENT_PLANS = {
    "medium": {"price_iqd": 5000, "plan": "medium", "points": 100, "is_unlimited": False},
    "pro": {"price_iqd": 15000, "plan": "pro", "points": 500, "is_unlimited": True},
}

SANDBOX_OTP_CODE = os.getenv("SANDBOX_OTP_CODE", "123456")


# ============================================================
# نماذج الطلبات
# ============================================================
class GenerateVideoRequest(BaseModel):
    prompt: str
    quality: str = "1080p"
    voice: str = "ar-IQ-BasselNeural"  # = DEFAULT_NARRATION_VOICE (مُعرَّف لاحقاً بالملف مع بقية إعدادات الصوت)


class AdminGrantRequest(BaseModel):
    key: str


class CheckoutRequest(BaseModel):
    plan: str
    gateway: str
    phone: str
    otp: str


# نماذج الطلبات الخاصة بتوليد المشاهد المُقسَّم (طلب منفصل لكل مشهد)
class PlanScenesRequest(BaseModel):
    prompt: str
    quality: str = "480p"


class GenerateSceneRequest(BaseModel):
    scene_prompt: str


class MergeScenesRequest(BaseModel):
    scene_keys: list[str]
    scenes_text: list[str] = []
    quality: str = "480p"
    voice: str = "ar-IQ-BasselNeural"  # = DEFAULT_NARRATION_VOICE (مُعرَّف لاحقاً بالملف مع بقية إعدادات الصوت)


class MergeVideosRequest(BaseModel):
    video_urls: list[str]


class YoutubePublishRequest(BaseModel):
    video_url: str
    title: str
    description: str = ""
    privacy_status: str = "private"  # افتراضي آمن: خاص - المستخدم يقرر التغيير لعام بوعي


# ============================================================
# رفع الفيديو المولَّد إلى Cloudflare R2 (بدل القرص المحلي غير الموثوق في الإنتاج)
# ============================================================
class VideoGenerationError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _get_r2_client():
    missing = [
        name
        for name, val in [
            ("R2_ACCOUNT_ID", R2_ACCOUNT_ID),
            ("R2_ACCESS_KEY_ID", R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
            ("R2_BUCKET_NAME", R2_BUCKET_NAME),
            ("R2_PUBLIC_URL_BASE", R2_PUBLIC_URL_BASE),
        ]
        if not val
    ]
    if missing:
        raise VideoGenerationError(
            f"إعدادات Cloudflare R2 ناقصة على السيرفر: {', '.join(missing)}. "
            "أضفها في متغيرات بيئة Vercel قبل استخدام التوليد.",
            status_code=500,
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def _upload_video_to_r2_sync(video_bytes: bytes) -> str:
    client = _get_r2_client()
    key = f"generated/{uuid.uuid4().hex}.mp4"
    try:
        client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=video_bytes, ContentType="video/mp4")
    except Exception as exc:  # noqa: BLE001 - نلتقط أي خطأ boto3 حقيقي ونعرضه بصدق
        raise VideoGenerationError(
            f"⚠️ فشل رفع الفيديو إلى Cloudflare R2: {exc}. "
            "تأكد أن R2_ACCESS_KEY_ID وR2_SECRET_ACCESS_KEY وR2_BUCKET_NAME صحيحة "
            "ومطابقة لبعضها في إعدادات بيئة Vercel.",
            status_code=502,
        )
    return f"{R2_PUBLIC_URL_BASE.rstrip('/')}/{key}"


async def upload_generated_video(video_bytes: bytes) -> str:
    """رفع غير متزامن (عبر threadpool لأن boto3 مكتبة متزامنة) - يعيد رابطاً عاماً دائماً."""
    return await run_in_threadpool(_upload_video_to_r2_sync, video_bytes)


# ============================================================
# دعم تقسيم التوليد لطلبات منفصلة لكل مشهد (تجاوز مهلة تنفيذ Vercel)
# ============================================================
# بدل توليد كل المشاهد ودمجها ضمن طلب HTTP واحد طويل (قد يقترب من/يتجاوز
# مهلة Vercel القصوى)، كل مشهد الآن يُولَّد بطلب مستقل قصير (٣٠-٥٠ ثانية)،
# يُرفع مؤقتاً إلى R2 تحت مجلد scenes-tmp/، ثم طلب أخير منفصل وقصير يجمع كل
# المشاهد المرفوعة، يدمجها، يضيف الصوت، ويحذف الملفات المؤقتة. هذا يجعل مدة
# كل طلب منفرد صغيرة وثابتة بغض النظر عن عدد المشاهد الكلي.
def _upload_bytes_to_r2_sync(data: bytes, key: str) -> None:
    client = _get_r2_client()
    try:
        client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=data, ContentType="video/mp4")
    except Exception as exc:  # noqa: BLE001
        raise VideoGenerationError(
            f"⚠️ فشل رفع مشهد مؤقت إلى Cloudflare R2: {exc}.",
            status_code=502,
        )


def _download_bytes_from_r2_sync(key: str) -> bytes:
    client = _get_r2_client()
    try:
        return client.get_object(Bucket=R2_BUCKET_NAME, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001
        raise VideoGenerationError(
            f"⚠️ فشل تحميل مشهد مؤقت من Cloudflare R2 (قد يكون منتهي الصلاحية): {exc}.",
            status_code=502,
        )


def _delete_object_from_r2_sync(key: str) -> None:
    client = _get_r2_client()
    try:
        client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
    except Exception:  # noqa: BLE001 - تنظيف غير حرج، لا نُسقط الطلب بسببه
        pass


# ============================================================
# سلامة البرومبت: ترجمة + توصيف كرتوني إلزامي قبل إرساله لـ LTX-Video
# ------------------------------------------------------------
# السبب: LTX-Video-distilled مدرَّب أساساً على نصوص إنكليزية. إرسال نص عربي
# خام كما كان يحدث سابقاً يعني أن الموديل عملياً "لا يفهم" البرومبت، فيرجع
# فيديو شبه عشوائي من بيانات تدريبه (غالباً لقطات بشر حقيقيين/بورتريهات) -
# غير مناسب إطلاقاً لتطبيق كرتوني موجّه للأطفال. الحل: (1) ترجمة أفضل-جهد
# للإنكليزية (2) إضافة بادئة أسلوب كرتوني صريحة (3) negative_prompt أقوى
# يمنع البشر الواقعيين/المحتوى الجنسي (انظر LTX_NEGATIVE_PROMPT أعلاه).
# الترجمة "أفضل-جهد" فقط: أي فشل (شبكة، حصة، تنسيق غير متوقع) يُعاد فيه
# النص الأصلي بصمت دون إسقاط الطلب - التوليد يجب ألا يتعطل بسبب الترجمة.
# ============================================================
CARTOON_STYLE_PREFIX = (
    "children's cartoon style, 2D animation, colorful, cute animal characters, "
    "family-friendly, wholesome, no realistic humans, no photorealistic imagery, "
    "no live-action footage. Scene: "
)


def _translate_to_english_best_effort(text: str) -> str:
    try:
        params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text})
        url = f"https://translate.googleapis.com/translate_a/single?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        translated = "".join(segment[0] for segment in data[0] if segment and segment[0])
        translated = translated.strip()
        return translated or text
    except Exception:  # noqa: BLE001 - الترجمة أفضل-جهد فقط، لا تُسقط التوليد أبداً
        return text


def _build_safe_scene_prompt(scene_text: str) -> str:
    english_scene = _translate_to_english_best_effort(scene_text)
    return CARTOON_STYLE_PREFIX + english_scene


# ============================================================
# استدعاء LTX-Video عبر gradio_client لتوليد مشهد واحد (~4.8 ثانية)
# ============================================================
def _generate_scene_via_gradio_sync(prompt: str, image_path: str | None = None) -> str:
    # image_path مُمرَّرة => وضع "صورة-إلى-فيديو" (المستخدم رفع صورة مرجعية مع
    # البرومت) بدل "نص-إلى-فيديو" الافتراضي. نفس المساحة (Lightricks) تدعم
    # الوضعين، والتبديل بينهما يكون فقط عبر mode + input_image_filepath.
    client = GradioClient(LTX_SPACE_ID, token=HF_API_TOKEN)
    result = client.predict(
        prompt=prompt,
        negative_prompt=LTX_NEGATIVE_PROMPT,
        input_image_filepath=handle_file(image_path) if image_path else None,
        input_video_filepath=None,   # لا فيديو إدخال بأي من الوضعين المدعومين هنا
        height_ui=LTX_HEIGHT,
        width_ui=LTX_WIDTH,
        mode="image-to-video" if image_path else "text-to-video",
        duration_ui=SCENE_DURATION_SECONDS,
        ui_frames_to_use=9,  # لا تأثير لها إلا بوضع فيديو-إلى-فيديو (غير مستخدم هنا)
        seed_ui=42,    # يُتجاهل عملياً لأن randomize_seed=True أدناه
        randomize_seed=True,  # كل مشهد يحصل على بذرة عشوائية لتنويع الحركة
        ui_guidance_scale=LTX_GUIDANCE_SCALE,
        improve_texture_flag=LTX_IMPROVE_TEXTURE,
        api_name=LTX_SPACE_API_NAME_IMAGE if image_path else LTX_SPACE_API_NAME,
    )
    # الإرجاع الحقيقي (video, seed) - الفيديو إما مسار نصي مباشر أو dict بمفتاح path
    video_value = result[0] if isinstance(result, (tuple, list)) else result
    return _extract_video_path(video_value)


def _extract_video_path(value) -> str:
    if isinstance(value, dict) and "video" in value:
        value = value["video"]
    if isinstance(value, dict) and "path" in value:
        value = value["path"]
    if isinstance(value, str) and value:
        return value
    raise VideoGenerationError("⚠️ شكل استجابة غير متوقع من مساحة التوليد.", status_code=502)


def _generate_lipsync_video_sync(image_path: str, audio_path: str) -> str:
    """يستدعي مساحة Wav2Lip الحقيقية لمزامنة شفاه الصورة مع ملف الصوت المُعطى.
    يعيد مساراً محلياً لفيديو الشفاه الناتج (بدون رواية مدموجة - الصوت أصلاً
    جزء من الفيديو الناتج من Wav2Lip نفسه، فلا حاجة لـ_mux_narration_sync)."""
    client = GradioClient(LIPSYNC_SPACE_ID)
    result = client.predict(
        video=handle_file(image_path),
        audio=handle_file(audio_path),
        checkpoint=LIPSYNC_CHECKPOINT,
        no_smooth=0,
        resize_factor=10,
        pad_top=0,
        pad_bottom=0,
        pad_left=1,
        api_name=LIPSYNC_API_NAME,
    )
    video_value = result.get("video") if isinstance(result, dict) else result
    return _extract_video_path(video_value)


def _classify_gradio_error(exc: Exception) -> tuple[bool, float, str]:
    message = str(exc).lower()

    if "quota" in message or ("gpu" in message and ("exceed" in message or "quota" in message)):
        return False, 0, (
            "⚠️ تم تجاوز حصة GPU المجانية (ZeroGPU) لهذه المساحة حالياً. "
            "الرجاء المحاولة بعد بضع دقائق، أو أضف HF_API_TOKEN لحساب يملك حصة أكبر."
        )
    if "queue" in message or "429" in message or "too many requests" in message:
        return True, RETRY_WAIT_QUEUE_BUSY, "⏳ الطابور مزدحم حالياً، تتم إعادة المحاولة تلقائياً..."
    if "loading" in message or "cold" in message or "503" in message or "not running" in message or "sleeping" in message or "paused" in message:
        return True, RETRY_WAIT_COLD_START, "🕒 المساحة قيد الإقلاع من جديد، تتم إعادة المحاولة تلقائياً..."
    if "timeout" in message or "connection" in message:
        return True, RETRY_WAIT_QUEUE_BUSY, "🔌 انقطاع اتصال مؤقت، تتم إعادة المحاولة تلقائياً..."

    return False, 0, f"⚠️ خطأ غير متوقع أثناء توليد الفيديو: {exc}"


async def _generate_one_scene_with_retry(prompt: str, image_path: str | None = None) -> pathlib.Path:
    """يولّد مشهداً واحداً مع إعادة محاولة تلقائية، ويعيد مساراً محلياً لملف الفيديو.
    قبل الإرسال الفعلي لـ LTX-Video، يُحوَّل نص المشهد إلى برومبت آمن
    (ترجمة أفضل-جهد + بادئة أسلوب كرتوني) عبر _build_safe_scene_prompt -
    انظر الشرح أعلاه لسبب هذه الخطوة (حماية من محتوى غير مناسب للأطفال).
    إن مُرِّرت image_path (ميزة "صورة + برومت")، يُستخدَم وضع صورة-إلى-فيديو
    بدل نص-إلى-فيديو - انظر _generate_scene_via_gradio_sync."""
    safe_prompt = await run_in_threadpool(_build_safe_scene_prompt, prompt)
    last_message = "⚠️ فشل توليد المشهد لسبب غير معروف."
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            video_path = await run_in_threadpool(_generate_scene_via_gradio_sync, safe_prompt, image_path)
            return pathlib.Path(video_path)
        except VideoGenerationError:
            raise
        except Exception as exc:  # noqa: BLE001
            should_retry, wait_seconds, message = _classify_gradio_error(exc)
            last_message = message
            if not should_retry or attempt == MAX_RETRIES:
                raise VideoGenerationError(message, status_code=503 if should_retry else 502)
            await asyncio.sleep(wait_seconds)
    raise VideoGenerationError(last_message, status_code=503)


def _split_into_scenes(prompt: str, max_scenes: int) -> list[str]:
    """
    يقسّم نص المستخدم إلى عدة "مشاهد" قصيرة (كل واحد يُولَّد كفيديو ~4.8 ثانية
    مستقل، ثم تُدمَج كلها لاحقاً). أولوية التقسيم:
    1) كل سطر غير فارغ = مشهد منفصل (الأنسب - يعطي المستخدم تحكماً مباشراً
       إن كتب قصته على شكل أسطر/مشاهد مرقّمة).
    2) إن كان النص سطراً واحداً فقط، يُقسَّم على علامات نهاية الجملة (. ! ؟).
    يُقتصَر الناتج دائماً على max_scenes (حسب باقة المستخدم) - لا يُهمَل الباقي
    بصمت، بل يُقتطَع بوضوح والعدد الفعلي يُعاد لاحقاً للواجهة.
    """
    lines = [ln.strip() for ln in prompt.splitlines() if ln.strip()]
    if len(lines) <= 1:
        parts = re.split(r"(?<=[.!?؟])\s+", prompt.strip())
        lines = [p.strip() for p in parts if p.strip()]
    if not lines:
        lines = [prompt.strip()]
    return lines[:max_scenes]


def _concat_videos_sync(clip_paths: list[pathlib.Path]) -> bytes:
    """
    يدمج عدة مقاطع فيديو قصيرة في فيديو واحد متسلسل عبر ffmpeg (concat demuxer).
    يستخدم مكتبة imageio_ffmpeg للحصول على ثنائي ffmpeg جاهز (Static Binary)
    بدل الاعتماد على تثبيت ffmpeg على نظام التشغيل - وهو غير مضمون على بيئة
    Python الخادمة لـ Vercel بدون هذه المكتبة.
    """
    if len(clip_paths) == 1:
        # مشهد واحد فقط - لا حاجة لأي دمج، نعيد بايتات الملف كما هي
        return clip_paths[0].read_bytes()

    import imageio_ffmpeg

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = pathlib.Path(tmp_dir)
        list_file = tmp_path / "concat_list.txt"
        with list_file.open("w", encoding="utf-8") as fh:
            for clip in clip_paths:
                fh.write(f"file '{clip.as_posix()}'\n")

        output_path = tmp_path / f"final_{uuid.uuid4().hex}.mp4"

        result = subprocess.run(
            [
                ffmpeg_exe, "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not output_path.exists():
            raise VideoGenerationError(
                f"⚠️ فشل دمج المشاهد عبر ffmpeg: {result.stderr[-500:]}",
                status_code=502,
            )
        return output_path.read_bytes()


# ============================================================
# الرواية الصوتية (Narration) - تحويل نص القصة إلى صوت عربي حقيقي عبر Edge TTS
# ============================================================
# التبديل من gTTS إلى Edge TTS (مكتبة edge-tts): تستخدم نفس أصوات "Read
# Aloud" العصبية (Neural) المستخدمة داخل متصفح Microsoft Edge - مجانية بالكامل
# وبلا حد استخدام يومي، وجودتها الصوتية أعلى بمراحل من gTTS (صوت طبيعي حقيقي
# بدل نبرة آلية مسطّحة). تعتمد على واجهة غير رسمية (لا يوجد API key)، لذلك فشل
# توليد الصوت لا يُسقط الطلب بالكامل: يُعاد الفيديو صامتاً مع الإفصاح الصريح
# بذلك، بدل ادّعاء نجاح كامل غير حقيقي - نفس مبدأ gTTS السابق.
#
# قرار تصميم مهم (لم يتغيّر): تُولَّد رواية صوتية واحدة لكامل نص القصة (كل
# الأسطر مجتمعة) وتُلصَق فوق الفيديو النهائي المدموج، بدل توليد صوت منفصل لكل
# مشهد ومزامنته بدقة.
#
# قائمة الأصوات: تم التحقق حياً (edge-tts --list-voices) من وجود كل صوت أدناه
# فعلياً بما فيها لهجة عراقية حقيقية (ar-IQ) - غير متوفرة في gTTS إطلاقاً.
NARRATION_VOICES = {
    "ar-IQ-BasselNeural": "🇮🇶 بسل - عراقي (رجالي)",
    "ar-IQ-RanaNeural": "🇮🇶 رنا - عراقي (نسائي)",
    "ar-SA-HamedNeural": "🇸🇦 حامد - سعودي فصيح (رجالي)",
    "ar-SA-ZariyahNeural": "🇸🇦 زارية - سعودي فصيح (نسائي)",
    "ar-EG-ShakirNeural": "🇪🇬 شاكر - مصري (رجالي)",
    "ar-EG-SalmaNeural": "🇪🇬 سلمى - مصري (نسائي)",
}
DEFAULT_NARRATION_VOICE = "ar-IQ-BasselNeural"


def _resolve_narration_voice(voice: str | None) -> str:
    return voice if voice in NARRATION_VOICES else DEFAULT_NARRATION_VOICE


async def _generate_narration_async(full_text: str, voice: str) -> pathlib.Path:
    import edge_tts

    tmp_dir = pathlib.Path(tempfile.mkdtemp())
    narration_path = tmp_dir / f"narration_{uuid.uuid4().hex}.mp3"
    communicate = edge_tts.Communicate(full_text, voice)
    await communicate.save(str(narration_path))
    return narration_path


def _mux_narration_sync(video_bytes: bytes, narration_path: pathlib.Path) -> bytes:
    """
    يلصق مسار الصوت (الرواية) فوق الفيديو الصامت المدموج عبر ffmpeg.
    يستخدم -shortest: إن كان الصوت أطول من الفيديو يُقتَص عند نهاية الفيديو
    (قد تُقطَع الرواية قبل اكتمالها لو كان نص القصة طويلاً جداً)، وإن كان
    الفيديو أطول يستمر صامتاً بعد انتهاء الرواية - هذا حل واقعي بسيط بدل
    محاولة مزامنة مثالية غير مضمونة.
    """
    import imageio_ffmpeg

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = pathlib.Path(tmp_dir)
        video_in = tmp_path / "silent_video.mp4"
        video_in.write_bytes(video_bytes)
        output_path = tmp_path / f"with_audio_{uuid.uuid4().hex}.mp4"

        result = subprocess.run(
            [
                ffmpeg_exe, "-y",
                "-i", str(video_in),
                "-i", str(narration_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac",
                "-shortest",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not output_path.exists():
            raise VideoGenerationError(
                f"⚠️ فشل إضافة الصوت إلى الفيديو عبر ffmpeg: {result.stderr[-500:]}",
                status_code=502,
            )
        return output_path.read_bytes()


async def generate_stitched_video(prompt: str, max_scenes: int, voice: str = DEFAULT_NARRATION_VOICE) -> tuple[bytes, int, bool]:
    """
    يولّد فيديو متعدد المشاهد كاملاً: يقسّم النص، يولّد كل مشهد تسلسلياً عبر
    LTX-Video، يدمج الكل بـ ffmpeg، ثم يضيف رواية صوتية عربية حقيقية (gTTS)
    فوق الفيديو الناتج. يعيد (بايتات الفيديو النهائي، عدد المشاهد الفعلي،
    هل تم تضمين الصوت فعلاً) - عدد المشاهد الفعلي وحالة الصوت تُستخدَمان لاحقاً
    لعرض الحقيقة الكاملة للمستخدم دون أي ادّعاء غير دقيق.
    """
    # ملاحظة مهمة (تم التراجع عن التوازي): جربنا سابقاً توليد المشاهد
    # بالتوازي عبر asyncio.gather لتقليل الوقت الكلي، لكن اتضح من سجلات
    # الإنتاج الفعلية أن مساحة ZeroGPU تسمح بطلب GPU واحد فقط في نفس اللحظة
    # لكل مستخدم/توكن - أي طلب إضافي متزامن يُرفض فوراً برسالة "تجاوز الحصة"
    # (فشل خلال أقل من ثانية، وليس بعد استهلاك حصة حقيقية). لذلك رجعنا
    # للتوليد التسلسلي الآمن: مشهد واحد يكتمل قبل بدء التالي.
    scenes = _split_into_scenes(prompt, max_scenes)
    clip_paths: list[pathlib.Path] = []
    for scene_prompt in scenes:
        clip_path = await _generate_one_scene_with_retry(scene_prompt)
        clip_paths.append(clip_path)

    video_bytes = await run_in_threadpool(_concat_videos_sync, clip_paths)

    full_narration_text = ". ".join(scenes)
    has_narration = False
    try:
        narration_path = await _generate_narration_async(full_narration_text, _resolve_narration_voice(voice))
        video_bytes = await run_in_threadpool(_mux_narration_sync, video_bytes, narration_path)
        has_narration = True
    except Exception as exc:  # noqa: BLE001 - فشل الصوت لا يُسقط الفيديو الصامت الناجح
        print(f"⚠️ تحذير: فشل توليد/إضافة الرواية الصوتية (تم إرجاع الفيديو صامتاً): {exc}")

    return video_bytes, len(clip_paths), has_narration


# ============================================================
# المسارات (Routes)
# ============================================================

def _db_error_page(request: Request, exc: Exception) -> HTMLResponse:
    """
    بدل ترك أي خطأ غير متوقع (DATABASE_URL غير مضبوط، الجدول غير موجود، تعذر
    الاتصال بـ Postgres...) يُسقط السيرفر بصفحة "Internal Server Error" فارغة
    من Vercel، نعيد صفحة تشخيصية واضحة بالعربية توضح السبب الحقيقي والحل.
    هذا لا "يُخفي" الخطأ - يعرضه بصدق، لكن بشكل قابل للفهم بدل شاشة سوداء.
    """
    return templates.TemplateResponse(
        request,
        "error.html",
        {"error_message": str(exc)},
        status_code=503,
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        user = get_user_row(get_current_user_id())
    except Exception as exc:  # noqa: BLE001 - نريد عرض أي خطأ DB بوضوح بدل انهيار عام
        return _db_error_page(request, exc)
    # ملاحظة: التوقيع الحديث لـ TemplateResponse هو (request, name, context) -
    # الشكل القديم (name, {"request": request, ...}) بات غير مدعوم فعلياً في
    # إصدارات Starlette الحالية ويسبب خطأً غامضاً (unhashable type: dict).
    return templates.TemplateResponse(request, "index.html", {"user": user})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """
    صفحة لوحة التحكم نفسها غير حسّاسة (مجرد نموذج كلمة سر)؛ الحماية الحقيقية
    تحدث في /api/admin/grant-pro حيث يُتحقَّق من ADMIN_SECRET قبل أي تعديل
    فعلي على قاعدة البيانات - تماماً كمبدأ "لا تثق بالواجهة، تحقق في السيرفر"
    المطبَّق في بقية أجزاء هذا المشروع.

    السبب الأرجح لخطأ 500 السابق على هذا المسار تحديداً: get_user_row() كانت
    تفشل بدون Try/Except (DATABASE_URL غير مضبوط على Vercel، أو الجدول لم
    يُنشأ لأن init_db() فشل بصمت عند الإقلاع) فيسقط المسار بالكامل. الآن
    يُلتقط أي خطأ ويُعرض بوضوح بدل الانهيار.
    """
    try:
        user = get_user_row(get_current_user_id())
    except Exception as exc:  # noqa: BLE001
        return _db_error_page(request, exc)
    return templates.TemplateResponse(request, "admin.html", {"user": user})


@app.get("/api/user-status")
async def user_status():
    user = get_user_row(get_current_user_id())
    return {
        "points": user["points"],
        "plan": user["plan"],
        "is_unlimited": user["is_unlimited"],
    }


@app.post("/api/admin/grant-pro")
async def admin_grant_pro(payload: AdminGrantRequest):
    """
    ترقية حقيقية وفعلية في قاعدة البيانات (وليست رسالة شكلية بدون أثر):
    تُفعِّل باقة PRO ووضع "غير محدود" (تجاوز فحص تكلفة النقاط في التوليد).
    مرفوضة افتراضياً (Fail Closed) ما لم يُضبط ADMIN_SECRET فعلياً على السيرفر.
    """
    if not ADMIN_SECRET or payload.key != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="كلمة السر الإدارية غير صحيحة أو غير مُفعَّلة على السيرفر.")

    user_id = get_current_user_id()
    set_user_plan_and_unlimited(user_id, "pro", True)
    user = get_user_row(user_id)

    return {
        "success": True,
        "plan": user["plan"],
        "points": user["points"],
        "is_unlimited": user["is_unlimited"],
    }


@app.post("/api/admin/grant-gold")
async def admin_grant_gold(payload: AdminGrantRequest):
    """
    باقة 'سوبر ذهبي' (gold) - أعلى من PRO (12 مشهد ≈58 ثانية بدل 8 ≈38 ثانية).
    خاصة بصاحب المشروع فقط: لا يوجد لها أي سعر في PAYMENT_PLANS ولا مسار شراء
    من /api/checkout - الطريقة الوحيدة لتفعيلها هي هذا المسار، ومرفوض افتراضياً
    (Fail Closed) ما لم يُضبط ADMIN_SECRET فعلياً على السيرفر - بنفس مبدأ
    /api/admin/grant-pro أعلاه بالضبط.
    """
    if not ADMIN_SECRET or payload.key != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="كلمة السر الإدارية غير صحيحة أو غير مُفعَّلة على السيرفر.")

    user_id = get_current_user_id()
    set_user_plan_and_unlimited(user_id, "gold", True)
    user = get_user_row(user_id)

    return {
        "success": True,
        "plan": user["plan"],
        "points": user["points"],
        "is_unlimited": user["is_unlimited"],
    }


@app.get("/api/admin/logs")
async def admin_view_logs(key: str = "", limit: int = 50):
    """
    عرض سجلات الأخطاء والأداء المُجمَّعة تلقائياً عبر performance_logging_middleware.
    مرفوض افتراضياً (Fail Closed) بنفس مبدأ /api/admin/grant-pro - لا يعمل
    إطلاقاً ما لم يُضبط ADMIN_SECRET فعلياً، ويُمرَّر كمعامل ?key= في الرابط.
    """
    if not ADMIN_SECRET or key != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="كلمة السر الإدارية غير صحيحة أو غير مُفعَّلة على السيرفر.")

    limit = max(1, min(limit, 200))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT endpoint, method, level, status_code, duration_ms, message, created_at
                   FROM event_logs ORDER BY id DESC LIMIT %s""",
                (limit,),
            )
            recent_rows = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """SELECT
                       endpoint,
                       COUNT(*) AS total_requests,
                       COUNT(*) FILTER (WHERE level = 'error') AS error_count,
                       COUNT(*) FILTER (WHERE level = 'warning') AS warning_count,
                       ROUND(AVG(duration_ms)::numeric, 1) AS avg_duration_ms,
                       ROUND(MAX(duration_ms)::numeric, 1) AS max_duration_ms
                   FROM event_logs
                   WHERE created_at > NOW() - INTERVAL '24 hours'
                   GROUP BY endpoint
                   ORDER BY total_requests DESC"""
            )
            summary_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for row in recent_rows:
        if row.get("created_at") is not None:
            row["created_at"] = row["created_at"].isoformat()

    return {"logs": recent_rows, "summary_24h": summary_rows}


@app.post("/api/checkout")
async def checkout(payload: CheckoutRequest):
    """
    دفع تجريبي (Sandbox) حقيقي في قاعدة البيانات - وليس محاكاة واجهة فقط:
    عند نجاح OTP التجريبي، تُحدَّث نقاط/باقة المستخدم فعلياً في Postgres.
    لا يوجد اتصال بمعالج دفع حقيقي بعد (Zain Cash / AsiaHawala / Qi Card)،
    ولهذا success=True في الاستجابة يُرفق دائماً بـ "sandbox": true حتى لا
    يُفهم كدفع فعلي بمال حقيقي.
    """
    if payload.gateway not in PAYMENT_GATEWAYS:
        raise HTTPException(status_code=400, detail=f"بوابة دفع غير مدعومة: {payload.gateway}")

    plan_info = PAYMENT_PLANS.get(payload.plan)
    if not plan_info:
        raise HTTPException(status_code=400, detail=f"باقة غير معروفة: {payload.plan}")

    if not payload.phone.strip():
        raise HTTPException(status_code=400, detail="الرجاء إدخال رقم الهاتف.")

    if payload.otp.strip() != SANDBOX_OTP_CODE:
        raise HTTPException(status_code=400, detail="رمز التحقق (OTP) غير صحيح.")

    user_id = get_current_user_id()
    set_user_plan_and_unlimited(user_id, plan_info["plan"], plan_info["is_unlimited"])
    update_user_points(user_id, plan_info["points"])
    user = get_user_row(user_id)

    return {
        "success": True,
        "sandbox": True,
        "gateway": payload.gateway,
        "plan": user["plan"],
        "points": user["points"],
        "is_unlimited": user["is_unlimited"],
        "message": f"✅ (Sandbox) تم الدفع التجريبي عبر {payload.gateway} وتحديث باقتك فعلياً في قاعدة البيانات.",
    }


@app.post("/generate-video")
async def generate_video(payload: GenerateVideoRequest):
    """
    توليد حقيقي 100% عبر gradio_client + LTX-Video (Lightricks)، مع دمج
    متعدد المشاهد بـ ffmpeg لتجاوز حد الـ5 ثوانٍ الحقيقي لكل استدعاء واحد.
    لا يوجد أي مسار "محاكاة" في هذا الكود - أي نجاح معروض للمستخدم يعكس
    فيديو حقيقياً تم توليده فعلياً ورفعه إلى Cloudflare R2.
    """
    user_id = get_current_user_id()
    user = get_user_row(user_id)

    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="الرجاء كتابة سيناريو أو وصف الفيديو أولاً.")

    quality = payload.quality if payload.quality in SCENE_TIER_TO_COUNT else "480p"
    cost = 0

    if not user["is_unlimited"]:
        allowed_qualities = PLAN_ALLOWED_QUALITY.get(user["plan"], ["480p"])
        if quality not in allowed_qualities:
            raise HTTPException(
                status_code=403,
                detail=f"باقة {quality} تتطلب ترقية باقتك الحالية ({user['plan']}).",
            )
        cost = QUALITY_POINTS_COST[quality]
        if user["points"] < cost:
            raise HTTPException(
                status_code=402,
                detail=f"رصيدك غير كافٍ. هذا التوليد يحتاج {cost} نقطة وتملك حالياً {user['points']} فقط.",
            )

    max_scenes = SCENE_TIER_TO_COUNT[quality]

    try:
        video_bytes, actual_scene_count, has_narration = await generate_stitched_video(prompt, max_scenes, payload.voice)
        video_url = await upload_generated_video(video_bytes)
    except VideoGenerationError as exc:
        # لا يتم خصم أي نقاط عند الفشل
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    remaining_points = user["points"]
    if not user["is_unlimited"] and cost:
        remaining_points = update_user_points(user_id, -cost)

    return {
        "video_url": video_url,
        "remaining_points": remaining_points,
        "quality": quality,
        "scene_count": actual_scene_count,
        "scene_duration_seconds": SCENE_DURATION_SECONDS,
        "total_duration_seconds": round(actual_scene_count * SCENE_DURATION_SECONDS, 1),
        "has_narration": has_narration,
        "is_unlimited": user["is_unlimited"],
    }


# ============================================================
# مسارات التوليد المُقسَّم (طلب منفصل لكل مشهد) - الحل الدائم لمشكلة مهلة Vercel
# ============================================================
# التدفق الجديد من الواجهة (بدل طلب /generate-video الطويل الوحيد):
#   1) POST /api/plan-scenes   -> تحقق من الباقة/النقاط وتقسيم النص لمشاهد (سريع جداً، بلا توليد فعلي)
#   2) POST /api/generate-scene (مرة لكل مشهد بالتسلسل) -> يولّد مشهداً واحداً فقط ويرفعه مؤقتاً لـR2
#   3) POST /api/merge-scenes  -> يجمع كل المشاهد المرفوعة، يدمجها، يضيف الصوت، يخصم النقاط، وينظّف الملفات المؤقتة
# كل طلب من الثلاثة قصير ومستقل (لا يتجاوز عملياً بضع عشرات الثواني) مهما
# كان عدد المشاهد الكلي، لأن العدد لم يعد محكوماً بمهلة تنفيذ دالة واحدة.
@app.post("/api/plan-scenes")
async def plan_scenes(payload: PlanScenesRequest):
    user_id = get_current_user_id()
    user = get_user_row(user_id)

    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="الرجاء كتابة سيناريو أو وصف الفيديو أولاً.")

    quality = payload.quality if payload.quality in SCENE_TIER_TO_COUNT else "480p"

    if not user["is_unlimited"]:
        allowed_qualities = PLAN_ALLOWED_QUALITY.get(user["plan"], ["480p"])
        if quality not in allowed_qualities:
            raise HTTPException(
                status_code=403,
                detail=f"باقة {quality} تتطلب ترقية باقتك الحالية ({user['plan']}).",
            )
        cost = QUALITY_POINTS_COST[quality]
        if user["points"] < cost:
            raise HTTPException(
                status_code=402,
                detail=f"رصيدك غير كافٍ. هذا التوليد يحتاج {cost} نقطة وتملك حالياً {user['points']} فقط.",
            )

    max_scenes = SCENE_TIER_TO_COUNT[quality]
    scenes = _split_into_scenes(prompt, max_scenes)
    return {"scenes": scenes, "quality": quality}


@app.post("/api/generate-scene")
async def generate_scene(payload: GenerateSceneRequest):
    scene_prompt = (payload.scene_prompt or "").strip()
    if not scene_prompt:
        raise HTTPException(status_code=400, detail="نص المشهد فارغ.")

    try:
        clip_path = await _generate_one_scene_with_retry(scene_prompt)
        clip_bytes = clip_path.read_bytes()
    except VideoGenerationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    scene_key = f"scenes-tmp/{uuid.uuid4().hex}.mp4"
    await run_in_threadpool(_upload_bytes_to_r2_sync, clip_bytes, scene_key)
    return {"scene_key": scene_key}


# ============================================================
# ميزة "صورة + برومت": توليد فيديو واحد (~4.8 ثانية) عبر LTX-Video بوضع
# صورة-إلى-فيديو، مع رواية صوتية اختيارية فوقه ونشر تلقائي على يوتيوب (نفس
# منطق /api/merge-scenes بالضبط) - انظر _auto_publish_to_youtube أعلاه.
# ============================================================
ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB - كافٍ لصورة مرجعية، يمنع رفع ملفات ضخمة عرضاً


@app.post("/api/generate-image-video")
async def generate_image_video(
    prompt: str = Form(...),
    voice: str = Form(DEFAULT_NARRATION_VOICE),
    talking_mode: bool = Form(False),
    image: UploadFile = File(...),
):
    """توليد فيديو من صورة + برومت. وضعان:
    1) الافتراضي (talking_mode=False): حركة عامة على الصورة عبر LTX-Video
       image-to-video، مع رواية صوتية فوقها بلا مزامنة شفاه.
    2) talking_mode=True: "الوجه يتكلم فعلياً" - يولَّد صوت الرواية أولاً، ثم
       يُمرَّر مع الصورة الأصلية لمساحة Wav2Lip الحقيقية لمزامنة الشفاه فعلياً
       مع الكلام - هذا هو الوضع المناسب لصور بورتريه بشرية حقيقية (بعكس
       LTX-Video الذي بادئته المقصودة أصلاً "كرتوني بلا بشر حقيقيين")."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="الرجاء كتابة وصف للفيديو المطلوب من الصورة.")

    if image.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="صيغة صورة غير مدعومة. استخدم JPG أو PNG أو WEBP.")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="ملف الصورة فارغ.")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="حجم الصورة كبير جداً (الحد الأقصى 8MB).")

    user_id = get_current_user_id()
    user = get_user_row(user_id)
    cost = 0
    if not user["is_unlimited"]:
        cost = QUALITY_POINTS_COST.get("480p", 5)
        if user["points"] < cost:
            raise HTTPException(status_code=402, detail="رصيدك غير كافٍ لتوليد فيديو من صورة.")

    suffix = pathlib.Path(image.filename or "").suffix or ".jpg"
    with tempfile.TemporaryDirectory() as tmp_dir:
        image_path = pathlib.Path(tmp_dir) / f"input{suffix}"
        image_path.write_bytes(image_bytes)

        has_narration = False
        try:
            if talking_mode:
                narration_path = await _generate_narration_async(prompt, _resolve_narration_voice(voice))
                has_narration = True
                try:
                    lip_path = await run_in_threadpool(
                        _generate_lipsync_video_sync, str(image_path), str(narration_path)
                    )
                except Exception as exc:  # noqa: BLE001 - خطأ حقيقي من مساحة Wav2Lip يُعرَض بصدق
                    raise VideoGenerationError(f"⚠️ فشل توليد فيديو مزامنة الشفاه: {exc}", status_code=502)
                video_bytes = pathlib.Path(lip_path).read_bytes()
            else:
                clip_path = await _generate_one_scene_with_retry(prompt, image_path=str(image_path))
                video_bytes = clip_path.read_bytes()
                try:
                    narration_path = await _generate_narration_async(prompt, _resolve_narration_voice(voice))
                    video_bytes = await run_in_threadpool(_mux_narration_sync, video_bytes, narration_path)
                    has_narration = True
                except Exception as exc:  # noqa: BLE001 - فشل الصوت لا يُسقط الفيديو الصامت الناجح
                    print(f"⚠️ تحذير: فشل توليد/إضافة الرواية الصوتية لفيديو الصورة: {exc}")

            video_url = await upload_generated_video(video_bytes)
        except VideoGenerationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    remaining_points = user["points"]
    if not user["is_unlimited"] and cost:
        remaining_points = update_user_points(user_id, -cost)

    youtube_result = await _auto_publish_to_youtube(video_url, [prompt])

    return {
        "video_url": video_url,
        "remaining_points": remaining_points,
        "scene_duration_seconds": SCENE_DURATION_SECONDS,
        "has_narration": has_narration,
        "talking_mode": talking_mode,
        "is_unlimited": user["is_unlimited"],
        **youtube_result,
    }


@app.post("/api/merge-scenes")
async def merge_scenes(payload: MergeScenesRequest):
    if not payload.scene_keys:
        raise HTTPException(status_code=400, detail="لا توجد مشاهد لدمجها.")

    user_id = get_current_user_id()
    user = get_user_row(user_id)
    quality = payload.quality if payload.quality in SCENE_TIER_TO_COUNT else "480p"

    cost = 0
    if not user["is_unlimited"]:
        cost = QUALITY_POINTS_COST.get(quality, 0)
        if user["points"] < cost:
            raise HTTPException(status_code=402, detail="رصيدك غير كافٍ لإكمال هذا الفيديو.")

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            clip_paths: list[pathlib.Path] = []
            for i, key in enumerate(payload.scene_keys):
                clip_bytes = await run_in_threadpool(_download_bytes_from_r2_sync, key)
                clip_path = pathlib.Path(tmp_dir) / f"scene_{i}.mp4"
                clip_path.write_bytes(clip_bytes)
                clip_paths.append(clip_path)

            video_bytes = await run_in_threadpool(_concat_videos_sync, clip_paths)

        has_narration = False
        full_narration_text = ". ".join(t for t in payload.scenes_text if t.strip())
        if full_narration_text.strip():
            try:
                narration_path = await _generate_narration_async(full_narration_text, _resolve_narration_voice(payload.voice))
                video_bytes = await run_in_threadpool(_mux_narration_sync, video_bytes, narration_path)
                has_narration = True
            except Exception as exc:  # noqa: BLE001 - فشل الصوت لا يُسقط الفيديو الصامت الناجح
                print(f"⚠️ تحذير: فشل توليد/إضافة الرواية الصوتية (تم إرجاع الفيديو صامتاً): {exc}")

        video_url = await upload_generated_video(video_bytes)
    except VideoGenerationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    finally:
        # تنظيف المشاهد المؤقتة من R2 (تنجح عملية الدمج أو تفشل، لا داعي لتركها للأبد)
        for key in payload.scene_keys:
            try:
                await run_in_threadpool(_delete_object_from_r2_sync, key)
            except Exception:  # noqa: BLE001
                pass

    remaining_points = user["points"]
    if not user["is_unlimited"] and cost:
        remaining_points = update_user_points(user_id, -cost)

    scene_count = len(payload.scene_keys)

    youtube_result = await _auto_publish_to_youtube(video_url, payload.scenes_text)

    return {
        "video_url": video_url,
        "remaining_points": remaining_points,
        "quality": quality,
        "scene_count": scene_count,
        "scene_duration_seconds": SCENE_DURATION_SECONDS,
        "total_duration_seconds": round(scene_count * SCENE_DURATION_SECONDS, 1),
        "has_narration": has_narration,
        "is_unlimited": user["is_unlimited"],
        **youtube_result,
    }


# ============================================================
# دمج فيديوهات سابقة مكتملة إلى فيديو واحد أطول (بلا استهلاك حصة GPU إطلاقاً)
# ============================================================
# فكرة الميزة: التطبيق الحالي يحدّد الفيديو الواحد بأقصى 8 مشاهد (~38 ثانية)
# كحد مبرمَج بالكود (SCENE_TIER_TO_COUNT)، بغض النظر عن حصة GPU المتبقية.
# هذه الميزة تلتف حول ذاك الحد دون تعديله: تولّد عدة فيديوهات كاملة منفصلة
# (كل واحد بطلب توليد طبيعي مستقل عبر الزر الرئيسي)، ثم تدمجها هنا بـffmpeg
# فقط - بدون أي استدعاء لمحرك LTX-Video، أي بلا أي تكلفة إضافية من حصة GPU
# اليومية. الأمان: نقبل فقط روابط فيديو تنتمي فعلاً لحاوية R2 الخاصة بهذا
# التطبيق (تبدأ بـR2_PUBLIC_URL_BASE) لمنع استخدام هذا المسار كوسيط لجلب أي
# رابط خارجي عشوائي (SSRF) - يُرفض أي رابط آخر برسالة خطأ واضحة.
def _extract_r2_key_from_public_url(url: str) -> str:
    base = R2_PUBLIC_URL_BASE.rstrip("/") + "/"
    if not R2_PUBLIC_URL_BASE or not url.startswith(base):
        raise VideoGenerationError(
            f"⚠️ الرابط '{url}' ليس فيديو مولَّداً من هذا التطبيق (لازم يبدأ بـ {R2_PUBLIC_URL_BASE}). "
            "يُقبَل فقط دمج فيديوهات وُلِّدت سابقاً من نفس التطبيق.",
            status_code=400,
        )
    return url[len(base):]


@app.post("/api/merge-videos")
async def merge_videos(payload: MergeVideosRequest):
    if len(payload.video_urls) < 2:
        raise HTTPException(status_code=400, detail="أدخل رابطين على الأقل لدمجهما.")
    if len(payload.video_urls) > 10:
        raise HTTPException(status_code=400, detail="أقصى حد 10 فيديوهات بالدمج الواحد.")

    try:
        keys = [_extract_r2_key_from_public_url(u.strip()) for u in payload.video_urls if u.strip()]
        with tempfile.TemporaryDirectory() as tmp_dir:
            clip_paths: list[pathlib.Path] = []
            for i, key in enumerate(keys):
                clip_bytes = await run_in_threadpool(_download_bytes_from_r2_sync, key)
                clip_path = pathlib.Path(tmp_dir) / f"merge_{i}.mp4"
                clip_path.write_bytes(clip_bytes)
                clip_paths.append(clip_path)

            merged_bytes = await run_in_threadpool(_concat_videos_sync, clip_paths)

        merged_url = await upload_generated_video(merged_bytes)
    except VideoGenerationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    return {"video_url": merged_url, "merged_count": len(keys)}


# ============================================================
# النشر على يوتيوب (YouTube Data API v3) - نص-آلي بمراجعة بشرية إلزامية
# ============================================================
# فلسفة التصميم: الأتمتة الكاملة (توليد -> نشر تلقائي بلا مراجعة) تخالف سياسة
# يوتيوب لـ"المحتوى غير الأصيل" (Inauthentic Content) وتُعرِّض القناة للحظر.
# لذلك هذا المسار لا يُستدعى تلقائياً أبداً بعد التوليد - المستخدم لازم يفتح
# الفيديو، يكتب عنوان ووصف حقيقيين بنفسه، ويضغط زر النشر بوعي (نفس مبدأ زر
# الدفع/الترقية بهذا التطبيق: لا فعل حساس يحدث بدون ضغطة مستخدم صريحة).
# الخصوصية الافتراضية "private" عمداً - يوتيوب لا يسمح لك تنشر "عام" بالخطأ.
def _youtube_upload_sync(video_bytes: bytes, title: str, description: str, privacy_status: str) -> dict:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload

    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )

    youtube = build("youtube", "v3", credentials=creds)
    media = MediaInMemoryUpload(video_bytes, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "categoryId": "1",  # Film & Animation - أقرب تصنيف لمحتوى كرتوني مولَّد
            },
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        },
        media_body=media,
    )
    response = None
    while response is None:
        _status, response = request.next_chunk()
    return response


# ============================================================
# نشر تلقائي كامل على يوتيوب بعد كل توليد فيديو (بلا أي تدخل بشري في لحظة النشر)
# ------------------------------------------------------------
# قرار مقصود: الخصوصية الافتراضية دائماً "غير مدرج" (unlisted) - وليس "عام" -
# حتى مع النشر الآلي الكامل. السبب: أي خلل مستقبلي بجودة/محتوى الفيديو (كما
# حدث فعلاً سابقاً مع خلل توليد محتوى غير متطابق مع السيناريو) يبقى أثره
# محدوداً على القناة (الفيديو موجود، بس ما يظهر بالبحث ولا يُقترَح لأحد)
# لين يراجعه صاحب الحساب ويحوّله يدوياً لعام من يوتيوب استوديو. هذا يوازن بين
# طلب المستخدم (نشر أوتوماتيكي بلا انتظار) وحماية القناة من سياسة "المحتوى
# غير الأصيل" لو تكرر أي خلل توليد لم نكتشفه بعد.
# فشل النشر (توكن منتهي، حصة يوتيوب، انقطاع مؤقت...) لا يُسقط طلب توليد الفيديو
# إطلاقاً - المستخدم يحصل على فيديوه دائماً، والنشر مجرد إضافة أفضل-جهد فوقه.
# ============================================================
def _auto_publish_title_from_scenes(scenes_text: list[str]) -> str:
    first_line = next((t.strip() for t in scenes_text if t.strip()), "")
    title = first_line if first_line else "فيديو كرتوني مولَّد بالذكاء الاصطناعي"
    return title[:95]


async def _auto_publish_to_youtube(video_url: str, scenes_text: list[str]) -> dict:
    if not (YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN):
        return {"youtube_publish_status": "skipped", "youtube_publish_message": "حساب يوتيوب غير مربوط بعد."}

    title = _auto_publish_title_from_scenes(scenes_text)
    description = ". ".join(t.strip() for t in scenes_text if t.strip())
    try:
        key = _extract_r2_key_from_public_url(video_url)
        video_bytes = await run_in_threadpool(_download_bytes_from_r2_sync, key)
        result = await run_in_threadpool(_youtube_upload_sync, video_bytes, title, description, "unlisted")
        video_id = result.get("id")
        return {
            "youtube_publish_status": "published",
            "youtube_video_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            "youtube_privacy_status": "unlisted",
        }
    except Exception as exc:  # noqa: BLE001 - فشل النشر أفضل-جهد فقط، لا يُسقط الفيديو أبداً
        return {"youtube_publish_status": "failed", "youtube_publish_message": f"⚠️ فشل النشر التلقائي على يوتيوب: {exc}"}


@app.post("/api/youtube/publish")
async def youtube_publish(payload: YoutubePublishRequest):
    if not (YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN):
        raise HTTPException(
            status_code=500,
            detail="⚠️ حساب يوتيوب غير مربوط بعد. أضف YOUTUBE_CLIENT_ID وYOUTUBE_CLIENT_SECRET "
            "وYOUTUBE_REFRESH_TOKEN بمتغيرات بيئة Vercel أولاً.",
        )

    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="الرجاء إدخال عنوان الفيديو.")
    if payload.privacy_status not in ("private", "unlisted", "public"):
        raise HTTPException(status_code=400, detail="حالة خصوصية غير صحيحة.")

    try:
        key = _extract_r2_key_from_public_url(payload.video_url.strip())
        video_bytes = await run_in_threadpool(_download_bytes_from_r2_sync, key)
        result = await run_in_threadpool(
            _youtube_upload_sync, video_bytes, title, payload.description, payload.privacy_status
        )
    except VideoGenerationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:  # noqa: BLE001 - أي خطأ فعلي من مكتبة Google (توكن منتهي، حصة، إلخ) يُعرَض بصدق
        raise HTTPException(status_code=502, detail=f"⚠️ فشل النشر على يوتيوب: {exc}")

    video_id = result.get("id")
    return {
        "youtube_video_id": video_id,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        "privacy_status": payload.privacy_status,
    }


# ============================================================
# قائمة الأصوات المتاحة + تجربة صوت قصيرة قبل الاختيار (Edge TTS مجاني)
# ============================================================
PREVIEW_SAMPLE_TEXT = "مرحباً، هذا مثال على هذا الصوت في محرك توليد الفيديو."


class PreviewVoiceRequest(BaseModel):
    voice: str = "ar-IQ-BasselNeural"  # = DEFAULT_NARRATION_VOICE


@app.get("/api/voices")
async def list_voices():
    """قائمة الأصوات العربية المتاحة للرواية الصوتية (للعرض بقائمة الاختيار بالواجهة)."""
    return {
        "voices": [{"id": vid, "label": label} for vid, label in NARRATION_VOICES.items()],
        "default": DEFAULT_NARRATION_VOICE,
    }


@app.post("/api/preview-voice")
async def preview_voice(payload: PreviewVoiceRequest):
    """
    يولّد مقطعاً صوتياً قصيراً بنفس الصوت المختار حتى يسمعه المستخدم قبل أن
    يقرر - لا يُرفع إلى R2 (مؤقت وصغير جداً)، يُعاد مباشرة كبايتات صوتية.
    """
    voice = _resolve_narration_voice(payload.voice)
    try:
        narration_path = await _generate_narration_async(PREVIEW_SAMPLE_TEXT, voice)
        audio_bytes = narration_path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"⚠️ فشل توليد عينة الصوت: {exc}")

    return Response(content=audio_bytes, media_type="audio/mpeg")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
