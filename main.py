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
import asyncio
import pathlib
import subprocess
import tempfile

import boto3
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from gradio_client import Client as GradioClient

BASE_DIR = pathlib.Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="AI Cartoon Video Studio")


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
# إعدادات محرك التوليد الجديد: Wan 2.2 14B (فيديو حقيقي متعدد المشاهد)
# ============================================================
# لماذا تم الاستبدال: المحرك القديم (ByteDance/AnimateDiff-Lightning) كان
# يُنتج مقاطع ~1 ثانية فقط بشخصية عامة لا تتبع النص المكتوب - قيد حقيقي في
# النموذج نفسه عند 2-8 خطوات استدلال وبدون أي معامل مدة. تم التحقق حياً
# (view_api) من Upsampler/wan-2-2-14b-text-to-video: نموذج فعلي يتبع النص
# بدقة عالية، لكنه محدود بحد أقصى 5 ثوانٍ لكل استدعاء واحد (Lightning LoRA
# بـ4 خطوات، 0.5-5.0 ثانية).
#
# لتحقيق "فيديو كامل" أطول من 5 ثوانٍ: نُقسِّم نص المستخدم إلى عدة "مشاهد"
# قصيرة (كل سطر أو جملة = مشهد)، نولّد كل مشهد على حدة (~4.8 ثانية)، ثم
# ندمجها بـ ffmpeg في فيديو واحد متسلسل. هذا هو المعنى الفعلي لعبارة "توليد
# وفصل الفيديو المتسلسل" الظاهرة في واجهة الموقع.
#
# قيد حقيقي يجب معرفته: كل مشهد يستغرق تقريباً 30-50 ثانية على GPU مجاني
# (ZeroGPU) شاملاً وقت التحميل. توليد 3 مشاهد متسلسلة قد يقترب من حد الوقت
# المسموح لدالة Vercel. تم رفع الحد في vercel.json إلى 300 ثانية، لكن على
# خطة Vercel Hobby العادية (بدون Fluid Compute مُفعَّل) الحد الفعلي 60 ثانية
# فقط - إن ظهر خطأ Timeout عند اختيار باقة تسمح بأكثر من مشهد واحد، الحل هو
# تفعيل Fluid Compute من إعدادات المشروع أو الترقية لخطة Pro.
HF_API_TOKEN = os.getenv("HF_API_TOKEN", "") or None
WAN_SPACE_ID = os.getenv("WAN_SPACE_ID", "Upsampler/wan-2-2-14b-text-to-video")
WAN_SPACE_API_NAME = "/generate_video"

WAN_ASPECT_RATIO = "16:9 (832x480)"
WAN_STEPS = 4  # القيمة الافتراضية المُحسّنة لهذا النموذج (Lightning LoRA بـ4 خطوات)
WAN_NEGATIVE_PROMPT = (
    "色调艳丽, 过曝, 静态, 细节模糊不清, 字幕, 风格, 作品, 画作, 画面, 静止, "
    "整体发灰, 最差质量, 低质量, JPEG压缩残留, 丑陋的, 残缺的, 多余的手指, "
    "画得不好的手部, 画得不好的脸部, 畸形的, 毁容的, 形态畸形的肢体, 手指融合, "
    "静止不动的画面, 杂乱的背景, 三条腿, 背景人很多, 倒着走"
)  # نفس القيمة الافتراضية للمساحة الحية - مُختبَرة وتُنتج جودة أفضل فعلياً
WAN_SCENE_DURATION_SECONDS = 4.8  # قريب من الحد الأقصى الحقيقي للنموذج (5.0s)
WAN_GUIDANCE_SCALE = 1.0
WAN_GUIDANCE_SCALE_2 = 1.0

MAX_RETRIES = 3
RETRY_WAIT_QUEUE_BUSY = 20
RETRY_WAIT_COLD_START = 25

# كل باقة تحدد كم "مشهداً" (~4.8 ثانية للمشهد) يمكن دمجها في فيديو واحد متسلسل.
# أسماء الباقات القديمة (480p/720p/1080p) أصبحت الآن تعني عدد المشاهد/مدة
# الفيديو الإجمالية - وليست دقة بكسل حقيقية (المخرج دائماً 832x480 لكل مشهد)،
# تجنباً لأي وصف مضلِّل لدقة غير موجودة فعلياً في الفيديو الناتج.
SCENE_TIER_TO_COUNT = {"480p": 1, "720p": 2, "1080p": 8}
# ملاحظة صادقة: 8 مشاهد × ~4.8 ثانية = ~38 ثانية فيديو، لكن التوليد الفعلي
# لكل مشهد يستغرق ~30-45 ثانية على GPU مجاني (ZeroGPU) شاملاً وقت الطابور -
# أي أن طلب 8 مشاهد قد يستغرق 240-360+ ثانية إجمالاً، وهذا قريب جداً أو قد
# يتجاوز سقف Vercel (300 ثانية حتى مع Fluid Compute)، خصوصاً لو احتاج أي
# مشهد إعادة محاولة (إزدحام/تحميل بارد). المستخدم صاحب المشروع اختار هذا
# الرقم بوعي كامل بهذا الخطر (بدل قيمة أكثر أماناً كـ5 مشاهد).
QUALITY_POINTS_COST = {"480p": 5, "720p": 10, "1080p": 20}
PLAN_ALLOWED_QUALITY = {
    "free": ["480p"],
    "medium": ["480p", "720p"],
    "pro": ["480p", "720p", "1080p"],
}

# كلمة سر لوحة التحكم الإدارية - يجب ضبطها كمتغير بيئة حقيقي على Vercel.
# إن تُركت فارغة، يُرفض أي طلب ترقية إدارية تلقائياً (فشل آمن - Fail Closed).
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")


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


class AdminGrantRequest(BaseModel):
    key: str


class CheckoutRequest(BaseModel):
    plan: str
    gateway: str
    phone: str
    otp: str


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
# استدعاء Wan 2.2 14B عبر gradio_client لتوليد مشهد واحد (~4.8 ثانية)
# ============================================================
def _generate_scene_via_gradio_sync(prompt: str) -> str:
    client = GradioClient(WAN_SPACE_ID, token=HF_API_TOKEN)
    result = client.predict(
        prompt,
        WAN_ASPECT_RATIO,
        WAN_STEPS,
        WAN_NEGATIVE_PROMPT,
        WAN_SCENE_DURATION_SECONDS,
        WAN_GUIDANCE_SCALE,
        WAN_GUIDANCE_SCALE_2,
        42,    # seed - يُتجاهل عملياً لأن randomize_seed=True أدناه
        True,  # randomize_seed: كل مشهد يحصل على بذرة عشوائية لتنويع الحركة
        api_name=WAN_SPACE_API_NAME,
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


async def _generate_one_scene_with_retry(prompt: str) -> pathlib.Path:
    """يولّد مشهداً واحداً مع إعادة محاولة تلقائية، ويعيد مساراً محلياً لملف الفيديو."""
    last_message = "⚠️ فشل توليد المشهد لسبب غير معروف."
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            video_path = await run_in_threadpool(_generate_scene_via_gradio_sync, prompt)
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


async def generate_stitched_video(prompt: str, max_scenes: int) -> tuple[bytes, int]:
    """
    يولّد فيديو متعدد المشاهد كاملاً: يقسّم النص، يولّد كل مشهد تسلسلياً عبر
    Wan 2.2 14B، ثم يدمج الكل بـ ffmpeg. يعيد (بايتات الفيديو النهائي، عدد
    المشاهد الفعلي) - العدد الفعلي يُستخدَم لاحقاً لعرض الحقيقة للمستخدم
    (مثلاً لو كتب سطراً واحداً فقط رغم أن باقته تسمح بـ3 مشاهد).
    """
    scenes = _split_into_scenes(prompt, max_scenes)
    clip_paths: list[pathlib.Path] = []
    for scene_prompt in scenes:
        clip_path = await _generate_one_scene_with_retry(scene_prompt)
        clip_paths.append(clip_path)

    video_bytes = await run_in_threadpool(_concat_videos_sync, clip_paths)
    return video_bytes, len(clip_paths)


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
    توليد حقيقي 100% عبر gradio_client + Wan 2.2 14B (Upsampler)، مع دمج
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
        video_bytes, actual_scene_count = await generate_stitched_video(prompt, max_scenes)
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
        "scene_duration_seconds": WAN_SCENE_DURATION_SECONDS,
        "total_duration_seconds": round(actual_scene_count * WAN_SCENE_DURATION_SECONDS, 1),
        "is_unlimited": user["is_unlimited"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
