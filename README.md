# ArenaUZ — MLBB va PUBG turnir sayti

Flask + SQLAlchemy. Telefon uchun birinchi navbatda moslangan.

## Imkoniyatlar
- Ro'yxatdan o'tish: Gmail, nikneym, parol, telefon (parol hash qilinadi)
- **PvP (1 ga 1)**: 50 tagacha ishtirokchi, olimpiya (yutqazgan chiqib ketadi) jadvali
- **Jamoaviy**: o'yinchilar yakka ro'yxatdan o'tadi, boshlanganda **random jamoalar** tuziladi
  - MLBB: 5 ga 5, 30 tagacha jamoa — jadval bo'yicha o'ynaladi
  - PUBG: 4 talik, 20 tagacha jamoa (80 o'yinchi) — ball jadvali
- Mukofotlar: admin turnir yaratayotganda nechta o'rin va har biriga qancha ekanini kiritadi
- SMS: boshlanishidan 30, 10 va 1 daqiqa oldin (Eskiz.uz)
- Admin panel: turnir yaratish, boshlash, natijalar, ball kiritish

## Ishga tushirish
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # ADMIN_EMAIL va ADMIN_PASSWORD ni to'ldiring
python app.py             # http://localhost:5000
```
Admin yaratishning boshqa yo'li: `flask --app app create-admin`

## Production
```bash
gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
```
`PRODUCTION=1`, `TRUST_PROXY=1` (Render kabi proxy ortida) va uzun `SECRET_KEY` qo'ying.
Render/Supabase uchun `DATABASE_URL` ga Postgres manzilini bering (`pip install psycopg2-binary`).

## SMS (Eskiz.uz)
`ESKIZ_EMAIL` va `ESKIZ_PASSWORD` ni kiriting. Eskiz xabar matnini oldindan tasdiqlashni talab qilishi mumkin
(matn: `<turnir nomi> turniri N daqiqadan keyin boshlanadi (SS:DD). Tayyor bo'ling!`).
Kalitlar bo'sh bo'lsa SMS yuborilmaydi, faqat logda ko'rinadi.

## Xavfsizlik
Parol hash (werkzeug), CSRF himoya, login/ro'yxatdan o'tishga limit, 5 xato urinishdan keyin 10 daqiqa blok,
HttpOnly/SameSite cookie, qat'iy CSP va xavfsizlik headerlari, ORM (SQL injection yo'q), telefon/email ommaga
ko'rsatilmaydi (adminga ham telefon yashirin ko'rinadi).
