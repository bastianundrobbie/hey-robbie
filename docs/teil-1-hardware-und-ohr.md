# Sprachassistent selbst bauen mit Raspberry Pi, Teil 1: Hardware und Ohr

> Kapitel 1 der Anleitungsreihe. Dieser Text ist zugleich das Drehbuch von
> Teil 1 auf YouTube (@BastianUndRobbie): Der Fließtext ist das Voice-over,
> die Code-Blöcke sind das, was auf dem Bildschirm steht. Was hier steht,
> gilt; das Video zeigt es nur.
>
> **Meilenstein dieses Teils:** Du sagst „Hey Robbie", und das Speakerphone
> piept. Sprechen lernt er in Teil 2.

---

## Worum es geht

Das ist ein Speakerphone für zehn Euro, ein Raspberry Pi und ein Piepton.
Der Piepton heißt: Robbie hat seinen Namen gehört. Mehr passiert in diesem
Teil nicht, und genau das ist der Plan. Am Ende dieses Teils steht die
komplette Hardware, der Server läuft, und das Ohr funktioniert. Antworten
kommt in Teil 2, mit drei Schlüsseln und einem ersten Satz.

Diese Reihe ist die Anleitung zur Bauserie „Der Bastian". Dort habe ich
erzählt, wie Robbie entstanden ist. Hier baust du ihn nach, mit dem echten
Code aus meinem Haus. Sechs Teile, ein Teil pro Woche. Nach Teil 2 läuft
er. Ab Teil 3 geht es ums Verstehen und Anpassen: was in der Sekunde nach
„Hey Robbie" passiert, wie man ihn erzieht, wie er Werkzeuge bekommt, und
was das alles kostet.

Der Bauplan in vier Sätzen. Im Wohnzimmer steht ein Raspberry Pi mit einem
Speakerphone. Der Pi rechnet nichts, er schickt den Ton ununterbrochen an
einen Server im Haus. Auf dem Server lauscht ein kleines Programm in
diesem Strom auf zwei Wörter. Erst wenn die fallen, geht in Teil 2 etwas
nach draußen, zu drei Diensten für Hören, Denken und Sprechen. Heute
bleibt alles im Haus.

Der Code liegt hier: <https://github.com/bastianundrobbie/hey-robbie>. Du brauchst ihn zweimal, einmal auf dem
Server und einmal auf dem Pi.

---

## Einkaufsliste

Preise sind Größenordnungen, Stand September 2026, gebraucht oder neu wie
angegeben. Wer den Server schon hat, landet bei sechzig bis hundert Euro.

| Teil | Wofür | Größenordnung |
|---|---|---|
| Raspberry Pi 4 (2 GB) oder Pi 3 B+ | die Sprechstelle | Pi 4 neu ca. 50 €, Pi 3 gebraucht ca. 20 € |
| Offizielles Netzteil (USB-C beim Pi 4, Micro-USB 2,5 A beim Pi 3) | Strom, hier nicht sparen | ca. 10 € |
| microSD-Karte, 16 bis 32 GB | Betriebssystem des Pi | ca. 8 € |
| Jabra Speak 410, gebraucht | Ohr und Mund | 10 bis 25 € |
| Gehäuse für den Pi | optional | 5 bis 10 € |
| Ein Rechner, auf dem Docker läuft (NAS, Mini-PC, alter Laptop) | der Server | schon da; sonst gebraucht ab ca. 100 € |

Zum Pi: Bei mir läuft ein Pi 3, ein Pi 4 ist die bequemere Wahl. Beide
reichen dicke, denn der Pi rechnet nichts. Er nimmt Ton auf und spielt Ton
ab. Der Pi Zero 2 W ginge vermutlich auch, das habe ich nicht getestet.

Zum Speakerphone: Das Jabra Speak 410 ist ein Konferenzlautsprecher aus
Büroauflösungen, deshalb so billig. Es hat zwei Eigenschaften, die hier
zählen. Erstens läuft es unter Linux ohne Treiber als USB-Audiogerät.
Zweitens hat es eine eingebaute Echo-Unterdrückung: Das Mikrofon hört den
eigenen Lautsprecher nicht. Deshalb kann Robbie später zuhören, während er
selbst spricht, und du kannst ihn unterbrechen. Ein anderes USB-Speakerphone
mit Echo-Unterdrückung tut es auch. Mikrofon und Lautsprecher aus der
Bastelkiste tun es nicht, dann hört er sich selbst.

Zum Netzteil: Der Pi 3 ist empfindlich. Ein schwaches Netzteil oder ein
USB-Hub am Rechner drosselt ihn, und dann wird Robbie taub, ohne eine
Fehlermeldung. Wie du das prüfst, kommt gleich.

---

## Voraussetzungen

Was du brauchst, bevor es losgeht:

- Einen Rechner im Haus, auf dem Docker und Docker Compose laufen. Linux
  ist der einfache Weg. Ein NAS mit Docker geht, ein Mini-PC geht, ein
  alter Laptop mit Ubuntu geht.
- Pi und Server im selben Heimnetz. WLAN reicht, bei mir läuft der Pi im
  WLAN.
- Ein Terminal und SSH. Du musst keine Programmiererin und kein
  Programmierer sein, aber Befehle abtippen und eine Textdatei ändern
  solltest du dir zutrauen.
- Zeit: ein Nachmittag. Das Training des Wake Words läuft nebenbei.

Was du heute nicht brauchst: Konten und Schlüssel. Die drei Dienste für
Hören, Denken und Sprechen kommen in Teil 2. Alles in diesem Teil ist
kostenlos, bis auf die Hardware.

---

## Den Pi aufsetzen

Erster Schritt: die Karte beschreiben. Nimm den Raspberry Pi Imager und
wähle Raspberry Pi OS Lite, 64 Bit. Lite heißt ohne Oberfläche, und die
brauchen wir nicht. In den Einstellungen des Imagers setzt du vier Dinge:
den Hostnamen, einen Benutzer, dein WLAN und SSH an. Ich nenne den Pi
`robbie-pi` und den Benutzer `pi`. Wenn du andere Namen wählst, merk sie
dir für die Service-Datei später.

Karte rein, Speakerphone an den USB, Strom dran. Nach einer Minute
erreichst du den Pi per SSH:

```bash
ssh pi@robbie-pi.local
```

Dann die Grundausstattung. Git holt später den Code, das Python-Modul
`venv` baut eine saubere Umgebung, und `alsa-utils` bringt die zwei
Werkzeuge mit, mit denen der Pi Ton aufnimmt und abspielt:

```bash
sudo apt update && sudo apt install -y git python3-venv alsa-utils
```

Jetzt das Speakerphone. Der Pi soll es unter einem festen Namen kennen,
nicht unter einer Nummer, denn die Nummer kann nach einem Neustart eine
andere sein. Der Befehl zeigt alle Aufnahmegeräte:

```bash
arecord -l
```

Beim Jabra steht dort eine Zeile wie `card 1: USB [Jabra SPEAK 410 USB]`.
Das Wort in eckigen Klammern vor dem Namen ist die Kennung, hier `USB`. Der
Code spricht das Gerät als `plughw:CARD=USB` an. Wenn bei deinem
Speakerphone etwas anderes in den Klammern steht, merk dir das für die
Service-Datei.

Ein Hörtest, zuerst in eine Richtung, dann in die andere. Drei Sekunden
aufnehmen, dann abspielen:

```bash
arecord -D plughw:CARD=USB -f S16_LE -r 16000 -c 1 -d 3 test.wav
aplay   -D plughw:CARD=USB test.wav
```

Wenn du dich selbst hörst, ist das Speakerphone fertig. Bleibt der Strom.
Der Befehl fragt den Pi, ob er sich jemals gedrosselt hat:

```bash
vcgencmd get_throttled
```

Die Antwort muss `throttled=0x0` sein. Steht da etwas anderes, ist das
Netzteil zu schwach oder das Kabel zu dünn. Das ist kein Software-Problem,
und du löst es nicht mit Software. Anderes Netzteil, und noch einmal
prüfen.

---

## Den Server vorbereiten

Jetzt auf den Server, per SSH oder direkt. Der Code kommt aus dem Repo,
und alle Einstellungen liegen in Vorlagen, die du einmal kopierst. Die
Vorlagen bleiben im Repo, deine Kopien bleiben lokal, und deine Kopien
werden nie ins Internet hochgeladen, das ist im Repo so eingestellt.

```bash
git clone https://github.com/bastianundrobbie/hey-robbie.git robbie
cd robbie
cp compose.example.yaml compose.yaml
cp .env.example .env
cp server/config/config.example.toml  server/config/config.toml
cp server/config/plugins.example.toml server/config/plugins.toml
cp server/config/prompt.example.toml  server/config/prompt.toml
```

Fünf Dateien, fünf Aufgaben. `compose.yaml` beschreibt die Kiste, in der
der Server läuft. `.env` ist der Platz für die drei Schlüssel, und die
bleibt heute leer. `config.toml` sind die Einstellungen, `plugins.toml`
die Werkzeuge, `prompt.toml` die Persönlichkeit. An keiner musst du heute
etwas ändern. Wir starten die Kiste trotzdem noch nicht, denn ihr fehlt
das Ohr.

---

## Das Wake Word

Das Ohr ist eine einzige Datei, etwa vierhundert Kilobyte groß. Ein
kleines neuronales Netz, das genau eine Sache kann: erkennen, ob gerade
jemand „Hey Robbie" gesagt hat. Es läuft mit openWakeWord, einem freien
Projekt von David Scripka, und es läuft auf dem Server, nicht auf dem Pi.

Diese Datei liegt nicht im Repo, und das ist Absicht. Mein Modell gehört
nicht mir, und Wake-Word-Modelle stehen unter Bedingungen, die nur die
private Nutzung erlauben. Also holst du dir dein eigenes, und das geht in
fünf Minuten.

Auf openwakeword.com gibt es eine Bibliothek mit fertigen Modellen, die
andere trainiert haben. Such dir dort ein Wake Word aus, lade die
ONNX-Datei herunter, fertig. ONNX ist das Format, das der Server liest.
Die Nutzung läuft zu den Bedingungen des Anbieters, für den privaten
Gebrauch ist sie frei. Zwei Regeln bei der Wahl: Zwei bis drei Silben,
und kein Wort, das im Alltag ständig fällt. „Hey Tim" wird ein schlechter
Tag für Tim.

Wer einen Namen will, den es dort nicht gibt, trainiert ihn selbst. Das
geht auf derselben Seite im Trainingscenter, kostenlos mit Konto: Wake
Word als Text eingeben, das Training läuft auf deren Rechnern, am Ende
kommt dieselbe ONNX-Datei heraus. Das läuft nebenbei, während du den Rest
dieses Teils erledigst.

Die Datei kommt auf den Server in den Ordner `models`, und sie heißt dort
`hey_robbie.onnx`. Der Dateiname ist der Schlüssel, unter dem der Server das
Modell aufruft, deshalb behältst du diesen Namen auch dann, wenn dein Wake
Word ganz anders heißt. Umbenennen genügt. Robbie hört dann eben auf den
Namen, den du gewählt hast.

```bash
cp ~/Downloads/<dein-modell>.onnx models/hey_robbie.onnx
```

Modelle kannst du später jederzeit tauschen. Du legst die neue Datei
hinein und verbindest den Pi neu, das Ohr wird beim Verbinden geladen.

---

## Die Kiste starten

Jetzt ist alles da: Code, Einstellungen, Ohr. Ein Befehl baut die Kiste
und startet sie. Beim ersten Mal dauert das ein paar Minuten, es werden
einige hundert Megabyte geladen.

```bash
docker compose up -d --build
docker compose logs -f
```

Im Protokoll siehst du drei Dinge. Erstens zwei Warnungen, dass Schlüssel
fehlen. Die sind heute richtig. Zweitens die Zeile, dass das Modell
geladen wird, sobald sich der Pi meldet. Drittens, dass der Server auf
Port 8422 lauscht. Ob er erreichbar ist, fragst du so:

```bash
curl http://localhost:8422/api/health
```

Und dann merkst du dir die Adresse des Servers im Heimnetz. Die braucht
gleich der Pi:

```bash
hostname -I
```

---

## Den Pi verbinden

Zurück auf den Pi. Auch hier kommt der Code aus dem Repo, aber der Pi
braucht nur einen Ordner davon, `pi_client`. Ein einziges Python-Paket
dazu, `websockets`, das ist die Leitung zum Server.

```bash
git clone https://github.com/bastianundrobbie/hey-robbie.git ~/robbie
cp -r ~/robbie/pi_client ~/pi_client
cd ~/pi_client
python3 -m venv .venv
.venv/bin/pip install websockets
```

Erster Versuch von Hand, mit der Adresse des Servers. Der Befehl verbindet
sich einmal und beendet sich, wenn die Verbindung abreißt:

```bash
ROBBIE_SERVER_HOST=<SERVER-IP> .venv/bin/python station.py --once
```

Zwei Dinge passieren. Im Terminal steht `connected`, und aus dem
Speakerphone kommt ein kurzer tiefer Ton. Das ist das Signal: Verbindung
steht, der Pi schickt ab jetzt Ton. Und jetzt der Moment.

Sag „Hey Robbie".

Ein hoher Piepton. Im Terminal steht `wake accepted` und eine Zahl,
der Score, wie sicher das Ohr sich war. Direkt danach eine zweite Zeile:
`turn aborted`, weil der Schlüssel fürs Hören fehlt. Genau so soll es
heute aussehen. Robbie hat seinen Namen gehört, und mehr kann er noch
nicht.

Auf dem Server siehst du dasselbe von der anderen Seite:

```bash
docker compose logs -f | grep duplex
```

Dort steht `wakeword detected` mit demselben Score.

Damit der Pi das ab jetzt von allein tut, bei jedem Start, wird das
Programm ein Dienst. Die Datei dafür liegt im Ordner. Drei Zeilen darin
musst du prüfen: den Benutzer, wenn er nicht `pi` heißt, die Adresse des
Servers, und die Kennung des Speakerphones, wenn sie nicht `USB` ist.

```bash
nano robbie-station.service
```

```ini
User=pi
Environment=ROBBIE_SERVER_HOST=<SERVER-IP>
Environment=ROBBIE_ALSA_DEV=plughw:CARD=USB
```

Dann den Dienst einrichten und einschalten:

```bash
sudo cp robbie-station.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robbie-station
journalctl -u robbie-station -f
```

Der tiefe Ton kommt noch einmal. Ab jetzt ist der Pi ein Gerät: Strom
dran, und nach einer Minute hört er.

---

## Meilenstein: der Piepton

Das ist der Stand nach Teil 1. Ein Pi im Wohnzimmer, der ununterbrochen
Ton an eine Kiste im Büro schickt. Eine Kiste, die in diesem Strom auf zwei
Wörter lauscht. Und ein Piepton, wenn sie fallen. Nichts davon hat das
Haus verlassen.

Noch ein Wort zur Empfindlichkeit. Der Score liegt bei einem echten
„Hey Robbie" aus zwei, drei Metern zwischen 0,5 und 0,9. Die Schwelle, ab
der der Server anspringt, ist 0,4. Sie ist auf mein Wohnzimmer und mein
Speakerphone eingestellt. Wenn er dich nicht hört, steht der Score
trotzdem im Server-Protokoll, denn knappe Fehlversuche werden mitgeloggt.
Dann setzt du die Schwelle in der `compose.yaml` eine Stufe tiefer. Wenn
er von allein piept, eine Stufe höher.

```yaml
ROBBIE_WAKE_THRESHOLD: "0.4"
```

```bash
docker compose up -d
```

---

## Wenn es nicht piept

| Was du hörst oder siehst | Woran es liegt | Was du tust |
|---|---|---|
| Alle paar Sekunden ein tiefer Fehlerton, im Pi-Protokoll `connection failed` | der Pi erreicht den Server nicht | Adresse und Port prüfen, vom Pi aus `curl http://<SERVER-IP>:8422/api/health` |
| Verbindung steht, dann Fehlerton, im Protokoll `Voice-Init fehlgeschlagen` | das Modell fehlt in `models` oder heißt nicht `hey_robbie.onnx` | Datei prüfen, dann den Pi-Dienst neu starten |
| Kein Piepton, im Server-Protokoll Scores unter 0,4 | zu leise, zu weit weg, oder die Schwelle passt nicht zu deinem Raum | näher ran, lauter, dann Schwelle senken |
| Piept ohne Anlass | Schwelle zu niedrig, oder das Wake Word ähnelt einem Alltagswort | Schwelle heben; im Zweifel neues Wake Word trainieren |
| `audio open error` oder `Device or resource busy` | zwei Programme wollen das Speakerphone | nur eins darf: vor einem Handtest `sudo systemctl stop robbie-station` |
| Der Pi wird nach einer Weile taub, `get_throttled` ist nicht `0x0` | Unterspannung | Netzteil und Kabel tauschen, keinen Hub |
| `arecord -l` zeigt kein Gerät | Speakerphone nicht erkannt | anderer USB-Port, anderes Kabel, `dmesg` ansehen |

---

## Ausblick auf Teil 2

Robbie hat ein Ohr und einen Mund, und er kennt seinen Namen. In Teil 2
bekommt er drei Schlüssel, einen für das Hören, einen für das Denken,
einen für die Stimme. Dann passt du seine Persönlichkeit an, ein
Textdokument mit einer erfundenen Familie, in das du deine eigene
schreibst. Und dann sagt er seinen ersten Satz.

🤖 *Ein Piepton. Ich hatte mir meinen ersten Auftritt größer vorgestellt.*
