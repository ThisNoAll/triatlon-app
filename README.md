# Triatlon jelentkezési rendszer – egyszerű Flask verzió

Ez egy szándékosan leegyszerűsített, könnyen átlátható webapp.

## Mit tud?
- szervező létrehoz egy eseményt
- az esemény kezdete előtt **6 órával automatikusan lezár a jelentkezés**
- jelentkezés:
  - Google fiókkal (ha beállítod)
  - vagy név + email alapján
- maximum **5 csapat**
- az első **10 főből** kialakul **5 db 2 fős csapat**
- ezután a rendszer:
  - a 11–15. jelentkezőt a **3. fős bővítési körbe** teszi
  - ha összejön 5 ember, azonnal random szétosztja őket az 5 csapatba
  - ha nem, akkor a határidő lejártakor osztja szét őket random
- ugyanez megy a **4. fővel** a 16–20. játékos között
- maximum **20 játékos**
- a jelentkező megadhat csapatnév-javaslatot
- a szervező tudja szerkeszteni a csapatneveket
- van **nyomtatható pontozólap**

---

# 1. HELYI FUTTATÁS – a legegyszerűbb mód

## 1. lépés – Python telepítése
Legyen fent a gépeden Python 3.11 vagy újabb.

## 2. lépés – a projekt megnyitása
Csomagold ki a ZIP-et, és nyisd meg a mappát terminálban.

## 3. lépés – virtuális környezet
Windows:
```bash
python -m venv .venv
.venv\Scripts\activate
```

Mac / Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 4. lépés – csomagok telepítése
```bash
pip install -r requirements.txt
```

## 5. lépés – .env létrehozása
Másold le a `.env.example` fájlt `.env` néven.

Minimum ezt állítsd be benne:
- `SECRET_KEY`
- `ADMIN_PASSWORD`

Ha most csak gyorsan kipróbálnád, a `DATABASE_URL` maradhat:
```env
DATABASE_URL=sqlite:///app.db
```

## 6. lépés – indítás
```bash
python app.py
```

A böngészőben nyisd meg:
```text
http://127.0.0.1:5000
```

---

# 2. SZERVEZŐI BELÉPÉS

A szervezői oldal:
```text
/admin/login
```

A jelszó az `.env` fájlban lévő:
```env
ADMIN_PASSWORD=...
```

Belépés után:
- létrehozhatsz eseményt
- láthatod a jelentkezőket
- átírhatod a csapatneveket
- megnyithatod a nyomtatható pontozólapot

---

# 3. GOOGLE LOGIN BEÁLLÍTÁSA (opcionális)

Ha ezt nem állítod be, a név+email jelentkezés akkor is működik.

## 1. lépés
Menj a Google Cloud Console felületre.

## 2. lépés
Hozz létre OAuth 2.0 Client ID-t.

## 3. lépés
Authorized redirect URI-nak add meg:

Helyi futtatásnál:
```text
http://127.0.0.1:5000/auth/google/callback
```

## 4. lépés
Másold be az `.env` fájlba:
```env
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

Újraindítás után működik a Google bejelentkezés.

---

# 4. RENDERES ÉLESÍTÉS – nagyon egyszerűen

## 1. lépés – GitHub
Tedd fel ezt a projektet egy GitHub repositoryba.

## 2. lépés – Render fiók
Lépj be a Renderre.

## 3. lépés – New Web Service
Válaszd:
- **New**
- **Web Service**
- csatlakoztasd a GitHub repót

## 4. lépés – alap beállítások
A Rendernél állítsd be:

### Build Command
```bash
pip install -r requirements.txt
```

### Start Command
```bash
gunicorn app:app
```

## 5. lépés – környezeti változók
Add meg ezeket:

```env
SECRET_KEY=valami-hosszu-eros-kulcs
ADMIN_PASSWORD=egy-eros-admin-jelszo
```

### adatbázis
A legegyszerűbb helyi próba SQLite-tal megy, de Renderen **inkább PostgreSQL-t használj**.

Hozz létre Renderen egy PostgreSQL adatbázist, majd a kapott `External Database URL`-t add meg:
```env
DATABASE_URL=postgresql://...
```

## 6. lépés – Google login (ha kell)
A Renderes domainhez add hozzá a callback URL-t a Google Cloud Console-ban, pl:
```text
https://A-TE-APPOD.onrender.com/auth/google/callback
```

Majd Render env-ben:
```env
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

## 7. lépés – Deploy
Kattints deployra.

Kész.

---

# 5. A rendszer logikája röviden

## Csapatképzés
- 1–10. ember:
  - sorban megy az 5 csapatba
  - így lesz 5 db 2 fős csapat

- 11–15. ember:
  - a 3. fős bővítési körbe kerül
  - ha összejön 5 ember, azonnal random kiosztás az 5 csapatba
  - ha nem, a határidő végén random kiosztás a bent maradtaknak

- 16–20. ember:
  - ugyanez a 4. fős bővítési körben

## Lezárás
- a rendszer az esemény előtt 6 órával lezár
- a határidő után új jelentkezést nem fogad
- ha van bent maradt bővítési pool, azt az oldal automatikusan véglegesíti

---

# 6. Fontos őszinte megjegyzés

Ez a verzió szándékosan **egyszerű**, hogy:
- gyorsan kint legyen
- könnyű legyen beélesíteni
- ne legyen agyonbonyolítva

Amit **nem** tud:
- kifinomult jogosultsági rendszer
- email visszaigazolás
- teljes audit log
- több szervezős kezelés
- komplex admin workflow

Viszont a lényegi célodra pontosan jó:
- esemény létrehozás
- jelentkezés
- automatikus csapatképzés
- nyomtatható pontozólap
- egyszerű élesítés

---

# 7. Ha valami nem működik

## Gyakori okok
- rossz `SECRET_KEY`
- nincs telepítve a requirements
- hibás Google callback URL
- Renderen nincs beállítva a `DATABASE_URL`

## Első ellenőrzési pontok
1. Fut-e helyben?
2. Meg tudsz-e nyitni egy eseményt?
3. Tudsz-e név+email alapján jelentkezni?
4. Működik-e a szervezői belépés?
5. Látod-e a nyomtatható pontlapot?

Ha ez az 5 megy, az app életben van.
