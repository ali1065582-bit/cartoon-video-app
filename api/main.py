"""
ملف "غلاف" (shim) بسيط - Vercel يتطلب أن تكون دوال Python الخادمة داخل مجلد
api/ تحديداً (main.py في جذر المشروع مباشرة لم يعد يُكتشف تلقائياً - هذا هو
سبب فشل عملية النشر السابقة برسالة: "The pattern main.py defined in
functions doesn't match any Serverless Functions inside the api directory").

لا يوجد أي منطق مكرر هنا: هذا الملف يستورد فقط كائن FastAPI الحقيقي `app`
من main.py الأصلي في جذر المشروع (كل الكود الفعلي و templates/ يبقيان في
مكانهما دون أي تغيير في المسارات، لأن __file__ لوحدة main.py المستورَدة يبقى
يشير لموقعها الحقيقي في الجذر بغض النظر عن مكان استيرادها).
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from main import app  # noqa: E402,F401
