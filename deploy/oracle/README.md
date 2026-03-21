# Oracle Cloud Always Free deploy

Ez a projekt Oracle Cloud Always Free VM-re van elokeszitve, ahol nincs platform oldali altatas ugy, mint a klasszikus free web service hosztokon.

## Javasolt cel

- Ubuntu 22.04 VM
- 1 publikus IP
- `nginx` reverse proxy
- `gunicorn` + `systemd`
- SQLite adatfajl kulon adatmappaban

## Javasolt mappaszerkezet

```text
/opt/triatlon
```

## .env pelda

```env
SECRET_KEY=csereld-erosebbre
ADMIN_PASSWORD=csereld-erosebbre
DATABASE_PATH=/opt/triatlon/data/triatlon.sqlite3
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
```

Adatmapa letrehozasa:

```bash
mkdir -p /opt/triatlon/data
```

## Telepites

1. Masold fel a projektet a VM-re az `/opt/triatlon` ala.
2. Hozd letre az `.env` fajlt.
3. Futtasd:

```bash
cd /opt/triatlon
bash deploy/oracle/install.sh
```

## HTTPS

Ha domain is lesz:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d pelda.hu -d www.pelda.hu
```

## Frissites

```bash
cd /opt/triatlon
git pull
./.venv/bin/pip install -r requirements.txt
sudo systemctl restart triatlon
```

## Mentes

Az app jelenleg SQLite-ot hasznal, ezert erdemes rendszeres mentest csinalni:

```bash
cp /opt/triatlon/data/triatlon.sqlite3 /opt/triatlon/data/triatlon.sqlite3.bak
```
