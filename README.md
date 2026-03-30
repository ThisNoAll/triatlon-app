# Triatlon jelentkezési rendszer

Egyszerű Flask alkalmazás több, párhuzamosan futó esemény kezelésére.

## Mit tud?

- a szervező több eseményt tud létrehozni és párhuzamosan kezelni
- minden eseménynek saját publikus URL-je van: `/e/<slug>`
- jelentkezés név + email alapján
- opcionális Google bejelentkezés
- maximum 5 csapat / 20 fő eseményenként
- automatikus csapatképzés 2, majd 3, majd 4 fős körökben
- esemény előtt 6 órával automatikus nevezéslezárás
- csapatnév-javaslat és csapatszintű szavazás
- admin csapatkezelés, fizetéskezelés és nyomtatható pontozólap

## URL modell

- `/` publikus eseménylista
- `/e/<slug>` adott esemény publikus oldala
- `/admin` összes esemény admin listája
- `/admin/events/<id>` adott esemény admin dashboardja

## Helyi futtatás

### 1. Python

Python 3.11 vagy újabb ajánlott.

### 2. Virtuális környezet

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Függőségek

```bash
pip install -r requirements.txt
```

### 4. .env

Készíts `.env` fájlt a [`.env.example`](/abs/path/c:/PROJEKTEK/triatlon/.env.example) alapján.

Minimum:

```env
SECRET_KEY=valami-hosszu-titkos-kulcs
ADMIN_PASSWORD=valami-eros-jelszo
DATABASE_PATH=triatlon.sqlite3
```

### 5. Indítás

```bash
python app.py
```

Ezután:

```text
http://127.0.0.1:5000
```

## Admin belépés

Admin oldal:

```text
/admin/login
```

Az admin jelszó az `.env` fájl `ADMIN_PASSWORD` értéke.

## Google login

Ha szeretnéd bekapcsolni:

```env
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

Helyi callback példa:

```text
http://127.0.0.1:5000/e/az-esemeny-slugja/auth/google/callback
```

Fontos: a callback URL esemény-specifikus, mert a publikus útvonal is eseményenként külön él.

## Csapatképzési logika

- 1-10. aktív jelentkező: 5 darab 2 fős csapat
- 11-15. aktív jelentkező: 3. körös bővítési pool
- 16-20. aktív jelentkező: 4. körös bővítési pool
- ha egy pool eléri az 5 főt, a rendszer azonnal szétosztja őket az 5 csapatba
- ha nem telik meg, a rendszer lezáráskor véglegesíti

## Tesztek

Futtatás:

```bash
.\.venv\Scripts\python -m unittest discover -s tests -v
```

## Oracle Cloud deploy

A projekt Oracle Cloud Always Free VM-re van előkészítve.

Részletes leírás:

- [deploy/oracle/README.md](/abs/path/c:/PROJEKTEK/triatlon/deploy/oracle/README.md)
- [deploy/oracle/install.sh](/abs/path/c:/PROJEKTEK/triatlon/deploy/oracle/install.sh)
- [deploy/oracle/triatlon.service](/abs/path/c:/PROJEKTEK/triatlon/deploy/oracle/triatlon.service)
- [deploy/oracle/nginx-triatlon.conf](/abs/path/c:/PROJEKTEK/triatlon/deploy/oracle/nginx-triatlon.conf)

## Fontos megjegyzés

Az alkalmazás jelenleg SQLite-ot használ. Kis-közepes forgalmú, egyszerű eseménykezeléshez ez még teljesen jó lehet, de érdemes rendszeresen menteni az adatbázist.

## Render deploy (adatmegorzeshez)

A repoban van egy `render.yaml`, ami ugy van beallitva, hogy:

- a web service `starter` plan-on fusson
- legyen Persistent Disk mountolva ide: `/opt/render/project/src/static/user-data`
- az SQLite adatbazis fajl is ezen a diszken legyen:
  `DATABASE_PATH=/opt/render/project/src/static/user-data/triatlon.sqlite3`
- a feltoltott kepek/avatarok is ezen a diszken maradjanak meg

Mi fog megmaradni ujrainditas utan:

- esemenyek es nevezesek (adatbazis)
- feltoltott eredmenykepek
- feltoltott avatarok
- feltoltott versenyszam-kepek

Fontos Render limitacio:

- a Free web service alvas utan felkel, de nem tamogat Persistent Disket
- ha biztos adatmegorzes kell (kepek + sqlite), web service plan legyen legalabb `starter`

Render dokumentacio:

- https://render.com/free

### Meglevo online adatok megtartasa (ajanlott lepesek)

1. A regi szolgaltatasrol mentsd le az aktualis `triatlon.sqlite3` fajlt.
2. Az uj Render service-ben engedelyezd a Persistent Disket (a fenti mount path-ra).
3. Masold be a mentett DB fajlt a diszkre:
   `/opt/render/project/src/static/user-data/triatlon.sqlite3`
4. Deploy utan ellenorizd az admin dashboardot es a publikus esemenylistat.

Ha a regi service is SQLite-ot hasznalt es nem volt persistent diszk, akkor a legutolso allapot csak akkor mentheto, ha meg most le tudod menteni a fajlt a regi instance-bol.