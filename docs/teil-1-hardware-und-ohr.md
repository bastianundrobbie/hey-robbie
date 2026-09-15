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
Code aus meinem Haus, Schritt für Schritt, zum Mitmachen. Jeder Befehl
steht im Bild, mit der Ausgabe, die du sehen wirst, und ich sage jedes Mal
dazu, wo du ihn eintippst. Sechs Teile, ein Teil pro Woche. Nach Teil 2
läuft er. Ab Teil 3 geht es ums Verstehen und Anpassen: was in der Sekunde
nach „Hey Robbie" passiert, wie man ihn erzieht, wie er Werkzeuge bekommt,
und was das alles kostet.

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

**Der Server.** Ein Rechner im Haus, der Tag und Nacht läuft und auf dem
Docker installiert ist. Docker ist ein Programm, das andere Programme in
abgeschlossene Kisten packt, mit allem, was sie brauchen: einem eigenen
kleinen Linux, den passenden Bibliotheken, den Einstellungen. So eine
Kiste heißt Container. Robbies Server ist so eine Kiste, und Docker baut
sie aus einer einzigen Datei zusammen. Was für ein Rechner das ist, ist
egal: ein NAS mit Docker aus dem App-Katalog, ein Mini-PC, ein alter
Laptop. Linux ist der einfache Weg. Ubuntu ist die verbreitetste
Linux-Ausgabe für den Hausgebrauch, kostenlos, und die Docker-Anleitung
dafür ist die ausführlichste. Windows geht auch: Dort heißt das Programm
Docker Desktop, und die Befehle sind dieselben, getippt in der
PowerShell statt im Linux-Terminal.

```
Docker installieren:  https://docs.docker.com/engine/install/        (Linux)
Docker Desktop:       https://docs.docker.com/desktop/                (Windows, Mac)
```

**Das Heimnetz.** Pi und Server müssen im selben Netz sein, also am
selben Router. WLAN reicht, bei mir läuft der Pi im WLAN.

**Ein Terminal.** Das ist das schwarze Fenster, in das man Befehle tippt.
Unter Linux und auf dem Mac heißt es Terminal, unter Windows nimmst du die
PowerShell oder das Windows Terminal. Jeder Befehl in diesem Kapitel steht
zum Kopieren da, mit der Ausgabe, die du sehen wirst, und ich sage jedes
Mal, auf welchem Rechner du ihn eintippst: auf dem Server oder auf dem Pi.

**SSH.** Der Pi hat keinen Bildschirm und keine Tastatur. SSH ist der Weg
hinein: Du tippst auf deinem eigenen Rechner einen Befehl, und ab dann ist
dein Terminal das Terminal des Pi, über das Netz. SSH ist auf Linux, Mac
und aktuellem Windows schon eingebaut, du musst nichts installieren.

**Eine Textdatei ändern.** Zweimal in diesem Teil ändern wir ein paar
Zeilen in einer Datei. Auf dem Pi geht das mit `nano`, einem kleinen
Editor im Terminal: öffnen, mit den Pfeiltasten hin, tippen, Strg+O und
Enter zum Speichern, Strg+X zum Schließen. Auf dem Server unter Linux
genauso. Unter Windows öffnest du die Datei mit dem normalen Editor.

**Zeit:** ein Nachmittag.

**Konten und Schlüssel:** brauchst du heute nicht. Robbie bezieht Hören,
Denken und Sprechen in Teil 2 von drei Diensten im Internet, und jeder
davon gibt dir dafür einen Schlüssel, eine lange Zeichenkette, die du in
eine Datei einträgst. Heute bleibt die Datei leer, und alles in diesem
Teil ist kostenlos, bis auf die Hardware.

---

## Den Pi aufsetzen

**Die Karte beschreiben.** Dafür gibt es ein Programm von der
Raspberry-Pi-Stiftung, den Imager, für Windows, Mac und Linux. Herunterladen,
installieren, die microSD-Karte in den Kartenleser deines Rechners
stecken, Imager starten.

```
Raspberry Pi Imager:  https://www.raspberrypi.com/software/
```

Der Imager fragt dich drei Dinge, von links nach rechts. Erstens das
Modell, dein Pi. Zweitens das Betriebssystem: Wähle „Raspberry Pi OS
(other)" und darin „Raspberry Pi OS Lite (64-bit)". Lite heißt ohne
Oberfläche, und die brauchen wir nicht, der Pi bekommt nie einen
Bildschirm. Drittens die SD-Karte. Dann „Weiter".

Jetzt fragt der Imager, ob du Einstellungen anpassen willst. Ja, das ist
der wichtige Teil, die Einstellungen sind hinter dem Knopf „Einstellungen
bearbeiten" versteckt. Zwei Reiter. Im Reiter „Allgemein": den Hostnamen
setzen, das ist der Name, unter dem der Pi im Netz erreichbar ist; einen
Benutzernamen mit Passwort; dein WLAN mit Passwort; deine Zeitzone. Im
Reiter „Dienste": SSH aktivieren, mit Passwort. Ohne diesen Haken kommst
du nie auf den Pi. Speichern, „Ja", die Karte wird geschrieben. Ich nenne
den Pi `robbie-pi` und den Benutzer `pi`. Wenn du andere Namen wählst, merk
sie dir für die Service-Datei später.

```
Imager → Weiter → „Einstellungen bearbeiten"
  Reiter Allgemein:  Hostname robbie-pi · Benutzer pi + Passwort · WLAN + Passwort · Zeitzone
  Reiter Dienste:    SSH aktivieren, Passwort-Anmeldung
→ Speichern → Ja
```

**Der erste Start.** Karte aus dem Rechner, in den Pi stecken, das
Speakerphone an einen der USB-Anschlüsse des Pi, dann das Netzteil. Der
Pi startet ohne Bildschirm, nach etwa einer Minute ist er im WLAN.

**Per SSH auf den Pi.** Jetzt auf deinem eigenen Rechner ein Terminal
öffnen, unter Windows die PowerShell, und diesen Befehl tippen. Er
funktioniert auf Windows, Mac und Linux gleich:

```bash
ssh pi@robbie-pi.local
```

`pi` ist der Benutzername, `robbie-pi.local` ist der Hostname aus dem
Imager mit `.local` dahinter. Das funktioniert in fast jedem Heimnetz von
selbst. Wenn nicht, hat der Pi trotzdem eine Adresse, und die zeigt dir
dein Router: In der Oberfläche des Routers gibt es eine Liste der Geräte
im Netz, bei der Fritzbox unter „Heimnetz", bei anderen unter „Geräte"
oder „DHCP". Dort steht `robbie-pi` mit einer Adresse wie `192.168.x.y`,
und die nimmst du statt des Namens.

Beim ersten Mal fragt SSH, ob du diesem Rechner vertraust. `yes`. Dann
das Passwort aus dem Imager, und ab jetzt ist dein Terminal das Terminal
des Pi: Vorne steht `pi@robbie-pi`.

```
The authenticity of host 'robbie-pi.local' can't be established.
Are you sure you want to continue connecting (yes/no)? yes
pi@robbie-pi.local's password:
pi@robbie-pi:~ $
```

**Die Grundausstattung.** Alles Folgende tippst du in diesem Fenster, auf
dem Pi. Drei Pakete: Git holt später den Code von GitHub, und auf dem
frischen Pi OS Lite ist Git tatsächlich nicht drauf. Das Python-Modul
`venv` legt später eine saubere Umgebung für Robbies Programm an. Und
`alsa-utils` bringt die zwei Werkzeuge mit, mit denen der Pi Ton aufnimmt
und abspielt. `sudo` davor heißt: mit Verwalterrechten, der Pi fragt dafür
noch einmal nach deinem Passwort.

```bash
sudo apt update && sudo apt install -y git python3-venv alsa-utils
```

```
Fetched 27.4 MB in 21s (1,306 kB/s)
Reading package lists... Done
...
Setting up git (1:2.47.3-0+deb13u1) ...
```

**Das Speakerphone beim Namen nennen.** Linux nummeriert Audiogeräte
durch, Karte null, Karte eins. Diese Nummer kann nach einem Neustart eine
andere sein, je nachdem, was zuerst erkannt wird. Deshalb spricht Robbies
Programm das Speakerphone nicht über die Nummer an, sondern über seine
Kennung, und die bleibt gleich. Der Befehl zeigt alle Aufnahmegeräte:

```bash
arecord -l
```

```
**** List of CAPTURE Hardware Devices ****
card 1: USB [Jabra SPEAK 410 USB], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
```

Beim Jabra steht dort `card 1`, und dahinter, vor der eckigen Klammer, die
Kennung: `USB`. Der Code spricht das Gerät als `plughw:CARD=USB` an. Wenn
bei deinem Speakerphone etwas anderes vor der Klammer steht, merk dir das
für die Service-Datei.

**Der Hörtest.** Zwei Befehle, nacheinander, auf dem Pi. Der erste nimmt
drei Sekunden über das Speakerphone auf und schreibt sie in eine Datei,
sag dabei irgendetwas. Der zweite spielt die Datei über das Speakerphone
ab. Beide Befehle geben nichts aus, sie tun nur etwas:

```bash
arecord -D plughw:CARD=USB -f S16_LE -r 16000 -c 1 -d 3 test.wav
aplay   -D plughw:CARD=USB test.wav
```

Wenn du dich selbst aus dem Speakerphone hörst, funktionieren beide
Richtungen: das Mikrofon und der Lautsprecher, und der Pi kennt das Gerät
unter seiner Kennung. Genau das braucht Robbie.

**Der Strom.** Der Befehl fragt den Pi, ob er sich jemals gedrosselt hat:

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

Jetzt auf den Server. Ein Terminal auf dem Server öffnen, per SSH oder
direkt an der Maschine, unter Windows die PowerShell. Alles in diesem
Abschnitt tippst du dort.

**Git.** Der Code liegt in einem Repo, das ist ein Ordner auf GitHub, in
dem alles liegt, mit seiner ganzen Geschichte. Git ist das Programm, das
so einen Ordner auf deinen Rechner kopiert und später Änderungen holt.
Ob es da ist, sagt dir ein Befehl. Wenn nicht: unter Linux
`sudo apt install git`, unter Windows von git-scm.com installieren.

```bash
git --version
```

```
git version 2.47.3
```

**Den Code holen.** Der Befehl kopiert das Repo in einen neuen Ordner
`robbie`. Danach wechseln wir mit `cd` in diesen Ordner hinein, und alle
weiteren Befehle laufen dort:

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

Ein Blick hinein: `server` ist Robbies Programm, `pi_client` das Programm
für den Pi, die zwei `mcp`-Ordner sind seine ersten Werkzeuge, Uhrzeit und
Wetter, und die Dateien mit `example` im Namen sind Vorlagen.

**Die Vorlagen kopieren.** Alle Einstellungen liegen in Vorlagen, die du
einmal kopierst. Die Vorlagen heißen `example`, deine Kopien nicht. Die
Vorlagen bleiben im Repo, deine Kopien bleiben lokal, und sie werden nie
ins Internet hochgeladen, das ist im Repo so eingestellt.

```bash
cp compose.example.yaml compose.yaml
cp .env.example .env
cp server/config/config.example.toml  server/config/config.toml
cp server/config/plugins.example.toml server/config/plugins.toml
cp server/config/prompt.example.toml  server/config/prompt.toml
```

Fünf Dateien, fünf Aufgaben. `compose.yaml` beschreibt die Kiste, die
Docker baut: welches Programm, welcher Port, welche Ordner. Ein Auszug:

```yaml
services:
  robbie-server:
    build: .
    ports:
      - "8422:8422"
    env_file:
      - .env
    environment:
      ROBBIE_STT_ENGINE: flux
      # ROBBIE_WAKE_THRESHOLD: "0.4"
    volumes:
      - ./server/config:/app/server/config:rw
      - ./models:/app/models:ro
```

`.env` ist der Platz für die drei Schlüssel aus Teil 2, einer für jeden
Dienst. Heute bleibt sie leer:

```
ANTHROPIC_API_KEY=
DEEPGRAM_API_KEY=
CARTESIA_API_KEY=
```

`config.toml` sind die Einstellungen, `plugins.toml` die Werkzeuge,
`prompt.toml` die Persönlichkeit. An keiner musst du heute etwas ändern.
Wir starten die Kiste trotzdem noch nicht, denn ihr fehlt das Ohr. Ein
Blick in den Ordner `models` zeigt, was fehlt:

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
Modelle laden:     https://openwakeword.com/library
Selbst trainieren: https://openwakeword.com   (Trainingscenter)
```

Auf openwakeword.com gibt es eine Bibliothek mit fertigen Modellen, die
andere trainiert haben. Such dir dort ein Wake Word aus, lade die
ONNX-Datei herunter, fertig. Die Nutzung läuft zu den Bedingungen des
Anbieters: privat frei, gewerblich kostet es. Zwei Regeln bei der Wahl.
Zwei bis drei Silben, damit das Ohr etwas zum Erkennen hat. Und kein
Wort, das im Alltag ständig fällt, denn das Ohr springt bei jedem Treffer
an. „Hey Tim" ist ein schlechtes Wake Word, wenn ein Tim im Haus wohnt:
Jedes Mal, wenn jemand Tim ruft, piept Robbie und hört zu.

Wer einen Namen will, den es dort nicht gibt, trainiert ihn selbst. Das
geht auf derselben Seite im Trainingscenter, mit Konto. Der Anbieter
nennt die Plattform kostenlos, Stand September 2026, prüf das auf der
Seite. Wake Word als Text eingeben, das Training läuft auf deren Rechnern,
am Ende kommt dieselbe ONNX-Datei heraus. Das läuft nebenbei, während du
den Rest dieses Teils erledigst.

Die Datei kommt auf den Server in den Ordner `models`, und sie heißt dort
`hey_robbie.onnx`. Der Dateiname ist der Schlüssel, unter dem der Server das
Modell aufruft, deshalb behältst du diesen Namen auch dann, wenn dein Wake
Word ganz anders heißt. Umbenennen genügt. Robbie hört dann eben auf den
Namen, den du gewählt hast. Auf dem Server, im Ordner `robbie`:

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

Jetzt ist alles da: Code, Einstellungen, Ohr. Auf dem Server, im Ordner
`robbie`, ein Befehl. Er baut die Kiste, das ist der Container mit Robbies
Programm, und startet sie im Hintergrund. Beim ersten Mal dauert das ein
paar Minuten, es werden einige hundert Megabyte geladen. Am Ende steht:
gebaut, erstellt, gestartet.

```bash
docker compose up -d --build
```

```
#15 exporting layers 3.8s done
#15 naming to docker.io/library/robbie-server:local done
 robbie-server  Built
 Container robbie-server  Created
 Container robbie-server  Started
```

Der zweite Befehl zeigt das Protokoll der Kiste, laufend, bis du Strg+C
drückst:

```bash
docker compose logs -f
```

```
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

Drei Dinge siehst du. Erstens Warnungen, dass Schlüssel fehlen: Anthropic
für das Denken, Deepgram für das Hören, Cartesia für das Sprechen. Das
ist heute richtig so, die Schlüssel kommen in Teil 2, und ohne sie kann
Robbie trotzdem seinen Namen hören. Zweitens die zwei Werkzeuge, Uhrzeit
und Wetter, die er schon mitbringt. Drittens, dass der Server auf Port
8422 lauscht. Ein Port ist die Türnummer, unter der ein Programm auf dem
Rechner erreichbar ist.

Ob er erreichbar ist, fragst du am einfachsten im Browser, auf dem Server
oder auf jedem Rechner im Haus: `http://<SERVER-IP>:8422/api/health`
eingeben. Oder im Terminal des Servers:

```bash
curl http://localhost:8422/api/health
```

```
{"status":"ok","default_entity":"robbie", ... "station":{"connected":false}}
```

Die Antwort ist eine Zeile in geschweiften Klammern, so sprechen
Programme miteinander. Wichtig ist nur das erste Wort: `status ok`. Und
ganz hinten steht `station connected false`: Der Server läuft, aber noch
ist kein Pi verbunden.

Dann merkst du dir die Adresse des Servers im Heimnetz. Die braucht gleich
der Pi. Auf einem Rechner mit Docker hat der Befehl dafür viele Adressen,
Docker legt für jede Kiste ein eigenes kleines Netz an. Die erste ist die
richtige. Unter Windows heißt der Befehl `ipconfig`, und du suchst die
Zeile „IPv4-Adresse" deiner Netzwerkkarte.

```bash
hostname -I | awk '{print $1}'
```

```
192.168.178.30
```

Bei dir steht da deine eigene Adresse. Im Rest des Kapitels heißt sie
`<SERVER-IP>`.

---

## Den Pi verbinden

Zurück in das SSH-Fenster des Pi. Auch hier kommt der Code aus dem Repo,
aber der Pi braucht nur einen Ordner davon, `pi_client`. Wir holen das
Repo, kopieren den Ordner ins Home-Verzeichnis und wechseln hinein.
Sieben Dateien liegen darin. Das Programm, das später dauerhaft läuft,
ist `station.py`, die Station.

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

**Eine saubere Umgebung.** Python-Programme brauchen Pakete, und die
sollen nicht im System des Pi herumliegen. `venv` legt dafür einen Ordner
`.venv` an, eine eigene kleine Python-Installation nur für Robbie. Die
Alternative wäre ein Werkzeug wie micromamba, das dasselbe macht; `venv`
ist schon dabei. Ein einziges Paket kommt hinein, `websockets`, das ist
die Leitung zum Server. `pip` ist das Programm, das Python-Pakete
installiert, und wir rufen es direkt aus dem Ordner `.venv` auf. So
musst du die Umgebung nie „betreten", jeder Befehl sagt selbst, welches
Python er meint.

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

**Erster Versuch von Hand.** Der Befehl startet die Station einmal, mit
der Adresse des Servers vorneweg, und beendet sich, wenn die Verbindung
abreißt. Trag deine Server-Adresse ein:

```bash
ROBBIE_SERVER_HOST=<SERVER-IP> .venv/bin/python station.py --once
```

```
[station] connecting ws://<SERVER-IP>:8422/voice/duplex
[station] connected (entity=robbie)
```

Zwei Dinge passieren. Im Terminal steht `connected`, und aus dem
Speakerphone am Pi kommt ein kurzer tiefer Ton. Das ist das Signal:
Verbindung steht, der Pi schickt ab jetzt Ton. Auf dem Server siehst du
im Protokoll, dass er in diesem Moment das Ohr lädt:

```
[duplex] loading wakeword model: /app/models/hey_robbie.onnx
[duplex] station connected
[duplex] hello from station=pi entity=robbie
```

Und jetzt der Moment. Sag „Hey Robbie", oder was immer dein Wake Word ist.

```
[station] wake accepted: speaker= (wake=0.918)
[station] turn aborted: DEEPGRAM_API_KEY not set
```

Ein hoher Piepton aus dem Speakerphone. Im Terminal steht `wake accepted`
und eine Zahl, der Score, wie sicher das Ohr sich war. Direkt danach eine
zweite Zeile: `turn aborted`, weil der Schlüssel fürs Hören fehlt. Genau
so soll es heute aussehen. Robbie hat seinen Namen gehört, und mehr kann
er noch nicht. Auf dem Server steht dasselbe von der anderen Seite:

```
[duplex] wakeword detected: score=0.918 (ring 1.8s)
[duplex] turn aborted: DEEPGRAM_API_KEY not set
```

Beende den Handversuch mit Strg+C.

**Die Station als Dienst.** Damit der Pi das ab jetzt von allein tut, bei
jedem Start, wird das Programm ein Dienst. Ein Dienst ist ein Programm,
das das System selbst startet und am Laufen hält, ohne dass jemand
eingeloggt ist. Die Beschreibung dafür liegt schon im Ordner, die Datei
`robbie-station.service`. Öffne sie mit `nano` und prüfe drei Zeilen: den
Benutzer, wenn er nicht `pi` heißt; die Adresse des Servers, dort trägst
du deine `<SERVER-IP>` ein; und die Kennung des Speakerphones, wenn sie
nicht `USB` ist. Speichern mit Strg+O und Enter, schließen mit Strg+X.

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

Dann vier Befehle. Der erste kopiert die Datei dorthin, wo das System
seine Dienste sucht. Der zweite sagt dem System, dass es dort neu
nachsehen soll. Der dritte schaltet den Dienst ein, jetzt und bei jedem
Start. Und der vierte zeigt, was der Dienst ausgibt, denn ein Dienst hat
kein eigenes Terminal, seine Ausgabe landet im Protokoll des Systems, und
`journalctl` liest es:

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

Der tiefe Ton kommt noch einmal aus dem Speakerphone. Ab jetzt ist der Pi
ein Gerät: Strom dran, und nach einer Minute hört er.

---

## Meilenstein: der Piepton

Das ist der Stand nach Teil 1. Ein Pi im Wohnzimmer, der ununterbrochen
Ton an eine Kiste im Büro schickt. Eine Kiste, die in diesem Strom auf zwei
Wörter lauscht. Und ein Piepton, wenn sie fallen. Nichts davon hat das
Haus verlassen.

Drei Töne kennt die Station, alle aus dem Speakerphone am Pi. Der tiefe
Ton heißt: verbunden. Der hohe Ton heißt: Name gehört, ich höre zu. Ein
tiefer, brummiger Ton alle paar Sekunden heißt: Ich finde den Server
nicht.

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
einen für die Stimme, und ich zeige, wie man die drei Konten anlegt und
was sie kosten. Dann passt du seine Persönlichkeit an, ein Textdokument
mit einer erfundenen Familie, in das du deine eigene schreibst. Und dann
sagt er seinen ersten Satz.

🤖 *Ein Piepton. Ich hatte mir meinen ersten Auftritt größer vorgestellt.*
