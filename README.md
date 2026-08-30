# 720pier → Torznab

Der Dienst liest den offiziellen 720pier-NFL-Feed, entfernt Antwort-Posts, löst echte Torrent-Beiträge auf und stellt sie Sportarr als Torznab-Indexer bereit. Torrent-Dateien werden über den Adapter heruntergeladen, damit das private 720pier-Cookie niemals an Sportarr oder Transmission weitergegeben wird.

## Einrichtung

1. `.env.example` nach `.env` kopieren.
2. In `.env` einen lokalen `API_KEY` setzen.
3. In Chrome bei 720pier anmelden und `PIER_COOKIE` wie unten beschrieben eintragen.
4. In `docker-compose.yml` `BASE_URL` auf die für Sportarr erreichbare Adresse setzen.
5. Starten:

   ```sh
   docker compose up -d --build
   ```

## Cookie aus Chrome übernehmen

1. 720pier öffnen und angemeldet bleiben.
2. Entwicklertools mit `F12` öffnen.
3. `Network` wählen und die Seite neu laden.
4. Den Request `viewtopic.php` anklicken.
5. Unter `Headers` bei `Request Headers` den Wert von `Cookie` kopieren.
6. Den kompletten Wert lokal hinter `PIER_COOKIE=` in `.env` einsetzen.

Die `.env`-Datei nicht hochladen, teilen oder in Git einchecken. Das Cookie gewährt Zugriff auf dein Konto und kann ablaufen. Bei einem HTTP-401 vom Download-Endpunkt das Cookie erneuern.

## Sportarr

- Torznab URL: `http://HOST-IP:8788/api`
- API-Key: Wert aus `.env`
- Kategorie: `5000`
- Minimum Seeders: kann auf `1` bleiben, da der Adapter echte Seeder-Zahlen übernimmt

Tests:

```sh
curl 'http://HOST-IP:8788/api?t=caps&apikey=DEIN_KEY'
curl 'http://HOST-IP:8788/api?t=search&q=NFL%202026&apikey=DEIN_KEY'
```

Der Feed enthält auch Antworten auf bestehende Themen. Der Adapter übernimmt deshalb ausschließlich Feed-Einträge, deren Inhalt einen `.torrent`-Anhang nennt. Größen, Seeder, Leecher und der echte Download werden anschließend aus der zugehörigen Themenseite gelesen und fünf Minuten zwischengespeichert.

