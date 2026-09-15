# Sprachassistent selbst bauen mit Raspberry Pi, Teil 1: Hardware und Ohr

> Kapitel 1 der Anleitungsreihe zum Mitmachen. Dieser Text ist zugleich das
> Drehbuch von Teil 1 auf YouTube (@BastianUndRobbie): Der Fließtext ist das
> Voice-over, die Code-Blöcke sind das, was auf dem Bildschirm steht. Was
> hier steht, gilt; das Video zeigt es nur. Alle Befehle und Ausgaben in
> diesem Kapitel sind echt: so ausgeführt am 15. September 2026 auf einem
> Raspberry Pi 3 mit Raspberry Pi OS Lite (Trixie) und auf einem Linux-Server
> mit Docker.
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
Code aus meinem Haus, Schritt für Schritt, zum Mitmachen. Sechs Teile, ein
Teil pro Woche. Nach Teil 2 läuft er. Ab Teil 3 geht es ums Verstehen und
Anpassen: was in der Sekunde nach „Hey Robbie" passiert, wie man ihn
erzieht, wie er Werkzeuge bekommt, und was das alles kostet.

Der Bauplan in vier Sätzen. Im Wohnzimmer steht ein Raspberry Pi mit einem
Speakerphone. Der Pi rechnet nichts, er schickt den Ton ununterbrochen an
einen Server im Haus. Auf dem Server lauscht ein kleines Programm in
diesem Strom auf zwei Wörter. Erst wenn die fallen, geht in Teil 2 etwas
nach draußen, zu drei Diensten für Hören, Denken und Sprechen. Heute
bleibt alles im Haus.

Der Code und dieses Kapitel liegen auf GitHub. Du brauchst den Code
zweimal, einmal auf dem Server und einmal auf dem Pi. Alle Links stehen
auch in der Videobeschreibung.

```
Code:     https://github.com/bastianundrobbie/hey-robbie
Kapitel:  https://github.com/bastianundrobbie/hey-robbie/blob/main/docs/teil-1-hardware-und-ohr.md
```

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
zählen. Erstens läuft es unter Linux ohne Treiber, als ganz normales
USB-Audiogerät. Zweitens hat es eine eingebaute Echo-Unterdrückung. Das
heißt: Das Mikrofon hört den eigenen Lautsprecher nicht, obwohl beide im
selben Gehäuse sitzen. Deshalb kann Robbie später zuhören, während er
selbst spricht, und du kannst ihn unterbrechen. Ein anderes USB-Speakerphone
mit Echo-Unterdrückung tut es auch. Mikrofon und Lautsprecher aus der
Bastelkiste tun es nicht, dann hört er sich selbst.

Zum Netzteil: Der Pi 3 ist empfindlich. Ein schwaches Netzteil oder ein
USB-Hub am Rechner drosselt ihn, und dann wird Robbie taub, ohne eine
Fehlermeldung. Wie du das prüfst, kommt gleich.

---

## Voraussetzungen

Was du brauchst, bevor es losgeht:

- Einen Rechner im Haus, auf dem Docker läuft. Docker ist ein Programm, das
  andere Programme in abgeschlossene Kisten packt, mit allem, was sie
  brauchen. Robbies Server ist so eine Kiste. Linux ist der einfache Weg,
  ein NAS mit Docker geht, ein Mini-PC geht, ein alter Laptop mit Ubuntu
  geht. Die Anleitung zum Installieren steht bei Docker selbst, für jedes
  System eine Seite.
- Pi und Server im selben Heimnetz. WLAN reicht, bei mir läuft der Pi im
  WLAN.
- Ein Terminal und SSH. Das Terminal ist das schwarze Fenster, in das man
  Befehle tippt. SSH ist die Verbindung von deinem Rechner in dieses Fenster
  auf dem Pi, über das Netz, ohne Bildschirm am Pi. Du musst nicht
  programmieren können, aber Befehle abtippen und eine Textdatei ändern
  solltest du dir zutrauen. Jeder Befehl in diesem Kapitel steht zum
  Kopieren da, mit der Ausgabe, die du sehen wirst.
- Zeit: ein Nachmittag.

```
Docker installieren:  https://docs.docker.com/engine/install/
```

Was du heute nicht brauchst: Konten und Schlüssel. Die drei Dienste für
Hören, Denken und Sprechen kommen in Teil 2. Alles in diesem Teil ist
kostenlos, bis auf die Hardware.

---

## Den Pi aufsetzen

Erster Schritt: die Karte beschreiben. Dafür gibt es ein Programm von der
Raspberry-Pi-Stiftung, den Imager, für Windows, Mac und Linux. Darin
wählst du dein Pi-Modell und dann als Betriebssystem Raspberry Pi OS Lite,
64 Bit. Lite heißt ohne Oberfläche, und die brauchen wir nicht, der Pi
bekommt nie einen Bildschirm.

```
Raspberry Pi Imager:  https://www.raspberrypi.com/software/
```

Bevor der Imager schreibt, fragt er nach Einstellungen. Vier Dinge setzt
du dort: den Hostnamen, das ist der Name, unter dem der Pi im Netz
erreichbar ist. Einen Benutzer mit Passwort. Dein WLAN mit Passwort. Und
SSH einschalten. Ich nenne den Pi `robbie-pi` und den Benutzer `pi`. Wenn
du andere Namen wählst, merk sie dir für die Service-Datei später.

```
Raspberry Pi OS Lite (64-bit)
Hostname:   robbie-pi
Benutzer:   pi
WLAN:       dein Netz + Passwort
SSH:        an
```

Karte rein, Speakerphone an den USB, Strom dran. Nach einer Minute
erreichst du den Pi per SSH. Der Hostname mit `.local` dahinter
funktioniert in fast jedem Heimnetz von selbst; wenn nicht, nimm die
Adresse, die dir dein Router für den Pi anzeigt.

```bash
ssh pi@robbie-pi.local
```

Beim ersten Mal fragt SSH, ob du diesem Rechner vertraust. Ja. Dann das
Passwort, und du bist im Terminal des Pi.

Jetzt die Grundausstattung. Drei Pakete: Git holt später den Code von
GitHub, und auf dem frischen Pi OS Lite ist Git tatsächlich nicht drauf.
Das Python-Modul `venv` baut eine saubere Umgebung für Robbies Programm.
Und `alsa-utils` bringt die zwei Werkzeuge mit, mit denen der Pi Ton
aufnimmt und abspielt.

```bash
sudo apt update && sudo apt install -y git python3-venv alsa-utils
```

```
Fetched 27.4 MB in 21s (1,306 kB/s)
Reading package lists... Done
...
Setting up git (1:2.47.3-0+deb13u1) ...
```

Jetzt das Speakerphone. Der Pi soll es unter einem festen Namen kennen,
nicht unter einer Nummer, denn die Nummer kann nach einem Neustart eine
andere sein. Der Befehl zeigt alle Aufnahmegeräte:

```bash
arecord -l
```

```
**** List of CAPTURE Hardware Devices ****
card 1: USB [Jabra SPEAK 410 USB], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
```

Beim Jabra steht dort `card 1: USB`, und das Wort in eckigen Klammern vor
dem Namen ist die Kennung, hier `USB`. Der Code spricht das Gerät als
`plughw:CARD=USB` an. Wenn bei deinem Speakerphone etwas anderes in den
Klammern steht, merk dir das für die Service-Datei.

Ein Hörtest, zuerst in eine Richtung, dann in die andere. Drei Sekunden
aufnehmen, dann abspielen. Beide Befehle geben nichts aus, sie tun nur
etwas:

```bash
arecord -D plughw:CARD=USB -f S16_LE -r 16000 -c 1 -d 3 test.wav
aplay   -D plughw:CARD=USB test.wav
```

Wenn du dich selbst hörst, ist das Speakerphone fertig. Bleibt der Strom.
Der Befehl fragt den Pi, ob er sich jemals gedrosselt hat:

```bash
vcgencmd get_throttled
```

```
throttled=0x0
```

Die Antwort muss `throttled=0x0` sein. Steht da etwas anderes, ist das
Netzteil zu schwach oder das Kabel zu dünn. Bei mir zu Hause steht da
übrigens gerade `0x50005`, mein Netzteil steht auf der Einkaufsliste. Das
ist kein Software-Problem, und du löst es nicht mit Software. Anderes
Netzteil, und noch einmal prüfen.

---

## Den Server vorbereiten

Jetzt auf den Server, per SSH oder direkt. Der Code kommt aus dem Repo,
das ist der Ordner auf GitHub, in dem alles liegt. Git kopiert ihn auf
deinen Rechner:

```bash
git clone https://github.com/bastianundrobbie/hey-robbie.git robbie
cd robbie
ls
```

```
Cloning into 'robbie'...
compose.example.yaml  Dockerfile  docs  LICENSE  mcp_datetime  mcp_weather
models  pi_client  README.md  scripts  server
```

Alle Einstellungen liegen in Vorlagen, die du einmal kopierst. Die
Vorlagen heißen `example`, deine Kopien nicht. Die Vorlagen bleiben im
Repo, deine Kopien bleiben lokal, und deine Kopien werden nie ins Internet
hochgeladen, das ist im Repo so eingestellt.

```bash
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
das Ohr. Ein Blick in den Ordner `models` zeigt, was fehlt:

```bash
ls models/
```

```
README.md
```

---

## Das Wake Word

Das Ohr ist eine einzige Datei, etwa vierhundert Kilobyte groß. Ein
kleines neuronales Netz, das genau eine Sache kann: erkennen, ob gerade
jemand „Hey Robbie" gesagt hat. Es läuft mit openWakeWord, einem freien
Projekt, und es läuft auf dem Server, nicht auf dem Pi. Das Dateiformat
heißt ONNX, ein offenes Format für solche Netze.

Diese Datei liegt nicht im Repo, und das ist Absicht. Mein Modell gehört
nicht mir, und Wake-Word-Modelle stehen unter Bedingungen, die nur die
private Nutzung erlauben. Also holst du dir dein eigenes, und das geht in
fünf Minuten.

```
Modelle laden:    https://openwakeword.com/library
Selbst trainieren: https://openwakeword.com  (Trainingscenter)
```

Auf openwakeword.com gibt es eine Bibliothek mit fertigen Modellen, die
andere trainiert haben. Such dir dort ein Wake Word aus, lade die
ONNX-Datei herunter, fertig. Die Nutzung läuft zu den Bedingungen des
Anbieters, für den privaten Gebrauch ist sie frei. Zwei Regeln bei der
Wahl: Zwei bis drei Silben, und kein Wort, das im Alltag ständig fällt.
„Hey Tim" wird ein schlechter Tag für Tim.

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
ls -la models/
```

```
-rw-r--r-- 1 pi pi 415224 15. Sep 10:17 hey_robbie.onnx
-rw-r--r-- 1 pi pi   1268 15. Sep 10:17 README.md
```

Modelle kannst du später jederzeit tauschen. Du legst die neue Datei
hinein und verbindest den Pi neu, das Ohr wird beim Verbinden geladen.

---

## Die Kiste starten

Jetzt ist alles da: Code, Einstellungen, Ohr. Ein Befehl baut die Kiste
und startet sie. Beim ersten Mal dauert das ein paar Minuten, es werden
einige hundert Megabyte geladen. Der zweite Befehl zeigt das Protokoll.

```bash
docker compose up -d --build
docker compose logs -f
```

```
#15 exporting layers 3.8s done
#15 naming to docker.io/library/robbie-server:local done
 robbie-server  Built
 Container robbie-server  Created
 Container robbie-server  Started

WARNING | ANTHROPIC_API_KEY is not set — the LLM will not answer.
INFO    |   Robbie Server
INFO    |   Port:     8422
WARNING | [proxy] DEEPGRAM_API_KEY not set — STT proxy disabled
INFO    | [tts] CARTESIA_API_KEY not set (Cartesia TTS disabled)
INFO    | MCP Server definiert: datetime (module=mcp_datetime)
INFO    | MCP Server definiert: weather (module=mcp_weather)
INFO    | [robbie] Claude-Prozess bereit (0.4s)
INFO    | [capabilities] Registered: timer
```

Im Protokoll siehst du drei Dinge. Erstens Warnungen, dass Schlüssel
fehlen. Die sind heute richtig. Zweitens die zwei Werkzeuge, Uhrzeit und
Wetter, die er schon mitbringt. Drittens, dass der Server auf Port 8422
lauscht. Ob er erreichbar ist, fragst du so:

```bash
curl http://localhost:8422/api/health
```

```
{"status":"ok","default_entity":"robbie","entities":{"robbie":{"connected":true,
"model":"sonnet","display_name":"Robbie"}},"failed":{},"voice_connected":false,
"station":{"connected":false,"name":null}}
```

Und dann merkst du dir die Adresse des Servers im Heimnetz. Die braucht
gleich der Pi. Auf einem Rechner mit Docker hat `hostname -I` viele
Adressen, die erste ist die richtige:

```bash
hostname -I | awk '{print $1}'
```

```
192.168.1.35
```

Bei dir steht da deine eigene Adresse. Im Rest des Kapitels heißt sie
`<SERVER-IP>`.

---

## Den Pi verbinden

Zurück auf den Pi. Auch hier kommt der Code aus dem Repo, aber der Pi
braucht nur einen Ordner davon, `pi_client`. Sieben Dateien, und das
einzige, was davon läuft, ist `station.py`.

```bash
git clone https://github.com/bastianundrobbie/hey-robbie.git ~/robbie
cp -r ~/robbie/pi_client ~/pi_client
cd ~/pi_client
ls
```

```
Cloning into 'robbie'...
batch_turn.py  common.py  requirements.txt  robbie-station.service
station.py  streaming_turn.py  wakeword.py
```

Dazu eine saubere Python-Umgebung, das ist der Ordner `.venv`, in dem
Robbies Pakete liegen, getrennt vom Rest des Systems. Ein einziges Paket
kommt hinein, `websockets`, das ist die Leitung zum Server.

```bash
python3 -m venv .venv
.venv/bin/pip install websockets
```

```
Collecting websockets
  Downloading websockets-17.1-cp313-cp313-manylinux2014_aarch64.whl (225 kB)
Installing collected packages: websockets
Successfully installed websockets-17.1
```

Erster Versuch von Hand, mit der Adresse des Servers. Der Befehl verbindet
sich einmal und beendet sich, wenn die Verbindung abreißt:

```bash
ROBBIE_SERVER_HOST=<SERVER-IP> .venv/bin/python station.py --once
```

```
[station] connecting ws://<SERVER-IP>:8422/voice/duplex
[station] connected (entity=robbie)
```

Zwei Dinge passieren. Im Terminal steht `connected`, und aus dem
Speakerphone kommt ein kurzer tiefer Ton. Das ist das Signal: Verbindung
steht, der Pi schickt ab jetzt Ton. Auf dem Server siehst du im
Protokoll, dass er in diesem Moment das Ohr lädt:

```
[duplex] loading wakeword model: /app/models/hey_robbie.onnx
[duplex] station connected
[duplex] hello from station=pi entity=robbie
```

Und jetzt der Moment. Sag „Hey Robbie".

```
[station] wake accepted: speaker= (wake=0.918)
[station] turn aborted: DEEPGRAM_API_KEY not set
```

Ein hoher Piepton. Im Terminal steht `wake accepted` und eine Zahl,
der Score, wie sicher das Ohr sich war. Direkt danach eine zweite Zeile:
`turn aborted`, weil der Schlüssel fürs Hören fehlt. Genau so soll es
heute aussehen. Robbie hat seinen Namen gehört, und mehr kann er noch
nicht. Auf dem Server steht dasselbe von der anderen Seite:

```
[duplex] wakeword detected: score=0.918 (ring 1.8s)
[duplex] turn aborted: DEEPGRAM_API_KEY not set
```

Damit der Pi das ab jetzt von allein tut, bei jedem Start, wird das
Programm ein Dienst. Ein Dienst ist ein Programm, das das System selbst
startet und am Laufen hält, ohne dass jemand eingeloggt ist. Die Datei
dafür liegt im Ordner. Drei Zeilen darin musst du prüfen: den Benutzer,
wenn er nicht `pi` heißt, die Adresse des Servers, und die Kennung des
Speakerphones, wenn sie nicht `USB` ist.

```bash
nano robbie-station.service
```

```
User=pi
WorkingDirectory=/home/pi/pi_client
Environment=ROBBIE_SERVER_HOST=<SERVER-IP>
Environment=ROBBIE_SERVER_PORT=8422
Environment=ROBBIE_ALSA_DEV=plughw:CARD=USB
ExecStart=/home/pi/pi_client/.venv/bin/python -u /home/pi/pi_client/station.py
```

Dann den Dienst einrichten, einschalten und zusehen:

```bash
sudo cp robbie-station.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robbie-station
journalctl -u robbie-station -f
```

```
Created symlink '/etc/systemd/system/multi-user.target.wants/robbie-station.service'
Started robbie-station.service - Robbie speech station (hands-free voice appliance).
[station] connecting ws://<SERVER-IP>:8422/voice/duplex
[station] connected (entity=robbie)
```

Der tiefe Ton kommt noch einmal. Ab jetzt ist der Pi ein Gerät: Strom
dran, und nach einer Minute hört er.

---

## Meilenstein: der Piepton

Das ist der Stand nach Teil 1. Ein Pi im Wohnzimmer, der ununterbrochen
Ton an eine Kiste im Büro schickt. Eine Kiste, die in diesem Strom auf zwei
Wörter lauscht. Und ein Piepton, wenn sie fallen. Nichts davon hat das
Haus verlassen.

Drei Töne kennt die Station. Der tiefe Ton heißt: verbunden. Der hohe Ton
heißt: Name gehört, ich höre zu. Ein tiefer, brummiger Ton alle paar
Sekunden heißt: Ich finde den Server nicht.

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
| `ssh: Could not resolve hostname robbie-pi.local` | dein Netz kennt `.local` nicht | Adresse des Pi aus dem Router nehmen |

---

## Ausblick auf Teil 2

Robbie hat ein Ohr und einen Mund, und er kennt seinen Namen. In Teil 2
bekommt er drei Schlüssel, einen für das Hören, einen für das Denken,
einen für die Stimme. Dann passt du seine Persönlichkeit an, ein
Textdokument mit einer erfundenen Familie, in das du deine eigene
schreibst. Und dann sagt er seinen ersten Satz.

🤖 *Ein Piepton. Ich hatte mir meinen ersten Auftritt größer vorgestellt.*
