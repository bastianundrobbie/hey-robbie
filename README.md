# Robbie — ein Sprachassistent für zu Hause, selbst gebaut

> **English in short:** Robbie is a hands-free voice assistant: a Raspberry Pi
> with a USB speakerphone in the living room, and a small server in the house
> that listens for the wake word, sends speech to Deepgram, thinks with Claude
> (Anthropic API key), speaks with Cartesia, and can use tools via MCP. This
> repository is the public edition of the code that runs in the author's home,
> reduced to the speech loop plus two example tools. The guide (German) is in
> `docs/`, one chapter per video of the YouTube series
> [Bastian & Robbie](https://www.youtube.com/@BastianUndRobbie).

Robbie hört auf „Hey Robbie", versteht Deutsch, antwortet mit einer eigenen
Stimme und kann Werkzeuge benutzen: Uhrzeit, Wetter, und alles, was du ihm
als Werkzeug dazubaust. Er läuft ohne Bildschirm, ohne App, ohne Cloud-Konto
eines Herstellers. Was er hört, bleibt im Haus, bis die zwei Wörter fallen.

Das hier ist der echte Code aus meinem Haus, bereinigt auf das, was man zum
Nachbauen braucht. Die Anleitung dazu ist eine YouTube-Reihe, sechs Teile,
und jeder Teil ist ein Kapitel in `docs/`.

## Wie er gebaut ist

```
Wohnzimmer                          Büro (Server, Docker)                  Internet
┌──────────────────┐   Ton, ständig   ┌───────────────────────────┐
│ Raspberry Pi     │ ───────────────▶ │ robbie-server :8422       │  nach dem Wake Word:
│ + USB-Speaker-   │   WebSocket      │  • Wake Word (openWakeWord)│ ──▶ Deepgram   (hören)
│   phone          │ ◀─────────────── │  • Turn-Logik, Barge-in   │ ──▶ Anthropic  (denken)
│ station.py       │   Stimme, Beeps  │  • Werkzeuge (MCP)        │ ──▶ Cartesia   (sprechen)
└──────────────────┘                  └───────────────────────────┘
```

- **Der Pi rechnet nichts.** `pi_client/station.py` nimmt Ton auf, schickt
  ihn über eine dauerhafte WebSocket-Verbindung an den Server und spielt ab,
  was zurückkommt. Ein paar Piepstöne als Rückmeldung, das ist alles.
- **Der Server** (`server/`, FastAPI im Docker-Container) hört im Tonstrom auf
  das Wake Word, schneidet den Befehl heraus, lässt ihn transkribieren, holt
  die Antwort vom Modell und streamt die Stimme zurück, Satz für Satz. Du
  kannst Robbie unterbrechen, während er spricht, und „Robbie stopp" wird
  ohne Modell verstanden.
- **Werkzeuge** sind kleine Python-Programme nach dem MCP-Standard
  (`mcp_datetime/`, `mcp_weather/` als Beispiele). Der Server startet sie und
  reicht sie dem Modell als Fähigkeiten weiter.
- **Persönlichkeit** ist ein Textdokument (`server/config/prompt.toml`) mit
  Regeln, Ton und Familienwissen. Die Vorlage enthält eine erfundene Familie.

## Die Anleitung

| Teil | Kapitel | Meilenstein |
|---|---|---|
| 1 | [Hardware und Ohr](docs/teil-1-hardware-und-ohr.md) | Robbie piept auf „Hey Robbie" |
| 2 | Der erste Satz *(folgt)* | Robbie antwortet |
| 3 | Was in der Sekunde passiert *(folgt)* | |
| 4 | Erziehung: der Prompt *(folgt)* | |
| 5 | Werkzeuge: ein eigenes MCP *(folgt)* | |
| 6 | Was es kostet, und wo es aufhört *(folgt)* | |

## Schnellstart

Ausführlich mit allen Prüfschritten steht das in Teil 1. Die Kurzfassung:

**Server** (ein Rechner mit Docker im Heimnetz):

```bash
git clone https://github.com/bastianundrobbie/hey-robbie.git robbie && cd robbie
cp compose.example.yaml compose.yaml
cp .env.example .env                                 # drei Schlüssel, siehe unten
cp server/config/config.example.toml  server/config/config.toml
cp server/config/plugins.example.toml server/config/plugins.toml
cp server/config/prompt.example.toml  server/config/prompt.toml
# Wake-Word-Modell nach models/hey_robbie.onnx legen (siehe models/README.md)
docker compose up -d --build
docker compose logs -f
```

**Pi** (Raspberry Pi OS Lite, Speakerphone am USB):

```bash
git clone https://github.com/bastianundrobbie/hey-robbie.git ~/robbie && cp -r ~/robbie/pi_client ~/pi_client
cd ~/pi_client && python3 -m venv .venv && .venv/bin/pip install websockets
ROBBIE_SERVER_HOST=<SERVER-IP> .venv/bin/python station.py --once   # Handtest
# dann robbie-station.service anpassen (User, Server-Adresse) und einschalten
```

**Schlüssel** in `.env` (alle drei nach Verbrauch abgerechnet, mit
Startguthaben; ohne Schlüssel läuft das Wake Word trotzdem):

| Variable | Dienst | Wofür |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com | denken |
| `DEEPGRAM_API_KEY` | console.deepgram.com | hören |
| `CARTESIA_API_KEY` | play.cartesia.ai | sprechen |

Einstellungen greifen nach `curl -X POST localhost:8422/api/reload`.
Stimmen anhören: `GET /api/voices`, Probe unter `/api/voices/<id>/preview`.

## Hardware

Raspberry Pi 3 B+ oder 4, offizielles Netzteil, microSD, und ein
USB-Speakerphone mit Echo-Unterdrückung. Bei mir läuft ein Jabra Speak 410
aus einer Büroauflösung, gebraucht für zehn bis zwanzig Euro. Die
Schwellwerte in der Compose-Vorlage sind an diesem Gerät kalibriert. Ohne
Server ab etwa sechzig Euro. Die Einkaufsliste mit Preisen steht in Teil 1.

## Was es kostet

Alle drei Dienste rechnen nach Verbrauch ab. Bei einem Familienassistenten,
der ein paar Dutzend Mal am Tag gefragt wird, sind das ein paar Euro im
Monat. Die gemessene Rechnung, je Anbieter getrennt, kommt in Teil 6.

## Grenzen und bekannte Fehlerbilder

- **Kein lokales Modell.** Die Werkzeug-Anbindung hängt an Anthropics
  Claude-Schnittstelle.
  Ein Umbau auf ein lokales Modell ist nicht geplant, aber auch nicht
  ausgeschlossen.
- **Das Wake-Word-Modell liegt nicht bei.** Modelle aus der openWakeWord-
  Bibliothek unterliegen den Bedingungen des Anbieters; du lädst dein eigenes
  (`models/README.md`).
- **Unterspannung am Pi** macht ihn taub, ohne Fehlermeldung. Prüfen mit
  `vcgencmd get_throttled`, Antwort muss `0x0` sein. Das ist ein Netzteil-
  Problem, kein Software-Problem.
- **Playback ist 48 kHz Stereo**, fest verdrahtet für das Speakerphone. Wer
  ein anderes Gerät nimmt und Micky-Maus-Stimmen hört, schaut in
  `pi_client/common.py`.
- **Das Speakerphone gehört einem Programm.** Vor einem Handtest den Dienst
  stoppen: `sudo systemctl stop robbie-station`.
- **Ein Pi pro Server.** Der Server nimmt genau eine Sprechstelle an.
- **Cartesia antwortet mit 402**, wenn das Konto keine Sprech-Verbindung
  mehr erlaubt (Guthaben, Plan). Der Server startet trotzdem, Robbie bleibt
  dann stumm. Im Protokoll: `Cartesia handshake failed: HTTP 402`.
- **`authentication_failed — Not logged in`** im Protokoll heißt: der
  Anthropic-Schlüssel fehlt oder ist falsch. Robbie hört und versteht dann,
  antwortet aber nicht; die Station spielt den Fehlerton.

## Was nicht drin ist

Die Fassung bei mir zu Hause hat mehr: Sprechererkennung, Wecker,
Musiksteuerung, Smart Home, ein Nachschlagewerk, ein Web-Dashboard. Das ist
Stoff für spätere Teile, wenn danach gefragt wird.

## Lizenz

MIT, siehe `LICENSE`. Die Werkzeuge und Dienste, die Robbie benutzt, haben
ihre eigenen Bedingungen.
